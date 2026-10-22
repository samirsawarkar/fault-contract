"""fault-contract: check every tool call an agent makes against a per-tool contract, in and out."""
__version__ = "0.1.0"

from .config import Settings  # noqa: E402
from .contract import Contract, ContractError, Contracts, load_dir  # noqa: E402
from .validate import Violation, validate_step, validate_trace  # noqa: E402

__all__ = ["Contract", "ContractError", "Contracts", "Settings", "Violation", "__version__", "load_dir",
           "validate_step", "validate_trace"]
