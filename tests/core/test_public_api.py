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


@pytest.mark.parametrize(
    "module", ["pyrung", "pyrung.click", "pyrung.circuitpy", "pyrung.validation", "pyrung.live"]
)
def test_every_public_name_resolves(module):
    imported = importlib.import_module(module)
    for name in imported.__all__:
        assert hasattr(imported, name), f"{module}.{name}"


@pytest.mark.parametrize("name", ["validate", "register_dialect", "registered_dialects"])
def test_program_has_no_dialect_validation(name):
    from pyrung import Program

    assert not hasattr(Program, name)


def test_click_does_not_export_validate_click_program():
    import pyrung.click

    assert "validate_click_program" not in pyrung.click.__all__
    assert not hasattr(pyrung.click, "validate_click_program")
