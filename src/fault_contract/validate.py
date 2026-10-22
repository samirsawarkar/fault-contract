"""The one validator: a tool step's arguments and result against its contract.

``validate_trace`` is the only entry point the pytest check and the offline check use, so the same
run gives the same violations in both places.

Messages are built here from the keyword, the contract's own schema value and the type of the value.
jsonschema's error text is never shown, because it quotes the rejected value; Pydantic errors are read
with ``include_input=False``. Field names come from the payload, so every path is redacted.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from faultgate_zero.redact import builtin as redact
from faultgate_zero.results import error_of
from faultgate_zero.trace import Step, Trace
from jsonschema.exceptions import best_match

from .config import Settings
from .contract import Contract, Contracts, Side

PREVIEW_MAX = 40
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Violation:
    tool: str
    side: str          # "request" | "response"
    step: int          # faultgate-zero step index, 1-based
    path: str          # "$.items[2].sku"
    keyword: str       # "type", "required", "additionalProperties", "enum", ... or "not_json"
    kind: str          # "schema" | "rejected" | "coerced" | "not_json" | "unknown_tool"
    message: str       # plain words, built here
    preview: str = ""  # redacted, at most 40 characters; empty for missing fields
    schema_path: str = ""  # "#/properties/amount/type" (JSON Schema sources only), for explain

    def line(self) -> str:
        return f"step {self.step} {self.tool} {self.side} {self.path}: {self.message}"


def counted(v: Violation, settings: Settings) -> bool:
    """Whether a violation fires the check. Coerced calls can be reported without failing."""
    return not (v.kind == "coerced" and settings.coerced == "ignore")


# ---------------------------------------------------------------- formatting

def json_type(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def fmt_path(parts: Iterable[Any]) -> str:
    out = "$"
    for p in parts:
        if isinstance(p, int) and not isinstance(p, bool):
            out += f"[{p}]"
            continue
        key = redact(str(p))
        out += f".{key}" if _IDENT.match(key) else f"[{json.dumps(key, ensure_ascii=False)}]"
    return out


def preview(value: Any) -> str:
    """A redacted preview, at most 40 characters. Redaction runs on the whole text first, so cutting it
    never leaves part of an email or a card number behind."""
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(value)
    if isinstance(value, str):
        text = json.dumps(text, ensure_ascii=False)
    text = redact(text)
    return text if len(text) <= PREVIEW_MAX else text[:PREVIEW_MAX - 1] + "…"


def _values(vals: Iterable[Any]) -> str:
    return ", ".join(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for v in vals)


def _expected(t: Any) -> str:
    return " or ".join(t) if isinstance(t, list) else str(t)


def _schema_message(e: Any) -> str:
    """Plain words for one jsonschema error, from the keyword, the schema value and the value's type."""
    kw, sv, inst = e.validator, e.validator_value, e.instance
    if kw == "type":
        return f"expected {_expected(sv)}, got {json_type(inst)}"
    if kw == "enum":
        return f"value is not one of: {_values(sv)}"
    if kw == "const":
        return f"value is not one of: {_values([sv])}"
    if kw == "minimum":
        return f"value is below the minimum {sv}"
    if kw == "maximum":
        return f"value is above the maximum {sv}"
    if kw == "exclusiveMinimum":
        return f"value must be above {sv}"
    if kw == "exclusiveMaximum":
        return f"value must be below {sv}"
    if kw == "multipleOf":
        return f"value is not a multiple of {sv}"
    if kw == "minLength":
        return f"text is shorter than {sv} characters"
    if kw == "maxLength":
        return f"text is longer than {sv} characters"
    if kw == "pattern":
        return f"text does not match the pattern {sv}"
    if kw == "format":
        return f"is not a valid {sv}"
    if kw == "minItems":
        return f"list has {len(inst) if isinstance(inst, list) else 0} items, needs at least {sv}"
    if kw == "maxItems":
        return f"list has {len(inst) if isinstance(inst, list) else 0} items, allows at most {sv}"
    if kw == "uniqueItems":
        return "list has repeated items"
    if kw in ("oneOf", "anyOf"):
        if not e.context:
            return "matches more than one of the allowed shapes"
        sub = _closest(e.context)
        rel = fmt_path(sub.relative_path)[1:]
        return f"matches none of the allowed shapes (closest: {rel + ' ' if rel else ''}{_schema_message(sub)})"
    return f"breaks the contract rule {kw} at {_schema_path(e)}"


def _closest(context: list[Any]) -> Any:
    """The branch the value came closest to: one whose type it has (no type error at the top), then the
    one with the fewest errors. Its first error, by path, is shown."""
    branches: dict[Any, list[Any]] = {}
    for c in context:
        branches.setdefault(c.relative_schema_path[0] if c.relative_schema_path else 0, []).append(c)

    def rank(errs: list[Any]) -> tuple[int, int]:
        wrong_type = any(x.validator == "type" and not x.relative_path for x in errs)
        return (1 if wrong_type else 0, len(errs))

    best = min(branches.values(), key=rank)
    return min(best, key=lambda x: [str(p) for p in x.relative_path]) if best else best_match(context)


def _schema_path(e: Any) -> str:
    return "#/" + "/".join(str(p) for p in e.absolute_schema_path) if e.absolute_schema_path else "#"


# ---------------------------------------------------------------- engines

def _schema_violations(side: Side, instance: Any, tool: str, step: int) -> list[Violation]:
    out: list[Violation] = []
    for e in sorted(side.validator.iter_errors(instance), key=lambda e: [str(p) for p in e.absolute_path]):
        parts = list(e.absolute_path)
        sp = _schema_path(e)
        if e.validator == "required":
            missing = [p for p in e.validator_value if isinstance(e.instance, dict) and p not in e.instance]
            name = next((p for p in missing if e.message == f"{p!r} is a required property"),
                        missing[0] if missing else "?")
            out.append(Violation(tool, side.name, step, fmt_path([*parts, name]), "required", "schema",
                                 "missing required field", "", sp))
        elif e.validator == "additionalProperties" and isinstance(e.instance, dict):
            props = e.schema.get("properties", {}) if isinstance(e.schema, dict) else {}
            patterns = [re.compile(p) for p in (e.schema.get("patternProperties") or {})] \
                if isinstance(e.schema, dict) else []
            extras = [k for k in e.instance if k not in props and not any(p.search(k) for p in patterns)]
            for k in extras:
                out.append(Violation(tool, side.name, step, fmt_path([*parts, k]), "additionalProperties", "schema",
                                     f"field {redact(k)} is not in the contract", preview(e.instance[k]), sp))
        else:
            out.append(Violation(tool, side.name, step, fmt_path(parts), str(e.validator), "schema",
                                 _schema_message(e), preview(e.instance), sp))
    out.sort(key=lambda v: v.path)
    return out


PYDANTIC_KEYWORDS = {
    "missing": "required", "extra_forbidden": "additionalProperties", "literal_error": "enum", "enum": "enum",
    "greater_than": "exclusiveMinimum", "greater_than_equal": "minimum", "less_than": "exclusiveMaximum",
    "less_than_equal": "maximum", "string_too_short": "minLength", "string_too_long": "maxLength",
    "string_pattern_mismatch": "pattern", "too_short": "minItems", "too_long": "maxItems",
    "multiple_of": "multipleOf",
}
_TYPE_ERROR = re.compile(r"_(?:type|parsing)$|^(?:none_required|is_instance_of|model_attributes_type|dataclass_type)$")


def _pydantic_keyword(t: str) -> str:
    if t in PYDANTIC_KEYWORDS:
        return PYDANTIC_KEYWORDS[t]
    if _TYPE_ERROR.search(t):
        return "type"
    return t


def _loc_parts(loc: tuple[Any, ...], instance: Any) -> tuple[list[Any], Any, bool]:
    """Payload path parts from a Pydantic ``loc``, dropping union and validator tags that are not keys.
    Returns (parts, value at the path, found)."""
    parts: list[Any] = []
    cur, found = instance, True
    for p in loc:
        if isinstance(cur, dict) and isinstance(p, str):
            parts.append(p)
            found = found and p in cur
            cur = cur.get(p)
        elif isinstance(cur, list) and isinstance(p, int) and not isinstance(p, bool):
            parts.append(p)
            found = found and 0 <= p < len(cur)
            cur = cur[p] if 0 <= p < len(cur) else None
        # anything else is a tag (a union member, a validator name): not part of the payload path
    return parts, cur, found


def _pydantic_violations(side: Side, raw: str, instance: Any, tool: str, step: int) -> list[Violation]:
    from pydantic import ValidationError

    ta = side.adapter
    try:
        ta.validate_json(raw, strict=side.strict)
        return []
    except ValidationError as err:
        strict_err = err
    if side.strict:
        try:
            ta.validate_json(raw)
            kind = "coerced"        # accepted only by converting values
        except ValidationError:
            kind = "rejected"       # a backend with this model would refuse the call
    else:
        kind = "rejected"
    out = []
    for d in strict_err.errors(include_input=False, include_url=False):
        parts, value, found = _loc_parts(tuple(d.get("loc", ())), instance)
        kw = _pydantic_keyword(str(d.get("type", "")))
        if kw == "required":
            msg = "missing required field"
        elif kw == "additionalProperties":
            msg = f"field {redact(str(parts[-1])) if parts else '?'} is not in the contract"
        else:
            msg = redact(str(d.get("msg", "")))
        pv = "" if kw == "required" or not found else preview(value)
        out.append(Violation(tool, side.name, step, fmt_path(parts), kw, kind, msg, pv))
    return out


def _parse(text: str) -> tuple[bool, Any]:
    try:
        return True, json.loads(text)
    except ValueError:
        return False, None


def validate_side(side: Side, raw: str, tool: str, step: int) -> list[Violation]:
    ok, instance = _parse(raw)
    if not ok:
        return [Violation(tool, side.name, step, "$", "not_json", "not_json",
                          f"{'arguments are' if side.name == 'request' else 'result is'} not JSON", preview(raw))]
    if side.kind == "pydantic":
        return _pydantic_violations(side, raw, instance, tool, step)
    return _schema_violations(side, instance, tool, step)


def _arguments(step: Step) -> str:
    # a tool called with no arguments may be recorded with none at all: that is an empty object
    return step.arguments if step.arguments.strip() else "{}"


def response_checked(step: Step, contract: Contract | None) -> bool:
    """Whether a step's result is checked: the contract has a response side, the result went back to the
    model, and it is not an error (error shapes are not in v1)."""
    return bool(contract and contract.response and step.result_seen and error_of(step) is None)


def validate_step(step: Step, contracts: Contracts, settings: Settings) -> list[Violation]:
    c = contracts.get(step.tool)
    if c is None:
        if settings.unknown_tools == "fail":
            return [Violation(step.tool, "request", step.index, "$", "unknown_tool", "unknown_tool",
                              "tool has no contract")]
        return []
    out: list[Violation] = []
    if c.request:
        out += validate_side(c.request, _arguments(step), step.tool, step.index)
    if response_checked(step, c):
        out += validate_side(c.response, step.result, step.tool, step.index)
    return out


def validate_trace(trace: Trace, contracts: Contracts, settings: Settings,
                   steps: list[Step] | None = None) -> list[Violation]:
    """Every violation in one run, in step order. ``steps`` saves recomputing ``trace.steps``."""
    out: list[Violation] = []
    for s in trace.steps if steps is None else steps:
        out += validate_step(s, contracts, settings)
    return out
