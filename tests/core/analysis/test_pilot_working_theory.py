"""Pure lifecycle contracts for the shadow-only WorkingTheory ledger."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass, replace
from typing import Any

import pytest

from pyrung import PLC
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
    OpenSuccessor,
    OpenTheory,
    ProveTheory,
    RecordTheoryAttempt,
    RefineTheory,
    TheoryAttemptDisposition,
    TheoryBoundaryIdentity,
    TheoryClaim,
    TheoryFirstEdgeExclusion,
    TheoryInvariantError,
    TheoryObjectiveSnapshot,
    TheoryObligationSnapshot,
    TheoryRequirementSnapshot,
    TheoryState,
    TheoryTermination,
    reduce_theory,
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
            phase_receipts=(("phase", "observed"),),
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
