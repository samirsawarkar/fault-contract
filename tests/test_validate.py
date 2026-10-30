"""The validator: messages per keyword, all errors, skipping, unknown tools, privacy."""
import json

import pytest

from fault_contract.config import Settings
from fault_contract.validate import PREVIEW_MAX, counted, fmt_path, preview, validate_side, validate_step, validate_trace

from conftest import GOOD_REFUND, REFUND, REFUND_RESULT, contracts_with, make_trace, schema, step


def side_for(tmp_path, s, name="request"):
    cs, _ = contracts_with(tmp_path, t={name: schema(s)})
    return cs.get("t").side(name)


def one(tmp_path, s, value):
    vs = validate_side(side_for(tmp_path, s), json.dumps(value), "t", 1)
    assert len(vs) == 1, vs
    return vs[0]


@pytest.mark.parametrize("s, value, path, keyword, message", [
    ({"type": "object", "properties": {"amount": {"type": "number"}}}, {"amount": "45.00"},
     "$.amount", "type", "expected number, got string"),
    ({"type": ["number", "null"]}, "x", "$", "type", "expected number or null, got string"),
    ({"type": "object", "required": ["currency"]}, {}, "$.currency", "required", "missing required field"),
    ({"type": "object", "properties": {"a": {}}, "additionalProperties": False}, {"a": 1, "override_limit": True},
     "$.override_limit", "additionalProperties", "field override_limit is not in the contract"),
    ({"enum": ["pending", "completed"]}, "lost", "$", "enum", "value is not one of: pending, completed"),
    ({"const": "v1"}, "v2", "$", "const", "value is not one of: v1"),
    ({"minimum": 0}, -1, "$", "minimum", "value is below the minimum 0"),
    ({"maximum": 500}, 900, "$", "maximum", "value is above the maximum 500"),
    ({"exclusiveMinimum": 0}, 0, "$", "exclusiveMinimum", "value must be above 0"),
    ({"exclusiveMaximum": 10}, 10, "$", "exclusiveMaximum", "value must be below 10"),
    ({"maxLength": 3}, "abcd", "$", "maxLength", "text is longer than 3 characters"),
    ({"minLength": 2}, "a", "$", "minLength", "text is shorter than 2 characters"),
    ({"pattern": "^[0-9]{4,}$"}, "12a", "$", "pattern", "text does not match the pattern ^[0-9]{4,}$"),
    ({"format": "date"}, "2026-02-31", "$", "format", "is not a valid date"),
    ({"minItems": 1}, [], "$", "minItems", "list has 0 items, needs at least 1"),
    ({"maxItems": 1}, [1, 2], "$", "maxItems", "list has 2 items, allows at most 1"),
    ({"uniqueItems": True}, [1, 1], "$", "uniqueItems", "list has repeated items"),
    ({"multipleOf": 5}, 7, "$", "multipleOf", "value is not a multiple of 5"),
    ({"type": "object", "minProperties": 1}, {}, "$", "minProperties", "breaks the contract rule minProperties at #/minProperties"),
    ({"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
        "sku": {"type": "string"}}}}}}, {"items": [{"sku": "a"}, {"sku": "b"}, {"sku": 3}]},
     "$.items[2].sku", "type", "expected string, got integer"),
])
def test_each_keyword_gives_path_keyword_and_message(tmp_path, s, value, path, keyword, message):
    v = one(tmp_path, s, value)
    assert (v.path, v.keyword, v.message, v.kind) == (path, keyword, message, "schema")


def test_one_of_reports_the_closest_branch(tmp_path):
    s = {"oneOf": [{"type": "object", "properties": {"amount": {"type": "number"}}, "required": ["amount"]},
                   {"type": "string"}]}
    v = one(tmp_path, s, {"amount": "x"})
    assert v.keyword == "oneOf" and v.message.startswith("matches none of the allowed shapes (closest: ")
    assert "expected number, got string" in v.message
    v = one(tmp_path, {"oneOf": [{"type": "number"}, {"type": "integer"}]}, 3)
    assert v.message == "matches more than one of the allowed shapes"


def test_every_error_is_reported_sorted_by_path(tmp_path):
    vs = validate_side(side_for(tmp_path, REFUND), json.dumps({"order_id": 5, "amount": "45.00", "x": 1}), "t", 4)
    got = [(v.path, v.keyword) for v in vs]
    assert ("$.amount", "type") in got and ("$.order_id", "type") in got
    assert ("$.customer_id", "required") in got and ("$.x", "additionalProperties") in got
    assert len(vs) == 4 and all(v.step == 4 for v in vs)
    assert [g[0] for g in got] == sorted(g[0] for g in got)


def test_two_missing_fields_are_two_violations(tmp_path):
    vs = validate_side(side_for(tmp_path, REFUND), "{}", "t", 1)
    assert sorted(v.path for v in vs) == ["$.amount", "$.customer_id", "$.order_id"]


def test_numeric_text_is_not_a_number(tmp_path):
    v = one(tmp_path, {"type": "number"}, "45.00")
    assert v.message == "expected number, got string"


@pytest.mark.parametrize("raw", ["not json", '{"a": ', "{'a': 1}"])
def test_arguments_that_are_not_json(tmp_path, raw):
    [v] = validate_side(side_for(tmp_path, REFUND), raw, "t", 1)
    assert (v.path, v.keyword, v.kind, v.message) == ("$", "not_json", "not_json", "arguments are not JSON")


def test_double_encoded_arguments_are_a_type_error_at_the_root(tmp_path):
    raw = json.dumps(json.dumps(GOOD_REFUND))
    [v] = validate_side(side_for(tmp_path, REFUND), raw, "t", 1)
    assert (v.path, v.keyword, v.message) == ("$", "type", "expected object, got string")


def test_no_arguments_at_all_is_an_empty_object(tmp_path):
    cs, st = contracts_with(tmp_path, ping={"request": schema({"type": "object", "additionalProperties": False})})
    assert validate_step(step("ping", ""), cs, st) == []


# ---------------------------------------------------------------- what is checked

def refund_contracts(tmp_path, **extra):
    return contracts_with(tmp_path, issue_refund={"request": schema(REFUND), "response": schema(REFUND_RESULT)}, **extra)


def test_error_results_are_skipped(tmp_path):
    cs, st = refund_contracts(tmp_path)
    for s in (step("issue_refund", GOOD_REFUND, "Error: billing down"),
              step("issue_refund", GOOD_REFUND, {"error": "timeout"}),
              step("issue_refund", GOOD_REFUND, "anything", error=True)):
        assert validate_step(s, cs, st) == []


def test_unseen_results_are_skipped(tmp_path):
    cs, st = refund_contracts(tmp_path)
    assert validate_step(step("issue_refund", GOOD_REFUND, "", seen=False), cs, st) == []


def test_a_request_only_contract_never_checks_results(tmp_path):
    cs, st = contracts_with(tmp_path, issue_refund={"request": schema(REFUND)})
    assert validate_step(step("issue_refund", GOOD_REFUND, "plain text, not json"), cs, st) == []


def test_results_are_checked(tmp_path):
    cs, st = refund_contracts(tmp_path)
    [v] = validate_step(step("issue_refund", GOOD_REFUND, {"refunded": 20}), cs, st)
    assert (v.side, v.path, v.keyword) == ("response", "$.order_id", "required")
    [v] = validate_step(step("issue_refund", GOOD_REFUND, "ok"), cs, st)
    assert (v.side, v.keyword, v.message) == ("response", "not_json", "result is not JSON")


def test_unknown_tools_follow_the_setting(tmp_path):
    cs, st = refund_contracts(tmp_path)
    s = step("send_email", {"to": "x"})
    assert validate_step(s, cs, st) == []
    [v] = validate_step(s, cs, Settings(root=tmp_path, unknown_tools="fail"))
    assert (v.kind, v.keyword, v.tool) == ("unknown_tool", "unknown_tool", "send_email")


def test_validate_trace_walks_every_step_in_order(tmp_path):
    cs, st = refund_contracts(tmp_path)
    t = make_trace([("issue_refund", GOOD_REFUND, {"refunded": 20, "order_id": "1042"}),
                    ("issue_refund", {**GOOD_REFUND, "amount": "20.00"}, {"refunded": 20, "order_id": "1042"}),
                    ("issue_refund", {"order_id": "1042", "amount": 1}, "{}")])
    vs = validate_trace(t, cs, st)
    assert [(v.step, v.path) for v in vs] == [(2, "$.amount"), (3, "$.customer_id"), (3, "$.order_id"),
                                             (3, "$.refunded")]


def test_coerced_ignore_reports_without_counting():
    from fault_contract.validate import Violation
    v = Violation("t", "request", 1, "$.n", "type", "coerced", "x")
    assert counted(v, Settings()) and not counted(v, Settings(coerced="ignore"))
    assert counted(Violation("t", "request", 1, "$.n", "type", "rejected", "x"), Settings(coerced="ignore"))


# ---------------------------------------------------------------- privacy

EMAIL = "alice.secret@example.com"
CARD = "4111 1111 1111 1111"
KEY = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
PLANTED = (EMAIL, CARD, KEY, "4111111111111111", "alice.secret", "abcdefghijklmnop")


def test_planted_values_never_appear_in_messages_paths_or_previews(tmp_path):
    s = {"type": "object", "properties": {
        "email": {"type": "integer"}, "card": {"type": "number"}, "key": {"enum": ["a"]},
        "note": {"maxLength": 3}, "code": {"pattern": "^x$"},
        "map": {"type": "object", "additionalProperties": {"type": "integer"}},
        "nested": {"type": "object", "properties": {}, "additionalProperties": False}}}
    payload = {"email": EMAIL, "card": CARD, "key": KEY, "note": f"call {EMAIL} or {CARD}", "code": KEY,
               "map": {EMAIL: "x", CARD: "y"}, "nested": {EMAIL: 1, KEY: 2}}
    vs = validate_side(side_for(tmp_path, s), json.dumps(payload), "t", 1)
    assert len(vs) >= 8
    blob = json.dumps([v.__dict__ for v in vs], ensure_ascii=False)
    for planted in PLANTED:
        assert planted not in blob, planted
    assert all(len(v.preview) <= PREVIEW_MAX for v in vs)


def test_planted_values_in_pydantic_errors(tmp_path, monkeypatch):
    from conftest import HERE
    monkeypatch.syspath_prepend(str(HERE))
    cs, _ = contracts_with(tmp_path, t={"request": {"pydantic": "models:Refund"}})
    raw = json.dumps({"order_id": EMAIL, "amount": KEY, "customer_id": 5, EMAIL: CARD})
    vs = validate_side(cs.get("t").request, raw, "t", 1)
    blob = json.dumps([v.__dict__ for v in vs], ensure_ascii=False)
    for planted in PLANTED:
        assert planted not in blob, planted


def test_preview_redacts_before_cutting():
    long_email = "x" * 30 + "@example.com"
    p = preview(long_email)
    assert len(p) <= PREVIEW_MAX and "xxxxxxxx@" not in p and "[REDACTED" in p
    assert preview("a" * 100).endswith("…") and len(preview("a" * 100)) == PREVIEW_MAX


def test_paths_redact_keys_and_quote_odd_ones():
    assert fmt_path(["items", 2, "sku"]) == "$.items[2].sku"
    assert fmt_path(["a.b"]) == '$["a.b"]'
    assert EMAIL not in fmt_path([EMAIL])
