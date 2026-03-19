"""Tests for CLICK-specific raw instruction DSL."""

from __future__ import annotations

import pytest

from pyrung.click.raw import RawInstruction, raw
from pyrung.core import Bool, PLCRunner, Program, Rung


def test_raw_attaches_instruction_and_is_noop():
    Enable = Bool("Enable")

    with Program() as logic:
        with Rung(Enable):
            raw("Copy", blob=bytes.fromhex("0a1b2c3d"))

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True})
    runner.step()

    # raw is a no-op — just verify the runner doesn't crash
    # and the instruction was attached
    rung = logic.rungs[0]
    assert len(rung._instructions) == 1
    instr = rung._instructions[0]
    assert isinstance(instr, RawInstruction)
    assert instr.class_name == "Copy"
    assert instr.blob == bytes.fromhex("0a1b2c3d")


def test_raw_disabled_is_noop():
    Enable = Bool("Enable")

    with Program() as logic:
        with Rung(Enable):
            raw("Cnt", blob=b"\x00\x01\x02")

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": False})
    runner.step()  # should not crash


def test_raw_outside_rung_raises():
    with pytest.raises(RuntimeError):
        raw("Copy", blob=b"\x00")


def test_raw_importable_from_pyrung_click():
    from pyrung.click import RawInstruction as RI
    from pyrung.click import raw as raw_fn

    assert RI is RawInstruction
    assert raw_fn is raw
