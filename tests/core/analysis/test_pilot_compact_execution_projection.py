from __future__ import annotations

import gc

import pytest

from pyrung.core import PLC, Bool, Program, Rung, branch, out, rise, system
from pyrung.core.analysis.causal._rung_writes import (
    ScanRungWriteProjection,
    compact_projection_condition_views,
    estimate_compact_projection_weight,
)
from pyrung.core.analysis.pilot import coast as coast_module
from pyrung.core.analysis.pilot.coast import CoastSession
from pyrung.core.analysis.pilot.requirements import _evaluate_run_guard
from pyrung.core.analysis.pilot.types import _PulseState
from pyrung.core.condition import Condition
from pyrung.core.executor import ConditionViewCapture, ReadOccurrence, WriteOccurrence


class _MissingDefaultIsSeven(Condition):
    def evaluate(self, ctx) -> bool:
        return ctx.get_tag("CompactMissing", 7) == 7


def _guard_surface_program():
    pending = Bool("CompactPending")
    entry = Bool("CompactEntry", default=True)
    edge = Bool("CompactEdge", external=True)
    anchor_out = Bool("CompactAnchorOut")
    continued_out = Bool("CompactContinuedOut")
    branch_out = Bool("CompactBranchOut")
    false_out = Bool("CompactFalseOut")

    with Program(strict=False) as program:
        with Rung():
            out(pending)
        with Rung(
            pending,
            entry,
            system.sys.always_on,
            _MissingDefaultIsSeven(),
            rise(edge),
        ):
            out(anchor_out)
        with Rung(entry).continued():
            out(continued_out)
            with branch(pending):
                out(branch_out)
        with Rung(entry == 0):
            out(false_out)
    return program, pending, entry, edge


def _captured_projection():
    program, pending, entry, edge = _guard_surface_program()
    plc = PLC(program)
    # Seed the edge detector's previous-value memory so the selected scan has
    # an entry-origin memory read rather than a default-origin read.
    plc.step()
    plc.patch({edge.name: True})
    captures: dict[int, ConditionViewCapture] = {}
    plc._run_single_scan(
        consume_pause_request=True,
        capture_execution=True,
        capture_sink=captures.__setitem__,
    )
    capture = captures[plc.state.scan_id]
    projection = plc._projection_from_capture(
        plc.state.scan_id,
        capture,
        include_memory_reads=True,
    )
    assert isinstance(projection, ScanRungWriteProjection)
    return plc, capture, projection, pending, entry, edge


def test_generic_causal_projection_remains_tag_read_only() -> None:
    plc, _capture, compact_surface, _pending, _entry, _edge = _captured_projection()

    generic = plc._replay_rung_write_projection_at(plc.state.scan_id)

    assert generic is not None
    assert all(read.occurrence.domain == "tag" for read in generic.reads)
    assert any(read.occurrence.domain == "memory" for read in compact_surface.reads)


def test_pilot_fallback_is_fresh_compact_and_leaves_generic_capture_raw() -> None:
    program, _pending, _entry, edge = _guard_surface_program()
    plc = PLC(program)
    plc.step()
    plc.patch({edge.name: True})
    plc.step()
    scan_id = plc.state.scan_id

    raw_capture = plc._replay_capture_at(scan_id)
    assert raw_capture is not None
    generic_projection = plc._replay_rung_write_projection_at(scan_id)
    assert generic_projection is not None
    raw_view = raw_capture.runs[0].view
    raw_state = raw_view.original_state
    raw_tags = raw_view._tags
    raw_memory = raw_view._memory
    raw_resolver = raw_view._resolver
    cached_items = tuple(plc._cached_replay_captures.items())

    full_surface = plc._projection_from_capture(
        scan_id,
        raw_capture,
        include_memory_reads=True,
    )
    assert isinstance(full_surface, ScanRungWriteProjection)
    expected_guards = tuple(
        _evaluate_run_guard(run, full_surface) for run in full_surface.runs if run.rung._conditions
    )

    pilot_projection = plc._replay_pilot_rung_write_projection_at(scan_id)

    assert isinstance(pilot_projection, ScanRungWriteProjection)
    assert tuple(plc._cached_replay_captures.items()) == cached_items
    assert plc._replay_capture_at(scan_id) is raw_capture
    assert plc._replay_rung_write_projection_at(scan_id) is generic_projection
    assert raw_view.original_state is raw_state
    assert raw_view._tags is raw_tags
    assert raw_view._memory is raw_memory
    assert raw_view._resolver is raw_resolver
    assert hasattr(raw_state, "tags")
    assert hasattr(raw_state, "memory")

    actual_guards = tuple(
        _evaluate_run_guard(run, pilot_projection)
        for run in pilot_projection.runs
        if run.rung._conditions
    )
    assert actual_guards == expected_guards

    def source_shape(source):
        if isinstance(source, WriteOccurrence):
            return (
                source.ordinal,
                source.domain,
                source.name,
                source.before,
                source.after,
            )
        return source

    def read_shapes(projection):
        return tuple(
            (
                read.ordinal,
                read.run_order,
                read.call_invocation,
                read.rung_id,
                read.instruction is None,
                read.occurrence.domain,
                read.occurrence.name,
                read.occurrence.value,
                source_shape(read.occurrence.source),
            )
            for read in projection.reads
        )

    assert read_shapes(pilot_projection) == read_shapes(full_surface)
    assert any(read.occurrence.domain == "memory" for read in pilot_projection.reads)

    pilot_views = {id(run.view): run.view for run in pilot_projection.runs}.values()
    raw_views = {id(run.view): run.view for run in raw_capture.runs}.values()
    assert {id(view) for view in pilot_views}.isdisjoint(id(view) for view in raw_views)
    for view in pilot_views:
        assert not hasattr(view.original_state, "tags")
        assert not hasattr(view.original_state, "memory")
        assert view.original_state is not raw_state
        assert view._tags is not raw_tags
        assert view._memory is not raw_memory
        assert view._resolver is not raw_resolver
        referents = gc.get_referents(view)
        assert raw_state not in referents
        assert raw_tags not in referents
        assert raw_memory not in referents

    weight = estimate_compact_projection_weight(pilot_projection)
    assert weight.owned_bytes > 0
    assert weight.views == len({id(run.view) for run in pilot_projection.runs})
    assert weight.view_entries >= 1
    assert raw_capture not in gc.get_referents(pilot_projection)


def test_compact_views_preserve_guard_cursor_and_guard_derivation_exactly() -> None:
    _plc, capture, projection, pending, entry, edge = _captured_projection()
    selected_runs = tuple(run for run in projection.runs if run.rung._conditions)
    before = tuple(_evaluate_run_guard(run, projection) for run in selected_runs)

    anchor = next(run for run in projection.runs if len(run.rung._conditions) == 5)
    continued = next(
        run
        for run in projection.runs
        if run.kind == "rung" and run is not anchor and run.rung._use_prior_snapshot
    )
    branch_run = next(run for run in projection.runs if run.kind == "branch")
    assert anchor.view is continued.view is branch_run.view

    guard_reads = tuple(
        read
        for read in projection.reads
        if read.instruction is None and read.run.view is anchor.view
    )
    pending_read = next(read for read in guard_reads if read.occurrence.name == pending.name)
    entry_read = next(read for read in guard_reads if read.occurrence.name == entry.name)
    resolved_read = next(
        read for read in guard_reads if read.occurrence.name == system.sys.always_on.name
    )
    default_read = next(read for read in guard_reads if read.occurrence.name == "CompactMissing")
    memory_read = next(
        read
        for read in guard_reads
        if read.occurrence.domain == "memory" and read.occurrence.name.endswith(edge.name)
    )
    assert isinstance(pending_read.occurrence.source, WriteOccurrence)
    assert entry_read.occurrence.source == "entry"
    assert resolved_read.occurrence.source == "resolved"
    assert default_read.occurrence.source == "default"
    assert memory_read.occurrence.source == "entry"

    shared_view = anchor.view
    original_state = shared_view._state
    original_tags = shared_view._tags
    original_memory = shared_view._memory
    original_resolver = shared_view._resolver
    scan_id = shared_view.scan_id
    timestamp = shared_view.timestamp
    scope_token = shared_view.scope_token
    assert projection.entry_tags is capture.entry_tags
    assert projection.exit_tags is capture.exit_tags

    compact_projection_condition_views(projection)

    after = tuple(_evaluate_run_guard(run, projection) for run in selected_runs)
    assert after == before
    assert any(not result.value and result.requirement is not None for result in after)
    assert shared_view.scan_id == scan_id
    assert shared_view.timestamp == timestamp
    assert shared_view.scope_token is scope_token
    assert shared_view._state is not original_state
    assert shared_view._tags is not original_tags
    assert shared_view._memory is not original_memory
    assert shared_view._resolver is not original_resolver
    expected_entry_tags = {
        read.occurrence.name
        for read in guard_reads
        if read.occurrence.domain == "tag" and read.occurrence.source == "entry"
    }
    assert set(shared_view._tags) == expected_entry_tags
    assert shared_view._memory == {memory_read.occurrence.name: memory_read.occurrence.value}
    assert shared_view._tags_snapshot[pending.name] == pending_read.occurrence.value
    assert shared_view._tag_source_snapshot[pending.name] is pending_read.occurrence.source
    assert "CompactMissing" not in shared_view._tags
    assert "CompactMissing" not in shared_view._tags_snapshot
    assert capture not in gc.get_referents(projection)


def test_session_store_contains_only_compact_projections() -> None:
    program, _pending, _entry, edge = _guard_surface_program()
    plc = PLC(program)
    plc.step()
    plc.patch({edge.name: True})
    session = CoastSession(plc, capture_execution=True)

    session.step_kernel()

    assert set(session._execution_projections) == {plc.state.scan_id}
    projection = session._execution_projections[plc.state.scan_id]
    assert isinstance(projection, ScanRungWriteProjection)
    assert not isinstance(projection, ConditionViewCapture)
    weight = estimate_compact_projection_weight(projection)
    assert session.execution_projection_bytes > weight.retained_bytes
    assert weight.owned_bytes >= 128 * len(projection.entry_tags) + 8 * 1024
    assert weight.views == len({id(run.view) for run in projection.runs})
    assert weight.views < len(projection.runs)
    assert weight.view_entries >= 1
    assert any(run.view._memory or run.view._memory_snapshot for run in projection.runs)
    for run in projection.runs:
        assert run.view.original_state is not plc.state
        assert not hasattr(run.view.original_state, "tags")
        assert not hasattr(run.view.original_state, "memory")


def test_weight_estimator_identity_deduplicates_shared_projection_graph() -> None:
    _plc, _capture, projection, _pending, _entry, _edge = _captured_projection()
    compact_projection_condition_views(projection)

    weight = estimate_compact_projection_weight(projection)

    assert weight.owned_bytes > 0
    assert weight.views == len({id(run.view) for run in projection.runs})

    def journal_occurrences(items):
        for item in items:
            if isinstance(item, ReadOccurrence | WriteOccurrence):
                yield item
                if isinstance(item, ReadOccurrence) and isinstance(
                    item.source,
                    WriteOccurrence,
                ):
                    yield item.source
            else:
                body = getattr(item, "body", None)
                if isinstance(body, tuple):
                    yield from journal_occurrences(body)

    assert weight.occurrences == len({id(item) for item in journal_occurrences(projection.runs)})


def test_duplicate_scan_projection_fails_whole_stream_closed() -> None:
    program, _pending, _entry, edge = _guard_surface_program()
    plc = PLC(program)
    plc.step()
    plc.patch({edge.name: True})
    session = CoastSession(plc, capture_execution=True)
    session.step_kernel()
    scan_id = session.kernel_scan_ids[-1]
    assert session._execution_projections

    session._retain_capture(scan_id, ConditionViewCapture())

    assert session._execution_projections == {}
    assert session.execution_projection_bytes == 0
    assert not session.capture_execution


def test_incompatible_compaction_discards_whole_stream_and_disables_capture(
    monkeypatch,
) -> None:
    program, _pending, _entry, edge = _guard_surface_program()
    plc = PLC(program)
    plc.step()
    plc.patch({edge.name: True})
    session = CoastSession(plc, capture_execution=True)
    session.step_kernel()
    assert session._execution_projections

    def _incompatible(_projection) -> None:
        raise ValueError("synthetic compact-view incompatibility")

    monkeypatch.setattr(
        "pyrung.core.analysis.causal._rung_writes.compact_projection_condition_views",
        _incompatible,
    )
    session.step_kernel()

    assert session._execution_projections == {}
    assert not session.capture_execution
    assert not session.execution_capture_overflowed
    assert session.kernel_scan_ids[-2:] == (plc.state.scan_id - 1, plc.state.scan_id)


def test_pilot_fallback_compaction_value_error_fails_closed(monkeypatch) -> None:
    program, _pending, _entry, _edge = _guard_surface_program()
    plc = PLC(program)
    plc.step()

    def _incompatible(_projection) -> None:
        raise ValueError("synthetic compact-view incompatibility")

    monkeypatch.setattr(
        "pyrung.core.analysis.causal._rung_writes.compact_projection_condition_views",
        _incompatible,
    )

    assert plc._replay_pilot_rung_write_projection_at(plc.state.scan_id) is None


def test_pilot_fallback_compaction_unexpected_error_propagates(monkeypatch) -> None:
    program, _pending, _entry, _edge = _guard_surface_program()
    plc = PLC(program)
    plc.step()

    def _unexpected(_projection) -> None:
        raise RuntimeError("synthetic implementation fault")

    monkeypatch.setattr(
        "pyrung.core.analysis.causal._rung_writes.compact_projection_condition_views",
        _unexpected,
    )

    with pytest.raises(RuntimeError, match="synthetic implementation fault"):
        plc._replay_pilot_rung_write_projection_at(plc.state.scan_id)


def test_retained_weight_overflow_discards_stream_and_replays_historically(
    monkeypatch,
) -> None:
    program, _pending, _entry, edge = _guard_surface_program()
    plc = PLC(program)
    plc.step()
    scan_before = plc.state.scan_id
    plc.patch({edge.name: True})
    session = CoastSession(plc, capture_execution=True)
    session.step_kernel()
    compact_projection = session._execution_projections[session.kernel_scan_ids[0]]
    compact_guard_results = tuple(
        _evaluate_run_guard(run, compact_projection)
        for run in compact_projection.runs
        if run.rung._conditions
    )
    first_weight = session.execution_projection_bytes
    assert first_weight > 0
    monkeypatch.setattr(
        coast_module,
        "_MAX_EXECUTION_PROJECTION_BYTES",
        first_weight + 1,
    )

    session.step_kernel()

    assert session.execution_capture_overflowed
    assert not session.capture_execution
    assert session._execution_projections == {}
    assert session.execution_projection_bytes == 0

    snap = dict(plc.state.tags)
    pulse = _PulseState(
        fork=plc,
        scan_before=scan_before,
        action_scan=session.kernel_scan_ids[0],
        action_snap=snap,
        wait_snaps=(),
        post_pulse_snap=snap,
        post_pulse_key=("post",),
        snap=snap,
        key=("landing",),
        kernel_scan_ids=session.kernel_scan_ids,
        execution_projections=session._execution_projections,
    )
    replayed: list[int] = []
    original = plc._replay_pilot_rung_write_projection_at

    def _replay(scan_id: int):
        replayed.append(scan_id)
        return original(scan_id)

    monkeypatch.setattr(plc, "_replay_pilot_rung_write_projection_at", _replay)
    replay_projection = pulse.projection_at(session.kernel_scan_ids[0])
    assert replay_projection is not None
    assert replayed == [session.kernel_scan_ids[0]]
    replay_guard_results = tuple(
        _evaluate_run_guard(run, replay_projection)
        for run in replay_projection.runs
        if run.rung._conditions
    )
    assert replay_guard_results == compact_guard_results
    assert sum(read.occurrence.domain == "memory" for read in replay_projection.reads) == sum(
        read.occurrence.domain == "memory" for read in compact_projection.reads
    )
