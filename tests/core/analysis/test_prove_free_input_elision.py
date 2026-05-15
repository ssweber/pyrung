"""Tests for the exhaustive verifier and state-space snapshot."""

from __future__ import annotations

import importlib

import pytest

from pyrung.core import (
    PLC,
    Block,
    Bool,
    Int,
    Or,
    Program,
    Rung,
    TagType,
    latch,
    out,
    reset,
    rise,
)
from pyrung.core.analysis.prove import (
    Counterexample,
    Intractable,
    Proven,
    TraceStep,
    prove,
    reachable_states,
)

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
# Group: Free input elision
# ===================================================================


class TestFreeInputElision:
    """Verify the free-input state key elision optimization."""

    def test_free_input_reduces_states(self):
        """Free input (xic only) is not in nondeterministic_names; state count halved."""
        free = Bool("Free", external=True)
        edge = Bool("Edge", external=True)
        x = Bool("X")

        with Program(strict=False) as logic:
            with Rung(free):
                latch(x)
            with Rung(rise(edge)):
                reset(x)

        context = prove_module._build_explore_context(logic)
        assert not isinstance(context, Intractable)
        assert "Free" in context.free_input_names
        assert "Free" not in context.nondeterministic_names
        assert "Edge" not in context.free_input_names
        assert "Edge" in context.nondeterministic_names

    def test_shift_clock_is_edge_bearing(self):
        """ShiftInstruction clock ND input stays in nondeterministic_names."""
        from pyrung.core import shift

        clk = Bool("Clk", external=True)
        data = Bool("Data", external=True)
        rst = Bool("Rst", external=True)
        bits = Block("SR", TagType.BOOL, 1, 4)

        with Program(strict=False) as logic:
            with Rung(data):
                shift(bits.select(1, 4)).clock(clk).reset(rst)

        context = prove_module._build_explore_context(logic)
        assert not isinstance(context, Intractable)
        assert "Clk" not in context.free_input_names
        assert "Clk" in context.nondeterministic_names

    def test_drum_jog_is_edge_bearing(self):
        """Drum jog ND input stays in nondeterministic_names."""
        from pyrung.core import event_drum

        enable = Bool("Enable", external=True)
        jog = Bool("Jog", external=True)
        reset_sig = Bool("Rst", external=True)
        e1 = Bool("E1", external=True)
        step = Int("Step")
        done = Bool("Done")
        y1 = Bool("Y1")

        with Program(strict=False) as logic:
            with Rung(enable):
                event_drum(
                    outputs=[y1],
                    events=[e1],
                    pattern=[[1]],
                    current_step=step,
                    completion_flag=done,
                ).reset(reset_sig).jog(jog)

        context = prove_module._build_explore_context(logic)
        assert not isinstance(context, Intractable)
        assert "Jog" not in context.free_input_names
        assert "Jog" in context.nondeterministic_names

    def test_drum_event_is_edge_bearing(self):
        """EventDrum per-step event ND input stays in nondeterministic_names."""
        from pyrung.core import event_drum

        enable = Bool("Enable", external=True)
        reset_sig = Bool("Rst", external=True)
        e1 = Bool("E1", external=True)
        step = Int("Step")
        done = Bool("Done")
        y1 = Bool("Y1")

        with Program(strict=False) as logic:
            with Rung(enable):
                event_drum(
                    outputs=[y1],
                    events=[e1],
                    pattern=[[1]],
                    current_step=step,
                    completion_flag=done,
                ).reset(reset_sig)

        context = prove_module._build_explore_context(logic)
        assert not isinstance(context, Intractable)
        assert "E1" not in context.free_input_names
        assert "E1" in context.nondeterministic_names

    def test_drum_event_input_live_for_reachability(self):
        """ND input used only as a drum event must be live so BFS flips it."""
        from pyrung.core import event_drum

        ev = Bool("Ev", external=True)
        step = Int("Step")
        done = Bool("Done")
        y1 = Bool("Y1")

        with Program(strict=False) as logic:
            with Rung():
                event_drum(
                    outputs=[y1],
                    events=[ev, ev],
                    pattern=[[False], [True]],
                    current_step=step,
                    completion_flag=done,
                ).reset(done)

        result = reachable_states(logic, project=["Y1"], max_states=500, depth_budget=10)
        assert not isinstance(result, Intractable)
        values = {dict(s)["Y1"] for s in result}
        assert True in values, f"BFS never reached Y1=True; states={result}"

    def test_projected_free_input_kept(self):
        """Free input in project stays in nondeterministic_names."""
        a = Bool("A", external=True)
        x = Bool("X")

        with Program(strict=False) as logic:
            with Rung(a):
                out(x)

        context = prove_module._build_explore_context(logic, project=("A",))
        assert not isinstance(context, Intractable)
        assert "A" in context.nondeterministic_names

    def test_all_edge_bearing_no_reduction(self):
        """All ND inputs use rise() — no free inputs, names unchanged."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(rise(a)):
                latch(x)
            with Rung(rise(b)):
                latch(y)

        context = prove_module._build_explore_context(logic)
        assert not isinstance(context, Intractable)
        assert context.free_input_names == frozenset()
        assert "A" in context.nondeterministic_names
        assert "B" in context.nondeterministic_names

    def test_soundness_preserved(self):
        """Latch controlled by free input: prove() result is sound."""
        free = Bool("Free", external=True)
        x = Bool("X")

        with Program(strict=False) as logic:
            with Rung(free):
                latch(x)

        result = prove(logic, ~x)
        assert isinstance(result, Counterexample)

        result2 = prove(logic, Or(x, ~x))
        assert isinstance(result2, Proven)
