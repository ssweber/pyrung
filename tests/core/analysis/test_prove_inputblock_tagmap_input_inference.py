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
# InputBlock / TagMap input inference
# ===================================================================


class TestInputBlockNondeterministic:
    def test_input_block_tags_classified_nondeterministic(self):
        x = InputBlock("X", TagType.BOOL, 1, 4)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(x[1]):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        sd, nd, combinational, _, _, _ = result
        assert "X1" in nd
        assert nd["X1"] == (False, True)
        assert "X1" not in sd

    def test_output_block_tags_not_nondeterministic(self):
        x = InputBlock("X", TagType.BOOL, 1, 4)
        y = OutputBlock("Y", TagType.BOOL, 1, 4)

        with Program(strict=False) as logic:
            with Rung(x[1]):
                out(y[1])

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        sd, nd, combinational, _, _, _ = result
        assert "X1" in nd
        assert "Y1" not in nd
        assert "Y1" in combinational

    def test_reachable_states_with_input_block(self):
        x = InputBlock("X", TagType.BOOL, 1, 4)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(x[1]):
                out(light)

        states = reachable_states(logic, project=["Light"])
        assert not isinstance(states, Intractable)
        assert len(states) == 2

    def test_prove_with_input_block(self):
        x = InputBlock("X", TagType.BOOL, 1, 4)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(x[1]):
                out(light)

        result = prove(logic, light)
        assert isinstance(result, Counterexample)

    def test_tagmap_stamps_external_on_input_mapped_tags(self):
        from pyrung.click import TagMap, x, y

        button = Bool("Button")
        motor = Bool("Motor")
        assert not button.external
        assert not motor.external

        TagMap({button: x[1], motor: y[1]}, include_system=False)

        assert button.external
        assert not motor.external

    def test_tagmap_stamped_tag_becomes_nondeterministic(self):
        from pyrung.click import TagMap, x

        button = Bool("Button")
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(button):
                out(light)

        TagMap({button: x[1]}, include_system=False)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _, nd, _, _, _, _ = result
        assert "Button" in nd

    def test_tagmap_stamped_reachable_states(self):
        from pyrung.click import TagMap, x

        button = Bool("Button")
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(button):
                out(light)

        TagMap({button: x[1]}, include_system=False)

        states = reachable_states(logic, project=["Light"])
        assert not isinstance(states, Intractable)
        assert len(states) == 2


class TestPointerDomainInference:
    """Pointer tags into blocks get auto-bounded from block address range."""

    def test_external_pointer_auto_bounded(self):
        """External pointer with no annotation gets domain from block bounds."""
        blk = Block("DS", TagType.INT, 1, 10)
        idx = Int("Idx", external=True)
        dest = Int("Out")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nondeterministic, *_ = result
        assert "Idx" in nondeterministic
        assert nondeterministic["Idx"] == tuple(range(0, 11))

    def test_explicit_choices_not_overridden(self):
        """Pointer with explicit choices= keeps its annotated domain."""
        blk = Block("DS", TagType.INT, 1, 10)
        idx = Int("Idx", external=True, choices={1: "first", 5: "fifth"})
        dest = Int("Out")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nondeterministic, *_ = result
        assert "Idx" in nondeterministic
        assert nondeterministic["Idx"] == (1, 5)

    def test_explicit_min_max_not_overridden(self):
        """Pointer with explicit min=/max= keeps its annotated domain."""
        blk = Block("DS", TagType.INT, 1, 50)
        idx = Int("Idx", external=True, min=1, max=5)
        dest = Int("Out")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nondeterministic, *_ = result
        assert "Idx" in nondeterministic
        assert nondeterministic["Idx"] == tuple(range(1, 6))

    def test_literal_copy_domain_not_overridden(self):
        """Pointer already inferred by literal copies keeps that tighter domain."""
        blk = Block("DS", TagType.INT, 1, 50)
        idx = Int("Idx")
        dest = Int("Out")
        sel = Bool("Sel", external=True)

        with Program(strict=False) as logic:
            with Rung(sel):
                copy(1, idx)
            with Rung(~sel):
                copy(3, idx)
            with Rung():
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nondeterministic, *_ = result
        assert "Idx" in stateful
        assert set(stateful["Idx"]) == {0, 1, 3}

    def test_wide_block_still_intractable(self):
        """Block with > 1000 addresses leaves the pointer intractable."""
        blk = Block("Big", TagType.INT, 1, 2000)
        idx = Int("Idx", external=True)
        dest = Int("Out")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert "Idx" in result.tags

    def test_internal_pointer_auto_bounded(self):
        """Internal pointer written by calc gets domain from block bounds."""
        blk = Block("DS", TagType.INT, 1, 10)
        idx = Int("Idx")
        dest = Int("Out")
        step = Bool("Step", external=True)

        with Program(strict=False) as logic:
            with Rung(step):
                calc(idx + 1, idx)
            with Rung():
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, *_ = result
        assert "Idx" in stateful
        assert stateful["Idx"] == tuple(range(0, 11))

    def test_unconditioned_pointer_not_silently_dropped(self):
        """Pointer used only in instructions (no conditions) must still
        surface as intractable — not be silently dropped because it has
        no comparison atoms."""
        blk = Block("DS", TagType.INT, 1, 2000)
        idx = Int("Idx", external=True)
        dest = Int("Out")
        flag = Bool("Flag", external=True)

        with Program(strict=False) as logic:
            with Rung(flag):
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert "Idx" in result.tags
        assert any("pointer" in h and "Idx" in h for h in result.hints)


class TestBlocklessProveKernel:
    """prove defaults to blockless compiled kernels."""

    def test_build_explore_context_uses_blockless_kernel(self):
        blk = Block("DS", TagType.INT, 1, 100)
        idx = Int("Idx", external=True, min=1, max=5)
        dest = Int("Out")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)

        context = prove_module._build_explore_context(logic)

        assert not isinstance(context, Intractable)
        assert context.compiled.blockless is True

    def test_public_compile_sites_use_blockless_kernel(self, monkeypatch):
        from pyrung.circuitpy import codegen as codegen_module

        cmd = Bool("Cmd", external=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(cmd):
                out(light)

        real_compile = codegen_module.compile_kernel
        calls: list[dict[str, object]] = []

        def _record(*args, **kwargs):
            calls.append(dict(kwargs))
            return real_compile(*args, **kwargs)

        monkeypatch.setattr(codegen_module, "compile_kernel", _record)

        assert isinstance(prove(logic, lambda _state: True), Proven)
        states = reachable_states(logic, project=["Light"])
        assert not isinstance(states, Intractable)
        assert isinstance(program_hash(logic), str)
        assert calls
        assert all(call.get("blockless") is True for call in calls)

    def test_reachable_states_still_work_for_full_pointer_domain(self):
        """Full pointer domains still prove and enumerate correctly."""
        blk = Block("DS", TagType.INT, 1, 10)
        idx = Int("Idx", external=True, min=1, max=10)
        dest = Int("Out")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)

        states = reachable_states(logic, project=["Out"])
        assert not isinstance(states, Intractable)

    def test_prove_correct_with_narrowed_block(self):
        """Prove still produces correct results with narrowed indirect blocks."""
        blk = Block("DS", TagType.INT, 1, 100)
        idx = Int("Idx", external=True, min=1, max=3)
        dest = Int("Out")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)

        result = prove(logic, dest >= 0)
        assert isinstance(result, Proven)

    def test_mixed_access_prove_correct(self):
        """Prove works with mixed static + indirect access on same block."""
        blk = Block("DS", TagType.INT, 1, 100)
        idx = Int("Idx", external=True, min=1, max=3)
        dest = Int("Out")
        dest2 = Int("Out2")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)
            with Rung():
                copy(blk[50], dest2)

        result = prove(logic, dest >= 0)
        assert isinstance(result, Proven)
