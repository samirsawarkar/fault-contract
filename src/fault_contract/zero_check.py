"""The ``contract`` check for faultgate-zero, registered through the ``faultgate_zero.checks`` entry point.

Contracts load once per process. Inside pytest, ``fault_contract.pytest_plugin`` loads them when the
session starts (a bad contract stops the session, as a bad policy file does); anywhere else they load
on the first check, from the nearest pyproject.toml.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from faultgate_zero.checks import Options, Verdict
from faultgate_zero.trace import Trace

from . import config
from .contract import Contracts, load_dir
from .validate import counted, validate_trace

MAX_LISTED = 5


@dataclass
class _Loaded:
    settings: config.Settings
    contracts: Contracts


_state: _Loaded | None = None


def configure(root: Path | None = None) -> _Loaded:
    """Resolve settings and load contracts now. Raises ConfigError or ContractError."""
    global _state
    settings = config.resolve(root=root)
    _state = _Loaded(settings, load_dir(settings.contracts_dir))
    return _state


def reset() -> None:
    global _state
    _state = None


def loaded() -> _Loaded:
    return _state if _state is not None else configure()


def _where(s: config.Settings) -> str:
    try:
        return s.contracts_dir.relative_to(s.root).as_posix()
    except ValueError:
        return s.contracts_dir.as_posix()


def verdict(trace: Trace, state: _Loaded) -> Verdict:
    if not state.contracts.found:
        return Verdict("contract", False, f"no contracts found at {_where(state.settings)}")
    steps = trace.steps
    found = validate_trace(trace, state.contracts, state.settings, steps)
    fire = [v for v in found if counted(v, state.settings)]
    if not fire:
        note = f"; {len(found)} coerced (coerced = ignore)" if found else ""
        return Verdict("contract", False, f"{len(steps)} tool calls match their contracts{note}")
    parts = [v.line() for v in fire[:MAX_LISTED]]
    if len(fire) > MAX_LISTED:
        parts.append(f"and {len(fire) - MAX_LISTED} more")
    return Verdict("contract", True, "; ".join(parts), fire[0].step)


class ContractCheck:
    name = "contract"
    default = True

    def run(self, trace: Trace, options: Options) -> Verdict:
        return verdict(trace, loaded())


CHECK = ContractCheck()
