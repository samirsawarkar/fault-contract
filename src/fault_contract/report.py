"""Reports for ``check``: terminal, JSON (``--json``) and Markdown (``--markdown``).

The terminal never prints payload values: only paths, messages and locations (a hashed trace id and a
step). JSON and Markdown follow the preview rule: redacted, at most 40 characters.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from faultgate_zero.redact import builtin as redact

from . import __version__
from .check import Found, Result
from .config import Settings
from .validate import counted


def _n(x: int) -> str:
    return f"{x:,}"


def _groups(found: list[Found]) -> dict[tuple[str, str], list[Found]]:
    by: dict[tuple[str, str], list[Found]] = defaultdict(list)
    for f in found:
        by[(f.v.tool, f.v.side)].append(f)
    return by


def terminal(r: Result, settings: Settings, max_examples: int = 1) -> str:
    fmt = ", ".join(r.formats) or "none"
    lines = [f"format     {fmt}{' (detected)' if r.detected else ''}   traces {_n(r.traces)}   "
             f"tool calls {_n(r.tool_calls)}   with a contract {_n(r.with_contract)}", ""]
    shown = [f for f in r.found if f.v.kind != "unknown_tool"]
    groups = _groups(shown)
    width = max((len(f.v.path) for f in shown), default=10)
    mwidth = max((len(f.v.message) for f in shown), default=20)
    for (tool, side) in sorted(groups):
        items = groups[(tool, side)]
        noun = "calls" if side == "request" else "results"
        bad = r.bad_calls[(tool, side)]
        lines.append(f"{redact(tool):<14} {side:<9} {_n(bad)} of {_n(r.calls[(tool, side)])} {noun}"
                     + ("" if bad else "   (coerced only, not counted)"))
        by_rule = Counter((f.v.path, f.v.keyword, f.v.message) for f in items)
        for (path, kw, msg), n in by_rule.most_common():
            share = round(100 * n / len(items))
            where = [f for f in items if (f.v.path, f.v.keyword, f.v.message) == (path, kw, msg)][:max_examples]
            eg = ", ".join(f"trace {f.trace}… step {f.v.step}" for f in where)
            tag = "  (coerced)" if any(f.v.kind == "coerced" for f in where) else ""
            lines.append(f"  {share:>3}%  {path:<{width}}  {msg:<{mwidth}}  e.g. {eg}{tag}")
    if groups:
        lines.append("")
    if r.no_contract:
        listed = ", ".join(f"{redact(t)} ({_n(n)} call{'s' if n != 1 else ''})" for t, n in r.no_contract.most_common())
        verb = "violation" if settings.unknown_tools == "fail" else "ignored"
        lines.append(f"no contract: {listed}   [{verb}: unknown_tools = {settings.unknown_tools}]")
    total = r.counted(settings)
    tools = {f.v.tool for f in total}
    if total:
        lines.append(f"FAIL  {_n(len(total))} violation{'s' if len(total) != 1 else ''} in "
                     f"{len(tools)} tool{'s' if len(tools) != 1 else ''}")
    else:
        extra = len(r.found) - len(total)
        lines.append("OK  0 violations" + (f" ({extra} coerced, not counted: coerced = ignore)" if extra else ""))
    return "\n".join(lines)


def to_dict(r: Result, settings: Settings) -> dict[str, Any]:
    total = r.counted(settings)
    return {
        "tool": "fault-contract", "version": __version__,
        "settings": {"contracts": settings.contracts.as_posix(), "coerced": settings.coerced,
                     "unknown_tools": settings.unknown_tools},
        "summary": {"formats": r.formats, "traces": r.traces, "tool_calls": r.tool_calls,
                    "with_contract": r.with_contract, "violations": len(total),
                    "reported": len(r.found), "failed": bool(total),
                    "no_contract": {redact(t): n for t, n in r.no_contract.most_common()}},
        "violations": [{"trace": f.trace, "step": f.v.step, "tool": redact(f.v.tool), "side": f.v.side,
                        "path": f.v.path, "keyword": f.v.keyword, "kind": f.v.kind, "message": f.v.message,
                        "preview": f.v.preview, "counted": counted(f.v, settings)} for f in r.found],
    }


def write_json(r: Result, settings: Settings, path: str | Path) -> None:
    Path(path).write_text(json.dumps(to_dict(r, settings), indent=2, ensure_ascii=False), encoding="utf-8")


def _cell(s: Any) -> str:
    return str("" if s is None else s).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def markdown(r: Result, settings: Settings) -> str:
    total = r.counted(settings)
    head = (f"**{len(total)} violation{'s' if len(total) != 1 else ''}** in {r.tool_calls} tool calls "
            f"({r.with_contract} with a contract, {r.traces} traces)")
    lines = ["## fault-contract", "", head, ""]
    if not r.found:
        lines.append("Every tool call matches its contract.")
    else:
        lines += ["| Tool | Side | Path | Rule | Message | Calls | Example |", "|---|---|---|---|---|---|---|"]
        rows = Counter((f.v.tool, f.v.side, f.v.path, f.v.keyword, f.v.message) for f in r.found)
        for (tool, side, path, kw, msg), n in sorted(rows.items(), key=lambda x: (-x[1], x[0])):
            eg = next(f for f in r.found if (f.v.tool, f.v.side, f.v.path, f.v.keyword, f.v.message)
                      == (tool, side, path, kw, msg))
            lines.append(f"| {_cell(redact(tool))} | {side} | `{_cell(path)}` | {kw} | {_cell(msg)} | {n} | "
                         f"trace {eg.trace} step {eg.v.step} |")
    if r.no_contract:
        lines += ["", "No contract: " + ", ".join(f"{_cell(redact(t))} ({n})" for t, n in r.no_contract.most_common())]
    return "\n".join(lines) + "\n"


def write_markdown(r: Result, settings: Settings, path: str | Path) -> None:
    Path(path).write_text(markdown(r, settings), encoding="utf-8")
