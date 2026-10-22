"""init: drafts from definitions, Pydantic models and traces. Never invents, never overwrites."""
import json

import pytest

from fault_contract import init
from fault_contract.cli import main
from fault_contract.contract import ContractError, load_dir

from conftest import HERE, make_trace, write_export

TOOLS = [
    {"type": "function", "function": {"name": "issue_refund", "parameters": {"type": "object", "properties": {
        "order_id": {"type": "string"}, "amount": {"type": "number"},
        "meta": {"type": "object", "properties": {"note": {"type": "string"}}},
        "lines": {"type": "array", "items": {"type": "object", "properties": {"sku": {"type": "string"}}}}}}}},
    {"type": "function", "function": {"name": "lookup_order", "parameters": {"type": "object", "properties": {
        "order_id": {"type": "string"}}, "required": ["order_id"]}}},
]


def read(folder, tool):
    return json.loads((folder / f"{tool}.contract.json").read_text(encoding="utf-8"))


def test_definitions_are_copied_exactly_plus_additional_properties_false(tmp_path):
    w = init.from_defs(init_defs(), tmp_path)
    c = read(tmp_path, "issue_refund")
    s = c["request"]["schema"]
    assert c["contract"] == 1 and c["tool"] == "issue_refund"
    assert s["additionalProperties"] is False
    assert s["properties"]["meta"]["additionalProperties"] is False
    assert s["properties"]["lines"]["items"]["additionalProperties"] is False
    stripped = json.loads(json.dumps(s).replace(', "additionalProperties": false', ""))
    assert stripped == TOOLS[0]["function"]["parameters"]
    assert "required" not in s                       # never invented
    assert read(tmp_path, "lookup_order")["request"]["schema"]["required"] == ["order_id"]   # copied as it is
    assert any(line.startswith("wrote ") for line in w.lines)
    assert load_dir(tmp_path).found


def init_defs():
    from fault_contract import defs
    return defs.parse(TOOLS)


def test_properties_named_like_keywords_are_closed_too():
    s = init.close_objects({"type": "object", "properties": {
        "enum": {"type": "object", "properties": {"a": {"type": "string"}}},
        "properties": {"type": "object"}, "default": {"type": "string", "default": {"x": {"properties": {}}}}}})
    assert s["properties"]["enum"]["additionalProperties"] is False
    assert s["properties"]["properties"]["additionalProperties"] is False
    assert "additionalProperties" not in s["properties"]          # the map of names itself is not a schema
    assert s["properties"]["default"]["default"] == {"x": {"properties": {}}}   # data is left alone


def test_allow_extra_skips_additional_properties(tmp_path):
    init.from_defs(init_defs(), tmp_path, allow_extra=True)
    assert "additionalProperties" not in read(tmp_path, "issue_refund")["request"]["schema"]


def test_a_note_when_no_definition_marks_a_field_required(tmp_path):
    from fault_contract import defs
    w = init.from_defs(defs.parse(TOOLS[:1]), tmp_path)
    assert any(line.startswith("note  no tool definition marks any field as required") for line in w.lines)


def test_existing_files_are_never_overwritten_without_force(tmp_path):
    init.from_defs(init_defs(), tmp_path)
    p = tmp_path / "issue_refund.contract.json"
    p.write_text(p.read_text(encoding="utf-8").replace('"number"', '"integer"'), encoding="utf-8")
    before = p.read_text(encoding="utf-8")
    w = init.from_defs(init_defs(), tmp_path)
    assert p.read_text(encoding="utf-8") == before
    assert any(line.startswith("skip  ") and "issue_refund" in line for line in w.lines)
    init.from_defs(init_defs(), tmp_path, force=True)
    assert '"integer"' not in p.read_text(encoding="utf-8")


def test_a_missing_side_is_added_to_an_existing_contract(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(HERE))
    init.from_defs(init_defs(), tmp_path)
    w = init.from_pydantic("models:Refund", "issue_refund", "response", tmp_path)
    c = read(tmp_path, "issue_refund")
    assert c["response"] == {"pydantic": "models:Refund", "strict": True} and "request" in c
    assert w.lines == [f"added response in {(tmp_path / 'issue_refund.contract.json').as_posix()}"]


def test_pydantic_snapshot_writes_the_schema_inline(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(HERE))
    init.from_pydantic("models:Refund", "issue_refund", "request", tmp_path, snapshot=True)
    s = read(tmp_path, "issue_refund")["request"]["schema"]
    assert s["required"] == ["order_id", "amount", "customer_id"] and "snapshot of models:Refund" in s["$comment"]
    assert load_dir(tmp_path).get("issue_refund").request.kind == "schema"


def test_pydantic_import_failure_is_an_error(tmp_path):
    with pytest.raises(ContractError, match="cannot import nope_mod"):
        init.from_pydantic("nope_mod:M", "t", "request", tmp_path)


# ---------------------------------------------------------------- inference

def test_inference_required_only_at_100_percent_and_types_unioned():
    s = init.infer([{"a": 1, "b": "x", "c": None}, {"a": 2.5, "b": "y"}, {"a": 3, "b": None}])
    assert s["type"] == "object"
    assert s["required"] == ["a", "b"]
    assert s["properties"]["a"] == {"type": "number"}
    assert s["properties"]["b"] == {"type": ["null", "string"]}
    assert s["properties"]["c"] == {"type": "null"}


def test_inference_never_writes_enum_format_pattern_bounds_or_values():
    payloads = [{"status": "shipped", "when": "2026-10-22", "n": 5, "id": "1042"}] * 50
    s = init.infer(payloads)
    text = json.dumps(s)
    for word in ("enum", "format", "pattern", "minimum", "maximum", "shipped", "2026-10-22", "1042"):
        assert word not in text


def test_inference_stops_at_depth_4():
    s = init.infer([{"a": {"b": {"c": {"d": {"e": 1}}}}}])
    assert s["properties"]["a"]["properties"]["b"]["properties"]["c"]["properties"]["d"] == {}


def test_arrays_merge_their_items():
    s = init.infer([{"xs": [1, "a"]}, {"xs": [{"k": 1}]}, {"xs": []}])
    assert s["properties"]["xs"]["type"] == "array"
    assert set(s["properties"]["xs"]["items"]["type"]) == {"integer", "object", "string"}


def test_private_keys_are_collapsed():
    s = init.infer([{"by_email": {"alice@example.com": {"n": 1}, "bob@example.org": {"n": 2}}}])
    m = s["properties"]["by_email"]
    assert "properties" not in m and m["additionalProperties"] == {"type": "object", "properties": {
        "n": {"type": "integer"}}, "required": ["n"]}
    assert "alice" not in json.dumps(s) and "bob" not in json.dumps(s)


def test_from_traces_writes_response_contracts_with_provenance(tmp_path):
    exp = write_export(tmp_path / "export.json", [make_trace([
        ("lookup_order", {"order_id": "1042"}, {"order_id": "1042", "status": "shipped", "email": "a@example.com"}),
        ("lookup_order", {"order_id": "1043"}, {"order_id": "1043", "status": "delivered"}),
        ("lookup_order", {"order_id": "9"}, "Error: not found"),
        ("ping", {}, "pong")])])
    folder = tmp_path / "contracts"
    w = init.from_traces([exp], "response", folder, all_tools=True)
    c = read(folder, "lookup_order")
    s = c["response"]["schema"]
    assert s["$comment"].startswith("inferred from 2 results in ") and "review before relying on it" in s["$comment"]
    assert s["required"] == ["order_id", "status"]
    assert not (folder / "ping.contract.json").exists()      # "pong" is not JSON
    blob = (folder / "lookup_order.contract.json").read_text(encoding="utf-8")
    for value in ("1042", "shipped", "a@example.com", "delivered"):
        assert value not in blob
    assert any("lookup_order" in line for line in w.lines)
    assert load_dir(folder).get("lookup_order").response is not None


def test_cli_init_from_defs_and_then_from_traces(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tools.json").write_text(json.dumps(TOOLS), encoding="utf-8")
    assert main(["init", "--defs", "tools.json"]) == 0
    out = capsys.readouterr().out
    assert "wrote .faultline/contracts/issue_refund.contract.json" in out.replace(str(tmp_path).replace("\\", "/") + "/", "")
    write_export(tmp_path / "exp.json", [make_trace([("lookup_order", {"order_id": "1"}, {"status": "ok"})])])
    assert main(["init", "--from-traces", "exp.json", "--side", "response", "--all-tools"]) == 0
    c = read(tmp_path / ".faultline" / "contracts", "lookup_order")
    assert "request" in c and "response" in c
    assert main(["init", "--from-traces", "exp.json", "--side", "response", "--all-tools"]) == 0
    assert "skip  " in capsys.readouterr().out
    assert main(["init", "--from-traces", "exp.json"]) == 2   # needs --tool or --all-tools
