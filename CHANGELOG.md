# Changelog

## 0.1.0 (unreleased)

First version.

- Contract files (`.faultline/contracts/<tool>.contract.json`): request and response sides, each from
  an inline JSON Schema 2020-12, a local schema file with a JSON pointer (OpenAPI components work), or
  a Pydantic model; loading errors name the file and key; metaschema check; local `$ref` resolved at
  load time, remote `$ref` refused; unchecked formats listed.
- One validator (`validate_trace`) for pytest and offline: every JSON Schema error with plain-words
  messages; Pydantic strict then lax, telling rejected from coerced; `coerced` and `unknown_tools`
  settings; redacted paths and previews of 40 characters or fewer.
- The `contract` check for faultgate-zero 0.2 (`faultgate_zero.checks` entry point, on by default).
- `fault-contract check` on trace exports and fixture folders (every format faultgate reads), with
  terminal, JSON and Markdown reports; `defs` (rules D1 to D7); `init` from tool definitions,
  Pydantic models and traces; `explain`; `contracts`.
- Evaluation: clean corpus, 12 mutation types, acceptance table (`evals/run.py`).
