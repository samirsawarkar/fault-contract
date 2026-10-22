"""Pydantic sources: strict and lax, rejected versus coerced."""
import json

import pytest

from fault_contract.config import Settings
from fault_contract.validate import counted, validate_side

from conftest import HERE, contracts_with


@pytest.fixture(autouse=True)
def _models(monkeypatch):
    monkeypatch.syspath_prepend(str(HERE))


def side(tmp_path, model, strict=None):
    body = {"pydantic": f"models:{model}"}
    if strict is not None:
        body["strict"] = strict
    cs, _ = contracts_with(tmp_path, t={"request": body})
    return cs.get("t").request


def check(tmp_path, model, value, strict=None):
    return validate_side(side(tmp_path, model, strict), json.dumps(value), "t", 1)


def test_numeric_text_for_an_int_is_coerced(tmp_path):
    [v] = check(tmp_path, "Count", {"n": "900"})
    assert (v.path, v.keyword, v.kind) == ("$.n", "type", "coerced")


def test_non_numeric_text_is_rejected(tmp_path):
    [v] = check(tmp_path, "Count", {"n": "abc"})
    assert (v.path, v.keyword, v.kind) == ("$.n", "type", "rejected")


def test_an_extra_field_with_extra_forbid_is_rejected(tmp_path):
    [v] = check(tmp_path, "Refund", {"order_id": "1", "amount": 1.0, "customer_id": "c", "override_limit": True})
    assert (v.path, v.keyword, v.kind, v.message) == ("$.override_limit", "additionalProperties", "rejected",
                                                     "field override_limit is not in the contract")


def test_a_missing_field_is_rejected_with_no_preview(tmp_path):
    [v] = check(tmp_path, "Refund", {"order_id": "1", "amount": 1.0})
    assert (v.path, v.keyword, v.kind, v.message, v.preview) == ("$.customer_id", "required", "rejected",
                                                                "missing required field", "")


def test_an_iso_date_string_is_clean_in_strict_json_mode(tmp_path):
    assert check(tmp_path, "Shipping", {"when": "2026-10-22"}) == []
    [v] = check(tmp_path, "Shipping", {"when": "2026-10-22", "mode": "sea"})
    assert (v.path, v.keyword, v.kind) == ("$.mode", "enum", "rejected")


def test_an_int_for_a_float_is_clean(tmp_path):
    assert check(tmp_path, "Refund", {"order_id": "1", "amount": 20, "customer_id": "c"}) == []


def test_nested_and_array_paths(tmp_path):
    vs = check(tmp_path, "Order", {"order_id": "1", "items": [{"sku": "a", "qty": 1}, {"sku": "b", "qty": "x"}],
                                   "color": "green"})
    got = {(v.path, v.keyword, v.kind) for v in vs}
    assert ("$.items[1].qty", "type", "rejected") in got and ("$.color", "enum", "rejected") in got


def test_null_and_double_encoded_and_wrong_container(tmp_path):
    [v] = check(tmp_path, "Count", {"n": None})
    assert (v.path, v.keyword, v.kind) == ("$.n", "type", "rejected")
    [v] = validate_side(side(tmp_path, "Count"), json.dumps(json.dumps({"n": 1})), "t", 1)
    assert (v.path, v.keyword, v.kind) == ("$", "type", "rejected")
    vs = check(tmp_path, "Order", {"order_id": "1", "items": {"sku": "a", "qty": 1}, "color": "red"})
    assert [(v.path, v.keyword, v.kind) for v in vs] == [("$.items", "type", "rejected")]


def test_lax_contracts_only_reject(tmp_path):
    assert check(tmp_path, "Count", {"n": "900"}, strict=False) == []
    [v] = check(tmp_path, "Count", {"n": "abc"}, strict=False)
    assert v.kind == "rejected"


def test_coerced_ignore_reports_without_firing(tmp_path):
    [v] = check(tmp_path, "Count", {"n": "900"})
    assert not counted(v, Settings(coerced="ignore")) and counted(v, Settings())


def test_json_schema_of_a_pydantic_side(tmp_path):
    s = side(tmp_path, "Refund").json_schema()
    assert s["required"] == ["order_id", "amount", "customer_id"] and s["properties"]["amount"]["type"] == "number"
