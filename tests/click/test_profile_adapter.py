"""Tests for click hardware profile adapter."""

from __future__ import annotations

from pyrung.click.profile import ClickHardwareProfileAdapter, load_default_profile


class _FakeProfile:
    def is_writable(self, memory_type: str, address: int | None = None) -> bool:
        return memory_type == "DS" and address == 1

    def valid_for_role(self, memory_type: str, role: str) -> bool:
        return memory_type == "T" and role == "timer_done_bit"

    def copy_compatible(self, operation: str, source_type: str, dest_type: str) -> bool:
        return operation == "single" and source_type == "DS" and dest_type == "DD"


def test_adapter_delegates_calls():
    adapter = ClickHardwareProfileAdapter(_FakeProfile())
    assert adapter.is_writable("DS", 1) is True
    assert adapter.is_writable("DS", 2) is False
    assert adapter.valid_for_role("T", "timer_done_bit") is True
    assert adapter.valid_for_role("DS", "timer_done_bit") is False
    assert adapter.copy_compatible("single", "DS", "DD") is True
    assert adapter.copy_compatible("block", "DS", "DD") is False


def test_default_profile_loads():
    profile = load_default_profile()
    assert profile is not None
    assert profile.is_writable("SC", 50) is True
    assert profile.is_writable("SC", 1) is False
