"""Tests for conflicting output target validation.

Covers all INERT_WHEN_DISABLED=False instruction types with mutual-exclusivity
detection for out(), timers, counters, drums, and shift registers.
"""

from pyrung.core import (
    Block,
    Bool,
    Counter,
    Int,
    OutputBlock,
    Program,
    Rung,
    TagType,
    Timer,
    branch,
    call,
    count_down,
    count_up,
    event_drum,
    latch,
    off_delay,
    on_delay,
    out,
    reset,
    shift,
    subroutine,
)
from pyrung.core.validation.duplicate_out import (
    CORE_CONFLICTING_OUTPUT,
    validate_conflicting_outputs,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

Light = Bool("Light")
Motor = Bool("Motor")
Valve = Bool("Valve")
ButtonA = Bool("ButtonA")
ButtonB = Bool("ButtonB")
ButtonC = Bool("ButtonC")
Flag = Bool("Flag")
State = Int("State")
ResetBtn = Bool("ResetBtn")


# ---------------------------------------------------------------------------
# 1. Two rungs, same out target, non-exclusive conditions
# ---------------------------------------------------------------------------


class TestDirectDuplicateOut:
    def test_non_exclusive_conditions(self):
        with Program() as prog:
            with Rung(ButtonA):
                out(Light)
            with Rung(ButtonB):
                out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 1
        assert report.findings[0].target_name == "Light"
        assert report.findings[0].code == CORE_CONFLICTING_OUTPUT
        assert len(report.findings[0].sites) == 2


# ---------------------------------------------------------------------------
# 2. Two rungs in same subroutine, same out target
# ---------------------------------------------------------------------------


class TestSubroutineDuplicateOut:
    def test_same_subroutine(self):
        with Program() as prog:
            with Rung():
                call("init")
            with subroutine("init"):
                with Rung(ButtonA):
                    out(Light)
                with Rung(ButtonB):
                    out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 1
        assert report.findings[0].target_name == "Light"


# ---------------------------------------------------------------------------
# 3. Outs in different subroutines, callers mutually exclusive
# ---------------------------------------------------------------------------


class TestMutuallyExclusiveSubroutines:
    def test_state_machine_subroutines(self):
        with Program() as prog:
            with Rung(State == 1):
                call("sub_idle")
            with Rung(State == 2):
                call("sub_run")
            with subroutine("sub_idle"):
                with Rung():
                    out(Light)
            with subroutine("sub_run"):
                with Rung():
                    out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 0


# ---------------------------------------------------------------------------
# 4. Single subroutine one out, called from multiple places
# ---------------------------------------------------------------------------


class TestSingleSubroutineMultipleCallers:
    def test_no_conflict(self):
        with Program() as prog:
            with Rung(ButtonA):
                call("handler")
            with Rung(ButtonB):
                call("handler")
            with subroutine("handler"):
                with Rung():
                    out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 0


# ---------------------------------------------------------------------------
# 5. Direct outs with mutually exclusive conditions — still a conflict
#    because out() always executes (sets or resets) in same scope
# ---------------------------------------------------------------------------


class TestSameScopeAlwaysConflicts:
    def test_compare_eq_different_constants(self):
        """State==1 vs State==2 in main: both outs execute, last one stomps."""
        with Program() as prog:
            with Rung(State == 1):
                out(Light)
            with Rung(State == 2):
                out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 1

    def test_complementary_bit_conditions(self):
        """Flag vs ~Flag in main: both outs execute, last one stomps."""
        with Program() as prog:
            with Rung(Flag):
                out(Light)
            with Rung(~Flag):
                out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 1


# ---------------------------------------------------------------------------
# 7a. Branches of same rung, non-exclusive conditions
# ---------------------------------------------------------------------------


class TestBranchNonExclusive:
    def test_different_conditions_not_exclusive(self):
        with Program() as prog:
            with Rung():
                with branch(ButtonA):
                    out(Light)
                with branch(ButtonB):
                    out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 1


# ---------------------------------------------------------------------------
# 7b. Branches of same rung, exclusive conditions — still a conflict
#     because branches always execute (with different enabled states)
# ---------------------------------------------------------------------------


class TestBranchExclusive:
    def test_exclusive_branch_conditions_still_conflict(self):
        """Branches always execute; disabled branch resets the target."""
        with Program() as prog:
            with Rung():
                with branch(State == 1):
                    out(Light)
                with branch(State == 2):
                    out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 1


# ---------------------------------------------------------------------------
# 8. No relevant instructions at all
# ---------------------------------------------------------------------------


class TestNoInstructions:
    def test_empty_program(self):
        with Program() as prog:
            with Rung(ButtonA):
                latch(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 0
        assert report.summary() == "No conflicting outputs."


# ---------------------------------------------------------------------------
# 9. Single out (no duplicate)
# ---------------------------------------------------------------------------


class TestSingleOut:
    def test_no_conflict(self):
        with Program() as prog:
            with Rung(ButtonA):
                out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 0


# ---------------------------------------------------------------------------
# 10. latch/reset excluded
# ---------------------------------------------------------------------------


class TestLatchResetExcluded:
    def test_latch_not_flagged(self):
        with Program() as prog:
            with Rung(ButtonA):
                latch(Light)
            with Rung(ButtonB):
                latch(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 0

    def test_reset_not_flagged(self):
        with Program() as prog:
            with Rung(ButtonA):
                reset(Light)
            with Rung(ButtonB):
                reset(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 0


# ---------------------------------------------------------------------------
# 11. BlockRange target
# ---------------------------------------------------------------------------


class TestBlockRangeTarget:
    def test_overlapping_block_ranges(self):
        Y = OutputBlock("Y", TagType.BOOL, 1, 16)
        with Program() as prog:
            with Rung(ButtonA):
                out(Y.select(1, 4))
            with Rung(ButtonB):
                out(Y.select(1, 4))

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 4  # Y1, Y2, Y3, Y4


# ---------------------------------------------------------------------------
# 12. Mixed main + subroutine out, non-exclusive
# ---------------------------------------------------------------------------


class TestMixedMainSubroutine:
    def test_non_exclusive(self):
        with Program() as prog:
            with Rung(ButtonA):
                out(Light)
            with Rung(ButtonB):
                call("sub")
            with subroutine("sub"):
                with Rung():
                    out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 1


# ---------------------------------------------------------------------------
# 13. Mixed main + subroutine out — always a conflict
#     because the main-scope out always executes (resets when disabled)
# ---------------------------------------------------------------------------


class TestMixedMainSubroutineAlwaysConflicts:
    def test_mixed_is_always_conflict(self):
        """Main out always resets; order-dependent stomping."""
        with Program() as prog:
            with Rung(State == 1):
                out(Light)
            with Rung(State == 2):
                call("sub")
            with subroutine("sub"):
                with Rung():
                    out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 1


# ---------------------------------------------------------------------------
# 14. CompareEq vs CompareNe complement — still conflict in same scope
# ---------------------------------------------------------------------------


class TestEqualityComplement:
    def test_eq_ne_same_scope_still_conflict(self):
        """State==1 vs State!=1 in main: both outs always execute."""
        with Program() as prog:
            with Rung(State == 1):
                out(Light)
            with Rung(State != 1):
                out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 1


# ---------------------------------------------------------------------------
# 15. Two unconditional rungs
# ---------------------------------------------------------------------------


class TestUnconditionalRungs:
    def test_unconditional_conflict(self):
        with Program() as prog:
            with Rung():
                out(Light)
            with Rung():
                out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 1


# ---------------------------------------------------------------------------
# 16. Three-way conflict
# ---------------------------------------------------------------------------


class TestThreeWayConflict:
    def test_three_sites_one_finding(self):
        with Program() as prog:
            with Rung(ButtonA):
                out(Light)
            with Rung(ButtonB):
                out(Light)
            with Rung(ButtonC):
                out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 1
        assert len(report.findings[0].sites) == 3


# ---------------------------------------------------------------------------
# 17. Range complement in same scope — still conflict
# ---------------------------------------------------------------------------


class TestRangeComplement:
    def test_lt_ge_same_scope_conflict(self):
        with Program() as prog:
            with Rung(State < 5):
                out(Light)
            with Rung(State >= 5):
                out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 1

    def test_le_gt_same_scope_conflict(self):
        with Program() as prog:
            with Rung(State <= 5):
                out(Light)
            with Rung(State > 5):
                out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 1


# ---------------------------------------------------------------------------
# 18. Two timers sharing same done_bit
# ---------------------------------------------------------------------------


class TestTimerConflict:
    def test_shared_done_bit(self):
        with Program() as prog:
            with Rung(ButtonA):
                on_delay(Timer[1], preset=1000)
            with Rung(ButtonB):
                on_delay(Timer[1], preset=2000)

        report = validate_conflicting_outputs(prog)
        assert any(f.target_name == "Timer1_done" for f in report.findings)

    def test_shared_accumulator(self):
        with Program() as prog:
            with Rung(ButtonA):
                on_delay(Timer[1], preset=1000)
            with Rung(ButtonB):
                on_delay(Timer[1], preset=2000)

        report = validate_conflicting_outputs(prog)
        assert any(f.target_name == "Timer1_acc" for f in report.findings)


# ---------------------------------------------------------------------------
# 19. Two counters sharing same accumulator
# ---------------------------------------------------------------------------


class TestCounterConflict:
    def test_shared_accumulator(self):
        with Program() as prog:
            with Rung(ButtonA):
                count_up(Counter[1], preset=10).reset(ResetBtn)
            with Rung(ButtonB):
                count_up(Counter[1], preset=20).reset(ResetBtn)

        report = validate_conflicting_outputs(prog)
        assert any(f.target_name == "Counter1_acc" for f in report.findings)


# ---------------------------------------------------------------------------
# 20. Timer + out targeting same tag
# ---------------------------------------------------------------------------


class TestTimerOutConflict:
    def test_timer_done_bit_vs_out(self):
        with Program() as prog:
            with Rung(ButtonA):
                on_delay(Timer[2], preset=1000)
            with Rung(ButtonB):
                out(Timer[2].done)

        report = validate_conflicting_outputs(prog)
        assert any(f.target_name == "Timer2_done" for f in report.findings)


# ---------------------------------------------------------------------------
# 21. Two drums sharing outputs — exclusive conditions don't help in same scope
# ---------------------------------------------------------------------------


class TestDrumSameScopeConflict:
    def test_shared_outputs_same_scope(self):
        """Even with exclusive conditions, drums in main scope always execute."""
        D_out1 = Bool("D_out1")
        D_out2 = Bool("D_out2")
        D_step1 = Int("D_step1")
        D_step2 = Int("D_step2")
        D_flag1 = Bool("D_flag1")
        D_flag2 = Bool("D_flag2")
        Ev1 = Bool("Ev1")
        Ev2 = Bool("Ev2")

        with Program() as prog:
            with Rung(State == 1):
                event_drum(
                    outputs=[D_out1, D_out2],
                    events=[Ev1, Ev2],
                    pattern=[[1, 0], [0, 1]],
                    current_step=D_step1,
                    completion_flag=D_flag1,
                ).reset(ResetBtn)
            with Rung(State == 2):
                event_drum(
                    outputs=[D_out1, D_out2],
                    events=[Ev1, Ev2],
                    pattern=[[0, 1], [1, 0]],
                    current_step=D_step2,
                    completion_flag=D_flag2,
                ).reset(ResetBtn)

        report = validate_conflicting_outputs(prog)
        # D_out1 and D_out2 each appear as conflicts
        assert any(f.target_name == "D_out1" for f in report.findings)
        assert any(f.target_name == "D_out2" for f in report.findings)

    def test_exclusive_drums_in_subroutines(self):
        """Drums in different subroutines with exclusive callers are safe."""
        D_out1 = Bool("D_out1_s")
        D_step1 = Int("D_step1_s")
        D_step2 = Int("D_step2_s")
        D_flag1 = Bool("D_flag1_s")
        D_flag2 = Bool("D_flag2_s")
        Ev1 = Bool("Ev1_s")

        with Program() as prog:
            with Rung(State == 1):
                call("drum_a")
            with Rung(State == 2):
                call("drum_b")
            with subroutine("drum_a"):
                with Rung():
                    event_drum(
                        outputs=[D_out1],
                        events=[Ev1],
                        pattern=[[1]],
                        current_step=D_step1,
                        completion_flag=D_flag1,
                    ).reset(ResetBtn)
            with subroutine("drum_b"):
                with Rung():
                    event_drum(
                        outputs=[D_out1],
                        events=[Ev1],
                        pattern=[[0]],
                        current_step=D_step2,
                        completion_flag=D_flag2,
                    ).reset(ResetBtn)

        report = validate_conflicting_outputs(prog)
        assert not any(f.target_name == "D_out1_s" for f in report.findings)


# ---------------------------------------------------------------------------
# 22. Shift register sharing bit_range with out
# ---------------------------------------------------------------------------


class TestShiftOutConflict:
    def test_shift_vs_out(self):
        C = Block("C", TagType.BOOL, 1, 16)
        ClockBit = Bool("ClockBit")

        with Program() as prog:
            with Rung(ButtonA):
                shift(C.select(1, 4)).clock(ClockBit).reset(ResetBtn)
            with Rung(ButtonB):
                out(C.select(1, 4))

        report = validate_conflicting_outputs(prog)
        # C1..C4 each appear as a conflict
        assert len(report.findings) == 4


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_uncalled_subroutine_no_finding(self):
        """Uncalled subroutine out() does not conflict with anything."""
        with Program() as prog:
            with Rung():
                out(Light)
            with subroutine("unused"):
                with Rung():
                    out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 0

    def test_same_rung_same_instruction_not_duplicate(self):
        """A single out() instruction is never a conflict by itself."""
        with Program() as prog:
            with Rung(ButtonA):
                out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 0

    def test_summary_with_findings(self):
        with Program() as prog:
            with Rung(ButtonA):
                out(Light)
            with Rung(ButtonB):
                out(Light)

        report = validate_conflicting_outputs(prog)
        assert "1 conflicting output target(s)" in report.summary()

    def test_different_targets_no_conflict(self):
        with Program() as prog:
            with Rung(ButtonA):
                out(Light)
            with Rung(ButtonB):
                out(Motor)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 0

    def test_off_delay_conflict(self):
        with Program() as prog:
            with Rung(ButtonA):
                off_delay(Timer[3], preset=1000)
            with Rung(ButtonB):
                off_delay(Timer[3], preset=2000)

        report = validate_conflicting_outputs(prog)
        assert any(f.target_name == "Timer3_done" for f in report.findings)

    def test_count_down_conflict(self):
        with Program() as prog:
            with Rung(ButtonA):
                count_down(Counter[2], preset=10).reset(ResetBtn)
            with Rung(ButtonB):
                count_down(Counter[2], preset=20).reset(ResetBtn)

        report = validate_conflicting_outputs(prog)
        assert any(f.target_name == "Counter2_done" for f in report.findings)

    def test_exclusive_conditions_same_scope_still_conflict(self):
        """Timers in same scope always execute — conditions don't help."""
        with Program() as prog:
            with Rung(State == 1):
                on_delay(Timer[4], preset=1000)
            with Rung(State == 2):
                on_delay(Timer[4], preset=2000)

        report = validate_conflicting_outputs(prog)
        assert any(f.target_name == "Timer4_done" for f in report.findings)
        assert any(f.target_name == "Timer4_acc" for f in report.findings)

    def test_exclusive_timers_in_subroutines(self):
        """Timers in different subroutines with exclusive callers are safe."""
        with Program() as prog:
            with Rung(State == 1):
                call("timer_a")
            with Rung(State == 2):
                call("timer_b")
            with subroutine("timer_a"):
                with Rung():
                    on_delay(Timer[5], preset=1000)
            with subroutine("timer_b"):
                with Rung():
                    on_delay(Timer[5], preset=2000)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 0


class TestCallerExclusivityPatterns:
    """Condition exclusivity patterns work correctly at the caller level."""

    def test_complementary_bit_callers(self):
        """BitCondition vs NormallyClosedCondition on callers → safe."""
        with Program() as prog:
            with Rung(Flag):
                call("sub_on")
            with Rung(~Flag):
                call("sub_off")
            with subroutine("sub_on"):
                with Rung():
                    out(Light)
            with subroutine("sub_off"):
                with Rung():
                    out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 0

    def test_eq_ne_callers(self):
        """CompareEq vs CompareNe on callers → safe."""
        with Program() as prog:
            with Rung(State == 1):
                call("sub_eq")
            with Rung(State != 1):
                call("sub_ne")
            with subroutine("sub_eq"):
                with Rung():
                    out(Light)
            with subroutine("sub_ne"):
                with Rung():
                    out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 0

    def test_range_complement_callers(self):
        """CompareLt vs CompareGe on callers → safe."""
        with Program() as prog:
            with Rung(State < 5):
                call("sub_low")
            with Rung(State >= 5):
                call("sub_high")
            with subroutine("sub_low"):
                with Rung():
                    out(Light)
            with subroutine("sub_high"):
                with Rung():
                    out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 0

    def test_non_exclusive_callers_conflict(self):
        """Non-exclusive callers → conflict even across subroutines."""
        with Program() as prog:
            with Rung(ButtonA):
                call("sub_a")
            with Rung(ButtonB):
                call("sub_b")
            with subroutine("sub_a"):
                with Rung():
                    out(Light)
            with subroutine("sub_b"):
                with Rung():
                    out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 1
