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


# ===================================================================
# Group 1: Dimension classification
# ===================================================================


class TestDimensionClassification:
    def test_ote_only_all_combinational(self):
        """Program with only OTE writes — all tags combinational, zero state dims."""
        input_a = Bool("InputA", external=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(input_a):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, combinational, _done_acc, _done_presets, _done_kinds = result
        assert "Light" not in stateful
        assert "Light" in combinational
        assert "InputA" in nd

    def test_latch_reset_are_stateful(self):
        """Latch/reset writes make tags stateful when referenced."""
        button = Bool("Button", external=True)
        stop = Bool("Stop", external=True)
        running = Bool("Running")
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(button):
                latch(running)
            with Rung(stop):
                reset(running)
            with Rung(running):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, combinational, _done_acc, _done_presets, _done_kinds = result
        assert "Running" in stateful
        assert stateful["Running"] == (False, True)
        assert "Button" in nd
        assert "Stop" in nd

    def test_unreferenced_bool_excluded(self):
        """Written Bool not referenced in any expression is excluded."""
        button = Bool("Button", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(button):
                latch(flag)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, combinational, _done_acc, _done_presets, _done_kinds = result
        assert "Flag" not in stateful
        assert "Flag" in combinational

    def test_write_only_terminal_is_combinational(self):
        """Tag written by copy but never read is combinational (dead output)."""
        sensor = Bool("Sensor", external=True)
        level = Int("Level", external=True, min=0, max=10)
        output = Int("Output")

        with Program(strict=False) as logic:
            with Rung(sensor):
                copy(level, output)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, combinational, _done_acc, _done_presets, _done_kinds = result
        assert "Output" not in stateful
        assert "Output" in combinational

    def test_external_tags_are_nondeterministic(self):
        """Tags with external=True are nondeterministic."""
        sensor = Bool("Sensor", external=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(sensor):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        assert "Sensor" in nd

    def test_readonly_tags_excluded(self):
        """Readonly tags are excluded from enumeration."""
        config = Bool("Config", readonly=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(config):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        assert "Config" not in nd
        assert "Config" not in stateful

    def test_timer_done_is_bool_state_dim(self):
        """Timer.Done is a Bool state dimension."""
        enable = Bool("Enable", external=True)
        t = Timer.clone("T1")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=100)
            with Rung(t.Done):
                out(Bool("Output"))

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        assert "T1_Done" in stateful
        assert stateful["T1_Done"] == (False, PENDING, True)
