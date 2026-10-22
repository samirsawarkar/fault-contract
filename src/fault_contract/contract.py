"""Contract files: one ``<tool>.contract.json`` per tool, loaded and checked before anything runs.

A contract has a ``request`` side (the arguments the agent sends), a ``response`` side (the successful
result it relies on), or both. Each side has exactly one source: an inline JSON Schema (``schema``),
a JSON Schema in a local file with an optional JSON pointer (``schema_file``), or a Pydantic model
(``pydantic``). Schemas are checked against the 2020-12 metaschema and every ``$ref`` is resolved at
load time; remote ``$ref`` is refused, because nothing here touches the network.
"""
from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urldefrag, urljoin, urlparse

import jsonschema
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource, Unresolvable
from referencing.jsonschema import DRAFT202012

TOP_KEYS = {"contract", "tool", "request", "response", "owner", "description"}
SOURCES = ("schema", "schema_file", "pydantic")
SIDE_KEYS = {*SOURCES, "strict"}
SIDES = ("request", "response")
SUFFIX = ".contract.json"
FORMAT_CHECKER = Draft202012Validator.FORMAT_CHECKER


class ContractError(ValueError):
    """A contract that cannot be used. The message names the file and the key. CLI exit code 2."""


@dataclass
class Side:
    name: str                       # "request" | "response"
    kind: str                       # "schema" | "pydantic"
    source: str                     # for people: "inline schema", "schema_file ...", "pydantic mod:Model"
    schema: dict[str, Any] | None = None       # JSON Schema sources: the schema validated against
    validator: Any = None                      # compiled Draft202012Validator
    registry: Any = None                       # referencing Registry with every local file it needs
    base_uri: str = ""
    pydantic: str | None = None                # "module:Model"
    strict: bool = True
    adapter: Any = None                        # pydantic TypeAdapter
    unchecked_formats: tuple[str, ...] = ()

    def json_schema(self) -> dict[str, Any]:
        """The side as JSON Schema (a Pydantic model's validation schema), for ``defs`` and ``explain``."""
        if self.kind == "pydantic":
            return self.adapter.json_schema(mode="validation")
        return self.schema or {}


@dataclass
class Contract:
    tool: str
    path: Path
    request: Side | None = None
    response: Side | None = None
    owner: str | None = None
    description: str | None = None

    def side(self, name: str) -> Side | None:
        return self.request if name == "request" else self.response


@dataclass
class Contracts:
    folder: Path
    by_tool: dict[str, Contract] = field(default_factory=dict)
    found: bool = True              # False when the folder does not exist or has no contract files

    def get(self, tool: str) -> Contract | None:
        return self.by_tool.get(tool)

    def __len__(self) -> int:
        return len(self.by_tool)

    def notes(self) -> list[str]:
        """Formats that will not be checked, one line each, so nobody assumes they are."""
        out = []
        for c in self.by_tool.values():
            for s in (c.request, c.response):
                for f in s.unchecked_formats if s else ():
                    out.append(f"{c.path.name}: {s.name} uses format {f!r}, which is not checked "
                               f"(jsonschema needs an extra package for it)")
        return out


# ---------------------------------------------------------------- loading

def load_dir(folder: str | Path) -> Contracts:
    """Every ``*.contract.json`` in ``folder``. A missing or empty folder is not an error: ``found`` is False."""
    folder = Path(folder)
    if not folder.is_dir():
        return Contracts(folder, found=False)
    files = sorted(folder.glob(f"*{SUFFIX}"))
    out = Contracts(folder, found=bool(files))
    for f in files:
        c = load_file(f)
        if c.tool in out.by_tool:
            raise ContractError(f"{f}: tool {c.tool!r} already has a contract in {out.by_tool[c.tool].path.name}; "
                                "keep one file per tool")
        out.by_tool[c.tool] = c
    return out


def load_file(path: str | Path) -> Contract:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ContractError(f"{path}: cannot read it ({type(exc).__name__})") from None
    except ValueError as exc:
        raise ContractError(f"{path}: not valid JSON ({exc})") from None
    if not isinstance(data, dict):
        raise ContractError(f"{path}: a contract is a JSON object")
    unknown = sorted(set(data) - TOP_KEYS)
    if unknown:
        raise ContractError(f"{path}: {unknown[0]}: unknown key; allowed keys: {', '.join(sorted(TOP_KEYS))}")
    if data.get("contract") != 1:
        raise ContractError(f"{path}: contract: must be 1 (the format version)")
    tool = data.get("tool")
    if not isinstance(tool, str) or not tool:
        raise ContractError(f"{path}: tool: missing; it is the exact tool name the model calls")
    if not any(k in data for k in SIDES):
        raise ContractError(f"{path}: needs a request or a response section (or both)")
    for k in ("owner", "description"):
        if k in data and not isinstance(data[k], str):
            raise ContractError(f"{path}: {k}: must be text")
    c = Contract(tool, path, owner=data.get("owner"), description=data.get("description"))
    for name in SIDES:
        if name in data:
            setattr(c, name, _side(path, name, data[name]))
    return c


def _side(path: Path, name: str, raw: Any) -> Side:
    if not isinstance(raw, dict):
        raise ContractError(f"{path}: {name}: must be an object with one of {', '.join(SOURCES)}")
    unknown = sorted(set(raw) - SIDE_KEYS)
    if unknown:
        raise ContractError(f"{path}: {name}.{unknown[0]}: unknown key; allowed keys: {', '.join(sorted(SIDE_KEYS))}")
    given = [s for s in SOURCES if s in raw]
    if len(given) != 1:
        what = "both " + " and ".join(given) if given else "no source"
        raise ContractError(f"{path}: {name}: has {what}; give exactly one of {', '.join(SOURCES)}")
    src = given[0]
    if "strict" in raw and src != "pydantic":
        raise ContractError(f"{path}: {name}.strict: only applies to a pydantic source")
    if src == "pydantic":
        return _pydantic_side(path, name, raw)
    if src == "schema":
        schema = raw["schema"]
        if not isinstance(schema, (dict, bool)):
            raise ContractError(f"{path}: {name}.schema: must be a JSON Schema object")
        return _schema_side(path, name, schema, "inline schema", f"{name}.schema")
    ref = raw["schema_file"]
    if not isinstance(ref, str) or not ref:
        raise ContractError(f"{path}: {name}.schema_file: must be a path, optionally with #/json/pointer")
    if _remote(ref):
        raise ContractError(f"{path}: {name}.schema_file: {ref!r} is remote; only local files are read")
    file_part, _, pointer = ref.partition("#")
    target = (path.parent / unquote(file_part)).resolve()
    if not target.is_file():
        raise ContractError(f"{path}: {name}.schema_file: {file_part!r} not found (relative to the contract file)")
    _, doc = _read_json(target, path, f"{name}.schema_file")
    try:
        sub = _pointer(doc, pointer)
    except LookupError:
        raise ContractError(f"{path}: {name}.schema_file: pointer #{pointer} not found in {file_part}") from None
    # validate against {"$ref": file#pointer}, so refs inside the pointed schema resolve against its document
    schema = {"$ref": target.as_uri() + ("#" + pointer if pointer else "")}
    side = _schema_side(path, name, schema, f"schema_file {ref}", f"{name}.schema_file", check_target=sub)
    return side


def _pydantic_side(path: Path, name: str, raw: dict[str, Any]) -> Side:
    spec = raw["pydantic"]
    strict = raw.get("strict", True)
    if not isinstance(strict, bool):
        raise ContractError(f"{path}: {name}.strict: must be true or false")
    if not isinstance(spec, str) or ":" not in spec:
        raise ContractError(f"{path}: {name}.pydantic: use 'module:Model'")
    hint = (f"install the package in this environment, or snapshot it: "
            f"fault-contract init --pydantic {spec} --snapshot")
    try:
        from pydantic import TypeAdapter
    except ImportError:
        raise ContractError(f"{path}: {name}.pydantic: pydantic is not installed; {hint}") from None
    model = import_object(spec, path, f"{name}.pydantic", hint)
    try:
        adapter = TypeAdapter(model)
    except Exception as exc:
        raise ContractError(f"{path}: {name}.pydantic: {spec} is not a type Pydantic can validate "
                            f"({type(exc).__name__})") from None
    return Side(name, "pydantic", f"pydantic {spec}" + ("" if strict else " (lax)"), pydantic=spec,
                strict=strict, adapter=adapter)


def import_object(spec: str, path: Path | str, key: str, hint: str = "") -> Any:
    """Import ``module:attr.attr``. The only user code fault-contract runs is this import."""
    module, _, attr = spec.partition(":")
    if str(Path.cwd()) not in sys.path:  # agent code is imported from where you run, like python -m
        sys.path.insert(0, str(Path.cwd()))
    try:
        obj: Any = importlib.import_module(module)
    except Exception as exc:
        raise ContractError(f"{path}: {key}: cannot import {module} ({type(exc).__name__})"
                            + (f"; {hint}" if hint else "")) from None
    for part in attr.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError:
            raise ContractError(f"{path}: {key}: {module} has no {attr!r}") from None
    return obj


def _schema_side(path: Path, name: str, schema: dict[str, Any] | bool, source: str, key: str,
                 check_target: Any = None) -> Side:
    target = schema if check_target is None else check_target
    try:
        Draft202012Validator.check_schema(target)
    except jsonschema.SchemaError as exc:
        at = "#/" + "/".join(str(p) for p in exc.path) if exc.path else "#"
        raise ContractError(f"{path}: {key}: invalid schema at {at}: {exc.message}") from None
    base = path.resolve().as_uri()
    if isinstance(schema, dict):
        # the root is identified by the contract file; a relative $id of its own is anchored there
        own = schema.get("$id")
        schema = {**schema, "$id": urljoin(base, own) if isinstance(own, str) else base}
        base = schema["$id"]
    resources = _check_refs(schema, base, path, key)
    registry = Registry().with_resources(resources.items())
    validator = Draft202012Validator(schema, registry=registry, format_checker=FORMAT_CHECKER)
    used = set(_formats(target)) | {f for r in resources.values() for f in _formats(r.contents)}
    unchecked = tuple(sorted(f for f in used if f not in FORMAT_CHECKER.checkers))
    return Side(name, "schema", source, schema=schema if isinstance(schema, dict) else {}, validator=validator,
                registry=registry, base_uri=base, unchecked_formats=unchecked)


def _remote(ref: str) -> bool:
    return urlparse(ref).scheme in ("http", "https")


def _read_json(target: Path, path: Path, key: str) -> tuple[str, Any]:
    try:
        return target.as_uri(), json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ContractError(f"{path}: {key}: cannot read {target.name} ({type(exc).__name__})") from None
    except ValueError:
        raise ContractError(f"{path}: {key}: {target.name} is not valid JSON") from None


def _pointer(doc: Any, pointer: str) -> Any:
    if not pointer:
        return doc
    cur = doc
    for raw in pointer.lstrip("/").split("/"):
        part = unquote(raw).replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise KeyError(part)
    return cur


def walk(node: Any) -> Iterator[dict[str, Any]]:
    """Every object in a JSON document, depth first."""
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            yield n
            stack.extend(n.values())
        elif isinstance(n, list):
            stack.extend(n)


def _formats(schema: Any) -> Iterator[str]:
    for n in walk(schema):
        if isinstance(n.get("format"), str):
            yield n["format"]


NOT_SCHEMA = ("enum", "const", "examples", "default")  # keywords whose values are data, not subschemas
SCHEMA_MAPS = ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas")  # name -> schema


def _check_refs(schema: Any, base: str, path: Path, key: str) -> dict[str, Resource]:
    """Resolve every ``$ref`` now, with referencing's own resolver (so ``$id`` scopes count exactly as
    they do when validating), so a bad one is a load error and not a failure mid-run. Every schema a
    ``$ref`` reaches is checked against the metaschema. Returns the local files read, for the registry."""
    resources: dict[str, Resource] = {}

    def retrieve(uri: str) -> Resource:
        if _remote(uri):
            raise ContractError(f"{path}: {key}: $ref to {uri!r} is remote; remote $ref is refused (there is no network)")
        if urlparse(uri).scheme != "file":
            raise ContractError(f"{path}: {key}: $ref to {uri!r} is not a local file")
        target = Path(_file_path(uri))
        if not target.is_file():
            raise ContractError(f"{path}: {key}: $ref to {target.name}: file not found")
        _, contents = _read_json(target, path, key)
        resources[uri] = Resource.from_contents(contents, default_specification=DRAFT202012)
        return resources[uri]

    root = Resource.from_contents(schema, default_specification=DRAFT202012)
    registry = Registry(retrieve=retrieve).with_resource(base, root)
    seen = {id(schema)}

    def visit(node: Any, resolver: Any, entered: bool = False) -> None:
        """``entered``: the resolver is already scoped to this node (a $ref target, or the root)."""
        if isinstance(node, list):
            for x in node:
                visit(x, resolver)
            return
        if not isinstance(node, dict):
            return
        if isinstance(node.get("$id"), str) and not entered:
            resolver = resolver.in_subresource(Resource.from_contents(node, default_specification=DRAFT202012))
        ref = node.get("$ref")
        if isinstance(ref, str):
            if _remote(ref):
                raise ContractError(f"{path}: {key}: $ref {ref!r} is remote; remote $ref is refused (there is no network)")
            try:
                got = resolver.lookup(ref)
            except Exception as exc:  # referencing wraps a failed retrieve; ours carries the message
                cause: BaseException | None = exc
                while cause is not None and not isinstance(cause, ContractError):
                    cause = cause.__cause__ or cause.__context__
                if cause is not None:
                    raise cause from None
                raise ContractError(f"{path}: {key}: $ref {ref!r} points at nothing") from None
            if id(got.contents) not in seen:
                seen.add(id(got.contents))
                try:
                    Draft202012Validator.check_schema(got.contents)
                except jsonschema.SchemaError as exc:
                    raise ContractError(f"{path}: {key}: $ref {ref!r} points at an invalid schema: {exc.message}") from None
                visit(got.contents, got.resolver, entered=True)
        for k, v in node.items():
            if k in SCHEMA_MAPS and isinstance(v, dict):
                for sub in v.values():  # names are data; every value is a schema
                    visit(sub, resolver)
            elif k not in NOT_SCHEMA:
                visit(v, resolver)

    # the registry already holds the root at ``base``; a root with an $id of its own is scoped by it
    visit(schema, registry.resolver(base_uri=base), entered=_scoped_at(schema, base))
    return resources


def _scoped_at(schema: Any, base: str) -> bool:
    return not isinstance(schema, dict) or not isinstance(schema.get("$id"), str) or schema["$id"] == base


def _file_path(uri: str) -> str:
    p = unquote(urlparse(uri).path)
    if sys.platform == "win32" and p.startswith("/") and len(p) > 2 and p[2] == ":":
        p = p[1:]
    return p


def resolve_refs(schema: Any, side: Side | None = None, depth: int = 0) -> Any:
    """A copy of ``schema`` with every local ``$ref`` replaced by what it points at (for comparison and
    display). Cycles and refs that cannot be resolved are kept as ``$ref``."""
    if side is None or side.kind != "schema":
        return _resolve_pointer_refs(schema, schema, depth)
    root = Resource.from_contents(side.schema or {}, default_specification=DRAFT202012)
    resolver = side.registry.with_resource(side.base_uri, root).resolver(base_uri=side.base_uri)
    entered = schema is not side.schema or _scoped_at(schema, side.base_uri)
    return _resolve_with(schema, resolver, depth, entered)


def _resolve_with(node: Any, resolver: Any, depth: int, entered: bool = False) -> Any:
    """``entered``: the resolver is already scoped to this node (a $ref target, or the root)."""
    if isinstance(node, list):
        return [_resolve_with(x, resolver, depth) for x in node]
    if not isinstance(node, dict):
        return node
    if isinstance(node.get("$id"), str) and not entered:
        resolver = resolver.in_subresource(Resource.from_contents(node, default_specification=DRAFT202012))
    if isinstance(node.get("$ref"), str) and depth < 32:
        try:
            got = resolver.lookup(node["$ref"])
        except (Unresolvable, NoSuchResource, ValueError, KeyError, TypeError):
            return dict(node)
        rest = _resolve_with({k: v for k, v in node.items() if k != "$ref"}, resolver, depth, entered=True)
        target = _resolve_with(got.contents, got.resolver, depth + 1, entered=True)
        return merge_ref(target, rest)
    return {k: _resolve_with(v, resolver, depth) for k, v in node.items()}


def json_key(v: Any) -> Any:
    """A value as JSON Schema compares it: a boolean is never a number, and 1 equals 1.0."""
    if isinstance(v, bool) or v is None:
        return ("const", v)
    if isinstance(v, (int, float)):
        return ("number", float(v))
    if isinstance(v, str):
        return ("string", v)
    if isinstance(v, list):
        return ("array", tuple(json_key(x) for x in v))
    if isinstance(v, dict):
        return ("object", tuple(sorted((str(k), json_key(x)) for k, x in v.items())))
    return ("other", repr(v))


def json_equal(a: Any, b: Any) -> bool:
    return json_key(a) == json_key(b)


EXTRA_KEYWORDS = ("additionalProperties", "unevaluatedProperties", "propertyNames", "minProperties", "maxProperties")


def merge_ref(target: Any, rest: dict[str, Any]) -> Any:
    """A ``$ref`` target and its sibling keywords as one schema. Both apply, so nothing is dropped:
    property maps are joined, ``required`` lists united, and anything that truly conflicts is kept
    side by side as ``allOf`` (which ``defs`` reports as not compared)."""
    if not rest:
        return target
    if not isinstance(target, dict):
        return {"allOf": [target, rest]}
    # additionalProperties and its kin are judged against each schema's own properties: joining the
    # maps would change what they allow, so such a pair stays a conjunction
    def extra(s: dict[str, Any]) -> bool:
        return any(k in s for k in EXTRA_KEYWORDS)

    def props(s: dict[str, Any]) -> bool:
        return "properties" in s or "patternProperties" in s

    if (extra(target) and props(rest)) or (extra(rest) and props(target)):
        return {"allOf": [target, rest]}
    out = dict(target)
    for k, v in rest.items():
        if k not in out or json_equal(out[k], v):
            out[k] = v
        elif k in ("properties", "patternProperties", "$defs") and isinstance(v, dict) and isinstance(out[k], dict):
            joined = dict(out[k])
            for name, sub in v.items():
                same = name not in joined or json_equal(joined[name], sub)
                joined[name] = sub if same else {"allOf": [joined[name], sub]}
            out[k] = joined
        elif k == "required" and isinstance(v, list) and isinstance(out[k], list):
            out[k] = list(dict.fromkeys([*out[k], *v]))
        elif k in ("$id", "$comment", "title", "description"):
            continue
        else:
            return {"allOf": [target, rest]}
    return out


def _resolve_pointer_refs(node: Any, root: Any, depth: int) -> Any:
    if isinstance(node, list):
        return [_resolve_pointer_refs(x, root, depth) for x in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#") and depth < 32:
        try:
            target = _pointer(root, ref[1:])
        except (LookupError, ValueError):
            return dict(node)
        rest = _resolve_pointer_refs({k: v for k, v in node.items() if k != "$ref"}, root, depth)
        target = _resolve_pointer_refs(target, root, depth + 1)
        return merge_ref(target, rest)
    return {k: _resolve_pointer_refs(v, root, depth) for k, v in node.items()}
