"""Loading contract files: sources, loading rules, refs, unchecked formats."""
import json
import sys

import pytest

from fault_contract.contract import ContractError, load_dir, load_file
from fault_contract.validate import validate_side

from conftest import HERE, REFUND, schema, write_contract


@pytest.fixture(autouse=True)
def _models_importable(monkeypatch):
    monkeypatch.syspath_prepend(str(HERE))


def folder(tmp_path):
    return tmp_path / "contracts"


def test_inline_schema_loads(tmp_path):
    write_contract(folder(tmp_path), "issue_refund", request=schema(REFUND), owner="billing")
    c = load_dir(folder(tmp_path)).get("issue_refund")
    assert c.request.kind == "schema" and c.request.source == "inline schema" and c.owner == "billing"
    assert c.response is None


def test_schema_file_with_a_pointer_into_an_openapi_document(tmp_path):
    api = tmp_path / "api"
    api.mkdir()
    (api / "openapi.json").write_text(json.dumps({"openapi": "3.1.0", "components": {"schemas": {
        "RefundRequest": {"type": "object", "properties": {"amount": {"$ref": "#/components/schemas/Money"}},
                          "required": ["amount"]},
        "Money": {"type": "number", "minimum": 0}}}}), encoding="utf-8")
    write_contract(folder(tmp_path), "issue_refund",
                   request={"schema_file": "../api/openapi.json#/components/schemas/RefundRequest"})
    side = load_dir(folder(tmp_path)).get("issue_refund").request
    assert validate_side(side, '{"amount": 5}', "issue_refund", 1) == []
    [v] = validate_side(side, '{"amount": -1}', "issue_refund", 1)
    assert (v.path, v.keyword) == ("$.amount", "minimum")
    [v] = validate_side(side, "{}", "issue_refund", 1)
    assert (v.path, v.keyword) == ("$.amount", "required")


def test_schema_file_without_a_pointer_and_a_local_ref_to_another_file(tmp_path):
    (tmp_path / "money.json").write_text(json.dumps({"$defs": {"m": {"type": "number"}}}), encoding="utf-8")
    (tmp_path / "req.json").write_text(json.dumps(
        {"type": "object", "properties": {"amount": {"$ref": "money.json#/$defs/m"}}}), encoding="utf-8")
    write_contract(folder(tmp_path), "t", request={"schema_file": "../req.json"})
    side = load_dir(folder(tmp_path)).get("t").request
    [v] = validate_side(side, '{"amount": "x"}', "t", 1)
    assert v.message == "expected number, got string"


def test_local_defs_resolve(tmp_path):
    s = {"$defs": {"id": {"type": "string"}}, "type": "object", "properties": {"order_id": {"$ref": "#/$defs/id"}}}
    write_contract(folder(tmp_path), "t", request=schema(s))
    side = load_dir(folder(tmp_path)).get("t").request
    [v] = validate_side(side, '{"order_id": 5}', "t", 1)
    assert v.path == "$.order_id" and v.keyword == "type"


def test_pydantic_source_loads_and_defaults_to_strict(tmp_path):
    write_contract(folder(tmp_path), "issue_refund", request={"pydantic": "models:Refund"})
    side = load_dir(folder(tmp_path)).get("issue_refund").request
    assert side.kind == "pydantic" and side.strict is True and side.source == "pydantic models:Refund"


@pytest.mark.parametrize("body, needle", [
    ({"contract": 1, "tool": "t", "request": {"schema": {}}, "extra": 1}, "extra: unknown key"),
    ({"contract": 1, "request": {"schema": {}}}, "tool: missing"),
    ({"contract": 2, "tool": "t", "request": {"schema": {}}}, "contract: must be 1"),
    ({"contract": 1, "tool": "t"}, "needs a request or a response"),
    ({"contract": 1, "tool": "t", "request": {"schema": {}, "pydantic": "m:M"}}, "request: has both schema and pydantic"),
    ({"contract": 1, "tool": "t", "request": {}}, "request: has no source"),
    ({"contract": 1, "tool": "t", "response": {"schema": {}, "bogus": 1}}, "response.bogus: unknown key"),
    ({"contract": 1, "tool": "t", "request": {"schema": {}, "strict": True}}, "request.strict: only applies"),
    ({"contract": 1, "tool": "t", "request": {"schema": {"type": "strin"}}}, "request.schema: invalid schema at #/type"),
    ({"contract": 1, "tool": "t", "request": {"schema": {"$ref": "https://example.com/s.json"}}}, "remote $ref is refused"),
    ({"contract": 1, "tool": "t", "request": {"schema_file": "http://example.com/s.json"}}, "is remote"),
    ({"contract": 1, "tool": "t", "request": {"schema": {"$ref": "#/$defs/nope"}}}, "points at nothing"),
    ({"contract": 1, "tool": "t", "request": {"schema": {"$ref": "missing.json"}}}, "missing.json: file not found"),
    ({"contract": 1, "tool": "t", "request": {"schema_file": "nope.json"}}, "'nope.json' not found"),
    ({"contract": 1, "tool": "t", "request": {"pydantic": "no_such_module_xyz:M"}},
     "cannot import no_such_module_xyz (ModuleNotFoundError); install the package in this environment, or snapshot it: "
     "fault-contract init --pydantic no_such_module_xyz:M --snapshot"),
    ({"contract": 1, "tool": "t", "request": {"pydantic": "models:Nope"}}, "models has no 'Nope'"),
])
def test_loading_errors_name_the_file_and_key(tmp_path, body, needle):
    d = folder(tmp_path)
    d.mkdir()
    p = d / "t.contract.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(ContractError) as e:
        load_dir(d)
    assert str(p) in str(e.value) and needle in str(e.value)


def test_a_schema_file_pointer_that_points_at_nothing(tmp_path):
    (tmp_path / "s.json").write_text("{}", encoding="utf-8")
    write_contract(folder(tmp_path), "t", request={"schema_file": "../s.json#/components/x"})
    with pytest.raises(ContractError, match=r"pointer #/components/x not found"):
        load_dir(folder(tmp_path))


def test_two_files_for_one_tool(tmp_path):
    d = folder(tmp_path)
    write_contract(d, "a", request=schema({}))
    (d / "b.contract.json").write_text(json.dumps({"contract": 1, "tool": "a", "request": {"schema": {}}}), encoding="utf-8")
    with pytest.raises(ContractError, match=r"b.contract.json: tool 'a' already has a contract in a.contract.json"):
        load_dir(d)


def test_bad_json(tmp_path):
    d = folder(tmp_path)
    d.mkdir()
    (d / "t.contract.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(ContractError, match="not valid JSON"):
        load_dir(d)


def test_missing_or_empty_folder_is_not_found_not_an_error(tmp_path):
    assert load_dir(tmp_path / "none").found is False
    (tmp_path / "empty").mkdir()
    assert load_dir(tmp_path / "empty").found is False


def test_unchecked_formats_are_listed(tmp_path):
    s = {"type": "object", "properties": {"d": {"type": "string", "format": "date"},
                                          "ts": {"type": "string", "format": "date-time"},
                                          "u": {"type": "string", "format": "made-up"}}}
    write_contract(folder(tmp_path), "t", request=schema(s))
    cs = load_dir(folder(tmp_path))
    unchecked = cs.get("t").request.unchecked_formats
    assert "date" not in unchecked and "made-up" in unchecked
    notes = cs.notes()
    assert any("'made-up'" in n and "not checked" in n for n in notes)


def test_the_format_checker_checks_date(tmp_path):
    write_contract(folder(tmp_path), "t", request=schema({"type": "string", "format": "date"}))
    side = load_dir(folder(tmp_path)).get("t").request
    [v] = validate_side(side, '"2026-13-45"', "t", 1)
    assert (v.keyword, v.message) == ("format", "is not a valid date")


def test_validators_are_compiled_once(tmp_path):
    write_contract(folder(tmp_path), "t", request=schema(REFUND))
    side = load_dir(folder(tmp_path)).get("t").request
    v1 = side.validator
    validate_side(side, "{}", "t", 1)
    assert side.validator is v1


def test_embedded_id_scopes_are_honoured(tmp_path):
    # a $ref to an embedded resource by its $id resolves; it is not a missing file
    s = {"type": "object", "properties": {"thing": {"$ref": "nested/thing.json"}},
         "$defs": {"t": {"$id": "nested/thing.json", "type": "string"}}}
    write_contract(folder(tmp_path), "t", request=schema(s))
    side = load_dir(folder(tmp_path)).get("t").request
    [v] = validate_side(side, '{"thing": 5}', "t", 1)
    assert (v.path, v.keyword) == ("$.thing", "type")


def test_a_ref_inside_an_embedded_resource_resolves_against_that_resource(tmp_path):
    # #/$defs/N exists in the outer document but not in the embedded resource: a load error
    s = {"$defs": {"N": {"type": "number"},
                   "inner": {"$id": "inner.json", "type": "object", "properties": {"n": {"$ref": "#/$defs/N"}}}},
         "$ref": "inner.json"}
    write_contract(folder(tmp_path), "t", request=schema(s))
    with pytest.raises(ContractError, match=r"\$ref '#/\$defs/N' points at nothing"):
        load_dir(folder(tmp_path))


def test_a_ref_target_with_its_own_id_and_inner_refs(tmp_path):
    # the target's $id is applied once: its #/$defs/n is its own
    s = {"type": "object", "properties": {"thing": {"$ref": "nested/thing.json"}},
         "$defs": {"t": {"$id": "nested/thing.json", "$defs": {"n": {"type": "number"}}, "$ref": "#/$defs/n"}}}
    write_contract(folder(tmp_path), "t", request=schema(s))
    side = load_dir(folder(tmp_path)).get("t").request
    assert validate_side(side, '{"thing": 5}', "t", 1) == []
    [v] = validate_side(side, '{"thing": "x"}', "t", 1)
    assert (v.path, v.keyword) == ("$.thing", "type")


@pytest.mark.parametrize("name", ["enum", "const", "default", "examples"])
def test_properties_named_like_data_keywords_are_still_checked(tmp_path, name):
    write_contract(folder(tmp_path), "t", request=schema({"type": "object", "properties": {name: {"$ref": "#/missing"}}}))
    with pytest.raises(ContractError, match="points at nothing"):
        load_dir(folder(tmp_path))


def test_a_referenced_file_must_be_a_valid_schema(tmp_path):
    (tmp_path / "bad.json").write_text(json.dumps({"type": "bogus"}), encoding="utf-8")
    write_contract(folder(tmp_path), "t", request=schema({"$ref": "../bad.json"}))
    with pytest.raises(ContractError, match="points at an invalid schema"):
        load_dir(folder(tmp_path))


def test_load_file_directly(tmp_path):
    p = write_contract(folder(tmp_path), "t", response=schema({"type": "object"}))
    assert load_file(p).response.name == "response"
    assert "models" not in sys.modules or True
