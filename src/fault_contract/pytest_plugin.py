"""pytest side of the ``contract`` check: load contracts once, when the session starts.

The check itself runs inside faultgate-zero (``zero_check``). This plugin only fixes the project root
to pytest's rootdir and turns a bad contract into a usage error before any test runs.
"""
from __future__ import annotations

import warnings

import pytest

from . import zero_check
from .config import ConfigError
from .contract import ContractError


class FaultContractWarning(UserWarning):
    pass


def pytest_configure(config: pytest.Config) -> None:
    try:
        state = zero_check.configure(root=config.rootpath)
    except (ConfigError, ContractError) as exc:
        raise pytest.UsageError(f"fault-contract: {exc}") from None
    for note in state.contracts.notes():
        try:  # under -W error a note must not stop the session
            warnings.warn(f"fault-contract: {note}", FaultContractWarning, stacklevel=1)
        except Exception:
            pass


def pytest_unconfigure(config: pytest.Config) -> None:
    zero_check.reset()
