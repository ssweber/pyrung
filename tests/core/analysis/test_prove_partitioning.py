"""Tests for the exhaustive verifier and state-space snapshot."""

from __future__ import annotations

import importlib
from pathlib import Path

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
    calc,
    copy,
    latch,
    out,
)
from pyrung.core.analysis.prove import (
    Counterexample,
    Intractable,
    Proven,
    TraceStep,
    check_lock,
    program_hash,
    prove,
    reachable_states,
    write_lock,
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


class TestBatchPartitioning:
    """Auto-partition independent batch properties into separate BFS passes."""

    def test_batch_partitions_independent_subsystems(self):
        """Two independent subsystems are proved separately."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)

        results = prove(logic, [~x, ~y])
        assert len(results) == 2
        assert isinstance(results[0], Counterexample)
        assert isinstance(results[1], Counterexample)

    def test_batch_overlapping_properties_share_bfs(self):
        """Properties referencing the same tags are grouped together."""
        button = Bool("Button", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(button):
                latch(flag)

        results = prove(logic, [~flag, Or(flag, ~flag)])
        assert len(results) == 2
        assert isinstance(results[0], Counterexample)
        assert isinstance(results[1], Proven)

    def test_batch_lambda_falls_back_to_full_scope(self):
        """Lambda properties use full scope."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)

        results = prove(logic, [lambda s: not s.get("X"), ~y])
        assert len(results) == 2
        assert isinstance(results[0], Counterexample)
        assert isinstance(results[1], Counterexample)

    def test_batch_partition_preserves_result_order(self):
        """Results are returned in original property order, not partition order."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        c = Bool("C", external=True)
        x = Bool("X")
        y = Bool("Y")
        z = Bool("Z")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)
            with Rung(c):
                latch(z)

        results = prove(logic, [~x, ~y, ~z])
        assert isinstance(results, list)
        assert len(results) == 3
        assert all(isinstance(r, Counterexample) for r in results)

    def test_batch_single_group_degenerates_to_current(self):
        """All-overlapping properties produce one group."""
        button = Bool("Button", external=True)
        flag = Bool("Flag")
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung(button):
                latch(flag)
            with Rung(flag):
                out(output)

        results = prove(logic, [~flag, ~output])
        assert len(results) == 2
        assert isinstance(results[0], Counterexample)
        assert isinstance(results[1], Counterexample)


class TestReachablePartitioning:
    """Reachable-state partitioning: independent clusters explored separately."""

    def test_independent_ote_outputs_partitioned(self):
        """Two OTE outputs with separate inputs are explored independently."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                out(x)
            with Rung(b):
                out(y)

        states = reachable_states(logic, project=["X", "Y"])
        assert not isinstance(states, Intractable)
        assert states == frozenset(
            {
                frozenset({("X", False), ("Y", False)}),
                frozenset({("X", False), ("Y", True)}),
                frozenset({("X", True), ("Y", False)}),
                frozenset({("X", True), ("Y", True)}),
            }
        )

    def test_independent_latches_partitioned(self):
        """Two independent latch subsystems produce Cartesian product."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)

        states = reachable_states(logic, project=["X", "Y"])
        assert not isinstance(states, Intractable)
        assert len(states) == 4
        x_vals = {dict(s)["X"] for s in states}
        y_vals = {dict(s)["Y"] for s in states}
        assert x_vals == {True, False}
        assert y_vals == {True, False}

    def test_coupled_tags_stay_together(self):
        """Tags sharing upstream input are not falsely split."""
        btn = Bool("Btn", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(btn):
                out(x)
                out(y)

        states = reachable_states(logic, project=["X", "Y"])
        assert not isinstance(states, Intractable)
        assert states == frozenset(
            {
                frozenset({("X", False), ("Y", False)}),
                frozenset({("X", True), ("Y", True)}),
            }
        )

    def test_three_cluster_partition(self):
        """Three independent subsystems each explored separately."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        c = Bool("C", external=True)
        x = Bool("X")
        y = Bool("Y")
        z = Bool("Z")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)
            with Rung(c):
                latch(z)

        states = reachable_states(logic, project=["X", "Y", "Z"])
        assert not isinstance(states, Intractable)
        assert len(states) == 8

    def test_explicit_scope_disables_partitioning(self):
        """Explicit scope= bypasses automatic partitioning."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                out(x)
            with Rung(b):
                out(y)

        states = reachable_states(logic, scope=["X", "Y"], project=["X", "Y"])
        assert not isinstance(states, Intractable)
        assert len(states) == 4

    def test_simplified_form_refines_partition(self):
        """Pivot resolution via simplified forms enables finer splitting.

        X and Y share a pivot tag P in the PDG, but P is OTE-resolvable
        and resolves to different inputs — so simplified forms show they
        are truly independent.
        """
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        p = Bool("P")
        q = Bool("Q")
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                out(p)
            with Rung(b):
                out(q)
            with Rung(p):
                out(x)
            with Rung(q):
                out(y)

        states = reachable_states(logic, project=["X", "Y"])
        assert not isinstance(states, Intractable)
        assert states == frozenset(
            {
                frozenset({("X", False), ("Y", False)}),
                frozenset({("X", False), ("Y", True)}),
                frozenset({("X", True), ("Y", False)}),
                frozenset({("X", True), ("Y", True)}),
            }
        )

    def test_lock_roundtrip_with_partitioned_states(self, tmp_path: Path):
        """Lock write/check works with partitioned reachable-state computation."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)

        proj = ["X", "Y"]
        states = reachable_states(logic, project=proj)
        assert not isinstance(states, Intractable)

        lock_path = tmp_path / "pyrung.lock"
        write_lock(lock_path, states, proj, program_hash(logic))

        diff = check_lock(logic, lock_path)
        assert diff is None


class TestReachableStateSlicing:
    """Whole-rung sliced kernels preserve reachable-state behavior."""

    def test_explicit_scope_slice_seeds_scope_and_projection(self):
        """Explicit scope= keeps projected writers even when they sit outside scope."""
        a = Bool("A", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(x):
                out(y)

        states = reachable_states(logic, scope=["X"], project=["Y"])
        assert not isinstance(states, Intractable)
        assert states == frozenset(
            {
                frozenset({("Y", False)}),
                frozenset({("Y", True)}),
            }
        )

    def test_reachable_states_tracks_continued_snapshot_across_scans(self):
        """continued() readers make their source tag part of the reachable state."""
        a = Bool("A", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                out(x)
            with Rung(x).continued():
                out(y)

        states = reachable_states(logic, project=["Y"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert states == frozenset(
            {
                frozenset({("Y", False)}),
                frozenset({("Y", True)}),
            }
        )

    def test_reachable_states_subroutine_slice_keeps_callers(self):
        """Selecting a subroutine writer also keeps the caller path that invokes it."""
        from pyrung.core import call, subroutine

        go = Bool("Go", external=True)
        step = Int("Step", external=True, choices={0: "Idle", 1: "Run"})
        active = Bool("Active")

        @subroutine("Worker", strict=False)
        def worker():
            with Rung(step == 1):
                out(active)

        with Program(strict=False) as logic:
            with Rung(go):
                call(worker)

        states = reachable_states(logic, project=["Active"])
        assert not isinstance(states, Intractable)
        assert states == frozenset(
            {
                frozenset({("Active", False)}),
                frozenset({("Active", True)}),
            }
        )

    def test_reachable_states_fault_projection_keeps_implicit_fault_writers(self):
        """Implicit fault-tag writers stay in the slice when a projection depends on them."""
        from pyrung.core import system

        enable = Bool("Enable", external=True)
        divisor = Int("Divisor", external=True, min=0, max=1)
        result = Int("Result")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                calc(100 / divisor, result)
            with Rung(system.fault.division_error):
                out(alarm)

        states = reachable_states(logic, project=["Alarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert states == frozenset(
            {
                frozenset({("Alarm", False)}),
                frozenset({("Alarm", True)}),
            }
        )

    def test_reachable_context_keeps_init_backed_indirect_lookup_writers(self):
        """Reachable-state contexts retain init-backed lookup writers for elision."""
        init_done = Bool("InitDone")
        selector = Int("Selector", external=True, choices={1: "A", 2: "B"})
        idx = Int("Idx")
        tmp = Int("Tmp")
        outv = Int("OutV")
        table = Block("Table", TagType.INT, 1, 2)

        with Program(strict=False) as logic:
            with Rung(~init_done):
                copy(10, table[1])
                copy(20, table[2])
                copy(1, init_done)
            with Rung():
                calc(selector, idx)
            with Rung():
                copy(table[idx], tmp)
            with Rung():
                copy(tmp, outv)

        context = prove_module._build_reachable_context(
            logic,
            scope=["OutV"],
            project=("OutV",),
        )
        assert not isinstance(context, Intractable)
        assert "Idx" not in context.stateful_dims
        assert "Tmp" not in context.stateful_dims
