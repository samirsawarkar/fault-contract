import json
import socket
from pathlib import Path

import pytest
from faultgate_zero import otlp
from faultgate_zero.trace import Span, Step, Trace

from fault_contract import zero_check
from fault_contract.config import Settings
from fault_contract.contract import load_dir

pytest_plugins = ["pytester"]

HERE = Path(__file__).parent
ROOT = HERE.parent
EXAMPLE = ROOT / "examples" / "refund_agent"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Nothing here may touch the network: every connect fails the test."""
    def refuse(*a, **k):
        raise AssertionError("fault-contract tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    yield


@pytest.fixture(autouse=True)
def _fresh_state():
    zero_check.reset()
    yield
    zero_check.reset()


def write_contract(folder: Path, tool: str, **sides) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{tool}.contract.json"
    p.write_text(json.dumps({"contract": 1, "tool": tool, **sides}, indent=2), encoding="utf-8")
    return p


def schema(s) -> dict:
    return {"schema": s}


def contracts_with(tmp_path: Path, **tools) -> tuple:
    """``contracts_with(tmp, issue_refund={"request": schema({...})})`` -> (Contracts, Settings)."""
    folder = tmp_path / ".faultline" / "contracts"
    for tool, sides in tools.items():
        write_contract(folder, tool, **sides)
    return load_dir(folder), Settings(root=tmp_path)


def step(tool: str, args="{}", result="{}", *, index=1, error=False, seen=True) -> Step:
    a = args if isinstance(args, str) else json.dumps(args)
    r = result if isinstance(result, str) else json.dumps(result)
    return Step(index, tool, f"c{index}", a, r, error, seen)


def make_trace(calls, trace_id="trace-1") -> Trace:
    """calls: (tool, args, result[, error]). One execute_tool span per call, under an agent span."""
    spans = [Span("root", None, "invoke_agent agent", 0, 10_000,
                  {"gen_ai.operation.name": "invoke_agent", "faultgate.scenario": trace_id})]
    for i, c in enumerate(calls, start=1):
        tool, args, result = c[:3]
        err = c[3] if len(c) > 3 else False
        spans.append(Span(f"s{i}", "root", f"execute_tool {tool}", i * 10, i * 10 + 5, {
            "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool,
            "gen_ai.tool.call.arguments": args if isinstance(args, str) else json.dumps(args),
            "gen_ai.tool.call.result": result if isinstance(result, str) else json.dumps(result)}, error=err))
    return Trace(trace_id, trace_id, "agent", None, spans)


def write_export(path: Path, traces) -> Path:
    otlp.write(list(traces), path)
    return path


REFUND = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "pattern": "^[0-9]{4,}$"},
        "amount": {"type": "number", "exclusiveMinimum": 0},
        "customer_id": {"type": "string"},
    },
    "required": ["order_id", "amount", "customer_id"],
    "additionalProperties": False,
}
REFUND_RESULT = {"type": "object", "properties": {"refunded": {"type": "number"}, "order_id": {"type": "string"}},
                 "required": ["refunded", "order_id"]}
GOOD_REFUND = {"order_id": "1042", "amount": 20.0, "customer_id": "cust_42"}
