# fault-contract

Write down the exact shape of data an agent sends to each tool and reads back from it, then check
every real tool call against it: in pytest, in trace exports, and against the tool definitions the
model is given.

A **contract** is one small JSON file per tool. It says what the agent may send (the request) and what
it relies on getting back (the response), as JSON Schema 2020-12 or as the tool's own Pydantic model.
Checks read data that was already sent and received: nothing calls a tool, a backend or the network.

> **Status:** 0.1.0 alpha. MIT, free. Needs faultgate-zero 0.2 (check plugins, `formats.load`).

## 5-minute quickstart

The repo includes faultgate-zero's refund agent with contracts for its four tools.

```bash
git clone https://github.com/Faultline-lab/fault-contract && cd fault-contract
python -m venv .venv
.venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip install -e ".[dev]"
cd examples/refund_agent
```

Until faultgate-zero 0.2.0 is on PyPI, install it from source first:
`.venv/bin/pip install "faultgate-zero[formats] @ git+https://github.com/Faultline-lab/faultgate-zero@feat/check-plugins-formats"`.
Commands below assume the venv is active (`source .venv/bin/activate`, Windows `.venv\Scripts\activate`).

**1. Draft contracts from what already exists.** (The example already has them; this shows how they
were started. `init` never overwrites a file.)

```bash
fault-contract init --defs-module refund_agent:TOOLS --contracts /tmp/drafts
```

It copies each tool definition's parameters and adds `"additionalProperties": false` on every object.
It never invents required fields: it tells you to decide them. Add response sides from a trace
export with `fault-contract init --from-traces EXPORT --side response --all-tools`.

**2. Review and tighten them in a pull request.** `.faultline/contracts/issue_refund.contract.json`:

```json
{
  "contract": 1,
  "tool": "issue_refund",
  "owner": "billing",
  "request": {
    "schema": {
      "type": "object",
      "properties": {
        "order_id":    {"type": "string", "pattern": "^[0-9]{4,}$"},
        "amount":      {"type": "number", "exclusiveMinimum": 0},
        "customer_id": {"type": "string"}
      },
      "required": ["order_id", "amount", "customer_id"],
      "additionalProperties": false
    }
  },
  "response": {
    "schema": {
      "type": "object",
      "properties": {"refunded": {"type": "number"}, "order_id": {"type": "string"}},
      "required": ["refunded", "order_id"]
    }
  }
}
```

**3. Tests check it automatically.** With fault-contract installed, faultgate-zero runs the `contract`
check through its plugin entry point. No test changes:

```bash
pytest
```

The example's tests give the same result as with faultgate-zero alone (one passes, six fail on
faultgate-zero's own checks): their tool calls match the contracts. Change the fake model in
`test_refunds_one_double_charge` to send `"amount": "20.00"` and that test fails too. The agent's
`issue_refund` echoes the amount back, so the result breaks its contract as well:

```
faultgate-zero: 1 check fired on run 'refund-double-charge' (3 steps, 4 model calls)
  contract  step 3 issue_refund request $.amount: expected number, got string
            step 3 issue_refund response $.refunded: expected number, got string
```

**4. Check real traffic.** Export runs (`pytest --fgz-export-otlp runs.json`, or any Langfuse,
LangSmith, Braintrust or OpenInference export) and check them:

```bash
fault-contract check runs.json
```

```
format     otlp (detected)   traces 7   tool calls 17   with a contract 17

issue_refund   request   1 of 2 calls
  100%  $.amount    expected number, got string  e.g. trace 7418fc74… step 3
issue_refund   response  1 of 2 results
  100%  $.refunded  expected number, got string  e.g. trace 7418fc74… step 3

FAIL  2 violations in 1 tool
```

Locations are a hashed trace id and a step; `fault-contract explain runs.json --trace 7418fc74`
shows that run step by step.

**5. Keep definitions and contracts in step, in CI.**

```bash
fault-contract defs --defs-module refund_agent:TOOLS
```

```
issue_refund     WARN  D7  $.order_id is required by the contract, but the tool definition marks it optional
issue_refund     WARN  D7  $.amount is required by the contract, but the tool definition marks it optional
issue_refund     WARN  D7  $.customer_id is required by the contract, but the tool definition marks it optional
issue_refund     info      not compared: $.order_id pattern (contract); $.amount exclusiveMinimum (contract)
lookup_order     OK
search_orders    OK
verify_identity  OK
0 errors, 3 warnings   (--strict makes warnings fail)
```

The fix is in the agent code: mark those fields `required` in `TOOLS`, so the model is told what the
backend needs.

## Contract, policy and tool definition

| | Tool definition | Contract (fault-contract) | Policy (faultgate-zero) |
|---|---|---|---|
| Answers | What does the model think this tool takes? | What shape must the data have, in and out? | What is the agent allowed to do? |
| Covers results | No | Yes | Only "an earlier call succeeded" |
| Example | `amount: number` | `amount` is a number above 0; `customer_id` required; no extra fields | `issue_refund.amount ≤ 500`; only after `verify_identity` |
| File | Agent code | `.faultline/contracts/*.contract.json` | `.faultline/policy.json` |

**Shape goes in the contract** (types, required fields, formats, allowed values of a field's type).
**Business limits and order go in the policy.** Do not write the same limit in both places.

Tool owners review what the agent depends on: name them in `CODEOWNERS` for their tool's file,
for example `.faultline/contracts/issue_refund.contract.json @your-org/billing`.

## Contract files

One file per tool in `.faultline/contracts/`, named `<tool>.contract.json` (set another folder with
`[tool.fault_contract] contracts = "..."`, `FAULT_CONTRACT_DIR` or `--contracts`).

| Key | Required | Meaning |
|---|---|---|
| `contract` | yes | Format version: `1`. |
| `tool` | yes | The exact tool name the model calls. Case-sensitive. |
| `request` | one of the two | The shape of the arguments the agent sends. |
| `response` | one of the two | The shape of the successful result the agent relies on. List only the fields the agent reads. |
| `owner`, `description` | no | For people. Never checked. |

Each side has exactly one source:

| Source | Example | When |
|---|---|---|
| `schema` | `{"schema": {"type": "object", ...}}` | Most tools. Inline JSON Schema 2020-12. |
| `schema_file` | `{"schema_file": "../../api/openapi.json#/components/schemas/RefundRequest"}` | The tool owner already publishes a JSON Schema or OpenAPI JSON file. Relative to the contract file; an optional JSON pointer follows `#`. |
| `pydantic` | `{"pydantic": "billing.api.models:RefundRequest", "strict": true}` | The tool validates with a Pydantic model: the contract is the backend's own model. `strict` defaults to `true`. |

Loading rules: unknown keys, a missing `tool`, both or neither source on a side, two files for one
tool, a schema that breaks the 2020-12 metaschema, a `$ref` that points at nothing, a remote `$ref`
(`http://`, `https://`) or a Pydantic model that cannot be imported are errors (exit 2) naming the file
and the key. `$ref` may point inside the document (`#/$defs/...`) or at a local file.

**Importing a Pydantic model runs that module's import-time code** (settings, connections). It is the
only code of yours fault-contract runs. If that is a problem, snapshot the model:
`fault-contract init --pydantic billing.api.models:RefundRequest --tool issue_refund --side request --snapshot`.

### Formats that are checked

jsonschema checks these `format` values with no extra package: `date`, `email`, `idn-email`, `ipv4`,
`ipv6`, `regex`, `uuid` (and `idn-hostname` when `idna` is installed). Others, notably `date-time`,
`time`, `uri` and `hostname`, need extra packages and are **not** checked otherwise; fault-contract
prints a note for every unchecked format a contract uses (`fault-contract contracts` lists them).
Use a `pattern` instead, or install jsonschema's format extras.

## What is checked

For each tool step: the contract is found by the exact tool name. A tool with no contract is listed
(or, with `unknown_tools = "fail"`, a violation). The arguments are validated against `request`;
arguments that are not JSON are one `not_json` violation. The result is validated against `response`,
except when the contract has no response, the result never went back to the model, or the result is
an error (as faultgate-zero's `error_of` sees it: error shapes are not in v1).

JSON Schema never converts types: `"45.00"` against `{"type": "number"}` is a violation. For Pydantic
sources a call is validated in strict mode, then in lax mode: **rejected** means a backend with that
model would refuse it, **coerced** means it is accepted only by converting values. Both fail by
default; `coerced = "ignore"` reports coerced calls without failing.

Messages are plain words built from the keyword, the contract's schema value and the type of the
value: `expected number, got string`, `missing required field`, `field override_limit is not in the
contract`, `value is not one of: pending, shipped, delivered`, `text does not match the pattern
^[0-9]{4,}$`. jsonschema's own message text is never printed, because it quotes the rejected value.

## Commands

| Command | Does | Exit |
|---|---|---|
| `init --defs FILE \| --defs-module MOD:ATTR [--allow-extra]` | Contracts from tool definitions (OpenAI Chat, OpenAI Responses or Anthropic). | 0 / 2 |
| `init --pydantic MOD:MODEL --tool NAME --side request\|response [--snapshot]` | A side that points at the model, or its schema inline. | 0 / 2 |
| `init --from-traces PATH... --side response (--tool NAME \| --all-tools)` | A schema inferred from successful results: types and keys only; `required` only for keys in 100% of objects; never `enum`, `format`, `pattern` or bounds; depth 4; objects keyed by private data collapsed. | 0 / 2 |
| `check PATH... [--json P] [--markdown P] [--tool NAME] [--max-examples N]` | Validates every tool call in exports and fixture folders (a folder means its `*.json` files). | 0 clean, 1 violations, 2 input error |
| `defs --defs FILE \| --defs-module MOD:ATTR [--strict]` | Compares definitions with contracts: D1 tool without contract (error, or warning while `unknown_tools = "ignore"`), D2 stale contract (warning), D3 required field missing from the definition, D4 extra field the contract forbids, D5 different types, D6 enum values the contract refuses (errors), D7 required but optional in the definition (warning). Lists what it does not compare: `pattern`, `format`, bounds, `oneOf`/`anyOf`/`allOf`, non-local `$ref`. | 1 on an error (or a warning with `--strict`) |
| `explain PATH --trace ID [--step N]` | One trace, step by step: settings, contract used, keys and types of arguments and result, every error with its schema path, Pydantic strict and lax outcome. `ID` is the trace id or the hashed id `check` prints. | 0 / 2 |
| `contracts` | Lists loaded contracts, their sources and unchecked formats; validates every file. | 0 / 2 |

Shared options: `--contracts DIR`, `--format auto|otlp|openinference|langfuse|langsmith|braintrust`,
`--coerced fail|ignore`, `--unknown-tools ignore|fail`, `-v` (or `FAULT_CONTRACT_DEBUG=1`) for stage
counts and timings on stderr.

## Settings

First match wins: a command-line option, an environment variable (`FAULT_CONTRACT_DIR`,
`FAULT_CONTRACT_COERCED`, `FAULT_CONTRACT_UNKNOWN_TOOLS`), `[tool.fault_contract]` in `pyproject.toml`,
then the default.

```toml
[tool.fault_contract]
contracts = ".faultline/contracts"
coerced = "fail"            # fail | ignore
unknown_tools = "ignore"    # ignore | fail
```

## In pytest

The check is registered with faultgate-zero (`faultgate_zero.checks` entry point, on by default).
Everything else is faultgate-zero's: `@pytest.mark.faultgate(skip=["contract"])`, the `fail`, `warn`
and `off` modes, `--fgz-json` and `--fgz-markdown` reports, xdist, `run.assert_clean()`. Contracts load
once per session; a bad contract stops the session with the loader's message, as a bad policy file
does. With no contracts folder the check never fires ("no contracts found at .faultline/contracts"):
installing fault-contract in a repo without contracts changes nothing.

The pytest check and `fault-contract check` call the same function, `validate_trace`, so the same
run gives identical violations in both places.

## Privacy

- Payload values appear only as redacted previews of 40 characters or fewer, and only in `explain`
  and the JSON and Markdown reports. The terminal output of `check` prints paths, messages and
  locations, never values. Locations are a hashed trace id and a step number.
- Field names in paths are redacted too (a dictionary key can be an email address).
- `init --from-traces` writes keys and types, never values.
- Nothing leaves the machine.

## Debugging

| Symptom | Likely cause | Check, and the fix |
|---|---|---|
| "no contract" for a tool that has one | Names differ: matching is exact and case-sensitive. | `check` lists the names actually called. Make `tool` match. |
| Exit 2 "invalid schema" | The schema breaks JSON Schema 2020-12. | The message names the file and schema path. Then run `fault-contract contracts`. |
| Exit 2 "cannot import billing.api.models" | The backend package is not installed here. | Install it, or `init --pydantic ... --snapshot`. |
| Many `additionalProperties` violations right after init | The model sends fields the definitions never listed, or a framework wraps the arguments. | `explain` shows the real keys. Add them, or allow extra on that object. |
| Every Pydantic call is `coerced` | The model sends numbers or booleans as text. | Fix the definition's types or the prompt; `coerced = "ignore"` only as a temporary step. |
| Response violations on calls that failed | The result was not recognised as an error. | Return a clear error signal (an error flag, an `Error:` prefix or an `error` field). |
| Different results in pytest and offline | Different contracts folder or settings. | Compare the `settings` line at the top of `explain` in both places. |
| A `format` rule never fires | jsonschema does not check that format without an extra package. | `fault-contract contracts` lists unchecked formats. Use a `pattern`. |
| "check crashed: contract" in the pytest summary | A bug in fault-contract; faultgate-zero kept your test safe. | Reproduce with `fault-contract explain` on the exported run and file it. |

## Evaluation

`python evals/run.py` prints the acceptance table with measured values: the 12 planted mutation
types (`evals/mutate.py`), the clean corpus (refund agent runs with JSON Schema and Pydantic
contracts, and faultgate's example traces), definition drift, privacy (planted values searched in
every output), safe drafting, faultgate-zero's own suite with fault-contract installed, the network
blocked, and 25,000 tool calls timed. `python evals/build_corpus.py` regenerates the refund corpus.

## Development

```bash
pip install -e "path/to/faultgate-zero[formats]" -e ".[dev]"
pytest                    # the unit suite; pytest -m slow runs the performance test
python evals/run.py       # the acceptance table
```

## License

MIT.
