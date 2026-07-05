"""Specification tests for CMP_TRUE_AT_RESET.

For a monotone-from-zero register, a comparison evaluated at the reset value
(``Acc = 0``) that comes out TRUE is the complement of a completion check: it
fires from the scan the timer starts and turns off at the crossing.  Where the
accumulator resets on state transitions, the inverted comparison manufactures a
spurious pulse on every state entry.

The rule fires only when the comparand matches the configured preset (magnitude
or the preset register) — the botched-completion-check shape — keeping it a
zero-false-positive warning.  ``Acc <= 2`` against a preset of 5 is the
legitimate early-window idiom and stays quiet.
"""

from __future__ import annotations

from pyrung.core import Bool, Int, Program, Rung, Timer, copy, on_delay
from pyrung.core.validation.report import validate


def _literal_preset_program() -> Program:
    """``Acc < 5`` where the timer preset is the literal 5."""
    tmr = Timer.clone("Tmr")
    out = Bool("Out", external=True)
    with Program(strict=False) as prog:
        with Rung():
            on_delay(tmr, 5, "sec")
        with Rung(tmr.Acc < 5):
            copy(1, out)
    return prog


def _tag_preset_program() -> Program:
    """``Setpoint > Acc`` where the timer preset *is* the Setpoint register."""
    setpoint = Int("Setpoint", external=True)
    tmr = Timer.clone("Tmr")
    out = Bool("Out", external=True)
    with Program(strict=False) as prog:
        with Rung():
            on_delay(tmr, setpoint, "sec")
        with Rung(setpoint > tmr.Acc):
            copy(1, out)
    return prog


class TestTrueAtReset:
    def test_literal_preset_reported_as_warning(self):
        report = validate(_literal_preset_program())
        far = [f for f in report if f.code == "CMP_TRUE_AT_RESET"]
        assert len(far) == 1
        assert far[0].severity == "warning"

    def test_message_describes_reset_behavior_and_repair(self):
        report = validate(_literal_preset_program())
        far = next(f for f in report if f.code == "CMP_TRUE_AT_RESET")
        assert "Acc=0" in far.message
        assert "Tmr_Acc >= 5" in far.message

    def test_tag_preset_match_reported(self):
        report = validate(_tag_preset_program())
        far = [f for f in report if f.code == "CMP_TRUE_AT_RESET"]
        assert len(far) == 1
        assert "Setpoint" in far[0].message


def _completion_check_program() -> Program:
    """``Acc >= 5`` — the correct completion form, false at reset."""
    tmr = Timer.clone("Tmr")
    out = Bool("Out", external=True)
    with Program(strict=False) as prog:
        with Rung():
            on_delay(tmr, 5, "sec")
        with Rung(tmr.Acc >= 5):
            copy(1, out)
    return prog


def _early_window_program() -> Program:
    """``Acc <= 2`` against a preset of 5 — the legitimate early-window idiom."""
    tmr = Timer.clone("Tmr")
    out = Bool("Out", external=True)
    with Program(strict=False) as prog:
        with Rung():
            on_delay(tmr, 5, "sec")
        with Rung(tmr.Acc <= 2):
            copy(1, out)
    return prog


class TestNoFalsePositive:
    def test_completion_form_not_flagged(self):
        report = validate(_completion_check_program())
        assert not [f for f in report if f.code == "CMP_TRUE_AT_RESET"]

    def test_early_window_stays_quiet(self):
        report = validate(_early_window_program())
        assert not [f for f in report if f.code == "CMP_TRUE_AT_RESET"]
