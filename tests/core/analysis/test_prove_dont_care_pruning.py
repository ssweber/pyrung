"""Tests for the exhaustive verifier and state-space snapshot."""

from __future__ import annotations

import importlib

import pytest

from pyrung.core import (
    PLC,
    Program,
)
from pyrung.core.analysis.prove import (
    Intractable,
    TraceStep,
    _eval_atom,
    _live_inputs,
    _partial_eval,
    prove,
)
from pyrung.core.analysis.simplified import And as ExprAnd
from pyrung.core.analysis.simplified import Atom, Const
from pyrung.core.analysis.simplified import Or as ExprOr

prove_module = importlib.import_module("pyrung.core.analysis.prove")


def _replay_trace(program: Program, trace: list[TraceStep]) -> PLC:
    """Replay a prove() counterexample trace on the concrete PLC."""
    plc = PLC(program, dt=0.010)
    for step in trace:
        plc.patch(step.inputs)
        for _ in range(step.scans):
            plc.step()
    return plc


def _assert_soundness(
    logic: Program,
    condition,
    *,
    max_states: int = 10_000,
    depth_budget: int = 20,
) -> None:
    """Assert that optimized and unoptimized prove() agree on the result type."""
    optimized = prove(
        logic, condition, max_states=max_states, depth_budget=depth_budget, journal=True
    )
    unoptimized = prove(
        logic,
        condition,
        max_states=max_states,
        depth_budget=depth_budget,
        _skip_optimizations=True,
        journal=True,
    )
    if isinstance(optimized, Intractable) or isinstance(unoptimized, Intractable):
        pytest.skip("one side intractable")
    assert type(optimized) is type(unoptimized), (
        f"optimized={type(optimized).__name__}, unoptimized={type(unoptimized).__name__}\n"
        f"--- optimized journal ---\n{optimized.journal}\n"
        f"--- unoptimized journal ---\n{unoptimized.journal}"
    )


# ===================================================================
# Group 3: Don't-care pruning
# ===================================================================


class TestDontCarePruning:
    def test_masked_input_not_live(self):
        """And(StateBit, Input) with StateBit=False → Input not live."""
        state_expr = ExprAnd((Atom("StateBit", "xic"), Atom("Input", "xic")))
        result = _partial_eval(state_expr, {"StateBit": False})
        assert isinstance(result, Const)
        assert result.value is False

    def test_unmasked_input_is_live(self):
        """And(StateBit, Input) with StateBit=True → Input remains."""
        state_expr = ExprAnd((Atom("StateBit", "xic"), Atom("Input", "xic")))
        result = _partial_eval(state_expr, {"StateBit": True})
        assert not isinstance(result, Const)

    def test_live_inputs_partial_masking(self):
        """Some inputs masked, others live — correct subset."""
        exprs = [
            ExprAnd((Atom("StateBit", "xic"), Atom("InputA", "xic"))),
            Atom("InputB", "xic"),
        ]
        nd_dims = {
            "InputA": (False, True),
            "InputB": (False, True),
        }
        state = {"StateBit": False, "InputA": False, "InputB": False}
        live = _live_inputs(state, nd_dims, exprs)
        assert "InputB" in live
        assert "InputA" not in live

    def test_all_inputs_live_in_or(self):
        """All inputs in top-level Or are live."""
        exprs = [ExprOr((Atom("InputA", "xic"), Atom("InputB", "xic")))]
        nd_dims = {"InputA": (False, True), "InputB": (False, True)}
        state = {"InputA": False, "InputB": False}
        live = _live_inputs(state, nd_dims, exprs)
        assert live == {"InputA", "InputB"}

    def test_hidden_residual_tag_keeps_upstream_input_live(self):
        """Residual hidden tags propagate liveness through their ND upstream deps."""
        exprs = [Atom("Stored", "gt", 150)]
        nd_dims = {"Source": tuple(range(3))}
        live = _live_inputs({}, nd_dims, exprs, {"Stored": frozenset({"Source"})})
        assert live == {"Source"}

    def test_eval_atom_xic(self):
        assert _eval_atom(Atom("X", "xic"), True) is True
        assert _eval_atom(Atom("X", "xic"), False) is False

    def test_eval_atom_xio(self):
        assert _eval_atom(Atom("X", "xio"), True) is False
        assert _eval_atom(Atom("X", "xio"), False) is True

    def test_eval_atom_eq(self):
        assert _eval_atom(Atom("X", "eq", 5), 5) is True
        assert _eval_atom(Atom("X", "eq", 5), 3) is False

    def test_eval_atom_rise_returns_none(self):
        assert _eval_atom(Atom("X", "rise"), True) is None
