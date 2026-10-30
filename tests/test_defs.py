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


@pytest.mark.parametrize("d_enum, c_enum, fires", [
    ([1], [True], True), ([0], [False], True), ([True], [1], True), ([1.0], [1], False),
    ([{"a": 1}], [{"a": True}], True), ([[1, 2]], [[1, 2]], False), (["x", None], ["x"], True),
])
def test_d6_compares_values_as_json_schema_does(tmp_path, d_enum, c_enum, fires):
    found = run(tmp_path, obj({"v": {"enum": d_enum}}), schema(obj({"v": {"enum": c_enum}})))
    assert ("D6" in rules(found)) is fires


def test_big_integers_keep_their_precision(tmp_path):
    from fault_contract.contract import json_equal, merge_ref
    assert not json_equal(9007199254740992, 9007199254740993) and json_equal(10**400, 10**400)
    assert json_equal(2, 2.0) and not json_equal(float("nan"), 1)
    assert rules(run(tmp_path, obj({"v": {"enum": [9007199254740993]}}),
                     schema(obj({"v": {"enum": [9007199254740992]}})))) == ["D6"]
    assert merge_ref({"const": 9007199254740992}, {"const": 9007199254740993}) == {
        "allOf": [{"const": 9007199254740992}, {"const": 9007199254740993}]}


@pytest.mark.parametrize("with_side", [True, False])
def test_literal_data_is_never_expanded_as_schema(tmp_path, with_side):
    from fault_contract.contract import resolve_refs
    s = {"$defs": {"x": {"type": "string"}}, "type": "object", "properties": {
        "v": {"enum": [{"$ref": "#/$defs/x"}]}, "enum": {"$ref": "#/$defs/x"}}}
    if with_side:
        cs, _ = contracts_with(tmp_path, t={"request": schema(s)})
        side = cs.get("t").request
        out = resolve_refs(side.json_schema(), side)
    else:
        out = resolve_refs(s)
    assert out["properties"]["v"]["enum"] == [{"$ref": "#/$defs/x"}]       # data stays data
    assert out["properties"]["enum"]["type"] == "string"                   # a property named enum is a schema
    found = run(tmp_path / "d", obj({"v": {"enum": [{"type": "string"}]}}), schema(s))
    assert "D6" in rules(found)


def test_nested_enums_compare_as_json_too(tmp_path):
    d = obj({"m": obj({"flag": {"enum": [1]}})})
    c = obj({"m": obj({"flag": {"enum": [True]}})})
    assert rules(run(tmp_path, d, schema(c))) == ["D6"]


def test_a_ref_sibling_const_true_does_not_merge_with_const_1(tmp_path):
    from fault_contract.contract import merge_ref
    assert merge_ref({"const": 1}, {"const": True}) == {"allOf": [{"const": 1}, {"const": True}]}


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


@pytest.mark.parametrize("closed_side", ["target", "sibling"])
def test_a_closed_object_behind_a_ref_is_not_reopened_by_sibling_properties(tmp_path, closed_side):
    money = {"type": "object", "properties": {"amount": {"type": "number"}}}
    sib = {"properties": {"note": {"type": "string"}}}
    (money if closed_side == "target" else sib)["additionalProperties"] = False
    c = {"$defs": {"money": money}, "$ref": "#/$defs/money", **sib}
    found = run(tmp_path, obj({"amount": {"type": "number"}, "note": {"type": "string"}}), schema(c))
    # the contract rejects {"amount": 1, "note": "x"}; defs must not call the pair OK silently
    assert any(f.level == "info" and "allOf" in f.message for f in found)
    cs, _ = contracts_with(tmp_path / "v", t={"request": schema(c)})
    from fault_contract.validate import validate_side
    assert validate_side(cs.get("t").request, '{"amount": 1, "note": "x"}', "t", 1)


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
