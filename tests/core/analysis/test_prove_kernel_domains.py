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



class TestKernelDomainDiscovery:
    """Pilot sweep discovers finite domains for program-derived tags."""

    def test_copy_choices_inherits_domain(self):
        """copy(Step, StoredStep) where Step has choices= becomes tractable."""
        Step = Int("Step", external=True, choices={0: "Idle", 1: "Fill", 2: "Dump"})
        StoredStep = Int("StoredStep")
        DumpMode = Bool("DumpMode")

        with Program(strict=False) as logic:
            with Rung():
                copy(Step, StoredStep)
            with Rung(StoredStep == 2):
                out(DumpMode)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Proven)

    def test_calc_identity_inherits_domain(self):
        """calc(Step, StoredStep) identity-style assignment becomes tractable."""
        Step = Int("Step", external=True, choices={0: "Off", 1: "On"})
        StoredStep = Int("StoredStep")
        Active = Bool("Active")

        with Program(strict=False) as logic:
            with Rung():
                calc(Step, StoredStep)
            with Rung(StoredStep == 1):
                out(Active)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Proven)

    def test_large_comparison_only_calc_tag_is_absorbed(self):
        """Large written comparison-only tags use vector keying instead of exact state."""
        source = Int("Source", external=True, min=0, max=300)
        stored = Int("Stored")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung():
                calc(source, stored)
            with Rung(stored > 150):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert "Stored" not in stateful

        states = reachable_states(logic, project=["Alarm"], depth_budget=2)
        assert not isinstance(states, Intractable)
        assert frozenset({("Alarm", False)}) in states
        assert frozenset({("Alarm", True)}) in states

    def test_timer_preset_source_excluded_from_comparison_only(self):
        """Tags used as timer preset sources are excluded from comparison-only absorption."""
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.analysis.prove.absorb import (
            _find_comparison_absorptions,
        )
        from pyrung.core.analysis.prove.classify import (
            _collect_all_exprs,
            _collect_structural_domains,
        )

        enable = Bool("Enable", external=True)
        h1 = Int("H1", min=0, max=100)
        h2 = Int("H2", min=0, max=100)
        t1 = Timer.clone("T1")
        t2 = Timer.clone("T2")
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(enable):
                copy(50, h1)
                copy(75, h2)
            with Rung(enable):
                on_delay(t1, preset=h1)
                on_delay(t2, preset=h2)
            with Rung(h1 > 30, h2 > 60):
                out(flag)

        graph = build_program_graph(logic)
        all_exprs = _collect_all_exprs(logic, graph)
        structural_domains = _collect_structural_domains(logic, graph, all_exprs)

        comparison = _find_comparison_absorptions(logic, graph, all_exprs, structural_domains)
        assert "H1" not in comparison.comparison_tags
        assert "H2" not in comparison.comparison_tags

    def test_self_feeding_calc_is_excluded_from_comparison_only(self):
        """calc(X + N, X) reads X via _reads — _has_forbidden_data_read excludes it."""
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.analysis.prove.absorb import (
            _find_comparison_absorptions,
        )
        from pyrung.core.analysis.prove.classify import (
            _collect_all_exprs,
            _collect_structural_domains,
        )

        boost = Bool("SelfFeedBoost", external=True)
        counter = Int("SelfFeedCounter", min=0, max=100)
        flag = Bool("SelfFeedFlag")

        with Program(strict=False) as logic:
            with Rung(boost):
                calc(counter + 2, counter)
            with Rung(~boost):
                calc(counter + 1, counter)
            with Rung(counter > 50):
                out(flag)

        graph = build_program_graph(logic)
        all_exprs = _collect_all_exprs(logic, graph)
        structural_domains = _collect_structural_domains(logic, graph, all_exprs)

        comparison = _find_comparison_absorptions(logic, graph, all_exprs, structural_domains)
        assert "SelfFeedCounter" not in comparison.comparison_tags

    def test_mixed_self_feed_and_overwrite_writers_are_excluded(self):
        """A self-feeding calc next to an overwrite copy still reads the tag — excluded."""
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.analysis.prove.absorb import (
            _find_comparison_absorptions,
        )
        from pyrung.core.analysis.prove.classify import (
            _collect_all_exprs,
            _collect_structural_domains,
        )

        boost = Bool("MixBoost", external=True)
        reset_btn = Bool("MixReset", external=True)
        counter = Int("MixCounter", min=0, max=100)
        flag = Bool("MixFlag")

        with Program(strict=False) as logic:
            with Rung(boost):
                calc(counter + 3, counter)
            with Rung(~boost):
                calc(counter + 1, counter)
            with Rung(reset_btn):
                copy(0, counter)
            with Rung(counter > 50):
                out(flag)

        graph = build_program_graph(logic)
        all_exprs = _collect_all_exprs(logic, graph)
        structural_domains = _collect_structural_domains(logic, graph, all_exprs)

        comparison = _find_comparison_absorptions(logic, graph, all_exprs, structural_domains)
        assert "MixCounter" not in comparison.comparison_tags

    def test_overwrite_only_tag_source_still_absorbs(self):
        """copy(Source, Stored) is overwrite-only — comparison-only absorption still applies."""
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.analysis.prove.absorb import (
            _find_comparison_absorptions,
        )
        from pyrung.core.analysis.prove.classify import (
            _collect_all_exprs,
            _collect_structural_domains,
        )

        source = Int("OverwriteSource", external=True, min=0, max=300)
        stored = Int("OverwriteStored")
        flag = Bool("OverwriteFlag")

        with Program(strict=False) as logic:
            with Rung():
                copy(source, stored)
            with Rung(stored > 150):
                out(flag)

        graph = build_program_graph(logic)
        all_exprs = _collect_all_exprs(logic, graph)
        structural_domains = _collect_structural_domains(logic, graph, all_exprs)

        comparison = _find_comparison_absorptions(logic, graph, all_exprs, structural_domains)
        assert "OverwriteStored" in comparison.comparison_tags

    def test_self_feeding_calc_prove_matches_unoptimized(self):
        """End-to-end soundness oracle: variable-stride self-feed prove() agrees with unoptimized.

        Locks in that the existing data-flow read check keeps comparison-only
        absorption from collapsing a self-feeding accumulator into a vector.
        """
        boost = Bool("SelfFeedProveBoost", external=True)
        counter = Int("SelfFeedProveCounter", min=0, max=30)
        alarm = Bool("SelfFeedProveAlarm")

        with Program(strict=False) as logic:
            with Rung(boost):
                calc(counter + 2, counter)
            with Rung(~boost):
                calc(counter + 1, counter)
            with Rung(counter > 20):
                out(alarm)

        optimized = prove(logic, ~alarm, max_states=100_000, depth_budget=60)
        unoptimized = prove(
            logic,
            ~alarm,
            max_states=100_000,
            depth_budget=60,
            _skip_optimizations=True,
        )

        assert isinstance(optimized, Counterexample)
        assert type(optimized) is type(unoptimized)

    def test_real_operand_side_boundary_uses_comparison_partner(self):
        """Projected REAL tags resolve operand-side comparison boundaries from the partner tag."""
        from pyrung.core import Real
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.analysis.prove.classify import (
            _classify_dimensions_from_graph,
            _collect_all_exprs,
        )

        source = Int("Source", external=True, min=0, max=300)
        stored = Real("Stored")
        limit = Real("Limit", readonly=True, default=15.0)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung():
                calc(source / 10, stored)
            with Rung(limit > stored):
                out(alarm)

        graph = build_program_graph(logic)
        all_exprs = _collect_all_exprs(logic, graph)
        result = _classify_dimensions_from_graph(
            logic,
            graph,
            all_exprs,
            project=("Stored",),
        )
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert stateful["Stored"] == (0.0, 14.0, 15.0, 16.0)

    def test_literal_writes_discover_domain(self):
        """Literal writes discover {default, 5, 10}."""
        trigger_a = Bool("TrigA", external=True)
        trigger_b = Bool("TrigB", external=True)
        dest = Int("Dest")
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(trigger_a):
                copy(5, dest)
            with Rung(trigger_b):
                copy(10, dest)
            with Rung(dest == 5):
                out(flag)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Proven)

    def test_fill_literal_writes_discover_domain(self):
        """fill(constant, block.select(...)) counts as literal writes per target."""
        enable = Bool("Enable", external=True)
        ds = Block("DS", TagType.INT, 1, 3)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(enable):
                fill(7, ds.select(1, 3))
            with Rung(ds[1] == 7):
                out(flag)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Proven)

    def test_direct_self_feed_threshold_only_progress_becomes_tractable(self):
        """Monotone self-feed is tractable when the only consumer is a threshold."""
        from pyrung.core.analysis.pdg import build_program_graph

        trigger = Bool("Trigger", external=True)
        count = Int("Count")
        threshold = Int("Threshold", external=True, default=1000)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(trigger):
                calc(count + 1, count)
            with Rung(count > threshold):
                out(flag)

        graph = build_program_graph(logic)
        assert _has_data_feedback("Count", graph)

        states = reachable_states(logic, project=["Flag"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("Flag", False)}) in states
        assert frozenset({("Flag", True)}) in states

    def test_transitive_feedback_remains_intractable(self):
        """Transitive feedback A→B→A remains intractable."""
        from pyrung.core.analysis.pdg import build_program_graph

        trigger = Bool("Trigger", external=True)
        a = Int("A")
        b = Int("B")
        threshold = Int("Threshold", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(trigger):
                calc(a + 1, b)
            with Rung(trigger):
                copy(b, a)
            with Rung(a > threshold):
                out(flag)

        graph = build_program_graph(logic)
        assert _has_data_feedback("A", graph)
        assert _has_data_feedback("B", graph)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Intractable)

    def test_condition_only_not_data_feedback(self):
        """Condition-only reference is not treated as data feedback."""
        from pyrung.core.analysis.pdg import build_program_graph

        Step = Int("Step", external=True, choices={0: "Idle", 1: "Run"})
        StoredStep = Int("StoredStep")
        Active = Bool("Active")

        with Program(strict=False) as logic:
            with Rung(StoredStep > 0):
                copy(Step, StoredStep)
            with Rung(StoredStep == 1):
                out(Active)

        graph = build_program_graph(logic)
        assert not _has_data_feedback("StoredStep", graph)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Proven)

    def test_raw_external_no_writer_remains_intractable(self):
        """Raw external tag with no writer remains intractable."""
        ext = Int("ExtVal", external=True)
        other = Int("OtherVal", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(ext > other):
                latch(flag)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Intractable)

    def test_external_final_with_writer_discoverable(self):
        """external=True, final=True with an in-ladder writer is discovered."""
        trigger = Bool("Trigger", external=True)
        ext_final = Int("ExtFinal", external=True, final=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(trigger):
                copy(42, ext_final)
            with Rung(ext_final == 42):
                out(flag)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Proven)

    def test_huge_input_product_skips_discovery(self):
        """Huge relevant input product over max_combos skips discovery."""
        from pyrung.circuitpy.codegen import compile_kernel
        from pyrung.core.analysis.pdg import build_program_graph

        inputs = [Int(f"In{i}", external=True, min=0, max=200) for i in range(5)]
        dest = Int("Dest")
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            for i, inp in enumerate(inputs):
                with Rung():
                    calc(inp + i, dest)
            with Rung(dest > 0):
                out(flag)

        graph = build_program_graph(logic)
        compiled = compile_kernel(logic)
        nd = {inp.name: tuple(range(201)) for inp in inputs}
        result = _pilot_sweep_domains(
            compiled,
            ["Dest"],
            nd,
            graph,
            max_combos=100_000,
        )
        assert "Dest" not in result

    def test_copy_from_min_max_source_inherits_domain(self):
        """copy(Source, Dest) where Source has min=/max= but no comparison atoms."""
        Source = Int("Source", external=True, min=0, max=5)
        Stored = Int("Stored")
        Other = Int("Other", external=True, default=5)
        Flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung():
                copy(Source, Stored)
            with Rung(Stored > Other):
                out(Flag)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Proven)

    def test_copy_with_reset_from_bounded_source(self):
        """copy(CurStep, StoredStep) plus copy(0, StoredStep) reset path."""
        CurStep = Int("CurStep", external=True, min=0, max=3)
        StoredStep = Int("StoredStep")
        ResetBtn = Bool("ResetBtn", external=True)
        Active = Bool("Active")

        with Program(strict=False) as logic:
            with Rung():
                copy(CurStep, StoredStep)
            with Rung(ResetBtn):
                copy(0, StoredStep)
            with Rung(StoredStep == 2):
                out(Active)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Proven)

    def test_subroutine_gated_tag_discovered_via_multiscan(self):
        """Tag written inside a subroutine behind a call gate needs multi-scan."""
        from pyrung.core import call, subroutine

        xCall = Bool("xCall", external=True)
        running = Bool("Running")
        Step = Int("Step", external=True, choices={0: "Idle", 1: "Run", 2: "Done"})
        StoredStep = Int("StoredStep")
        Active = Bool("Active")

        @subroutine("Worker", strict=False)
        def worker():
            with Rung():
                copy(Step, StoredStep)
            with Rung(StoredStep == 1):
                out(Active)

        with Program(strict=False) as logic:
            with Rung(xCall):
                out(running)
            with Rung(running):
                call(worker)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Proven)
