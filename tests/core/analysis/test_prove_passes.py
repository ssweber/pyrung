"""Regression coverage for prove pass manifests and toggles."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import replace

import pytest

from pyrung.core import (
    Block,
    Bool,
    Int,
    Or,
    Program,
    Rung,
    TagType,
    Timer,
    calc,
    call,
    copy,
    fill,
    latch,
    on_delay,
    out,
    return_early,
    rise,
    subroutine,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.prove import (
    Intractable,
    Proven,
    _bfs_explore,
    _build_explore_context,
    prove,
)
from pyrung.core.analysis.prove.elision import (
    _collect_forced_true_coverage,
    _ConcreteStateElider,
    _elide_scan_local_stateful_dims,
)
from pyrung.core.analysis.prove.elision.abstract import (
    _ConstEntry,
    _ScanLocalStateElider,
    _UnavailableEntry,
)
from pyrung.core.analysis.prove.inputs import _iter_input_assignments
from pyrung.core.analysis.prove.kernel import (
    _EdgeCompressor,
    _restore_kernel,
    _seed_synthetic_presets,
    _snapshot_kernel,
    _step_kernel,
)
from pyrung.core.analysis.prove.passes import (
    _DEFAULT_PRE_BFS_PASSES,
    _BFSConfig,
    _pass_build_graph,
    _pass_diagnose_unwritten_tags,
    _PassContext,
    _run_pre_bfs_pipeline,
    _validate_pass_dag,
)

events_module = importlib.import_module("pyrung.core.analysis.prove.events")
elision_module = importlib.import_module("pyrung.core.analysis.prove.elision")
concrete_elision_module = importlib.import_module("pyrung.core.analysis.prove.elision.concrete")


def _make_pass_context(
    program: Program,
    *,
    scope: list[str] | None = None,
    project: tuple[str, ...] | None = None,
    progress_info: Callable[[str], None] | None = None,
) -> _PassContext:
    return _PassContext(
        program=program,
        scope=scope,
        project=project,
        extra_exprs=None,
        dt=0.010,
        compiled=None,
        progress_info=progress_info,
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


def _chained_settle_pending_program() -> Program:
    cmd = Bool("Cmd", external=True)
    fb = Bool("Fb", external=True)
    stage1 = Timer.clone("Stage1")
    stage2 = Timer.clone("Stage2")
    alarm = Bool("Alarm")

    with Program(strict=False) as logic:
        with Rung(cmd, ~fb):
            on_delay(stage1, preset=30)
        with Rung(stage1.Done):
            on_delay(stage2, preset=30)
        with Rung(stage2.Done):
            latch(alarm)

    return logic


def _literal_copy_program() -> Program:
    trig_a = Bool("TrigA", external=True)
    trig_b = Bool("TrigB", external=True)
    dest = Int("Dest")
    flag = Bool("Flag")

    with Program(strict=False) as logic:
        with Rung(trig_a):
            copy(5, dest)
        with Rung(trig_b):
            copy(10, dest)
        with Rung(dest == 5):
            out(flag)

    return logic


def _literal_fill_program() -> Program:
    enable = Bool("Enable", external=True)
    ds = Block("DS", TagType.INT, 1, 3)
    flag = Bool("Flag")

    with Program(strict=False) as logic:
        with Rung(enable):
            fill(7, ds.select(1, 3))
        with Rung(ds[1] == 7):
            out(flag)

    return logic


def _hidden_event_memo_program() -> Program:
    enable = Bool("Enable", external=True)
    exact_threshold = Int("ExactThreshold", final=True)
    hmi_threshold = Int("HmiThreshold", external=True, default=1000)
    t = Timer.clone("MemoTmr")
    exact_alarm = Bool("ExactAlarm")
    hmi_alarm = Bool("HmiAlarm")

    with Program(strict=False) as logic:
        with Rung():
            copy(500, exact_threshold)
        with Rung(enable):
            on_delay(t, preset=1000)
        with Rung(t.Acc > exact_threshold):
            out(exact_alarm)
        with Rung(t.Acc > hmi_threshold):
            out(hmi_alarm)

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


def _exclusive_input_encoder_program() -> Program:
    cmd_a = Bool("CmdA", external=True)
    cmd_b = Bool("CmdB", external=True)
    cmd_c = Bool("CmdC", external=True)
    cmd = Int("Cmd", choices={0: "None", 1: "A", 2: "B", 3: "C"})
    flag = Bool("Flag")

    with Program(strict=False) as logic:
        with Rung():
            copy(0, cmd)
        with Rung(cmd_a):
            copy(1, cmd)
        with Rung(cmd_b):
            copy(2, cmd)
        with Rung(cmd_c):
            copy(3, cmd)
        with Rung(cmd == 1):
            out(flag)

    return logic


def _nonexclusive_input_program() -> Program:
    cmd_a = Bool("CmdA", external=True)
    cmd_b = Bool("CmdB", external=True)
    cmd = Int("Cmd", choices={0: "None", 1: "A", 2: "B"})
    mirror = Bool("Mirror")
    flag = Bool("Flag")

    with Program(strict=False) as logic:
        with Rung():
            copy(0, cmd)
        with Rung(cmd_a):
            copy(1, cmd)
        with Rung(cmd_b):
            copy(2, cmd)
        with Rung(cmd_a):
            out(mirror)
        with Rung(cmd == 1):
            out(flag)

    return logic


class TestPassManifest:
    def test_default_pre_bfs_passes_manifest(self) -> None:
        assert [p.name for p in _DEFAULT_PRE_BFS_PASSES] == [
            "build_graph",
            "classify_dimensions",
            "pilot_sweep",
            "diagnose_unwritten_tags",
            "elide_scan_local_state",
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
            "exclusive_input_grouping",
            "edge_compression",
            "hidden_event_jumping",
            "pending_settlement",
        )

    def test_default_passes_have_valid_dag(self) -> None:
        _validate_pass_dag(_DEFAULT_PRE_BFS_PASSES)

    def test_reordered_passes_fail_dag_validation(self) -> None:
        swapped = (
            (
                _DEFAULT_PRE_BFS_PASSES[1],  # classify_dimensions (requires graph)
                _DEFAULT_PRE_BFS_PASSES[0],  # build_graph (provides graph)
            )
            + _DEFAULT_PRE_BFS_PASSES[2:]
        )
        with pytest.raises(ValueError, match="requires.*graph"):
            _validate_pass_dag(swapped)

    def test_disabled_provider_detected(self) -> None:
        passes = tuple(
            replace(p, enabled=False) if p.name == "build_graph" else p
            for p in _DEFAULT_PRE_BFS_PASSES
        )
        with pytest.raises(ValueError, match="requires.*graph"):
            _validate_pass_dag(passes)


class TestDiagnoseUnwrittenTagsProgressInfo:
    def test_unwritten_tags_emitted_via_progress_info(self) -> None:
        threshold = Int("Threshold")
        alarm = Bool("Alarm")
        value = Bool("Value", external=True)
        with Program(strict=False) as logic:
            with Rung(value, threshold > 0):
                out(alarm)
        messages: list[str] = []
        ctx = _make_pass_context(logic, progress_info=messages.append)
        _pass_build_graph(ctx)
        ctx.stateful_dims = {}
        ctx.nondeterministic_dims = {"Value": (False, True)}
        _pass_diagnose_unwritten_tags(ctx)
        assert any("Threshold" in m for m in messages)


class TestPassDisabling:
    def test_disable_pilot_sweep_still_allows_structural_discovery(self) -> None:
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

        assert not isinstance(disabled_result, Intractable)

    def test_disable_pilot_sweep_still_allows_literal_copy_domain_mining(self) -> None:
        disabled_passes = tuple(
            replace(p, enabled=False) if p.name == "pilot_sweep" else p
            for p in _DEFAULT_PRE_BFS_PASSES
        )

        result = _run_pre_bfs_pipeline(
            _make_pass_context(_literal_copy_program()),
            passes=disabled_passes,
        )

        assert not isinstance(result, Intractable)

    def test_disable_pilot_sweep_still_allows_literal_fill_domain_mining(self) -> None:
        disabled_passes = tuple(
            replace(p, enabled=False) if p.name == "pilot_sweep" else p
            for p in _DEFAULT_PRE_BFS_PASSES
        )

        result = _run_pre_bfs_pipeline(
            _make_pass_context(_literal_fill_program()),
            passes=disabled_passes,
        )

        assert not isinstance(result, Intractable)

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
        assert no_compression_result.states_explored >= default_result.states_explored


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

    def test_detects_exclusive_input_encoder_groups(self) -> None:
        context = _build_explore_context(
            _exclusive_input_encoder_program(),
            project=("Flag",),
        )
        assert not isinstance(context, Intractable)
        assert len(context.exclusive_input_groups) == 1
        group = context.exclusive_input_groups[0]
        assert group.target_name == "Cmd"
        assert set(group.members) == {"CmdA", "CmdB", "CmdC"}

    def test_skips_encoder_group_when_raw_input_is_observed_elsewhere(self) -> None:
        context = _build_explore_context(
            _nonexclusive_input_program(),
            project=("Flag",),
        )
        assert not isinstance(context, Intractable)
        assert context.exclusive_input_groups == ()

    def test_specialized_assignments_collapse_one_hot_encoder_family(self) -> None:
        context = _build_explore_context(
            _exclusive_input_encoder_program(),
            project=("Flag",),
        )
        assert not isinstance(context, Intractable)

        assignments = list(
            _iter_input_assignments(
                frozenset({"CmdA", "CmdB", "CmdC"}),
                context.nondeterministic_dims,
                context.exclusive_input_groups,
                context.exclusive_input_group_by_member,
            )
        )

        assert len(assignments) == 4
        assert {tuple(sorted(a)) for a in assignments} == {
            (("CmdA", False), ("CmdB", False), ("CmdC", False)),
            (("CmdA", True), ("CmdB", False), ("CmdC", False)),
            (("CmdA", False), ("CmdB", True), ("CmdC", False)),
            (("CmdA", False), ("CmdB", False), ("CmdC", True)),
        }

    def test_hidden_event_jump_memoizes_repeated_pending_plateaus(self, monkeypatch) -> None:
        context = _build_explore_context(
            _hidden_event_memo_program(),
            project=("ExactAlarm", "HmiAlarm"),
        )
        assert not isinstance(context, Intractable)

        kernel = context.compiled.create_kernel()
        _seed_synthetic_presets(context, kernel)
        edge_comp = _EdgeCompressor(context)
        cache = events_module._HiddenEventCache(context)

        snap = _snapshot_kernel(kernel)
        kernel.tags["Enable"] = True
        _step_kernel(context, kernel)
        new_key = edge_comp.state_key(kernel)
        visited = {new_key}

        calls = {"exact": 0, "abstract": 0}
        resolve_exact = events_module._resolve_nearest_exact_hidden_event
        resolve_abstract = events_module._abstract_threshold_outcomes

        def _count_exact(*args, **kwargs):
            calls["exact"] += 1
            return resolve_exact(*args, **kwargs)

        def _count_abstract(*args, **kwargs):
            calls["abstract"] += 1
            return resolve_abstract(*args, **kwargs)

        monkeypatch.setattr(events_module, "_resolve_nearest_exact_hidden_event", _count_exact)
        monkeypatch.setattr(events_module, "_abstract_threshold_outcomes", _count_abstract)

        first = events_module._maybe_jump_hidden_event(
            context,
            kernel,
            snap,
            visited,
            new_key,
            edge_comp,
            cache,
        )
        second = events_module._maybe_jump_hidden_event(
            context,
            kernel,
            snap,
            visited,
            new_key,
            edge_comp,
            cache,
        )

        assert len(first) == 2
        assert {outcome.key for outcome in first} == {outcome.key for outcome in second}
        assert calls == {"exact": 1, "abstract": 1}

    def test_hidden_event_cache_key_tracks_hidden_progress(self) -> None:
        context = _build_explore_context(_settle_pending_program())
        assert not isinstance(context, Intractable)

        kernel = context.compiled.create_kernel()
        _seed_synthetic_presets(context, kernel)
        edge_comp = _EdgeCompressor(context)
        cache = events_module._HiddenEventCache(context)

        initial_snap = _snapshot_kernel(kernel)
        kernel.tags["Cmd"] = True
        kernel.tags["Fb"] = False
        _step_kernel(context, kernel)
        first_key = edge_comp.state_key(kernel)
        first_jump_key = cache.plateau_key(context, initial_snap, kernel, first_key)

        second_before = _snapshot_kernel(kernel)
        kernel.tags["Cmd"] = True
        kernel.tags["Fb"] = False
        _step_kernel(context, kernel)
        second_key = edge_comp.state_key(kernel)
        second_jump_key = cache.plateau_key(context, second_before, kernel, second_key)

        assert first_key == second_key
        assert first_jump_key != second_jump_key

    def test_settle_pending_fully_resolves_chained_exact_timer_plateau(self) -> None:
        context = _build_explore_context(_chained_settle_pending_program())
        assert not isinstance(context, Intractable)

        kernel = context.compiled.create_kernel()
        _seed_synthetic_presets(context, kernel)
        edge_comp = _EdgeCompressor(context)

        before = _snapshot_kernel(kernel)
        kernel.tags["Cmd"] = True
        kernel.tags["Fb"] = False
        _step_kernel(context, kernel)
        first_key = edge_comp.state_key(kernel)
        assert events_module._has_pending_hidden_event(context, first_key)

        outcomes = events_module._settle_pending(
            context,
            kernel,
            before,
            edge_comp,
        )

        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome.additional_scans == 4
        assert not events_module._has_pending_hidden_event(context, outcome.key)

        _restore_kernel(kernel, outcome.snapshot)
        assert kernel.tags["Alarm"] is True


def _elide_stateful_dims(
    program: Program,
    stateful_dims: dict[str, tuple[object, ...]],
    nondeterministic_dims: dict[str, tuple[object, ...]],
) -> dict[str, tuple[object, ...]]:
    reduced, _, _ = _elide_scan_local_stateful_dims(
        program,
        build_program_graph(program),
        stateful_dims,
        nondeterministic_dims,
    )
    return reduced


class TestForcedTrueCoverage:
    def test_collect_forced_true_coverage_caps_total_seeded_combos(
        self,
        monkeypatch,
    ) -> None:
        inp = Bool("Inp", external=True)
        stored = Bool("Stored")

        with Program(strict=False) as logic:
            with Rung(inp):
                out(stored)

        graph = build_program_graph(logic)
        calls = 0
        real_step = concrete_elision_module._step_compiled_kernel

        def _count_step(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real_step(*args, **kwargs)

        monkeypatch.setattr(concrete_elision_module, "_step_compiled_kernel", _count_step)

        _collect_forced_true_coverage(
            logic,
            graph,
            {"Stored": (False, True)},
            {"Inp": (False, True)},
            combo_limit=3,
        )

        assert calls == 3

    def test_elision_compile_sites_use_blockless_kernels(self, monkeypatch) -> None:
        from pyrung.circuitpy import codegen as codegen_module

        inp = Bool("Inp", external=True)
        stored = Bool("Stored")

        with Program(strict=False) as logic:
            with Rung(inp):
                out(stored)

        graph = build_program_graph(logic)
        real_compile = codegen_module.compile_kernel
        calls: list[dict[str, object]] = []

        def _record(*args, **kwargs):
            calls.append(dict(kwargs))
            return real_compile(*args, **kwargs)

        monkeypatch.setattr(codegen_module, "compile_kernel", _record)

        _collect_forced_true_coverage(
            logic,
            graph,
            {"Stored": (False, True)},
            {"Inp": (False, True)},
            compiled=None,
            combo_limit=2,
        )
        _ConcreteStateElider(
            logic,
            graph,
            {"Stored": (False, True)},
            {"Inp": (False, True)},
            compiled=None,
        )

        assert calls
        assert all(call.get("blockless") is True for call in calls)
        assert any(call.get("force_rung_enable") is True for call in calls)

    def test_inline_step_wrapper_removed(self) -> None:
        kernel_module = importlib.import_module("pyrung.core.analysis.prove.kernel")
        assert not hasattr(kernel_module, "_compile_inline_step")


class TestScanLocalStateElision:
    def test_elision_progress_reports_candidate_checks(self) -> None:
        tmp = Bool("Tmp")
        seen = Bool("Seen")
        messages: list[str] = []

        with Program(strict=False) as logic:
            with Rung():
                copy(False, tmp)
            with Rung(tmp):
                out(seen)

        reduced, _, _ = _elide_scan_local_stateful_dims(
            logic,
            build_program_graph(logic),
            {"Tmp": (False, True), "Seen": (False, True)},
            {},
            progress=messages.append,
        )

        assert reduced == {}
        assert any("abstract phase complete" in message for message in messages)
        assert any("elision complete" in message for message in messages)

    def test_can_elide_scopes_observation_to_relevant_retained_tags(self) -> None:
        tmp = Bool("Tmp")
        seen = Bool("Seen")

        with Program(strict=False) as logic:
            with Rung():
                copy(False, tmp)
            with Rung(tmp):
                out(seen)

        stateful_dims: dict[str, tuple[object, ...]] = {
            "Tmp": (False, True),
            "Seen": (False, True),
        }
        for idx in range(18):
            stateful_dims[f"Unrelated{idx}"] = (False, True)

        elider = _ConcreteStateElider(logic, build_program_graph(logic), stateful_dims, {})

        assert elider._can_elide(
            "Tmp",
            frozenset(name for name in stateful_dims if name != "Tmp"),
        )

    def test_can_elide_still_groups_observed_retained_entry_values(self) -> None:
        tmp = Bool("Tmp")
        stored = Bool("Stored")

        with Program(strict=False) as logic:
            with Rung(tmp):
                copy(False, stored)
            with Rung():
                copy(False, tmp)

        elider = _ConcreteStateElider(
            logic,
            build_program_graph(logic),
            {"Tmp": (False, True), "Stored": (False, True)},
            {},
        )

        assert not elider._can_elide("Tmp", frozenset({"Stored"}))

    def test_elides_indirect_pointer_scratch_from_init_backed_table(self) -> None:
        init_done = Bool("InitDone")
        selector = Int("Selector", external=True, choices={1: "A", 2: "B"})
        idx = Int("Idx")
        tmp = Int("Tmp")
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

        reduced = _elide_stateful_dims(
            logic,
            {"Idx": (1, 2), "Tmp": (10, 20)},
            {"Selector": (1, 2)},
        )

        assert reduced == {}

    def test_elides_reset_before_self_read_counter(self) -> None:
        idx = Int("Idx")
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung():
                copy(0, idx)
            with Rung():
                calc(idx + 1, idx)
            with Rung(idx == 1):
                out(flag)

        reduced = _elide_stateful_dims(logic, {"Idx": (0, 1, 2)}, {})

        assert reduced == {}

    def test_elides_canonical_return_early_pulse_flag(self) -> None:
        req = Bool("Req", external=True)
        pulse = Int("Pulse", choices={0: "No", 1: "Yes"})

        @subroutine("worker", strict=False)
        def worker():
            with Rung(req):
                copy(1, pulse)
            with Rung(pulse == 1):
                copy(0, pulse)
                return_early()
            with Rung():
                copy(0, pulse)

        with Program(strict=False) as logic:
            with Rung():
                call(worker)

        reduced = _elide_stateful_dims(logic, {"Pulse": (0, 1)}, {"Req": (False, True)})

        assert reduced == {}

    def test_elides_canonical_branch_reset_flag(self) -> None:
        req_a = Bool("ReqA", external=True)
        req_b = Bool("ReqB", external=True)
        pulse = Int("Pulse", choices={0: "No", 1: "Yes"})
        seen = Int("Seen", choices={0: "No", 1: "Yes"})

        with Program(strict=False) as logic:
            with Rung():
                copy(0, seen)
            with Rung(req_a):
                copy(1, pulse)
            with Rung(req_b):
                copy(1, pulse)
            with Rung(pulse == 1):
                copy(1, seen)
                copy(0, pulse)

        reduced = _elide_stateful_dims(
            logic,
            {"Pulse": (0, 1), "Seen": (0, 1)},
            {"ReqA": (False, True), "ReqB": (False, True)},
        )

        assert reduced == {}

    def test_elides_indirect_scratch_with_unmapped_default_slot(self) -> None:
        selector = Int("Selector", external=True, choices={10: "None", 11: "Go"})
        tmp = Int("Tmp", choices={0: "None", 7: "Go"})
        outv = Int("Out", choices={0: "None", 7: "Go"})
        table = Block("Table", TagType.INT, 10, 11)

        with Program(strict=False) as logic:
            with Rung():
                copy(0, outv)
            with Rung():
                copy(7, table[11])
            with Rung():
                copy(table[selector], tmp)
            with Rung(tmp != 0):
                copy(tmp, outv)
                copy(0, tmp)

        reduced = _elide_stateful_dims(
            logic,
            {"Tmp": (0, 7), "Out": (0, 7)},
            {"Selector": (10, 11)},
        )

        assert reduced == {}

    def test_keeps_prewrite_input_memory_when_next_scan_entry_matters(self) -> None:
        inp = Bool("Inp", external=True)
        tmp = Int("Tmp", choices={0: "No", 1: "Yes"})
        stored = Int("Stored", choices={0: "No", 1: "Yes"})

        with Program(strict=False) as logic:
            with Rung(tmp == 1):
                copy(1, stored)
            with Rung():
                copy(inp, tmp)

        reduced = _elide_stateful_dims(
            logic,
            {"Tmp": (0, 1), "Stored": (0, 1)},
            {"Inp": (False, True)},
        )

        assert reduced == {"Tmp": (0, 1)}

    def test_packml_bench_drops_pointer_scratch_tags_from_state_key(self) -> None:
        from examples.packml_bench import logic as packml_logic

        context = _build_explore_context(packml_logic, project=("StateCurrent",))
        assert not isinstance(context, Intractable)

        for name in (
            "CmdValidIdx",
            "ModeConfigIdx",
            "StateMaskIdx",
            "StateJumpIdx",
            "StateJumpTarget",
            "StateEnableYes",
            "UnitModeCurrent",
        ):
            assert name not in context.stateful_dims

        for name in ("StateCurrent", "StateRequested"):
            assert name in context.stateful_dims


class TestDiagnoseUnwrittenTags:
    def test_fires_for_unwritten_tag(self) -> None:
        threshold = Int("Threshold")
        value = Int("Value", external=True, choices={0: "Lo", 1: "Hi"})
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(value > threshold):
                out(alarm)

        messages: list[str] = []
        ctx = _make_pass_context(logic, progress_info=messages.append)
        _pass_build_graph(ctx)
        ctx.stateful_dims = {}
        ctx.nondeterministic_dims = {"Value": (0, 1)}

        _pass_diagnose_unwritten_tags(ctx)

        assert any("never written" in m and "Threshold" in m for m in messages)
        assert any("external=True" in m for m in messages)
        assert any("readonly=True" in m for m in messages)

    def test_silent_when_all_tags_written(self) -> None:
        sensor = Bool("Sensor", external=True)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(sensor):
                out(alarm)

        messages: list[str] = []
        ctx = _make_pass_context(logic, progress_info=messages.append)
        _pass_build_graph(ctx)
        ctx.stateful_dims = {"Alarm": (False, True)}
        ctx.nondeterministic_dims = {"Sensor": (False, True)}

        _pass_diagnose_unwritten_tags(ctx)

        assert not any("never written" in m for m in messages)

    def test_excludes_external_and_readonly_tags(self) -> None:
        ext_input = Int("ExtInput", external=True, choices={0: "Off", 1: "On"})
        config = Int("Config", readonly=True)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(ext_input > config):
                out(alarm)

        messages: list[str] = []
        ctx = _make_pass_context(logic, progress_info=messages.append)
        _pass_build_graph(ctx)
        ctx.stateful_dims = {}
        ctx.nondeterministic_dims = {"ExtInput": (0, 1)}

        _pass_diagnose_unwritten_tags(ctx)

        assert not any("never written" in m for m in messages)

    def test_diagnostic_appears_in_full_pipeline(self) -> None:
        threshold = Int("Threshold")
        value = Bool("Value", external=True)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(value, threshold > 0):
                out(alarm)

        messages: list[str] = []
        ctx = _make_pass_context(logic)
        ctx.progress_info = messages.append

        _run_pre_bfs_pipeline(ctx)

        assert any("never written" in m and "Threshold" in m for m in messages)


def _run_abstract_elider(
    program: Program,
    stateful_dims: dict[str, tuple[object, ...]],
    nondeterministic_dims: dict[str, tuple[object, ...]],
) -> tuple[dict[str, tuple[object, ...]], dict]:
    graph = build_program_graph(program)
    elider = _ScanLocalStateElider(program, graph, stateful_dims, nondeterministic_dims)
    return elider.elide()


class TestAbstractEntrySummary:
    """Phase-aware entry summary: only constants cross scan boundaries."""

    def test_same_scan_safe_nonconstant_exports_unavailable(self):
        """A tag overwritten by a non-constant value before any read is elidable,
        but its entry summary must be unavailable (not reconstructible)."""
        inp = Bool("Inp", external=True)
        tmp = Bool("Tmp")
        seen = Bool("Seen")

        with Program(strict=False) as logic:
            with Rung():
                copy(inp, tmp)
            with Rung(tmp):
                out(seen)

        _retained, accepted = _run_abstract_elider(
            logic,
            {"Tmp": (False, True), "Seen": (False, True)},
            {"Inp": (False, True)},
        )

        assert "Tmp" in accepted
        assert isinstance(accepted["Tmp"].entry_summary, _UnavailableEntry)

    def test_latch_with_input_guard_cannot_converge(self):
        """A latch whose guard depends on ND inputs cannot converge to a constant
        in the abstract pass and must remain retained."""
        inp = Bool("Inp", external=True)
        intermediate = Bool("Intermediate")
        dependent = Bool("Dependent")

        with Program(strict=False) as logic:
            with Rung():
                copy(inp, intermediate)
            with Rung(intermediate):
                latch(dependent)
            with Rung(dependent):
                latch(dependent)

        retained, accepted = _run_abstract_elider(
            logic,
            {"Intermediate": (False, True), "Dependent": (False, True)},
            {"Inp": (False, True)},
        )

        assert "Intermediate" in accepted
        assert isinstance(accepted["Intermediate"].entry_summary, _UnavailableEntry)
        assert "Dependent" in retained

    def test_constant_fixed_point_still_elides(self):
        """A self-resetting tag that converges to a constant must still be
        accepted by the abstract pass via constant-entry convergence."""
        flag = Int("Flag", choices={0: "No", 1: "Yes"})

        with Program(strict=False) as logic:
            with Rung(flag == 1):
                copy(0, flag)

        retained, accepted = _run_abstract_elider(
            logic,
            {"Flag": (0, 1)},
            {},
        )

        assert "Flag" not in retained
        assert "Flag" in accepted
        assert isinstance(accepted["Flag"].entry_summary, _ConstEntry)
        assert accepted["Flag"].entry_summary.value == 0

    def test_nonconstant_canonical_fixed_point_rejected(self):
        """A tag whose exit is canonical but non-constant must not converge.
        The old _RETAINED_VALUE feedback path would have accepted this; the new
        constants-only rule correctly rejects it."""
        start = Bool("Start", external=True)
        mode = Bool("Mode")
        mirror = Bool("Mirror")
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung(mirror):
                latch(target)
            with Rung():
                copy(mode, mirror)
            with Rung(start):
                latch(mode)

        retained, accepted = _run_abstract_elider(
            logic,
            {"Mode": (False, True), "Mirror": (False, True), "Target": (False, True)},
            {"Start": (False, True)},
        )

        assert "Mirror" in retained

    def test_accepted_chain_const_then_nonconstant(self):
        """Tag A is const-elidable, tag B reads A and produces non-constant exit.
        B must get unavailable entry summary."""
        inp = Bool("Inp", external=True)
        flag = Bool("Flag")
        combo = Bool("Combo")

        with Program(strict=False) as logic:
            with Rung():
                copy(False, flag)
            with Rung():
                copy(inp, combo)

        retained, accepted = _run_abstract_elider(
            logic,
            {"Flag": (False, True), "Combo": (False, True)},
            {"Inp": (False, True)},
        )

        assert "Flag" in accepted
        assert isinstance(accepted["Flag"].entry_summary, _ConstEntry)
        assert "Combo" in accepted
        assert isinstance(accepted["Combo"].entry_summary, _UnavailableEntry)


class TestExplanation:
    def test_explain_false_returns_none(self):
        button = Bool("Button", external=True)
        light = Bool("Light")
        with Program() as logic:
            with Rung(button):
                out(light)

        result = prove(logic, Or(light, ~button))
        assert isinstance(result, Proven)
        assert result.explanation is None

    def test_explain_classifications(self):
        button = Bool("Button", external=True)
        light = Bool("Light")
        with Program() as logic:
            with Rung(button):
                out(light)

        result = prove(logic, Or(light, ~button), explain=True)
        assert isinstance(result, Proven)
        expl = result.explanation
        assert expl is not None
        button_entry = expl["Button"]
        assert button_entry.outcome.startswith("nondeterministic")
        assert any(
            d.kind == "classification" and d.outcome == "nondeterministic"
            for d in button_entry.decisions
        )

    def test_explain_domain_sources_bool(self):
        button = Bool("Button", external=True)
        light = Bool("Light")
        with Program() as logic:
            with Rung(button):
                out(light)

        result = prove(logic, Or(light, ~button), explain=True)
        assert isinstance(result, Proven)
        expl = result.explanation
        assert expl is not None
        assert expl["Button"].domain == (False, True)
        assert expl["Button"].domain_source == "bool"

    def test_explain_domain_sources_choices(self):
        mode = Int("Mode", external=True, choices={0: "Off", 1: "Auto", 2: "Manual"})
        out_tag = Bool("Out")
        with Program() as logic:
            with Rung(mode == 1):
                out(out_tag)

        result = prove(logic, Or(~out_tag, mode == 1), explain=True)
        assert isinstance(result, Proven)
        expl = result.explanation
        assert expl is not None
        assert expl["Mode"].domain_source == "choices"

    def test_explain_exclusion_readonly(self):
        Int("Version", readonly=True, default=1)
        out_tag = Bool("Out")
        button = Bool("Button", external=True)
        with Program() as logic:
            with Rung(button):
                out(out_tag)

        result = prove(logic, Or(out_tag, ~button), explain=True)
        assert isinstance(result, Proven)
        expl = result.explanation
        assert expl is not None
        if "Version" in expl:
            assert expl["Version"].outcome == "excluded:readonly"

    def test_explain_elision(self):
        Bool("Inp", external=True)
        tmp = Bool("Tmp")
        seen = Bool("Seen")
        with Program() as logic:
            with Rung():
                copy(False, tmp)
            with Rung(tmp):
                out(seen)

        context = _build_explore_context(logic, explain=True)
        assert not isinstance(context, Intractable)
        expl = context.explanation
        assert expl is not None
        if "Tmp" in expl:
            entry = expl["Tmp"]
            has_elision = any(d.kind == "elision" for d in entry.decisions)
            if has_elision:
                assert entry.outcome.startswith("elided:")

    def test_explain_redundant_absorption(self):
        inp = Bool("Inp", external=True)
        t = Timer.clone("T")
        out_tag = Bool("Out")
        with Program() as logic:
            with Rung(inp):
                on_delay(t, 100)
            with Rung(t.Done):
                out(out_tag)

        result = prove(logic, Or(~out_tag, t.Done), explain=True)
        assert isinstance(result, Proven)
        expl = result.explanation
        assert expl is not None
        acc_entry = expl.tags.get("T.Acc")
        if acc_entry is not None:
            has_absorption = any(d.kind in ("absorption", "exclusion") for d in acc_entry.decisions)
            assert has_absorption

    def test_explain_threshold_absorption_blocked(self):

        inp = Bool("Inp", external=True)
        t = Timer.clone("T")
        out_tag = Bool("Out")
        with Program() as logic:
            with Rung(inp):
                on_delay(t, 100)
            with Rung(t.Done):
                out(out_tag)

        context = _build_explore_context(logic, explain=True)
        if isinstance(context, Intractable):
            return
        expl = context.explanation
        assert expl is not None
        for entry in expl:
            blocked = [d for d in entry.decisions if d.kind == "absorption_blocked"]
            for d in blocked:
                assert d.outcome == "blocked"
                assert d.reason

    def test_explain_input_partition(self):
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        out_tag = Bool("Out")
        with Program() as logic:
            with Rung(rise(a)):
                out(out_tag)
            with Rung(b):
                pass

        result = prove(logic, Or(out_tag, ~out_tag), explain=True)
        assert isinstance(result, Proven)
        expl = result.explanation
        assert expl is not None
        a_entry = expl["A"]
        has_partition = any(d.kind == "input_partition" for d in a_entry.decisions)
        assert has_partition

    def test_explain_skip_optimizations(self):
        inp = Bool("Inp", external=True)
        out_tag = Bool("Out")
        with Program() as logic:
            with Rung(inp):
                out(out_tag)

        result = prove(logic, Or(out_tag, ~inp), explain=True, _skip_optimizations=True)
        assert isinstance(result, Proven)
        expl = result.explanation
        assert expl is not None
        assert any("disabled" in note for note in expl.notes)

    def test_explain_notes_depth_truncation(self):
        inp = Bool("Inp", external=True)
        out_tag = Bool("Out")
        t = Timer.clone("T")
        with Program() as logic:
            with Rung(inp):
                on_delay(t, 1000)
            with Rung(t.Done):
                out(out_tag)

        result = prove(logic, Or(~out_tag, t.Done), depth_budget=2, explain=True)
        if isinstance(result, Proven) and result.explanation is not None:
            if result.explanation.notes:
                assert any("depth_budget" in note for note in result.explanation.notes)
