"""Tests for stuck-bit validation (latch/reset imbalance detection)."""

from pyrung.core import (
    Bool,
    Int,
    Program,
    Rung,
    call,
    copy,
    latch,
    out,
    reset,
    subroutine,
)
from pyrung.core.validation.stuck_bits import (
    CORE_STUCK_HIGH,
    CORE_STUCK_LOW,
    validate_stuck_bits,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

Light = Bool("Light")
Motor = Bool("Motor")
Valve = Bool("Valve")
ButtonA = Bool("ButtonA")
ButtonB = Bool("ButtonB")
Flag = Bool("Flag")
State = Int("State")


# ---------------------------------------------------------------------------
# 1. Latch with matching reset → no finding
# ---------------------------------------------------------------------------


class TestMatchingPair:
    def test_latch_and_reset_no_finding(self):
        with Program() as prog:
            with Rung(ButtonA):
                latch(Light)
            with Rung(ButtonB):
                reset(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0
        assert report.summary() == "No stuck bits."


# ---------------------------------------------------------------------------
# 2. Latch with no reset anywhere → STUCK_HIGH
# ---------------------------------------------------------------------------


class TestStuckHigh:
    def test_latch_no_reset(self):
        with Program() as prog:
            with Rung(ButtonA):
                latch(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 1
        f = report.findings[0]
        assert f.code == CORE_STUCK_HIGH
        assert f.target_name == "Light"
        assert f.kind == "high"
        assert f.missing_side == "reset"
        assert "can be latched but never reset" in f.message


# ---------------------------------------------------------------------------
# 3. Reset with no latch anywhere → STUCK_LOW
# ---------------------------------------------------------------------------


class TestStuckLow:
    def test_reset_no_latch(self):
        with Program() as prog:
            with Rung(ButtonA):
                reset(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 1
        f = report.findings[0]
        assert f.code == CORE_STUCK_LOW
        assert f.target_name == "Light"
        assert f.kind == "low"
        assert f.missing_side == "latch"
        assert "can be reset but never latched" in f.message


# ---------------------------------------------------------------------------
# 4. Latch in main, reset in uncalled subroutine → STUCK_HIGH
# ---------------------------------------------------------------------------


class TestUncalledSubroutine:
    def test_reset_in_uncalled_sub(self):
        with Program() as prog:
            with Rung(ButtonA):
                latch(Light)
            with subroutine("unused"):
                with Rung():
                    reset(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 1
        assert report.findings[0].code == CORE_STUCK_HIGH
        assert report.findings[0].target_name == "Light"


# ---------------------------------------------------------------------------
# 5. Latch in main, reset in subroutine called under contradicting
#    conditions → STUCK_HIGH
# ---------------------------------------------------------------------------


class TestContradictingCallerConditions:
    def test_contradicting_caller(self):
        """Reset's caller has State==1 AND State==2 — impossible."""
        with Program() as prog:
            with Rung(ButtonA):
                latch(Light)
            with Rung(State == 1, State == 2):
                call("do_reset")
            with subroutine("do_reset"):
                with Rung():
                    reset(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 1
        assert report.findings[0].code == CORE_STUCK_HIGH


# ---------------------------------------------------------------------------
# 6. Latch in main, reset in subroutine called under real conditions →
#    no finding (the pause case; must not false-positive)
# ---------------------------------------------------------------------------


class TestSubroutineGatedPause:
    def test_real_caller_no_finding(self):
        with Program() as prog:
            with Rung(ButtonA):
                latch(Light)
            with Rung(ButtonB):
                call("do_reset")
            with subroutine("do_reset"):
                with Rung():
                    reset(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0

    def test_state_gated_no_finding(self):
        with Program() as prog:
            with Rung(State == 1):
                latch(Light)
            with Rung(State == 2):
                call("cleanup")
            with subroutine("cleanup"):
                with Rung():
                    reset(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0


# ---------------------------------------------------------------------------
# 7. Latch and reset both present with non-contradicting conditions →
#    no finding
# ---------------------------------------------------------------------------


class TestNonContradictingConditions:
    def test_different_buttons(self):
        with Program() as prog:
            with Rung(ButtonA):
                latch(Light)
            with Rung(ButtonB):
                reset(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0

    def test_unconditional_both(self):
        with Program() as prog:
            with Rung():
                latch(Light)
            with Rung():
                reset(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0


# ---------------------------------------------------------------------------
# 8. Latch with contradicting rung conditions (unreachable) and normal
#    reset → STUCK_LOW
# ---------------------------------------------------------------------------


class TestUnreachableLatch:
    def test_contradicting_latch_conditions(self):
        """Latch has State==1 AND State==2 on the same rung — impossible."""
        with Program() as prog:
            with Rung(State == 1, State == 2):
                latch(Light)
            with Rung(ButtonA):
                reset(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 1
        assert report.findings[0].code == CORE_STUCK_LOW
        assert report.findings[0].target_name == "Light"


# ---------------------------------------------------------------------------
# 9. Empty program / program with no latch or reset → no findings
# ---------------------------------------------------------------------------


class TestEmptyPrograms:
    def test_empty_program(self):
        with Program() as prog:
            pass

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0
        assert report.summary() == "No stuck bits."

    def test_program_with_out_only(self):
        """out() is not latch/reset — should produce no findings."""
        with Program() as prog:
            with Rung(ButtonA):
                out(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0

    def test_summary_with_findings(self):
        with Program() as prog:
            with Rung(ButtonA):
                latch(Light)
            with Rung(ButtonB):
                reset(Motor)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 2
        assert "2 stuck bit(s)" in report.summary()


# ---------------------------------------------------------------------------
# 10. Copy(True, Bool) acts as latch — suppresses stuck-high false positive
# ---------------------------------------------------------------------------


class TestCopyAsBoolLatch:
    def test_copy_true_counts_as_latch(self):
        """Copy(True, C) provides the latch side for a reset-only tag."""
        with Program() as prog:
            with Rung(ButtonA):
                copy(True, Light)
            with Rung(ButtonB):
                reset(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0

    def test_copy_1_counts_as_latch(self):
        """Copy(1, C) also acts as a latch for a Bool destination."""
        with Program() as prog:
            with Rung(ButtonA):
                copy(1, Light)
            with Rung(ButtonB):
                reset(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0

    def test_copy_true_no_reset_stuck_high(self):
        """Copy(True, C) with no reset → STUCK_HIGH."""
        with Program() as prog:
            with Rung(ButtonA):
                copy(True, Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 1
        assert report.findings[0].code == CORE_STUCK_HIGH
        assert report.findings[0].target_name == "Light"


# ---------------------------------------------------------------------------
# 11. Copy(False, Bool) acts as reset — suppresses stuck-low false positive
# ---------------------------------------------------------------------------


class TestCopyAsBoolReset:
    def test_copy_false_counts_as_reset(self):
        """Copy(False, C) provides the reset side for a latch-only tag."""
        with Program() as prog:
            with Rung(ButtonA):
                latch(Light)
            with Rung(ButtonB):
                copy(False, Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0

    def test_copy_0_counts_as_reset(self):
        """Copy(0, C) also acts as a reset for a Bool destination."""
        with Program() as prog:
            with Rung(ButtonA):
                latch(Light)
            with Rung(ButtonB):
                copy(0, Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0

    def test_copy_false_no_latch_stuck_low(self):
        """Copy(False, C) with no latch → STUCK_LOW."""
        with Program() as prog:
            with Rung(ButtonA):
                copy(False, Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 1
        assert report.findings[0].code == CORE_STUCK_LOW
        assert report.findings[0].target_name == "Light"


# ---------------------------------------------------------------------------
# 12. Copy(Tag, Bool) counts as both latch and reset (conservative)
# ---------------------------------------------------------------------------


class TestCopyTagSourceBoth:
    def test_copy_tag_source_covers_both_sides(self):
        """Copy(tag, C) is conservatively both latch and reset — no finding."""
        with Program() as prog:
            with Rung(ButtonA):
                copy(ButtonB, Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0

    def test_copy_tag_suppresses_latch_only(self):
        """Copy(tag, C) + reset → no finding (Copy covers latch side)."""
        with Program() as prog:
            with Rung(ButtonA):
                copy(ButtonB, Light)
            with Rung(ButtonB):
                reset(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0

    def test_copy_tag_suppresses_reset_only(self):
        """Copy(tag, C) + latch → no finding (Copy covers reset side)."""
        with Program() as prog:
            with Rung(ButtonA):
                latch(Light)
            with Rung(ButtonB):
                copy(ButtonB, Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0


# ---------------------------------------------------------------------------
# 13. Copy to non-Bool destination is ignored by stuck-bit analysis
# ---------------------------------------------------------------------------


class TestCopyNonBoolIgnored:
    def test_copy_to_int_not_analyzed(self):
        """Copy to an Int tag should not produce stuck-bit findings."""
        with Program() as prog:
            with Rung(ButtonA):
                copy(42, State)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0
