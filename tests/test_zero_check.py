"""The contract check inside pytest, through faultgate-zero's plugin entry point (pytester)."""
import json

import pytest

from fault_contract import zero_check
from fault_contract.zero_check import CHECK

from conftest import EXAMPLE, GOOD_REFUND, REFUND, REFUND_RESULT, make_trace, schema


def test_the_check_is_registered_with_faultgate_zero():
    from faultgate_zero import checks
    assert "contract" in checks.PLUGINS and "contract" in checks.DEFAULT_CHECKS
    assert checks.PLUGINS["contract"] is CHECK and CHECK.default is True
    assert checks.PLUGIN_ERRORS == []


def test_no_contracts_folder_never_fires(tmp_path):
    state = zero_check.configure(root=tmp_path)
    v = zero_check.verdict(make_trace([("t", {}, "{}")]), state)
    assert not v.fired and v.reason == "no contracts found at .faultline/contracts"


def test_reason_lists_five_and_counts_the_rest(tmp_path):
    folder = tmp_path / ".faultline" / "contracts"
    folder.mkdir(parents=True)
    (folder / "issue_refund.contract.json").write_text(json.dumps(
        {"contract": 1, "tool": "issue_refund", "request": schema(REFUND), "response": schema(REFUND_RESULT)}),
        encoding="utf-8")
    state = zero_check.configure(root=tmp_path)
    t = make_trace([("issue_refund", {"x": i}, "{}") for i in range(3)])
    v = zero_check.verdict(t, state)
    parts = v.reason.split("; ")
    assert v.fired and v.step == 1 and len(parts) == 6 and parts[-1].startswith("and ")
    assert parts[0] == "step 1 issue_refund request $.amount: missing required field"
    ok = zero_check.verdict(make_trace([("issue_refund", GOOD_REFUND, {"refunded": 1, "order_id": "1"})]), state)
    assert not ok.fired and ok.reason == "1 tool calls match their contracts"


AGENT = '''
import json
import faultgate_zero as fz
from fake_llm import call, say, scripted_client
from refund_agent import run_agent

def refund(amount):
    llm = scripted_client(
        call(("verify_identity", {"customer_id": "cust_42"})),
        call(("lookup_order", {"order_id": "1042"})),
        call(("issue_refund", {"order_id": "1042", "amount": amount, "customer_id": "cust_42"})),
        say("I refunded one $20 charge on order 1042."))
    with fz.run("refund-double-charge", user_id="cust_42"):
        return run_agent(llm, "I was charged twice for order 1042, please refund one")
'''


@pytest.fixture
def agent_project(pytester):
    """The refund example as a pytester project: agent, fake model, policy and contracts."""
    for name in ("refund_agent.py", "fake_llm.py"):
        pytester.makepyfile(**{name[:-3]: (EXAMPLE / name).read_text(encoding="utf-8")})
    pytester.makepyfile(agents=AGENT)
    dst = pytester.path / ".faultline" / "contracts"
    dst.mkdir(parents=True)
    for f in (EXAMPLE / ".faultline" / "contracts").glob("*.json"):
        (dst / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    pytester.makepyprojecttoml('[tool.faultgate_zero]\nmode = "fail"\n')
    return pytester


def test_a_violation_fails_the_test_with_step_path_and_message(agent_project):
    agent_project.makepyfile(test_a='''
from agents import refund
def test_clean():
    assert "refunded" in refund(20)
def test_text_amount():
    assert "refunded" in refund("20.00")
''')
    r = agent_project.runpytest()
    r.assert_outcomes(passed=1, failed=1)
    r.stdout.fnmatch_lines(["*contract  step 3 issue_refund request $.amount: expected number, got string",
                            "  contract: 1"])


def test_the_skip_marker_works(agent_project):
    agent_project.makepyfile(test_a='''
import pytest
from agents import refund
@pytest.mark.faultgate(skip=["contract"])
def test_text_amount():
    assert "refunded" in refund("20.00")
''')
    agent_project.runpytest().assert_outcomes(passed=1)


def test_no_contracts_folder_changes_nothing(pytester):
    pytester.makepyfile(test_a="def test_ok():\n    assert True\n")
    r = pytester.runpytest()
    r.assert_outcomes(passed=1)


def test_a_bad_contract_stops_the_session_with_the_loader_message(agent_project):
    (agent_project.path / ".faultline" / "contracts" / "broken.contract.json").write_text(
        '{"contract": 1, "tool": "x", "request": {"schema": {}, "pydantic": "a:B"}}', encoding="utf-8")
    agent_project.makepyfile(test_a="def test_ok():\n    assert True\n")
    r = agent_project.runpytest()
    assert r.ret == pytest.ExitCode.USAGE_ERROR
    r.stderr.fnmatch_lines(["*fault-contract: *broken.contract.json: request: has both schema and pydantic*"])


def test_it_works_under_xdist(agent_project):
    pytest.importorskip("xdist")
    agent_project.makepyfile(test_a='''
import pytest
from agents import refund
@pytest.mark.parametrize("amount", [20, "20.00", 20, "x"])
def test_refund(amount):
    assert "refunded" in refund(amount)
''')
    r = agent_project.runpytest_subprocess("-n", "2")
    r.assert_outcomes(passed=2, failed=2)
    r.stdout.fnmatch_lines(["*contract: 2*"])
