"""Public pyrung facade exports."""

from pyrung import Harness, profile
from pyrung.core import Harness as CoreHarness
from pyrung.core import profile as core_profile


def test_harness_and_profile_are_top_level_exports():
    assert Harness is CoreHarness
    assert profile is core_profile
