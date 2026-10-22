"""Settings resolution. First found wins: a command-line option, an environment variable,
``[tool.fault_contract]`` in pyproject.toml, then the default."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONTRACTS = Path(".faultline") / "contracts"
PYPROJECT_KEYS = {"contracts", "coerced", "unknown_tools"}
CHOICES = {"coerced": ("fail", "ignore"), "unknown_tools": ("ignore", "fail")}
ENV = {"contracts": "FAULT_CONTRACT_DIR", "coerced": "FAULT_CONTRACT_COERCED",
       "unknown_tools": "FAULT_CONTRACT_UNKNOWN_TOOLS"}


class ConfigError(ValueError):
    """Bad settings. The CLI maps it to exit code 2."""


@dataclass(frozen=True)
class Settings:
    contracts: Path = DEFAULT_CONTRACTS
    coerced: str = "fail"
    unknown_tools: str = "ignore"
    debug: bool = False
    root: Path = field(default_factory=Path.cwd)

    @property
    def contracts_dir(self) -> Path:
        return self.contracts if self.contracts.is_absolute() else self.root / self.contracts

    def line(self) -> str:
        """One line for explain and debug output, so two places can be compared."""
        return (f"contracts={self.contracts.as_posix()} coerced={self.coerced} "
                f"unknown_tools={self.unknown_tools}")


def find_root(start: Path | None = None) -> Path:
    """The nearest folder at or above ``start`` that has a pyproject.toml, else ``start``."""
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / "pyproject.toml").is_file():
            return d
    return here


def _pyproject(root: Path) -> dict[str, Any]:
    p = root / "pyproject.toml"
    if not p.is_file():
        return {}
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{p}: {exc}") from None
    section = data.get("tool", {}).get("fault_contract", {})
    if not isinstance(section, dict):
        raise ConfigError(f"{p}: [tool.fault_contract] must be a table")
    unknown = set(section) - PYPROJECT_KEYS
    if unknown:
        raise ConfigError(f"{p}: [tool.fault_contract] {sorted(unknown)[0]}: unknown key; "
                          f"allowed keys: {', '.join(sorted(PYPROJECT_KEYS))}")
    return section


def _first(*vals: Any) -> Any:
    return next((v for v in vals if v is not None and v != ""), None)


def resolve(cli: dict[str, Any] | None = None, env: dict[str, str] | None = None,
            root: Path | None = None) -> Settings:
    """``cli``: option values (None when not given). ``env``: os.environ by default."""
    cli = cli or {}
    env = os.environ if env is None else env
    root = root or find_root()
    pp = _pyproject(root)
    values: dict[str, Any] = {}
    for key in ("contracts", "coerced", "unknown_tools"):
        v = _first(cli.get(key), env.get(ENV[key]), pp.get(key))
        if v is None:
            continue
        if key in CHOICES and v not in CHOICES[key]:
            raise ConfigError(f"{key} {v!r}: use one of {', '.join(CHOICES[key])}")
        if key == "contracts":
            if not isinstance(v, (str, os.PathLike)):
                raise ConfigError(f"contracts {v!r}: must be a folder path")
            # a path typed on the command line or in the shell is relative to where you are;
            # one in pyproject.toml, to the project root
            from_shell = _first(cli.get(key), env.get(ENV[key])) is not None
            v = Path(v) if Path(v).is_absolute() or not from_shell else Path.cwd() / v
        values[key] = v
    debug = bool(cli.get("debug")) or env.get("FAULT_CONTRACT_DEBUG", "") not in ("", "0")
    return Settings(root=root, debug=debug, **values)
