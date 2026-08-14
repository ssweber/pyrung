from __future__ import annotations

import gc

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Int,
    Or,
    Program,
    Rung,
    branch,
    call,
    copy,
    out,
    reset,
    rise,
    subroutine,
    system,
)
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


def _nested_displacement_projection():
    top = Bool("CompactTopGate", default=True)
    top_skipped = Bool("CompactTopSkipped")
    middle = Bool("CompactMiddleGate", default=True)
    middle_skipped = Bool("CompactMiddleSkipped")
    leaf = Bool("CompactLeafGate", default=True)
    unrelated_data = Int("CompactUnrelatedData", default=7)
    scratch = Int("CompactScratch")
    result = Bool("CompactNestedResult")

    with Program() as program:
        with Rung():
            out(result)
        with Rung(Or(top, top_skipped)):
            call("CompactMiddle")
        with subroutine("CompactMiddle"):
            with Rung(Or(middle, middle_skipped)):
                copy(unrelated_data, scratch)
                call("CompactLeaf")
        with subroutine("CompactLeaf"):
            with Rung(leaf):
                reset(result)

    plc = PLC(program)
    plc.step()
    projection = plc._replay_pilot_rung_write_projection_at(plc.state.scan_id)
    assert projection is not None
    return (
        program,
        projection,
        result,
        top,
        top_skipped,
        middle,
        middle_skipped,
        leaf,
        unrelated_data,
    )


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


def test_write_enabling_read_closure_includes_only_its_dynamic_ancestors() -> None:
    _plc, _capture, projection, pending, entry, _edge = _captured_projection()
    branch_write = next(
        write for write in projection.writes if write.transition.tag_name == "CompactBranchOut"
    )

    direct = projection.enabling_reads_observed_by_write(branch_write)
    closure = projection.enabling_read_closure_observed_by_write(branch_write)

    assert tuple(read.occurrence.name for read in direct) == (pending.name,)
    assert tuple(read.occurrence.name for read in closure) == (entry.name, pending.name)


def test_nested_displacement_owns_exact_guards_not_skipped_or_ancestor_data() -> None:
    (
        program,
        projection,
        result,
        top,
        top_skipped,
        middle,
        middle_skipped,
        leaf,
        unrelated_data,
    ) = _nested_displacement_projection()
    observation = projection.observe_appeared_handoff(
        result.name,
        True,
        producer_rung=program.rungs[0],
        consumer_rung=None,
    )[0]

    assert observation.disposition == "OVERWRITTEN"
    assert tuple(read.occurrence.name for read in observation.observed_reads) == (leaf.name,)
    assert tuple(read.occurrence.name for read in observation.displacement_enabling_reads) == (
        top.name,
        middle.name,
        leaf.name,
    )
    assert top_skipped.name not in {
        read.occurrence.name for read in observation.displacement_enabling_reads
    }
    assert middle_skipped.name not in {
        read.occurrence.name for read in observation.displacement_enabling_reads
    }
    assert unrelated_data.name not in {
        read.occurrence.name for read in observation.displacement_enabling_reads
    }


def test_enabling_read_closure_rejects_a_write_from_another_projection() -> None:
    _program, first, *_rest = _nested_displacement_projection()
    _program, second, result, *_rest = _nested_displacement_projection()
    foreign = next(write for write in second.writes if write.transition.tag_name == result.name)

    with pytest.raises(ValueError, match="not owned"):
        first.enabling_read_closure_observed_by_write(foreign)


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
