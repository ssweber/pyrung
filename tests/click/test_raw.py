"""Tests for CLICK-specific raw instruction DSL."""

from __future__ import annotations

import pytest

from pyrung.click.raw import RawInstruction, raw
from pyrung.core import PLC, Bool, Program, Rung


def test_raw_attaches_instruction_and_is_noop():
    Enable = Bool("Enable")

    with Program() as logic:
        with Rung(Enable):
            raw("Copy", "0x2711,1,6066=Y001,3218=8193,0000=")

    runner = PLC(logic=logic)
    runner.patch({"Enable": True})
    runner.step()

    # raw is a no-op — just verify the runner doesn't crash
    # and the instruction was attached
    rung = logic.rungs[0]
    assert len(rung._instructions) == 1
    instr = rung._instructions[0]
    assert isinstance(instr, RawInstruction)
    assert instr.class_name == "Copy"
    assert instr.fields == "0x2711,1,6066=Y001,3218=8193,0000="


def test_raw_disabled_is_noop():
    Enable = Bool("Enable")

    with Program() as logic:
        with Rung(Enable):
            raw("Cnt", "0x2719,1,6068=CT1,3218=8300,0000=")

    runner = PLC(logic=logic)
    runner.patch({"Enable": False})
    runner.step()  # should not crash


def test_raw_outside_rung_raises():
    with pytest.raises(RuntimeError):
        raw("Copy", "0x2711,1,0000=")


def test_raw_importable_from_pyrung_click():
    from pyrung.click import RawInstruction as RI
    from pyrung.click import raw as raw_fn

    assert RI is RawInstruction
    assert raw_fn is raw
