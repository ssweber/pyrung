"""Cheap locks for regression replay's structural stop boundaries."""

from __future__ import annotations

from types import SimpleNamespace

from pyrung import PLC, Bool, Int, Program, copy, rung
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.investigation_replay import (
    RegressionWitness,
    ReplayIncident,
    ReplayJustification,
    ReplayStep,
    _replay_step,
    _step_owns_departure,
    build_replay_fn,
)
from pyrung.core.analysis.pilot.navigation_contracts import TargetSpec
from pyrung.core.analysis.pilot.types import ChannelMotion, MotionKind
from pyrung.core.analysis.steerable import compute_steerable
from pyrung.core.crossing import Cmp


def test_rebound_incident_keeps_the_original_physical_boundary() -> None:
    """Semantic incident ownership must not replace the operation replay runs."""

    boundary = Cmp("Replay_Acc", ">", 4)
    context = SimpleNamespace(
        policy=SimpleNamespace(motion=MotionKind.COAST_TO_BEARING),
        execution=SimpleNamespace(
            channel_motion=ChannelMotion("Replay_State", 3, stop_reason="departed"),
            replay_motion=ChannelMotion("Replay_Acc", 4, boundary),
        ),
    )
    step = SimpleNamespace(inputs={}, scans=42, scan_before=10, scan_after=52)

    replay = _replay_step(step, context)

    assert replay.channel_tag == "Replay_Acc"
    assert replay.channel_target == 4
    assert replay.channel_boundary is boundary
    assert (replay.scan_before, replay.scan_after) == (10, 52)


def test_only_the_recorded_incident_operation_owns_the_extra_departure_watch() -> None:
    """Committed prefix coasts cannot inherit a later incident's guard."""

    witness = SimpleNamespace(departure_scan=42)
    prefix = ReplayStep((), 30, "bearing_coast", scan_before=10, scan_after=40)
    incident = ReplayStep((), 10, "bearing_coast", scan_before=40, scan_after=50)

    assert not _step_owns_departure(prefix, witness)
    assert _step_owns_departure(incident, witness)


def _replay_context(program: Program, plc: PLC, target: TargetSpec) -> SimpleNamespace:
    pdg = build_program_graph(program)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, program)
    return SimpleNamespace(
        resting={name: False for name in steerable if isinstance(plc.state.tags.get(name), bool)},
        edge_tags=set(),
        target=target,
        pdg=pdg,
        program=program,
        steerable=steerable,
        opaque_loop=frozenset(),
        pipeline_internal_tags=frozenset(),
        route=None,
        domain_prior=None,
        clear_only=frozenset(),
        avoid_pred=None,
        max_scans=100,
    )


def test_terminal_target_wins_when_it_fires_with_incident_departure() -> None:
    """A requested terminal landing is success even when the guard fires too."""

    go = Bool("Boundary_Go", external=True, default=True)
    state = Int("Boundary_State", default=1)
    acc = Int("Boundary_Acc")
    with Program() as program:
        with rung(go):
            copy(2, state)
        with rung(~go):
            copy(0, acc)
    plc = PLC(program)
    boundary = Cmp(acc.name, ">", 100)
    step = ReplayStep(
        (),
        5,
        "bearing_coast",
        acc.name,
        100,
        boundary,
        scan_before=0,
        scan_after=5,
    )
    witness = RegressionWitness(
        channel_tag=state.name,
        source=1,
        departed=90,
        landing=90,
        departure_scan=1,
        cause=(),
        causal_spine=frozenset({state.name}),
    )
    replay = build_replay_fn(
        plc,
        1,
        (),
        (step,),
        ctx=_replay_context(program, plc, TargetSpec(state.name, 2)),
        incident=ReplayIncident(
            channel_tag=state.name,
            channel_target=1,
            watch_roles=(state.name,),
            regression_witness=witness,
        ),
    )

    outcome = replay(())

    assert outcome.accepted
    assert outcome.justification is ReplayJustification.REACHED
    assert outcome.snapshot[state.name] == 2


def test_unreachable_boundary_stops_at_the_recorded_incident_horizon(monkeypatch) -> None:
    """Suppressing an incident cannot turn replay into a fresh 10k-scan coast."""

    enabled = Bool("Horizon_Enabled", external=True)
    acc = Int("Horizon_Acc")
    state = Int("Horizon_State", default=1)
    with Program() as program:
        with rung(enabled):
            copy(1, acc)
        with rung(state == 99):
            copy(0, acc)
    plc = PLC(program)
    boundary = Cmp(acc.name, ">", 100)
    step = ReplayStep(
        (),
        4,
        "bearing_coast",
        acc.name,
        100,
        boundary,
        scan_before=0,
        scan_after=4,
    )
    witness = RegressionWitness(
        channel_tag=state.name,
        source=1,
        departed=90,
        landing=90,
        departure_scan=4,
        cause=(),
        causal_spine=frozenset({state.name}),
    )
    from pyrung.core.analysis.pilot import steer as steer_module

    receipts = []
    coast_to_bearing = steer_module._coast_to_bearing

    def _capture_coast(*args, **kwargs):
        trajectory, receipt = coast_to_bearing(*args, **kwargs)
        receipts.append(receipt)
        return trajectory, receipt

    monkeypatch.setattr(steer_module, "_coast_to_bearing", _capture_coast)
    replay = build_replay_fn(
        plc,
        1,
        (),
        (step,),
        ctx=_replay_context(program, plc, TargetSpec(state.name, 2)),
        incident=ReplayIncident(
            channel_tag=state.name,
            channel_target=1,
            watch_roles=(state.name,),
            regression_witness=witness,
        ),
    )

    outcome = replay(())

    assert plc.state.scan_id == 0
    assert outcome.snapshot[state.name] == 1
    assert len(receipts) == 1
    assert receipts[0].budget == 4
    assert receipts[0].logical_scans == 4
    assert receipts[0].stop_reason == "timeout"
