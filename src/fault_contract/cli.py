"""``fault-contract`` command line. Exit codes: 0 clean, 1 violations or errors found, 2 usage or input error."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from faultgate_zero import formats as zformats
from faultgate_zero.redact import builtin as redact

from . import __version__, check, defs, init, report
from .check import InputError, trace_hash
from .config import ConfigError, Settings, resolve
from .contract import Contracts, ContractError, load_dir, resolve_refs
from .validate import json_type, preview, response_checked, validate_side


def _settings(a: argparse.Namespace) -> Settings:
    return resolve({"contracts": getattr(a, "contracts", None), "coerced": getattr(a, "coerced", None),
                    "unknown_tools": getattr(a, "unknown_tools", None), "debug": getattr(a, "verbose", False)})


def _contracts(s: Settings) -> Contracts:
    t0 = time.perf_counter()
    c = load_dir(s.contracts_dir)
    if s.debug:
        print(f"debug  {len(c)} contracts loaded from {s.contracts_dir} in {time.perf_counter() - t0:.2f}s",
              file=sys.stderr)
    for note in c.notes():
        print(f"note  {note}", file=sys.stderr)
    return c


def _shared(p: argparse.ArgumentParser, *, fmt: bool = True) -> None:
    p.add_argument("--contracts", metavar="DIR", help="contracts folder (default: .faultline/contracts)")
    if fmt:
        p.add_argument("--format", dest="fmt", default="auto", choices=zformats.FORMATS,
                       help="trace format (default: auto)")
    p.add_argument("--coerced", choices=("fail", "ignore"), help="Pydantic calls accepted only by converting values")
    p.add_argument("--unknown-tools", dest="unknown_tools", choices=("ignore", "fail"),
                   help="tool calls with no contract")
    p.add_argument("-v", "--verbose", action="store_true", help="stage counts and timings on stderr")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="fault-contract",
                                 description="Check every tool call an agent makes against a per-tool contract.")
    ap.add_argument("--version", action="version", version=f"fault-contract {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="draft contracts from tool definitions, a Pydantic model or a trace export")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--defs", metavar="FILE", help="tool definitions JSON (OpenAI Chat, Responses or Anthropic)")
    src.add_argument("--defs-module", metavar="MOD:ATTR", help="import the definitions, e.g. refund_agent:TOOLS")
    src.add_argument("--pydantic", metavar="MOD:MODEL", help="a Pydantic model the tool validates with")
    src.add_argument("--from-traces", metavar="PATH", nargs="+", help="infer from the results seen in exports")
    p.add_argument("--tool", help="tool name (--pydantic, --from-traces)")
    p.add_argument("--all-tools", action="store_true", help="every tool in the export (--from-traces)")
    p.add_argument("--side", choices=("request", "response"), help="which side to write (--pydantic, --from-traces)")
    p.add_argument("--snapshot", action="store_true", help="write the model's JSON Schema inline (--pydantic)")
    p.add_argument("--allow-extra", action="store_true", help="do not add additionalProperties: false (--defs)")
    p.add_argument("--force", action="store_true", help="replace a side that already exists")
    _shared(p)

    p = sub.add_parser("check", help="validate tool calls in exports and fixture folders")
    p.add_argument("paths", nargs="+", metavar="PATH")
    p.add_argument("--json", metavar="PATH", help="write every violation as JSON")
    p.add_argument("--markdown", metavar="PATH", help="write a Markdown summary (CI job summary)")
    p.add_argument("--tool", help="check one tool only")
    p.add_argument("--max-examples", type=int, default=1, metavar="N", help="example locations per rule (default 1)")
    _shared(p)

    p = sub.add_parser("defs", help="compare tool definitions with contracts")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--defs", metavar="FILE")
    g.add_argument("--defs-module", metavar="MOD:ATTR")
    p.add_argument("--strict", action="store_true", help="warnings fail too")
    _shared(p, fmt=False)

    p = sub.add_parser("explain", help="why did this call fail? one trace, step by step")
    p.add_argument("path", metavar="PATH")
    p.add_argument("--trace", required=True, metavar="ID", help="trace id, or the hashed id check prints")
    p.add_argument("--step", type=int, metavar="N")
    _shared(p)

    p = sub.add_parser("contracts", help="list and validate the loaded contracts")
    _shared(p, fmt=False)
    return ap


# ---------------------------------------------------------------- commands

def cmd_init(a: argparse.Namespace) -> int:
    s = _settings(a)
    folder = s.contracts_dir
    if a.defs or a.defs_module:
        w = init.from_defs(defs.load(a.defs, a.defs_module), folder, allow_extra=a.allow_extra, force=a.force)
    elif a.pydantic:
        if not a.tool or not a.side:
            raise InputError("init --pydantic needs --tool NAME and --side request|response")
        w = init.from_pydantic(a.pydantic, a.tool, a.side, folder, snapshot=a.snapshot, force=a.force)
    else:
        w = init.from_traces(a.from_traces, a.side or "response", folder, tool=a.tool, all_tools=a.all_tools,
                             fmt=a.fmt, force=a.force)
    print("\n".join(w.lines))
    return 0


def cmd_check(a: argparse.Namespace) -> int:
    s = _settings(a)
    c = _contracts(s)
    if not c.found:
        raise InputError(f"no contracts found at {s.contracts_dir}; draft them with fault-contract init")
    r = check.run(a.paths, c, s, a.fmt, a.tool)
    print(report.terminal(r, s, max(1, a.max_examples)))
    if a.json:
        report.write_json(r, s, a.json)
        print(f"JSON report written to {a.json}")
    if a.markdown:
        report.write_markdown(r, s, a.markdown)
        print(f"Markdown summary written to {a.markdown}")
    return 1 if r.counted(s) else 0


def cmd_defs(a: argparse.Namespace) -> int:
    s = _settings(a)
    c = _contracts(s)
    d = defs.load(a.defs, a.defs_module)
    found = defs.run(d, c, s)
    print(defs.text(found, sorted(set(d) | set(c.by_tool)), a.strict))
    return 1 if defs.failed(found, a.strict) else 0


def _structure(v: Any, path: str = "$", depth: int = 0, out: list[str] | None = None) -> list[str]:
    """Keys and types, with redacted previews of leaves. Never more than 40 lines."""
    out = [] if out is None else out
    if len(out) >= 40:
        return out
    if isinstance(v, dict) and v and depth < 6:
        for k, x in v.items():
            key = redact(str(k))
            _structure(x, f"{path}.{key}" if key.isidentifier() else f"{path}[{json.dumps(key)}]", depth + 1, out)
    elif isinstance(v, list) and v and depth < 6:
        out.append(f"{path}  array of {len(v)}")
        _structure(v[0], f"{path}[0]", depth + 1, out)
    else:
        out.append(f"{path}  {json_type(v)}  {preview(v)}")
    return out


def _payload(label: str, raw: str) -> list[str]:
    try:
        v = json.loads(raw if raw.strip() or label == "result" else "{}")
    except ValueError:
        return [f"    {label}: not JSON  {preview(raw)}"]
    return [f"    {label}"] + [f"      {line}" for line in _structure(v)]


def cmd_explain(a: argparse.Namespace) -> int:
    s = _settings(a)
    c = _contracts(s)
    try:
        traces = zformats.load(a.path, a.fmt)
    except ValueError as exc:
        raise InputError(str(exc)) from None
    want = a.trace.rstrip("…")
    hits = [t for t in traces if t.trace_id == a.trace or trace_hash(t.trace_id).startswith(want)]
    if not hits:
        raise InputError(f"no trace {a.trace!r} in {a.path}")
    if len(hits) > 1:
        raise InputError(f"{a.trace!r} matches {len(hits)} traces; give more of the id")
    t = hits[0]
    print(f"settings  {s.line()} format={a.fmt}")
    steps = t.steps
    print(f"trace {trace_hash(t.trace_id)}  {len(steps)} steps")
    for st in steps:
        if a.step is not None and st.index != a.step:
            continue
        ct = c.get(st.tool)
        where = ct.path.as_posix() if ct else "no contract"
        print(f"\nstep {st.index}  {redact(st.tool)}  contract {where}")
        print("\n".join(_payload("arguments", st.arguments)))
        if st.result_seen:
            print("\n".join(_payload("result", st.result)))
        else:
            print("    result: not seen (the run ended before it went back to the model)")
        if ct is None:
            continue
        for side in (ct.request, ct.response):
            if side is None:
                continue
            if side.name == "response" and not response_checked(st, ct):
                print("  response  not checked (no result, or the result is an error)")
                continue
            raw = (st.arguments if st.arguments.strip() else "{}") if side.name == "request" else st.result
            vs = validate_side(side, raw, st.tool, st.index)
            print(f"  {side.name}  {side.source}: {'OK' if not vs else f'{len(vs)} violation(s)'}")
            for v in vs:
                extra = f"   schema {v.schema_path}" if v.schema_path else ""
                print(f"    {v.path}  {v.keyword}  {v.kind}  {v.message}{extra}"
                      + (f"   value {v.preview}" if v.preview else ""))
            if side.kind == "pydantic" and vs:
                lax = all(v.kind == "coerced" for v in vs)
                print(f"    strict validation failed; lax validation {'passed (coerced)' if lax else 'failed too'}")
    return 0


def cmd_contracts(a: argparse.Namespace) -> int:
    s = _settings(a)
    c = _contracts(s)
    if not c.found:
        print(f"no contracts found at {s.contracts_dir}")
        return 0
    print(f"contracts  {s.contracts_dir.as_posix()}  ({len(c)} file{'s' if len(c) != 1 else ''})")
    width = max(len(t) for t in c.by_tool)
    for tool, ct in sorted(c.by_tool.items()):
        req = ct.request.source if ct.request else "-"
        res = ct.response.source if ct.response else "-"
        print(f"{tool:<{width}}  request {req:<28}  response {res:<28}  {ct.path.name}")
    unchecked = [(t, f) for t, ct in sorted(c.by_tool.items()) for sd in (ct.request, ct.response) if sd
                 for f in sd.unchecked_formats]
    if unchecked:
        print("unchecked formats: " + ", ".join(f"{t} {f}" for t, f in unchecked))
    print(f"OK  {len(c)} contract{'s' if len(c) != 1 else ''} loaded")
    return 0


COMMANDS = {"init": cmd_init, "check": cmd_check, "defs": cmd_defs, "explain": cmd_explain, "contracts": cmd_contracts}


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    try:
        return COMMANDS[a.cmd](a)
    except (ContractError, ConfigError, InputError, defs.DefsError, zformats.FormatError) as exc:
        print(f"fault-contract: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
