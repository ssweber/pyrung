"""Fixture + specification tests for RUNG_CONTRADICTION / RUNG_TAUTOLOGY.

The canonical specimen is the invalid-mode-request guard rung that shipped in
``examples/packml_bench.py`` (spec §1.1).  The rung's args AND together::

    with rung(Or(StateCurrent != IDLE, StateCurrent != STOPPED, StateCurrent != ABORTED),
              UnitModeCmd < 1, UnitModeCmd > 3):
        copy(0, UnitModeCmd)
        reset(ModeChgRequest)

Two independent defects, one rung — a double De Morgan's slip:

* ``UnitModeCmd < 1 AND UnitModeCmd > 3`` is an interval contradiction; no
  integer satisfies it, so the rung never fires (RUNG_CONTRADICTION).
* ``Or(x != a, x != b, x != c)`` over a single variable is a tautology (a value
  cannot equal all three states at once), so the state gate contributes nothing
  (RUNG_TAUTOLOGY); the effective condition is the residual ``UnitModeCmd < 1
  AND UnitModeCmd > 3``.

The bench itself is repaired (task #6); this file preserves the buggy pattern as
the should-fail fixture and pins what the new rules must report.  The
``validate()``-facing assertions are ``xfail(strict=True)`` until the rules land
— when they do, the XPASS forces the marker off.
"""

from __future__ import annotations

import pytest

from pyrung.core import Bool, Int, Or, Program, Rung, copy, reset
from pyrung.core.condition import (
    AnyCondition,
    BitCondition,
    CompareEq,
    CompareGt,
    CompareLt,
    NormallyClosedCondition,
)
from pyrung.core.validation._common import _conjunction_satisfiable
from pyrung.core.validation.report import validate
from pyrung.core.validation.sat import (
    conjunction_satisfiable,
    disjunction_tautological,
    negate_leaf,
)

# PackML state ordinals, mirrored from the bench so the fixture reads the same.
IDLE, STOPPED, ABORTED = 4, 2, 9


def _buggy_guard_program() -> Program:
    """The unsatisfiable invalid-mode guard rung, as a standalone program."""
    state_current = Int("StateCurrent")
    unit_mode_cmd = Int("UnitModeCmd", external=True)
    mode_chg_request = Bool("ModeChgRequest", external=True)

    with Program(strict=False) as prog:
        with Rung(
            Or(
                state_current != IDLE,
                state_current != STOPPED,
                state_current != ABORTED,
            ),
            unit_mode_cmd < 1,
            unit_mode_cmd > 3,
        ):
            copy(0, unit_mode_cmd)
            reset(mode_chg_request)
    return prog


def _guard_rung(prog: Program):
    """The single rung in the fixture program (a stored ``core.rung.Rung``)."""
    (rung,) = prog.rungs
    return rung


class TestFixtureReproducesBug:
    """Anchors that always pass — they prove the specimen is genuinely broken
    and that the contradiction machinery already flags it (only the rule wiring
    is missing)."""

    def test_conjunction_is_unsatisfiable(self):
        rung = _guard_rung(_buggy_guard_program())
        assert _conjunction_satisfiable(rung._conditions) is False

    def test_blocking_pair_is_the_range_contradiction(self):
        rung = _guard_rung(_buggy_guard_program())
        lts = [c for c in rung._conditions if isinstance(c, CompareLt)]
        gts = [c for c in rung._conditions if isinstance(c, CompareGt)]
        assert len(lts) == 1 and len(gts) == 1
        # Same tag, disjoint intervals: < 1 and > 3 cannot both hold.
        assert lts[0].tag.name == gts[0].tag.name == "UnitModeCmd"
        assert lts[0].value == 1 and gts[0].value == 3


@pytest.mark.xfail(strict=True, reason="RUNG_CONTRADICTION not yet implemented")
class TestRungContradiction:
    def test_contradiction_reported(self):
        report = validate(_buggy_guard_program())
        codes = {f.code for f in report}
        assert "RUNG_CONTRADICTION" in codes

    def test_finding_names_blocking_pair(self):
        report = validate(_buggy_guard_program())
        contradiction = next(f for f in report if f.code == "RUNG_CONTRADICTION")
        # The diagnostic must surface the contradictory pair by name/value.
        assert "UnitModeCmd" in contradiction.message
        assert "< 1" in contradiction.message
        assert "> 3" in contradiction.message


@pytest.mark.xfail(strict=True, reason="RUNG_TAUTOLOGY not yet implemented")
class TestRungTautology:
    def test_tautology_reported_on_or_term(self):
        report = validate(_buggy_guard_program())
        codes = {f.code for f in report}
        assert "RUNG_TAUTOLOGY" in codes

    def test_tautology_reports_residual(self):
        report = validate(_buggy_guard_program())
        tautology = next(f for f in report if f.code == "RUNG_TAUTOLOGY")
        # Half the value is showing the real gate: the residual after the
        # always-true Or term is stripped out.
        assert "UnitModeCmd" in tautology.message


class TestSatReduction:
    """Proof-of-concept for the sat.py primitives (spec/plan §4).  The De Morgan
    reduction detects the tautological Or-subterm on the real fixture *before* the
    RUNG_TAUTOLOGY rule is wired into validate() — the validate()-facing xfails
    above stay red until that wiring lands (task #7/#9)."""

    def test_buggy_or_term_is_tautological(self):
        rung = _guard_rung(_buggy_guard_program())
        or_term = next(c for c in rung._conditions if isinstance(c, AnyCondition))
        # Or(x != IDLE, x != STOPPED, x != ABORTED) is always true for one variable.
        assert disjunction_tautological(or_term.conditions) is True

    def test_real_gate_or_is_not_tautological(self):
        x = Int("X")
        # Or(x != 4, x < 2) is a genuine gate (false at x == 4) — no false positive.
        assert disjunction_tautological([x != 4, x < 2]) is False

    def test_bool_complement_or_is_tautological(self):
        b = Bool("B")
        assert disjunction_tautological([BitCondition(b), NormallyClosedCondition(b)]) is True

    def test_opaque_only_disjunction_is_not_provable(self):
        a, b = Bool("A"), Bool("B")
        # Both terms negate cleanly here, but an empty survivor set must be False;
        # use a mixed case: a lone bit is not tautological on its own.
        assert disjunction_tautological([BitCondition(a)]) is False
        # Or(A, ~A) survives and is tautological; dropping one still isn't.
        assert disjunction_tautological([BitCondition(a), BitCondition(b)]) is False

    def test_negate_leaf_flips_compare(self):
        x = Int("X")
        neg = negate_leaf(x != 5)  # CompareNe → CompareEq
        assert isinstance(neg, CompareEq)
        assert neg.value == 5

    def test_negate_leaf_opaque_returns_none(self):
        x = Int("X")
        # An AnyCondition is compound, not a negatable leaf.
        assert negate_leaf(Or(x != 1, x != 2)) is None

    def test_conjunction_satisfiable_matches_common(self):
        rung = _guard_rung(_buggy_guard_program())
        # Public alias agrees with the private solver it delegates to.
        assert conjunction_satisfiable(rung._conditions) == _conjunction_satisfiable(
            rung._conditions
        )
