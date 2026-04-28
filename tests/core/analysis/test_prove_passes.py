"""Regression coverage for prove pass manifests and toggles."""

from __future__ import annotations

from dataclasses import replace

from pyrung.core import Bool, Int, Program, Rung, Timer, copy, latch, on_delay, out, rise
from pyrung.core.analysis.prove import Intractable, Proven, _bfs_explore, _build_explore_context
from pyrung.core.analysis.prove.passes import (
    _DEFAULT_PRE_BFS_PASSES,
    _BFSConfig,
    _pass_build_graph,
    _PassContext,
    _run_pre_bfs_pipeline,
)


def _make_pass_context(
    program: Program,
    *,
    scope: list[str] | None = None,
    project: tuple[str, ...] | None = None,
) -> _PassContext:
    return _PassContext(
        program=program,
        scope=scope,
        project=project,
        extra_exprs=None,
        dt=0.010,
        compiled=None,
    )


def _discovery_program() -> Program:
    step = Int("Step", external=True, choices={0: "Idle", 1: "Fill", 2: "Dump"})
    threshold_in = Int("ThresholdIn", external=True, choices={0: "Low", 1: "High"})
    stored_step = Int("StoredStep")
    stored_threshold = Int("StoredThreshold")
    dump_mode = Bool("DumpMode")

    with Program(strict=False) as logic:
        with Rung():
            copy(step, stored_step)
        with Rung():
            copy(threshold_in, stored_threshold)
        with Rung(stored_step > stored_threshold):
            out(dump_mode)

    return logic


def _settle_pending_program() -> Program:
    cmd = Bool("Cmd", external=True)
    fb = Bool("Fb", external=True)
    fault_done = Timer.clone("Fault")
    alarm = Bool("Alarm")

    with Program(strict=False) as logic:
        with Rung(cmd, ~fb):
            on_delay(fault_done, 3000)
        with Rung(fault_done.Done):
            latch(alarm)

    return logic


def _edge_masked_rise_program() -> Program:
    trigger = Bool("Trigger", external=True)
    armed = Bool("Armed")
    alarm = Bool("Alarm")

    with Program(strict=False) as logic:
        with Rung(~armed, rise(trigger)):
            latch(armed)
        with Rung(armed):
            out(alarm)

    return logic


class TestPassManifest:
    def test_default_pre_bfs_passes_manifest(self) -> None:
        assert [p.name for p in _DEFAULT_PRE_BFS_PASSES] == [
            "build_graph",
            "classify_dimensions",
            "pilot_sweep",
            "compile_kernel",
            "collect_done_acc_pairs",
            "find_redundant_absorptions",
            "find_threshold_absorptions",
            "build_event_specs",
            "collect_edge_exprs",
            "discover_memory_keys",
        ]

    def test_bfs_config_manifest(self) -> None:
        assert _BFSConfig().active_optimizations == (
            "live_input_pruning",
            "edge_compression",
            "hidden_event_jumping",
            "pending_settlement",
        )


class TestPassDisabling:
    def test_disable_pilot_sweep_returns_intractable(self) -> None:
        logic = _discovery_program()

        default_result = _run_pre_bfs_pipeline(_make_pass_context(logic))
        assert not isinstance(default_result, Intractable)

        disabled_passes = tuple(
            replace(p, enabled=False) if p.name == "pilot_sweep" else p
            for p in _DEFAULT_PRE_BFS_PASSES
        )
        disabled_result = _run_pre_bfs_pipeline(
            _make_pass_context(logic),
            passes=disabled_passes,
        )

        assert isinstance(disabled_result, Intractable)
        assert disabled_result.tags == ["StoredStep", "StoredThreshold"]

    def test_disable_hidden_event_jumping_still_proves(self) -> None:
        context = _build_explore_context(_settle_pending_program())
        assert not isinstance(context, Intractable)

        result = _bfs_explore(
            context,
            predicates=[lambda s: (not s["Cmd"]) or bool(s["Fb"]) or bool(s["Alarm"])],
            bfs_config=_BFSConfig(hidden_event_jumping=False),
        )[0]

        assert isinstance(result, Proven)

    def test_disable_edge_compression_still_proves_and_explores_more(self) -> None:
        context = _build_explore_context(_edge_masked_rise_program())
        assert not isinstance(context, Intractable)

        default_result = _bfs_explore(
            context,
            predicates=[lambda _s: True],
        )[0]
        no_compression_result = _bfs_explore(
            context,
            predicates=[lambda _s: True],
            bfs_config=_BFSConfig(edge_compression=False),
        )[0]

        assert isinstance(default_result, Proven)
        assert isinstance(no_compression_result, Proven)
        assert no_compression_result.states_explored > default_result.states_explored


class TestIndividualPasses:
    def test_pass_build_graph_populates_context(self) -> None:
        sensor = Bool("Sensor", external=True)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(sensor):
                out(alarm)

        ctx = _make_pass_context(logic)
        _pass_build_graph(ctx)

        assert ctx.graph is not None
        assert ctx.all_exprs is not None
        assert ctx.all_exprs
