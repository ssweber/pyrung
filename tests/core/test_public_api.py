"""Public pyrung facade exports."""

import importlib

import pytest

from pyrung import Harness, Physical
from pyrung.core import Harness as CoreHarness
from pyrung.core import Physical as CorePhysical


def test_harness_and_physical_are_top_level_exports():
    assert Harness is CoreHarness
    assert Physical is CorePhysical


@pytest.mark.parametrize("module", ["pyrung", "pyrung.core", "pyrung.core.physical"])
@pytest.mark.parametrize("name", ["Pulse", "Ramp", "Approach"])
def test_removed_feedback_classes_are_gone(module, name):
    imported = importlib.import_module(module)
    assert not hasattr(imported, name)
    assert name not in getattr(imported, "__all__", ())
    with pytest.raises(ImportError):
        exec(f"from {module} import {name}", {})
