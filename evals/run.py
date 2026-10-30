"""The acceptance run: prints spec §18's table with measured values. Exit 0 only when every row is met.

    python evals/run.py [--faultgate-zero PATH] [--skip-slow]

Everything runs offline: socket connections are blocked for the whole run.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "corpus"))

import mutate  # noqa: E402
from faultgate_zero import formats, otlp  # noqa: E402
from faultgate_zero.trace import Trace  # noqa: E402

from fault_contract import check, init  # noqa: E402
from fault_contract.cli import main as cli  # noqa: E402
from fault_contract.config import Settings  # noqa: E402
from fault_contract.contract import load_dir  # noqa: E402

PLANTED = ("alice.planted@example.com", "4111 1111 1111 1111", "4111111111111111",
           "sk-proj-PLANTEDabcdefghijklmnop1234", "AKIAPLANTED000000000")


def _block_network() -> None:
    def refuse(*a, **k):
        raise OSError("network blocked by evals/run.py")

    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.socket.connect_ex = refuse  # type: ignore[method-assign]


def _cli(*args: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli(list(args))
    return code, out.getvalue() + err.getvalue()


def _pytest(*args: str, cwd: Path = ROOT) -> tuple[bool, str]:
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args], cwd=cwd,
                       capture_output=True, text=True)
    last = (r.stdout.strip().splitlines() or ["?"])[-1]
    if r.returncode != 0:  # show why, so a failing row can be fixed from the log alone
        print(f"--- pytest {' '.join(args)} in {cwd} failed:\n{r.stdout[-3000:]}{r.stderr[-1500:]}", file=sys.stderr)
    return r.returncode == 0, last


# ---------------------------------------------------------------- rows

def row_mutations():
    rows = mutate.run_all()
    covered = sorted({r.number for r in rows if r.mutated})
    n = sum(r.mutated for r in rows)
    caught = sum(r.matched for r in rows if r.reported)
    clean_elsewhere = sum(r.others_clean for r in rows)
    pyd = [r for r in rows if r.engine == "pydantic" and r.mutated]
    pn, pk = sum(r.mutated for r in pyd), sum(r.kind_ok for r in pyd)
    ok = len(covered) == 12 and caught == n and clean_elsewhere == n
    return [("Catches shape errors", "all 12 types, 100% of mutated calls, correct path and keyword",
             f"{len(covered)} of 12 types; {caught} of {n} calls ({100 * caught / n:.1f}%); "
             f"unmutated calls clean in {clean_elsewhere} of {n} runs", ok),
            ("Rejected versus coerced", "correct kind on every Pydantic mutation",
             f"{pk} of {pn} Pydantic mutations", pk == pn and pn > 0)]


def row_clean():
    counts = mutate.clean_counts(Settings(root=ROOT))
    calls = sum(c for c, _ in counts.values())
    bad = sum(v for _, v in counts.values())
    detail = ", ".join(f"{k} {v} in {c} calls" for k, (c, v) in counts.items())
    return [("No false alarms", "0 violations on the clean corpus", f"{bad} violations ({detail})", bad == 0)]


def row_one_answer():
    ok, last = _pytest("tests/test_e2e.py")
    return [("One answer everywhere", "identical violations in pytest and offline", f"tests/test_e2e.py: {last}", ok)]


def row_defs():
    ok, last = _pytest("tests/test_defs.py")
    cwd = os.getcwd()
    os.chdir(ROOT / "examples" / "refund_agent")
    sys.path.insert(0, os.getcwd())
    try:
        code, out = _cli("defs", "--defs-module", "refund_agent:TOOLS")
    finally:
        os.chdir(cwd)
    errors = out.strip().splitlines()[-1].split(",")[0]
    return [("Definition drift", "D1 to D7 caught on the crafted set; 0 errors on the refund agent",
             f"tests/test_defs.py: {last}; refund agent: {errors}", ok and code == 0 and errors == "0 errors")]


def _planted_traces() -> list[Trace]:
    """Refund agent tool calls whose violating values and dictionary keys carry private data."""
    from faultgate_zero.trace import Span

    email, card, card_digits, key, aws = PLANTED
    calls = [
        ("issue_refund", {"order_id": email, "amount": card, "customer_id": 42, email: key,
                          "note": f"card {card_digits} key {aws}"}, {email: {"card": card}, "refunded": key}),
        ("lookup_order", {"order_id": key, card_digits: email},
         {"order_id": email, "charges": [card, key], "status": aws, "customer_id": {email: card}}),
        ("verify_identity", {"customer_id": [email, card]}, f"verified {email} with card {card}"),
        ("search_orders", {"query": {"email": email}}, [{"order_id": key, "status": 5, aws: email}]),
    ]
    traces = []
    for n in range(3):
        spans = [Span("root", None, "invoke_agent refund", 0, 1_000, {"gen_ai.operation.name": "invoke_agent"})]
        for i, (tool, args, result) in enumerate(calls, start=1):
            spans.append(Span(f"s{i}", "root", f"execute_tool {tool}", i * 10, i * 10 + 5, {
                "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool,
                "gen_ai.tool.call.arguments": json.dumps(args),
                "gen_ai.tool.call.result": result if isinstance(result, str) else json.dumps(result)}))
        traces.append(Trace(f"run-{email}-{n}", f"planted {email}", "refund", email, spans))
    return traces


def row_privacy():
    blobs: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        exp = tmp / "planted.json"
        otlp.write(_planted_traces(), exp)
        contracts = str(ROOT / "examples" / "refund_agent" / ".faultline" / "contracts")
        code, out = _cli("check", str(exp), "--contracts", contracts, "--json", str(tmp / "r.json"),
                         "--markdown", str(tmp / "r.md"), "-v")
        blobs += [out, (tmp / "r.json").read_text(encoding="utf-8"), (tmp / "r.md").read_text(encoding="utf-8")]
        for t in formats.load(exp)[:3]:
            blobs.append(_cli("explain", str(exp), "--trace", t.trace_id, "--contracts", contracts)[1])
        blobs.append(_cli("init", "--from-traces", str(exp), "--side", "response", "--all-tools",
                          "--contracts", str(tmp / "drafts"))[1])
        blobs += [p.read_text(encoding="utf-8") for p in (tmp / "drafts").glob("*.json")]
        state_dir = tmp / "proj"
        (state_dir / ".faultline").mkdir(parents=True)
        shutil.copytree(contracts, state_dir / ".faultline" / "contracts")
        from fault_contract import zero_check
        st = zero_check.configure(root=state_dir)
        blobs += [zero_check.verdict(t, st).reason for t in formats.load(exp)]
        zero_check.reset()
    text = "\n".join(blobs)
    found = [p for p in PLANTED if p in text]
    return [("Private data", "0 planted values in any output, message, report or log",
             f"{len(found)} planted values found in {len(text):,} bytes of output (check, JSON, Markdown, "
             f"explain, init drafts, pytest reasons)", not found and code == 1)]


def row_safe_drafting():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "c"
        paths = [HERE / "corpus" / "refund.otlp.json"]
        init.from_traces(paths, "response", folder, all_tools=True)
        before = {p.name: p.read_text(encoding="utf-8") for p in folder.glob("*.json")}
        for p in folder.glob("*.json"):
            p.write_text(p.read_text(encoding="utf-8").replace("inferred", "reviewed"), encoding="utf-8")
        edited = {p.name: p.read_text(encoding="utf-8") for p in folder.glob("*.json")}
        w = init.from_traces(paths, "response", folder, all_tools=True)
        after = {p.name: p.read_text(encoding="utf-8") for p in folder.glob("*.json")}
        kept = after == edited and all(line.startswith("skip  ") for line in w.lines)
        values: set[str] = set()
        runs, _ = mutate.load_corpus(mutate.CORPORA[0])
        for _, steps in runs:
            for s in steps:
                for raw in (s.result, s.arguments):
                    try:
                        values |= {v for v in _leaves(json.loads(raw)) if isinstance(v, str)}
                    except ValueError:
                        pass
        drafted = "\n".join(before.values())
        keys = set(_keys(json.loads(t) for t in before.values()))
        leaked = sorted(v for v in values - keys if json.dumps(v) in drafted)
        banned = [w for w in ('"enum"', '"format"', '"pattern"', '"minimum"', '"maximum"') if w in drafted]
    return [("Safe drafting", "init never overwrites; inference never writes values or enums",
             f"existing files kept: {kept}; values written: {len(leaked)}; enum/format/pattern/bounds: {len(banned)}",
             kept and not leaked and not banned)]


def _leaves(v):
    if isinstance(v, dict):
        for x in v.values():
            yield from _leaves(x)
    elif isinstance(v, list):
        for x in v:
            yield from _leaves(x)
    else:
        yield v


def _keys(docs):
    for d in docs:
        stack = [d]
        while stack:
            n = stack.pop()
            if isinstance(n, dict):
                yield from n.keys()
                stack.extend(n.values())
            elif isinstance(n, list):
                stack.extend(n)


def row_harmless(zero_path: Path | None):
    if zero_path is None or not (zero_path / "tests").is_dir():
        return [("Harmless when unused", "faultgate-zero's own suite passes unchanged with fault-contract installed",
                 "not run (pass --faultgate-zero PATH to a faultgate-zero checkout)", False)]
    ok, last = _pytest("-x", cwd=zero_path)
    return [("Harmless when unused", "faultgate-zero's own suite passes unchanged with fault-contract installed",
             f"faultgate-zero suite with fault-contract installed: {last}", ok)]


def row_offline(results):
    return [("Offline", "passes with the network blocked",
             "every row above ran with socket connect blocked; the unit suite blocks it too (tests/conftest.py)",
             all(r[3] for r in results if r[0] not in ("Harmless when unused",)))]


def row_speed(skip: bool):
    if skip:
        return [("Speed", "25,000 tool calls in under 10 s on a laptop", "skipped (--skip-slow)", False)]
    runs, contracts = mutate.load_corpus(mutate.CORPORA[2])
    base = [t for t, _ in runs]
    per = sum(len(s) for _, s in runs)
    copies = -(-25_000 // per)
    traces = [Trace(f"{t.trace_id}-{i}", t.scenario, t.agent, t.user_id, t.spans) for i in range(copies) for t in base]
    with tempfile.TemporaryDirectory() as tmp:
        exp = Path(tmp) / "big.json"
        otlp.write(traces, exp)
        settings = Settings(root=ROOT)
        t0 = time.perf_counter()
        r = check.run([exp], contracts, settings)
        secs = time.perf_counter() - t0
        tracemalloc.start()  # a second pass for memory: tracing slows Python down, so it is not timed
        check.run([exp], contracts, settings)
        peak = tracemalloc.get_traced_memory()[1] / 2**20
        tracemalloc.stop()
    machine = f"{platform.system()} {platform.machine()}, Python {platform.python_version()}, {os.cpu_count()} CPUs"
    return [("Speed", "25,000 tool calls checked in under 10 seconds on a laptop",
             f"{r.tool_calls:,} calls in {secs:.2f} s (load + check), peak {peak:.0f} MiB traced; {machine}",
             r.tool_calls >= 25_000 and secs < 10 and not r.found)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--faultgate-zero", type=Path, default=ROOT.parent / "Faultgate-zero")
    ap.add_argument("--skip-slow", action="store_true")
    a = ap.parse_args()
    _block_network()
    rows = []
    for fn in (row_mutations, row_clean, row_one_answer, row_defs, row_privacy, row_safe_drafting):
        rows += fn()
    rows += row_harmless(a.faultgate_zero)
    rows += row_offline(rows)
    rows += row_speed(a.skip_slow)
    w = max(len(r[0]) for r in rows)
    print(f"{'Property':<{w}}  {'Met':<4} Measured")
    for name, target, measured, ok in rows:
        print(f"{name:<{w}}  {'yes' if ok else 'NO':<4} {measured}")
        print(f"{'':<{w}}       target: {target}")
    met = sum(r[3] for r in rows)
    print(f"\n{met} of {len(rows)} rows met")
    return 0 if met == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
