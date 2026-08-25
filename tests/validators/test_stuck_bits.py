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
    return_early,
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
        assert "never reset" in f.message


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
        assert "never latched" in f.message


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
        assert "5 coils are reset here, latched nowhere" in g.message
        assert "C1, C2, C3, C4, C5" in g.message

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


# ---------------------------------------------------------------------------
# 15. out() in a skippable subroutine is retentive — it latches, it never resets
# ---------------------------------------------------------------------------


class TestRetentiveOut:
    def test_out_in_conditionally_called_sub_is_stuck_high(self):
        """The sub stops being called with the coil high → it stays high."""
        with Program() as prog:
            with Rung(ButtonA):
                call("run")
            with subroutine("run"):
                with Rung(ButtonB):
                    out(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 1
        f = report.findings[0]
        assert f.code == COIL_STUCK_HIGH
        assert f.target_name == "Light"
        assert f.missing_side == "reset"
        assert "held high when skipped" in f.message
        assert "out() in run holds its last value" in f.message

    def test_reset_elsewhere_clears_the_finding(self):
        """A reset() outside the subroutine supplies the missing side."""
        with Program() as prog:
            with Rung(ButtonA):
                call("run")
            with Rung(~ButtonA):
                reset(Light)
            with subroutine("run"):
                with Rung(ButtonB):
                    out(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0

    def test_unconditionally_called_sub_is_not_retentive(self):
        """A sub called every scan runs its out() every scan — normal OTE."""
        with Program() as prog:
            with Rung():
                call("run")
            with subroutine("run"):
                with Rung(ButtonB):
                    out(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0

    def test_unconditional_call_chain_is_not_retentive(self):
        """main → outer → inner, both calls unconditional → still an OTE."""
        with Program() as prog:
            with Rung():
                call("outer")
            with subroutine("outer"):
                with Rung():
                    call("inner")
            with subroutine("inner"):
                with Rung(ButtonB):
                    out(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0

    def test_conditional_call_chain_is_retentive(self):
        """One conditional link in the chain is enough to freeze the coil."""
        with Program() as prog:
            with Rung(ButtonA):
                call("outer")
            with subroutine("outer"):
                with Rung():
                    call("inner")
            with subroutine("inner"):
                with Rung(ButtonB):
                    out(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 1
        assert report.findings[0].code == COIL_STUCK_HIGH
        assert report.findings[0].target_name == "Light"

    def test_early_return_above_the_out_is_retentive(self):
        """The sub runs every scan, but return_early() can skip the out()."""
        with Program() as prog:
            with Rung():
                call("run")
            with subroutine("run"):
                with Rung(ButtonA):
                    return_early()
                with Rung(ButtonB):
                    out(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 1
        assert report.findings[0].code == COIL_STUCK_HIGH
        assert report.findings[0].target_name == "Light"

    def test_out_above_an_early_return_is_not_retentive(self):
        """A return below the out() never skips it."""
        with Program() as prog:
            with Rung():
                call("run")
            with subroutine("run"):
                with Rung(ButtonB):
                    out(Light)
                with Rung(ButtonA):
                    return_early()

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0

    def test_retentive_out_supplies_the_latch_side(self):
        """reset() + a retentive out() is a balanced pair — no STUCK_LOW."""
        with Program() as prog:
            with Rung(ButtonA):
                call("run")
            with Rung(~ButtonA):
                reset(Light)
            with subroutine("run"):
                with Rung(ButtonB):
                    out(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0

    def test_main_out_alone_supplies_both_sides(self):
        """An out() in main clears its own coil — never stuck, either way."""
        with Program() as prog:
            with Rung(ButtonA):
                latch(Light)
            with Rung(ButtonB):
                out(Motor)

        report = validate_stuck_bits(prog)
        assert [f.target_name for f in report.findings] == ["Light"]


# ---------------------------------------------------------------------------
# 16. out() sites that cover the state space between them are self-clearing
# ---------------------------------------------------------------------------

Mode = Int("Mode", choices={1: "run", 2: "hold", 3: "stop"})
Step = Int("Step", min=0, max=2)


class TestOutCoverage:
    def test_covering_states_no_finding(self):
        """Every state of Mode drives the coil → one out() runs every scan."""
        with Program() as prog:
            with Rung(Mode == 1):
                call("run")
            with Rung(Mode == 2):
                call("hold")
            with Rung(Mode == 3):
                call("stop")
            with subroutine("run"):
                with Rung(ButtonA):
                    out(Light)
            with subroutine("hold"):
                with Rung(ButtonB):
                    out(Light)
            with subroutine("stop"):
                with Rung():
                    out(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0

    def test_missing_state_is_stuck_high(self):
        """Mode==3 drives nothing, so the coil holds through that state."""
        with Program() as prog:
            with Rung(Mode == 1):
                call("run")
            with Rung(Mode == 2):
                call("hold")
            with subroutine("run"):
                with Rung(ButtonA):
                    out(Light)
            with subroutine("hold"):
                with Rung(ButtonB):
                    out(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 1
        assert report.findings[0].code == COIL_STUCK_HIGH
        assert report.findings[0].target_name == "Light"

    def test_min_max_domain_covers(self):
        """A min/max range closes the domain just as choices= does."""
        with Program() as prog:
            with Rung(Step == 0):
                call("s0")
            with Rung(Step >= 1):
                call("s12")
            with subroutine("s0"):
                with Rung(ButtonA):
                    out(Light)
            with subroutine("s12"):
                with Rung(ButtonB):
                    out(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0

    def test_bool_complement_covers_without_declarations(self):
        """A Bool's domain is closed by construction — no choices= needed."""
        with Program() as prog:
            with Rung(ButtonA):
                call("on")
            with Rung(~ButtonA):
                call("off")
            with subroutine("on"):
                with Rung(ButtonB):
                    out(Light)
            with subroutine("off"):
                with Rung():
                    out(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0

    def test_undeclared_state_tag_still_warns(self):
        """State has no declared domain — State==7 is not ruled out."""
        with Program() as prog:
            with Rung(State == 1):
                call("run")
            with Rung(State == 2):
                call("hold")
            with subroutine("run"):
                with Rung(ButtonA):
                    out(Light)
            with subroutine("hold"):
                with Rung(ButtonB):
                    out(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 1
        f = report.findings[0]
        assert f.code == COIL_STUCK_HIGH
        assert "declare the domain of State" in f.message

    def test_no_domain_hint_when_a_declaration_would_not_help(self):
        """Mode 2 and 3 drive nothing — declaring Gate's domain would not close that."""
        Gate = Int("Gate")  # undeclared domain, but not the reason coverage fails

        with Program() as prog:
            with Rung(Mode == 1, Gate != 1):
                call("run")
            with subroutine("run"):
                with Rung(ButtonA):
                    out(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 1
        assert report.findings[0].code == COIL_STUCK_HIGH
        assert "declare the domain" not in report.findings[0].message

    def test_rung_condition_is_not_a_gap(self):
        """A false rung still ran — out() drove the coil low. Only scope matters."""
        with Program() as prog:
            with Rung():
                call("run")
            with subroutine("run"):
                with Rung(ButtonA, ButtonB, State == 5):
                    out(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0

    def test_coverage_through_a_call_chain(self):
        """Reach chains compose: main → dispatch(Mode==N) → per-state subs."""
        with Program() as prog:
            with Rung():
                call("dispatch")
            with subroutine("dispatch"):
                with Rung(Mode == 1):
                    call("run")
                with Rung(Mode != 1):
                    call("idle")
            with subroutine("run"):
                with Rung(ButtonA):
                    out(Light)
            with subroutine("idle"):
                with Rung():
                    out(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0
