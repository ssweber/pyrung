"""Public pyrung facade exports."""

from pyrung import Approach, Harness, Ramp
from pyrung.core import Approach as CoreApproach
from pyrung.core import Harness as CoreHarness
from pyrung.core import Ramp as CoreRamp


def test_harness_and_feedback_specs_are_top_level_exports():
    assert Harness is CoreHarness
    assert Ramp is CoreRamp
    assert Approach is CoreApproach
