"""Pure lifecycle contracts for the immutable WorkingTheory ledger."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import FrozenInstanceError, fields, is_dataclass, replace
from typing import Any

import pytest

from pyrung import PLC
from pyrung.core.analysis.pilot.candidate_read import CandidateRead
from pyrung.core.analysis.pilot.compass import Compass
from pyrung.core.analysis.pilot.conductivity import (
    ConductivityProgress,
    ConductivityReach,
)
from pyrung.core.analysis.pilot.effects import (
    ConsumerBoundary,
    EffectObligationSnapshot,
    EffectObservationSnapshot,
    EffectOccurrenceSelector,
    EffectOccurrenceSnapshot,
)
from pyrung.core.analysis.pilot.execution import CheckpointRef, ScanEntryConfiguration
from pyrung.core.analysis.pilot.intrascan_research import (
    IntrascanBoundaryRealization,
    IntrascanProducerGoal,
    IntrascanProducerTrace,
    IntrascanReadRequirement,
    IntrascanTracebackStep,
    IntrascanTracebackWitness,
    IntrascanWriteEvidence,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    Bearing,
    ChannelHeading,
    NavigationAct,
    OrientationRead,
    RouteEdgeContext,
)
from pyrung.core.analysis.pilot.overlay import PilotRung
from pyrung.core.analysis.pilot.theory_reducer import (
    AbandonTheory,
    AdvanceTheory,
    ComposeTheoryCorrection,
    OpenTheory,
    ProveTheory,
    RebaseTheoryWorld,
    RecordConductivityResearch,
    RecordIntrascanTraceback,
    RecordIntrascanTracebackFrontier,
    RecordTheoryAttempt,
    RefineTheory,
    reduce_theory,
)
from pyrung.core.analysis.pilot.trace_read import TraceChoice
from pyrung.core.analysis.pilot.working_theory import (
    ConductivityResearchFinding,
    IntrascanTracebackFinding,
    IntrascanTracebackFrontier,
    ProgramTransaction,
    TheoryAttemptDisposition,
    TheoryBoundaryIdentity,
    TheoryClaim,
    TheoryFirstEdgeExclusion,
    TheoryInvariantError,
    TheoryObjectiveSnapshot,
    TheoryPhaseKind,
    TheoryPhaseReceipt,
    TheoryRequirementSnapshot,
    TheoryState,
    TheoryStatus,
    TheoryTemporalIntent,
    TheoryTermination,
    active_theory_configurations,
    active_theory_pilot_rung_identities,
    active_theory_superseded_pilot_rung_identities,
    assert_detached_theory_value,
    assert_temporal_need_current,
    temporal_need_request,
    temporal_setup_configuration_tags,
    theory_view,
)
from pyrung.core.analysis.pilot.world import _Checkpoint, _World
from pyrung.core.context import RungId
from pyrung.core.intrascan_counterfactual import OccurrenceBoundary
from pyrung.core.runner import EpochRef


def _execution_ref(label: str) -> EpochRef:
    return EpochRef(int.from_bytes(label.encode(), "big"))


def _boundary(label: str, scan: int) -> TheoryBoundaryIdentity:
    return TheoryBoundaryIdentity(
        world_key=("world", label),
        scan_id=scan,
        owner_ref=_execution_ref(label),
        occurrence_identity=("occurrence", label),
    )


def _claim(*, target: str = "stepper_complete", source: str = "source") -> TheoryClaim:
    boundary = _boundary(source, 0)
    return TheoryClaim(
        source=boundary,
        objective=TheoryObjectiveSnapshot(target, True),
        obligations=(
            EffectObligationSnapshot(
                tag="consumer_ready",
                value=True,
                producer=("producer", 0),
                consumer=("consumer", 1),
                required_shape=(("consumer_ready", True),),
                boundary=("consumer", 1),
                terminal_target=False,
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
        execution_ref=_execution_ref("source"),
        phase="assertion",
        status="active",
        provenance="projection",
        scope=("call", label, 0),
    )


def _consumer_boundary(*, source_scan: int = 0) -> ConsumerBoundary:
    produced = EffectOccurrenceSnapshot(
        kind="write",
        ordinal=20,
        scan_id=source_scan + 1,
        run_order=4,
        call_invocation=None,
        rung=(None, 4),
        execution_kind="rung",
        caller_rung=4,
        call_stack=(),
        depth=0,
        enabled=True,
        tag="Step",
        values=(40, 41),
        branch_path=(0,),
    )
    consumer = replace(
        produced,
        kind="read",
        ordinal=23,
        run_order=5,
        rung=(None, 5),
        caller_rung=5,
        values=(41,),
        branch_path=None,
    )
    return ConsumerBoundary(
        produced_occurrence=produced,
        consumer_occurrence=consumer,
        producer_selector=EffectOccurrenceSelector(
            kind="write",
            tag="Step",
            static_address=(None, 4, (0,)),
            instruction_path=(0,),
            execution_kind="rung",
            caller_rung=4,
            call_stack=(),
            depth=0,
            call_invocation=None,
            access_index=0,
        ),
        consumer_selector=EffectOccurrenceSelector(
            kind="read",
            tag="Step",
            static_address=(None, 5, ()),
            instruction_path=(0,),
            execution_kind="rung",
            caller_rung=5,
            call_stack=(),
            depth=0,
            call_invocation=None,
            access_index=0,
        ),
        producer_scan_offset=1,
        consumer_scan_offset=1,
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
    execution_ref = _execution_ref(f"attempt-{transition}")
    occurrence = ("attempt-occurrence", transition)
    return RecordTheoryAttempt(
        theory_id=theory_id,
        version_id=version_id,
        attempt_identity=(transition, execution_ref, occurrence),
        source=source,
        execution_ref=execution_ref,
        occurrence_evidence=occurrence,
        act_identity=actions,
        act_pairs=actions,
        selected_act_pairs=actions,
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


def test_no_scan_composition_keeps_the_physical_tip_and_updates_configuration() -> None:
    state, theory_id, version_id = _opened()
    source = _boundary("source", 0)
    first = ScanEntryConfiguration((("PresetMs", 11),))
    fact = ComposeTheoryCorrection(
        theory_id=theory_id,
        version_id=version_id,
        source=source,
        composed_source=source,
        requirement_identities=(("requirement", "preset"),),
        configuration=first,
        composition_identity=("compose", "preset"),
    )

    state = reduce_theory(state, fact)
    view = theory_view(state)

    assert view is not None
    assert view.source == source
    assert view.configurations == (first,)
    theory = state.ledger.theories[theory_id]
    progress = state.ledger.progress[theory.current_progress_id]
    assert progress.provisional_tip == source
    assert progress.provisional_tip.scan_id == source.scan_id
    assert progress.phase_receipts[-1].kind is TheoryPhaseKind.CORRECTION_COMPOSITION

    replacement = ScanEntryConfiguration((("PresetMs", 21),))
    state = reduce_theory(
        state,
        ComposeTheoryCorrection(
            theory_id=theory_id,
            version_id=version_id,
            source=source,
            composed_source=source,
            requirement_identities=(("requirement", "preset-20"),),
            configuration=replacement,
            composition_identity=("compose", "preset-20"),
            superseded_configuration_identities=(first.identity,),
        ),
    )
    theory = state.ledger.theories[theory_id]
    progress = state.ledger.progress[theory.current_progress_id]

    assert progress.provisional_tip == source
    assert progress.phase_receipts[-1].superseded_configuration_identities == (
        first.identity,
    )
    assert active_theory_configurations(state) == (replacement,)
    assert temporal_setup_configuration_tags(state) == frozenset({"PresetMs"})


def test_no_scan_composition_owns_and_supersedes_exact_pilot_rungs() -> None:
    state, theory_id, version_id = _opened()
    source = _boundary("source", 0)
    first = ("Sail", True, ("guard", "running"), None)
    replacement = ("Sail", False, ("guard", "running"), None)

    state = reduce_theory(
        state,
        ComposeTheoryCorrection(
            theory_id=theory_id,
            version_id=version_id,
            source=source,
            composed_source=source,
            requirement_identities=(("requirement", "sail-on"),),
            pilot_rung_identities=(first,),
            composition_identity=("compose", "sail-on"),
        ),
    )
    assert active_theory_pilot_rung_identities(state) == frozenset((first,))

    state = reduce_theory(
        state,
        ComposeTheoryCorrection(
            theory_id=theory_id,
            version_id=version_id,
            source=source,
            composed_source=source,
            requirement_identities=(("requirement", "sail-off"),),
            pilot_rung_identities=(replacement,),
            superseded_pilot_rung_identities=(first,),
            composition_identity=("compose", "sail-off"),
        ),
    )

    assert active_theory_pilot_rung_identities(state) == frozenset((replacement,))
    assert active_theory_superseded_pilot_rung_identities(state) == frozenset((first,))


def test_correction_install_promotes_tentative_pilot_rung() -> None:
    state, theory_id, version_id = _opened()
    source = _boundary("source", 0)
    correction = ("Sail", True, ("guard", "running"), None)
    state = reduce_theory(
        state,
        ComposeTheoryCorrection(
            theory_id=theory_id,
            version_id=version_id,
            source=source,
            composed_source=source,
            requirement_identities=(("requirement", "sail-on"),),
            pilot_rung_identities=(correction,),
            composition_identity=("compose", "sail-on"),
        ),
    )
    composed = theory_view(state)
    assert composed is not None
    assert composed.pending_overlay_identities == frozenset((correction,))

    accepted = replace(
        _attempt(
            theory_id,
            version_id,
            transition="install-sail-on",
            actions=(),
            disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
        ),
        pilot_rung_identities=(correction,),
    )
    state = reduce_theory(state, accepted)
    state = reduce_theory(
        state,
        AdvanceTheory(
            theory_id=theory_id,
            version_id=version_id,
            source=source,
            boundary=_boundary("installed", 1),
            accepted_attempt_id=accepted.attempt_identity,
            advance_identity=("advance", "installed-sail-on"),
            phase_receipts=(
                TheoryPhaseReceipt(
                    kind=TheoryPhaseKind.CORRECTION_INSTALL,
                    evidence_identity=accepted.attempt_identity,
                    pilot_rung_identities=(correction,),
                ),
            ),
        ),
    )

    installed = theory_view(state)
    assert installed is not None
    assert installed.pending_overlay_identities == frozenset()
    assert installed.overlay_identities == frozenset((correction,))


def test_intrascan_traceback_finding_is_retained_without_advancing_world() -> None:
    state, theory_id, version_id = _opened()
    source = _boundary("source", 0)
    state = reduce_theory(
        state,
        RefineTheory(
            theory_id=theory_id,
            parent_version_id=version_id,
            source=source,
            refined_source=source,
            requirements=(_requirement("link"),),
            refinement_identity=("own-intrascan-link-requirement",),
        ),
    )
    version_id = state.ledger.theories[theory_id].current_version_id
    producer_boundary = OccurrenceBoundary(
        RungId("route", 0),
        "subroutine",
        0,
        ("route",),
        1,
        0,
    )
    consumer_boundary = replace(producer_boundary, rung_id=RungId("route", 1))
    producer_write = IntrascanWriteEvidence(
        producer_boundary,
        1,
        4,
        "Step",
        10,
        98,
    )
    patch_write = IntrascanWriteEvidence(
        consumer_boundary,
        2,
        7,
        "Link",
        False,
        True,
        counterfactual=True,
    )
    useful_write = IntrascanWriteEvidence(
        consumer_boundary,
        3,
        10,
        "Step",
        98,
        10,
    )
    consumer_requirements = (
        IntrascanReadRequirement(
            consumer_boundary,
            2,
            8,
            "Link",
            True,
            "counterfactual_write",
            patch_write,
        ),
        IntrascanReadRequirement(
            consumer_boundary,
            3,
            9,
            "Step",
            98,
            "program_write",
            producer_write,
        ),
    )
    producer_requirements = (
        IntrascanReadRequirement(
            producer_boundary,
            1,
            2,
            "Link",
            False,
            "entry",
        ),
        IntrascanReadRequirement(
            producer_boundary,
            1,
            3,
            "Step",
            10,
            "entry",
        ),
    )
    request_identity = ("intrascan-traceback-request", "Link", True)
    witness = IntrascanTracebackWitness(
        request_identity=request_identity,
        source_scan=0,
        assertion_scan=1,
        applied_exactly_once=True,
        application_values=((False, True),),
        traceback_step=IntrascanTracebackStep(
            useful_write,
            consumer_requirements,
            (IntrascanProducerTrace(producer_write, producer_requirements),),
        ),
    )
    realization = IntrascanBoundaryRealization(
        stage_scan=1,
        consumer_scan=2,
        stage_write=producer_write,
        consumer_write=useful_write,
        consumer_assignments=(("Link", True),),
        witnessed=True,
    )
    finding = IntrascanTracebackFinding(
        theory_id=theory_id,
        version_id=version_id,
        source=source,
        request_identity=request_identity,
        hop_identity=("intrascan-traceback-hop", "Link", True),
        requirement_identities=(("requirement", "link"),),
        witness=witness,
        realization=realization,
    )
    fact = RecordIntrascanTraceback(finding)
    progress_id = state.ledger.theories[theory_id].current_progress_id

    recorded = reduce_theory(state, fact)
    view = theory_view(recorded)

    assert recorded.ledger.theories[theory_id].current_progress_id == progress_id
    assert recorded.ledger.theories[theory_id].traceback_finding_ids == (finding.identity,)
    assert recorded.ledger.traceback_findings[finding.identity] is finding
    assert view is not None
    assert view.traceback_finding(request_identity) is finding
    assert view.has_traceback_finding(request_identity)
    assert reduce_theory(recorded, fact) is recorded

    with pytest.raises(TheoryInvariantError, match="no exact boundary realization"):
        reduce_theory(
            state,
            RecordIntrascanTraceback(
                replace(
                    finding,
                    realization=replace(realization, witnessed=False),
                )
            ),
        )


def test_open_intrascan_traceback_frontier_is_retained_without_scan_authority() -> None:
    state, theory_id, version_id = _opened()
    source = _boundary("source", 0)
    state = reduce_theory(
        state,
        RefineTheory(
            theory_id=theory_id,
            parent_version_id=version_id,
            source=source,
            refined_source=source,
            requirements=(_requirement("step"),),
            refinement_identity=("own-intrascan-step-requirement",),
        ),
    )
    version_id = state.ledger.theories[theory_id].current_version_id
    boundary = OccurrenceBoundary(
        RungId("route", 1),
        "subroutine",
        0,
        ("route",),
        1,
        0,
    )
    useful_write = IntrascanWriteEvidence(boundary, 3, 10, "Step", 98, 10)
    request_identity = ("intrascan-traceback-request", "Step", 98)
    witness = IntrascanTracebackWitness(
        request_identity=request_identity,
        source_scan=0,
        assertion_scan=1,
        applied_exactly_once=True,
        traceback_step=IntrascanTracebackStep(
            useful_write,
            (
                IntrascanReadRequirement(
                    boundary,
                    3,
                    9,
                    "Step",
                    98,
                    "program_write",
                ),
            ),
            (),
        ),
    )
    goal = IntrascanProducerGoal(
        tag="Step",
        value=98,
        node_index=7,
        rung_id=RungId("route", 0),
        branch_path=(),
    )
    frontier = IntrascanTracebackFrontier(
        theory_id=theory_id,
        version_id=version_id,
        source=source,
        request_identity=request_identity,
        hop_identity=("intrascan-traceback-hop", "Step", 98),
        requirement_identities=(("requirement", "step"),),
        witness=witness,
        producer_goals=(goal,),
        consumer_assignments=(("Link", True),),
    )
    fact = RecordIntrascanTracebackFrontier(frontier)
    progress_id = state.ledger.theories[theory_id].current_progress_id

    recorded = reduce_theory(state, fact)
    view = theory_view(recorded)

    assert recorded.ledger.theories[theory_id].current_progress_id == progress_id
    assert recorded.ledger.theories[theory_id].traceback_frontier_ids == (frontier.identity,)
    assert recorded.ledger.traceback_frontiers[frontier.identity] is frontier
    assert view is not None
    assert view.traceback_frontier(request_identity) is frontier
    assert view.has_traceback_result(request_identity)
    assert not view.has_traceback_finding(request_identity)
    assert reduce_theory(recorded, fact) is recorded

    selected_attempt = replace(
        _attempt(
            theory_id,
            version_id,
            transition="selected-frontier-goal",
            actions=(("Reset", True),),
        ),
        investigation_frontier_id=frontier.identity,
        producer_goal_id=goal.identity,
    )
    selected = reduce_theory(recorded, selected_attempt)
    selected_receipt = selected.ledger.attempts[selected_attempt.attempt_identity]
    assert selected_receipt.investigation_frontier_id == frontier.identity
    assert selected_receipt.producer_goal_id == goal.identity

    accepted_attempt = replace(
        _attempt(
            theory_id,
            version_id,
            transition="accepted-frontier-goal",
            actions=(("Reset", True),),
            disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
        ),
        investigation_frontier_id=frontier.identity,
        producer_goal_id=goal.identity,
    )
    advanced = reduce_theory(recorded, accepted_attempt)
    advanced_source = _boundary("accepted-frontier-goal", 1)
    advanced = reduce_theory(
        advanced,
        AdvanceTheory(
            theory_id=theory_id,
            version_id=version_id,
            accepted_attempt_id=accepted_attempt.attempt_identity,
            source=source,
            boundary=advanced_source,
            advance_identity=("advance-accepted-frontier-goal",),
        ),
    )
    advanced_view = theory_view(advanced)
    assert advanced_view is not None
    assert advanced_view.current_progress_attempt_id == accepted_attempt.attempt_identity
    assert advanced_view.realized_traceback_frontier() == (
        frontier,
        goal,
        advanced.ledger.attempts[accepted_attempt.attempt_identity],
    )
    realized_finding = IntrascanTracebackFinding(
        theory_id=theory_id,
        version_id=version_id,
        source=advanced_source,
        request_identity=frontier.request_identity,
        hop_identity=frontier.hop_identity,
        requirement_identities=frontier.requirement_identities,
        witness=frontier.witness,
        realization=IntrascanBoundaryRealization(
            stage_scan=None,
            consumer_scan=advanced_source.scan_id + 1,
            stage_write=None,
            consumer_write=useful_write,
            consumer_assignments=frontier.consumer_assignments,
            witnessed=True,
        ),
        parent_frontier_id=frontier.identity,
        parent_producer_goal_id=goal.identity,
        parent_attempt_id=accepted_attempt.attempt_identity,
    )
    realized = reduce_theory(advanced, RecordIntrascanTraceback(realized_finding))
    assert realized.ledger.traceback_findings[realized_finding.identity] is realized_finding
    realized_view = theory_view(realized)
    assert realized_view is not None
    assert realized_view.realized_traceback_frontier() is None

    with pytest.raises(TheoryInvariantError, match="does not belong"):
        reduce_theory(
            recorded,
            replace(
                selected_attempt,
                attempt_identity=("wrong-goal", "owner", "occurrence"),
                producer_goal_id=("intrascan-producer-goal", "other"),
            ),
        )

    refined = reduce_theory(
        selected,
        RefineTheory(
            theory_id=theory_id,
            parent_version_id=version_id,
            source=source,
            refined_source=source,
            requirements=(_requirement("later-displacement"),),
            refinement_identity=("retain-frontier-through-later-displacement",),
        ),
    )
    refined_view = theory_view(refined)
    assert refined_view is not None
    assert refined_view.traceback_frontier(request_identity) is frontier
    assert refined_view.current_traceback_frontiers() == (frontier,)

    refined_version_id = refined.ledger.theories[theory_id].current_version_id
    child = replace(
        frontier,
        version_id=refined_version_id,
        request_identity=("intrascan-traceback-request", "RouteMode", 99),
        hop_identity=("intrascan-traceback-hop", "RouteMode", 99),
        witness=replace(
            frontier.witness,
            request_identity=("intrascan-traceback-request", "RouteMode", 99),
        ),
        requirement_identities=(("requirement", "later-displacement"),),
        parent_frontier_id=frontier.identity,
        parent_producer_goal_id=goal.identity,
        parent_attempt_id=selected_attempt.attempt_identity,
    )
    chained = reduce_theory(refined, RecordIntrascanTracebackFrontier(child))
    assert chained.ledger.traceback_frontiers[child.identity] is child
    chained_view = theory_view(chained)
    assert chained_view is not None
    assert chained_view.current_traceback_frontiers() == (child,)
    # Supersession changes actionability, not immutable research history.
    assert chained_view.traceback_frontier(request_identity) is frontier

    with pytest.raises(TheoryInvariantError, match="did not select"):
        reduce_theory(
            refined,
            RecordIntrascanTracebackFrontier(
                replace(child, parent_attempt_id=("unlinked-attempt",))
            ),
        )


def test_world_rebase_retains_only_theory_owned_overlay_at_same_physical_boundary() -> None:
    source = TheoryBoundaryIdentity(
        world_key=(("physical", "source"), ()),
        scan_id=0,
        owner_ref=_execution_ref("source"),
        occurrence_identity=("occurrence", "source"),
    )
    claim = replace(_claim(), source=source, selected_boundary=source)
    state = reduce_theory(
        TheoryState(),
        OpenTheory(claim=claim, opening_identity=("world-rebase",), remaining_budget=8),
    )
    theory_id = state.active_theory_id
    assert theory_id is not None
    version_id = state.ledger.theories[theory_id].current_version_id
    correction = ("Link", False, "guard")
    accepted = RecordTheoryAttempt(
        theory_id=theory_id,
        version_id=version_id,
        attempt_identity=("accepted", "owner", "occurrence"),
        source=source,
        execution_ref=_execution_ref("attempt-owner"),
        occurrence_evidence=("attempt-occurrence",),
        act_identity=(("Reset", True),),
        pilot_rung_identities=(correction,),
        disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
        evidence=(("scan", 1),),
    )
    state = reduce_theory(state, accepted)
    execution_boundary = replace(
        source,
        scan_id=1,
        owner_ref=_execution_ref("executed"),
        occurrence_identity=("occurrence", "executed"),
    )
    state = reduce_theory(
        state,
        AdvanceTheory(
            theory_id=theory_id,
            version_id=version_id,
            source=source,
            boundary=execution_boundary,
            accepted_attempt_id=accepted.attempt_identity,
            advance_identity=("advance", "link-low"),
            phase_receipts=(
                TheoryPhaseReceipt(
                    kind=TheoryPhaseKind.CORRECTION_INSTALL,
                    evidence_identity=("install", "link-low"),
                    pilot_rung_identities=(correction,),
                ),
            ),
        ),
    )
    composed = replace(
        execution_boundary,
        world_key=(execution_boundary.world_key[0], (correction,)),
    )

    state = reduce_theory(
        state,
        RebaseTheoryWorld(
            theory_id=theory_id,
            version_id=version_id,
            source=execution_boundary,
            rebased_source=composed,
            retained_pilot_rung_identities=(correction,),
            rebase_identity=("restore-owned-link-low",),
        ),
    )

    view = theory_view(state)
    assert view is not None
    assert view.source == composed
    progress = state.ledger.progress[state.ledger.theories[theory_id].current_progress_id]
    assert tuple(receipt.kind for receipt in progress.phase_receipts) == (
        TheoryPhaseKind.CORRECTION_INSTALL,
        TheoryPhaseKind.WORLD_REBASE,
    )

    foreign = ("Foreign", True, "guard")
    with pytest.raises(TheoryInvariantError, match="not owned by this theory"):
        reduce_theory(
            state,
            RebaseTheoryWorld(
                theory_id=theory_id,
                version_id=version_id,
                source=composed,
                rebased_source=replace(
                    composed,
                    world_key=(composed.world_key[0], (correction, foreign)),
                ),
                retained_pilot_rung_identities=(foreign,),
                rebase_identity=("foreign-overlay",),
            ),
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
        execution_ref=first.execution_ref,
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

    assert receipt.execution_ref == _execution_ref("attempt-owner-occurrence")
    assert receipt.occurrence_evidence == ("attempt-occurrence", "owner-occurrence")
    assert receipt.attempt_id == (
        "owner-occurrence",
        receipt.execution_ref,
        receipt.occurrence_evidence,
    )
    assert receipt.source.execution_ref == _execution_ref("source")
    assert receipt.execution_ref != receipt.source.execution_ref


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


def test_program_transaction_normalizes_declared_route_and_exact_target_write() -> None:
    wrapped = ChannelHeading(
        "InnerStep",
        41,
        route=RouteEdgeContext(
            channel_tag="Step",
            from_value=3,
            target_value=4,
            effect_tag="Step",
            effect_value=4,
        ),
    )
    direct = ChannelHeading("Step", 4)
    observation = EffectObservationSnapshot(
        disposition="OVERWRITTEN",
        obligation=EffectObligationSnapshot(
            tag="Step",
            value=4,
            producer=(None, 0, ()),
            consumer=None,
            required_shape=(),
            boundary=None,
        ),
        appeared=_effect_occurrence("write", 4, tag="Step", values=(3, 4)),
    )

    transaction = ProgramTransaction.from_heading(wrapped, {"Step": 3})
    assert transaction == ProgramTransaction.from_heading(direct, {"Step": 3})
    assert transaction == ProgramTransaction.from_effect_observation(
        observation,
        channel_tag="Step",
        target_value=4,
    )
    assert transaction is not None
    with pytest.raises(FrozenInstanceError):
        transaction.target_value = 5  # ty: ignore[invalid-assignment]


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


def test_conductivity_comparison_ignores_entry_state_and_consumer_annotation() -> None:
    state, theory_id, version_id = _opened()

    def observation(
        scan_id: int,
        before: int,
        consumer: tuple[Any, ...] | None,
    ) -> EffectObservationSnapshot:
        return EffectObservationSnapshot(
            disposition="OVERWRITTEN",
            obligation=EffectObligationSnapshot(
                tag="Step",
                value=40,
                producer=(None, 2, ()),
                consumer=consumer,
                required_shape=(),
                boundary=None,
            ),
            appeared=_effect_occurrence(
                "write",
                5,
                scan_id=scan_id,
                values=(before, 40),
                rung_index=2,
            ),
            displacement=_effect_occurrence(
                "write",
                21,
                scan_id=scan_id,
                values=(40, 91),
                rung_index=16,
            ),
        )

    for index, current in enumerate((observation(4, 98, (None, 4, ())), observation(5, 10, None))):
        state = reduce_theory(
            state,
            replace(
                _attempt(
                    theory_id,
                    version_id,
                    transition=f"same-produced-front-{index}",
                    actions=(("Retry", index),),
                ),
                conductivity_observations=(current,),
            ),
        )

    front = Compass().conductivity_front(theory_view(state))

    assert front is not None
    assert front.comparisons[-1].progress is ConductivityProgress.SAME_STOP


def test_conductivity_comparison_changes_when_the_produced_front_advances() -> None:
    state, theory_id, version_id = _opened()

    def observation(
        scan_id: int,
        value: int,
        producer_rung: int,
    ) -> EffectObservationSnapshot:
        return EffectObservationSnapshot(
            disposition="OVERWRITTEN",
            obligation=EffectObligationSnapshot(
                tag="Step",
                value=value,
                producer=(None, producer_rung, ()),
                consumer=None,
                required_shape=(),
                boundary=None,
            ),
            appeared=_effect_occurrence(
                "write",
                5,
                scan_id=scan_id,
                values=(40 if value == 41 else 98, value),
                rung_index=producer_rung,
            ),
            displacement=_effect_occurrence(
                "write",
                21,
                scan_id=scan_id,
                values=(value, 91),
                rung_index=16,
            ),
        )

    for index, current in enumerate((observation(4, 40, 2), observation(5, 41, 4))):
        state = reduce_theory(
            state,
            replace(
                _attempt(
                    theory_id,
                    version_id,
                    transition=f"advanced-produced-front-{index}",
                    actions=(("Advance", index),),
                ),
                conductivity_observations=(current,),
            ),
        )

    front = Compass().conductivity_front(theory_view(state))

    assert front is not None
    comparison = front.comparisons[-1]
    assert comparison.progress is ConductivityProgress.STOP_CHANGED
    assert comparison.common_stop_identity == (
        "write",
        (None, 16),
        "rung",
        16,
        (),
        0,
        "Step",
    )


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


def test_compass_research_joins_an_enclosing_guard_to_a_nested_stopping_writer() -> None:
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
        branch_selector = _effect_occurrence(
            "read",
            128,
            scan_id=scan_id,
            tag="BranchSelector",
            values=(True,),
            rung_index=17,
        )
        return EffectObservationSnapshot(
            disposition="OVERWRITTEN",
            obligation=obligation,
            appeared=appeared,
            displacement=displacement,
            observed_reads=(branch_selector,),
            displacement_enabling_reads=(watchdog_done, branch_selector),
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
    assert tuple(read.tag for read in request.enabling_reads) == (
        "Watchdog.Done",
        "BranchSelector",
    )

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


def test_temporal_request_follows_an_accepted_setup_progress_boundary() -> None:
    state, theory_id, version_id = _opened()
    rejected = _attempt(
        theory_id,
        version_id,
        transition="setup-trigger-before-progress",
        actions=(("original", True),),
    )
    state = reduce_theory(state, rejected)
    state = reduce_theory(
        state,
        RefineTheory(
            theory_id=theory_id,
            parent_version_id=version_id,
            source=rejected.source,
            refined_source=rejected.source,
            requirements=(_requirement("prior"),),
            refinement_identity=("setup-before-progress",),
            temporal_intent=TheoryTemporalIntent.SETUP_FIRST,
            trigger_attempt_id=rejected.attempt_identity,
            temporal_source=rejected.source,
        ),
    )
    version_id = state.ledger.theories[theory_id].current_version_id
    stale_request = temporal_need_request(state)
    assert stale_request is not None
    accepted = _attempt(
        theory_id,
        version_id,
        transition="accepted-setup-progress",
        actions=(("setup", True),),
        disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
    )
    state = reduce_theory(state, accepted)
    landing = _boundary("accepted-setup-landing", 1)
    state = reduce_theory(
        state,
        AdvanceTheory(
            theory_id=theory_id,
            version_id=version_id,
            accepted_attempt_id=accepted.attempt_identity,
            source=accepted.source,
            boundary=landing,
            advance_identity=("accepted-setup-progress",),
        ),
    )

    with pytest.raises(
        TheoryInvariantError,
        match="source is not the current progress boundary",
    ):
        assert_temporal_need_current(state, stale_request)
    request = temporal_need_request(state)

    assert request is not None
    assert request.source == landing


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
    boundary = _consumer_boundary()
    accepted = replace(
        _attempt(
            theory_id,
            version_id,
            transition="accepted-overlay-scan",
            actions=(("setup", True),),
            disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
        ),
        consumer_boundary=boundary,
        execution_source=_boundary("source-with-new-correctives", 0),
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
            phase_receipts=(
                TheoryPhaseReceipt(
                    kind=TheoryPhaseKind.TRANSACTION_ATTEMPT,
                    evidence_identity=accepted.attempt_identity,
                    execution_source=overlay_source,
                ),
                TheoryPhaseReceipt(
                    kind=TheoryPhaseKind.CONSUMER_BOUNDARY,
                    evidence_identity=accepted.attempt_identity,
                ),
                TheoryPhaseReceipt(
                    kind=TheoryPhaseKind.CONSUMER_STOP,
                    evidence_identity=accepted.attempt_identity,
                    execution_tip=_boundary("overlay-landing", 1),
                ),
            ),
        ),
    )
    landing = _boundary("overlay-landing", 1)
    scoped_view = theory_view(state)
    assert scoped_view is not None
    assert scoped_view.investigation_scope is not None
    assert scoped_view.investigation_scope.execution_source == overlay_source
    assert scoped_view.investigation_scope.frontier == landing
    assert scoped_view.investigation_scope.accepted_attempt_id == accepted.attempt_identity
    assert scoped_view.investigation_scope.consumer_boundary is boundary

    successor = replace(
        _attempt(
            theory_id,
            version_id,
            transition="successor-at-overlay-source",
            actions=(("successor", True),),
        ),
        source=overlay_source,
        observation_boundary=landing,
    )
    state = reduce_theory(state, successor)
    assert state.ledger.attempts[successor.attempt_identity].observation_boundary == landing
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
    retried_view = theory_view(state)
    assert retried_view is not None
    assert retried_view.investigation_scope is not None
    assert retried_view.investigation_scope.retry_act_identity == accepted.act_identity
    assert retried_view.investigation_scope.transaction_act_pairs == (("setup", True),)
    assert retried_view.investigation_scope.transaction_selected_pairs == (("setup", True),)
    assert retried_view.investigation_scope.consumer_boundary is boundary
    assert retried_view.investigation_scope.transaction_rearmed is False


def test_child_consumer_boundary_advances_without_replacing_transaction_owner() -> None:
    state, theory_id, version_id = _opened()
    transaction_boundary = _consumer_boundary()
    transaction = replace(
        _attempt(
            theory_id,
            version_id,
            transition="accepted-context-transaction",
            actions=(("Context", True),),
            disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
        ),
        consumer_boundary=transaction_boundary,
        execution_source=_boundary("transaction-execution", 0),
    )
    state = reduce_theory(state, transaction)
    transaction_landing = _boundary("transaction-landing", 1)
    state = reduce_theory(
        state,
        AdvanceTheory(
            theory_id=theory_id,
            version_id=version_id,
            accepted_attempt_id=transaction.attempt_identity,
            source=transaction.source,
            boundary=transaction_landing,
            advance_identity=("accept-context-transaction",),
            execution_source=transaction.execution_source,
            phase_receipts=(
                TheoryPhaseReceipt(
                    TheoryPhaseKind.TRANSACTION_ATTEMPT,
                    transaction.attempt_identity,
                    execution_source=transaction.execution_source,
                ),
            ),
        ),
    )

    child_boundary = _consumer_boundary(source_scan=1)
    child = replace(
        _attempt(
            theory_id,
            version_id,
            transition="accepted-child-consumer",
            actions=(("Child", True),),
            disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
        ),
        source=transaction_landing,
        consumer_boundary=child_boundary,
        execution_source=transaction_landing,
    )
    state = reduce_theory(state, child)
    state = reduce_theory(
        state,
        AdvanceTheory(
            theory_id=theory_id,
            version_id=version_id,
            accepted_attempt_id=child.attempt_identity,
            source=transaction_landing,
            boundary=_boundary("child-landing", 2),
            advance_identity=("accept-child-consumer",),
            execution_source=transaction_landing,
            phase_receipts=(
                TheoryPhaseReceipt(
                    TheoryPhaseKind.CONSUMER_BOUNDARY,
                    child.attempt_identity,
                ),
            ),
        ),
    )
    child_landing = _boundary("child-landing", 2)
    continuation = replace(
        _attempt(
            theory_id,
            version_id,
            transition="accepted-consumer-continuation",
            actions=(),
            disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
        ),
        source=child_landing,
    )
    state = reduce_theory(state, continuation)
    horizon_tip = _boundary("consumer-horizon-tip", 3)
    state = reduce_theory(
        state,
        AdvanceTheory(
            theory_id=theory_id,
            version_id=version_id,
            accepted_attempt_id=continuation.attempt_identity,
            source=child_landing,
            boundary=horizon_tip,
            advance_identity=("extend-consumer-horizon",),
            execution_source=child_landing,
            phase_receipts=(
                TheoryPhaseReceipt(
                    TheoryPhaseKind.CONSUMER_STOP,
                    continuation.attempt_identity,
                    execution_tip=horizon_tip,
                ),
            ),
        ),
    )

    view = theory_view(state)
    assert view is not None
    assert view.investigation_scope is not None
    assert view.investigation_scope.transaction_attempt_id == transaction.attempt_identity
    assert view.investigation_scope.transaction_act_pairs == (("Context", True),)
    assert view.investigation_scope.execution_source == transaction.execution_source
    assert view.investigation_scope.consumer_boundary is child_boundary
    assert view.investigation_scope.consumer_boundary_attempt_id == child.attempt_identity
    assert view.investigation_scope.consumer_stop == horizon_tip

    later_failure = replace(
        _attempt(
            theory_id,
            version_id,
            transition="failure-at-consumer-horizon",
            actions=(),
        ),
        source=transaction.execution_source,
        observation_boundary=horizon_tip,
    )
    state = reduce_theory(state, later_failure)
    assert later_failure.attempt_identity in state.ledger.attempts


def test_transaction_phase_can_supersede_an_exact_earlier_overlay() -> None:
    state, theory_id, version_id = _opened()
    setup = replace(
        _attempt(
            theory_id,
            version_id,
            transition="accepted-setup",
            actions=(("Mode", True),),
            disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
        ),
        pilot_rung_identities=(("Mode", True, "setup-guard"),),
    )
    state = reduce_theory(state, setup)
    landing = _boundary("setup-landing", 1)
    state = reduce_theory(
        state,
        AdvanceTheory(
            theory_id=theory_id,
            version_id=version_id,
            accepted_attempt_id=setup.attempt_identity,
            source=setup.source,
            boundary=landing,
            advance_identity=("accept-setup",),
            execution_source=setup.source,
            phase_receipts=(
                TheoryPhaseReceipt(
                    TheoryPhaseKind.TEMPORAL_SETUP,
                    setup.attempt_identity,
                    pilot_rung_identities=setup.pilot_rung_identities,
                ),
            ),
        ),
    )
    transaction = replace(
        _attempt(
            theory_id,
            version_id,
            transition="accepted-mode-transaction",
            actions=(("Mode", False),),
            disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
        ),
        source=landing,
        execution_source=landing,
    )
    state = reduce_theory(state, transaction)
    state = reduce_theory(
        state,
        AdvanceTheory(
            theory_id=theory_id,
            version_id=version_id,
            accepted_attempt_id=transaction.attempt_identity,
            source=landing,
            boundary=_boundary("transaction-landing", 2),
            advance_identity=("accept-mode-transaction",),
            execution_source=landing,
            phase_receipts=(
                TheoryPhaseReceipt(
                    TheoryPhaseKind.TRANSACTION_ATTEMPT,
                    transaction.attempt_identity,
                    superseded_pilot_rung_identities=setup.pilot_rung_identities,
                    execution_source=landing,
                ),
            ),
        ),
    )

    assert active_theory_pilot_rung_identities(state) == frozenset()
    assert active_theory_superseded_pilot_rung_identities(state) == frozenset(
        setup.pilot_rung_identities
    )


def test_attempt_cannot_pair_a_retained_execution_source_with_an_unowned_observation() -> None:
    state, theory_id, version_id = _opened()
    accepted = _attempt(
        theory_id,
        version_id,
        transition="accepted-owned-scan",
        actions=(("setup", True),),
        disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
    )
    state = reduce_theory(state, accepted)
    execution_source = _boundary("execution-source", 0)
    state = reduce_theory(
        state,
        AdvanceTheory(
            theory_id=theory_id,
            version_id=version_id,
            accepted_attempt_id=accepted.attempt_identity,
            source=accepted.source,
            boundary=_boundary("owned-frontier", 1),
            advance_identity=("accept-owned-scan",),
            execution_source=execution_source,
        ),
    )
    mismatched = replace(
        _attempt(
            theory_id,
            version_id,
            transition="mismatched-observation",
            actions=(("successor", True),),
        ),
        source=execution_source,
        observation_boundary=accepted.source,
    )

    with pytest.raises(
        TheoryInvariantError,
        match="attempt observation is outside its active investigation scope",
    ):
        reduce_theory(state, mismatched)


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
        owner_ref=CheckpointRef(1_000_001),
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


def test_theory_view_is_reused_until_the_immutable_state_changes() -> None:
    state, theory_id, version_id = _opened()

    first = theory_view(state)
    assert first is not None
    assert theory_view(state) is first

    updated = reduce_theory(
        state,
        _attempt(
            theory_id,
            version_id,
            transition="cached-view-boundary",
            actions=(("Reconnect", True),),
        ),
    )
    updated_view = theory_view(updated)

    assert updated_view is not None
    assert updated_view is not first
    assert theory_view(updated) is updated_view

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
        attempt = replace(attempt, execution_ref=None)  # type: ignore[arg-type]
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


def test_refine_retains_changed_boundary_in_the_row_behind_a_compact_version_id() -> None:
    state, theory_id, version_id = _opened()
    source = _boundary("source", 0)
    refined_source = replace(
        source,
        world_key=("world", "requirements-changed"),
        scan_id=1,
        owner_ref=_execution_ref("requirements-changed"),
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
    refined = state.ledger.versions[refined_version_id]
    assert refined.source == refined_source
    assert refined.parent_version_id == version_id
    assert refined_version_id[0] == "version"
    assert len(refined_version_id) == 2
    assert len(refined_version_id[1]) == 64


@pytest.mark.parametrize("changed", ["backward_scan", "owner_after_scan"])
def test_refine_fails_closed_on_inexact_refined_boundary(changed: str) -> None:
    state, theory_id, version_id = _opened()
    source = _boundary("source", 0)
    refined_source = replace(source, world_key=("world", "requirements-changed"))
    if changed == "backward_scan":
        refined_source = replace(refined_source, scan_id=-1)
    else:
        with pytest.raises(TheoryInvariantError):
            replace(refined_source, scan_id=1, owner_ref=CheckpointRef(1_000_002))
        return

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


def test_prove_closes_theory_and_retains_detached_fact() -> None:
    state, theory_id, version_id = _opened()
    fact = ProveTheory(
        theory_id=theory_id,
        version_id=version_id,
        proof_identity=("prove",),
    )

    state = reduce_theory(state, fact)

    assert state.active_theory_id is None
    assert state.ledger.theories[theory_id].status is TheoryStatus.PROVED
    assert state.ledger.applied_facts[("prove", fact.proof_identity)] == fact


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
    assert state.ledger.theories[theory_id].status is TheoryStatus.ABANDONED
    assert state.ledger.applied_facts[("abandon", fact.abandonment_identity)] == fact


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
            proof_identity=("prove",),
        ),
    )

    _assert_detached(state.ledger)
