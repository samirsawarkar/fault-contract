"""``fault-contract defs``: do the tool definitions given to the model agree with the contracts?

Static: nothing runs and nothing is called. Each definition's parameters schema (D) is compared with
the contract's request schema (C), into nested objects and arrays.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .contract import ContractError, Contracts, import_object, resolve_refs

NOT_COMPARED = ("pattern", "format", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
                "minLength", "maxLength", "minItems", "maxItems", "oneOf", "anyOf", "allOf")


class DefsError(ValueError):
    """Definitions that cannot be read. CLI exit code 2."""


@dataclass(frozen=True)
class Finding:
    tool: str
    rule: str      # "D1".."D7", or "info"
    level: str     # "error" | "warning" | "info"
    message: str


# ---------------------------------------------------------------- reading definitions

def parse(entries: Any, where: str = "definitions") -> dict[str, dict[str, Any]]:
    """Tool name -> parameters schema, from OpenAI Chat, OpenAI Responses or Anthropic entries."""
    if isinstance(entries, dict) and isinstance(entries.get("tools"), list):
        entries = entries["tools"]
    if not isinstance(entries, (list, tuple)):
        raise DefsError(f"{where}: expected a list of tool definitions")
    out: dict[str, dict[str, Any]] = {}
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            raise DefsError(f"{where}[{i}]: a tool definition is an object")
        if isinstance(e.get("function"), dict):                       # OpenAI Chat
            name, params = e["function"].get("name"), e["function"].get("parameters", {})
        elif "input_schema" in e:                                     # Anthropic
            name, params = e.get("name"), e.get("input_schema", {})
        elif e.get("type") == "function" and "name" in e:             # OpenAI Responses
            name, params = e.get("name"), e.get("parameters", {})
        else:
            raise DefsError(f"{where}[{i}]: not an OpenAI Chat, OpenAI Responses or Anthropic tool definition")
        if not isinstance(name, str) or not name:
            raise DefsError(f"{where}[{i}]: the tool has no name")
        if name in out:
            raise DefsError(f"{where}[{i}]: tool {name!r} is defined twice")
        out[name] = params if isinstance(params, dict) else {}
    return out


def load(defs_file: str | None = None, defs_module: str | None = None) -> dict[str, dict[str, Any]]:
    if bool(defs_file) == bool(defs_module):
        raise DefsError("give one of --defs FILE or --defs-module module:ATTR")
    if defs_file:
        try:
            data = json.loads(Path(defs_file).read_text(encoding="utf-8"))
        except OSError as exc:
            raise DefsError(f"{defs_file}: cannot read it ({type(exc).__name__})") from None
        except ValueError:
            raise DefsError(f"{defs_file}: not valid JSON") from None
        return parse(data, defs_file)
    if ":" not in defs_module:
        raise DefsError(f"--defs-module {defs_module!r}: use module:ATTR, for example refund_agent:TOOLS")
    try:
        obj = import_object(defs_module, "--defs-module", defs_module)
    except ContractError as exc:
        raise DefsError(str(exc)) from None
    return parse(obj, defs_module)


# ---------------------------------------------------------------- comparing

def _types(s: dict[str, Any]) -> set[str] | None:
    t = s.get("type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return {x for x in t if isinstance(x, str)}
    if "properties" in s:
        return {"object"}
    if "enum" in s and s["enum"]:
        vals = s["enum"]
        if all(isinstance(v, str) for v in vals):
            return {"string"}
    return None


def _fits(d: set[str], c: set[str]) -> bool:
    """Every type the definition allows is allowed by the contract (an integer is a number)."""
    return all(t in c or (t == "integer" and "number" in c) for t in d)


def _enum(s: dict[str, Any]) -> list[Any] | None:
    if isinstance(s.get("enum"), list):
        return s["enum"]
    if "const" in s:
        return [s["const"]]
    return None


def _child(path: str, key: str) -> str:
    return f"{path}.{key}" if key.isidentifier() else f"{path}[{json.dumps(key)}]"


def compare(d: Any, c: Any, path: str = "$") -> tuple[list[tuple[str, str, str]], list[str]]:
    """(findings as (rule, level, message), not-compared notes) for D against C at ``path``."""
    found: list[tuple[str, str, str]] = []
    skipped: list[str] = []
    if not isinstance(d, dict) or not isinstance(c, dict):
        return found, skipped
    for side, s in (("contract", c), ("definition", d)):
        for kw in NOT_COMPARED:
            if kw in s:
                skipped.append(f"{path} {kw} ({side})")
        if isinstance(s.get("$ref"), str):
            skipped.append(f"{path} $ref {s['$ref']} ({side})")
    dt, ct = _types(d), _types(c)
    if dt and ct and not _fits(dt, ct):
        found.append(("D5", "error", f"{path} is {' or '.join(sorted(dt))} in the tool definition, "
                                     f"but {' or '.join(sorted(ct))} in the contract"))
        return found, skipped
    de, ce = _enum(d), _enum(c)
    if de is not None and ce is not None:
        extra = [v for v in de if v not in ce]
        if extra:
            found.append(("D6", "error", f"{path} lists values the contract does not allow: "
                                         + ", ".join(json.dumps(v, ensure_ascii=False) for v in extra)))
    dprops = d.get("properties") if isinstance(d.get("properties"), dict) else {}
    cprops = c.get("properties") if isinstance(c.get("properties"), dict) else {}
    if "properties" in c or "required" in c or "properties" in d:
        dreq = set(d.get("required") or [])
        for name in c.get("required") or []:
            if name not in dprops:
                found.append(("D3", "error", f"{_child(path, name)} is required by the contract, "
                                             "but the tool definition does not have it"))
            elif name not in dreq:
                found.append(("D7", "warning", f"{_child(path, name)} is required by the contract, "
                                               "but the tool definition marks it optional"))
        if c.get("additionalProperties") is False:
            for name in dprops:
                if name not in cprops and not c.get("patternProperties"):
                    found.append(("D4", "error", f"{_child(path, name)} is in the tool definition, "
                                                 "but the contract does not allow extra fields"))
        for name in dprops:
            if name in cprops:
                f, s = compare(dprops[name], cprops[name], _child(path, name))
                found += f
                skipped += s
    if isinstance(d.get("items"), dict) and isinstance(c.get("items"), dict):
        f, s = compare(d["items"], c["items"], f"{path}[]")
        found += f
        skipped += s
    return found, skipped


def run(definitions: dict[str, dict[str, Any]], contracts: Contracts, settings: Settings) -> list[Finding]:
    out: list[Finding] = []
    for tool in sorted(set(definitions) | set(contracts.by_tool)):
        c = contracts.get(tool)
        if tool not in definitions:
            out.append(Finding(tool, "D2", "warning", "has a contract, but no tool definition (a stale contract?)"))
            continue
        if c is None:
            level = "warning" if settings.unknown_tools == "ignore" else "error"
            out.append(Finding(tool, "D1", level, "is defined for the model, but has no contract"))
            continue
        if c.request is None:
            out.append(Finding(tool, "info", "info", "the contract has no request side; nothing to compare"))
            continue
        cschema = resolve_refs(c.request.json_schema(), c.request if c.request.kind == "schema" else None)
        dschema = resolve_refs(definitions[tool])
        found, skipped = compare(dschema, cschema)
        out += [Finding(tool, rule, level, msg) for rule, level, msg in found]
        if skipped:
            out.append(Finding(tool, "info", "info", "not compared: " + "; ".join(dict.fromkeys(skipped))))
    return out


def text(findings: list[Finding], tools: list[str], strict: bool) -> str:
    width = max((len(t) for t in tools), default=8)
    lines = []
    for tool in tools:
        mine = [f for f in findings if f.tool == tool]
        real = [f for f in mine if f.level != "info"]
        if not real:
            lines.append(f"{tool:<{width}}  OK")
        for f in real:
            lines.append(f"{tool:<{width}}  {'ERROR' if f.level == 'error' else 'WARN'}  {f.rule}  {f.message}")
        for f in mine:
            if f.level == "info":
                lines.append(f"{tool:<{width}}  info      {f.message}")
    errors = sum(f.level == "error" for f in findings)
    warnings = sum(f.level == "warning" for f in findings)
    lines.append(f"{errors} error{'s' if errors != 1 else ''}, {warnings} warning{'s' if warnings != 1 else ''}"
                 + ("   (--strict: warnings fail)" if strict else "   (--strict makes warnings fail)"))
    return "\n".join(lines)


def failed(findings: list[Finding], strict: bool) -> bool:
    return any(f.level == "error" or (strict and f.level == "warning") for f in findings)
