from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from pyrung import Int
from pyrung.core.analysis.pilot.compass import (
    ActionNogoodObservation,
    Compass,
    ProbeExhaustedObservation,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActPolicy,
    ActSource,
    BatchPulse,
    Bearing,
    BearingObjective,
    ChannelHeading,
    Coast,
    CrossingFidelity,
    Dwell,
    ExpectationExemption,
    IntrascanPulse,
    LandingReceiptAuthority,
    LocalProgressKind,
    MotionKind,
    NavigationConstraints,
    NeedIntrascanTraceback,
    NeedProbe,
    NeedResearch,
    OrientationWorld,
    ProgramScan,
    Pulse,
    PulseHorizon,
    RouteEdgeContext,
    Stuck,
    TargetSpec,
    act_identity,
    pulse_identity,
)
from pyrung.core.analysis.pilot.options import (
    CandidateDiagnosis,
    CandidateRead,
    CrossingBatchRead,
    LearnedBatchRead,
    PrerequisiteRead,
    WaitPrescription,
    WaitRead,
    _TraceAdmission,
)
from pyrung.core.analysis.pilot.overlay import PilotRung
from pyrung.core.analysis.pilot.recording import _candidate_payload
from pyrung.core.analysis.pilot.requirements import OperandAuthority
from pyrung.core.analysis.pilot.working_theory import (
    ProgramTransaction,
    ScanEntryConfiguration,
    TheoryTemporalIntent,
)
from pyrung.core.context import RungId
from pyrung.core.crossing import Cmp


@dataclass
class _Context:
    compass: Compass
    target: TargetSpec = TargetSpec("Target", True)
    avoid_pred: object = None
    route: object = None
    blocked_actions: frozenset = frozenset()
    pdg: object = None
    program: object = None
    steerable: frozenset = frozenset()
    clear_only: frozenset = frozenset()
    edge_tags: frozenset = frozenset()
    resting: dict | None = None
    opaque_loop: frozenset = frozenset()
    pipeline_internal_tags: frozenset = frozenset()
    domain_prior: object = None
    theory_view: object = None
    active_requirements: tuple = ()
    temporal_requirements: tuple = ()
    temporal_trigger_requirements: tuple = ()
    temporal_source_anchor: object = None


def _candidate(tag: str) -> SimpleNamespace:
    return SimpleNamespace(
        tag=tag,
        value=True,
        pair=(tag, True),
        source=ActSource.TRACE,
        awaited_action_note="",
        route_prescribed=False,
        learned_prescribed=False,
        awaited_action_prescribed=False,
        program_prescribed=False,
        program_note="",
        bearing_channel_tag=None,
        bearing_channel_value=None,
        provenance=(),
        downstream_reach=None,
        program_context_actions=(),
    )


def _options(
    *candidates,
    stuck_reason=None,
    prescribed_batch=None,
    active_trace_actions=(),
    wait=None,
    crossing_batches=(),
    batch_expectation=None,
    widening_expectation=None,
):
    return CandidateRead(
        trace=_TraceAdmission(
            active_actions=active_trace_actions,
            actions=active_trace_actions,
            details=(),
            detail_by_pair={},
            managed_boolean_rungs=(),
            establish_pending=False,
        ),
        options=tuple(candidates),
        downstream_reach_cap=20,
        wait=wait,
        prerequisites=PrerequisiteRead(),
        learned_batch=(
            LearnedBatchRead(prescribed_batch, batch_expectation)
            if prescribed_batch is not None
            else None
        ),
        crossing_batches=crossing_batches,
        diagnosis=CandidateDiagnosis(stuck_reason) if stuck_reason is not None else None,
        widening_expectations=(
            ((active_trace_actions[:2], widening_expectation),)
            if widening_expectation is not None
            else ()
        ),
    )


def test_orientation_threads_one_expectation_for_batch_crossing_widening_and_coast(
    monkeypatch,
) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    world = _world(compass)
    expectation = SimpleNamespace(obligations=(SimpleNamespace(tag="Effect"),))

    monkeypatch.setattr(
        orientation,
        "_build_candidates",
        lambda *_args: _options(
            prescribed_batch=(("A", True), ("B", True)),
            batch_expectation=expectation,
        ),
    )
    learned = orientation._orient_read(compass, world, TargetSpec("Target", True))
    assert isinstance(learned, Bearing)
    assert isinstance(learned.act, BatchPulse)
    assert learned.act.policy.expectation is expectation

    monkeypatch.setattr(
        orientation,
        "_build_candidates",
        lambda *_args: _options(
            crossing_batches=(
                CrossingBatchRead(
                    (("A", True), ("B", True)),
                    CrossingFidelity((), "cross", True, True, False),
                    expectation,
                ),
            )
        ),
    )
    crossing = orientation._orient_read(compass, world, TargetSpec("Target", True))
    assert isinstance(crossing, Bearing)
    assert crossing.act.policy.expectation is expectation

    monkeypatch.setattr(
        orientation,
        "_build_candidates",
        lambda *_args: _options(
            active_trace_actions=(("A", True), ("B", True)),
            widening_expectation=expectation,
        ),
    )
    widening = orientation._orient_read(compass, world, TargetSpec("Target", True))
    assert isinstance(widening, Bearing)
    assert widening.act.policy.expectation is expectation

    heading = ChannelHeading("State", 2)
    monkeypatch.setattr(
        orientation,
        "_build_candidates",
        lambda *_args: _options(
            wait=WaitRead(WaitPrescription(heading, "program coast", expectation=expectation))
        ),
    )
    coast = orientation._orient_read(compass, world, TargetSpec("Target", True))
    assert isinstance(coast, Bearing)
    assert isinstance(coast.act, Coast)
    assert coast.act.policy.expectation is expectation


def test_terminal_orientation_is_explicit_ambient_non_promise(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    world = _world(compass)
    monkeypatch.setattr(orientation, "_build_candidates", lambda *_args: _options())

    terminal = orientation._orient_read(compass, world, TargetSpec("Target", True))

    assert isinstance(terminal, Bearing)
    assert terminal.act.policy.expectation is None
    assert terminal.act.policy.expectation_exemption == "ambient_terminal"


def test_unresolved_executable_policies_are_explicitly_exempt(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    world = _world(compass)
    reads = (
        _options(_candidate("A")),
        _options(prescribed_batch=(("A", True), ("B", True))),
        _options(
            crossing_batches=(
                CrossingBatchRead(
                    (("A", True), ("B", True)),
                    CrossingFidelity((), "cross", True, True, False),
                    None,
                ),
            )
        ),
        _options(active_trace_actions=(("A", True), ("B", True))),
        _options(wait=WaitRead(WaitPrescription(ChannelHeading("State", 2)))),
    )

    for candidate_read in reads:
        monkeypatch.setattr(
            orientation,
            "_build_candidates",
            lambda *_args, _read=candidate_read: _read,
        )
        bearing = orientation._orient_read(compass, world, TargetSpec("Target", True))
        assert isinstance(bearing, Bearing)
        assert bearing.act.policy.expectation is None
        assert bearing.act.policy.expectation_exemption is ExpectationExemption.UNRESOLVED_EFFECT


def test_temporal_retry_augments_fresh_trigger_instead_of_requirement_candidate(
    monkeypatch,
) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    world = _world(compass)
    trigger = pulse_identity((("Request", True),))
    world = replace(
        world,
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                trigger_act_identity=trigger,
                claim=SimpleNamespace(obligations=()),
            ),
            temporal_requirements=(object(),),
        ),
    )
    candidates = _options(
        _candidate("Production"),
        _candidate("Request"),
        active_trace_actions=(("Production", True), ("Request", True)),
    )
    schedule = SimpleNamespace(
        assignments=(("Production", True),),
        pilot_rungs=(),
    )
    monkeypatch.setattr(
        orientation,
        "_iter_temporal_schedules",
        lambda *_args: iter((schedule,)),
    )
    production_policy = ActPolicy(
        source=ActSource.TRACE,
        action_pairs=(("Production", True),),
        applied=(("Production", True),),
        expectation_exemption=ExpectationExemption.UNRESOLVED_EFFECT,
    )
    ordinary = Bearing(
        world_key=world.world_key,
        act=Pulse(production_policy),
        objective=BearingObjective(TargetSpec("Target", True)),
    )

    retry = orientation._theory_temporal_retry_bearing(
        world,
        candidates,
        TargetSpec("Target", True),
        ordinary=ordinary,
    )

    assert isinstance(retry, Bearing)
    assert isinstance(retry.act, BatchPulse)
    assert retry.act.policy.applied == (("Production", True), ("Request", True))
    assert act_identity(retry.act) == pulse_identity((("Request", True), ("Production", True)))


def test_retry_through_deadline_persists_one_companion_before_fresh_steer(
    monkeypatch,
) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    world = _world(compass)
    world = replace(
        world,
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                temporal_intent=TheoryTemporalIntent.RETRY_THROUGH_DEADLINE,
                trigger_act_identity=pulse_identity((("Request", True),)),
                claim=SimpleNamespace(obligations=()),
            ),
            temporal_requirements=(object(),),
        ),
    )
    candidates = _options(
        _candidate("Request"),
        _candidate("FirstCorrective"),
        _candidate("SecondCorrective"),
        active_trace_actions=(
            ("Request", True),
            ("FirstCorrective", True),
            ("SecondCorrective", True),
        ),
    )
    monkeypatch.setattr(
        orientation,
        "_iter_temporal_schedules",
        lambda *_args: iter((SimpleNamespace(assignments=(), pilot_rungs=()),)),
    )
    result = orientation._theory_temporal_retry_bearing(
        world,
        candidates,
        TargetSpec("Target", True),
    )

    assert isinstance(result, Bearing)
    assert isinstance(result.act, Pulse)
    assert result.act.policy.applied == (("FirstCorrective", True),)
    assert result.act.policy.local_progress is LocalProgressKind.THEORY_CORRECTIVE
    assert result.act.policy.pulse_horizon is PulseHorizon.ASSERTION_SCAN


def test_retry_through_deadline_carries_the_transaction_consumer_boundary(
    monkeypatch,
) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    boundary = object()
    trigger = pulse_identity((("Request", True),))
    world = _world(Compass())
    world = replace(
        world,
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                temporal_intent=TheoryTemporalIntent.RETRY_THROUGH_DEADLINE,
                trigger_act_identity=trigger,
                claim=SimpleNamespace(obligations=()),
                investigation_scope=SimpleNamespace(
                    retry_act_identity=trigger,
                    transaction_act_identity=trigger,
                    transaction_rearmed=True,
                    consumer_boundary=boundary,
                ),
            ),
            temporal_requirements=(object(),),
        ),
    )
    candidates = _options(
        _candidate("Request"),
        active_trace_actions=(("Request", True),),
    )
    monkeypatch.setattr(
        orientation,
        "_iter_temporal_schedules",
        lambda *_args: iter((SimpleNamespace(assignments=(), pilot_rungs=()),)),
    )

    result = orientation._theory_temporal_retry_bearing(
        world,
        candidates,
        TargetSpec("Target", True),
    )

    assert isinstance(result, Bearing)
    assert result.act.policy.applied == (("Request", True),)
    assert result.act.policy.pulse_horizon is PulseHorizon.CONSUMER_BOUNDARY
    assert result.act.policy.consumer_boundary is boundary


def test_pending_configuration_retries_only_the_horizon_owned_transaction(
    monkeypatch,
) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    boundary = object()
    transaction = (("Request", True),)
    transaction_identity = pulse_identity(transaction)
    transaction_attempt_id = ("attempt", "transaction")
    boundary_attempt_id = ("attempt", "consumer")
    scope = SimpleNamespace(
        transaction_attempt_id=transaction_attempt_id,
        transaction_act_identity=transaction_identity,
        transaction_act_pairs=transaction,
        transaction_selected_pairs=transaction,
        transaction_rearmed=False,
        retry_act_identity=None,
        consumer_boundary_attempt_id=boundary_attempt_id,
        consumer_boundary=boundary,
        consumer_stop=object(),
    )
    world = _world(Compass())
    world.snapshot["SequenceStep"] = 50
    world = replace(
        world,
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                temporal_intent=TheoryTemporalIntent.RETRY_THROUGH_DEADLINE,
                trigger_act_identity=transaction_identity,
                claim=SimpleNamespace(obligations=()),
                investigation_scope=scope,
            ),
            temporal_requirements=(object(),),
        ),
    )
    candidates = _options(
        active_trace_actions=(
            ("Request", True),
            ("TestingMode", False),
            ("Ready", True),
        ),
    )
    schedule = SimpleNamespace(
        assignments=(("TestingMode", False), ("Ready", True)),
        pilot_rungs=(),
    )
    monkeypatch.setattr(
        orientation,
        "_iter_temporal_schedules",
        lambda *_args: iter((schedule,)),
    )
    monkeypatch.setattr(
        orientation,
        "_pending_theory_pairs",
        lambda *_args: (("TestingMode", True),),
    )
    broad_policy = ActPolicy(
        source=ActSource.WIDENING,
        action_pairs=(("Request", True), ("TestingMode", False), ("Ready", True)),
        applied=(("Request", True), ("TestingMode", False), ("Ready", True)),
        expectation_exemption=ExpectationExemption.UNRESOLVED_EFFECT,
    )
    ordinary = Bearing(
        world_key=world.world_key,
        act=BatchPulse(broad_policy),
        objective=BearingObjective(TargetSpec("Target", True)),
    )

    result = orientation._theory_temporal_retry_bearing(
        world,
        candidates,
        TargetSpec("Target", True),
        ordinary=ordinary,
    )

    assert isinstance(result, Bearing)
    assert isinstance(result.act, Pulse)
    assert result.act.policy.applied == transaction
    assert result.act.policy.pulse_horizon is PulseHorizon.CONSUMER_BOUNDARY
    assert result.act.policy.consumer_boundary is boundary


def test_pending_configuration_retries_the_fresh_actionless_program_transaction(
    monkeypatch,
) -> None:
    """A Coast is reread from ProgramStep; its failed receipt is identity only."""

    import pyrung.core.analysis.pilot.orientation as orientation

    heading = ChannelHeading("SequenceStep", 60)
    prescription = WaitPrescription(heading, "continue the autonomous sequence")
    coast = Coast(
        "bearing",
        ActPolicy(
            source=ActSource.PROGRAM,
            heading=heading,
            motion=MotionKind.COAST_TO_BEARING,
            expectation_exemption=ExpectationExemption.UNRESOLVED_EFFECT,
        ),
    )
    world = _world(Compass())
    world = replace(
        world,
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                temporal_intent=TheoryTemporalIntent.RETRY_TOGETHER,
                trigger_act_identity=act_identity(coast),
                configurations=(
                    ScanEntryConfiguration((("WatchdogPreset", 11),)),
                ),
                trigger_program_transaction=ProgramTransaction.from_heading(
                    heading,
                    world.snapshot,
                ),
                claim=SimpleNamespace(obligations=()),
                investigation_scope=None,
            ),
            temporal_requirements=(object(),),
        ),
    )
    candidates = _options(wait=WaitRead(prescription))
    corrective = PilotRung("WatchdogPreset", 11, Int("Guard") != 81)
    schedule = SimpleNamespace(
        assignments=(("WatchdogPreset", 11),),
        pilot_rungs=(corrective,),
        requirements=("watchdog-requirement",),
        requirement_sources=("watchdog-parent",),
    )
    monkeypatch.setattr(
        orientation,
        "_iter_temporal_schedules",
        lambda *_args: iter((schedule,)),
    )
    monkeypatch.setattr(
        orientation,
        "_pending_theory_pairs",
        lambda *_args: (("WatchdogPreset", 11),),
    )

    result = orientation._theory_temporal_retry_bearing(
        world,
        candidates,
        TargetSpec("Target", True),
    )

    assert isinstance(result, Bearing)
    assert isinstance(result.act, Coast)
    assert result.act.mode == "bearing"
    assert result.act.policy.heading is heading
    assert result.act.policy.applied == ()
    assert result.act.policy.local_progress is LocalProgressKind.TEMPORAL_EDGE
    assert result.act.policy.pulse_horizon is PulseHorizon.ASSERTION_SCAN
    assert result.act.policy.local_progress_requirements == ("watchdog-requirement",)
    assert result.act.policy.local_progress_sources == ("watchdog-parent",)
    assert result.prerequisites == ()
    assert result.entry_configurations == (
        ScanEntryConfiguration((("WatchdogPreset", 11),)),
    )
    assert act_identity(result.act) == act_identity(coast)


def test_temporal_retry_lazily_adds_current_trace_sibling_without_assigning_internal(
    monkeypatch,
) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    world = _world(compass)
    world = replace(
        world,
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                trigger_act_identity=pulse_identity((("Request", True),)),
                claim=SimpleNamespace(obligations=()),
            ),
            temporal_requirements=(object(),),
        ),
    )
    candidates = _options(
        _candidate("Request"),
        _candidate("Production"),
        active_trace_actions=(("Request", True), ("Production", True)),
    )
    monkeypatch.setattr(
        orientation,
        "_iter_temporal_schedules",
        lambda *_args: iter((SimpleNamespace(assignments=(), pilot_rungs=()),)),
    )

    retry = orientation._theory_temporal_retry_bearing(
        world,
        candidates,
        TargetSpec("Target", True),
    )

    assert isinstance(retry, Bearing)
    assert isinstance(retry.act, BatchPulse)
    assert retry.act.policy.applied == (("Request", True), ("Production", True))


def test_temporal_retry_continues_past_an_accepted_trace_companion(
    monkeypatch,
) -> None:
    """A wider trigger is identity; fresh trace siblings own the next width."""
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    world = _world(compass)
    world = replace(
        world,
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                trigger_act_identity=pulse_identity((("Request", True), ("Mode", True))),
                claim=SimpleNamespace(obligations=()),
            ),
            temporal_requirements=(object(),),
        ),
    )
    candidates = _options(
        _candidate("Request"),
        _candidate("Mode"),
        _candidate("Production"),
        active_trace_actions=(
            ("Request", True),
            ("Mode", True),
            ("Production", True),
        ),
    )
    monkeypatch.setattr(
        orientation,
        "_iter_temporal_schedules",
        lambda *_args: iter((SimpleNamespace(assignments=(), pilot_rungs=()),)),
    )
    base_policy = ActPolicy(
        source=ActSource.TRACE,
        action_pairs=(("Request", True),),
        applied=(("Request", True),),
        expectation_exemption=ExpectationExemption.UNRESOLVED_EFFECT,
    )
    ordinary = Bearing(
        world_key=world.world_key,
        act=Pulse(base_policy),
        objective=BearingObjective(TargetSpec("Target", True)),
    )

    retry = orientation._theory_temporal_retry_bearing(
        world,
        candidates,
        TargetSpec("Target", True),
        ordinary=ordinary,
    )

    assert isinstance(retry, Bearing)
    assert retry.act.policy.applied == (
        ("Request", True),
        ("Mode", True),
        ("Production", True),
    )


def test_retry_together_keeps_an_owned_unchanged_transaction_pair(
    monkeypatch,
) -> None:
    """Fresh reader authority preserves the complete consumer-bound transaction."""

    import pyrung.core.analysis.pilot.orientation as orientation

    transaction = (("Request", True), ("Mode", True))
    identity = pulse_identity(transaction)
    consumer_boundary = object()
    transaction_attempt_id = ("attempt", 1)
    boundary_attempt_id = ("attempt", 2)
    world = _world(Compass())
    world.snapshot.update(transaction)
    world = replace(
        world,
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                temporal_intent=TheoryTemporalIntent.RETRY_TOGETHER,
                trigger_act_identity=("coast", "later-displacement"),
                claim=SimpleNamespace(obligations=()),
                investigation_scope=SimpleNamespace(
                    retry_act_identity=identity,
                    transaction_act_identity=identity,
                    transaction_act_pairs=transaction,
                    transaction_attempt_id=transaction_attempt_id,
                    consumer_boundary=consumer_boundary,
                    consumer_boundary_attempt_id=boundary_attempt_id,
                    consumer_stop=object(),
                    transaction_rearmed=False,
                ),
            ),
            temporal_requirements=(object(),),
        ),
    )
    candidates = _options(
        _candidate("Request"),
        _candidate("Mode"),
        active_trace_actions=transaction,
    )
    monkeypatch.setattr(
        orientation,
        "_iter_temporal_schedules",
        lambda *_args: iter((SimpleNamespace(assignments=(), pilot_rungs=()),)),
    )
    monkeypatch.setattr(
        orientation,
        "_pending_theory_pairs",
        lambda _world: (("WatchdogPreset", 11),),
    )
    base = ActPolicy(
        source=ActSource.TRACE,
        action_pairs=(("Request", True),),
        applied=(),
        expectation_exemption=ExpectationExemption.UNRESOLVED_EFFECT,
    )
    ordinary = Bearing(
        world_key=world.world_key,
        act=Pulse(base),
        objective=BearingObjective(TargetSpec("Target", True)),
    )

    retry = orientation._theory_temporal_retry_bearing(
        world,
        candidates,
        TargetSpec("Target", True),
        ordinary=ordinary,
    )

    assert isinstance(retry, Bearing)
    assert retry.act.policy.applied == transaction
    assert act_identity(retry.act) == identity
    assert retry.act.policy.pulse_horizon is PulseHorizon.CONSUMER_BOUNDARY
    assert retry.act.policy.consumer_boundary is consumer_boundary


def test_consumer_stop_does_not_escape_its_transaction() -> None:
    """A pulse missing any owned transaction action cannot inherit its stop."""

    import pyrung.core.analysis.pilot.orientation as orientation

    consumer_boundary = object()
    transaction_attempt_id = ("attempt", 1)
    boundary_attempt_id = ("attempt", 2)
    scope = SimpleNamespace(
        transaction_act_pairs=(("Request", True), ("Mode", True)),
        transaction_attempt_id=transaction_attempt_id,
        consumer_boundary=consumer_boundary,
        consumer_boundary_attempt_id=boundary_attempt_id,
        consumer_stop=object(),
    )

    assert orientation._owned_consumer_boundary(scope, (("Request", True),)) is None


def test_temporal_retry_does_not_widen_through_an_avoided_trace_sibling(
    monkeypatch,
) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    world = _world(compass)
    world = replace(
        world,
        snapshot={"Target": False, "Avoided": False},
        context=replace(
            world.context,
            avoid_pred=lambda snapshot: bool(snapshot.get("Avoided")),
            theory_view=SimpleNamespace(
                trigger_act_identity=pulse_identity((("Request", True),)),
                claim=SimpleNamespace(obligations=()),
            ),
            temporal_requirements=(object(),),
        ),
    )
    candidates = _options(
        _candidate("Request"),
        _candidate("Avoided"),
        _candidate("Production"),
        active_trace_actions=(
            ("Request", True),
            ("Avoided", True),
            ("Production", True),
        ),
    )
    monkeypatch.setattr(
        orientation,
        "_iter_temporal_schedules",
        lambda *_args: iter((SimpleNamespace(assignments=(), pilot_rungs=()),)),
    )

    retry = orientation._theory_temporal_retry_bearing(
        world,
        candidates,
        TargetSpec("Target", True),
    )

    assert isinstance(retry, Bearing)
    assert retry.act.policy.applied == (("Request", True), ("Production", True))


def test_temporal_retry_uses_one_read_and_can_lower_standalone_tip_setup(
    monkeypatch,
) -> None:
    """A missing ordinary Bearing does not discard an executable new need."""
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    world = _world(compass)
    world = replace(
        world,
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                temporal_intent=TheoryTemporalIntent.RETRY_TOGETHER,
                trigger_act_identity=pulse_identity((("Request", True),)),
                claim=SimpleNamespace(obligations=()),
            ),
            temporal_requirements=(object(),),
        ),
    )
    candidates = _options(stuck_reason="trace_empty")
    build_calls = 0

    def build_once(*_args):
        nonlocal build_calls
        build_calls += 1
        return candidates

    monkeypatch.setattr(orientation, "_build_candidates", build_once)
    monkeypatch.setattr(
        Compass,
        "conductivity_research",
        lambda _self, _view: None,
    )
    monkeypatch.setattr(
        orientation,
        "_iter_temporal_schedules",
        lambda *_args: iter(
            (
                SimpleNamespace(
                    assignments=(("Production", True),),
                    pilot_rungs=(),
                    requirements=("exact-lowered-requirement",),
                    requirement_sources=("parent-requirement",),
                ),
            )
        ),
    )

    result = orientation._orient_read(compass, world, TargetSpec("Target", True))

    assert build_calls == 1
    assert isinstance(result, Bearing)
    assert result.act.policy.local_progress is LocalProgressKind.TEMPORAL_SETUP
    assert result.act.policy.pulse_horizon is PulseHorizon.ASSERTION_SCAN
    assert result.act.policy.applied == (("Production", True),)
    assert result.act.policy.local_progress_requirements == ("exact-lowered-requirement",)
    assert result.act.policy.local_progress_sources == ("parent-requirement",)


def test_conducted_boolean_parent_requests_occurrence_traceback_for_lowered_leaf(
    monkeypatch,
) -> None:
    """A conducted parent becomes a hypothetical consumer write, not a hold."""
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    parent = SimpleNamespace(
        condition=SimpleNamespace(),
        navigation_identity=("parent",),
        demanding_occurrence=SimpleNamespace(
            kind="read",
            rung=("ErrorHandling", 5),
            execution_kind="subroutine",
            caller_rung=30,
            call_stack=("ErrorHandling",),
            depth=1,
            call_invocation=2,
        ),
    )
    lowered = SimpleNamespace(condition=SimpleNamespace(tag="Link"))
    rung = PilotRung("Link", True, SimpleNamespace())
    world = _world(compass)
    world = replace(
        world,
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                temporal_intent=TheoryTemporalIntent.RETRY_TOGETHER,
            ),
            temporal_requirements=(parent,),
        ),
    )
    monkeypatch.setattr(
        orientation,
        "_iter_temporal_schedules",
        lambda *_args: iter(
            (
                SimpleNamespace(
                    requirements=(lowered,),
                    requirement_sources=(parent,),
                    requirement_bindings=((parent, (lowered,)),),
                    pilot_rungs=(rung,),
                ),
            )
        ),
    )
    monkeypatch.setattr(
        orientation,
        "_theory_conducted_occurrence",
        lambda *_args: parent.demanding_occurrence,
    )

    result = orientation._theory_correction_composition(
        world,
        _options(),
        TargetSpec("Target", True),
    )

    assert isinstance(result, NeedIntrascanTraceback)
    assert result.request.patch.dest == "Link"
    assert result.request.patch.value is True
    assert result.request.patch.guard is rung.guard
    assert result.request.patch.boundary.rung_id == RungId("ErrorHandling", 5)
    assert result.request.patch.boundary.call_invocation == 2
    assert result.request.requirements == (parent,)

    researched_world = replace(
        world,
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                temporal_intent=TheoryTemporalIntent.RETRY_TOGETHER,
                has_traceback_finding=lambda _identity: True,
            ),
        ),
    )
    assert (
        orientation._theory_correction_composition(
            researched_world,
            _options(),
            TargetSpec("Target", True),
        )
        is None
    )


def test_program_owned_setup_requests_exact_branch_traceback_from_fresh_bearing() -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    step = Int("ProgramOwnedStep", default=20)
    condition = Cmp(step.name, "==", 98)
    occurrence = SimpleNamespace(
        kind="read",
        tag=step.name,
        values=(40,),
        rung=("ErrorHandling", 5),
        execution_kind="branch",
        caller_rung=30,
        call_stack=("ErrorHandling",),
        depth=2,
        call_invocation=2,
        run_order=77,
    )
    requirement = SimpleNamespace(
        condition=condition,
        operand_authority=OperandAuthority.PROGRAM_WRITTEN,
        demanding_occurrence=occurrence,
        navigation_identity=("program-owned-step",),
    )
    world = _world(compass)
    world.snapshot.update({step.name: 20, "Link": False})
    world.state.work = SimpleNamespace(_known_tags_by_name={step.name: step})
    world = replace(
        world,
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                temporal_intent=TheoryTemporalIntent.SETUP_FIRST,
                has_traceback_finding=lambda _identity: False,
            ),
            temporal_requirements=(requirement,),
        ),
    )
    policy = ActPolicy(
        source=ActSource.TRACE,
        action_pairs=(("Link", True),),
        applied=(("Link", True),),
        expectation_exemption=ExpectationExemption.UNRESOLVED_EFFECT,
    )
    ordinary = Bearing(
        world_key=world.world_key,
        act=Pulse(policy),
        objective=BearingObjective(TargetSpec("Target", True)),
    )

    result = orientation._theory_setup_traceback(
        world,
        _options(_candidate("Link")),
        ordinary,
    )

    assert isinstance(result, NeedIntrascanTraceback)
    assert result.request.patch.dest == step.name
    assert result.request.patch.value == 98
    assert result.request.patch.boundary.execution_kind == "branch"
    assert result.request.patch.boundary.run_order == 77
    assert result.request.consumer_assignments == (("Link", True),)
    assert result.request.requirements == (requirement,)


def test_traceback_finding_selects_only_one_exact_program_stage_scan() -> None:
    """A finding authorizes its immediate producer scan, not its consumer steer."""
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    world = _world(compass)
    world.snapshot.update({"Link": False, "Step": 10})
    source = SimpleNamespace(world_key=world.world_key, scan_id=7)
    stage_write = SimpleNamespace(counterfactual=False)
    producer = SimpleNamespace(
        write=stage_write,
        enabling_requirements=(
            SimpleNamespace(tag="Link", value=False, source_kind="entry"),
            SimpleNamespace(tag="Step", value=10, source_kind="entry"),
        ),
    )
    finding = SimpleNamespace(
        theory_id=("theory", 1),
        version_id=("version", 1),
        source=source,
        identity=("traceback-finding", 1),
        witness=SimpleNamespace(
            traceback_step=SimpleNamespace(producer_traces=(producer,)),
        ),
        realization=SimpleNamespace(
            direct=False,
            witnessed=True,
            stage_scan=8,
            stage_write=stage_write,
            # This is retained research evidence only. It must not appear in
            # the selected act before Compass reads the stage landing.
            consumer_assignments=(("Link", True),),
        ),
    )
    world = replace(
        world,
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                theory_id=finding.theory_id,
                version_id=finding.version_id,
                source=source,
                traceback_findings=(finding,),
            ),
        ),
    )

    result = orientation._theory_intrascan_bearing(
        world,
        _options(),
        TargetSpec("Target", True),
    )

    assert isinstance(result, Bearing)
    assert isinstance(result.act, ProgramScan)
    assert result.act.expected_write is stage_write
    assert result.act.evidence_identity == finding.identity
    assert result.act.policy.applied == ()
    assert result.act.policy.local_progress is LocalProgressKind.INTRASCAN_STAGE
    assert result.prerequisites == ()

    landed_source = SimpleNamespace(world_key=("landed-world",), scan_id=8)
    landed = replace(
        world,
        world_key=landed_source.world_key,
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                theory_id=finding.theory_id,
                version_id=finding.version_id,
                source=landed_source,
                traceback_findings=(finding,),
            ),
        ),
    )
    assert (
        orientation._theory_intrascan_bearing(
            landed,
            _options(),
            TargetSpec("Target", True),
        )
        is None
    )


def test_direct_traceback_finding_selects_only_its_fresh_scan_start_steer() -> None:
    """A direct consumer finding must not compose the prior rejected act."""
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    world = _world(compass)
    source = SimpleNamespace(world_key=world.world_key, scan_id=7)
    consumer_write = SimpleNamespace(tag="Step", before=98, after=10)
    finding = SimpleNamespace(
        theory_id=("theory", 1),
        version_id=("version", 1),
        source=source,
        identity=("traceback-finding", "direct"),
        witness=SimpleNamespace(traceback_step=SimpleNamespace()),
        realization=SimpleNamespace(
            direct=True,
            witnessed=True,
            consumer_write=consumer_write,
            consumer_assignments=(("Link", True),),
        ),
    )
    requirement = object()
    world = replace(
        world,
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                theory_id=finding.theory_id,
                version_id=finding.version_id,
                source=source,
                traceback_findings=(finding,),
            ),
            temporal_requirements=(requirement,),
            steerable=frozenset({"Link"}),
        ),
    )

    result = orientation._theory_intrascan_bearing(
        world,
        _options(),
        TargetSpec("Target", True),
    )

    assert isinstance(result, Bearing)
    assert isinstance(result.act, IntrascanPulse)
    assert result.act.policy.applied == (("Link", True),)
    assert result.act.expected_write is consumer_write
    assert result.act.policy.local_progress is LocalProgressKind.INTRASCAN_DIRECT
    assert result.act.policy.pulse_horizon is PulseHorizon.ASSERTION_SCAN


def test_parent_requirement_is_conducted_inside_appeared_to_displacement_interval(
    monkeypatch,
) -> None:
    """The immutable occurrence stream qualifies a parent without live markers."""
    import pyrung.core.analysis.pilot.orientation as orientation

    appeared = SimpleNamespace(scan_id=7, ordinal=10)
    demanding = SimpleNamespace(
        kind="read",
        tag="Link",
        values=(False,),
        scan_id=7,
        ordinal=12,
        rung=("ErrorHandling", 5),
        execution_kind="subroutine",
        caller_rung=31,
        call_stack=("ErrorHandling",),
        depth=1,
        call_invocation=0,
        dynamic_address=(("ErrorHandling", 5), "subroutine", 31, ("ErrorHandling",), 1, 0, 8, 12),
    )
    displacement = SimpleNamespace(scan_id=7, ordinal=14)
    flow = SimpleNamespace(
        appeared=appeared,
        consumer_reads=(),
        displacement=displacement,
        obligations=(SimpleNamespace(tag="Link", value=False),),
    )
    compass = Compass()
    monkeypatch.setattr(
        Compass,
        "conductivity_front",
        lambda _self, _view: SimpleNamespace(flows=(flow,)),
    )

    assert orientation._theory_requirement_was_conducted(
        compass,
        object(),
        SimpleNamespace(demanding_occurrence=demanding),
    )


def test_temporal_retry_yields_to_conductivity_research_before_another_steer(
    monkeypatch,
) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    request = SimpleNamespace(reason="same stop with a changed deadline")
    world = _world(compass)
    world = replace(
        world,
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                temporal_intent=TheoryTemporalIntent.RETRY_THROUGH_DEADLINE,
            ),
            temporal_requirements=(object(),),
        ),
    )
    monkeypatch.setattr(orientation, "_build_candidates", lambda *_args: _options())
    monkeypatch.setattr(
        Compass,
        "conductivity_research",
        lambda _self, _view: request,
    )
    monkeypatch.setattr(
        orientation,
        "_theory_correction_composition",
        lambda *_args: pytest.fail("research must preempt correction composition"),
    )

    result = orientation._orient_read(compass, world, TargetSpec("Target", True))

    assert isinstance(result, NeedResearch)
    assert result.request is request
    assert result.rationale == request.reason
    assert result.orientation is not None
    assert result.orientation.candidates.options == ()


def test_temporal_retry_researches_after_pending_overlay_was_in_exact_attempt(
    monkeypatch,
) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    attempt_id = ("attempt", 2)
    request = SimpleNamespace(
        reason="the exercised correction exposed another stopped flow",
        comparison=SimpleNamespace(later_attempt_id=attempt_id),
    )
    correction = PilotRung("Correction", True, Int("Guard") == 1)
    correction_identity = orientation._rung_identity(correction)
    world = _world(compass)
    world = replace(
        world,
        state=SimpleNamespace(**{**vars(world.state), "pilot_rungs": (correction,)}),
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                temporal_intent=TheoryTemporalIntent.RETRY_THROUGH_DEADLINE,
                pending_overlay_identities=frozenset((correction_identity,)),
                conductivity_attempts=(
                    SimpleNamespace(
                        attempt_id=attempt_id,
                        pilot_rung_identities=(correction_identity,),
                    ),
                ),
            ),
            temporal_requirements=(object(),),
        ),
    )
    monkeypatch.setattr(orientation, "_build_candidates", lambda *_args: _options())
    monkeypatch.setattr(
        Compass,
        "conductivity_research",
        lambda _self, _view: request,
    )

    result = orientation._orient_read(compass, world, TargetSpec("Target", True))

    assert isinstance(result, NeedResearch)
    assert result.request is request


def test_untried_pending_overlay_still_preempts_conductivity_research() -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    correction = PilotRung("Correction", True, Int("Guard") == 1)
    correction_identity = orientation._rung_identity(correction)
    world = _world(Compass())
    world = replace(
        world,
        state=SimpleNamespace(**{**vars(world.state), "pilot_rungs": (correction,)}),
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                pending_overlay_identities=frozenset((correction_identity,)),
                conductivity_attempts=(
                    SimpleNamespace(
                        attempt_id=("attempt", 2),
                        pilot_rung_identities=(),
                    ),
                ),
            ),
        ),
    )
    request = SimpleNamespace(
        comparison=SimpleNamespace(later_attempt_id=("attempt", 2)),
    )

    assert orientation._untried_pending_theory_pairs(world, request) == (("Correction", True),)


def test_rejected_frontier_bearing_requests_one_program_owned_traceback_hop() -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    program_value = Int("ProgramValue")
    occurrence = SimpleNamespace(
        kind="read",
        tag=program_value.name,
        values=(100,),
        rung=(None, 7),
        execution_kind="rung",
        caller_rung=7,
        call_stack=(),
        depth=0,
        call_invocation=None,
        run_order=12,
        ordinal=19,
        branch_path=(),
    )
    requirement = SimpleNamespace(
        condition=Cmp(program_value.name, "!=", 100),
        demanding_occurrence=occurrence,
        operand_authority=OperandAuthority.PROGRAM_WRITTEN,
        selected_writer=(None, 7, ()),
        navigation_identity=("program-owned-prevention", program_value.name),
    )
    terminal_alias = SimpleNamespace(
        condition=requirement.condition,
        demanding_occurrence=occurrence,
        operand_authority=OperandAuthority.PROGRAM_WRITTEN,
        selected_writer=(None, 7, ()),
        navigation_identity=("terminal-prevention", program_value.name),
    )
    producer_goal = SimpleNamespace(identity=("producer-goal", "program-value"))
    frontier = SimpleNamespace(
        identity=("traceback-frontier", "program-value"),
        hop_identity=("intrascan-traceback-hop", "ProtectedStep", 98),
        parent_frontier_id=None,
        producer_goals=(producer_goal,),
    )
    view = SimpleNamespace(
        temporal_intent=TheoryTemporalIntent.RETRY_TOGETHER,
        trigger_act_identity=pulse_identity((("Reset", True),)),
        trigger_attempt_id=("attempt", "reset"),
        conductivity_attempts=(
            SimpleNamespace(
                attempt_id=("attempt", "reset"),
                act_identity=pulse_identity((("Reset", True),)),
                investigation_frontier_id=frontier.identity,
                producer_goal_id=producer_goal.identity,
                conductivity_observations=(
                    SimpleNamespace(
                        obligation=SimpleNamespace(tag="ProtectedStep"),
                        appeared=object(),
                        displacement=object(),
                    ),
                ),
            ),
        ),
        traceback_frontiers=(frontier,),
        current_traceback_frontiers=lambda: (frontier,),
        has_traceback_result=lambda _identity: False,
    )
    world = _world(Compass())
    world.snapshot[program_value.name] = 100
    world = replace(
        world,
        state=SimpleNamespace(
            **{
                **vars(world.state),
                "work": SimpleNamespace(_known_tags_by_name={program_value.name: program_value}),
            }
        ),
        context=replace(
            world.context,
            theory_view=view,
            temporal_trigger_requirements=(requirement, terminal_alias),
        ),
    )

    result = orientation._theory_intrascan_continuation_traceback(
        world,
        _options(),
    )

    assert isinstance(result, NeedIntrascanTraceback)
    assert result.request.consumer_assignments == (("Reset", True),)
    assert result.request.requirements == (requirement, terminal_alias)
    assert result.request.parent_frontier_id == frontier.identity
    assert result.request.parent_producer_goal_id == producer_goal.identity
    assert result.request.parent_attempt_id == ("attempt", "reset")
    assert result.request.patch.dest == program_value.name
    assert result.request.patch.value != 100
    assert result.request.patch.boundary.rung_id == RungId(None, 7)


def test_temporal_rearm_declares_one_assertion_scan() -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    world = _world(compass)
    world.snapshot["Request"] = True
    world = replace(
        world,
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                trigger_act_identity=pulse_identity((("Request", True),)),
            ),
            edge_tags=frozenset(("Request",)),
            resting={"Request": False},
        ),
    )

    result = orientation._theory_rearm_bearing(
        world,
        _options(),
        TargetSpec("Target", True),
    )

    assert isinstance(result, Bearing)
    assert result.act.policy.applied == (("Request", False),)
    assert result.act.policy.local_progress is LocalProgressKind.REARM
    assert result.act.policy.pulse_horizon is PulseHorizon.ASSERTION_SCAN


def test_corrected_frontier_rearms_the_exact_selected_transaction_pair() -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    configuration = ScanEntryConfiguration((("WatchdogPreset", 31),))
    source = SimpleNamespace(scan_id=5)
    actions = (("Request", True),)
    requirement = object()
    world = _world(compass)
    world.snapshot["Request"] = True
    world = replace(
        world,
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                temporal_intent=TheoryTemporalIntent.RETRY_THROUGH_DEADLINE,
                source=source,
                investigation_scope=SimpleNamespace(
                    frontier=source,
                    transaction_act_identity=pulse_identity(actions),
                    transaction_act_pairs=actions,
                    transaction_selected_pairs=actions,
                    transaction_rearmed=False,
                ),
                configurations=(configuration,),
                pending_configuration_identities=frozenset((configuration.identity,)),
            ),
            steerable=frozenset(("Request",)),
            edge_tags=frozenset(("Request",)),
            resting={"Request": False},
            temporal_requirements=(requirement,),
        ),
    )

    result = orientation._theory_rearm_bearing(
        world,
        _options(),
        TargetSpec("Target", True),
    )

    assert isinstance(result, Bearing)
    assert result.act.policy.applied == (("Request", False),)
    assert result.act.policy.local_progress is LocalProgressKind.REARM
    assert result.act.policy.pulse_horizon is PulseHorizon.ASSERTION_SCAN


def test_temporal_rearm_preserves_the_original_level_command() -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    world = _world(compass)
    world.snapshot.update({"Command": False, "Request": True})
    world = replace(
        world,
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                trigger_act_identity=pulse_identity((("Command", True), ("Request", True))),
            ),
            edge_tags=frozenset(("Request",)),
            resting={"Command": False, "Request": False},
        ),
    )

    result = orientation._theory_rearm_bearing(
        world,
        _options(),
        TargetSpec("Target", True),
    )

    assert isinstance(result, Bearing)
    assert result.act.policy.applied == (("Command", True), ("Request", False))
    assert result.act.policy.local_progress is LocalProgressKind.REARM


def test_temporal_rearm_releases_an_asserted_competing_command(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    writes = {
        "Reset": {"CommandCode": 1, "RequestReady": True},
        "Clear": {"CommandCode": 9, "RequestReady": True},
        "Request": {},
    }
    monkeypatch.setattr(
        orientation,
        "_button_writes",
        lambda _context, tag, _snapshot: writes[tag],
    )
    compass = Compass()
    world = _world(compass)
    world.snapshot.update({"Reset": False, "Clear": True, "Request": True})
    world = replace(
        world,
        context=replace(
            world.context,
            theory_view=SimpleNamespace(
                trigger_act_identity=pulse_identity((("Reset", True), ("Request", True))),
            ),
            pdg=SimpleNamespace(rung_nodes=()),
            program=object(),
            steerable=frozenset(("Reset", "Clear", "Request")),
            edge_tags=frozenset(("Request",)),
            resting={"Reset": False, "Clear": False, "Request": False},
        ),
    )

    result = orientation._theory_rearm_bearing(
        world,
        _options(),
        TargetSpec("Target", True),
    )

    assert isinstance(result, Bearing)
    assert result.act.policy.applied == (
        ("Request", False),
        ("Reset", True),
        ("Clear", False),
    )


def _world(compass: Compass) -> OrientationWorld:
    from pyrung.core.analysis.pilot.trace import TraceNode

    context = _Context(compass, resting={})
    return OrientationWorld(
        world_key=("world",),
        snapshot={"Target": False},
        frame=SimpleNamespace(
            key=("world",),
            tree=TraceNode("Target", True, satisfied=False),
        ),
        state=SimpleNamespace(
            key_config=None,
            pilot_rungs=(),
            work=SimpleNamespace(),
        ),
        context=context,
    )


def test_proof_rejection_is_scoped_to_the_exact_input_context() -> None:
    from pyrung.core.analysis.pilot.compass import EvidenceScope
    from pyrung.core.analysis.pilot.orientation import _act_preserves_requirements

    world = _world(Compass())
    world.snapshot["Guard"] = False
    act = Pulse(
        ActPolicy(
            source=ActSource.TRACE,
            action_pairs=(("Command", True),),
            applied=(("Command", True),),
            expectation_exemption=ExpectationExemption.UNRESOLVED_EFFECT,
        )
    )
    rejected_scope = EvidenceScope.capture(world.world_key, world.snapshot.items())
    world.state.proof_rejected_acts = {(rejected_scope, act_identity(act))}

    assert not _act_preserves_requirements(world, act)

    corrected = replace(world, snapshot={**world.snapshot, "Guard": True})
    assert _act_preserves_requirements(corrected, act)


def test_nonpromising_executable_policies_have_typed_exemptions() -> None:
    assert Dwell().policy.expectation is None
    assert Dwell().policy.expectation_exemption is ExpectationExemption.AMBIENT_TERMINAL


def test_candidate_read_exposes_only_owned_receipts() -> None:
    flattened_aliases = {
        "active_trace_actions",
        "trace_actions",
        "trace_action_details",
        "route_plan",
        "route_candidates",
        "route_co_actions",
        "candidates",
        "wait_prescribed",
        "wait_reason",
        "heading",
        "advance_boundary",
        "advance_condition",
        "prescribed_batch",
        "prerequisite_pilot_rungs",
        "held_command_tags",
        "stuck_reason",
        "completion_frontier",
        "program_step",
    }

    assert flattened_aliases.isdisjoint(vars(CandidateRead))


def test_inferred_root_routes_are_read_together_without_commitment(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation
    from pyrung.core.analysis.pilot.trace import TraceChoice

    route_a = TraceChoice(id="route-a", label="A", route=("A",))
    route_b = TraceChoice(id="route-b", label="B", route=("B",))
    tree_a = object()
    tree_b = object()
    monkeypatch.setattr(
        orientation,
        "_route_rejected_actions",
        lambda _tree, _world, _exclusions: None,
    )
    monkeypatch.setattr(
        orientation,
        "rank_trace_choices",
        lambda *_args, **_kwargs: (
            (route_a, route_b),
            ((route_a, tree_a), (route_b, tree_b)),
        ),
    )

    routes = orientation._read_route_trees(
        _world(Compass()),
        TargetSpec("Target", True),
        NavigationConstraints(),
    )

    assert routes == ((route_a, tree_a), (route_b, tree_b))


def test_assembled_route_receipt_is_shared_by_world_and_context() -> None:
    from pyrung.core.analysis.pilot.orientation import _assemble_world
    from pyrung.core.analysis.pilot.trace import TraceChoice, TraceNode
    from pyrung.core.analysis.pilot.world_key import _StateKeyConfig

    route = TraceChoice(id="route-a", label="A", route=("A",))
    assembled = _assemble_world(
        _world(Compass()),
        route,
        TraceNode("Target", True, satisfied=False),
        _StateKeyConfig(
            stateful_names=("Target",),
            done_specs=(),
            threshold_vector_specs=(),
            acc_indices=frozenset(),
        ),
    )

    assert assembled.root_route is route
    assert assembled.context.route is route


def test_orient_passes_blocked_actions_to_candidate_admission(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    blocked = frozenset({("Blocked", True)})
    seen: list[frozenset] = []

    def _read(_frame, _state, context):
        seen.append(context.blocked_actions)
        return _options(stuck_reason="trace_empty")

    monkeypatch.setattr(orientation, "_build_candidates", _read)
    compass = Compass()

    compass.orient(
        _world(compass),
        TargetSpec("Target", True),
        NavigationConstraints(blocked_actions=blocked),
    )

    assert seen == [blocked]


def test_orient_returns_one_act_without_route_suffix(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    first = _candidate("First")
    monkeypatch.setattr(orientation, "_build_candidates", lambda *_args: _options(first))
    monkeypatch.setattr(
        orientation,
        "_candidate_applied",
        lambda option, _options, _context: (option.pair,),
    )

    world = _world(compass)
    result = compass.orient(
        world,
        TargetSpec("Target", True),
        NavigationConstraints(),
    )

    assert isinstance(result, Bearing)
    assert isinstance(result.act, Pulse)
    assert result.act.action == ("First", True)
    assert result.act.policy.source is ActSource.TRACE
    assert result.act.policy.action_pairs == (("First", True),)
    assert result.act.policy.applied == (("First", True),)
    assert not hasattr(result.act, "option")
    assert result.objective.target == TargetSpec("Target", True)
    assert result.objective.frontier == ()
    assert not hasattr(result, "path")
    assert not hasattr(result, "candidates")
    assert result.orientation is not None
    assert result.orientation.world.frame is world.frame
    assert result.orientation.candidates.options == (first,)
    assert not hasattr(result.orientation, "readings")


def test_learned_batch_materializes_the_common_policy_once(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    actions = (("First", True), ("Gate", True))
    monkeypatch.setattr(
        orientation,
        "_build_candidates",
        lambda *_args: _options(prescribed_batch=actions),
    )

    compass = Compass()
    result = compass.orient(
        _world(compass),
        TargetSpec("Target", True),
        NavigationConstraints(),
    )

    assert isinstance(result, Bearing)
    assert isinstance(result.act, BatchPulse)
    assert result.act.policy.source is ActSource.LEARNED_BATCH
    assert result.act.policy.action_pairs == actions
    assert result.act.policy.applied == actions
    assert result.act.policy.observe_label == "batch"
    assert result.act.policy.target_observe_label == "batch-target"
    assert result.act.policy.learned_prescribed
    assert not result.act.policy.chase_regression_causes


def test_learned_source_names_define_batch_identity() -> None:
    actions = (("First", True), ("Gate", True))
    act = BatchPulse(
        ActPolicy(
            source=ActSource.LEARNED_BATCH,
            action_pairs=actions,
            applied=actions,
        )
    )

    assert ActSource.LEARNED_ACTION.value == "learned_action"
    assert ActSource.LEARNED_BATCH.value == "learned_batch"
    assert act_identity(act) == ("pulse", actions)


def test_crossing_branch_materializes_one_atomic_verified_act(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    branch = CrossingBatchRead(
        actions=(("A", 1), ("B", 1)),
        fidelity=CrossingFidelity(
            constraints=("A == 1", "B == 1"),
            reason="grouped predecessor",
            verify_required=True,
            exact=None,
            proposed=True,
        ),
    )
    monkeypatch.setattr(
        orientation,
        "_build_candidates",
        lambda *_args: _options(crossing_batches=(branch,)),
    )

    compass = Compass()
    result = compass.orient(
        _world(compass),
        TargetSpec("Target", True),
        NavigationConstraints(),
    )

    assert isinstance(result, Bearing)
    assert isinstance(result.act, BatchPulse)
    assert result.act.actions == (("A", 1), ("B", 1))
    assert result.act.policy.applied == result.act.actions
    assert result.act.policy.source is ActSource.CROSSING
    assert result.act.crossing is not None
    assert result.act.crossing.verify_required is True
    assert result.act.crossing.proposed is True


def test_crossing_batch_nogood_identity_is_canonical_and_falls_back(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    first = CrossingBatchRead(
        actions=(("B", 1), ("A", 1)),
        fidelity=CrossingFidelity(
            constraints=(),
            reason="first",
            verify_required=True,
            exact=None,
            proposed=True,
        ),
    )
    sibling = replace(
        first,
        actions=(("D", 1), ("C", 1)),
        fidelity=replace(first.fidelity, reason="sibling"),
    )
    monkeypatch.setattr(
        orientation,
        "_build_candidates",
        lambda *_args: _options(crossing_batches=(first, sibling)),
    )

    compass = Compass()
    first_result = compass.orient(
        _world(compass),
        TargetSpec("Target", True),
        NavigationConstraints(),
    )
    assert isinstance(first_result, Bearing)
    assert isinstance(first_result.act, BatchPulse)
    first_identity = act_identity(first_result.act)
    assert first_identity == ("pulse", (("A", 1), ("B", 1)))
    assert first_result.act.policy.regression_nogoods == frozenset()
    assert first_identity == act_identity(
        BatchPulse(
            ActPolicy(
                source=ActSource.LEARNED_BATCH,
                action_pairs=(("A", 1), ("B", 1)),
                applied=(("A", 1), ("B", 1)),
            )
        )
    )
    joint_pulse = Pulse(
        ActPolicy(
            source=ActSource.TRACE,
            action_pairs=(("A", 1),),
            applied=(("B", 1), ("A", 1)),
        )
    )
    assert act_identity(joint_pulse) == first_identity
    assert joint_pulse.policy.regression_nogoods == frozenset()

    compass, _changed = compass.apply((ActionNogoodObservation(("world",), first_identity),))
    second_result = compass.orient(
        _world(compass),
        TargetSpec("Target", True),
        NavigationConstraints(),
    )

    assert isinstance(second_result, Bearing)
    assert isinstance(second_result.act, BatchPulse)
    assert second_result.act.actions == sibling.actions
    assert compass.knowledge.nogood_pairs(("world",)) == frozenset()


def test_coast_identity_is_operational_and_order_insensitive() -> None:
    def _coast(from_value, target_value, applied):
        return Coast(
            "bearing",
            ActPolicy(
                source=ActSource.ROUTE,
                applied=applied,
                heading=ChannelHeading(
                    "Inner",
                    4,
                    route=RouteEdgeContext("Outer", from_value, target_value),
                ),
            ),
        )

    baseline = act_identity(_coast(1, 7, (("B", 2), ("A", 1))))
    assert baseline == act_identity(_coast(99, 7, (("A", 1), ("B", 2))))
    assert baseline != act_identity(_coast(1, 8, (("A", 1), ("B", 2))))


def test_dwell_identity_normalizes_applied_overlay_order() -> None:
    first = Dwell(ActPolicy(ActSource.TERMINAL, applied=(("B", 2), ("A", 1))))
    second = Dwell(ActPolicy(ActSource.TERMINAL, applied=(("A", 1), ("B", 2))))

    assert act_identity(first) == act_identity(second)


def test_awaited_action_candidate_recording_keeps_route_diagnostic_distinct() -> None:
    policy = ActPolicy(
        source=ActSource.AWAITED_ACTION,
        action_pairs=(("Acknowledge", True),),
    )

    payload = _candidate_payload(policy)

    assert payload["awaited_action_prescribed"] is True
    assert payload["route_prescribed"] is False


def test_live_operation_owns_its_successor_residual_after_boundary_crosses() -> None:
    from pyrung.core.analysis.pilot.orientation import _current_work_evidence
    from pyrung.core.analysis.pilot.trace import TraceNode

    frame = SimpleNamespace(
        snap={"Heat_tmr_Acc": 2, "Heat_CurStep": 2},
        tree=TraceNode(
            "Target",
            True,
            children=[
                TraceNode(
                    "Heat_CurStep",
                    3,
                    satisfied=False,
                    children=[
                        TraceNode(
                            "ContinueHeat",
                            True,
                            satisfied=False,
                            is_steerable=True,
                        )
                    ],
                )
            ],
        ),
    )
    state = SimpleNamespace(
        pilot_rungs=(),
        pending_departure=None,
        committed_acts=(
            SimpleNamespace(
                context=SimpleNamespace(
                    policy=SimpleNamespace(
                        motion=SimpleNamespace(
                            value="coast-to-bearing",
                            is_coast=True,
                        ),
                    ),
                    execution=SimpleNamespace(
                        before_snap={"Heat_tmr_Acc": 0, "Heat_CurStep": 1},
                        after_snap={"Heat_tmr_Acc": 2, "Heat_CurStep": 2},
                    ),
                )
            ),
        ),
        earned_work=None,
    )

    assert _current_work_evidence(frame, state, None) == ("operation:Heat_CurStep",)


def test_open_operation_maintenance_owns_before_a_sibling_intervention(
    monkeypatch,
) -> None:
    """Keeping live work running is a continuation, not an actionless fallback."""
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    target = TargetSpec("Target", True)
    first = SimpleNamespace(name="live")
    second = SimpleNamespace(name="sibling")
    maintain = Bearing(
        ("world",),
        Dwell(),
        BearingObjective(target),
    )
    destroy = Bearing(
        ("world",),
        Pulse(
            ActPolicy(
                source=ActSource.TRACE,
                action_pairs=(("Destroy", True),),
                applied=(("Destroy", True),),
                nogood_pair=("Destroy", True),
            )
        ),
        BearingObjective(target),
    )
    monkeypatch.setattr(
        orientation,
        "_orient_read",
        lambda _compass, world, _target: maintain if world is first else destroy,
    )

    selected, results = orientation._read_group(
        compass,
        (first, second),
        target,
        maintenance_owns=True,
    )

    assert selected is maintain
    assert results == (maintain,)


def test_bearing_preserves_downstream_channel_goal(monkeypatch) -> None:
    """A Boolean target keeps the state-register goal Orientation traced for it."""
    import pyrung.core.analysis.pilot.orientation as orientation
    from pyrung.core.analysis.pilot.trace import TraceNode

    compass = Compass()
    first = _candidate("First")
    monkeypatch.setattr(orientation, "_build_candidates", lambda *_args: _options(first))
    monkeypatch.setattr(
        orientation,
        "_candidate_applied",
        lambda option, _options, _context: (option.pair,),
    )
    state_goal = TraceNode(
        "State",
        17,
        children=[TraceNode("CompleteCommand", True, is_steerable=True)],
    )
    complete_goal = TraceNode("Complete", True, children=[state_goal])
    world = _world(compass)
    world.snapshot.update({"Complete": False, "State": 6})
    world.frame.tree = complete_goal

    result = compass.orient(
        world,
        TargetSpec("Complete", True),
        NavigationConstraints(),
    )

    assert isinstance(result, Bearing)
    assert result.objective.target == TargetSpec("Complete", True)
    assert result.objective.channel_goals("State") == (17,)


def test_coast_act_carries_only_immediate_heading() -> None:
    act = Coast(
        "bearing",
        ActPolicy(
            source=ActSource.ROUTE,
            heading=ChannelHeading("State", 2),
        ),
    )

    assert act.policy.heading is not None
    assert act.policy.heading.channel_tag == "State"
    assert act.policy.heading.target_value == 2
    assert {
        "channel_tag",
        "target_value",
        "boundary",
        "route_channel_tag",
        "route_from_value",
        "route_target_value",
    }.isdisjoint(vars(Coast))
    assert not hasattr(act, "option")
    assert not hasattr(act, "path")


def test_orient_carries_wait_heading_and_outer_route_context_whole(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    route = RouteEdgeContext("OuterState", 6, 16)
    heading = ChannelHeading("InnerAcc", 5, boundary=object(), route=route)
    read = WaitRead(
        WaitPrescription(
            heading,
            "owned wait",
            frontier=(("PressableLever", True),),
            landing_receipt_authority=LandingReceiptAuthority.PROGRAM_STEP,
        )
    )
    monkeypatch.setattr(
        orientation,
        "_build_candidates",
        lambda *_args: _options(wait=read),
    )

    compass = Compass()
    world = _world(compass)
    result = compass.orient(
        world,
        TargetSpec("Target", True),
        NavigationConstraints(),
    )

    assert isinstance(result, Bearing)
    assert isinstance(result.act, Coast)
    assert result.act.policy.heading is heading
    assert result.act.policy.heading.channel_tag == "InnerAcc"
    assert result.act.policy.heading.route is not None
    assert result.act.policy.heading.route.channel_tag == "OuterState"
    assert result.act.policy.heading.route.from_value == 6
    assert result.act.policy.heading.route.target_value == 16
    assert result.act.policy.landing_receipt_authority is LandingReceiptAuthority.PROGRAM_STEP
    assert result.objective.frontier == (("PressableLever", True),)
    assert result.orientation is not None
    assert result.orientation.world.frame is world.frame
    assert not hasattr(world.frame, "completion_frontier")


def test_combined_nonbearing_assembles_every_alternative_frontier() -> None:
    from pyrung.core.analysis.pilot.orientation import _combined_nonbearing

    first = Stuck(
        world_key=("world",),
        reason_code="trace_empty",
        frontier=(("FirstLever", True),),
    )
    second = Stuck(
        world_key=("world",),
        reason_code="trace_empty",
        frontier=(("SecondLever", 2), ("FirstLever", True)),
    )

    result = _combined_nonbearing((first, second))

    assert isinstance(result, Stuck)
    assert result.frontier == (("FirstLever", True), ("SecondLever", 2))


def test_orient_returns_need_probe_then_stuck_after_budget(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    monkeypatch.setattr(
        orientation,
        "_build_candidates",
        lambda *_args: _options(stuck_reason="trace_opaque"),
    )
    compass = Compass()
    result = compass.orient(
        _world(compass),
        TargetSpec("Target", True),
        NavigationConstraints(),
    )
    assert isinstance(result, NeedProbe)

    for _ in range(2):
        compass, _ = compass.apply((ProbeExhaustedObservation(("world",)),))
    result = compass.orient(
        _world(compass),
        TargetSpec("Target", True),
        NavigationConstraints(),
    )
    assert isinstance(result, Stuck)
    assert result.reason_code == "trace_opaque"


def test_orient_returns_stuck_after_budget_with_route_receipt(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation
    from pyrung.core.analysis.pilot.trace import TraceChoice

    active = TraceChoice(id="route-a", label="A", route=("A",))
    compass = Compass()
    for _ in range(2):
        compass, _ = compass.apply((ProbeExhaustedObservation(("world",)),))
    monkeypatch.setattr(
        orientation,
        "_build_candidates",
        lambda *_args: _options(stuck_reason="trace_opaque"),
    )
    world = replace(_world(compass), root_route=active)

    result = compass.orient(
        world,
        TargetSpec("Target", True),
        NavigationConstraints(),
    )

    assert isinstance(result, Stuck)
    assert result.reason_code == "trace_opaque"
    assert result.evidence == ("probe budget 2",)


def test_orient_does_not_mutate_world_context_or_knowledge(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    compass = Compass()
    world = _world(compass)
    before_snapshot = dict(world.snapshot)
    before_context = dict(vars(world.context))
    monkeypatch.setattr(
        orientation,
        "_build_candidates",
        lambda *_args: _options(stuck_reason="trace_empty"),
    )

    compass.orient(world, TargetSpec("Target", True), NavigationConstraints())

    assert world.snapshot == before_snapshot
    assert vars(world.context) == before_context
    assert len(compass.knowledge.entries) == 0
    assert len(compass.knowledge.act_nogoods) == 0


def test_rejected_act_knowledge_forces_fresh_next_orientation(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.orientation as orientation

    first = _candidate("First")
    second = _candidate("Second")
    monkeypatch.setattr(
        orientation,
        "_build_candidates",
        lambda *_args: _options(first, second),
    )
    monkeypatch.setattr(
        orientation,
        "_candidate_applied",
        lambda option, _options, _context: (option.pair,),
    )
    compass = Compass()
    result = compass.orient(
        _world(compass),
        TargetSpec("Target", True),
        NavigationConstraints(),
    )
    assert isinstance(result, Bearing)
    assert result.act.action == first.pair

    compass, changed = compass.apply(
        (ActionNogoodObservation(("world",), act_identity(result.act)),)
    )
    assert changed
    next_result = compass.orient(
        _world(compass),
        TargetSpec("Target", True),
        NavigationConstraints(),
    )
    assert isinstance(next_result, Bearing)
    assert next_result.act.action == second.pair


def test_trace_rejections_require_exact_singleton_pulse_artifact() -> None:
    from pyrung.core.analysis.pilot.orientation import _exact_rejected_actions

    first = ("First", True)
    second = ("Second", True)
    exclusions = frozenset(
        {
            ("pulse", (first,)),
            ("pulse", (second, ("Gate", True))),
            ("pair", ("Direct", True)),
        }
    )

    assert _exact_rejected_actions(exclusions) == frozenset({first})


def test_joint_pulse_nogood_does_not_reject_its_primary_pair() -> None:
    primary = ("Start", True)
    gate_a = ("GateA", True)
    gate_b = ("GateB", True)
    world = ("world",)
    rejected = pulse_identity((primary, gate_a))

    compass, changed = Compass().apply((ActionNogoodObservation(world, rejected),))

    assert changed
    assert compass.knowledge.act_is_nogood(world, rejected)
    assert not compass.knowledge.act_is_nogood(
        world,
        pulse_identity((primary, gate_b)),
    )
    assert primary not in compass.knowledge.nogood_pairs(world)
    assert not compass.knowledge.act_is_nogood(("other-world",), rejected)

    singleton_world = ("singleton-world",)
    direct_pair_world = ("direct-pair-world",)
    compass, _ = compass.apply(
        (
            ActionNogoodObservation(
                singleton_world,
                pulse_identity((primary,)),
            ),
            ActionNogoodObservation(direct_pair_world, ("pair", primary)),
        )
    )
    assert primary in compass.knowledge.nogood_pairs(singleton_world)
    assert primary in compass.knowledge.nogood_pairs(direct_pair_world)


def test_stale_bearing_cannot_execute() -> None:
    from pyrung.core.analysis.pilot.steer import StaleBearingError, execute
    from pyrung.core.analysis.pilot.world_key import _StateKeyConfig

    state = SimpleNamespace(
        key_config=_StateKeyConfig(
            stateful_names=("X",),
            done_specs=(),
            threshold_vector_specs=(),
            acc_indices=frozenset(),
        ),
        work=SimpleNamespace(state=SimpleNamespace(tags={"X": 1})),
        pilot_rungs=(),
    )
    world = OrientationWorld(
        world_key=("stale",),
        snapshot={"X": 1},
        frame=SimpleNamespace(),
        state=state,
        context=SimpleNamespace(),
    )
    bearing = Bearing(
        world_key=("stale",),
        act=Pulse(
            ActPolicy(
                source=ActSource.TRACE,
                action_pairs=(("Cmd", True),),
                applied=(("Cmd", True),),
                nogood_pair=("Cmd", True),
            )
        ),
        objective=BearingObjective(TargetSpec("Target", True)),
    )
    with pytest.raises(StaleBearingError):
        execute(bearing, world)


def test_driver_has_no_direct_option_builder_or_probe_policy() -> None:
    from pathlib import Path

    import pyrung.core.analysis.pilot.pilot as pilot

    source = Path(pilot.__file__).read_text(encoding="utf-8")
    assert "_build_candidates" not in source
    assert "_orient_escalate_skiff" not in source
    assert "from pyrung.core.analysis.pilot.options" not in source


def test_production_pilot_forks_only_through_rung_aware_helper() -> None:
    import ast
    from pathlib import Path

    import pyrung.core.analysis.pilot as pilot_package

    class ForkVisitor(ast.NodeVisitor):
        def __init__(self, filename: str) -> None:
            self.filename = filename
            self.functions: list[str] = []
            self.calls: list[tuple[str, str, str | None]] = []

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function(node)

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Attribute) and node.func.attr == "fork":
                receiver = node.func.value.id if isinstance(node.func.value, ast.Name) else None
                owner = self.functions[-1] if self.functions else "<module>"
                self.calls.append((self.filename, owner, receiver))
            self.generic_visit(node)

    package_dir = Path(pilot_package.__file__).parent
    direct_forks: list[tuple[str, str, str | None]] = []
    for path in sorted(package_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = ForkVisitor(path.name)
        visitor.visit(tree)
        direct_forks.extend(visitor.calls)

    assert direct_forks == [("overlay.py", "fork_with_pilot_rungs", "source")]
