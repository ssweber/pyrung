from __future__ import annotations

import pytest

from pyrung.core import (
    PLC,
    Bool,
    ForLoop,
    Int,
    Program,
    Rung,
    Timer,
    calc,
    call,
    copy,
    on_delay,
    out,
    reset,
    rise,
    subroutine,
    system,
)
from pyrung.core.context import RungId, ScanContext
from pyrung.core.executor import (
    InstructionRun,
    LoopIterationRun,
    RungRun,
    WriteOccurrence,
)
from pyrung.core.instruction import Instruction


def test_replay_capture_uses_shared_state_slab_and_restores_force_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable = Bool("Enable")
    light = Bool("Light")

    with Program(strict=False) as program:
        with Rung(enable):
            out(light)

    source = PLC(program, dt=0.01)
    source.force("Enable", True)
    source.step()
    source.patch({"Enable": False})
    source.step()

    source._recent_state_cache.clear()
    source._recent_state_cache_bytes = 0
    source._cache_state(source.current_state)

    def _boom_replay_to(_scan_id: int) -> PLC:
        raise AssertionError("capture positioning should use the shared state slab")

    monkeypatch.setattr(source, "replay_to", _boom_replay_to)

    views = source._replay_node_views_at(2)

    assert views[RungId(None, 0)].get_tag("Enable") is True


def test_sparse_replay_seek_folds_to_exact_endpoint_across_recorded_events() -> None:
    enable = Bool("Enable", external=True)
    marker = Bool("Marker", external=True)
    timer = Timer.clone("ReplayFoldTmr")
    seen = Bool("Seen")

    with Program(strict=False) as program:
        with Rung(enable):
            on_delay(timer, 5000, "ms")
        with Rung(marker):
            out(seen)

    source = PLC(program, dt=0.01, checkpoint_interval=10_000)
    source.force(enable, True)
    for scan_id in range(1, 3001):
        if scan_id == 421:
            source.patch({marker.name: True})
        source.step()

    # Force both reconstructions to start at the initial state.  Checkpoint
    # force snapshots remain in the scan log and must not become false
    # boundaries when the effective force map is unchanged.
    source._checkpoints.clear()
    kernel = source._compiled_replay_supported_kernel()
    assert kernel is not None

    expected = source._replay_to_compiled(2999, kernel).state
    actual = source._replay_seek(2999).state

    assert actual.scan_id == expected.scan_id
    assert actual.timestamp == pytest.approx(expected.timestamp)
    assert dict(actual.tags) == dict(expected.tags)
    assert dict(actual.memory) == dict(expected.memory)
    assert source._last_replay_seek_stats["logical_scans"] == 2999
    assert source._last_replay_seek_stats["folded_scans"] > 2950
    assert source._last_replay_seek_stats["kernel_scans"] < 30
    assert (
        source._last_replay_seek_stats["ordinary_folded_scans"]
        + source._last_replay_seek_stats["cycle_folded_scans"]
        + source._last_replay_seek_stats["residual_scans"]
        == source._last_replay_seek_stats["logical_scans"]
    )
    assert not source._replay_slabs

    source._recent_state_cache.clear()
    source._recent_state_cache_bytes = 0
    source._cache_state(source.current_state)
    slab_state = source._state_at(2999)

    assert dict(slab_state.tags) == dict(expected.tags)
    assert len(next(iter(source._replay_slabs.values()))) == 1600
    assert source._last_replay_slab_stats == {
        "runup_scans": 1399,
        "materialized_states": 1600,
        "folded_runup": 1,
        "ordinary_folded_scans": source._last_replay_seek_stats["ordinary_folded_scans"],
        "cycle_folded_scans": 0,
        "residual_scans": source._last_replay_seek_stats["residual_scans"] + 1600,
    }
    assert source._state_at(2998).scan_id == 2998


def test_causal_slab_spans_intermediate_checkpoints_as_one_contiguous_window() -> None:
    timer = Timer.clone("CheckpointSpanningSlabTmr")

    with Program(strict=False) as program:
        with Rung():
            on_delay(timer, 50_000, "ms")

    source = PLC(program, dt=0.01, checkpoint_interval=200)
    source.run(2200)
    source._recent_state_cache.clear()
    source._recent_state_cache_bytes = 0
    source._cache_state(source.current_state)

    state = source._state_at(2199)
    slab = next(iter(source._replay_slabs.values()))

    assert state.scan_id == 2199
    assert min(slab) == 600
    assert max(slab) == 2199
    assert len(slab) == 1600
    assert source._last_replay_slab_stats == {
        "runup_scans": 199,
        "materialized_states": 1600,
        "folded_runup": 1,
        "ordinary_folded_scans": source._last_replay_seek_stats["ordinary_folded_scans"],
        "cycle_folded_scans": 0,
        "residual_scans": source._last_replay_seek_stats["residual_scans"] + 1600,
    }
    assert source._state_at(601) is slab[601]


def test_sparse_replay_seek_preserves_clock_edge_memory() -> None:
    pulses = Int("ClockPulses")

    with Program(strict=False) as program:
        with Rung(rise(system.sys.clock_1s)):
            calc(pulses + 1, pulses)

    source = PLC(program, dt=0.01)
    source.run(333)
    source._checkpoints.clear()
    kernel = source._compiled_replay_supported_kernel()
    assert kernel is not None

    expected = source._replay_to_compiled(332, kernel).state
    actual = source._replay_seek(332).state

    assert actual.timestamp == pytest.approx(expected.timestamp)
    assert dict(actual.tags) == dict(expected.tags)
    assert dict(actual.memory) == dict(expected.memory)
    assert source._last_replay_seek_stats["folded_scans"] > 250


def test_replay_capture_reuses_source_pdg(monkeypatch: pytest.MonkeyPatch) -> None:
    enable = Bool("Enable")
    light = Bool("Light")

    with Program(strict=False) as program:
        with Rung(enable):
            out(light)

    source = PLC(program)
    source.step()
    source._ensure_pdg()

    def _unexpected_rebuild(_program: Program):
        raise AssertionError("reconstructed replay should reuse the source PDG")

    monkeypatch.setattr("pyrung.core.analysis.pdg.build_program_graph", _unexpected_rebuild)

    assert source._replay_node_views_at(1)


def test_replay_capture_does_not_commit_disposable_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable = Bool("Enable")
    light = Bool("Light")

    with Program(strict=False) as program:
        with Rung(enable):
            out(light)

    source = PLC(program)
    source.patch({"Enable": True})
    source.step()

    def _unexpected_commit(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("observed evidence is complete before commit")

    monkeypatch.setattr(PLC, "_commit_scan", _unexpected_commit)

    runs = source._replay_rung_runs_at(1)

    assert len(runs) == 1
    assert runs[0].enabled is True
    assert dict(runs[0].writes) == {"Light": True}


def test_replay_capture_preserves_repeated_subroutine_occurrences() -> None:
    source = Int("Source")
    result = Int("Result")

    @subroutine("Shared")
    def shared():
        with Rung():
            copy(source, result)

    with Program(strict=False) as program:
        with Rung():
            copy(1, source)
            call(shared)
            copy(2, source)
            call(shared)

    plc = PLC(program)
    plc.step()

    runs = [
        run
        for run in plc._replay_rung_runs_at(plc.state.scan_id)
        if run.rung_id == RungId("Shared", 0)
    ]

    assert len(runs) == 2
    assert [run.caller_rung for run in runs] == [0, 0]
    assert [run.view.get_tag(source.name) for run in runs] == [1, 2]
    assert [dict(run.writes)[result.name] for run in runs] == [1, 2]
    # The compact compatibility view intentionally remains last-occurrence.
    assert (
        plc._replay_node_views_at(plc.state.scan_id)[RungId("Shared", 0)].get_tag(source.name) == 2
    )


def test_replay_capture_orders_multiple_instructions_and_their_events() -> None:
    source = Int("Source")
    result = Int("Result")

    with Program(strict=False) as program:
        with Rung():
            copy(1, source)
            copy(source, result)

    plc = PLC(program)
    plc.step()
    run = plc._replay_rung_runs_at(plc.state.scan_id)[0]

    assert [type(item) for item in run.body] == [InstructionRun, InstructionRun]
    first, second = run.body
    assert isinstance(first, InstructionRun)
    assert isinstance(second, InstructionRun)
    first_write = first.direct_write_occurrences[0]
    second_read = second.direct_read_occurrences[0]
    second_write = second.direct_write_occurrences[0]
    assert (first_write.domain, first_write.name, first_write.before, first_write.after) == (
        "tag",
        source.name,
        0,
        1,
    )
    assert (second_read.domain, second_read.name, second_read.value) == (
        "tag",
        source.name,
        1,
    )
    assert (second_write.domain, second_write.name, second_write.before, second_write.after) == (
        "tag",
        result.name,
        0,
        1,
    )
    assert [first_write.ordinal, second_read.ordinal, second_write.ordinal] == list(
        range(first_write.ordinal, first_write.ordinal + 3)
    )
    assert second_read.source is first_write
    assert run.write_occurrences == (
        *first.direct_write_occurrences,
        *second.direct_write_occurrences,
    )
    assert run.writes == ((source.name, 1), (result.name, 1))


def test_replay_capture_freezes_entry_definition_for_continued_view() -> None:
    state = Int("State")
    observed = Bool("Observed")

    with Program(strict=False) as program:
        with Rung():
            copy(1, state)
            copy(0, state)
        with Rung(state == 0).continued():
            out(observed)

    plc = PLC(program)
    plc.step()
    first, continued = plc._replay_rung_runs_at(plc.state.scan_id)

    assert [write.after for write in first.write_occurrences if write.name == state.name] == [1, 0]
    state_read = next(read for read in continued.direct_read_occurrences if read.name == state.name)
    assert state_read.value == 0
    assert state_read.source == "entry"


def test_replay_capture_freezes_pending_definition_for_continued_view() -> None:
    state = Int("State")
    observed = Bool("Observed")

    with Program(strict=False) as program:
        with Rung():
            copy(5, state)
        with Rung():
            copy(6, state)
            copy(5, state)
        with Rung(state == 5).continued():
            out(observed)

    plc = PLC(program)
    plc.step()
    source_run, mutating_run, continued = plc._replay_rung_runs_at(plc.state.scan_id)

    source_write = next(write for write in source_run.write_occurrences if write.name == state.name)
    assert [
        write.after for write in mutating_run.write_occurrences if write.name == state.name
    ] == [
        6,
        5,
    ]
    state_read = next(read for read in continued.direct_read_occurrences if read.name == state.name)
    assert state_read.value == 5
    assert state_read.source is source_write


def test_replay_capture_links_memory_read_to_exact_memory_write() -> None:
    result = Int("Result")

    class MemoryRoundTripInstruction(Instruction):
        _writes = ("dest",)

        def __init__(self) -> None:
            self.dest = result

        def execute(self, ctx: ScanContext, enabled: bool) -> None:
            if enabled:
                ctx.set_memory("scratch", 7)
                ctx.set_tag(self.dest.name, ctx.get_memory("scratch"))

    with Program(strict=False) as program:
        with Rung():
            pass
    program.rungs[0].add_instruction(MemoryRoundTripInstruction())

    plc = PLC(program)
    plc.step()
    instruction_run = plc._replay_rung_runs_at(plc.state.scan_id)[0].body[0]

    assert isinstance(instruction_run, InstructionRun)
    memory_write = next(
        write for write in instruction_run.direct_write_occurrences if write.domain == "memory"
    )
    memory_read = next(
        read for read in instruction_run.direct_read_occurrences if read.domain == "memory"
    )
    assert memory_read.value == 7
    assert memory_read.source is memory_write


def test_replay_capture_marks_default_read_without_a_dynamic_definition() -> None:
    result = Int("Result")

    class DefaultReadInstruction(Instruction):
        _writes = ("dest",)

        def __init__(self) -> None:
            self.dest = result

        def execute(self, ctx: ScanContext, enabled: bool) -> None:
            if enabled:
                ctx.set_tag(self.dest.name, ctx.get_tag("Missing", 9))

    with Program(strict=False) as program:
        with Rung():
            pass
    program.rungs[0].add_instruction(DefaultReadInstruction())

    plc = PLC(program)
    plc.step()
    instruction_run = plc._replay_rung_runs_at(plc.state.scan_id)[0].body[0]

    assert isinstance(instruction_run, InstructionRun)
    missing_read = next(
        read for read in instruction_run.direct_read_occurrences if read.name == "Missing"
    )
    assert missing_read.value == 9
    assert missing_read.source == "default"


def test_replay_capture_nests_parent_child_parent_in_source_order() -> None:
    state = Int("State")

    @subroutine("ApplyMiddle")
    def apply_middle():
        with Rung():
            copy(2, state)

    with Program(strict=False) as program:
        with Rung():
            copy(1, state)
            call(apply_middle)
            copy(3, state)

    plc = PLC(program)
    plc.step()
    parent = plc._replay_rung_runs_at(plc.state.scan_id)[0]

    assert [type(item) for item in parent.body] == [
        InstructionRun,
        InstructionRun,
        InstructionRun,
    ]
    before, call_run, after = parent.body
    assert isinstance(before, InstructionRun)
    assert isinstance(call_run, InstructionRun)
    assert isinstance(after, InstructionRun)
    assert len(call_run.body) == 1
    child = call_run.body[0]
    assert isinstance(child, RungRun)
    assert child.kind == "subroutine"
    assert [write.after for write in before.write_occurrences] == [1]
    assert [write.after for write in child.write_occurrences] == [2]
    assert [write.after for write in after.write_occurrences] == [3]
    assert [
        (write.before, write.after)
        for write in parent.write_occurrences
        if write.name == state.name
    ] == [(0, 1), (1, 2), (2, 3)]
    assert [
        (write.before, write.after)
        for write in parent.direct_write_occurrences
        if write.name == state.name
    ] == [(0, 1), (2, 3)]
    assert parent.writes == ((state.name, 3),)
    assert parent.direct_writes == ((state.name, 3),)
    assert parent.rung_occurrences == (parent, child)


def test_replay_capture_preserves_repeated_writes_inside_one_instruction() -> None:
    state = Int("State")

    class MultiWriteInstruction(Instruction):
        _writes = ("dest",)

        def __init__(self) -> None:
            self.dest = state

        def execute(self, ctx: ScanContext, enabled: bool) -> None:
            if enabled:
                ctx.set_tag(self.dest.name, 1)
                ctx.set_tag(self.dest.name, 2)

    with Program(strict=False) as program:
        with Rung():
            pass
    program.rungs[0].add_instruction(MultiWriteInstruction())

    plc = PLC(program)
    plc.step()
    run = plc._replay_rung_runs_at(plc.state.scan_id)[0]
    instruction_run = run.body[0]

    assert isinstance(instruction_run, InstructionRun)
    first_write, second_write = instruction_run.direct_write_occurrences
    assert (first_write.domain, first_write.name, first_write.before, first_write.after) == (
        "tag",
        state.name,
        0,
        1,
    )
    assert (second_write.domain, second_write.name, second_write.before, second_write.after) == (
        "tag",
        state.name,
        1,
        2,
    )
    assert second_write.ordinal == first_write.ordinal + 1
    assert run.writes == ((state.name, 2),)


def test_replay_capture_orders_force_rung_and_post_force_writes() -> None:
    signal = Bool("Signal")

    with Program(strict=False) as program:
        with Rung():
            reset(signal)

    plc = PLC(program)
    plc.force(signal, True)
    plc.step()
    capture = plc._replay_capture_at(plc.state.scan_id)

    assert capture is not None
    root_signal_writes = [
        (index, item)
        for index, item in enumerate(capture.body)
        if isinstance(item, WriteOccurrence) and item.domain == "tag" and item.name == signal.name
    ]
    main_index, main_run = next(
        (index, item)
        for index, item in enumerate(capture.body)
        if isinstance(item, RungRun) and item.kind == "rung"
    )
    nested_signal_writes = tuple(
        write
        for write in main_run.write_occurrences
        if write.domain == "tag" and write.name == signal.name
    )

    assert len(root_signal_writes) == 2
    assert len(nested_signal_writes) == 1
    (pre_index, pre_force), (post_index, post_force) = root_signal_writes
    (rung_write,) = nested_signal_writes
    assert pre_index < main_index < post_index
    assert (pre_force.before, pre_force.after) == (False, True)
    assert (rung_write.before, rung_write.after) == (True, False)
    assert (post_force.before, post_force.after) == (False, True)
    assert pre_force.ordinal < rung_write.ordinal < post_force.ordinal


def test_replay_capture_loop_iteration_owns_index_before_child() -> None:
    result = Int("Result")

    with Program(strict=False) as program:
        with Rung():
            with ForLoop(2) as loop:
                copy(loop.idx, result)

    plc = PLC(program)
    plc.step()
    rung_run = plc._replay_rung_runs_at(plc.state.scan_id)[0]
    loop_run = rung_run.body[0]

    assert isinstance(loop_run, InstructionRun)
    assert [type(item) for item in loop_run.body] == [
        LoopIterationRun,
        LoopIterationRun,
    ]
    first, second = loop_run.body
    assert isinstance(first, LoopIterationRun)
    assert isinstance(second, LoopIterationRun)
    assert first.direct_write_occurrences[0].name == "_forloop_idx"
    assert first.direct_write_occurrences[0].after == 0
    assert isinstance(first.body[1], InstructionRun)
    assert first.body[1].direct_read_occurrences[0].value == 0
    assert second.direct_write_occurrences[0].after == 1
    assert isinstance(second.body[1], InstructionRun)
    assert second.body[1].direct_read_occurrences[0].value == 1


def test_replay_capture_retains_condition_reads_outside_instruction_reads() -> None:
    enable = Bool("Enable", external=True)
    light = Bool("Light")

    with Program(strict=False) as program:
        with Rung(enable):
            out(light)

    plc = PLC(program)
    plc.patch({enable.name: True})
    plc.step()
    capture = plc._replay_capture_at(plc.state.scan_id)

    assert capture is not None
    run = capture.runs[0]
    enable_read = run.direct_read_occurrences[0]
    enable_write = next(
        item
        for item in capture.body
        if isinstance(item, WriteOccurrence) and item.name == enable.name
    )
    assert enable_read.name == enable.name
    assert enable_read.source is enable_write
    assert run.direct_read_occurrences[0].value is True
    instruction_run = next(item for item in run.body if isinstance(item, InstructionRun))
    assert instruction_run.direct_read_occurrences == ()
    assert enable.name not in plc._replay_node_reads_at(plc.state.scan_id).get(
        RungId(None, 0), set()
    )
