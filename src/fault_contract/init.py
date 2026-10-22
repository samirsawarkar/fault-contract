"""``fault-contract init``: draft contracts so a team never starts from an empty file.

Three sources: the tool definitions given to the model, a Pydantic model, or the successful results
seen in a trace export. init never invents required fields, never writes a value, and never
overwrites an existing contract unless ``--force`` is given: a side that is missing from an existing
contract is added, a side that is there is kept.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from faultgate_zero.redact import builtin as redact
from faultgate_zero.results import error_of

from .check import expand, load_traces
from .contract import NOT_SCHEMA, SCHEMA_MAPS, SUFFIX, ContractError, import_object

MAX_DEPTH = 4
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


@dataclass
class Written:
    lines: list[str] = field(default_factory=list)

    def say(self, line: str) -> None:
        self.lines.append(line)


def contract_path(folder: Path, tool: str) -> Path:
    return folder / f"{_UNSAFE.sub('_', tool)}{SUFFIX}"


def _write(folder: Path, tool: str, side: str, body: dict[str, Any], force: bool, out: Written) -> None:
    """Write one side of a contract, creating the file or adding the side to an existing one."""
    folder.mkdir(parents=True, exist_ok=True)
    p = contract_path(folder, tool)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            raise ContractError(f"{p}: exists and is not valid JSON; fix it or remove it first") from None
        if not isinstance(data, dict) or data.get("tool") != tool:
            raise ContractError(f"{p}: exists but is not the contract for {tool!r}")
        if side in data and not force:
            out.say(f"skip  {p.as_posix()} ({side} exists; --force replaces it)")
            return
        verb = "replaced" if side in data else "added"
        data[side] = body
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        out.say(f"{verb} {side} in {p.as_posix()}")
        return
    data = {"contract": 1, "tool": tool, side: body}
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out.say(f"wrote {p.as_posix()}")


# ---------------------------------------------------------------- from definitions

def close_objects(schema: Any) -> Any:
    """A copy with ``additionalProperties: false`` on every object that does not set it."""
    s = copy.deepcopy(schema)

    def visit(n: Any) -> None:
        if isinstance(n, list):
            for x in n:
                visit(x)
            return
        if not isinstance(n, dict):
            return
        is_object = n.get("type") == "object" or (isinstance(n.get("type"), list) and "object" in n["type"]) \
            or "properties" in n
        if is_object and "additionalProperties" not in n and "patternProperties" not in n \
                and "unevaluatedProperties" not in n:
            n["additionalProperties"] = False
        for k, v in list(n.items()):
            if k in SCHEMA_MAPS and isinstance(v, dict):
                for sub in v.values():  # property names are data, even "enum" or "type"
                    visit(sub)
            elif k not in NOT_SCHEMA:
                visit(v)

    visit(s)
    return s


def from_defs(definitions: dict[str, dict[str, Any]], folder: Path, *, allow_extra: bool = False,
              force: bool = False) -> Written:
    out = Written()
    any_required = False
    for tool, params in definitions.items():
        schema = copy.deepcopy(params) if allow_extra else close_objects(params)
        if not schema:
            schema = {"type": "object"} if allow_extra else {"type": "object", "additionalProperties": False}
        any_required = any_required or any(isinstance(n.get("required"), list) and n["required"]
                                           for n in _objects(schema))
        _write(folder, tool, "request", {"schema": schema}, force, out)
    if definitions and not any_required:
        out.say("note  no tool definition marks any field as required. Decide which fields are required and add them.")
    out.say('next  add a "response" section for each tool whose result the agent reads:')
    out.say("      fault-contract init --from-traces EXPORT --side response --all-tools")
    return out


def _objects(n: Any):
    if isinstance(n, dict):
        yield n
        for v in n.values():
            yield from _objects(v)
    elif isinstance(n, list):
        for v in n:
            yield from _objects(v)


# ---------------------------------------------------------------- from pydantic

def from_pydantic(spec: str, tool: str, side: str, folder: Path, *, snapshot: bool = False,
                  force: bool = False) -> Written:
    out = Written()
    if snapshot:
        try:
            from pydantic import TypeAdapter
        except ImportError:
            raise ContractError("--pydantic: pydantic is not installed in this environment") from None
        model = import_object(spec, "--pydantic", spec)
        schema = TypeAdapter(model).json_schema(mode="validation")
        schema = {"$comment": f"snapshot of {spec}; refresh with fault-contract init --pydantic {spec} --snapshot",
                  **schema}
        body: dict[str, Any] = {"schema": schema}
    else:
        import_object(spec, "--pydantic", spec)  # fail now, not on the first check
        body = {"pydantic": spec, "strict": True}
    _write(folder, tool, side, body, force, out)
    return out


# ---------------------------------------------------------------- from traces

def _is_private(key: str) -> bool:
    return redact(key) != key


@dataclass
class _Node:
    """What was seen at one path: the JSON types, and for objects and arrays their children."""
    types: set[str] = field(default_factory=set)
    objects: int = 0
    keys: dict[str, "_Node"] = field(default_factory=dict)
    key_count: dict[str, int] = field(default_factory=dict)
    private: bool = False
    values: "_Node | None" = None   # merged values of a private-keyed object
    items: "_Node | None" = None

    def add(self, v: Any, depth: int) -> None:
        t = _type(v)
        self.types.add(t)
        if depth >= MAX_DEPTH:
            return
        if isinstance(v, dict):
            self.objects += 1
            if not self.private and any(_is_private(str(k)) for k in v):
                self.private = True
            for k, x in v.items():
                k = str(k)
                # both views are kept: which one is written is decided once every object has been seen
                self.values = self.values or _Node()
                self.values.add(x, depth + 1)
                self.key_count[k] = self.key_count.get(k, 0) + 1
                self.keys.setdefault(k, _Node()).add(x, depth + 1)
        elif isinstance(v, list):
            for x in v:
                self.items = self.items or _Node()
                self.items.add(x, depth + 1)

    def schema(self, depth: int) -> dict[str, Any]:
        if depth >= MAX_DEPTH:
            return {}
        types = set(self.types)
        if {"integer", "number"} <= types:
            types.discard("integer")
        out: dict[str, Any] = {"type": sorted(types)[0] if len(types) == 1 else sorted(types)}
        if "object" in types:
            if self.private:
                out["additionalProperties"] = self.values.schema(depth + 1) if self.values else {}
            elif self.keys:
                out["properties"] = {k: n.schema(depth + 1) for k, n in self.keys.items()}
                req = [k for k in self.keys if self.key_count[k] == self.objects]
                if req:
                    out["required"] = req
        if "array" in types and self.items is not None:
            out["items"] = self.items.schema(depth + 1)
        return out


def _type(v: Any) -> str:
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
    return "object"


def infer(payloads: list[Any]) -> dict[str, Any]:
    """A JSON Schema from payloads: types and keys only. Never enum, format, pattern, bounds or values."""
    root = _Node()
    for p in payloads:
        root.add(p, 0)
    return root.schema(0) if payloads else {}


def files_sha12(paths: list[str | Path]) -> str:
    h = hashlib.sha256()
    for f in expand(paths):
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


def from_traces(paths: list[str | Path], side: str, folder: Path, *, tool: str | None = None,
                all_tools: bool = False, fmt: str = "auto", force: bool = False) -> Written:
    if bool(tool) == bool(all_tools):
        raise ContractError("--from-traces: give --tool NAME or --all-tools")
    traces, _ = load_traces(paths, fmt)
    seen: dict[str, list[Any]] = {}
    for t in traces:
        for s in t.steps:
            if tool and s.tool != tool:
                continue
            if side == "response":
                if not s.result_seen or error_of(s) is not None:
                    continue
                raw = s.result
            else:
                raw = s.arguments if s.arguments.strip() else "{}"
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            seen.setdefault(s.tool, []).append(payload)
    out = Written()
    if tool and tool not in seen:
        out.say(f"skip  {tool}: no successful {side}s with JSON found in the export")
        return out
    digest = files_sha12(paths)
    for name in sorted(seen):
        n = len(seen[name])
        noun = "results" if side == "response" else "calls"
        schema = {"$comment": f"inferred from {n} {noun} in {digest}; review before relying on it",
                  **infer(seen[name])}
        _write(folder, name, side, {"schema": schema}, force, out)
    return out
