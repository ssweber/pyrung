"""Execution-truth and factual designation tests for bootstrap scan 1."""

from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from types import MappingProxyType
from typing import Any, cast

import pytest

from pyrung import PLC, Bool, Program, Rung, latch, out
from pyrung.core.analysis.causal._rung_writes import ScanRungWriteProjection
from pyrung.core.analysis.pilot import entry_execution as entry_execution_module
from pyrung.core.analysis.pilot.api import pilot_events
from pyrung.core.analysis.pilot.bootstrap import (
    BootstrapExecutionSnapshot,
    _BootstrapExecution,
)
from pyrung.core.analysis.pilot.compass import Compass
from pyrung.core.analysis.pilot.types import PilotEvent
from pyrung.core.runner import _compile_avoid
from tests.fixtures.pilot_alarm_presets import aborted_on_first_scan as first_scan
from tests.fixtures.pilot_alarm_presets import alarmed_at_start as alarmed
from tests.fixtures.pilot_alarm_presets import conditional_negative


def _ordered_accesses(
    projection: ScanRungWriteProjection,
) -> tuple[tuple[Any, ...], ...]:
    accesses: list[tuple[Any, ...]] = [
        (
            read.ordinal,
            "read",
            read.occurrence.name,
            read.occurrence.value,
            read.rung_id.rung_index,
        )
        for read in projection.reads
    ]
    accesses.extend(
        (
            write.ordinal,
            "write",
            write.transition.tag_name,
            write.transition.from_value,
            write.transition.to_value,
            write.rung_id.rung_index,
        )
        for write in projection.writes
    )
    return tuple(sorted(accesses))


def _started_event(events: Iterator[PilotEvent]) -> PilotEvent:
    event = next(events)
    assert event.kind == "started"
    return event


def _entry_event(events: Iterator[PilotEvent]) -> PilotEvent:
    return next(event for event in events if event.kind == "entry_scan_observed")


def _snapshot_accesses(snapshot: BootstrapExecutionSnapshot) -> tuple[tuple[Any, ...], ...]:
    accesses: list[tuple[Any, ...]] = []
    for access in snapshot.ordered_accesses:
        if access.kind == "read":
            accesses.append(
                (access.ordinal, access.kind, access.tag, access.values[0], access.rung[1])
            )
        else:
            accesses.append(
                (
                    access.ordinal,
                    access.kind,
                    access.tag,
                    access.values[0],
                    access.values[1],
                    access.rung[1],
                )
            )
    return tuple(accesses)


def test_rejected_entry_observation_stops_instead_of_retrying_scan_zero() -> None:
    start = Bool("RejectedEntry_Start", external=True)
    running = Bool("RejectedEntry_Running")
    done = Bool("RejectedEntry_Done")
    with Program() as logic:
        with Rung(start):
            latch(running)
        with Rung(running):
            out(done)

    events = list(
        pilot_events(
            PLC(logic, dt=0.010),
            done,
            max_scans=5,
            avoid_pred=_compile_avoid(~start),
        )
    )

    assert [event.kind for event in events].count("bearing_coast_rejected") == 1
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is False
    assert "avoid excludes ~RejectedEntry_Start" in events[-1].data["reason"]


def test_cold_start_retains_boundary_zero_and_exact_bootstrap_execution() -> None:
    events = pilot_events(
        PLC(first_scan.logic, dt=0.010),
        first_scan.ProcessStep == first_scan.AT_TARGET,
        max_scans=100,
    )
    try:
        started = _started_event(events)
        observed = _entry_event(events)
    finally:
        events.close()

    snapshot = observed.data["execution"]
    assert isinstance(snapshot, BootstrapExecutionSnapshot)
    assert (snapshot.source_scan, snapshot.landing_scan, started.scan, observed.scan) == (
        0,
        1,
        0,
        1,
    )
    assert snapshot.source_world_key is not None
    assert snapshot.source[first_scan.ProcessStep.name] == first_scan.INITIAL
    assert snapshot.objective == (first_scan.ProcessStep.name, first_scan.AT_TARGET)
    assert snapshot.objective_frontier
    assert snapshot.objective_frontier[0] == (
        first_scan.ProcessStep.name,
        first_scan.AT_TARGET,
    )
    assert snapshot.landing[first_scan.ProcessStep.name] == first_scan.ABORTED
    assert _snapshot_accesses(snapshot) == (
        (14, "read", "sys.first_scan", True, 0),
        (15, "write", "FirstScanProcessStep", 0, 10, 0),
        (16, "read", "FirstScanProcessStep", 10, 1),
        (19, "write", "FirstScanProcessStep", 10, 40, 1),
        (20, "read", "FirstScanProcessStep", 40, 2),
        (23, "write", "FirstScanProcessStep", 40, 80, 2),
        (24, "read", "FirstScanWatchdog_Acc", 0, 2),
        (27, "read", "FirstScanWatchdogPresetMs", 0, 2),
        (29, "write", "FirstScanWatchdog_Done", False, True, 2),
        (30, "write", "FirstScanWatchdog_Acc", 0, 10, 2),
        (31, "write", "FirstScanWatchdog_EN", False, True, 2),
        (32, "write", "FirstScanWatchdog_TT", False, False, 2),
        (33, "read", "FirstScanWatchdog_Done", True, 3),
        (36, "write", "FirstScanProcessStep", 80, 90, 3),
    )


def test_destructive_bootstrap_reports_exact_target_overwrite_and_consumed_handoffs() -> None:
    events = pilot_events(
        PLC(first_scan.logic, dt=0.010),
        first_scan.ProcessStep == first_scan.AT_TARGET,
        max_scans=100,
    )
    try:
        _started_event(events)
        snapshot = _entry_event(events).data["execution"]
    finally:
        events.close()

    assert isinstance(snapshot, BootstrapExecutionSnapshot)
    assert tuple(
        (
            designation.tag,
            designation.value,
            designation.producer,
            designation.consumer,
            designation.required_shape,
        )
        for designation in snapshot.designations
    ) == (
        (first_scan.ProcessStep.name, first_scan.AT_TARGET, (None, 2, ()), None, ()),
        (
            first_scan.ProcessStep.name,
            first_scan.RUNNING,
            (None, 1, ()),
            (None, 2, ()),
            ((first_scan.ProcessStep.name, first_scan.RUNNING),),
        ),
        (
            first_scan.ProcessStep.name,
            first_scan.READY,
            (None, 0, ()),
            (None, 1, ()),
            ((first_scan.ProcessStep.name, first_scan.READY),),
        ),
    )
    # Pure reads and the steerable preset are not promoted to bootstrap work.
    assert {designation.tag for designation in snapshot.designations} == {
        first_scan.ProcessStep.name
    }

    ready, running, target = snapshot.appeared_effects
    assert target.disposition == "OVERWRITTEN"
    assert target.appeared is not None
    assert (target.appeared.ordinal, target.appeared.values) == (
        23,
        (first_scan.RUNNING, first_scan.AT_TARGET),
    )
    assert target.displacement is not None
    assert (target.displacement.ordinal, target.displacement.values) == (
        36,
        (first_scan.AT_TARGET, first_scan.ABORTED),
    )
    assert tuple((read.ordinal, read.tag, read.values) for read in target.observed_reads) == (
        (33, first_scan.Watchdog.Done.name, (True,)),
    )

    # Each intermediate value was correctly consumed at its exact read.  Its
    # later advancement is legitimate and does not become an overwrite.
    assert (running.disposition, running.appeared.ordinal, running.consumer_read.ordinal) == (
        "SURVIVED",
        19,
        20,
    )
    assert (ready.disposition, ready.appeared.ordinal, ready.consumer_read.ordinal) == (
        "SURVIVED",
        15,
        16,
    )


def test_bootstrap_event_consumer_cannot_mutate_or_advance_internal_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    internal_receipts: list[_BootstrapExecution] = []
    internal_states: list[Any] = []
    original_snapshot = _BootstrapExecution.diagnostic_snapshot
    original_bind = entry_execution_module.bind_entry_execution_to_route

    def _capture(receipt: _BootstrapExecution) -> BootstrapExecutionSnapshot:
        internal_receipts.append(receipt)
        return original_snapshot(receipt)

    def _capture_binding(
        state: Any, ctx: Any, result: Any, frame: Any
    ) -> _BootstrapExecution | None:
        receipt = original_bind(state, ctx, result, frame)
        internal_states.append(state)
        return receipt

    monkeypatch.setattr(_BootstrapExecution, "diagnostic_snapshot", _capture)
    monkeypatch.setattr(
        entry_execution_module,
        "bind_entry_execution_to_route",
        _capture_binding,
    )
    events = pilot_events(
        PLC(first_scan.logic, dt=0.010),
        first_scan.ProcessStep == first_scan.AT_TARGET,
        max_scans=100,
    )
    try:
        _started_event(events)
        observed = _entry_event(events)
    finally:
        events.close()

    snapshot = observed.data["execution"]
    assert isinstance(snapshot, BootstrapExecutionSnapshot)
    assert isinstance(snapshot.source, MappingProxyType)
    assert isinstance(snapshot.landing, MappingProxyType)
    assert not hasattr(snapshot, "checkpoint")
    assert not hasattr(snapshot, "projection")
    assert not hasattr(snapshot, "work")

    with pytest.raises(TypeError):
        snapshot.landing[first_scan.ProcessStep.name] = 999  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        snapshot.source_scan = 99  # type: ignore[misc]
    with pytest.raises(AttributeError):
        snapshot.checkpoint.world.work.step()  # type: ignore[attr-defined]

    # Even replacement of the caller-owned event payload cannot reach back to
    # the private state receipt captured by this test-only instrumentation.
    cast(dict[str, Any], observed.data)["execution"] = None
    internal = internal_receipts[0]
    before = internal.diagnostic_snapshot()
    assert internal.checkpoint.world.work.state.scan_id == 0
    assert internal.projection.scan_id == 1
    execution_owner = internal.execution.owner_at(internal.scan_after)
    assert execution_owner is not None
    assert execution_owner.epoch.reference == internal.execution.epoch_ref
    assert (
        execution_owner.epoch.first_scan <= internal.scan_after <= execution_owner.epoch.last_scan
    )
    assert execution_owner._live_plc is None

    # Restore and advance the source world through the production state owner.
    # The descendant runner is replaceable; the original scan-1 epoch/query is
    # already detached and remains the occurrence's causal owner.
    state = internal_states[0]
    state.load_world(internal.checkpoint.world)
    assert state.work.state.scan_id == 0
    state.work.step()
    assert state.work.state.scan_id == 1
    assert internal.checkpoint.world.work.state.scan_id == 0
    assert internal.diagnostic_snapshot() == before

    retained_state = execution_owner.state_at(1)
    retained_capture = execution_owner.replay_capture_at(1)
    assert retained_state.tags[first_scan.ProcessStep.name] == first_scan.ABORTED
    assert retained_capture is not None
    assert retained_capture.runs
    retained_projection = execution_owner.rung_write_projection_at(1)
    assert retained_projection is not None
    assert _ordered_accesses(retained_projection) == _ordered_accesses(internal.projection)


def test_prescanned_world_imports_the_adjacent_scan_without_executing_another() -> None:
    plc = PLC(first_scan.logic, dt=0.010)
    plc.step()
    start_scan = plc.state.scan_id
    events = pilot_events(
        plc,
        first_scan.ProcessStep == first_scan.AT_TARGET,
        max_scans=100,
    )
    try:
        started = _started_event(events)
        observed = _entry_event(events)
    finally:
        events.close()

    assert start_scan == 1
    assert started.scan == start_scan
    imported = started.data["bootstrap_execution"]
    assert isinstance(imported, BootstrapExecutionSnapshot)
    assert (imported.source_scan, imported.landing_scan) == (0, 1)
    assert observed.scan == 1
    assert isinstance(observed.data["execution"], BootstrapExecutionSnapshot)


def test_entry_route_binding_alone_defers_program_input_receipts(monkeypatch) -> None:
    deferred: list[bool] = []
    original_orient = Compass.orient

    def capture_constraint(self, world, target, constraints):
        deferred.append(constraints.defer_program_input_receipts)
        return original_orient(self, world, target, constraints)

    monkeypatch.setattr(Compass, "orient", capture_constraint)
    events = pilot_events(
        PLC(first_scan.logic, dt=0.010),
        first_scan.ProcessStep == first_scan.AT_TARGET,
        max_scans=100,
    )
    try:
        observed_entry = False
        for event in events:
            observed_entry |= event.kind == "entry_scan_observed"
            if observed_entry and event.kind == "iteration":
                break
    finally:
        events.close()

    assert deferred[:3] == [False, True, False]


def test_alarmed_action_scan_retains_complete_then_watchdog_overwrite() -> None:
    plc = PLC(alarmed.logic, dt=0.010)
    plc.force(alarmed.Reset, True)
    plc.force(alarmed.AtTarget, True)

    plc.step()

    projection = plc._replay_rung_write_projection_at(1)
    assert projection is not None
    assert projection.entry_tags[alarmed.ProcessStep.name] == alarmed.ALARMED
    assert projection.exit_tags[alarmed.ProcessStep.name] == alarmed.ALARMED
    assert _ordered_accesses(projection) == (
        (16, "read", "Reset", True, 0),
        (17, "read", "ProcessStep", 91, 0),
        (20, "write", "ProcessStep", 91, 40, 0),
        (21, "read", "ProcessStep", 40, 1),
        (22, "read", "AtTarget", True, 1),
        (25, "write", "ProcessStep", 40, 80, 1),
        (26, "read", "Watchdog_Acc", 0, 1),
        (29, "read", "WatchdogPresetMs", 0, 1),
        (31, "write", "Watchdog_Done", False, True, 1),
        (32, "write", "Watchdog_Acc", 0, 10, 1),
        (33, "write", "Watchdog_EN", False, True, 1),
        (34, "write", "Watchdog_TT", False, False, 1),
        (35, "read", "Watchdog_Done", True, 2),
        (38, "write", "ProcessStep", 80, 91, 2),
    )


def test_committed_spent_reset_requires_a_false_scan_before_reassertion() -> None:
    """Pin execution only; one-shot interpretation remains a later phase."""

    plc = PLC(alarmed.logic, dt=0.010)
    plc.force(alarmed.Reset, True)
    plc.force(alarmed.AtTarget, True)
    plc.step()

    plc.force(alarmed.WatchdogPresetMs, alarmed.SAFE_WATCHDOG_PRESET_MS)
    plc.step()
    preset_only = plc._replay_rung_write_projection_at(2)
    assert preset_only is not None
    assert [(read.occurrence.name, read.occurrence.value) for read in preset_only.reads[:2]] == [
        (alarmed.Reset.name, True),
        (alarmed.ProcessStep.name, alarmed.ALARMED),
    ]
    assert not any(
        write.transition.tag_name == alarmed.ProcessStep.name for write in preset_only.writes
    )

    plc.force(alarmed.Reset, False)
    plc.step()
    release = plc._replay_rung_write_projection_at(3)
    assert release is not None
    assert (release.reads[0].occurrence.name, release.reads[0].occurrence.value) == (
        alarmed.Reset.name,
        False,
    )

    plc.force(alarmed.Reset, True)
    plc.step()
    reassert = plc._replay_rung_write_projection_at(4)
    assert reassert is not None
    process_writes = tuple(
        (
            write.transition.from_value,
            write.transition.to_value,
            write.rung_id.rung_index,
        )
        for write in reassert.writes
        if write.transition.tag_name == alarmed.ProcessStep.name
    )
    assert process_writes == (
        (alarmed.ALARMED, alarmed.RUNNING, 0),
        (alarmed.RUNNING, alarmed.COMPLETE, 1),
    )
    assert reassert.exit_tags[alarmed.ProcessStep.name] == alarmed.COMPLETE


def test_conditional_negative_occurrence_and_separate_recovery_are_execution_facts() -> None:
    fixture = conditional_negative
    plc = PLC(fixture.logic, dt=0.010)

    plc.step()
    committed = plc._replay_rung_write_projection_at(1)
    assert committed is not None
    done_read = next(
        read for read in committed.reads if read.occurrence.name == fixture.Watchdog.Done.name
    )
    consequence_write = next(
        write for write in committed.writes if write.transition.tag_name == fixture.Consequence.name
    )
    observed_definition = committed.transition_observed_by_read(done_read)
    assert (
        done_read.ordinal,
        done_read.occurrence.value,
        consequence_write.ordinal,
        done_read.run_order,
        consequence_write.run_order,
    ) == (23, False, 24, 1, 1)
    assert observed_definition is not None
    assert (
        observed_definition.occurrence_ordinal,
        observed_definition.from_value,
        observed_definition.to_value,
    ) == (19, False, False)
    assert committed.entry_tags[fixture.Watchdog.Done.name] is False
    assert committed.exit_tags[fixture.Watchdog.Done.name] is False
    assert committed.exit_tags[fixture.Consequence.name] is True

    # A preset change after the consequential read changes later timer truth,
    # but does not rewrite the already committed latch.
    plc.force(fixture.PresetMs, fixture.PREVENTING_PRESET_MS)
    plc.step()
    late = plc._replay_rung_write_projection_at(2)
    assert late is not None
    assert late.exit_tags[fixture.Watchdog.Done.name] is True
    assert late.exit_tags[fixture.Consequence.name] is True
    assert not any(write.transition.tag_name == fixture.Consequence.name for write in late.writes)

    # The checkpoint-equivalent source behaves differently only when the
    # preset is present before the timer and negative read execute.
    prevented = PLC(fixture.logic, dt=0.010)
    prevented.force(fixture.PresetMs, fixture.PREVENTING_PRESET_MS)
    prevented.step()
    prevented_projection = prevented._replay_rung_write_projection_at(1)
    assert prevented_projection is not None
    prevented_done_read = next(
        read
        for read in prevented_projection.reads
        if read.occurrence.name == fixture.Watchdog.Done.name
    )
    assert (prevented_done_read.ordinal, prevented_done_read.occurrence.value) == (24, True)
    assert prevented_projection.exit_tags[fixture.Consequence.name] is False
    assert not any(
        write.transition.tag_name == fixture.Consequence.name
        for write in prevented_projection.writes
    )

    # Current-state clearing is a separate scan and exact writer occurrence.
    plc.force(fixture.Clear, True)
    plc.step()
    cleared = plc._replay_rung_write_projection_at(3)
    assert cleared is not None
    clear_write = next(
        write for write in cleared.writes if write.transition.tag_name == fixture.Consequence.name
    )
    assert (
        clear_write.ordinal,
        clear_write.transition.from_value,
        clear_write.transition.to_value,
        clear_write.rung_id.rung_index,
    ) == (12, True, False, 2)
    assert cleared.exit_tags[fixture.Consequence.name] is False
