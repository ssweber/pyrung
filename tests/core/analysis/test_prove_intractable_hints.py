"""Tests for the exhaustive verifier and state-space snapshot."""

from __future__ import annotations

import importlib
from dataclasses import replace
from pathlib import Path

import pytest

from pyrung.cli import _apply_lock_config
from pyrung.core import (
    PLC,
    Block,
    Bool,
    Counter,
    Dint,
    InputBlock,
    Int,
    Or,
    OutputBlock,
    Program,
    Real,
    Rung,
    TagType,
    Timer,
    Word,
    calc,
    copy,
    count_down,
    count_up,
    fall,
    fill,
    forloop,
    latch,
    named_array,
    off_delay,
    on_delay,
    out,
    reset,
    rise,
    run_function,
)
from pyrung.core.analysis.prove import (
    PENDING,
    Counterexample,
    Intractable,
    Proven,
    StateDiff,
    TraceStep,
    _bfs_explore,
    _classify_dimensions,
    _default_projection,
    _eval_atom,
    _has_data_feedback,
    _live_inputs,
    _partial_eval,
    _pilot_sweep_domains,
    check_lock,
    diff_states,
    program_hash,
    prove,
    reachable_states,
    write_lock,
)
from pyrung.core.analysis.prove.passes import _BFSConfig
from pyrung.core.analysis.simplified import And as ExprAnd
from pyrung.core.analysis.simplified import Atom, Const
from pyrung.core.analysis.simplified import Or as ExprOr

from .conftest import no_agreement

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



class TestIntractableTags:
    """Item 3 follow-up: Intractable.tags field is populated."""

    def test_unbounded_domain_tags(self):
        """Intractable from unbounded domain carries tag names."""
        trigger = Bool("Trigger", external=True)
        result_tag = Int("Result")

        def compute() -> dict:
            return {"result": 42}

        with Program(strict=False) as logic:
            with Rung(trigger):
                run_function(compute, outs={"result": result_tag})
            with Rung(result_tag == 1):
                out(Bool("Output"))

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert "Result" in result.tags



class TestIntractableHints:
    """Intractable results carry actionable hints for the user."""

    def test_pointer_tag_hint(self):
        """Pointer into a block > 1000 elements gets a hint naming the block."""
        from pyrung.core import Block, TagType, copy

        blk = Block("Regs", TagType.INT, 1, 1500)
        idx = Int("Idx", external=True)
        dest = Int("Out")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert any("pointer" in h and "Regs" in h for h in result.hints)
        assert any("Idx" in h for h in result.hints)

    def test_wide_range_hint(self):
        """Tag with min/max range > 1000 gets a 'too wide' hint."""
        level = Int("Level", external=True, min=0, max=5000)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(level):
                out(alarm)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert any("too wide" in h and "Level" in h for h in result.hints)

    def test_no_constraint_hint(self):
        """Tag with no choices/min/max gets a generic hint."""
        val = Int("Val", external=True)
        other = Int("Other", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(val > other):
                out(flag)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert any("no domain constraint" in h for h in result.hints)

    def test_function_output_hint(self):
        """Unannotated function output gets a hint."""
        trigger = Bool("Trigger", external=True)
        result_tag = Int("Result")

        def compute() -> dict:
            return {"result": 42}

        with Program(strict=False) as logic:
            with Rung(trigger):
                run_function(compute, outs={"result": result_tag})
            with Rung(result_tag == 1):
                out(Bool("Output"))

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert any("function output" in h and "Result" in h for h in result.hints)

    def test_max_states_dimension_breakdown(self):
        """max_states exceeded carries dimension breakdown hints."""
        inputs = [Bool(f"In{i}", external=True) for i in range(20)]
        flags = [Bool(f"Flag{i}") for i in range(20)]
        output = Bool("Output")

        with Program(strict=False) as logic:
            for inp, flag in zip(inputs, flags, strict=True):
                with Rung(rise(inp)):
                    latch(flag)
            with Rung(*flags):
                out(output)

        result = prove(logic, lambda s: True, max_states=10)
        assert isinstance(result, Intractable)
        assert result.hints
        assert any("state space:" in h for h in result.hints)
        assert any("Constrain" in h for h in result.hints)

    def test_hints_suggest_choices_or_testing(self):
        """Unbounded hints suggest choices= or dt= testing."""
        val = Int("Val", external=True)
        other = Int("Other", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(val > other):
                out(flag)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert all("choices=" in h or "dt= testing" in h for h in result.hints)



class TestThresholdBlockerHints:
    """Intractable hints explain why threshold abstraction was blocked."""

    def test_literal_thresholds_are_structurally_bounded(self):
        """Literal-written thresholds no longer need readonly/final hints."""
        enable = Bool("Enable", external=True)
        pan_ts = Int("PanWatchdog_Ts")
        shaft_ts = Int("ShaftWatchdog_Ts")
        t = Timer.clone("CurStep_tmr")
        pan_alarm = Bool("PanAlarm")
        shaft_alarm = Bool("ShaftAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, pan_ts)
                copy(600, shaft_ts)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > pan_ts):
                out(pan_alarm)
            with Rung(t.Acc > shaft_ts):
                out(shaft_alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert "CurStep_tmr_Acc" not in stateful
        assert "PanWatchdog_Ts" not in stateful
        assert "ShaftWatchdog_Ts" not in stateful

    def test_literal_threshold_no_longer_suggests_readonly(self):
        """Literal-written thresholds are bounded without readonly hints."""
        enable = Bool("Enable", external=True)
        ts = Int("WatchdogTs")
        t = Timer.clone("WdTmr")
        alarm = Bool("WdAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, ts)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > ts):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert "WdTmr_Acc" not in stateful
        assert "WatchdogTs" not in stateful

    def test_shared_threshold_blocks_absorption(self):
        """One threshold shared across progress sources stays explicit."""
        enable_a = Bool("EnableA", external=True)
        enable_b = Bool("EnableB", external=True)
        shared_ts = Int("SharedWatchdogTs")
        t_a = Timer.clone("SharedA")
        t_b = Timer.clone("SharedB")
        alarm_a = Bool("SharedAlarmA")
        alarm_b = Bool("SharedAlarmB")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, shared_ts)
            with Rung(enable_a):
                on_delay(t_a, preset=1000)
            with Rung(enable_b):
                on_delay(t_b, preset=1000)
            with Rung(t_a.Acc > shared_ts):
                out(alarm_a)
            with Rung(t_b.Acc > shared_ts):
                out(alarm_b)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, done_acc, _done_presets, _done_kinds = result
        assert "SharedWatchdogTs" in stateful
        assert "SharedA_Acc" in stateful
        assert "SharedB_Acc" in stateful
        assert done_acc == {}

    def test_threshold_tag_non_threshold_comparison_blocks_absorption(self):
        """Threshold tags used in other comparisons stay explicit."""
        enable = Bool("Enable", external=True)
        ts = Int("ComparedThreshold")
        t = Timer.clone("ComparedTmr")
        alarm = Bool("ComparedAlarm")
        mode = Bool("ThresholdMode")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, ts)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > ts):
                out(alarm)
            with Rung(ts == 500):
                out(mode)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, done_acc, _done_presets, _done_kinds = result
        assert "ComparedThreshold" in stateful
        assert "ComparedTmr_Acc" in stateful
        assert done_acc == {}

    def test_data_read_blocker_falls_back_to_structural_bounding(self):
        """Data-flow reads can block absorption without forcing Intractable."""
        enable = Bool("Enable", external=True)
        threshold = Int("DataReadThreshold", final=True)
        t = Timer.clone("DataReadTmr")
        saved = Int("SavedValue")
        alarm = Bool("DataReadAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, threshold)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung():
                copy(t.Acc, saved)
            with Rung(t.Acc > threshold):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, done_acc, _done_presets, _done_kinds = result
        assert "DataReadTmr_Acc" in stateful
        assert "DataReadThreshold" in stateful
        assert done_acc == {}

    def test_stable_threshold_no_blocker(self):
        """Stable thresholds produce no blocker — absorption succeeds."""
        enable = Bool("Enable", external=True)
        ts = Int("StableTs", readonly=True)
        t = Timer.clone("StableTmr")
        alarm = Bool("StableAlarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > ts):
                out(alarm)

        result = reachable_states(logic, project=["StableAlarm"], depth_budget=5)
        assert not isinstance(result, Intractable)
