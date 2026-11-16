"""The fault_coverage example runs, and every coupling trips its alarm."""

from __future__ import annotations

import importlib
import sys

import pytest

pytestmark = pytest.mark.integration


def test_fault_coverage_example_runs():
    module_name = "examples.fault_coverage"
    if module_name in sys.modules:
        del sys.modules[module_name]
    mod = importlib.import_module(module_name)
    assert sorted(mod.tripped_couplings) == ["Fb", "PumpFb"]
