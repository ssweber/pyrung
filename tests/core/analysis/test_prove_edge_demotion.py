"""Tests for edge-source tag demotion from the BFS state key.

When a tag appears in rise()/fall() but its exit value is scan-local
(determined by retained state + inputs, not by its own entry), the tag's
prev value can be forwarded from transitions instead of tracked in the
state key.  This reduces state space without overapproximation.
"""

from __future__ import annotations

import importlib

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Int,
    Program,
    Rung,
    calc,
    copy,
    fall,
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
    always,
    never,
    reachable_states,
)

prove_module = importlib.import_module("pyrung.core.analysis.prove")


def _replay_trace(program: Program, trace: list[TraceStep]) -> PLC:
    """Replay a always() counterexample trace on the concrete PLC."""
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
    """Assert that optimized and unoptimized always() agree on the result type."""
    optimized = always(
        logic, condition, max_states=max_states, depth_budget=depth_budget, journal=True
    )
    unoptimized = always(
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


def _assert_trace_replays(
    logic: Program,
    result: Counterexample,
    tag_name: str,
) -> None:
    """Verify a counterexample trace replays on the concrete PLC."""
    plc = _replay_trace(logic, result.trace)
    assert plc.current_state.tags[tag_name] is True


# ===================================================================
# Group: Qualifying tag identification
# ===================================================================


class TestDemotionClassification:
    """Verify which tags get demoted vs retained."""

    def test_ote_written_edge_source_is_demoted(self):
        """OTE-written tag used in rise() is combinational → demoted."""
        button = Bool("Button", external=True)
        sensor = Bool("Sensor")
        target = Bool("Target")
        with Program(strict=False) as logic:
            with Rung(button):
                out(sensor)
            with Rung(rise(sensor)):
                latch(target)

        context = prove_module._build_explore_context(logic)
        assert "Sensor" in context.demoted_edge_names
        assert "Sensor" not in context.edge_tag_names
        assert "Sensor" not in context.stateful_names

    def test_copy_written_edge_source_is_demoted(self):
        """Copy-written tag used in rise() is scan-local → demoted via elision."""
        button = Bool("Button", external=True)
        sensor = Bool("Sensor")
        target = Bool("Target")
        with Program(strict=False) as logic:
            with Rung(button):
                copy(True, sensor)
            with Rung(~button):
                copy(False, sensor)
            with Rung(rise(sensor)):
                latch(target)

        context = prove_module._build_explore_context(logic)
        assert "Sensor" in context.demoted_edge_names
        assert "Sensor" not in context.edge_tag_names

    def test_latch_written_edge_source_not_demoted(self):
        """Latch-written tag used in rise() depends on entry → not demoted."""
        button = Bool("Button", external=True)
        sensor = Bool("Sensor")
        target = Bool("Target")
        with Program(strict=False) as logic:
            with Rung(button):
                latch(sensor)
            with Rung(rise(sensor)):
                latch(target)

        context = prove_module._build_explore_context(logic)
        assert "Sensor" not in context.demoted_edge_names
        assert "Sensor" in context.edge_tag_names
        assert "Sensor" in context.stateful_names

    def test_self_referencing_calc_not_demoted(self):
        """Self-referencing calc (accumulator pattern) depends on entry → not demoted."""
        button = Bool("Button", external=True)
        flag = Bool("Flag")
        target = Bool("Target")
        counter = Int("Counter", min=0, max=5)
        with Program(strict=False) as logic:
            with Rung(button):
                calc(counter + 1, counter)
            with Rung(counter >= 3):
                out(flag)
            with Rung(rise(flag)):
                latch(target)

        context = prove_module._build_explore_context(logic)
        assert "Flag" in context.demoted_edge_names
        assert "Counter" not in context.demoted_edge_names

    def test_fall_contact_also_qualifies(self):
        """fall() edge source is demoted when exit is scan-local."""
        button = Bool("Button", external=True)
        sensor = Bool("Sensor")
        target = Bool("Target")
        with Program(strict=False) as logic:
            with Rung(button):
                out(sensor)
            with Rung(fall(sensor)):
                latch(target)

        context = prove_module._build_explore_context(logic)
        assert "Sensor" in context.demoted_edge_names

    def test_external_input_not_demoted(self):
        """External input with rise() stays in edge_tag_names (ND, not demotable)."""
        button = Bool("Button", external=True)
        target = Bool("Target")
        with Program(strict=False) as logic:
            with Rung(rise(button)):
                latch(target)

        context = prove_module._build_explore_context(logic)
        assert "Button" not in context.demoted_edge_names


# ===================================================================
# Group: Correctness with demotion active
# ===================================================================


class TestDemotionCorrectness:
    """Verify BFS produces correct results when edge tags are demoted."""

    def test_rise_on_ote_finds_counterexample(self):
        """rise() on OTE-written tag correctly detects transition."""
        button = Bool("Button", external=True)
        sensor = Bool("Sensor")
        target = Bool("Target")
        with Program(strict=False) as logic:
            with Rung(button):
                out(sensor)
            with Rung(rise(sensor)):
                latch(target)

        result = always(logic, ~target, depth_budget=10)
        assert isinstance(result, Counterexample)
        _assert_trace_replays(logic, result, "Target")

    def test_fall_on_ote_finds_counterexample(self):
        """fall() on OTE-written tag correctly detects True→False transition."""
        button = Bool("Button", external=True)
        sensor = Bool("Sensor")
        target = Bool("Target")
        with Program(strict=False) as logic:
            with Rung(button):
                out(sensor)
            with Rung(fall(sensor)):
                latch(target)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Button": True})
        plc.step()
        assert plc.current_state.tags["Sensor"] is True
        plc.patch({"Button": False})
        plc.step()
        assert plc.current_state.tags["Target"] is True

        result = always(logic, ~target, depth_budget=10)
        assert isinstance(result, Counterexample)
        _assert_trace_replays(logic, result, "Target")

    def test_rise_on_copy_written_finds_counterexample(self):
        """rise() on copy-written tag correctly detects transition."""
        button = Bool("Button", external=True)
        sensor = Bool("Sensor")
        target = Bool("Target")
        with Program(strict=False) as logic:
            with Rung(button):
                copy(True, sensor)
            with Rung(~button):
                copy(False, sensor)
            with Rung(rise(sensor)):
                latch(target)

        result = always(logic, ~target, depth_budget=10)
        assert isinstance(result, Counterexample)
        _assert_trace_replays(logic, result, "Target")

    def test_property_holds_with_demoted_edge(self):
        """Property that genuinely holds is still Proven with demotion."""
        button = Bool("Button", external=True)
        sensor = Bool("Sensor")
        target = Bool("Target")
        with Program(strict=False) as logic:
            with Rung(button):
                out(sensor)
            with Rung(rise(sensor)):
                latch(target)
            with Rung(~button):
                reset(target)

        result = never(logic, target, ~button, depth_budget=10)
        assert isinstance(result, Proven)

    def test_reachable_states_with_demoted_edge(self):
        """reachable_states works correctly with demoted edge tags."""
        button = Bool("Button", external=True)
        sensor = Bool("Sensor")
        target = Bool("Target")
        with Program(strict=False) as logic:
            with Rung(button):
                out(sensor)
            with Rung(rise(sensor)):
                latch(target)

        states = reachable_states(logic, project=["Target"])
        assert not isinstance(states, Intractable)
        target_values = {dict(s)["Target"] for s in states}
        assert True in target_values
        assert False in target_values


# ===================================================================
# Group: Soundness agreement
# ===================================================================


class TestDemotionSoundness:
    """Optimized (with demotion) and unoptimized must agree."""

    def test_ote_rise_soundness(self):
        button = Bool("Button", external=True)
        sensor = Bool("Sensor")
        target = Bool("Target")
        with Program(strict=False) as logic:
            with Rung(button):
                out(sensor)
            with Rung(rise(sensor)):
                latch(target)
        _assert_soundness(logic, ~target)

    def test_copy_rise_soundness(self):
        button = Bool("Button", external=True)
        sensor = Bool("Sensor")
        target = Bool("Target")
        with Program(strict=False) as logic:
            with Rung(button):
                copy(True, sensor)
            with Rung(~button):
                copy(False, sensor)
            with Rung(rise(sensor)):
                latch(target)
        _assert_soundness(logic, ~target)

    def test_ote_fall_soundness(self):
        button = Bool("Button", external=True)
        sensor = Bool("Sensor")
        target = Bool("Target")
        with Program(strict=False) as logic:
            with Rung(button):
                out(sensor)
            with Rung(fall(sensor)):
                latch(target)
        _assert_soundness(logic, ~target)

    def test_mixed_demoted_and_retained_soundness(self):
        """Program with both demoted and retained edge sources."""
        button = Bool("Button", external=True)
        sensor = Bool("Sensor")
        flag = Bool("Flag")
        target = Bool("Target")
        with Program(strict=False) as logic:
            with Rung(button):
                out(sensor)
            with Rung(rise(sensor)):
                latch(flag)
            with Rung(rise(flag)):
                latch(target)

        context = prove_module._build_explore_context(logic)
        assert "Sensor" in context.demoted_edge_names
        assert "Flag" not in context.demoted_edge_names
        _assert_soundness(logic, ~target)

    def test_multiple_demoted_tags_soundness(self):
        """Multiple OTE-written tags both demoted."""
        button = Bool("Button", external=True)
        enable = Bool("Enable", external=True)
        s1 = Bool("S1")
        s2 = Bool("S2")
        target = Bool("Target")
        with Program(strict=False) as logic:
            with Rung(button):
                out(s1)
            with Rung(enable):
                out(s2)
            with Rung(rise(s1), rise(s2)):
                latch(target)

        context = prove_module._build_explore_context(logic)
        assert "S1" in context.demoted_edge_names
        assert "S2" in context.demoted_edge_names
        _assert_soundness(logic, ~target)


# ===================================================================
# Group: Replay with demoted edge prev values
# ===================================================================


class TestDemotedEdgeReplay:
    """Verify that BFS traces carry demoted prev values through replay."""

    def test_how_replay_with_demoted_rise(self):
        """how() path through rise() on a demoted tag must replay correctly."""
        from pyrung.core.runner import PLC

        button = Bool("Button", external=True)
        sensor = Bool("Sensor")
        target = Bool("Target")
        with Program(strict=False) as logic:
            with Rung(button):
                out(sensor)
            with Rung(rise(sensor)):
                latch(target)

        context = prove_module._build_explore_context(logic)
        assert "Sensor" in context.demoted_edge_names

        plc = PLC(logic, dt=0.010)
        path = plc.how(target)
        assert path.reachable

        replay = PLC(logic, dt=0.010)
        for step in path.steps:
            replay.patch(step.action)
            for _ in range(step.scans):
                replay.step()
        assert replay.state.tags["Target"] is True

    def test_how_replay_with_demoted_fall(self):
        """how() path through fall() on a demoted tag must replay correctly."""
        from pyrung.core.runner import PLC

        button = Bool("Button", external=True)
        sensor = Bool("Sensor")
        target = Bool("Target")
        with Program(strict=False) as logic:
            with Rung(button):
                out(sensor)
            with Rung(fall(sensor)):
                latch(target)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Button": True})
        plc.step()
        assert plc.state.tags["Sensor"] is True

        path = plc.how(target)
        assert path.reachable

        replay = PLC(logic, dt=0.010)
        replay.patch({"Button": True})
        replay.step()
        for step in path.steps:
            replay.patch(step.action)
            for _ in range(step.scans):
                replay.step()
        assert replay.state.tags["Target"] is True

    def test_counterexample_trace_carries_prev(self):
        """Counterexample traces for demoted tags include prev in TraceStep."""
        button = Bool("Button", external=True)
        sensor = Bool("Sensor")
        target = Bool("Target")
        with Program(strict=False) as logic:
            with Rung(button):
                out(sensor)
            with Rung(rise(sensor)):
                latch(target)

        result = always(logic, ~target, max_states=10_000, depth_budget=20)
        assert isinstance(result, Counterexample)
        has_prev = any(step.prev for step in result.trace)
        assert has_prev, "trace steps should carry demoted prev values"
        _assert_trace_replays(logic, result, "Target")
