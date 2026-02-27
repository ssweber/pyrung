"""Tests for click hardware profile."""

from __future__ import annotations

from pyrung.click.profile import load_default_profile


def test_default_profile_loads():
    profile = load_default_profile()
    assert profile is not None
    assert profile.is_writable("SC", 50) is True
    assert profile.is_writable("SC", 1) is False
