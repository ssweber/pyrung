from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

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
    NavigationConstraints,
    NeedProbe,
    OrientationWorld,
    Pulse,
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
from pyrung.core.analysis.pilot.recording import _candidate_payload


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
    opaque_loop: frozenset = frozenset()
    pipeline_internal_tags: frozenset = frozenset()
    domain_prior: object = None


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


def _world(compass: Compass) -> OrientationWorld:
    from pyrung.core.analysis.pilot.trace import TraceNode

    context = _Context(compass)
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
