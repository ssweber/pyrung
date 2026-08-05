from __future__ import annotations

import gc

import pytest

from pyrung.core import PLC, Bool, Program, Rung, branch, out, rise, system
from pyrung.core.analysis.causal._rung_writes import (
    ScanRungWriteProjection,
    compact_projection_condition_views,
)
from pyrung.core.analysis.pilot.requirements import _evaluate_run_guard
from pyrung.core.condition import Condition
from pyrung.core.executor import ConditionViewCapture, WriteOccurrence


class _MissingDefaultIsSeven(Condition):
    def evaluate(self, ctx) -> bool:
        return ctx.get_tag("CompactMissing", 7) == 7


def _guard_surface_program():
    pending = Bool("CompactPending")
    entry = Bool("CompactEntry", default=True)
    edge = Bool("CompactEdge", external=True)
    with Program(strict=False) as program:
        with Rung():
            out(pending)
        with Rung(pending, entry, system.sys.always_on, _MissingDefaultIsSeven(), rise(edge)):
            out(Bool("CompactAnchorOut"))
        with Rung(entry).continued():
            out(Bool("CompactContinuedOut"))
            with branch(pending):
                out(Bool("CompactBranchOut"))
    return program, pending, entry, edge


def _captured_projection():
    program, pending, entry, edge = _guard_surface_program()
    plc = PLC(program)
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


def test_pilot_selected_scan_replay_is_compact_and_does_not_mutate_generic_cache() -> None:
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
    cached_items = tuple(plc._cached_replay_captures.items())

    expected = plc._projection_from_capture(scan_id, raw_capture, include_memory_reads=True)
    assert expected is not None
    expected_guards = tuple(
        _evaluate_run_guard(run, expected) for run in expected.runs if run.rung._conditions
    )
    selected = plc._replay_pilot_rung_write_projection_at(scan_id)

    assert selected is not None
    assert tuple(plc._cached_replay_captures.items()) == cached_items
    assert plc._replay_capture_at(scan_id) is raw_capture
    assert plc._replay_rung_write_projection_at(scan_id) is generic_projection
    assert raw_view.original_state is raw_state
    assert (
        tuple(_evaluate_run_guard(run, selected) for run in selected.runs if run.rung._conditions)
        == expected_guards
    )
    assert any(read.occurrence.domain == "memory" for read in selected.reads)
    assert all(not hasattr(run.view.original_state, "tags") for run in selected.runs)
    assert raw_capture not in gc.get_referents(selected)


def test_compact_views_preserve_guard_cursor_and_derivation() -> None:
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
    memory_read = next(read for read in guard_reads if read.occurrence.domain == "memory")
    assert isinstance(pending_read.occurrence.source, WriteOccurrence)
    assert entry_read.occurrence.source == "entry"
    assert resolved_read.occurrence.source == "resolved"
    assert default_read.occurrence.source == "default"
    assert memory_read.occurrence.name.endswith(edge.name)

    compact_projection_condition_views(projection)

    after = tuple(_evaluate_run_guard(run, projection) for run in selected_runs)
    assert after == before
    assert anchor.view._tags_snapshot[pending.name] == pending_read.occurrence.value
    assert anchor.view._memory == {memory_read.occurrence.name: memory_read.occurrence.value}
    assert "CompactMissing" not in anchor.view._tags
    assert capture not in gc.get_referents(projection)


def test_selected_scan_compaction_value_error_fails_closed(monkeypatch) -> None:
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


def test_selected_scan_compaction_unexpected_error_propagates(monkeypatch) -> None:
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
