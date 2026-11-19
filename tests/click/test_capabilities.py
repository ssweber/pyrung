"""Tests for table-driven hardware capabilities."""

from __future__ import annotations

import pytest

from pyrung.click import ClickBlocks, TagMap
from pyrung.click.capabilities import (
    CLICK_HARDWARE_PROFILE,
    COMPARE_COMPATIBILITY,
    COMPARE_CONSTANT_COMPATIBILITY,
    INSTRUCTION_ROLE_COMPATIBILITY,
    LADDER_WRITABLE_SC,
    LADDER_WRITABLE_SD,
    ClickHardwareProfile,
    CompareConstantKind,
    InstructionRole,
)
from pyrung.click.validation import validate_click_program
from pyrung.core import to_value
from pyrung.core.instruction import Instruction
from pyrung.core.program import (
    Program,
    blockcopy,
    copy,
    fill,
    pack_bits,
    pack_words,
    rung,
    unpack_to_bits,
    unpack_to_words,
)

_BLOCKS = ClickBlocks()


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


def test_sd_control_sc_addresses_remain_writable():
    for address in (65, 66, 67):
        assert address in LADDER_WRITABLE_SC
        assert CLICK_HARDWARE_PROFILE.is_writable("SC", address) is True


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
        ("timer_preset", "DS", "DD"),
        ("counter_done_bit", "CT", "C"),
        ("counter_accumulator", "CTD", "DD"),
        ("counter_preset", "DD", "TD"),
        ("drum_output_bit", "Y", "DS"),
        ("drum_current_step", "DS", "DD"),
        ("drum_completion_flag", "C", "Y"),
        ("drum_accumulator", "TD", "DS"),
        ("drum_jump_step", "DS", "DD"),
        ("drum_event_condition", "X", "DS"),
        ("copy_pointer", "DS", "DD"),
    ],
)
def test_role_compatibility(role: InstructionRole, ok_bank: str, bad_bank: str):
    assert CLICK_HARDWARE_PROFILE.valid_for_role(ok_bank, role) is True
    assert CLICK_HARDWARE_PROFILE.valid_for_role(bad_bank, role) is False


# The bank pairs that pyrung's Copy table held before laddercodec took over the check,
# and the rows of rule-decisions that CLICK's editor settled. Each row is a program and the
# code that its validation must give, or None when CLICK accepts it.
_x, _y, _c, _t, _ct, _sc = _BLOCKS.x, _BLOCKS.y, _BLOCKS.c, _BLOCKS.t, _BLOCKS.ct, _BLOCKS.sc
_ds, _dd, _dh, _df, _yd, _txt = (
    _BLOCKS.ds,
    _BLOCKS.dd,
    _BLOCKS.dh,
    _BLOCKS.df,
    _BLOCKS.yd,
    _BLOCKS.txt,
)


# pyrung refuses these forms when the program is built.  The test builds each one again
# with that check turned off, so CLICK validation still sees it.
REFUSED_WHEN_BUILT = {
    "pack_bits 17 C->DS",
    "pack_bits 32 C->DH",
    "pack_bits 33 C->DD",
    "pack_words DD->DF",
    "unpack_bits DD->DS",
    "unpack_bits DS->32 C",
    "unpack_bits DH->32 C",
    "unpack_bits DD->33 C",
    "unpack_words DS->DH",
}


def _build(write) -> Program:
    prog = Program(strict=False)
    with prog:
        with rung(_x[1]):
            write()
    return prog


@pytest.mark.parametrize(
    ("case", "write", "expected"),
    [
        ("single X->Y", lambda: copy(_x[1], _y[1]), None),
        ("single DS->TXT", lambda: copy(_ds[1], _txt[1]), None),
        ("single DS->C", lambda: copy(_ds[1], _c[1]), "copy-bank-incompatible"),
        (
            "block TXT->DS to_value",
            lambda: blockcopy(_txt.select(1, 2), _ds.select(1, 2), convert=to_value),
            None,
        ),
        # rule-decisions N1: CLICK saves a Block copy from TXT into DS only with a converter.
        # The export writes the to_value that pyrung runs.
        ("block TXT->DS plain", lambda: blockcopy(_txt.select(1, 2), _ds.select(1, 2)), None),
        ("block TXT->TXT", lambda: blockcopy(_txt.select(1, 2), _txt.select(3, 4)), None),
        # CLICK refuses a Block copy of TXT into DF (OBJ-024).
        (
            "block TXT->DF",
            lambda: blockcopy(_txt.select(1, 3), _df.select(1, 3)),
            "copy-bank-incompatible",
        ),
        ("fill TXT->TXT", lambda: fill(_txt[1], _txt.select(2, 3)), None),
        # rule-decisions B3: CLICK packs X and Y into DH and not into DS (OBJ-024).
        ("pack_bits X->DS", lambda: pack_bits(_x.select(1, 16), _ds[1]), "copy-bank-incompatible"),
        ("pack_bits Y->DS", lambda: pack_bits(_y.select(1, 16), _ds[1]), "copy-bank-incompatible"),
        ("pack_bits X->DH", lambda: pack_bits(_x.select(1, 16), _dh[1]), None),
        ("pack_bits C->DF", lambda: pack_bits(_c.select(1, 32), _df[1]), None),
        ("pack_bits X->DD", lambda: pack_bits(_x.select(1, 16), _dd[1]), "copy-bank-incompatible"),
        # rule-decisions N5: CLICK packs T, CT and SC only into DH, at 16 bits (OBJ-024).
        ("pack_bits T->DS", lambda: pack_bits(_t.select(1, 16), _ds[1]), "copy-bank-incompatible"),
        (
            "pack_bits CT->DD",
            lambda: pack_bits(_ct.select(1, 16), _dd[1]),
            "copy-bank-incompatible",
        ),
        (
            "pack_bits SC->DF",
            lambda: pack_bits(_sc.select(1, 16), _df[1]),
            "copy-bank-incompatible",
        ),
        ("pack_bits T->DH", lambda: pack_bits(_t.select(1, 16), _dh[1]), None),
        # rule-decisions N4: CLICK packs at most 16 bits into DS or DH (OBJ-027), and at most
        # 32 into DD or DF (OBJ-041).
        ("pack_bits 17 C->DS", lambda: pack_bits(_c.select(1, 17), _ds[1]), "pack-too-many"),
        ("pack_bits 32 C->DH", lambda: pack_bits(_c.select(1, 32), _dh[1]), "pack-too-many"),
        ("pack_bits 32 C->DD", lambda: pack_bits(_c.select(1, 32), _dd[1]), None),
        ("pack_bits 33 C->DD", lambda: pack_bits(_c.select(1, 33), _dd[1]), "pack-too-many"),
        ("pack_words DS->DD", lambda: pack_words(_ds.select(1, 2), _dd[1]), None),
        ("pack_words DD->DF", lambda: pack_words(_dd.select(1, 2), _df[1]), "bank-not-allowed"),
        # rule-decisions B5: CLICK unpacks DH into Y, and no DS, DD or DF (OBJ-024).
        (
            "unpack_bits DD->Y",
            lambda: unpack_to_bits(_dd[1], _y.select(1, 16)),
            "copy-bank-incompatible",
        ),
        ("unpack_bits DH->Y", lambda: unpack_to_bits(_dh[1], _y.select(1, 16)), None),
        (
            "unpack_bits DD->DS",
            lambda: unpack_to_bits(_dd[1], _ds.select(1, 2)),
            "pack-kind-mismatch",
        ),
        # rule-decisions N4: CLICK unpacks DS or DH into no more than 16 bits (OBJ-032), and DD
        # into no more than 32 (OBJ-039).
        ("unpack_bits DS->32 C", lambda: unpack_to_bits(_ds[1], _c.select(1, 32)), "pack-too-many"),
        ("unpack_bits DH->32 C", lambda: unpack_to_bits(_dh[1], _c.select(1, 32)), "pack-too-many"),
        ("unpack_bits DD->32 C", lambda: unpack_to_bits(_dd[1], _c.select(1, 32)), None),
        ("unpack_bits DD->33 C", lambda: unpack_to_bits(_dd[1], _c.select(1, 33)), "pack-too-many"),
        ("unpack_words DF->DH", lambda: unpack_to_words(_df[1], _dh.select(1, 2)), None),
        (
            "unpack_words DS->DH",
            lambda: unpack_to_words(_ds[1], _dh.select(1, 2)),
            "copy-bank-incompatible",
        ),
        # rule-decisions N6: CLICK fills every range from YD0..YD1 to YD1..YD8.
        ("fill YD0..YD1", lambda: fill(5, _yd.select(0, 1)), None),
        ("fill YD1..YD8", lambda: fill(5, _yd.select(1, 8)), None),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_copy_compatibility(
    case: str, write, expected: str | None, monkeypatch: pytest.MonkeyPatch
):
    if case in REFUSED_WHEN_BUILT:
        with pytest.raises((TypeError, ValueError), match=r"got "):
            _build(write)
        monkeypatch.setattr(Instruction, "check_values", classmethod(lambda *_: None))
    prog = _build(write)
    report = validate_click_program(prog, TagMap(), mode="strict")
    codes = [f.code for f in report.errors]
    assert codes == ([] if expected is None else [expected])


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
    assert "timer_done_bit" in INSTRUCTION_ROLE_COMPATIBILITY
    assert "drum_output_bit" in INSTRUCTION_ROLE_COMPATIBILITY
    assert ("DS", "DD") in COMPARE_COMPATIBILITY
    assert "DS" in COMPARE_CONSTANT_COMPATIBILITY


@pytest.mark.parametrize(
    "call",
    [
        lambda: CLICK_HARDWARE_PROFILE.is_writable("ZZ", 1),
        lambda: CLICK_HARDWARE_PROFILE.valid_for_role("ZZ", "timer_done_bit"),
        lambda: CLICK_HARDWARE_PROFILE.valid_for_role("T", "bad_role"),
        lambda: CLICK_HARDWARE_PROFILE.compare_compatible("ZZ", "DS"),
        lambda: CLICK_HARDWARE_PROFILE.compare_constant_compatible("ZZ", "int1"),
        lambda: CLICK_HARDWARE_PROFILE.compare_constant_compatible("DS", "bad_kind"),
    ],
)
def test_unknown_inputs_raise_key_error(call):
    with pytest.raises(KeyError):
        call()
