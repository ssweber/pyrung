"""Tests for table-driven hardware capabilities."""

from __future__ import annotations

import pytest

from pyrung.click.capabilities import (
    CLICK_HARDWARE_PROFILE,
    COMPARE_COMPATIBILITY,
    COMPARE_CONSTANT_COMPATIBILITY,
    COPY_COMPATIBILITY,
    INSTRUCTION_ROLE_COMPATIBILITY,
    LADDER_WRITABLE_SC,
    LADDER_WRITABLE_SD,
    ClickHardwareProfile,
    CompareConstantKind,
    CopyOperation,
    InstructionRole,
)


def test_profile_export_available():
    assert isinstance(CLICK_HARDWARE_PROFILE, ClickHardwareProfile)


@pytest.mark.parametrize(
    ("memory_type", "address", "expected"),
    [
        ("X", 1, False),
        ("Y", 1, True),
        ("C", 1, True),
        ("T", 1, False),
        ("CT", 1, False),
        ("DS", 1, True),
        ("DD", 1, True),
        ("DH", 1, True),
        ("DF", 1, True),
        ("XD", 1, False),
        ("YD", 1, False),
        ("TD", 1, True),
        ("CTD", 1, True),
        ("TXT", 1, True),
    ],
)
def test_is_writable_baseline(memory_type: str, address: int, expected: bool):
    assert CLICK_HARDWARE_PROFILE.is_writable(memory_type, address) is expected


def test_is_writable_sc_subset():
    for address in LADDER_WRITABLE_SC:
        assert CLICK_HARDWARE_PROFILE.is_writable("SC", address) is True
    assert CLICK_HARDWARE_PROFILE.is_writable("SC", 1) is False
    assert CLICK_HARDWARE_PROFILE.is_writable("SC", None) is False


def test_is_writable_sd_subset():
    for address in LADDER_WRITABLE_SD:
        assert CLICK_HARDWARE_PROFILE.is_writable("SD", address) is True
    assert CLICK_HARDWARE_PROFILE.is_writable("SD", 1) is False
    assert CLICK_HARDWARE_PROFILE.is_writable("SD", None) is False


@pytest.mark.parametrize(
    ("role", "ok_bank", "bad_bank"),
    [
        ("timer_done_bit", "T", "C"),
        ("timer_accumulator", "TD", "DS"),
        ("timer_setpoint", "DS", "DD"),
        ("counter_done_bit", "CT", "C"),
        ("counter_accumulator", "CTD", "DD"),
        ("counter_setpoint", "DD", "TD"),
        ("copy_pointer", "DS", "DD"),
    ],
)
def test_role_compatibility(role: InstructionRole, ok_bank: str, bad_bank: str):
    assert CLICK_HARDWARE_PROFILE.valid_for_role(ok_bank, role) is True
    assert CLICK_HARDWARE_PROFILE.valid_for_role(bad_bank, role) is False


@pytest.mark.parametrize(
    ("operation", "source", "dest", "expected"),
    [
        ("single", "X", "Y", True),
        ("single", "DS", "TXT", True),
        ("single", "DS", "C", False),
        ("block", "TXT", "DS", True),
        ("block", "TXT", "TXT", False),
        ("fill", "TXT", "TXT", True),
        ("fill", "TXT", "DS", False),
        ("pack_bits", "X", "DS", True),
        ("pack_bits", "C", "DF", True),
        ("pack_bits", "X", "DD", False),
        ("pack_words", "DS", "DD", True),
        ("pack_words", "DD", "DF", False),
        ("unpack_bits", "DD", "Y", True),
        ("unpack_bits", "DD", "DS", False),
        ("unpack_words", "DF", "DH", True),
        ("unpack_words", "DS", "DH", False),
    ],
)
def test_copy_compatibility(operation: CopyOperation, source: str, dest: str, expected: bool):
    assert CLICK_HARDWARE_PROFILE.copy_compatible(operation, source, dest) is expected


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("DS", "DD", True),
        ("DH", "XD", True),
        ("TXT", "TXT", True),
        ("TXT", "DD", False),
        ("DH", "DD", False),
    ],
)
def test_compare_compatibility(left: str, right: str, expected: bool):
    assert CLICK_HARDWARE_PROFILE.compare_compatible(left, right) is expected


@pytest.mark.parametrize(
    ("bank", "const_kind", "expected"),
    [
        ("DS", "int1", True),
        ("DS", "float", True),
        ("DH", "hex", True),
        ("TXT", "text", True),
        ("DH", "int1", False),
        ("TXT", "hex", False),
    ],
)
def test_compare_constant_compatibility(bank: str, const_kind: CompareConstantKind, expected: bool):
    assert CLICK_HARDWARE_PROFILE.compare_constant_compatible(bank, const_kind) is expected


def test_lookup_tables_exported():
    assert "single" in COPY_COMPATIBILITY
    assert "timer_done_bit" in INSTRUCTION_ROLE_COMPATIBILITY
    assert ("DS", "DD") in COMPARE_COMPATIBILITY
    assert "DS" in COMPARE_CONSTANT_COMPATIBILITY


@pytest.mark.parametrize(
    "call",
    [
        lambda: CLICK_HARDWARE_PROFILE.is_writable("ZZ", 1),
        lambda: CLICK_HARDWARE_PROFILE.valid_for_role("ZZ", "timer_done_bit"),
        lambda: CLICK_HARDWARE_PROFILE.valid_for_role("T", "bad_role"),  # type: ignore[arg-type]
        lambda: CLICK_HARDWARE_PROFILE.copy_compatible("bad_op", "DS", "DD"),  # type: ignore[arg-type]
        lambda: CLICK_HARDWARE_PROFILE.copy_compatible("single", "ZZ", "DD"),
        lambda: CLICK_HARDWARE_PROFILE.copy_compatible("single", "DS", "ZZ"),
        lambda: CLICK_HARDWARE_PROFILE.compare_compatible("ZZ", "DS"),
        lambda: CLICK_HARDWARE_PROFILE.compare_constant_compatible("ZZ", "int1"),
        lambda: CLICK_HARDWARE_PROFILE.compare_constant_compatible("DS", "bad_kind"),  # type: ignore[arg-type]
    ],
)
def test_unknown_inputs_raise_key_error(call):
    with pytest.raises(KeyError):
        call()
