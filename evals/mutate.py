"""Mutation testing, the main proof: plant one shape error at a time in a clean corpus and check it is
caught with the right path and keyword (and, for Pydantic contracts, the right kind).

    python evals/mutate.py            # prints one row per corpus and mutation type

Every mutated call must be reported, its expected (path, keyword) must be among the violations, and
every other call in the run must report nothing. The random seed is fixed.
"""
from __future__ import annotations

import json
import random
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterator

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE / "corpus"))   # refund_models for the Pydantic copies

from faultgate_zero import formats  # noqa: E402
from faultgate_zero.trace import Step, Trace  # noqa: E402

from fault_contract.check import expand  # noqa: E402
from fault_contract.config import Settings  # noqa: E402
from fault_contract.contract import Contracts, Side, load_dir, resolve_refs  # noqa: E402
from fault_contract.validate import fmt_path, response_checked, validate_trace  # noqa: E402

SEED = 20261022
PER_TYPE = 25


@dataclass(frozen=True)
class Corpus:
    name: str
    paths: tuple[Path, ...]
    contracts: Path
    engine: str  # "schema" | "pydantic"


CORPORA = (
    Corpus("refund", (HERE / "corpus" / "refund.otlp.json",), ROOT / "examples" / "refund_agent" / ".faultline" / "contracts", "schema"),
    Corpus("refund-pydantic", (HERE / "corpus" / "refund.otlp.json",), HERE / "contracts" / "refund_pydantic", "pydantic"),
    Corpus("faultgate", (HERE / "corpus" / "faultgate",), HERE / "contracts" / "faultgate", "schema"),
)


def load_corpus(c: Corpus) -> tuple[list[tuple[Trace, list[Step]]], Contracts]:
    traces = [t for f in expand(list(c.paths)) for t in formats.load(f)]
    return [(t, t.steps) for t in traces], load_dir(c.contracts)


# ---------------------------------------------------------------- sites: where a mutation can go

@dataclass(frozen=True)
class Site:
    parts: tuple[Any, ...]
    value: Any
    schema: dict[str, Any]


def _types(s: dict[str, Any]) -> set[str]:
    t = s.get("type")
    return {t} if isinstance(t, str) else set(t) if isinstance(t, list) else set()


def sites(value: Any, schema: Any, parts: tuple[Any, ...] = ()) -> Iterator[Site]:
    if not isinstance(schema, dict):
        return
    yield Site(parts, value, schema)
    if isinstance(value, dict) and isinstance(schema.get("properties"), dict):
        for k, v in value.items():
            if k in schema["properties"]:
                yield from sites(v, schema["properties"][k], (*parts, k))
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(value):
            yield from sites(v, schema["items"], (*parts, i))


def set_at(doc: Any, parts: tuple[Any, ...], new: Any) -> Any:
    if not parts:
        return new
    doc = json.loads(json.dumps(doc))
    cur = doc
    for p in parts[:-1]:
        cur = cur[p]
    cur[parts[-1]] = new
    return doc


def del_at(doc: Any, parts: tuple[Any, ...]) -> Any:
    doc = json.loads(json.dumps(doc))
    cur = doc
    for p in parts[:-1]:
        cur = cur[p]
    del cur[parts[-1]]
    return doc


def rename_at(doc: Any, parts: tuple[Any, ...], new_key: str) -> Any:
    doc = json.loads(json.dumps(doc))
    cur = doc
    for p in parts[:-1]:
        cur = cur[p]
    cur[new_key] = cur.pop(parts[-1])
    return doc


# ---------------------------------------------------------------- the 12 mutations

@dataclass(frozen=True)
class Planned:
    raw: str                 # the mutated payload text
    path: str                # expected violation path
    keyword: str             # expected keyword
    kind: str                # expected Pydantic kind


def _num(s: Site) -> bool:
    return isinstance(s.value, (int, float)) and not isinstance(s.value, bool) and bool(_types(s.schema) & {"number", "integer"})


def m1(doc, s, side):   # number becomes numeric text
    if _num(s):
        return Planned(json.dumps(set_at(doc, s.parts, str(s.value))), fmt_path(s.parts), "type", "coerced")


def m2(doc, s, side):   # number becomes non-numeric text
    if _num(s):
        return Planned(json.dumps(set_at(doc, s.parts, "not a number")), fmt_path(s.parts), "type", "rejected")


def m3(doc, s, side):   # required field removed
    req = [k for k in s.schema.get("required") or [] if isinstance(s.value, dict) and k in s.value]
    if req and side == "request":
        k = req[0]
        return Planned(json.dumps(del_at(doc, (*s.parts, k))), fmt_path((*s.parts, k)), "required", "rejected")


def m4(doc, s, side):   # unknown field added
    if isinstance(s.value, dict) and s.schema.get("additionalProperties") is False:
        new = set_at(doc, (*s.parts, "zz_unexpected"), True) if s.parts else {**doc, "zz_unexpected": True}
        return Planned(json.dumps(new), fmt_path((*s.parts, "zz_unexpected")), "additionalProperties", "rejected")


def m5(doc, s, side):   # enum value replaced by one outside the set
    if isinstance(s.schema.get("enum"), list) and s.value in s.schema["enum"]:
        return Planned(json.dumps(set_at(doc, s.parts, "zz-not-allowed")), fmt_path(s.parts), "enum", "rejected")


def m6(doc, s, side):   # null where null is not allowed
    t = _types(s.schema)
    if s.parts and t and "null" not in t and "enum" not in s.schema and s.value is not None:
        return Planned(json.dumps(set_at(doc, s.parts, None)), fmt_path(s.parts), "type", "rejected")


def m7(doc, s, side):   # nested field gets the wrong type
    t = _types(s.schema)
    if len(s.parts) >= 2 and isinstance(s.parts[-1], str) and t & {"string", "number", "integer", "boolean"} \
            and "enum" not in s.schema and not isinstance(s.value, (dict, list)):
        wrong = 12345 if "string" in t else {"nested": True}
        return Planned(json.dumps(set_at(doc, s.parts, wrong)), fmt_path(s.parts), "type", "rejected")


def m8(doc, s, side):   # array item gets the wrong type
    if s.parts and isinstance(s.parts[-1], int) and "enum" not in s.schema:
        wrong = "zz-not-an-object" if isinstance(s.value, dict) else {"zz": True}
        return Planned(json.dumps(set_at(doc, s.parts, wrong)), fmt_path(s.parts), "type", "rejected")


def m9(doc, s, side):   # array replaced by a single object
    if isinstance(s.value, list) and s.value and "array" in _types(s.schema):
        one = s.value[0] if isinstance(s.value[0], dict) else {"item": s.value[0]}
        return Planned(json.dumps(set_at(doc, s.parts, one)), fmt_path(s.parts), "type", "rejected")


def m10(doc, s, side):  # result field renamed (status -> state)
    req = [k for k in s.schema.get("required") or [] if isinstance(s.value, dict) and k in s.value]
    if req and side == "response":
        k = "status" if "status" in req else req[0]
        new_key = "state" if k == "status" else f"{k}_renamed"
        return Planned(json.dumps(rename_at(doc, (*s.parts, k), new_key)), fmt_path((*s.parts, k)), "required", "rejected")


def m11(doc, s, side):  # result replaced by plain text
    if side == "response" and not s.parts:
        return Planned("Done, I looked that up for you.", "$", "not_json", "not_json")


def m12(doc, s, side):  # arguments encoded twice
    if side == "request" and not s.parts:
        return Planned(json.dumps(json.dumps(doc)), "$", "type", "rejected")


MUTATIONS: dict[int, tuple[str, Callable]] = {
    1: ("number becomes numeric text", m1), 2: ("number becomes non-numeric text", m2),
    3: ("required field removed", m3), 4: ("unknown field added", m4), 5: ("enum value outside the set", m5),
    6: ("null where not allowed", m6), 7: ("nested field wrong type", m7), 8: ("array item wrong type", m8),
    9: ("array replaced by an object", m9), 10: ("result field renamed", m10), 11: ("result replaced by text", m11),
    12: ("arguments encoded twice", m12),
}


# ---------------------------------------------------------------- running

@dataclass
class Row:
    corpus: str
    engine: str
    number: int
    name: str
    eligible: int = 0
    mutated: int = 0
    reported: int = 0
    matched: int = 0
    kind_ok: int = 0
    others_clean: int = 0
    misses: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        n = self.mutated
        return n > 0 and self.reported == n and self.matched == n and self.others_clean == n and \
            (self.engine != "pydantic" or self.kind_ok == n)


def _schema_of(side: Side) -> dict[str, Any]:
    return resolve_refs(side.json_schema(), side if side.kind == "schema" else None)


def candidates(runs, contracts: Contracts, fn: Callable) -> list[tuple[int, int, str, Planned]]:
    """(run index, step position, side, planned mutation) for every place this mutation fits."""
    out = []
    schemas: dict[tuple[str, str], dict] = {}
    for ri, (_, steps) in enumerate(runs):
        for si, st in enumerate(steps):
            c = contracts.get(st.tool)
            if c is None:
                continue
            for side_name in ("request", "response"):
                side = c.side(side_name)
                if side is None or (side_name == "response" and not response_checked(st, c)):
                    continue
                raw = (st.arguments or "{}") if side_name == "request" else st.result
                try:
                    doc = json.loads(raw)
                except ValueError:
                    continue
                key = (st.tool, side_name)
                if key not in schemas:
                    schemas[key] = _schema_of(side)
                for s in sites(doc, schemas[key]):
                    p = fn(doc, s, side_name)
                    if p is not None:
                        out.append((ri, si, side_name, p))
    return out


def run_corpus(c: Corpus, settings: Settings, rng: random.Random) -> list[Row]:
    runs, contracts = load_corpus(c)
    rows = []
    for number, (name, fn) in MUTATIONS.items():
        row = Row(c.name, c.engine, number, name)
        cands = candidates(runs, contracts, fn)
        row.eligible = len(cands)
        for ri, si, side, p in rng.sample(cands, min(PER_TYPE, len(cands))):
            trace, steps = runs[ri]
            st = steps[si]
            changed = replace(st, arguments=p.raw) if side == "request" else replace(st, result=p.raw)
            mutated = [changed if i == si else s for i, s in enumerate(steps)]
            vs = validate_trace(trace, contracts, settings, mutated)
            here = [v for v in vs if v.step == st.index and v.side == side]
            row.mutated += 1
            row.reported += bool(here)
            hit = [v for v in here if (v.path, v.keyword) == (p.path, p.keyword)]
            row.matched += bool(hit)
            row.kind_ok += bool(hit) and hit[0].kind == (p.kind if c.engine == "pydantic" else
                                                          ("not_json" if p.keyword == "not_json" else "schema"))
            row.others_clean += all(v.step == st.index and v.side == side for v in vs)
            if not hit or not row.others_clean == row.mutated:
                got = ", ".join(f"{v.path} {v.keyword} {v.kind}" for v in here) or "nothing"
                row.misses.append(f"{st.tool} {side} expected {p.path} {p.keyword} {p.kind}; got {got}")
        rows.append(row)
    return rows


def clean_counts(settings: Settings) -> dict[str, tuple[int, int]]:
    """Violations on the unmutated corpora: (tool calls, violations). Must be 0 violations."""
    out = {}
    for c in CORPORA:
        runs, contracts = load_corpus(c)
        vs = [v for t, steps in runs for v in validate_trace(t, contracts, settings, steps)]
        out[c.name] = (sum(len(s) for _, s in runs), len(vs))
    return out


def run_all() -> list[Row]:
    rng = random.Random(SEED)
    settings = Settings(root=ROOT)
    return [r for c in CORPORA for r in run_corpus(c, settings, rng)]


def main() -> int:
    rows = run_all()
    print(f"{'corpus':<16} {'#':>2}  {'mutation':<32} {'eligible':>8} {'mutated':>7} {'caught':>6} {'path+kw':>7} "
          f"{'kind':>5} {'clean':>5}")
    for r in rows:
        kind = str(r.kind_ok) if r.engine == "pydantic" else "-"
        print(f"{r.corpus:<16} {r.number:>2}  {r.name:<32} {r.eligible:>8} {r.mutated:>7} {r.reported:>6} "
              f"{r.matched:>7} {kind:>5} {r.others_clean:>5}" + ("" if r.ok or not r.mutated else "  MISS"))
        for m in r.misses[:3]:
            print(f"      {m}")
    covered = {r.number for r in rows if r.mutated}
    bad = [r for r in rows if r.mutated and not r.ok]
    print(f"\nmutation types covered: {len(covered)} of 12; rows with a miss: {len(bad)}")
    return 0 if len(covered) == 12 and not bad else 1


if __name__ == "__main__":
    sys.exit(main())
