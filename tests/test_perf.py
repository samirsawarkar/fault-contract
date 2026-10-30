"""Nightly performance test: 25,000 tool calls through check in under 10 seconds (run with -m slow)."""
import json
import platform
import time
import tracemalloc

import pytest

from fault_contract import check
from fault_contract.config import Settings
from fault_contract.contract import load_dir

from conftest import GOOD_REFUND, REFUND, REFUND_RESULT, make_trace, schema, write_contract, write_export


@pytest.mark.slow
def test_25000_tool_calls_in_under_10_seconds(tmp_path, record_property):
    folder = tmp_path / "contracts"
    write_contract(folder, "issue_refund", request=schema(REFUND), response=schema(REFUND_RESULT))
    calls = [("issue_refund", GOOD_REFUND, {"refunded": 20.0, "order_id": "1042"})] * 25
    exp = write_export(tmp_path / "big.json", [make_trace(calls, f"t{i}") for i in range(1000)])
    contracts, settings = load_dir(folder), Settings(root=tmp_path)
    t0 = time.perf_counter()
    r = check.run([exp], contracts, settings)
    secs = time.perf_counter() - t0
    tracemalloc.start()
    check.run([exp], contracts, settings)
    peak = tracemalloc.get_traced_memory()[1] / 2**20
    tracemalloc.stop()
    record_property("perf", json.dumps({"calls": r.tool_calls, "seconds": round(secs, 2), "peak_mib": round(peak),
                                        "machine": f"{platform.system()} {platform.machine()} {platform.python_version()}"}))
    assert r.tool_calls == 25_000 and not r.found
    assert secs < 10, f"{secs:.1f}s"
