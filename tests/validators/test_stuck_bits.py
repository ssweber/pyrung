"""Tests for stuck-bit validation (latch/reset imbalance detection)."""

from pyrung.core import (
    Block,
    Bool,
    Int,
    Program,
    Rung,
    TagType,
    call,
    copy,
    latch,
    out,
    reset,
    subroutine,
)
from pyrung.core.validation.stuck_bits import (
    COIL_STUCK_HIGH,
    COIL_STUCK_LOW,
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
        assert f.code == COIL_STUCK_HIGH
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
        assert f.code == COIL_STUCK_LOW
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
        assert report.findings[0].code == COIL_STUCK_HIGH
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
        assert report.findings[0].code == COIL_STUCK_HIGH


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
        assert report.findings[0].code == COIL_STUCK_LOW
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
    def test_latch_provides_latch_side(self):
        """latch(C) provides the latch side for a reset-only tag."""
        with Program() as prog:
            with Rung(ButtonA):
                latch(Light)
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

    def test_latch_no_reset_stuck_high(self):
        """latch(C) with no reset → STUCK_HIGH."""
        with Program() as prog:
            with Rung(ButtonA):
                latch(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 1
        assert report.findings[0].code == COIL_STUCK_HIGH
        assert report.findings[0].target_name == "Light"


# ---------------------------------------------------------------------------
# 11. Copy(False, Bool) acts as reset — suppresses stuck-low false positive
# ---------------------------------------------------------------------------


class TestCopyAsBoolReset:
    def test_reset_provides_reset_side(self):
        """reset(C) provides the reset side for a latch-only tag."""
        with Program() as prog:
            with Rung(ButtonA):
                latch(Light)
            with Rung(ButtonB):
                reset(Light)

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

    def test_reset_no_latch_stuck_low(self):
        """reset(C) with no latch → STUCK_LOW."""
        with Program() as prog:
            with Rung(ButtonA):
                reset(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 1
        assert report.findings[0].code == COIL_STUCK_LOW
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


# ---------------------------------------------------------------------------
# 14. Grouping: a range reset that clears a whole block collapses to one group
# ---------------------------------------------------------------------------


class TestGrouping:
    def test_range_reset_groups_to_one(self):
        """A single reset() over a block of bools → one group, N members."""
        c = Block("C", TagType.BOOL, 1, 5)
        with Program() as prog:
            with Rung(ButtonA):
                reset(c.select(1, 5))

        report = validate_stuck_bits(prog)
        # One per-tag finding each, but one group keyed on the shared site.
        assert len(report.findings) == 5
        groups = report.grouped()
        assert len(groups) == 1
        g = groups[0]
        assert g.code == COIL_STUCK_LOW
        assert g.kind == "low"
        assert g.missing_side == "latch"
        assert len(g.findings) == 5
        assert g.common_prefix == "C"
        assert "5 tags can be reset but never latched at the same site" in g.message
        assert "tags: C1, C2, C3, C4, C5" in g.message

    def test_distinct_sites_do_not_group(self):
        """Two unrelated stuck bits keep their own single-member groups."""
        with Program() as prog:
            with Rung(ButtonA):
                latch(Light)  # stuck high
            with Rung(ButtonB):
                reset(Motor)  # stuck low

        report = validate_stuck_bits(prog)
        groups = report.grouped()
        assert len(groups) == 2
        # Single-member groups reuse the original per-tag message.
        for g in groups:
            assert len(g.findings) == 1
            assert g.message == g.findings[0].message

    def test_empty_report_groups_to_nothing(self):
        with Program() as prog:
            with Rung(ButtonA):
                out(Light)

        assert validate_stuck_bits(prog).grouped() == ()
