"""Tests for click hardware profile."""

from __future__ import annotations

from pyrung.click.profile import load_default_profile


def test_default_profile_loads():
    profile = load_default_profile()
    assert profile is not None
    assert profile.valid_for_role("T", "timer_done_bit") is True
    assert profile.valid_for_role("C", "timer_done_bit") is False
