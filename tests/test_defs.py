"""defs: three definition formats, rules D1-D7, nesting, Pydantic, the not-compared list."""
import json

import pytest

from fault_contract import defs
from fault_contract.cli import main
from fault_contract.config import Settings

from conftest import HERE, contracts_with, schema


def obj(props, required=None, extra=None):
    s = {"type": "object", "properties": props}
    if required is not None:
        s["required"] = required
    if extra is not None:
        s["additionalProperties"] = extra
    return s


def run(tmp_path, d_params, c_side, **kw):
    cs, st = contracts_with(tmp_path, t={"request": c_side})
    return defs.run({"t": d_params}, cs, kw.get("settings", st))


def rules(findings):
    return [f.rule for f in findings if f.level != "info"]


def test_three_definition_formats_parse():
    params = obj({"a": {"type": "string"}})
    got = defs.parse([{"type": "function", "function": {"name": "chat", "parameters": params}},
                      {"type": "function", "name": "responses", "parameters": params},
                      {"name": "anthropic", "input_schema": params}])
    assert got == {"chat": params, "responses": params, "anthropic": params}
    with pytest.raises(defs.DefsError, match="not an OpenAI Chat"):
        defs.parse([{"foo": 1}])
    with pytest.raises(defs.DefsError, match="defined twice"):
        defs.parse([{"name": "x", "input_schema": {}}, {"name": "x", "input_schema": {}}])


CASES = {
    # rule: (definition params, contract schema, fixed definition params)
    "D3": (obj({"a": {"type": "string"}}), obj({"a": {"type": "string"}, "b": {"type": "string"}}, ["b"]),
           obj({"a": {"type": "string"}, "b": {"type": "string"}}, ["b"])),
    "D4": (obj({"a": {"type": "string"}, "z": {"type": "string"}}), obj({"a": {"type": "string"}}, extra=False),
           obj({"a": {"type": "string"}})),
    "D5": (obj({"a": {"type": "string"}}), obj({"a": {"type": "number"}}), obj({"a": {"type": "number"}})),
    "D6": (obj({"s": {"type": "string", "enum": ["a", "b", "x"]}}), obj({"s": {"enum": ["a", "b"]}}),
           obj({"s": {"type": "string", "enum": ["a", "b"]}})),
    "D7": (obj({"a": {"type": "string"}}), obj({"a": {"type": "string"}}, ["a"]), obj({"a": {"type": "string"}}, ["a"])),
}


@pytest.mark.parametrize("rule", sorted(CASES))
def test_each_rule_fires_on_its_pair_and_is_quiet_when_fixed(tmp_path, rule):
    d, c, fixed = CASES[rule]
    assert rules(run(tmp_path, d, schema(c))) == [rule]
    assert rules(run(tmp_path / "fixed", fixed, schema(c))) == []


def test_d1_and_d2(tmp_path):
    cs, st = contracts_with(tmp_path, stale={"request": schema({})})
    found = defs.run({"new_tool": {}}, cs, st)
    assert {(f.tool, f.rule, f.level) for f in found} == {("new_tool", "D1", "warning"), ("stale", "D2", "warning")}
    found = defs.run({"new_tool": {}}, cs, Settings(root=tmp_path, unknown_tools="fail"))
    assert ("new_tool", "D1", "error") in {(f.tool, f.rule, f.level) for f in found}


def test_integer_definition_fits_a_number_contract(tmp_path):
    assert rules(run(tmp_path, obj({"n": {"type": "integer"}}), schema(obj({"n": {"type": "number"}})))) == []


def test_nested_objects_and_arrays_are_compared(tmp_path):
    d = obj({"meta": obj({"x": {"type": "string"}}), "lines": {"type": "array", "items": obj({"q": {"type": "string"}})}})
    c = obj({"meta": obj({"x": {"type": "integer"}}), "lines": {"type": "array", "items": obj(
        {"q": {"type": "string"}, "sku": {"type": "string"}}, ["sku"])}})
    found = run(tmp_path, d, schema(c))
    msgs = [f.message for f in found if f.level != "info"]
    assert any(m.startswith("$.meta.x is string in the tool definition") for m in msgs)
    assert any(m.startswith("$.lines[].sku is required by the contract") for m in msgs)


def test_pydantic_contracts_compare_through_the_model_schema(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(HERE))
    d = obj({"order_id": {"type": "string"}, "amount": {"type": "string"}, "customer_id": {"type": "string"}})
    found = run(tmp_path, d, {"pydantic": "models:Refund"})
    got = {(f.rule, f.message.split(" ")[0]) for f in found if f.level != "info"}
    assert ("D5", "$.amount") in got and ("D7", "$.order_id") in got and ("D7", "$.customer_id") in got


def test_refs_in_the_contract_are_followed(tmp_path):
    c = {"$defs": {"id": {"type": "number"}}, "type": "object", "properties": {"a": {"$ref": "#/$defs/id"}}}
    assert rules(run(tmp_path, obj({"a": {"type": "string"}}), schema(c))) == ["D5"]


def test_a_ref_with_sibling_properties_keeps_both(tmp_path):
    # both the referenced properties and the sibling ones apply when validating; defs must see both
    c = {"$defs": {"money": {"type": "object", "properties": {"amount": {"type": "number"}}, "required": ["amount"]}},
         "$ref": "#/$defs/money", "properties": {"note": {"type": "string"}}, "required": ["note"]}
    d = obj({"amount": {"type": "string"}, "note": {"type": "string"}}, ["note"])
    found = run(tmp_path, d, schema(c))
    assert ("D5", "$.amount") in {(f.rule, f.message.split(" ")[0]) for f in found}
    assert ("D7", "$.amount") in {(f.rule, f.message.split(" ")[0]) for f in found}


def test_a_ref_whose_siblings_conflict_is_listed_as_not_compared(tmp_path):
    c = {"$defs": {"s": {"type": "string"}}, "type": "object", "properties": {"a": {"$ref": "#/$defs/s", "type": "integer"}}}
    found = run(tmp_path, obj({"a": {"type": "boolean"}}), schema(c))
    assert any(f.level == "info" and "$.a allOf" in f.message for f in found)


def test_pydantic_refs_with_siblings_keep_both(monkeypatch, tmp_path):
    from fault_contract.contract import resolve_refs
    s = {"$defs": {"M": {"type": "object", "properties": {"x": {"type": "number"}}}},
         "$ref": "#/$defs/M", "properties": {"y": {"type": "string"}}}
    out = resolve_refs(s)
    assert set(out["properties"]) == {"x", "y"}


def test_not_compared_list_appears(tmp_path):
    c = obj({"a": {"type": "string", "pattern": "^x$"}, "b": {"anyOf": [{"type": "string"}, {"type": "null"}]},
             "d": {"type": "string", "format": "date"}, "n": {"type": "number", "minimum": 0}})
    found = run(tmp_path, obj({"a": {"type": "string"}, "b": {}, "d": {"type": "string"}, "n": {"type": "number"}}),
                schema(c))
    [info] = [f for f in found if f.level == "info"]
    for needle in ("$.a pattern", "$.b anyOf", "$.d format", "$.n minimum"):
        assert needle in info.message


def test_cli_exit_codes_and_strict(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    contracts_with(tmp_path, t={"request": schema(obj({"a": {"type": "string"}}, ["a"]))})
    (tmp_path / "defs.json").write_text(json.dumps([{"name": "t", "input_schema": obj({"a": {"type": "string"}})}]),
                                        encoding="utf-8")
    assert main(["defs", "--defs", "defs.json"]) == 0
    out = capsys.readouterr().out
    assert "t  WARN  D7  $.a is required by the contract, but the tool definition marks it optional" in out
    assert "0 errors, 1 warning   (--strict makes warnings fail)" in out
    assert main(["defs", "--defs", "defs.json", "--strict"]) == 1
    (tmp_path / "defs.json").write_text(json.dumps([{"name": "t", "input_schema": obj({"a": {"type": "number"}})}]),
                                        encoding="utf-8")
    assert main(["defs", "--defs", "defs.json"]) == 1
    assert main(["defs", "--defs", "missing.json"]) == 2
    assert main(["defs", "--defs-module", "nope_module_xyz:TOOLS"]) == 2
