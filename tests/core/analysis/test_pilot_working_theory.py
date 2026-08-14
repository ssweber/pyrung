"""Pure lifecycle contracts for the immutable WorkingTheory ledger."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import FrozenInstanceError, fields, is_dataclass, replace
from typing import Any

import pytest

from pyrung import PLC
from pyrung.core.analysis.pilot.compass import Compass
from pyrung.core.analysis.pilot.conductivity import (
    ConductivityProgress,
    ConductivityReach,
)
from pyrung.core.analysis.pilot.effects import (
    EffectObligationSnapshot,
    EffectObservationSnapshot,
    EffectOccurrenceSnapshot,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    Bearing,
    NavigationAct,
    OrientationRead,
)
from pyrung.core.analysis.pilot.options import CandidateRead
from pyrung.core.analysis.pilot.overlay import PilotRung
from pyrung.core.analysis.pilot.trace import TraceChoice
from pyrung.core.analysis.pilot.types import _Checkpoint, _World
from pyrung.core.analysis.pilot.working_theory import (
    AbandonTheory,
    AdvanceTheory,
    ComposeTheoryCorrection,
    ConductivityResearchFinding,
    OpenSuccessor,
    OpenTheory,
    ProveTheory,
    RecordConductivityResearch,
    RecordTheoryAttempt,
    RefineTheory,
    TheoryAttemptDisposition,
    TheoryBoundaryIdentity,
    TheoryClaim,
    TheoryFirstEdgeExclusion,
    TheoryInvariantError,
    TheoryObjectiveSnapshot,
    TheoryObligationSnapshot,
    TheoryPhaseKind,
    TheoryPhaseReceipt,
    TheoryRequirementSnapshot,
    TheoryState,
    TheoryTemporalIntent,
    TheoryTermination,
    active_theory_correction_rung_identities,
    assert_detached_theory_value,
    reduce_theory,
    temporal_need_request,
    theory_view,
)


def _boundary(label: str, scan: int) -> TheoryBoundaryIdentity:
    return TheoryBoundaryIdentity(
        world_key=("world", label),
        scan_id=scan,
        checkpoint_token=("checkpoint", label),
        execution_owner_token=("execution", label),
        occurrence_identity=("occurrence", label),
    )


def _claim(*, target: str = "stepper_complete", source: str = "source") -> TheoryClaim:
    boundary = _boundary(source, 0)
    return TheoryClaim(
        source=boundary,
        objective=TheoryObjectiveSnapshot(target, True),
        obligations=(
            TheoryObligationSnapshot(
                tag="consumer_ready",
                value=True,
                producer=("producer", 0),
                consumer=("consumer", 1),
                required_shape=(("consumer_ready", True),),
                boundary=("consumer", 1),
                terminal_target=False,
                polarity="produce",
                occurrence_selector=("call", 0),
            ),
        ),
        selected_boundary=boundary,
    )


def _requirement(label: str) -> TheoryRequirementSnapshot:
    return TheoryRequirementSnapshot(
        semantic_identity=("requirement", label),
        condition_identity=("condition", label, True),
        demanding_occurrence=("consumer", label, 2),
        deadline_occurrence=("producer", label, 1),
        selected_writer=("writer", label, 0),
        operand_authority="producer",
        source_world_key=("world", "source"),
        source_scan=0,
        checkpoint_token=("checkpoint", "source"),
        execution_owner_token=("execution", "source"),
        phase="assertion",
        status="active",
        provenance="projection",
        scope=("call", label, 0),
    )


def _open_fact(*, identity: str = "open-1", claim: str = "stepper-complete") -> OpenTheory:
    return OpenTheory(
        claim=_claim(target=claim),
        opening_identity=(identity,),
        remaining_budget=8,
    )


def _opened() -> tuple[TheoryState, Any, Any]:
    state = reduce_theory(TheoryState(), _open_fact())
    theory_id = state.active_theory_id
    assert theory_id is not None
    return state, theory_id, state.ledger.theories[theory_id].current_version_id


def _attempt(
    theory_id: Any,
    version_id: Any,
    *,
    transition: str,
    actions: tuple[tuple[str, bool], ...],
    disposition: TheoryAttemptDisposition = TheoryAttemptDisposition.REJECTED_EXACT,
    first_edge_identity: tuple[Any, ...] | None = None,
) -> RecordTheoryAttempt:
    source = _boundary("source", 0)
    execution_owner = ("attempt-owner", transition)
    occurrence = ("attempt-occurrence", transition)
    return RecordTheoryAttempt(
        theory_id=theory_id,
        version_id=version_id,
        attempt_identity=(transition, execution_owner, occurrence),
        source=source,
        execution_owner_token=execution_owner,
        occurrence_evidence=occurrence,
        act_identity=actions,
        pilot_rung_identities=(),
        disposition=disposition,
        evidence=(("scan", 1),),
        first_edge_identity=first_edge_identity,
    )


def test_open_creates_one_active_theory_and_initial_version() -> None:
    state = reduce_theory(TheoryState(), _open_fact())

    assert state.active_theory_id is not None
    assert len(state.ledger.claims) == 1
    assert len(state.ledger.versions) == 1
    assert len(state.ledger.theories) == 1
    assert not state.ledger.attempts
    assert not state.ledger.receipts
    assert not state.ledger.tombstones


def test_research_finding_records_exact_evidence_without_advancing_the_world() -> None:
    state, theory_id, version_id = _opened()
    earlier = _attempt(
        theory_id,
        version_id,
        transition="earlier-stop",
        actions=(("Reconnect", True),),
    )
    source = _boundary("source", 0)
    displacement = EffectOccurrenceSnapshot(
        kind="write",
        ordinal=8,
        scan_id=1,
        run_order=3,
        call_invocation=None,
        rung=(None, 12),
        execution_kind="rung",
        caller_rung=12,
        call_stack=(),
        depth=0,
        enabled=True,
        tag="ResetDone",
        values=(False,),
    )
    stopping_read = replace(
        displacement,
        kind="read",
        ordinal=7,
        tag="WatchdogDone",
        values=(False,),
    )
    obligation = EffectObligationSnapshot(
        tag="SequenceStep",
        value=40,
        producer=(None, 10, ()),
        consumer=(None, 11, ()),
        required_shape=(("SequenceStep", 40),),
        boundary=("consumer", 11),
    )
    later = replace(
        _attempt(
            theory_id,
            version_id,
            transition="later-stop",
            actions=(("Reconnect", True),),
        ),
        conductivity_observations=(
            EffectObservationSnapshot(
                disposition="DISPLACED",
                obligation=obligation,
                displacement=displacement,
                observed_reads=(stopping_read,),
            ),
        ),
    )
    state = reduce_theory(state, earlier)
    state = reduce_theory(state, later)
    finding = ConductivityResearchFinding(
        theory_id=theory_id,
        version_id=version_id,
        source=source,
        comparison_identity=(
            "conductivity-comparison",
            earlier.attempt_identity,
            later.attempt_identity,
            "same-stop",
        ),
        compared_attempt_ids=(earlier.attempt_identity, later.attempt_identity),
        displacement=displacement,
        enabling_reads=(stopping_read,),
        requirement_drift_identities=(("requirement-drift", "preset", 10, 20),),
    )
    fact = RecordConductivityResearch(finding)
    progress_id = state.ledger.theories[theory_id].current_progress_id

    recorded = reduce_theory(state, fact)

    theory = recorded.ledger.theories[theory_id]
    view = theory_view(recorded)
    assert theory.current_progress_id == progress_id
    assert theory.research_finding_ids == (finding.identity,)
    assert finding.identity[0] == "conductivity-research-finding"
    assert len(finding.identity[1]) == 64
    assert recorded.ledger.research_findings[finding.identity] is finding
    assert view is not None
    assert view.research_findings == (finding,)
    assert reduce_theory(recorded, fact) is recorded
    with pytest.raises(FrozenInstanceError):
        finding.source = _boundary("future", 1)  # ty: ignore[invalid-assignment]


def test_no_scan_composition_moves_the_progress_tip_to_the_composed_world() -> None:
    state, theory_id, version_id = _opened()
    source = _boundary("source", 0)
    composed = _boundary("source-with-correction", 0)
    fact = ComposeTheoryCorrection(
        theory_id=theory_id,
        version_id=version_id,
        source=source,
        composed_source=composed,
        requirement_identities=(("requirement", "preset"),),
        pilot_rung_identities=(("PresetMs", 11),),
        composition_identity=("compose", "preset"),
    )

    state = reduce_theory(state, fact)
    view = theory_view(state)

    assert view is not None
    assert view.source == composed
    theory = state.ledger.theories[theory_id]
    progress = state.ledger.progress[theory.current_progress_id]
    assert progress.provisional_tip == composed
    assert progress.provisional_tip.scan_id == source.scan_id
    assert progress.phase_receipts[-1].kind is TheoryPhaseKind.CORRECTION_COMPOSITION

    replacement = _boundary("source-with-replacement", 0)
    replacement_identity = ("PresetMs", 21)
    state = reduce_theory(
        state,
        ComposeTheoryCorrection(
            theory_id=theory_id,
            version_id=version_id,
            source=composed,
            composed_source=replacement,
            requirement_identities=(("requirement", "preset-20"),),
            pilot_rung_identities=(replacement_identity,),
            composition_identity=("compose", "preset-20"),
            superseded_pilot_rung_identities=(("PresetMs", 11),),
        ),
    )
    theory = state.ledger.theories[theory_id]
    progress = state.ledger.progress[theory.current_progress_id]

    assert progress.provisional_tip == replacement
    assert progress.phase_receipts[-1].superseded_pilot_rung_identities == (
        ("PresetMs", 11),
    )
    assert active_theory_correction_rung_identities(state) == frozenset(
        (replacement_identity,)
    )


def test_multiple_attempts_share_one_version_and_duplicate_is_idempotent() -> None:
    state, theory_id, version_id = _opened()
    first = _attempt(
        theory_id,
        version_id,
        transition="attempt-a",
        actions=(("producer_a", True),),
    )
    second = _attempt(
        theory_id,
        version_id,
        transition="attempt-a-b",
        actions=(("producer_a", True), ("producer_b", True)),
    )

    after_first = reduce_theory(state, first)
    after_second = reduce_theory(after_first, second)

    assert len(after_second.ledger.versions) == 1
    assert len(after_second.ledger.attempts) == 2
    assert {attempt.version_id for attempt in after_second.ledger.attempts.values()} == {version_id}
    assert reduce_theory(after_second, second) is after_second


def test_duplicate_transition_identity_with_different_fact_fails_closed() -> None:
    state, theory_id, version_id = _opened()
    first = _attempt(
        theory_id,
        version_id,
        transition="attempt-1",
        actions=(("producer", True),),
    )
    state = reduce_theory(state, first)
    conflict = RecordTheoryAttempt(
        theory_id=theory_id,
        version_id=version_id,
        attempt_identity=first.attempt_identity,
        source=first.source,
        execution_owner_token=first.execution_owner_token,
        occurrence_evidence=first.occurrence_evidence,
        act_identity=(("producer", False),),
        pilot_rung_identities=(),
        disposition=first.disposition,
        evidence=first.evidence,
    )

    with pytest.raises(TheoryInvariantError):
        reduce_theory(state, conflict)


def test_attempt_preserves_exact_execution_owner_and_occurrence_evidence() -> None:
    state, theory_id, version_id = _opened()
    attempt = _attempt(
        theory_id,
        version_id,
        transition="owner-occurrence",
        actions=(("producer", True),),
    )

    state = reduce_theory(state, attempt)
    receipt = state.ledger.attempts[attempt.attempt_identity]

    assert receipt.execution_owner_token == ("attempt-owner", "owner-occurrence")
    assert receipt.occurrence_evidence == ("attempt-occurrence", "owner-occurrence")
    assert receipt.attempt_id == (
        "owner-occurrence",
        receipt.execution_owner_token,
        receipt.occurrence_evidence,
    )
    assert receipt.source.execution_owner_token == ("execution", "source")
    assert receipt.execution_owner_token != receipt.source.execution_owner_token


def test_attempt_passes_immutable_conductivity_observation_through_to_view() -> None:
    state, theory_id, version_id = _opened()
    observation = EffectObservationSnapshot(
        disposition="OVERWRITTEN",
        obligation=EffectObligationSnapshot(
            tag="Step",
            value=40,
            producer=(None, 4, ()),
            consumer=None,
            required_shape=(),
            boundary=None,
        ),
        detail="exact ordered projection",
    )
    attempt = replace(
        _attempt(
            theory_id,
            version_id,
            transition="conductivity",
            actions=(("Request", True),),
        ),
        conductivity_observations=(observation,),
    )

    state = reduce_theory(state, attempt)
    receipt = state.ledger.attempts[attempt.attempt_identity]
    view = theory_view(state)

    assert receipt.conductivity_observations == (observation,)
    assert receipt.conductivity_observations[0] is observation
    assert view is not None
    assert view.attempts[0].conductivity_observations[0] is observation

    def mutate_detail(value: Any) -> None:
        value.detail = "mutated"

    with pytest.raises(FrozenInstanceError):
        mutate_detail(observation)


def test_attempt_rejects_live_conductivity_evidence() -> None:
    state, theory_id, version_id = _opened()
    attempt = replace(
        _attempt(
            theory_id,
            version_id,
            transition="live-conductivity",
            actions=(("Request", True),),
        ),
        conductivity_observations=(object(),),  # type: ignore[arg-type]
    )

    with pytest.raises(TheoryInvariantError, match="unsupported live type object"):
        reduce_theory(state, attempt)


def test_detached_validation_visits_shared_identity_ancestry_once() -> None:
    shared: tuple[object, ...] = ("root",)
    for _ in range(1_200):
        shared = ("identity", shared, shared)

    assert_detached_theory_value(shared)


def _effect_occurrence(
    kind: str,
    ordinal: int,
    *,
    scan_id: int = 2,
    tag: str = "Step",
    values: tuple[Any, ...] = (40,),
    rung_index: int | None = None,
) -> EffectOccurrenceSnapshot:
    selected_rung = ordinal if rung_index is None else rung_index
    return EffectOccurrenceSnapshot(
        kind=kind,  # type: ignore[arg-type]
        ordinal=ordinal,
        scan_id=scan_id,
        run_order=ordinal,
        call_invocation=None,
        rung=(None, selected_rung),
        execution_kind="rung",
        caller_rung=selected_rung,
        call_stack=(),
        depth=0,
        enabled=True,
        tag=tag,
        values=values,
    )


def test_compass_derives_one_consumer_front_from_split_neutral_receipts() -> None:
    state, theory_id, version_id = _opened()
    obligation = EffectObligationSnapshot(
        tag="Step",
        value=40,
        producer=(None, 0, ()),
        consumer=(None, 1, ()),
        required_shape=(),
        boundary=None,
    )
    appeared = _effect_occurrence("write", 5, values=(91, 40))
    consumer = _effect_occurrence("read", 6)
    displacement = _effect_occurrence("write", 21, values=(40, 91))
    survived = EffectObservationSnapshot(
        disposition="SURVIVED",
        obligation=obligation,
        appeared=appeared,
        consumer_read=consumer,
    )
    overwritten = EffectObservationSnapshot(
        disposition="OVERWRITTEN",
        obligation=obligation,
        appeared=appeared,
        displacement=displacement,
    )
    attempt = replace(
        _attempt(
            theory_id,
            version_id,
            transition="consumer-then-overwrite",
            actions=(("Request", True),),
        ),
        conductivity_observations=(survived, overwritten),
    )
    state = reduce_theory(state, attempt)

    front = Compass().conductivity_front(theory_view(state))

    assert front is not None
    assert len(front.attempts) == 1
    assert len(front.flows) == 1
    flow = front.flows[0]
    assert flow.reach is ConductivityReach.CONSUMER
    assert flow.obligations == (obligation,)
    assert flow.observations[0] is survived
    assert flow.observations[1] is overwritten
    assert flow.appeared is appeared
    assert flow.front_occurrence is consumer
    assert flow.displacement is displacement


def test_conductivity_front_uses_occurrence_order_not_scan_zero() -> None:
    state, theory_id, version_id = _opened()
    obligation = EffectObligationSnapshot(
        tag="Step",
        value=40,
        producer=(None, 0, ()),
        consumer=(None, 1, ()),
        required_shape=(),
        boundary=None,
    )
    appeared = _effect_occurrence("write", 5, scan_id=19, values=(91, 40))
    displacement = _effect_occurrence("write", 21, scan_id=19, values=(40, 91))
    observation = EffectObservationSnapshot(
        disposition="OVERWRITTEN",
        obligation=obligation,
        appeared=appeared,
        displacement=displacement,
    )
    state = reduce_theory(
        state,
        replace(
            _attempt(
                theory_id,
                version_id,
                transition="prestepped-overwrite",
                actions=(("Request", True),),
            ),
            conductivity_observations=(observation,),
        ),
    )

    front = Compass().conductivity_front(theory_view(state))

    assert front is not None
    flow = front.flows[0]
    assert flow.reach is ConductivityReach.PRODUCER
    assert flow.front_occurrence is appeared
    assert flow.displacement is displacement


def test_theory_view_retains_conductivity_history_across_refinement() -> None:
    state, theory_id, version_id = _opened()
    observation = EffectObservationSnapshot(
        disposition="ABSENT",
        obligation=EffectObligationSnapshot(
            tag="Step",
            value=40,
            producer=(None, 0, ()),
            consumer=None,
            required_shape=(),
            boundary=None,
        ),
    )
    first = replace(
        _attempt(
            theory_id,
            version_id,
            transition="first-intrascan-issue",
            actions=(("First", True),),
        ),
        conductivity_observations=(observation,),
    )
    state = reduce_theory(state, first)
    state = reduce_theory(
        state,
        RefineTheory(
            theory_id=theory_id,
            parent_version_id=version_id,
            source=first.source,
            refined_source=first.source,
            requirements=(_requirement("first"),),
            refinement_identity=("first-refinement",),
            temporal_intent=TheoryTemporalIntent.RETRY_TOGETHER,
            trigger_attempt_id=first.attempt_identity,
        ),
    )
    refined_version = state.ledger.theories[theory_id].current_version_id
    second = replace(
        _attempt(
            theory_id,
            refined_version,
            transition="second-intrascan-issue",
            actions=(("Second", True),),
        ),
        conductivity_observations=(observation,),
    )
    state = reduce_theory(state, second)

    view = theory_view(state)
    front = Compass().conductivity_front(view)

    assert view is not None
    assert tuple(item.attempt_id for item in view.attempts) == (second.attempt_identity,)
    assert tuple(item.attempt_id for item in view.conductivity_attempts) == (
        first.attempt_identity,
        second.attempt_identity,
    )
    assert front is not None
    assert tuple(item.attempt_id for item in front.attempts) == (
        first.attempt_identity,
        second.attempt_identity,
    )


def test_compass_acknowledges_only_the_exact_retained_research_finding() -> None:
    state, theory_id, version_id = _opened()
    obligation = EffectObligationSnapshot(
        tag="Step",
        value=40,
        producer=(None, 2, ()),
        consumer=(None, 4, ()),
        required_shape=(),
        boundary=None,
    )

    def stopped_observation(scan_id: int) -> EffectObservationSnapshot:
        appeared = _effect_occurrence("write", 25, scan_id=scan_id, values=(98, 40))
        displacement = _effect_occurrence(
            "write",
            130,
            scan_id=scan_id,
            values=(40, 91),
        )
        watchdog_done = _effect_occurrence(
            "read",
            127,
            scan_id=scan_id,
            tag="Watchdog.Done",
            values=(True,),
            rung_index=16,
        )
        return EffectObservationSnapshot(
            disposition="OVERWRITTEN",
            obligation=obligation,
            appeared=appeared,
            displacement=displacement,
            observed_reads=(watchdog_done,),
        )

    first = replace(
        _attempt(
            theory_id,
            version_id,
            transition="watchdog-10",
            actions=(("Reconnect", True),),
        ),
        conductivity_observations=(stopped_observation(4),),
    )
    requirement_10 = replace(
        _requirement("watchdog"),
        condition_identity=("WatchdogPreset", ">", 10),
        deadline_occurrence=(
            "read",
            "WatchdogPreset",
            4,
            ((None, 4), "rung", 4, (), 0, None, 13, 35),
            (0,),
            True,
        ),
        demanding_occurrence=(
            "read",
            "Watchdog.Done",
            4,
            ((None, 16), "rung", 16, (), 0, None, 32, 127),
            (True,),
            True,
        ),
    )
    state = reduce_theory(state, first)
    state = reduce_theory(
        state,
        RefineTheory(
            theory_id=theory_id,
            parent_version_id=version_id,
            source=first.source,
            refined_source=first.source,
            requirements=(requirement_10,),
            refinement_identity=("watchdog-refine-10",),
            temporal_intent=TheoryTemporalIntent.RETRY_TOGETHER,
            trigger_attempt_id=first.attempt_identity,
        ),
    )
    second_version = state.ledger.theories[theory_id].current_version_id
    second = replace(
        _attempt(
            theory_id,
            second_version,
            transition="watchdog-20",
            actions=(("Checkpoint", True),),
        ),
        conductivity_observations=(stopped_observation(5),),
    )
    requirement_20 = replace(
        requirement_10,
        semantic_identity=("requirement", "watchdog", 20),
        condition_identity=("WatchdogPreset", ">", 20),
        deadline_occurrence=(
            "read",
            "WatchdogPreset",
            5,
            ((None, 4), "rung", 4, (), 0, None, 14, 34),
            (11,),
            True,
        ),
        demanding_occurrence=(
            "read",
            "Watchdog.Done",
            5,
            ((None, 16), "rung", 16, (), 0, None, 33, 127),
            (True,),
            True,
        ),
    )
    state = reduce_theory(state, second)
    state = reduce_theory(
        state,
        RefineTheory(
            theory_id=theory_id,
            parent_version_id=second_version,
            source=second.source,
            refined_source=second.source,
            requirements=(requirement_20,),
            refinement_identity=("watchdog-refine-20",),
            temporal_intent=TheoryTemporalIntent.RETRY_TOGETHER,
            trigger_attempt_id=second.attempt_identity,
        ),
    )

    view = theory_view(state)
    front = Compass().conductivity_front(view)
    request = Compass().conductivity_research(view)

    assert front is not None
    comparison = front.comparisons[-1]
    assert comparison.progress is ConductivityProgress.SAME_STOP
    assert len(comparison.requirement_drifts) == 1
    assert comparison.requirement_drifts[0].earlier is requirement_10
    assert comparison.requirement_drifts[0].later is requirement_20
    assert request is not None
    assert request.comparison == comparison
    assert request.displacement.tag == "Step"
    assert tuple(read.tag for read in request.enabling_reads) == ("Watchdog.Done",)

    finding = ConductivityResearchFinding(
        theory_id=request.theory_id,
        version_id=request.version_id,
        source=request.source,
        comparison_identity=request.comparison.identity,
        compared_attempt_ids=(
            request.comparison.earlier_attempt_id,
            request.comparison.later_attempt_id,
        ),
        displacement=request.displacement,
        enabling_reads=request.enabling_reads,
        requirement_drift_identities=tuple(
            drift.identity for drift in request.comparison.requirement_drifts
        ),
    )
    state = reduce_theory(state, RecordConductivityResearch(finding))
    researched_view = theory_view(state)

    assert researched_view is not None
    assert researched_view.research_findings == (finding,)
    assert Compass().conductivity_research(researched_view) is None

    other_world_finding = replace(
        finding,
        source=replace(finding.source, world_key=("world", "other")),
    )
    unmatched_view = replace(
        researched_view,
        research_findings=(other_world_finding,),
    )
    unmatched_request = Compass().conductivity_research(unmatched_view)
    assert unmatched_request is not None
    assert unmatched_request.identity == request.identity


def test_theory_view_projects_only_the_active_version_and_exact_source() -> None:
    state, theory_id, version_id = _opened()
    rejected = _attempt(
        theory_id,
        version_id,
        transition="rejected-at-root",
        actions=(("producer", True),),
        first_edge_identity=("chart-edge", "producer"),
    )
    accepted = _attempt(
        theory_id,
        version_id,
        transition="accepted-at-root",
        actions=(("alternate", True),),
        disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
    )
    state = reduce_theory(reduce_theory(state, rejected), accepted)

    root_view = theory_view(state)
    assert root_view is not None
    assert root_view.theory_id == theory_id
    assert root_view.version_id == version_id
    assert root_view.source == _boundary("source", 0)
    assert root_view.root == _boundary("source", 0)
    assert root_view.claim == _claim(target="stepper-complete")
    assert tuple(item.attempt_id for item in root_view.attempts) == (
        rejected.attempt_identity,
        accepted.attempt_identity,
    )
    assert root_view.first_edge_exclusions == (
        TheoryFirstEdgeExclusion(
            theory_id,
            version_id,
            rejected.source,
            rejected.first_edge_identity,
            rejected.attempt_identity,
            TheoryAttemptDisposition.REJECTED_EXACT,
        ),
    )
    assert rejected.first_edge_identity is not None
    assert root_view.excludes_first_edge(rejected.first_edge_identity)
    assert not root_view.excludes_first_edge(accepted.act_identity)

    landing = _boundary("landing", 1)
    state = reduce_theory(
        state,
        AdvanceTheory(
            theory_id=theory_id,
            version_id=version_id,
            accepted_attempt_id=accepted.attempt_identity,
            source=accepted.source,
            boundary=landing,
            advance_identity=("advance-to-landing",),
        ),
    )

    landing_view = theory_view(state)
    assert landing_view is not None
    assert landing_view.source == landing
    assert landing_view.attempts == ()
    assert landing_view.first_edge_exclusions == ()


def test_theory_view_scopes_failures_to_the_current_version() -> None:
    state, theory_id, version_id = _opened()
    rejected = _attempt(
        theory_id,
        version_id,
        transition="rejected-before-refinement",
        actions=(("producer", True),),
        disposition=TheoryAttemptDisposition.REJECTED_EMPIRICAL,
    )
    state = reduce_theory(state, rejected)
    state = reduce_theory(
        state,
        RefineTheory(
            theory_id=theory_id,
            parent_version_id=version_id,
            source=rejected.source,
            refined_source=rejected.source,
            requirements=(_requirement("consumer"),),
            refinement_identity=("refine-after-rejection",),
        ),
    )

    view = theory_view(state)
    assert view is not None
    assert view.version_id != version_id
    assert view.requirements == (_requirement("consumer"),)
    assert view.attempts == ()
    assert view.first_edge_exclusions == ()


def test_refined_retry_intent_projects_its_exact_trigger_and_requirements() -> None:
    state, theory_id, version_id = _opened()
    rejected = _attempt(
        theory_id,
        version_id,
        transition="retry-trigger",
        actions=(("producer", True),),
    )
    state = reduce_theory(state, rejected)
    refined_source = replace(
        rejected.source,
        world_key=("world", "source", ("requirement", "same-scan")),
        occurrence_identity=("requirements", "same-scan"),
    )
    state = reduce_theory(
        state,
        RefineTheory(
            theory_id=theory_id,
            parent_version_id=version_id,
            source=rejected.source,
            refined_source=refined_source,
            requirements=(_requirement("same-scan"),),
            refinement_identity=("retry-together",),
            temporal_intent=TheoryTemporalIntent.RETRY_TOGETHER,
            trigger_attempt_id=rejected.attempt_identity,
        ),
    )

    temporal = temporal_need_request(state)
    assert temporal is not None
    assert temporal.intent is TheoryTemporalIntent.RETRY_TOGETHER
    # Lowering executes at the rejected attempt's exact source; the refined
    # version source describes learned evidence, not a replay landing.
    assert temporal.source == rejected.source
    assert temporal.requirements == (_requirement("same-scan"),)


def test_refined_setup_intent_projects_need_without_an_action_artifact() -> None:
    state, theory_id, version_id = _opened()
    rejected = _attempt(
        theory_id,
        version_id,
        transition="setup-trigger",
        actions=(("original", True),),
    )
    state = reduce_theory(state, rejected)
    live_landing = replace(rejected.source, world_key=("failed", "landing"), scan_id=1)
    requirement = _requirement("prior")
    state = reduce_theory(
        state,
        RefineTheory(
            theory_id=theory_id,
            parent_version_id=version_id,
            source=rejected.source,
            refined_source=live_landing,
            requirements=(requirement,),
            refinement_identity=("setup-first",),
            temporal_intent=TheoryTemporalIntent.SETUP_FIRST,
            trigger_attempt_id=rejected.attempt_identity,
        ),
    )

    request = temporal_need_request(state)

    assert request is not None
    assert request.intent is TheoryTemporalIntent.SETUP_FIRST
    assert request.source == rejected.source
    assert request.trigger_act_identity == rejected.act_identity
    assert request.requirements == (requirement,)


def test_successor_temporal_request_excludes_accumulated_requirement_history() -> None:
    state, theory_id, v1 = _opened()
    first_rejection = _attempt(
        theory_id,
        v1,
        transition="first-setup-trigger",
        actions=(("first", True),),
    )
    state = reduce_theory(state, first_rejection)
    first = _requirement("first")
    state = reduce_theory(
        state,
        RefineTheory(
            theory_id=theory_id,
            parent_version_id=v1,
            source=first_rejection.source,
            refined_source=first_rejection.source,
            requirements=(first,),
            refinement_identity=("first-setup",),
            temporal_intent=TheoryTemporalIntent.SETUP_FIRST,
            trigger_attempt_id=first_rejection.attempt_identity,
        ),
    )

    v2 = state.ledger.theories[theory_id].current_version_id
    successor_rejection = _attempt(
        theory_id,
        v2,
        transition="successor-setup-trigger",
        actions=(("successor", True),),
    )
    state = reduce_theory(state, successor_rejection)
    successor = _requirement("successor")
    state = reduce_theory(
        state,
        RefineTheory(
            theory_id=theory_id,
            parent_version_id=v2,
            source=successor_rejection.source,
            refined_source=successor_rejection.source,
            requirements=(successor,),
            refinement_identity=("successor-setup",),
            temporal_intent=TheoryTemporalIntent.SETUP_FIRST,
            trigger_attempt_id=successor_rejection.attempt_identity,
        ),
    )

    view = theory_view(state)
    request = temporal_need_request(state)

    assert view is not None
    assert view.requirements == (first, successor)
    assert request is not None
    assert request.requirements == (successor,)


def test_adjacent_monitor_receipt_can_rewind_to_the_parent_progress_boundary() -> None:
    state, theory_id, version_id = _opened()
    accepted = _attempt(
        theory_id,
        version_id,
        transition="accepted-adjacent-scan",
        actions=(("setup", True),),
        disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
    )
    state = reduce_theory(state, accepted)
    landing = _boundary("landing", 1)
    state = reduce_theory(
        state,
        AdvanceTheory(
            theory_id=theory_id,
            version_id=version_id,
            accepted_attempt_id=accepted.attempt_identity,
            source=accepted.source,
            boundary=landing,
            advance_identity=("accept-adjacent-scan",),
        ),
    )

    successor = _attempt(
        theory_id,
        version_id,
        transition="successor-at-parent-boundary",
        actions=(("successor", True),),
    )
    state = reduce_theory(state, successor)
    requirement = _requirement("successor-at-parent-boundary")
    state = reduce_theory(
        state,
        RefineTheory(
            theory_id=theory_id,
            parent_version_id=version_id,
            source=successor.source,
            refined_source=successor.source,
            requirements=(requirement,),
            refinement_identity=("rewind-for-successor",),
            temporal_intent=TheoryTemporalIntent.SETUP_FIRST,
            trigger_attempt_id=successor.attempt_identity,
            temporal_source=successor.source,
        ),
    )

    view = theory_view(state)
    request = temporal_need_request(state)

    assert view is not None
    assert view.source == accepted.source
    assert request is not None
    assert request.source == accepted.source
    assert request.requirements == (requirement,)


def test_adjacent_monitor_receipt_can_rewind_to_its_structural_overlay_source() -> None:
    state, theory_id, version_id = _opened()
    accepted = _attempt(
        theory_id,
        version_id,
        transition="accepted-overlay-scan",
        actions=(("setup", True),),
        disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
    )
    state = reduce_theory(state, accepted)
    overlay_source = _boundary("source-with-new-correctives", 0)
    state = reduce_theory(
        state,
        AdvanceTheory(
            theory_id=theory_id,
            version_id=version_id,
            accepted_attempt_id=accepted.attempt_identity,
            source=accepted.source,
            boundary=_boundary("overlay-landing", 1),
            advance_identity=("accept-overlay-scan",),
            execution_source=overlay_source,
        ),
    )

    successor = replace(
        _attempt(
            theory_id,
            version_id,
            transition="successor-at-overlay-source",
            actions=(("successor", True),),
        ),
        source=overlay_source,
    )
    state = reduce_theory(state, successor)
    requirement = _requirement("successor-at-overlay-source")
    state = reduce_theory(
        state,
        RefineTheory(
            theory_id=theory_id,
            parent_version_id=version_id,
            source=overlay_source,
            refined_source=overlay_source,
            requirements=(requirement,),
            refinement_identity=("rewind-to-overlay-source",),
            temporal_intent=TheoryTemporalIntent.SETUP_FIRST,
            trigger_attempt_id=successor.attempt_identity,
            temporal_source=overlay_source,
        ),
    )

    request = temporal_need_request(state)

    assert request is not None
    assert request.source == overlay_source
    assert request.requirements == (requirement,)


def test_refinement_can_authorize_an_exact_earlier_temporal_source() -> None:
    state, theory_id, version_id = _opened()
    rejected = _attempt(
        theory_id,
        version_id,
        transition="rewind-trigger",
        actions=(("original", True),),
    )
    state = reduce_theory(state, rejected)
    earlier = replace(
        rejected.source,
        world_key=("world", "earlier"),
        scan_id=0,
        checkpoint_token=("checkpoint", "earlier"),
        execution_owner_token=(),
    )
    requirement = replace(
        _requirement("prior"),
        source_scan=0,
        source_world_key=earlier.world_key,
        provenance="program-guard-rebase",
    )
    state = reduce_theory(
        state,
        RefineTheory(
            theory_id=theory_id,
            parent_version_id=version_id,
            source=rejected.source,
            refined_source=earlier,
            requirements=(requirement,),
            refinement_identity=("rewind-to-exact-source",),
            temporal_intent=TheoryTemporalIntent.SETUP_FIRST,
            trigger_attempt_id=rejected.attempt_identity,
            temporal_source=earlier,
        ),
    )

    request = temporal_need_request(state)

    assert request is not None
    assert request.source == earlier
    view = theory_view(state)
    assert view is not None
    assert view.source == earlier


def test_theory_view_is_absent_without_an_open_theory() -> None:
    assert theory_view(TheoryState()) is None

    state, theory_id, version_id = _opened()
    state = reduce_theory(
        state,
        AbandonTheory(
            theory_id=theory_id,
            version_id=version_id,
            termination=TheoryTermination.STUCK,
            abandonment_identity=("close-before-view",),
        ),
    )
    assert theory_view(state) is None


@pytest.mark.parametrize("missing", ["owner", "occurrence"])
def test_attempt_fails_closed_without_exact_execution_evidence(missing: str) -> None:
    state, theory_id, version_id = _opened()
    attempt = _attempt(
        theory_id,
        version_id,
        transition=f"missing-{missing}",
        actions=(("producer", True),),
    )
    if missing == "owner":
        attempt = replace(attempt, execution_owner_token=())
    else:
        attempt = replace(attempt, occurrence_evidence=())

    with pytest.raises(TheoryInvariantError, match="evidence is missing"):
        reduce_theory(state, attempt)


def test_attempt_rejects_a_source_outside_the_active_boundary_chain() -> None:
    state, theory_id, version_id = _opened()
    attempt = replace(
        _attempt(
            theory_id,
            version_id,
            transition="foreign-source",
            actions=(("producer", True),),
        ),
        source=_boundary("foreign", 0),
    )

    with pytest.raises(TheoryInvariantError, match="active root, version, or progress"):
        reduce_theory(state, attempt)


def test_advance_appends_progress_under_one_parent_chain() -> None:
    state, theory_id, version_id = _opened()
    accepted_1 = _attempt(
        theory_id,
        version_id,
        transition="accepted-1",
        actions=(("producer", True),),
        disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
    )
    state = reduce_theory(state, accepted_1)
    first = AdvanceTheory(
        theory_id=theory_id,
        version_id=version_id,
        accepted_attempt_id=accepted_1.attempt_identity,
        source=accepted_1.source,
        boundary=_boundary("landing-1", 1),
        advance_identity=("advance-1",),
        remaining_budget=7,
    )
    state = reduce_theory(state, first)
    accepted_2 = replace(
        _attempt(
            theory_id,
            version_id,
            transition="accepted-2",
            actions=(("consumer", True),),
            disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
        ),
        source=_boundary("landing-1", 1),
    )
    state = reduce_theory(state, accepted_2)
    second = AdvanceTheory(
        theory_id=theory_id,
        version_id=version_id,
        accepted_attempt_id=accepted_2.attempt_identity,
        source=accepted_2.source,
        boundary=_boundary("landing-2", 2),
        advance_identity=("advance-2",),
        remaining_budget=6,
    )

    state = reduce_theory(state, second)
    progress = tuple(state.ledger.progress.values())

    assert len(progress) == 3
    by_id = {item.progress_id: item for item in progress}
    last = by_id[state.ledger.theories[theory_id].current_progress_id]
    middle = by_id[last.parent_progress_id]
    assert middle.parent_progress_id is not None
    assert {item.theory_id for item in progress} == {theory_id}
    assert last.accepted_attempt_id == accepted_2.attempt_identity


def test_advance_requires_an_accepted_attempt_and_monotonic_exact_source() -> None:
    state, theory_id, version_id = _opened()
    rejected = _attempt(
        theory_id,
        version_id,
        transition="rejected",
        actions=(("producer", False),),
    )
    state = reduce_theory(state, rejected)
    base = AdvanceTheory(
        theory_id=theory_id,
        version_id=version_id,
        accepted_attempt_id=rejected.attempt_identity,
        source=rejected.source,
        boundary=_boundary("landing", 1),
        advance_identity=("advance-rejected",),
    )

    with pytest.raises(TheoryInvariantError, match="accepted attempt"):
        reduce_theory(state, base)
    with pytest.raises(TheoryInvariantError, match="no linked attempt"):
        reduce_theory(
            state,
            replace(
                base,
                accepted_attempt_id=("missing",),
                advance_identity=("advance-missing",),
            ),
        )

    accepted = _attempt(
        theory_id,
        version_id,
        transition="accepted",
        actions=(("producer", True),),
        disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
    )
    state = reduce_theory(state, accepted)
    with pytest.raises(TheoryInvariantError, match="active root, version, or progress"):
        reduce_theory(
            state,
            replace(
                base,
                accepted_attempt_id=accepted.attempt_identity,
                source=_boundary("foreign", 0),
                advance_identity=("advance-foreign",),
            ),
        )
    with pytest.raises(TheoryInvariantError, match="not monotonic"):
        reduce_theory(
            state,
            replace(
                base,
                accepted_attempt_id=accepted.attempt_identity,
                boundary=accepted.source,
                advance_identity=("advance-static",),
            ),
        )


def test_refine_builds_v1_v2_v3_only_for_novel_strengthening() -> None:
    state, theory_id, v1 = _opened()
    add_a = RefineTheory(
        theory_id=theory_id,
        parent_version_id=v1,
        source=_boundary("source", 0),
        refined_source=_boundary("source", 0),
        requirements=(_requirement("producer_a"),),
        refinement_identity=("refine-a",),
    )
    state = reduce_theory(state, add_a)
    v2 = state.ledger.theories[theory_id].current_version_id

    repeated_a = RefineTheory(
        theory_id=theory_id,
        parent_version_id=v2,
        source=_boundary("source", 0),
        refined_source=_boundary("source", 0),
        requirements=(_requirement("producer_a"),),
        refinement_identity=("refine-a-repeat",),
    )
    repeated = reduce_theory(state, repeated_a)

    add_b = RefineTheory(
        theory_id=theory_id,
        parent_version_id=v2,
        source=_boundary("source", 0),
        refined_source=_boundary("source", 0),
        requirements=(_requirement("producer_b"),),
        refinement_identity=("refine-b",),
    )
    state = reduce_theory(repeated, add_b)
    v3 = state.ledger.theories[theory_id].current_version_id

    assert len(repeated.ledger.versions) == 2
    assert len(state.ledger.versions) == 3
    assert state.ledger.versions[v2].parent_version_id == v1
    assert state.ledger.versions[v3].parent_version_id == v2
    assert state.ledger.versions[v2].source == _boundary("source", 0)
    assert v3 not in {v1, v2}

    weakening = RefineTheory(
        theory_id=theory_id,
        parent_version_id=v3,
        source=_boundary("source", 0),
        refined_source=_boundary("source", 0),
        requirements=(_requirement("producer_a"),),
        refinement_identity=("refine-weaker",),
    )
    unchanged = reduce_theory(state, weakening)
    assert len(unchanged.ledger.versions) == 3


def test_refine_rejects_a_source_outside_the_active_boundary_chain() -> None:
    state, theory_id, version_id = _opened()

    with pytest.raises(TheoryInvariantError, match="active root, version, or progress"):
        reduce_theory(
            state,
            RefineTheory(
                theory_id=theory_id,
                parent_version_id=version_id,
                source=_boundary("foreign", 4),
                refined_source=_boundary("foreign", 4),
                requirements=(_requirement("producer"),),
                refinement_identity=("foreign-refinement",),
            ),
        )


def test_refine_retains_requirements_changed_boundary_for_the_new_version() -> None:
    state, theory_id, version_id = _opened()
    source = _boundary("source", 0)
    refined_source = replace(
        source,
        world_key=("world", "requirements-changed"),
        scan_id=1,
        checkpoint_token=("checkpoint", "requirements-changed"),
        execution_owner_token=("execution", "requirements-changed"),
        occurrence_identity=("requirements", "producer"),
    )

    state = reduce_theory(
        state,
        RefineTheory(
            theory_id=theory_id,
            parent_version_id=version_id,
            source=source,
            refined_source=refined_source,
            requirements=(_requirement("producer"),),
            refinement_identity=("requirements-changed",),
        ),
    )

    refined_version_id = state.ledger.theories[theory_id].current_version_id
    assert state.ledger.versions[refined_version_id].source == refined_source
    assert source in refined_version_id
    assert refined_source in refined_version_id


@pytest.mark.parametrize("changed", ["backward_scan", "owner_after_scan", "checkpoint"])
def test_refine_fails_closed_on_inexact_refined_boundary(changed: str) -> None:
    state, theory_id, version_id = _opened()
    source = _boundary("source", 0)
    refined_source = replace(source, world_key=("world", "requirements-changed"))
    if changed == "backward_scan":
        refined_source = replace(refined_source, scan_id=-1)
    elif changed == "owner_after_scan":
        refined_source = replace(refined_source, scan_id=1, execution_owner_token=())
    else:
        refined_source = replace(refined_source, checkpoint_token=())

    with pytest.raises(TheoryInvariantError):
        reduce_theory(
            state,
            RefineTheory(
                theory_id=theory_id,
                parent_version_id=version_id,
                source=source,
                refined_source=refined_source,
                requirements=(_requirement("producer"),),
                refinement_identity=("inexact-refinement", changed),
            ),
        )


def test_advance_accepts_attempt_from_direct_parent_version_after_refinement() -> None:
    state, theory_id, v1 = _opened()
    accepted = _attempt(
        theory_id,
        v1,
        transition="accepted-before-refine",
        actions=(("producer", True),),
        disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
    )
    state = reduce_theory(state, accepted)
    state = reduce_theory(
        state,
        RefineTheory(
            theory_id=theory_id,
            parent_version_id=v1,
            source=accepted.source,
            refined_source=accepted.source,
            requirements=(_requirement("producer"),),
            refinement_identity=("refine-after-attempt",),
        ),
    )
    v2 = state.ledger.theories[theory_id].current_version_id

    state = reduce_theory(
        state,
        AdvanceTheory(
            theory_id=theory_id,
            version_id=v2,
            accepted_attempt_id=accepted.attempt_identity,
            source=accepted.source,
            boundary=_boundary("landing-after-refine", 1),
            advance_identity=("advance-after-refine",),
        ),
    )

    progress = state.ledger.progress[state.ledger.theories[theory_id].current_progress_id]
    assert progress.accepted_attempt_id == accepted.attempt_identity


def test_prove_closes_theory_with_detached_receipt() -> None:
    state, theory_id, version_id = _opened()
    fact = ProveTheory(
        theory_id=theory_id,
        version_id=version_id,
        promoted_landing=_boundary("landing", 1),
        proof_identity=("prove",),
        fulfilled_obligations=(("consumer", True),),
        requirement_observations=(("producer", True),),
        retained_pilot_rung_identities=(("guard", 1),),
    )

    state = reduce_theory(state, fact)

    assert state.active_theory_id is None
    assert len(state.ledger.receipts) == 1
    assert next(iter(state.ledger.receipts.values())).theory_id == theory_id
    assert not state.ledger.tombstones


def test_abandon_closes_only_the_exact_version() -> None:
    state, theory_id, version_id = _opened()
    fact = AbandonTheory(
        theory_id=theory_id,
        version_id=version_id,
        termination=TheoryTermination.BUDGET,
        abandonment_identity=("abandon",),
    )

    state = reduce_theory(state, fact)

    assert state.active_theory_id is None
    assert len(state.ledger.tombstones) == 1
    tombstone = next(iter(state.ledger.tombstones.values()))
    assert tombstone.theory_id == theory_id
    assert tombstone.version_id == version_id
    assert not state.ledger.receipts


def test_successor_opens_only_from_a_proved_receipt() -> None:
    state, theory_id, version_id = _opened()
    state = reduce_theory(
        state,
        ProveTheory(
            theory_id=theory_id,
            version_id=version_id,
            promoted_landing=_boundary("landing", 1),
            proof_identity=("prove",),
        ),
    )
    receipt = next(iter(state.ledger.receipts.values()))
    successor = OpenSuccessor(
        parent_receipt_id=receipt.receipt_id,
        claim=_claim(source="landing"),
        opening_identity=("successor",),
        link_identity=("successor-link",),
        remaining_budget=4,
    )

    state = reduce_theory(state, successor)

    assert state.active_theory_id is not None
    assert state.active_theory_id != theory_id
    assert len(state.ledger.successors) == 1
    assert next(iter(state.ledger.successors.values())).parent_receipt_id == receipt.receipt_id


def test_successor_rejects_missing_or_abandoned_parent() -> None:
    with pytest.raises(TheoryInvariantError):
        reduce_theory(
            TheoryState(),
            OpenSuccessor(
                parent_receipt_id=("receipt", "missing"),
                claim=_claim(),
                opening_identity=("missing-parent",),
                link_identity=("missing-link",),
                remaining_budget=4,
            ),
        )

    abandoned, theory_id, version_id = _opened()
    abandoned = reduce_theory(
        abandoned,
        AbandonTheory(
            theory_id=theory_id,
            version_id=version_id,
            termination=TheoryTermination.STUCK,
            abandonment_identity=("abandon",),
        ),
    )
    tombstone = next(iter(abandoned.ledger.tombstones.values()))
    with pytest.raises(TheoryInvariantError):
        reduce_theory(
            abandoned,
            OpenSuccessor(
                parent_receipt_id=tombstone.tombstone_id,
                claim=_claim(),
                opening_identity=("abandoned-parent",),
                link_identity=("abandoned-link",),
                remaining_budget=4,
            ),
        )


def test_identical_fact_streams_produce_identical_ledgers_and_ids() -> None:
    def run() -> TheoryState:
        state, theory_id, version_id = _opened()
        facts = (
            (
                attempt := _attempt(
                    theory_id,
                    version_id,
                    transition="attempt",
                    actions=(("producer", True),),
                    disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
                )
            ),
            AdvanceTheory(
                theory_id=theory_id,
                version_id=version_id,
                accepted_attempt_id=attempt.attempt_identity,
                source=attempt.source,
                boundary=_boundary("landing", 1),
                advance_identity=("advance",),
                remaining_budget=7,
            ),
            ProveTheory(
                theory_id=theory_id,
                version_id=version_id,
                promoted_landing=_boundary("landing", 1),
                proof_identity=("prove",),
            ),
        )
        for fact in facts:
            state = reduce_theory(state, fact)
        return state

    assert run() == run()


_FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "bearing",
        "candidate",
        "candidate_read",
        "cursor",
        "fork",
        "future",
        "navigation_act",
        "orientation",
        "orientation_read",
        "predicted_world",
        "route",
        "route_suffix",
        "work",
    }
)


def _assert_detached(value: Any, path: str = "ledger") -> None:
    forbidden_types = (
        Bearing,
        CandidateRead,
        OrientationRead,
        NavigationAct,
        PLC,
        _World,
        _Checkpoint,
        PilotRung,
        TraceChoice,
    )
    assert not isinstance(value, forbidden_types), f"{path} retained {type(value).__name__}"
    assert not callable(value), f"{path} retained callable {value!r}"

    if is_dataclass(value) and not isinstance(value, type):
        for member in fields(value):
            assert member.name not in _FORBIDDEN_FIELD_NAMES, (
                f"forbidden field {path}.{member.name}"
            )
            _assert_detached(getattr(value, member.name), f"{path}.{member.name}")
    elif isinstance(value, Mapping):
        for key, member in value.items():
            _assert_detached(key, f"{path}.key")
            _assert_detached(member, f"{path}[{key!r}]")
    elif isinstance(value, tuple | list | set | frozenset):
        for index, member in enumerate(value):
            _assert_detached(member, f"{path}[{index}]")


def test_populated_closed_ledger_retains_no_navigation_or_executable_future() -> None:
    state, theory_id, version_id = _opened()
    accepted = _attempt(
        theory_id,
        version_id,
        transition="attempt",
        actions=(("producer", True),),
        disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
    )
    facts = (
        accepted,
        AdvanceTheory(
            theory_id=theory_id,
            version_id=version_id,
            accepted_attempt_id=accepted.attempt_identity,
            source=accepted.source,
            boundary=_boundary("landing", 1),
            advance_identity=("advance",),
            remaining_budget=7,
            phase_receipts=(
                TheoryPhaseReceipt(
                    kind=TheoryPhaseKind.SCAN_PROGRESS,
                    evidence_identity=("observed",),
                ),
            ),
        ),
        RefineTheory(
            theory_id=theory_id,
            parent_version_id=version_id,
            source=_boundary("landing", 1),
            refined_source=_boundary("landing", 1),
            requirements=(_requirement("consumer"),),
            refinement_identity=("refine",),
        ),
    )
    for fact in facts:
        state = reduce_theory(state, fact)
    final_version = state.ledger.theories[theory_id].current_version_id
    state = reduce_theory(
        state,
        ProveTheory(
            theory_id=theory_id,
            version_id=final_version,
            promoted_landing=_boundary("landing", 2),
            proof_identity=("prove",),
            retained_pilot_rung_identities=(("producer-guard", 1),),
        ),
    )

    _assert_detached(state.ledger)
