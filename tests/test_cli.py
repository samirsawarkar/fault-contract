"""The offline check, its reports, contracts, explain, exit codes."""
import json

import pytest

from fault_contract.check import trace_hash
from fault_contract.cli import main

from conftest import GOOD_REFUND, REFUND, REFUND_RESULT, make_trace, schema, write_contract, write_export

LOOKUP_RESULT = {"type": "object", "properties": {"status": {"enum": ["pending", "shipped", "delivered"]}},
                 "required": ["status"]}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / ".faultline" / "contracts"
    write_contract(folder, "issue_refund", request=schema(REFUND), response=schema(REFUND_RESULT))
    write_contract(folder, "lookup_order", request=schema({"type": "object"}), response=schema(LOOKUP_RESULT))
    return tmp_path


def export(path, *traces):
    return str(write_export(path, traces))


def clean_trace(tid="t-clean"):
    return make_trace([("lookup_order", {"order_id": "1042"}, {"status": "shipped"}),
                       ("issue_refund", GOOD_REFUND, {"refunded": 20.0, "order_id": "1042"})], tid)


def bad_trace(tid="t-bad"):
    return make_trace([("lookup_order", {"order_id": "1042"}, {"status": "lost"}),
                       ("issue_refund", {**GOOD_REFUND, "amount": "20.00"}, {"refunded": 20.0, "order_id": "1042"}),
                       ("send_email", {"to": "a@example.com"}, "sent")], tid)


def test_clean_export_exits_0(project, capsys):
    assert main(["check", export(project / "e.json", clean_trace())]) == 0
    out = capsys.readouterr().out
    assert "format     otlp (detected)   traces 1   tool calls 2   with a contract 2" in out
    assert out.strip().endswith("OK  0 violations")


def test_violations_exit_1_grouped_by_tool_and_side_with_hashed_locations(project, capsys):
    assert main(["check", export(project / "e.json", clean_trace(), bad_trace())]) == 1
    out = capsys.readouterr().out
    h = trace_hash("t-bad")
    assert "issue_refund   request   1 of 2 calls" in out
    assert "lookup_order   response  1 of 2 results" in out
    assert "$.amount" in out and "expected number, got string" in out and f"e.g. trace {h}… step 2" in out
    assert "value is not one of: pending, shipped, delivered" in out
    assert "no contract: send_email (1 call)" in out
    assert out.strip().endswith("FAIL  2 violations in 2 tools")
    assert "t-bad" not in out and "20.00" not in out and "lost" not in out and "a@example.com" not in out


def test_unknown_tools_fail(project, capsys):
    assert main(["check", "--unknown-tools", "fail", export(project / "e.json", make_trace(
        [("send_email", {}, "sent")]))]) == 1
    assert "FAIL  1 violation in 1 tool" in capsys.readouterr().out


def test_tool_filter_and_folder_of_fixtures(project, capsys):
    fx = project / "fixtures"
    fx.mkdir()
    export(fx / "a.otlp.json", bad_trace("t1"))
    export(fx / "b.otlp.json", clean_trace("t2"))
    assert main(["check", str(fx)]) == 1
    capsys.readouterr()
    assert main(["check", str(fx), "--tool", "issue_refund"]) == 1
    out = capsys.readouterr().out
    assert "lookup_order" not in out and "tool calls 2 " in out


def test_json_and_markdown_reports(project, capsys):
    e = export(project / "e.json", bad_trace())
    assert main(["check", e, "--json", "r.json", "--markdown", "r.md"]) == 1
    r = json.loads((project / "r.json").read_text(encoding="utf-8"))
    assert r["tool"] == "fault-contract" and r["summary"]["violations"] == 2 and r["summary"]["failed"]
    assert r["summary"]["no_contract"] == {"send_email": 1}
    v = next(x for x in r["violations"] if x["path"] == "$.amount")
    assert v == {"trace": trace_hash("t-bad"), "step": 2, "tool": "issue_refund", "side": "request",
                 "path": "$.amount", "keyword": "type", "kind": "schema", "message": "expected number, got string",
                 "preview": '"20.00"', "counted": True}
    md = (project / "r.md").read_text(encoding="utf-8")
    assert md.startswith("## fault-contract") and "| issue_refund | request | `$.amount` | type |" in md


def test_usage_and_input_errors_exit_2(project, capsys, tmp_path):
    assert main(["check", str(project / "missing.json")]) == 2
    (project / "bad.json").write_text("{nope", encoding="utf-8")
    assert main(["check", str(project / "bad.json")]) == 2
    assert main(["check", "--contracts", "nowhere", export(project / "e.json", clean_trace())]) == 2
    write_contract(project / ".faultline" / "contracts", "broken", request={"schema": {"type": "strin"}})
    assert main(["check", str(project / "e.json")]) == 2
    err = capsys.readouterr().err
    assert "broken.contract.json: request.schema: invalid schema" in err


def test_contracts_command_lists_and_validates(project, capsys):
    write_contract(project / ".faultline" / "contracts", "dated",
                   request=schema({"type": "object", "properties": {"ts": {"type": "string", "format": "date-time"}}}))
    assert main(["contracts"]) == 0
    out, err = capsys.readouterr()
    assert "issue_refund" in out and "request inline schema" in out and "OK  3 contracts loaded" in out
    if "date-time" in out:
        assert "unchecked formats: dated date-time" in out and "'date-time'" in err


def test_settings_from_pyproject_and_environment(project, capsys, monkeypatch):
    (project / "pyproject.toml").write_text('[tool.fault_contract]\ncoerced = "ignore"\nunknown_tools = "fail"\n',
                                            encoding="utf-8")
    e = export(project / "e.json", make_trace([("send_email", {}, "x")]))
    assert main(["check", e]) == 1
    monkeypatch.setenv("FAULT_CONTRACT_UNKNOWN_TOOLS", "ignore")
    assert main(["check", e]) == 0
    assert main(["check", e, "--unknown-tools", "fail"]) == 1
    (project / "pyproject.toml").write_text('[tool.fault_contract]\nbogus = 1\n', encoding="utf-8")
    assert main(["check", e]) == 2


def test_explain_shows_structure_errors_and_schema_paths(project, capsys):
    e = export(project / "e.json", clean_trace(), bad_trace())
    assert main(["explain", e, "--trace", trace_hash("t-bad"), "--step", "2"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("settings  contracts=") and "coerced=fail" in out
    assert "step 2  issue_refund  contract " in out and "issue_refund.contract.json" in out
    assert "$.amount  string" in out
    assert "$.amount  type  schema  expected number, got string   schema #/properties/amount/type" in out
    assert "step 1 " not in out
    assert main(["explain", e, "--trace", "t-bad"]) == 0
    assert main(["explain", e, "--trace", "zzzz"]) == 2


def test_verbose_prints_stage_counts(project, capsys):
    main(["check", "-v", export(project / "e.json", clean_trace())])
    err = capsys.readouterr().err
    assert "debug  2 contracts loaded" in err and "steps checked 2" in err
