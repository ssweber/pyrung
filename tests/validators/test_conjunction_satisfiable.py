"""Tests for per-tag conjunction satisfiability checking.

Covers _conjunction_satisfiable which replaces pairwise contradiction
detection with interval/domain feasibility per tag.
"""

from pyrung.core import Bool, Int, Program, Rung, call, latch, out, reset, subroutine
from pyrung.core.condition import (
    AnyCondition,
    BitCondition,
    CompareEq,
    CompareGe,
    CompareGt,
    CompareLe,
    CompareLt,
    CompareNe,
    NormallyClosedCondition,
)
from pyrung.core.tag import Tag, TagType
from pyrung.core.validation._common import (
    _chain_pair_mutually_exclusive,
    _conjunction_satisfiable,
)
from pyrung.core.validation.duplicate_out import validate_conflicting_outputs
from pyrung.core.validation.stuck_bits import (
    CORE_STUCK_HIGH,
    validate_stuck_bits,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

T = Int("T")
T2 = Int("T2")
B = Bool("B")
B2 = Bool("B2")

State = Int("State")
Light = Bool("Light")
ButtonA = Bool("ButtonA")
ButtonB = Bool("ButtonB")


# ---------------------------------------------------------------------------
# 1. Transitive numeric unsatisfiability
# ---------------------------------------------------------------------------


class TestTransitiveNumericUnsatisfiable:
    def test_eq_and_gt_contradict(self):
        """CompareEq(T, 4) + CompareGt(T, 5) → tag must be 4 AND >5."""
        assert not _conjunction_satisfiable([CompareEq(T, 4), CompareGt(T, 5)])

    def test_eq_and_lt_contradict(self):
        """CompareEq(T, 10) + CompareLt(T, 5) → tag must be 10 AND <5."""
        assert not _conjunction_satisfiable([CompareEq(T, 10), CompareLt(T, 5)])

    def test_eq_within_range_satisfiable(self):
        """CompareEq(T, 7) + CompareGt(T, 5) → 7 > 5, satisfiable."""
        assert _conjunction_satisfiable([CompareEq(T, 7), CompareGt(T, 5)])


# ---------------------------------------------------------------------------
# 2. Three-way numeric unsatisfiability
# ---------------------------------------------------------------------------


class TestThreeWayNumericUnsatisfiable:
    def test_eq_outside_range(self):
        """CompareGt(T, 5) + CompareLt(T, 10) + CompareEq(T, 3).

        Range is (5, 10) → integers 6..9.  Eq pins to 3, not in range.
        """
        conds = [CompareGt(T, 5), CompareLt(T, 10), CompareEq(T, 3)]
        assert not _conjunction_satisfiable(conds)

    def test_eq_inside_range(self):
        """CompareGt(T, 5) + CompareLt(T, 10) + CompareEq(T, 7) → satisfiable."""
        conds = [CompareGt(T, 5), CompareLt(T, 10), CompareEq(T, 7)]
        assert _conjunction_satisfiable(conds)


# ---------------------------------------------------------------------------
# 3. Satisfiable numeric range
# ---------------------------------------------------------------------------


class TestSatisfiableRange:
    def test_open_range(self):
        """CompareGt(T, 5) + CompareLt(T, 10) → integers 6..9, satisfiable."""
        assert _conjunction_satisfiable([CompareGt(T, 5), CompareLt(T, 10)])

    def test_closed_range(self):
        """CompareGe(T, 5) + CompareLe(T, 10) → integers 5..10, satisfiable."""
        assert _conjunction_satisfiable([CompareGe(T, 5), CompareLe(T, 10)])

    def test_singleton_range(self):
        """CompareGe(T, 5) + CompareLe(T, 5) → exactly {5}, satisfiable."""
        assert _conjunction_satisfiable([CompareGe(T, 5), CompareLe(T, 5)])

    def test_empty_range_discrete(self):
        """CompareGt(T, 5) + CompareLt(T, 6) → 6..5, empty for integers."""
        assert not _conjunction_satisfiable([CompareGt(T, 5), CompareLt(T, 6)])


# ---------------------------------------------------------------------------
# 4. Boolean contradiction
# ---------------------------------------------------------------------------


class TestBoolContradiction:
    def test_bit_and_normally_closed(self):
        """BitCondition(B) + NormallyClosedCondition(B) → True AND False."""
        assert not _conjunction_satisfiable([BitCondition(B), NormallyClosedCondition(B)])

    def test_same_polarity_satisfiable(self):
        """Two BitConditions on same tag → both require True, satisfiable."""
        assert _conjunction_satisfiable([BitCondition(B), BitCondition(B)])


# ---------------------------------------------------------------------------
# 5. Mixed tags — one infeasible tag makes conjunction unsatisfiable
# ---------------------------------------------------------------------------


class TestMixedTags:
    def test_one_tag_infeasible(self):
        """Contradictory on T, satisfiable on T2 → overall unsatisfiable."""
        conds = [
            CompareEq(T, 4),
            CompareGt(T, 5),  # T can't be 4 and >5
            CompareGe(T2, 0),
            CompareLe(T2, 100),  # T2 in [0, 100] is fine
        ]
        assert not _conjunction_satisfiable(conds)

    def test_all_tags_feasible(self):
        """Both tags satisfiable → satisfiable."""
        conds = [
            CompareGe(T, 0),
            CompareLe(T, 10),
            CompareGe(T2, 5),
            CompareLe(T2, 15),
        ]
        assert _conjunction_satisfiable(conds)


# ---------------------------------------------------------------------------
# 6. Cross-chain mutual exclusivity (no single cross-pair contradicts)
# ---------------------------------------------------------------------------


class TestCrossChainMutualExclusivity:
    def test_transitive_cross_chain(self):
        """Chain A: T > 5, T < 8.  Chain B: T > 10.

        No single pair from A contradicts any from B (pairwise misses this).
        But combined T > 5 AND T < 8 AND T > 10 is unsatisfiable.
        """
        chain_a = (CompareGt(T, 5), CompareLt(T, 8))
        chain_b = (CompareGt(T, 10),)
        assert _chain_pair_mutually_exclusive(chain_a, chain_b)

    def test_non_exclusive_cross_chain(self):
        """Chain A: T > 5.  Chain B: T < 10.  Overlap at 6..9."""
        chain_a = (CompareGt(T, 5),)
        chain_b = (CompareLt(T, 10),)
        assert not _chain_pair_mutually_exclusive(chain_a, chain_b)

    def test_integration_cross_chain_subroutine(self):
        """Two subroutines with cross-chain unsatisfiable callers.

        sub_a called when State > 5 AND State < 8.
        sub_b called when State > 10.
        No pairwise contradiction, but combined is infeasible.
        """
        with Program() as prog:
            with Rung(State > 5, State < 8):
                call("sub_a")
            with Rung(State > 10):
                call("sub_b")
            with subroutine("sub_a"):
                with Rung():
                    out(Light)
            with subroutine("sub_b"):
                with Rung():
                    out(Light)

        report = validate_conflicting_outputs(prog)
        assert len(report.findings) == 0


# ---------------------------------------------------------------------------
# 7. AnyCondition treated as opaque
# ---------------------------------------------------------------------------


class TestAnyConditionOpaque:
    def test_any_condition_ignored(self):
        """AnyCondition in the chain doesn't cause false infeasibility."""
        conds = [
            AnyCondition(BitCondition(B), NormallyClosedCondition(B)),
            CompareGe(T, 0),
        ]
        assert _conjunction_satisfiable(conds)

    def test_any_condition_does_not_mask_real_contradiction(self):
        """AnyCondition present but other conditions still contradict."""
        conds = [
            AnyCondition(BitCondition(B)),
            CompareEq(T, 4),
            CompareGt(T, 5),
        ]
        assert not _conjunction_satisfiable(conds)


# ---------------------------------------------------------------------------
# 8. Empty / singleton inputs
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_conditions(self):
        """Empty condition list → vacuously satisfiable."""
        assert _conjunction_satisfiable([])

    def test_single_condition(self):
        """Single condition → always satisfiable."""
        assert _conjunction_satisfiable([CompareGt(T, 5)])
        assert _conjunction_satisfiable([BitCondition(B)])

    def test_ne_with_singleton_interval(self):
        """CompareGe(T, 5) + CompareLe(T, 5) + CompareNe(T, 5) → empty."""
        conds = [CompareGe(T, 5), CompareLe(T, 5), CompareNe(T, 5)]
        assert not _conjunction_satisfiable(conds)

    def test_ne_with_wide_interval(self):
        """CompareGe(T, 0) + CompareLe(T, 10) + CompareNe(T, 5) → satisfiable."""
        conds = [CompareGe(T, 0), CompareLe(T, 10), CompareNe(T, 5)]
        assert _conjunction_satisfiable(conds)

    def test_two_different_eq_points(self):
        """CompareEq(T, 3) + CompareEq(T, 7) → unsatisfiable."""
        assert not _conjunction_satisfiable([CompareEq(T, 3), CompareEq(T, 7)])

    def test_eq_with_ne_same_value(self):
        """CompareEq(T, 5) + CompareNe(T, 5) → unsatisfiable."""
        assert not _conjunction_satisfiable([CompareEq(T, 5), CompareNe(T, 5)])


# ---------------------------------------------------------------------------
# 9. Continuous (Real) tag handling
# ---------------------------------------------------------------------------


R = Tag("R", type=TagType.REAL)


class TestContinuousDomain:
    def test_gt_and_lt_float_satisfiable(self):
        """CompareGt(R, 5.0) + CompareLt(R, 6.0) → continuous (5, 6), satisfiable."""
        assert _conjunction_satisfiable([CompareGt(R, 5.0), CompareLt(R, 6.0)])

    def test_gt_and_le_same_float_unsatisfiable(self):
        """CompareGt(R, 5.0) + CompareLe(R, 5.0) → (5, 5] empty."""
        assert not _conjunction_satisfiable([CompareGt(R, 5.0), CompareLe(R, 5.0)])

    def test_ge_and_le_same_float_satisfiable(self):
        """CompareGe(R, 5.0) + CompareLe(R, 5.0) → {5.0}, satisfiable."""
        assert _conjunction_satisfiable([CompareGe(R, 5.0), CompareLe(R, 5.0)])


# ---------------------------------------------------------------------------
# 10. Integration: stuck_bits catches transitive unreachability
# ---------------------------------------------------------------------------


class TestStuckBitsIntegration:
    def test_transitive_unreachable_latch(self):
        """Latch gated by State == 4 AND State > 5 → unreachable → stuck low."""
        with Program() as prog:
            with Rung(State == 4, State > 5):
                latch(Light)
            with Rung(ButtonA):
                reset(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 1
        assert report.findings[0].target_name == "Light"

    def test_transitive_unreachable_caller(self):
        """Reset's caller has State > 5 AND State < 3 → unreachable → stuck high."""
        with Program() as prog:
            with Rung(ButtonA):
                latch(Light)
            with Rung(State > 5, State < 3):
                call("do_reset")
            with subroutine("do_reset"):
                with Rung():
                    reset(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 1
        assert report.findings[0].code == CORE_STUCK_HIGH
