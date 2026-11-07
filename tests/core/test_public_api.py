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
@pytest.mark.parametrize(
    ("name", "keyword"), [("Pulse", "flasher_on"), ("Ramp", "ramp_on"), ("Approach", "approach_to")]
)
def test_removed_feedback_classes_name_the_new_keywords(module, name, keyword):
    with pytest.raises(ImportError, match=keyword):
        exec(f"from {module} import {name}", {})


@pytest.mark.parametrize("module", ["pyrung", "pyrung.core", "pyrung.core.physical"])
def test_removed_feedback_classes_are_missing_attributes(module):
    imported = importlib.import_module(module)
    assert not hasattr(imported, "Pulse")
    assert getattr(imported, "Ramp", None) is None
    with pytest.raises(AttributeError, match="approach_to"):
        imported.Approach  # noqa: B018
