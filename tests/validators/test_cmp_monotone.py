"""Specification tests for CMP_EQ_ON_MONOTONE.

Equality (``==`` / ``!=``) against a self-advancing register — a timer
accumulator or a counter accumulator — is fragile: the register steps by
``rate_per_scan`` every scan and can jump *over* the compared value between
scans, so the equality may never hold on the exact scan.  The boundary-safe
form is ``>=`` / ``<=`` (or the timer's Done bit).

The reset floor (``== 0`` / ``!= 0``) is exempt: 0 is the resting value the
accumulator never steps over, so ``Acc != 0`` ("has started") is well defined.
"""

from __future__ import annotations

from pyrung.core import (
    Bool,
    Counter,
    Program,
    Rung,
    Timer,
    copy,
    count_down,
    count_up,
    on_delay,
)
from pyrung.core.validation.report import validate


def _timer_eq_program() -> Program:
    tmr = Timer.clone("Tmr")
    out = Bool("Out", external=True)
    with Program(strict=False) as prog:
        with Rung():
            on_delay(tmr, 5, "sec")
        with Rung(tmr.Acc == 5):
            copy(1, out)
    return prog


class TestEqOnTimer:
    def test_equality_reported_as_error(self):
        report = validate(_timer_eq_program())
        eq = [f for f in report if f.code == "CMP_EQ_ON_MONOTONE"]
        assert len(eq) == 1
        assert eq[0].severity == "error"

    def test_message_suggests_ge_and_done_bit(self):
        report = validate(_timer_eq_program())
        eq = next(f for f in report if f.code == "CMP_EQ_ON_MONOTONE")
        assert "Tmr.Acc >= 5" in eq.message
        assert "Tmr.Done" in eq.message


def _reset_floor_program() -> Program:
    tmr = Timer.clone("Tmr")
    out = Bool("Out", external=True)
    with Program(strict=False) as prog:
        with Rung():
            on_delay(tmr, 5, "sec")
        with Rung(tmr.Acc != 0):
            copy(1, out)
    return prog


class TestResetFloorExempt:
    def test_ne_zero_is_not_flagged(self):
        report = validate(_reset_floor_program())
        assert not [f for f in report if f.code == "CMP_EQ_ON_MONOTONE"]


def _count_up_program() -> Program:
    out = Bool("Out", external=True)
    reset = Bool("Rst", external=True)
    counter = Counter.clone("Ctr")
    with Program(strict=False) as prog:
        with Rung():
            count_up(counter, preset=10).reset(reset)
        with Rung(counter.Acc == 10):
            copy(1, out)
    return prog


def _count_down_program() -> Program:
    counter = Counter.clone("Ctr")
    out = Bool("Out", external=True)
    reset = Bool("Rst", external=True)
    with Program(strict=False) as prog:
        with Rung():
            count_down(counter, preset=3).reset(reset)
        with Rung(counter.Acc == 3):
            copy(1, out)
    return prog


class TestEqOnCounter:
    def test_count_up_equality_error_suggests_ge(self):
        report = validate(_count_up_program())
        eq = [f for f in report if f.code == "CMP_EQ_ON_MONOTONE"]
        assert len(eq) == 1
        assert ">=" in eq[0].message

    def test_count_down_equality_suggests_le(self):
        report = validate(_count_down_program())
        eq = next(f for f in report if f.code == "CMP_EQ_ON_MONOTONE")
        assert "<=" in eq.message


def _correct_program() -> Program:
    tmr = Timer.clone("Tmr")
    out = Bool("Out", external=True)
    with Program(strict=False) as prog:
        with Rung():
            on_delay(tmr, 5, "sec")
        with Rung(tmr.Done):
            copy(1, out)
    return prog


class TestNoFalsePositive:
    def test_done_bit_use_is_clean(self):
        report = validate(_correct_program())
        assert not [f for f in report if f.code == "CMP_EQ_ON_MONOTONE"]
