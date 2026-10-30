"""``fault-contract check``: validate every tool call in trace exports and fixture folders."""
from __future__ import annotations

import hashlib
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from faultgate_zero import formats
from faultgate_zero.trace import Trace

from .config import Settings
from .contract import Contracts
from .validate import Violation, counted, response_checked, validate_trace


class InputError(ValueError):
    """An export or folder that cannot be read. CLI exit code 2."""


def trace_hash(trace_id: str) -> str:
    """The id printed for a trace: a hash, so ids that carry data never appear in output."""
    return hashlib.sha256(trace_id.encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True)
class Found:
    trace: str        # hashed trace id
    v: Violation


@dataclass
class Result:
    formats: list[str] = field(default_factory=list)
    detected: bool = True
    traces: int = 0
    tool_calls: int = 0
    with_contract: int = 0
    calls: Counter = field(default_factory=Counter)        # (tool, side) -> calls checked on that side
    bad_calls: Counter = field(default_factory=Counter)    # (tool, side) -> calls with a counted violation
    no_contract: Counter = field(default_factory=Counter)  # tool -> calls
    found: list[Found] = field(default_factory=list)

    def counted(self, settings: Settings) -> list[Found]:
        return [f for f in self.found if counted(f.v, settings)]


def expand(paths: list[str | Path]) -> list[Path]:
    """Files to read: a folder means every ``*.json`` file in it."""
    out: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            files = sorted(x for x in p.glob("*.json") if x.is_file())
            if not files:
                raise InputError(f"{p}: no *.json files in this folder")
            out += files
        elif p.is_file():
            out.append(p)
        else:
            raise InputError(f"{p}: no such file or folder")
    return out


def load_traces(paths: list[str | Path], fmt: str = "auto") -> tuple[list[Trace], list[str]]:
    traces: list[Trace] = []
    seen: list[str] = []
    for f in expand(paths):
        try:
            got = formats.load(f, fmt)
        except ValueError as exc:
            raise InputError(str(exc)) from None
        traces += got
        name = fmt if fmt != "auto" else _detect(f)
        if name not in seen:
            seen.append(name)
    return traces, seen


def _detect(f: Path) -> str:
    import json

    try:
        return formats.detect(json.loads(f.read_text(encoding="utf-8")))
    except ValueError:
        return "unknown"


def check_traces(traces: list[Trace], contracts: Contracts, settings: Settings, tool: str | None = None) -> Result:
    r = Result(traces=len(traces))
    for t in traces:
        steps = [s for s in t.steps if tool is None or s.tool == tool]
        th = trace_hash(t.trace_id)
        vs = validate_trace(t, contracts, settings, steps)
        r.found += [Found(th, v) for v in vs]
        bad = {(v.step, v.side) for v in vs if counted(v, settings)}
        for s in steps:
            r.tool_calls += 1
            c = contracts.get(s.tool)
            if c is None:
                r.no_contract[s.tool] += 1
                continue
            r.with_contract += 1
            if c.request:
                r.calls[(s.tool, "request")] += 1
            if response_checked(s, c):
                r.calls[(s.tool, "response")] += 1
            for side in ("request", "response"):
                if (s.index, side) in bad:
                    r.bad_calls[(s.tool, side)] += 1
    return r


def run(paths: list[str | Path], contracts: Contracts, settings: Settings, fmt: str = "auto",
        tool: str | None = None) -> Result:
    t0 = time.perf_counter()
    traces, seen = load_traces(paths, fmt)
    t1 = time.perf_counter()
    r = check_traces(traces, contracts, settings, tool)
    r.formats, r.detected = seen, fmt == "auto"
    if settings.debug:
        print(f"debug  contracts loaded {len(contracts)}  traces loaded {len(traces)} in {t1 - t0:.2f}s  "
              f"steps checked {r.tool_calls}  violations {len(r.found)} in {time.perf_counter() - t1:.2f}s",
              file=sys.stderr)
    return r
