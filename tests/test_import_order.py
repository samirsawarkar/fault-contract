"""The contract check loads whatever is imported first: faultgate-zero discovers its plugins while it is
being imported, which can be in the middle of importing fault_contract (CI found this)."""
import subprocess
import sys

import pytest

PROBE = ("import {first}; from faultgate_zero import checks; "
         "assert 'contract' in checks.PLUGINS and 'contract' in checks.DEFAULT_CHECKS, checks.PLUGIN_ERRORS; "
         "assert checks.PLUGIN_ERRORS == [], checks.PLUGIN_ERRORS")


@pytest.mark.parametrize("first", ["fault_contract", "fault_contract.validate", "fault_contract.cli",
                                   "fault_contract.zero_check", "fault_contract.pytest_plugin", "faultgate_zero"])
def test_the_plugin_loads_in_any_import_order(first):
    r = subprocess.run([sys.executable, "-c", PROBE.format(first=first)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-1500:]


def test_pytest_loading_our_plugin_first_still_has_the_check(pytester, monkeypatch):
    # the order CI hit: fault_contract's pytest plugin is loaded before faultgate-zero's
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    pytester.makepyfile(test_a="""
from faultgate_zero import checks
def test_registered():
    assert "contract" in checks.PLUGINS and checks.PLUGIN_ERRORS == []
""")
    r = pytester.runpytest_subprocess("-p", "fault_contract.pytest_plugin", "-p", "faultgate_zero.pytest_plugin")
    r.assert_outcomes(passed=1)
