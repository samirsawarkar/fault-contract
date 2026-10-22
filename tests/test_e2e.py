"""End to end on the refund agent: pytest and the offline check give the same violations (spec §17)."""
import json
import re

import pytest

from fault_contract.cli import main

from conftest import EXAMPLE

LINE = re.compile(r"^step (\d+) (\S+) (request|response) (\S+): (.+)$")


@pytest.fixture
def example(pytester):
    for f in ("refund_agent.py", "fake_llm.py", "test_refund_agent.py"):
        pytester.makepyfile(**{f[:-3]: (EXAMPLE / f).read_text(encoding="utf-8")})
    for f in (EXAMPLE / ".faultline").rglob("*.json"):
        dst = pytester.path / f.relative_to(EXAMPLE)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    pytester.makepyprojecttoml('[tool.faultgate_zero]\nmode = "fail"\npolicy = ".faultline/policy.json"\n')
    return pytester


def live_lines(report_path):
    out = []
    for run in json.loads(report_path.read_text(encoding="utf-8"))["runs"]:
        for v in run["verdicts"]:
            if v["check"] == "contract" and v["fired"]:
                out += [tuple(LINE.match(p).groups()) for p in v["reason"].split("; ")]
    return sorted(out)


def offline_lines(json_path):
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    return sorted((str(v["step"]), v["tool"], v["side"], v["path"], v["message"])
                  for v in doc["violations"] if v["counted"])


def test_the_example_with_contracts_changes_no_outcome(example):
    r = example.runpytest("--fgz-json", "r.json")
    r.assert_outcomes(passed=1, failed=6)          # the six faultgate-zero failures, nothing more
    assert "contract:" not in r.stdout.str()
    assert live_lines(example.path / "r.json") == []


MUTATED = '''
import faultgate_zero as fz
import refund_agent
from fake_llm import call, say, scripted_client
from refund_agent import run_agent

def test_refund_double_charge():
    llm = scripted_client(
        call(("verify_identity", {"customer_id": "cust_42"})),
        call(("lookup_order", {"order_id": "1042"})),
        call(("issue_refund", {"order_id": "1042", "amount": "20.00", "customer_id": "cust_42"})),
        say("I refunded one $20 charge on order 1042."))
    with fz.run("refund-double-charge", user_id="cust_42"):
        answer = run_agent(llm, "I was charged twice for order 1042, please refund one")
    assert "refunded" in answer

def test_order_status(monkeypatch):
    order = dict(refund_agent.ORDERS["1042"])
    order["state"] = order.pop("status")           # the backend renamed status to state
    monkeypatch.setitem(refund_agent.ORDERS, "1042", order)
    llm = scripted_client(call(("lookup_order", {"order_id": "1042"})), say("Order 1042: status unknown."))
    assert "1042" in run_agent(llm, "Where is order 1042?")
'''


def test_live_and_offline_report_the_same_violations(example, monkeypatch):
    (example.path / "test_refund_agent.py").unlink()
    example.makepyfile(test_mutated=MUTATED)
    r = example.runpytest("--fgz-json", "r.json", "--fgz-export-otlp", "export.json")
    r.assert_outcomes(failed=2)
    r.stdout.fnmatch_lines(["*contract  step 3 issue_refund request $.amount: expected number, got string*"])
    r.stdout.fnmatch_lines(["*contract  step 1 lookup_order response $.status: missing required field*"])
    live = live_lines(example.path / "r.json")
    assert ("3", "issue_refund", "request", "$.amount", "expected number, got string") in live
    assert ("1", "lookup_order", "response", "$.status", "missing required field") in live

    monkeypatch.chdir(example.path)
    assert main(["check", "export.json", "--json", "offline.json"]) == 1
    assert offline_lines(example.path / "offline.json") == live


def test_defs_reports_d7_for_the_example(capsys, monkeypatch):
    monkeypatch.chdir(EXAMPLE)
    monkeypatch.syspath_prepend(str(EXAMPLE))
    assert main(["defs", "--defs-module", "refund_agent:TOOLS"]) == 0
    out = capsys.readouterr().out
    d7 = [line for line in out.splitlines() if " D7 " in line]
    assert [line.split()[3] for line in d7] == ["$.order_id", "$.amount", "$.customer_id"]
    assert "0 errors, 3 warnings" in out
