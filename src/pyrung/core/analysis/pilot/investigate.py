"""Build and replay bounded hypotheses for departures and excursions.

``build_deviation_incident`` freezes the recorded window. ``corrections.py``
derives three hypothesis families; investigation ranks and tests them with the
exploratory replay returned by ``build_replay_fn``.
``refinement.py`` owns bounded relational counterexample refinement and pinned
suppression nominations; this module retains compatibility facades for its
former private imports.
``_resolve_replay_attempt`` either accepts, rejects, or composes a candidate;
bounded candidate composition uses ``_compose_hypotheses`` and always replays
the composite from the original checkpoint.  This is an orient-phase
optimization, not another PILOT iteration: it neither commits a world nor
installs a correction. A surviving exploratory result receives an
evidence-derived lifetime from
``_scoped_correction_rungs`` and must survive a guarded replay before the first
confirmed composite is returned.

``investigate_excursion`` is the shorter path for a verification-reported
trial that reverted. The drive loop invokes it exactly once and returns its
replay to verification for judgment. Neither path installs its correction;
installation belongs to the orchestration/recovery owner.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal

import pyrung.core.analysis.pilot.refinement as _refinement
from pyrung.core.analysis.pilot.advance import iter_advance_owners
from pyrung.core.analysis.pilot.avoid import _hold_allowed
from pyrung.core.analysis.pilot.causal import (
    _shared_cause,
    chase_cause_roots,
    chase_chain_tags,
)
from pyrung.core.analysis.pilot.coast import (
    _COAST_BUDGET,
    _coast_holding_state,
    _coast_to_value,
    _settle_delayed_effects,
)
from pyrung.core.analysis.pilot.constrained_reachability import (
    FrontierStatus,
    NoRoute,
    Reachable,
    Unknown,
)
from pyrung.core.analysis.pilot.corrections import (
    CorrectionHypothesis,
    break_guard_holds,
    derive_correction_hypotheses,
    producer_envelope_correction_holds,
    refine_relational_hypothesis,
)
from pyrung.core.analysis.pilot.earned_work import EarnedWorkMovement
from pyrung.core.analysis.pilot.options import _holds_defeat_needed
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _pilot_rung_execution_receipt,
    _pilot_rungs_from_proposals,
    _set_pilot_rungs,
    _target_unresolved_condition,
    _union_conditions,
    fork_with_pilot_rungs,
)
from pyrung.core.analysis.pilot.pulse import _apply_pulse
from pyrung.core.analysis.pilot.recovery import (
    AttemptContext,
    CompositionBudget,
    CompositionTermination,
    Extend,
    Reject,
    Retry,
    Succeed,
    compose_corrections,
)
from pyrung.core.analysis.pilot.skiff import run_pinned_scan
from pyrung.core.analysis.pilot.trace import (
    UnsupportedConstruct,
    _can_produce,
    _route_has_no_dead_end,
    enumerate_trace_choices,
    target_reached,
    trace_back,
)
from pyrung.core.analysis.pilot.types import (
    BearingDeparture,
    DeviationIncident,
    MotionKind,
    _ActionPair,
    _ConfirmedCorrection,
    _ExecutionEvidence,
    _IterationFrame,
    _Step,
    _StepContext,
)
from pyrung.core.analysis.pilot.world_key import (
    _pilot_state_key,
    _rung_identity,
    _semantic_key,
)
from pyrung.core.analysis.sp_values import (
    _values_match,
    _written_value_for_tag,
)
from pyrung.core.context import RungId

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.types import _PilotContext
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)

_RELATIONAL_REFINEMENT_BUDGET = _refinement._RELATIONAL_REFINEMENT_BUDGET
_SKIFF_MAX_PROBES = _refinement._SKIFF_MAX_PROBES
_SKIFF_SCANS = _refinement._SKIFF_SCANS
_RelationalRefinementReceipt = _refinement._RelationalRefinementReceipt
_MAX_CANDIDATE_COMPOSITIONS = 8

ActionPair = tuple[str, Any]
CorrectionIdentity = tuple[tuple[Any, ...], ...]


class UnsupportedOccurrenceScope(RuntimeError):
    """A retained writer condition cannot be projected without widening it."""


def _proposal_pair(proposal: Any) -> ActionPair:
    if isinstance(proposal, PilotRung):
        return proposal.dest, proposal.value
    return proposal


def _proposal_identity(proposal: Any) -> tuple[str, Any]:
    """Pre-install identity used only to compare generated hypotheses.

    A hypothesis names the corrective write and optional operation boundary,
    not its eventual installed lifetime.  Pair and ``PilotRung`` forms of the
    same idea therefore remain equivalent during causal closure; durable
    negative evidence uses :func:`correction_identity` only after scoping.
    """
    if isinstance(proposal, PilotRung):
        return proposal.dest, (
            _semantic_key(proposal.value),
            _semantic_key(proposal.operation),
        )
    tag, value = proposal
    return tag, (_semantic_key(value), None)


def _hypothesis_identity(proposals: Iterable[Any]) -> tuple[tuple[str, Any], ...]:
    """Stable comparison key for proposals that may not own a scope yet."""
    pairs = map(_proposal_identity, proposals)
    return tuple(sorted(pairs, key=lambda pair: (pair[0], repr(pair[1]))))


def correction_identity(pilot_rungs: Iterable[PilotRung]) -> CorrectionIdentity:
    """Exact identity of an executable, replay-confirmed correction.

    ``_rung_identity`` owns executable identity, including the guard and any
    owner-issued operation boundary.  A generated pair is only a hypothesis;
    it cannot become durable negative evidence until investigation gives it a
    scope and confirms that exact installed form.
    """
    identities: list[tuple[Any, ...]] = []
    for rung in pilot_rungs:
        if not isinstance(rung, PilotRung):
            raise TypeError("correction identity requires executable PilotRungs")
        identities.append(_rung_identity(rung))
    return tuple(sorted(identities, key=repr))


ReplayFn = Callable[[tuple[Any, ...]], "ReplayOutcome"]


@dataclass(frozen=True)
class ReplayStep:
    """One recorded journey step with its session spec, replay-ready.

    ``kind`` is the private replay kind (``"pulse"`` / ``"bearing_coast"`` /
    ``"letrun"`` / ``"dwell"``), read off the committed step context — never
    inferred from position or input emptiness. A bearing coast re-arms its own
    recorded ``channel_tag``/``channel_target``; a letrun step re-coasts
    toward the global target bounded by its own recorded span.
    """

    inputs: tuple[tuple[str, Any], ...]
    scans: int
    kind: str
    channel_tag: str | None = None
    channel_target: Any = None


def _replay_step(step: _Step, context: _StepContext) -> ReplayStep:
    """Map one recorded physical step and its operation context to replay."""

    kind = {
        MotionKind.INTERVENTION: "pulse",
        MotionKind.COAST_TO_BEARING: "bearing_coast",
        MotionKind.COAST_HOLDING_WORLD: "letrun",
    }[context.policy.motion]
    channel_motion = context.execution.channel_motion
    if kind == "bearing_coast" and channel_motion.channel_tag is None:
        kind = "dwell"
    return ReplayStep(
        inputs=tuple(step.inputs.items()),
        scans=step.scans,
        kind=kind,
        channel_tag=channel_motion.channel_tag,
        channel_target=channel_motion.target_value,
    )


def _deviation_bearing(
    execution: _ExecutionEvidence,
    frame: _IterationFrame,
    watch_tags: list[str],
    frontier: tuple[_ActionPair, ...],
) -> tuple[_ActionPair, ...]:
    """Facts the failed operation actually held and then lost."""

    needed_by_tag: dict[str, list[Any]] = {}
    for tag, value in frontier:
        needed_by_tag.setdefault(tag, []).append(value)
    bearing: list[_ActionPair] = [
        (tag, frame.snap.get(tag))
        for tag in watch_tags
        if not _values_match(frame.snap.get(tag), execution.after_snap.get(tag))
        and not any(
            _values_match(execution.after_snap.get(tag), needed)
            for needed in needed_by_tag.get(tag, ())
        )
    ]
    channel = execution.channel_motion.channel_tag
    if channel is not None:
        source = execution.before_snap.get(channel)
        landed = execution.after_snap.get(channel)
        if not _values_match(landed, source):
            bearing = [(tag, value) for tag, value in bearing if tag != channel]
            bearing.append((channel, source))
    return tuple(bearing)


@dataclass(frozen=True)
class CausalOccurrence:
    """One exact rung/write occurrence on a recorded causal explanation."""

    rung: RungId
    tag: str
    value: Any


@dataclass(frozen=True)
class RegressionWitness:
    """Exact causal explanation for a recorded channel regression.

    The witness is operation-shaped, not behavior-shaped. It retains the
    concrete changed writes on the recorded ``cause()`` chain, bounded to the
    incident, plus the full causal spine that owns those writes. A shared state
    executor may therefore run later for an unrelated request without
    reproducing this regression; replay has to reproduce the recorded causal
    branch, not merely reuse its final writer.
    """

    channel_tag: str
    source: Any
    departed: Any
    # The bounded incident may pass through the same first channel edge and
    # executor pipeline yet reach a different outcome. Keep that landing as
    # part of the witness so nested investigation groups effects, not plumbing.
    landing: Any
    departure_scan: int
    cause: tuple[CausalOccurrence, ...]
    causal_spine: frozenset[str]
    causal_roots: tuple[tuple[str, Any], ...] = ()
    # Snapshot in which synthetic guards were evaluated before the causal
    # departure scan. A correction may become active long after the incident
    # anchor, so lifecycle ownership cannot be reconstructed from the anchor.
    owner_snapshot: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ReplacementEvidence:
    """A counterfactual branch that reproduced a channel departure.

    ``plc`` is the replay fork that observed the branch.  Investigation needs
    that exact history to derive a correction for the newly exposed cause;
    replay owns observation, not interpretation. ``shared_suffix`` is ordered
    effect-backward and contains the exact rung/write pipeline common to the
    recorded and replacement causes of the same bounded channel outcome.
    """

    plc: Any
    incident: DeviationIncident
    witness: RegressionWitness
    shared_suffix: tuple[CausalOccurrence, ...] = ()


@dataclass(frozen=True)
class ReplayIncident:
    """The bounded occurrence and judgment evidence a replay must reproduce."""

    channel_tag: str | None = None
    channel_target: Any = None
    terminal_role_tags: tuple[str, ...] | None = None
    watch_roles: tuple[str, ...] = ()
    departure_bearing: tuple[ActionPair, ...] = ()
    regression_witness: RegressionWitness | None = None
    earned_work: Any = None
    progress_anchor: Mapping[str, Any] | None = None
    regression_progress_floor: Mapping[str, Any] | None = None


# ---------------------------------------------------------------------------
# Incident / hypothesis / result types
# ---------------------------------------------------------------------------


class ReplayJustification(Enum):
    """The target-relative ground on which a replayed correction succeeded."""

    REACHED = "reached"
    NEUTRALIZED = "neutralized"
    ADVANCED = "advanced"
    BEARING_HELD = "bearing-held"


@dataclass(frozen=True)
class ReplayOutcome:
    """Pilot's replay judgment for a proposed hold set."""

    accepted: bool
    trend: int | None
    snapshot: Mapping[str, Any]
    reason: str = ""
    justification: ReplayJustification | None = None
    continuation: FrontierStatus = Unknown("target continuation was not inspected")
    continuation_snapshot: Mapping[str, Any] | None = None
    # Whether ``snapshot`` is a real LANDING (target reached, or the coast
    # departed and settled somewhere) rather than a mid-journey timeout.  A
    # departure-silenced acceptance times out with the channel intact — its
    # snapshot is where the budget ran out, not a destination, and channel
    # scoping must not derive a lifetime from it.
    landed: bool = True
    # Causal spine of a replacement channel departure after the recorded
    # regression was suppressed. Investigation composes this receipt with its
    # sibling hypotheses; replay itself does not know the incident's full
    # proposal set.
    replacement_cause: frozenset[str] = frozenset()
    replacement: ReplacementEvidence | None = None


def _continuation_ground(status: FrontierStatus) -> str:
    """Compatibility facade for refinement's continuation-ground renderer."""

    return _refinement._continuation_ground(status)


def _refine_unknown_continuation(
    candidate: CorrectionHypothesis,
    replay_outcome: ReplayOutcome,
    ctx: Any,
    receipt: _RelationalRefinementReceipt,
) -> tuple[CorrectionHypothesis | None, str]:
    """Compatibility facade for bounded relational counterexample refinement."""

    return _refinement._refine_unknown_continuation(
        candidate,
        replay_outcome,
        ctx,
        receipt,
        refiner=refine_relational_hypothesis,
        identity=_hypothesis_identity,
    )


@dataclass(frozen=True)
class InvestigationRejection:
    """One rejected hypothesis and the exact ground that rejected it."""

    hypothesis: CorrectionHypothesis
    slug: str
    ground: str


@dataclass(frozen=True)
class _ReplayAccepted:
    """A replay attempt accepted without exposing another causal cut."""

    outcome: ReplayOutcome


@dataclass(frozen=True)
class _CandidateComposed:
    """A replay exposed another causal cut and composed one candidate."""

    hypothesis: CorrectionHypothesis


@dataclass(frozen=True)
class _ReplayRejected:
    """A replay attempt rejected the current hypothesis on an exact ground."""

    rejection: InvestigationRejection


_ReplayResolution = _ReplayAccepted | _CandidateComposed | _ReplayRejected


@dataclass(frozen=True)
class _InvestigationCompositionCandidate:
    """One hypothesis plus retry evidence retained across its extensions."""

    hypothesis: CorrectionHypothesis
    refinement: _RelationalRefinementReceipt


@dataclass(frozen=True)
class _InvestigationConfirmation:
    hypothesis: CorrectionHypothesis
    correction: _ConfirmedCorrection


@dataclass(frozen=True)
class InvestigationResult:
    """Replay-confirmed corrective information."""

    correction: _ConfirmedCorrection | None = None
    regression_nogoods: frozenset[ActionPair] = frozenset()
    hypotheses: tuple[CorrectionHypothesis, ...] = ()
    confirmed: tuple[CorrectionHypothesis, ...] = ()
    # A rejection is one artifact: its hypothesis, stable machine-readable
    # classification, and human ground cannot become index-desynchronized.
    rejected: tuple[InvestigationRejection, ...] = ()
    unresolved: tuple[str, ...] = ()


def _resolve_replay_attempt(
    *,
    phase: Literal["exploratory", "guarded"],
    current: CorrectionHypothesis,
    outcome: ReplayOutcome,
    seen_replacements: set[tuple[Any, ...]],
    extend: Callable[
        [CorrectionHypothesis, ReplacementEvidence],
        CorrectionHypothesis | None,
    ],
) -> _ReplayResolution:
    """Resolve one replay without flattening acceptance, extension, or rejection.

    Replacement identity is shared across exploratory and guarded attempts in
    one investigation. A repeated cause is rejected before another extension
    is derived, so causal closure cannot manufacture work from a cycle.
    """
    if not outcome.accepted:
        return _ReplayRejected(
            InvestigationRejection(
                current,
                f"{phase}-replay-failed",
                f"{phase} replay rejected: " + (outcome.reason or "no replay reason supplied"),
            )
        )

    replacement = outcome.replacement
    if replacement is None or not replacement.shared_suffix:
        return _ReplayAccepted(outcome)

    fingerprint = _replacement_identity(replacement)
    if fingerprint in seen_replacements:
        ground = (
            "counterfactual replacement cause repeated inside one investigation"
            if phase == "exploratory"
            else "guarded replay repeated a counterfactual replacement cause"
        )
        return _ReplayRejected(InvestigationRejection(current, "nested-cause-cycle", ground))
    seen_replacements.add(fingerprint)

    extended = extend(current, replacement)
    if extended is None:
        prefix = "replacement" if phase == "exploratory" else "guarded replacement"
        return _ReplayRejected(
            InvestigationRejection(
                current,
                "nested-cause-unresolved",
                f"{prefix} reproduced the same bounded outcome and pipeline "
                "but yielded no additional corrective cut",
            )
        )
    return _CandidateComposed(extended)


def _replacement_identity(replacement: ReplacementEvidence) -> tuple[Any, ...]:
    """Exact identity of one replacement cause inside a composition chain."""

    return tuple(
        (item.rung, item.tag, _semantic_key(item.value)) for item in replacement.witness.cause
    )


def _scoped_correction_rungs(
    plc: PLC,
    proposals: tuple[Any, ...],
    incident: DeviationIncident,
    outcome: ReplayOutcome,
    ctx: Any,
    progress_mark: tuple[tuple[str, Any], ...] = (),
    producer_envelope: bool = False,
) -> tuple[PilotRung, ...]:
    """Give a replay-successful correction its evidence-derived lifetime.

    The installed form is scoped from the incident-local replay and replayed
    *again* before confirmation:

    * neutralizing a recorded channel incident -> remain active only while
      that incident's source context holds;
    * other observed channel motion -> remain active until its bounded
      landing;
    * no channel evidence -> the target-unresolved outer boundary.

    When the caller has an exact progress receipt, its source mark further
    narrows that lifetime.  A correction proved while the recipe sat at Step
    101, for example, cannot keep owning the same input after the recipe earns
    Step 103 without a new proof for that occurrence.

    An operation-bearing :class:`PilotRung` already owns its lifetime and
    passes through unchanged. This scoper never manufactures operation
    ownership: only a program/trace owner that can name the operation's actual
    completion and progress conditions may issue that receipt. A guard-only
    rung from causal exposure names where the harmful writer fired; for a
    channel incident that is evidence about the correction, not its start
    time. The replay-derived source scope replaces that exposure guard so
    prerequisite inputs can settle before the harmful state begins.
    """
    if incident.occurrence_conditions:
        occurrence_scope = _retained_occurrence_scope(proposals, incident)
        return _retained_scoped_rungs(
            plc,
            proposals,
            occurrence_scope,
            ctx,
        )

    if all(
        isinstance(proposal, PilotRung) and proposal.operation is not None for proposal in proposals
    ):
        return tuple(proposals)

    if producer_envelope and all(isinstance(proposal, PilotRung) for proposal in proposals):
        # The proposer retained every non-lever producer and caller condition.
        # This broader executable form is replayed below before installation;
        # EarnedWork remains the fallback when that structural proof is absent.
        # Bound the envelope by the outer objective so a sibling producer after
        # the target (for example Completed -> Resetting) cannot keep ownership
        # after the route has already landed. Rung-entry evaluation keeps the
        # correction active through the target-establishing scan.
        from pyrung.core.condition import AllCondition

        unresolved = _target_unresolved_condition(
            plc,
            ctx.target.tag,
            ctx.target.value,
            ctx.target.predicate,
        )
        return tuple(
            PilotRung(
                proposal.dest,
                proposal.value,
                AllCondition(unresolved, proposal.guard),
                operation=proposal.operation,
            )
            for proposal in proposals
        )

    scoped_proposals = tuple(
        (proposal.dest, proposal.value)
        if isinstance(proposal, PilotRung)
        and proposal.operation is None
        and (incident.channel_tag is not None or progress_mark)
        else proposal
        for proposal in proposals
    )

    channel_tag = incident.channel_tag
    exposure_guards = tuple(
        proposal.guard
        for proposal in proposals
        if isinstance(proposal, PilotRung) and proposal.operation is None
    )
    if (
        channel_tag is not None
        and (channel := plc._known_tags_by_name.get(channel_tag)) is not None
        and (outcome.justification is ReplayJustification.NEUTRALIZED or outcome.landed)
    ):
        from pyrung.core.condition import CompareEq, CompareNe

        before = incident.before_snap.get(channel_tag)
        if (
            outcome.justification is ReplayJustification.NEUTRALIZED
            and not outcome.landed
            and exposure_guards
        ):
            # The bounded proof observed no safe landing. Preserve the exact
            # source-through-exposure lifetime that succeeded exploratorily;
            # narrowing back to the source would release the correction on the
            # first harmful intermediate state.
            scope = _union_conditions((CompareEq(channel, before), *exposure_guards))
        else:
            landing = outcome.snapshot.get(channel_tag) if outcome.landed else before
            if outcome.landed and progress_mark and exposure_guards:
                # Reaching the safe channel value is not yet an acknowledgment:
                # the user program reads that landing on its next scan. Keep
                # the correction across the observed source/intermediate/
                # landing corridor until the earned-work coordinate advances.
                scope = _union_conditions(
                    (
                        CompareEq(channel, before),
                        *exposure_guards,
                        CompareEq(channel, landing),
                    )
                )
            else:
                scope = (
                    CompareEq(channel, before)
                    if _values_match(landing, before)
                    else CompareNe(channel, landing)
                )
    else:
        scope = _target_unresolved_condition(
            plc,
            ctx.target.tag,
            ctx.target.value,
            ctx.target.predicate,
        )
    coordinates = []
    if progress_mark:
        from pyrung.core.condition import AllCondition, CompareEq

        for tag_name, value in progress_mark:
            tag = plc._known_tags_by_name.get(tag_name)
            if tag is None:
                raise KeyError(f"progress receipt tag {tag_name!r} is not a program tag")
            coordinates.append(CompareEq(tag, value))
        scope = AllCondition(scope, *coordinates)
    return tuple(_pilot_rungs_from_proposals(list(scoped_proposals), scope))


def _retained_occurrence_scope(
    proposals: tuple[Any, ...],
    incident: DeviationIncident,
) -> Any:
    """Project corrected direct conjuncts out of one observed writer guard.

    Rung-level conditions are an implicit conjunction. A conjunct that reads
    only a corrected destination is the lever term the correction deliberately
    falsifies; the remaining original condition objects name the exact writer
    opportunity. Mixed/nested dependencies are declined because dropping one
    would widen the occurrence beyond recorded evidence.
    """

    from pyrung.core.analysis.pdg import _extract_reads_from_condition
    from pyrung.core.condition import AllCondition

    corrected = {_proposal_pair(proposal)[0] for proposal in proposals}
    retained: list[Any] = []
    for condition in incident.occurrence_conditions:
        reads = set(_extract_reads_from_condition(condition, {}))
        overlap = reads & corrected
        if not overlap:
            retained.append(condition)
            continue
        if not reads <= corrected:
            raise UnsupportedOccurrenceScope(
                "retained writer has a mixed condition containing both the "
                f"corrected lever and occurrence context: {condition!r}"
            )
    if not retained:
        raise UnsupportedOccurrenceScope(
            "retained writer has no independent condition left after projecting the corrected lever"
        )
    return retained[0] if len(retained) == 1 else AllCondition(*retained)


def _retained_scoped_rungs(
    plc: PLC,
    proposals: tuple[Any, ...],
    occurrence_scope: Any,
    ctx: Any,
) -> tuple[PilotRung, ...]:
    """Start at the exact occurrence and self-continue only to the target.

    The corrected value is an executable receipt that this guard-only rule
    already fired. Combining it with the target's unresolved condition keeps
    the correction through the target-establishing scan, then releases it on
    the following scan. This is deliberately not an ``OperationReceipt``:
    retained causal evidence does not own a program operation contract.
    """

    from pyrung.core.condition import AllCondition, AnyCondition, CompareEq

    unresolved = _target_unresolved_condition(
        plc,
        ctx.target.tag,
        ctx.target.value,
        ctx.target.predicate,
    )
    result: list[PilotRung] = []
    for proposal in proposals:
        if isinstance(proposal, PilotRung) and proposal.operation is not None:
            result.append(proposal)
            continue
        tag, value = _proposal_pair(proposal)
        dest = plc._known_tags_by_name.get(tag)
        if dest is None:
            raise KeyError(f"retained correction tag {tag!r} is not a program tag")
        continuation = CompareEq(dest, value)
        # The historical occurrence owns the start even when the replay-floor
        # snapshot already satisfies the eventual target.  Requiring
        # ``unresolved`` on that same scan would prevent the intervention that
        # preserves the target from ever starting.  After the occurrence, the
        # corrected value self-continues only while the target is unresolved.
        guard = AnyCondition(
            occurrence_scope,
            AllCondition(unresolved, continuation),
        )
        result.append(PilotRung(tag, value, guard))
    return tuple(result)


def _discharges_occurrence_requirements(
    proposals: tuple[Any, ...],
    requirements: tuple[tuple[str, Any], ...],
) -> bool:
    """Whether *proposals* directly falsify every recorded support demand.

    The requirements come from the already-recorded producer occurrence.  This
    reader does not reconstruct its cause or widen a guard: it only recognizes
    the correction that changes each exact external support away from the value
    that made the producer conductive.
    """
    if not proposals or not requirements:
        return False
    demanded = {tag: value for tag, value in requirements}
    corrected: set[str] = set()
    for proposal in proposals:
        tag, value = _proposal_pair(proposal)
        if tag not in demanded or _values_match(value, demanded[tag]):
            return False
        corrected.add(tag)
    return corrected == set(demanded)


def _exploratory_correction_rungs(
    plc: PLC,
    proposals: tuple[Any, ...],
    incident: DeviationIncident,
    progress_mark: tuple[tuple[str, Any], ...],
    ctx: Any,
) -> tuple[Any, ...]:
    """Test a raw correction only where the incident was observed.

    A global exploratory hold is a broader intervention than the guarded fact
    PILOT would install. It can therefore erase earlier, compatible work and
    reject a locally-correct hypothesis for behavior the hypothesis would never
    own. An exact EarnedWork mark is enough to keep that first replay at the incident;
    the accepted outcome still supplies the narrower channel lifetime below.
    """

    from pyrung.core.condition import AllCondition, AnyCondition, CompareEq

    if incident.occurrence_conditions:
        occurrence_scope = _retained_occurrence_scope(proposals, incident)
        return _retained_scoped_rungs(plc, proposals, occurrence_scope, ctx)

    progress_coordinates = []
    source_scope = None
    channel_name = incident.channel_tag
    if channel_name is not None:
        channel = plc._known_tags_by_name.get(channel_name)
        if channel is None:
            raise KeyError(f"incident channel {channel_name!r} is not a program tag")
        source_scope = CompareEq(channel, incident.before_snap.get(channel_name))
    for tag_name, value in progress_mark:
        tag = plc._known_tags_by_name.get(tag_name)
        if tag is None:
            raise KeyError(f"progress receipt tag {tag_name!r} is not a program tag")
        progress_coordinates.append(CompareEq(tag, value))
    if source_scope is None and not progress_coordinates:
        return proposals

    result: list[PilotRung] = []
    for proposal in proposals:
        if isinstance(proposal, PilotRung) and proposal.operation is not None:
            result.append(proposal)
            continue
        if isinstance(proposal, PilotRung):
            # Causal exposure is where the harmful writer becomes conductive.
            # Start in the source context and keep the input owned through that
            # exposure; otherwise a Boolean overlay releases it on the first
            # intermediate channel scan.
            lifetime = (
                AnyCondition(source_scope, proposal.guard)
                if source_scope is not None
                else proposal.guard
            )
            guard = (
                AllCondition(lifetime, *progress_coordinates) if progress_coordinates else lifetime
            )
            result.append(PilotRung(proposal.dest, proposal.value, guard))
            continue
        guard_terms = (
            *((source_scope,) if source_scope is not None else ()),
            *progress_coordinates,
        )
        result.extend(_pilot_rungs_from_proposals([proposal], AllCondition(*guard_terms)))
    return tuple(result)


# ---------------------------------------------------------------------------
# Replay harness — fork, hold, replay steps, trace-back, judge
# ---------------------------------------------------------------------------


def incident_regression_witness(
    plc: PLC,
    incident: DeviationIncident,
) -> RegressionWitness | None:
    """Recover the exact causal branch of the incident's channel transition.

    ``cause()`` already resolves the recorded writer occurrence, including
    its upstream owners and replay-backed recovery when a firing timeline was
    filtered. Reuse the changed rung/write occurrences inside this incident.
    This makes the full recorded explanation the identity: a later cause may
    share the generic state executor without being mistaken for this cause.
    If the branch cannot be
    attributed, return ``None`` and let replay decline local-neutralization
    proof rather than substituting a behavior category.
    """
    channel = incident.channel_tag
    departure = next(
        (
            departure
            for departure in incident.departures
            if departure.tag == channel and departure.scan is not None
        ),
        None,
    )
    if channel is None or departure is None or departure.scan is None:
        return None
    chain = _shared_cause(plc, channel, departure.scan)
    if chain is None:
        return None
    effect = chain.effect
    if (
        effect.tag_name != channel
        or effect.scan_id != departure.scan
        or not _values_match(effect.from_value, departure.value)
        or _values_match(effect.from_value, effect.to_value)
    ):
        return None
    cause: list[CausalOccurrence] = []
    for step in chain.steps:
        transition = step.transition
        if (
            transition.scan_id <= incident.anchor_scan
            or transition.scan_id > departure.scan
            or _values_match(transition.from_value, transition.to_value)
        ):
            continue
        occurrence = CausalOccurrence(
            rung=RungId(step.subroutine, step.rung_index),
            tag=transition.tag_name,
            value=transition.to_value,
        )
        if not any(
            prior.rung == occurrence.rung
            and prior.tag == occurrence.tag
            and _values_match(prior.value, occurrence.value)
            for prior in cause
        ):
            cause.append(occurrence)
    if not cause:
        return None
    if not any(
        occurrence.tag == channel and _values_match(occurrence.value, effect.to_value)
        for occurrence in cause
    ):
        return None
    return RegressionWitness(
        channel_tag=channel,
        source=effect.from_value,
        departed=effect.to_value,
        landing=incident.after_snap.get(channel),
        departure_scan=departure.scan,
        cause=tuple(cause),
        causal_spine=frozenset(chase_chain_tags(plc, channel, scan=departure.scan)),
        causal_roots=tuple((root.tag_name, root.value) for root in chain.roots),
        owner_snapshot=(
            dict(plc.history.at(departure.scan - 1).tags)
            if departure.scan > plc.history.oldest_scan_id
            else dict(incident.before_snap)
        ),
    )


def _regression_cause_replayed(
    plc: PLC,
    witness: RegressionWitness,
    *,
    start_scan: int,
    end_scan: int,
) -> bool:
    """Whether replay reproduced every changed write on the recorded cause.

    A later fault may legitimately share the response pipeline and its generic
    state-copy rung. It reproduces this regression only when the replay firing
    evidence subsumes the whole incident-bounded causal signature. Exact
    interpreted captures recover attempted writes hidden by a same-scan mask.
    """
    remaining = list(witness.cause)
    for scan in range(start_scan + 1, end_scan + 1):
        main_firings = plc.rung_firings(scan)
        node_firings = plc._node_firings_at(scan)
        ambiguous: set[RungId] = set()
        matched: list[CausalOccurrence] = []
        for occurrence in remaining:
            writes = (
                main_firings.get(occurrence.rung.rung_index)
                if occurrence.rung.subroutine is None
                else node_firings.get(occurrence.rung)
            )
            if writes is None:
                continue
            if occurrence.tag in writes and _values_match(
                writes[occurrence.tag],
                occurrence.value,
            ):
                matched.append(occurrence)
            elif not writes:
                # Compact firing timelines retain an empty map when a rung
                # executed but its writes were PDG-filtered.
                ambiguous.add(occurrence.rung)
        if ambiguous:
            for run in plc._replay_rung_runs_at(scan):
                if run.rung_id not in ambiguous or not run.enabled:
                    continue
                attempted = dict(run.writes)
                for occurrence in remaining:
                    if (
                        occurrence.rung == run.rung_id
                        and occurrence.tag in attempted
                        and _values_match(attempted[occurrence.tag], occurrence.value)
                    ):
                        matched.append(occurrence)
        if matched:
            remaining = [occurrence for occurrence in remaining if occurrence not in matched]
            if not remaining:
                return True
    return False


@dataclass(frozen=True)
class _RegressionOwnership:
    """Current replay evidence about one recorded regression branch."""

    source_preserved: bool
    cause_silenced: bool
    replacement_cause: frozenset[str] | None
    replacement_owned: bool | None
    replacement_replays_recorded: bool | None
    unrelated_departure: bool
    neutralized: bool
    shared_suffix: tuple[CausalOccurrence, ...] = ()


def _replacement_departure_scan(
    witness: RegressionWitness,
    events: Sequence[Any],
) -> int | None:
    """First counterfactual departure from the recorded channel source."""
    return next(
        (
            event.scan
            for event in events
            for tag, before, after in event.transitions
            if tag == witness.channel_tag
            and _values_match(before, witness.source)
            and not _values_match(after, witness.source)
        ),
        None,
    )


def _same_occurrence(left: CausalOccurrence, right: CausalOccurrence) -> bool:
    return (
        left.rung == right.rung and left.tag == right.tag and _values_match(left.value, right.value)
    )


def _same_bounded_channel_outcome(
    recorded: RegressionWitness,
    replacement: RegressionWitness,
) -> bool:
    """Whether two witnesses describe the same bounded transition outcome.

    The first departure identifies the participating transition. The landing
    distinguishes another cause of the failure from a healthy path that merely
    begins with the same transition and uses the same executor machinery.
    """
    return (
        recorded.channel_tag == replacement.channel_tag
        and _values_match(recorded.source, replacement.source)
        and _values_match(recorded.departed, replacement.departed)
        and _values_match(recorded.landing, replacement.landing)
    )


def _same_bounded_channel_departure(
    recorded: RegressionWitness,
    replacement: RegressionWitness,
) -> bool:
    """Whether replay preserved the incident's first channel transition."""
    return (
        recorded.channel_tag == replacement.channel_tag
        and _values_match(recorded.source, replacement.source)
        and _values_match(recorded.departed, replacement.departed)
    )


def _shared_causal_suffix(
    recorded: RegressionWitness,
    replacement: RegressionWitness | None,
) -> tuple[CausalOccurrence, ...]:
    """Exact downstream pipeline shared by two effect-backward witnesses.

    ``CausalChain.steps`` and therefore :attr:`RegressionWitness.cause` are
    effect-first. Their common prefix is the program's common downstream
    suffix. One shared occurrence is normally only the generic channel
    executor; two prove a participating transition plus its executor pipeline.
    The bounded landing must also match: the same first hop through the same
    plumbing can be a healthy detour rather than another cause of the failure.
    """
    if replacement is None or not _same_bounded_channel_outcome(recorded, replacement):
        return ()
    common: list[CausalOccurrence] = []
    for left, right in zip(recorded.cause, replacement.cause, strict=False):
        if not _same_occurrence(left, right):
            break
        common.append(left)
    return tuple(common) if len(common) >= 2 else ()


def _regression_ownership(
    plc: PLC,
    witness: RegressionWitness,
    events: Sequence[Any],
    proposal_tags: set[str],
    *,
    start_scan: int,
    end_scan: int,
    replacement_witness: RegressionWitness | None = None,
) -> _RegressionOwnership:
    """Judge the recorded branch and any replacement inside its bounded replay.

    Replay does not chase a future stable landing. It does own departures
    already visible inside the recorded horizon. Reproducing the same bounded
    outcome on the proposal's causal spine disproves the proposal. A
    proposal-owned replacement with a different landing has instead changed
    this recorded incident; that landing remains probationary evidence for a
    later live-loop iteration.
    """
    bounded_events = tuple(event for event in events if event.scan <= end_scan)
    source_preserved = _values_match(
        plc.state.tags.get(witness.channel_tag), witness.source
    ) and not any(
        tag == witness.channel_tag and not _values_match(after, witness.source)
        for event in bounded_events
        for tag, _before, after in event.transitions
    )
    changed_writes_silenced = not _regression_cause_replayed(
        plc,
        witness,
        start_scan=start_scan,
        end_scan=end_scan,
    )
    replacement_cause = (
        replacement_witness.causal_spine if replacement_witness is not None else None
    )
    replacement_owned = (
        bool(proposal_tags & replacement_cause) if replacement_cause is not None else None
    )
    replacement_replays_recorded = (
        _same_bounded_channel_outcome(witness, replacement_witness)
        and witness.causal_spine.issubset(replacement_cause)
        if replacement_cause is not None and replacement_witness is not None
        else None
    )
    unrelated_departure = (
        replacement_cause is not None
        and replacement_replays_recorded is False
        and replacement_owned is False
    )
    shared_suffix = _shared_causal_suffix(witness, replacement_witness)
    branch_replaced = (
        bool(shared_suffix) and replacement_replays_recorded is False and replacement_owned is False
    )
    proposal_owned_detour = (
        replacement_witness is not None
        and replacement_owned is True
        and replacement_replays_recorded is False
        and _same_bounded_channel_departure(witness, replacement_witness)
        and not _same_bounded_channel_outcome(witness, replacement_witness)
    )
    cause_silenced = changed_writes_silenced or branch_replaced or proposal_owned_detour
    unrelated_departure = unrelated_departure and not shared_suffix
    return _RegressionOwnership(
        source_preserved=source_preserved,
        cause_silenced=cause_silenced,
        replacement_cause=replacement_cause,
        replacement_owned=replacement_owned,
        replacement_replays_recorded=replacement_replays_recorded,
        unrelated_departure=unrelated_departure,
        neutralized=(
            (source_preserved and cause_silenced)
            or branch_replaced
            or proposal_owned_detour
            or unrelated_departure
        ),
        shared_suffix=shared_suffix,
    )


def build_replay_fn(
    cp_fork: PLC,
    cp_trend: int,
    pilot_rungs: Sequence[Any],
    steps: Sequence[ReplayStep],
    *,
    ctx: _PilotContext,
    incident: ReplayIncident | None = None,
) -> ReplayFn:
    """Build a replay callback for ``investigate_deviation``.

    The returned function forks from the checkpoint, installs existing holds
    plus the proposed hypothesis holds, and re-runs the act that surfaced the
    regression.

    The judgment depends on the incident shape:

    * **Channel incident** (``bearing_channel_tag`` set — a bearing coast or a
      terminal let-run holding a macro-state) — a hold is *good* iff the
      channel register sits at its target/held value instead of ejecting.  The
      coast differs by shape: a **bearing** coast is unbounded and ejection-guarded
      (the immediate bearing may be a full coast away), a **let-run** coast is
      **bounded** to the departure window (its far-off global target is
      unreachable inside it).  In both cases the bearing's far-off conjuncts (the
      channel target, the global target, unrelated watch tags) are *not*
      required — only that the register did not eject — because a bounded coast
      cannot restore them and the bearing-held test would reject every hold.
    * **Terminal let-run without a channel register** — judge the global
      target at the bounded point.
    * **Command incident** — judge *departure_bearing* directly, else fall back
      to comparing the trace-back trend against the checkpoint trend.

    A correction need not finish the remaining route. When the recorded
    regression was a channel departure, suppressing its exact changed-write
    branch inside the recorded incident window is local **neutralization** and
    is sufficient. Merely overwriting that branch's result is not: exact firing
    testimony still detects masking. A replacement departure already inside
    that bounded window is accepted only when its cause is unrelated to the
    proposal; motion beyond the window belongs to a later live incident.
    """

    incident = incident or ReplayIncident()
    resting = ctx.resting
    edge_tags = ctx.edge_tags
    target_tag = ctx.target.tag
    target_value = ctx.target.value
    pdg = ctx.pdg
    program = ctx.program
    steerable = ctx.steerable
    opaque_loop = ctx.opaque_loop
    pipeline_internal_tags = ctx.pipeline_internal_tags
    route = ctx.route
    prior = getattr(ctx, "domain_prior", None)
    clear_only = getattr(ctx, "clear_only", frozenset())
    bearing_channel_tag = incident.channel_tag
    bearing_target_value = incident.channel_target
    terminal_letrun_role_tags = incident.terminal_role_tags
    replay_watch_roles = incident.watch_roles
    departure_bearing = incident.departure_bearing
    regression_witness = incident.regression_witness
    earned_work = incident.earned_work
    progress_anchor = incident.progress_anchor
    regression_progress_floor = incident.regression_progress_floor
    replay_cache: dict[tuple[bool, tuple[tuple[str, Any], ...]], ReplayOutcome] = {}

    def _replay(
        holds: tuple[Any, ...],
        *,
        prove_continuation: bool = False,
    ) -> ReplayOutcome:
        from pyrung.core.analysis.pilot.coast import CoastSession

        replay_key = (
            prove_continuation,
            tuple(_proposal_identity(hold) for hold in holds),
        )
        cached = replay_cache.get(replay_key)
        if cached is not None:
            return cached

        def _remember(outcome: ReplayOutcome) -> ReplayOutcome:
            replay_cache[replay_key] = outcome
            return outcome

        probe = fork_with_pilot_rungs(cp_fork, pilot_rungs)
        probe_pilot_rungs = list(pilot_rungs)
        scope = _target_unresolved_condition(probe, target_tag, target_value)
        probe_pilot_rungs.extend(_pilot_rungs_from_proposals(list(holds), scope))
        _set_pilot_rungs(probe, probe_pilot_rungs)
        # One session spans the whole replay. The channel pen proves whether the
        # incident's source context was preserved; exact rung-firing timelines
        # independently prove whether its recorded causal branch replayed.
        # ReplayStep.scans is the recorded incident's logical window.  Active
        # hypothesis holds must not reinterpret it as a fresh kernel-work
        # allowance and coast beyond the evidence being replayed.
        session = CoastSession(probe, kind="replay", kernel_budget=False)
        # The last coast step IS the incident's eject coast; its receipt is the
        # trigger-local verdict ("did the recorded departure reproduce?") the
        # judgment below reads alongside the endpoint snapshot.
        eject_receipt: Any = None
        if bearing_channel_tag is not None:
            session.arm_pens((bearing_channel_tag,))
        for step in steps:
            # Each step re-arms its RECORDED session spec (``ReplayStep.kind``
            # off the committed step context) — a letrun eject-coast is coasted,
            # never pulsed (pulsing it would skip the coast entirely: five
            # settle scans, channel intact, every hypothesis "confirms").
            if step.kind == "pulse" and step.inputs:
                _apply_pulse(probe, list(step.inputs), resting, edge_tags, session=session)
            elif step.kind == "letrun":
                # The replay reproduces the INCIDENT — "the channel departed" —
                # so its watch roles (*replay_watch_roles*, an explicit caller
                # parameter) are the channel alone, never the live coast's full
                # role set: the checkpoint world catches the state machine's
                # scratch registers (isCmdValid__cmd, sm__where2jump)
                # mid-settlement, and a role guard would pause on that
                # transient with the channel still at its held value.  The
                # budget is the step's own recorded span — the replay seeks to
                # first-of {target, eject, timeout} and the judgment below
                # reads which fired, so no departure margin is needed.
                eject_receipt = _coast_holding_state(
                    probe,
                    target_tag,
                    target_value,
                    replay_watch_roles,
                    budget=max(1, step.scans),
                    session=session,
                )
            elif step.kind == "bearing_coast" and step.channel_tag is not None:
                # Reproduce the recorded incident, not the rest of its route.
                # A correction only has to neutralize the causal regression
                # inside this bounded step; extending replay to the bearing's
                # distant destination lets a later unrelated fault reuse the
                # same state executor and falsely refute the correction.
                eject_receipt = _coast_to_value(
                    probe,
                    step.channel_tag,
                    step.channel_target,
                    budget=(
                        max(1, step.scans) if regression_witness is not None else _COAST_BUDGET
                    ),
                    session=session,
                )
            else:
                session.dwell(max(1, step.scans))
        incident_replay_end = probe.state.scan_id
        snap = dict(probe.state.tags)
        proposal_tags = {_proposal_pair(hold)[0] for hold in holds}
        replacement_incident: DeviationIncident | None = None
        replacement_witness: RegressionWitness | None = None
        if regression_witness is not None:
            replacement_scan = _replacement_departure_scan(
                regression_witness,
                session.events,
            )
            if replacement_scan is not None:
                replacement_incident = build_deviation_incident(
                    anchor_scan=cp_fork.state.scan_id,
                    end_scan=incident_replay_end,
                    action=(),
                    bearing=((regression_witness.channel_tag, regression_witness.source),),
                    before_snap=dict(cp_fork.state.tags),
                    after_snap=snap,
                    timeline=session.events,
                    channel_tag=regression_witness.channel_tag,
                )
                replacement_witness = incident_regression_witness(
                    probe,
                    replacement_incident,
                )
        ownership = (
            _regression_ownership(
                probe,
                regression_witness,
                session.events,
                proposal_tags,
                start_scan=cp_fork.state.scan_id,
                end_scan=incident_replay_end,
                replacement_witness=replacement_witness,
            )
            if regression_witness is not None
            else None
        )
        progress_erased = (
            ownership is not None
            and ownership.neutralized
            and earned_work is not None
            and regression_progress_floor is not None
            and earned_work.receipt(
                regression_progress_floor,
                snap,
            ).movement
            is EarnedWorkMovement.BACKWARD
        )
        # A correction owns the recorded operation, not merely its outer
        # channel. Keeping Execute while erasing the Step/phase receipt that
        # identified this incident is suppression by rollback, not
        # neutralization. Check the floor at the bounded incident horizon.
        neutralized = ownership is not None and ownership.neutralized and not progress_erased
        source_preserved = ownership is not None and ownership.source_preserved
        continuation_snapshot = snap
        continuation: FrontierStatus = Unknown(
            "bounded replay did not witness the target",
            ((target_tag, target_value),),
        )
        if target_reached(
            snap,
            target_tag,
            target_value,
            ctx.target.predicate,
        ):
            continuation = Reachable(("actual-target-witness",))
        elif neutralized and prove_continuation:
            # Relational corrections need a concrete counterexample beyond the
            # incident horizon: the safe boundary may be several refinements
            # away. This second stage is requested only for that family. Exact
            # latch/absence repairs stop at the bounded incident and return to
            # the ordinary outer loop instead of replaying the whole route for
            # every candidate.
            continuation_receipt = _coast_holding_state(
                probe,
                target_tag,
                target_value,
                ((bearing_channel_tag,) if bearing_channel_tag is not None else ()),
                budget=min(
                    _COAST_BUDGET,
                    max(1, int(getattr(ctx, "max_scans", _COAST_BUDGET))),
                ),
                reached_fn=(
                    (
                        lambda state: target_reached(
                            state.tags,
                            target_tag,
                            target_value,
                            ctx.target.predicate,
                        )
                    )
                    if ctx.target.predicate is not None
                    else None
                ),
                session=session,
            )
            continuation_snapshot = dict(probe.state.tags)
            if target_reached(
                continuation_snapshot,
                target_tag,
                target_value,
                ctx.target.predicate,
            ):
                continuation = Reachable(("actual-target-witness", "coast"))
            else:
                continuation = Unknown(
                    "coast-only continuation did not reach the target"
                    f" ({continuation_receipt.stop_reason})",
                    ((target_tag, target_value),),
                )
        if logger.isEnabledFor(logging.DEBUG):
            roles = terminal_letrun_role_tags or ()
            logger.debug(
                "replay probe: cp_scan=%s end_scan=%s steps=%d shape=%s channel=%s=%r roles=%s",
                cp_fork.state.scan_id,
                probe.state.scan_id,
                len(steps),
                ("letrun" if terminal_letrun_role_tags is not None else "bearing_coast"),
                bearing_channel_tag,
                snap.get(bearing_channel_tag) if bearing_channel_tag else None,
                {t: snap.get(t) for t in roles},
            )

        # Channel incident (channel coast OR terminal let-run hold): the hold is
        # good iff it reaches the requested bearing destination, advances the
        # target-relative earned work, or suppresses the incident's exact departure
        # causal branch within the incident window. A terminal let-run's
        # "target" is the state it was trying to hold, so equality alone is not
        # success there: a direct channel override could mask a still-firing
        # cause. Exact firing testimony distinguishes suppression from masking.
        if bearing_channel_tag is not None:
            reached = terminal_letrun_role_tags is None and _values_match(
                snap.get(bearing_channel_tag), bearing_target_value
            )
            neutralized_reason = None
            if neutralized and regression_witness is not None:
                neutralized_reason = (
                    "recorded regression neutralized: "
                    f"preserved {bearing_channel_tag}={regression_witness.source!r} "
                    f"and suppressed its {len(regression_witness.cause)}-write causal branch"
                    if source_preserved
                    else "recorded cause silenced before an unrelated replacement departure"
                )
            progressed = neutralized_reason
            earned_work_advanced = False
            if (
                not reached
                and progressed is None
                and earned_work is not None
                and progress_anchor is not None
                and earned_work.receipt(progress_anchor, snap).movement
                is EarnedWorkMovement.FORWARD
            ):
                earned_work_advanced = True
                progressed = "target-relative progress advanced"
            # Ownership already distinguishes masking from a genuine branch
            # replacement. A healthy replacement may share generic executor
            # writes with the recorded fault, so asking again whether every
            # changed write disappeared would reject the observed safe landing.
            cause_repeated = regression_witness is not None and not neutralized
            rejection_reason = (
                "correction erased the recorded incident's progress receipt"
                if progress_erased
                else (
                    "recorded regression cause replayed; correction masked its result"
                    if cause_repeated
                    else (
                        f"{bearing_channel_tag} -> {bearing_target_value!r} reached={reached}"
                        + (
                            f" (eject coast: {eject_receipt.stop_reason})"
                            if eject_receipt is not None
                            else ""
                        )
                    )
                )
            )
            accepted = (
                earned_work_advanced or (not cause_repeated and (reached or progressed is not None))
            ) and not progress_erased
            return _remember(
                ReplayOutcome(
                    accepted=accepted,
                    trend=None,
                    snapshot=snap,
                    reason=(progressed if accepted else rejection_reason) or rejection_reason,
                    continuation=continuation,
                    continuation_snapshot=continuation_snapshot,
                    # A coast that timed out mid-journey landed nowhere — its end
                    # snapshot must not seed a channel scope.
                    landed=(
                        reached
                        or (
                            neutralized
                            and not source_preserved
                            and replacement_witness is not None
                            and regression_witness is not None
                            and not _values_match(
                                replacement_witness.landing,
                                regression_witness.landing,
                            )
                        )
                    ),
                    justification=(
                        (
                            ReplayJustification.REACHED
                            if isinstance(continuation, Reachable)
                            else (
                                ReplayJustification.NEUTRALIZED
                                if neutralized_reason is not None
                                else ReplayJustification.ADVANCED
                                if progressed is not None
                                else None
                            )
                        )
                        if accepted
                        else None
                    ),
                    replacement_cause=(
                        ownership.replacement_cause or frozenset()
                        if ownership is not None
                        else frozenset()
                    ),
                    replacement=(
                        ReplacementEvidence(
                            plc=probe,
                            incident=replacement_incident,
                            witness=replacement_witness,
                            shared_suffix=ownership.shared_suffix,
                        )
                        if ownership is not None
                        and replacement_incident is not None
                        and replacement_witness is not None
                        else None
                    ),
                )
            )

        # Terminal let-run without a channel register (no recognized state
        # machine): judge the global target at the bounded point.
        if terminal_letrun_role_tags is not None:
            reached = _values_match(snap.get(target_tag), target_value)
            return _remember(
                ReplayOutcome(
                    accepted=reached,
                    trend=None,
                    snapshot=snap,
                    reason=f"{target_tag} -> {target_value!r} reached={reached}",
                    justification=ReplayJustification.REACHED if reached else None,
                    continuation=(
                        Reachable(("actual-target-witness",))
                        if reached
                        else Unknown(
                            "bounded terminal replay did not reach the target",
                            ((target_tag, target_value),),
                        )
                    ),
                    continuation_snapshot=snap,
                )
            )

        # Command incident: no register to coast toward — judge the bounded
        # bearing-held directly.
        if departure_bearing:
            held = all(_values_match(snap.get(t), v) for t, v in departure_bearing)
            return _remember(
                ReplayOutcome(
                    accepted=held,
                    trend=None,
                    snapshot=snap,
                    reason=f"bearing {'held' if held else 'departed'} at bounded replay",
                    justification=ReplayJustification.BEARING_HELD if held else None,
                    continuation=continuation,
                    continuation_snapshot=continuation_snapshot,
                )
            )

        tree = trace_back(
            target_tag,
            target_value,
            snap,
            pdg,
            program,
            steerable,
            clear_only=clear_only,
            opaque_loop=opaque_loop,
            pipeline_internal_tags=pipeline_internal_tags,
            route=route,
            prior=prior,
        )
        trend = tree.unsatisfied_count()
        return _remember(
            ReplayOutcome(
                accepted=trend <= cp_trend,
                trend=trend,
                snapshot=snap,
                reason=f"trend {trend} <= checkpoint {cp_trend}",
                justification=ReplayJustification.ADVANCED if trend < cp_trend else None,
                continuation=continuation,
                continuation_snapshot=continuation_snapshot,
            )
        )

    _replay.with_continuation = lambda holds: _replay(  # ty: ignore[unresolved-attribute]
        holds, prove_continuation=True
    )
    return _replay


# ---------------------------------------------------------------------------
# Excursion investigation — verify detected a revert, investigate diagnoses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExcursionResult:
    """Replay-confirmed correction from an excursion investigation."""

    reverted: list[str]
    correction: _ConfirmedCorrection | None = None
    replay_fork: Any = None
    # The replayed pulse's recorded session events — the timeline the replayed
    # trial carries forward (its Done-bit pen marks must stay visible to a
    # later incident window).
    replay_timeline: tuple[Any, ...] = ()


def investigate_excursion(
    work: PLC,
    fork: PLC,
    pre_snap: dict[str, Any],
    post_pulse_snap: dict[str, Any],
    pre_key: tuple[Any, ...],
    applied_actions: Sequence[ActionPair],
    *,
    cfg: Any,
    steerable: frozenset[str],
    pilot_rungs: Sequence[Any],
    resting: dict[str, Any],
    edge_tags: set[str],
    scan_budget: int,
    pdg: Any = None,
    program: Any = None,
    ctx: Any = None,
) -> ExcursionResult:
    """Diagnose an excursion and replay-validate candidate holds.

    Verify detected that the state key changed during the pulse but reverted
    after settling; the drive loop invokes this function to find *why* and
    validate one replay. ``applied_actions`` is the complete physical artifact:
    every member is replayed and excluded from corrective nominations.

    Primary path: suppress the *antagonist* — any writer of a reverted register
    that is **causally implicated** in the deviation (``cause()`` attributes the
    tag's change to it) and that **provably drives the tag away** from the value
    the pulse established (``_can_produce`` False).  Dispatch is by causal
    implication + producibility, never by instruction class name: a plain
    clobbering ``copy`` is suppressed exactly like a ``reset``.  Each implicated
    writer's guard is forced FALSE by the inverted-polarity forcing enumeration
    (``break_guard_holds``); when that punts on a genuinely-live word guard, the
    skiff runs bounded isolated probes for a suppressing lever (nominations only).

    Fallback: cause-chain walk and ``cause()`` enablers resolve seal-in
    establishment cases, where the writer *can* still produce the desired value
    and therefore is not a suppression antagonist.

    The successful result carries the exact guarded pilot rungs used by replay.
    The caller may admit and install that correction, but must not reconstruct
    its lifetime from the bare input values.
    """
    from pyrung.core.analysis.pdg import resolve_rung

    reverted: list[str] = []
    for i, name in enumerate(cfg.stateful_names):
        if i in cfg.acc_indices:
            continue
        if not _values_match(pre_snap.get(name), post_pulse_snap.get(name)):
            reverted.append(name)

    candidate_holds: list[ActionPair] = []
    seen: set[ActionPair] = set()

    # Antagonist suppression path: for each reverted register, suppress any writer
    # that is causally implicated in the deviation and provably clobbers the value
    # the pulse established.  Guard-force enumeration first; skiff on a live-word
    # punt.  Every hold is confirmed by the replay gate below — nothing unverified.
    if pdg is not None and program is not None:
        settled_snap = dict(fork.state.tags)
        mini_ctx = SimpleNamespace(
            pdg=pdg,
            program=program,
            steerable=steerable,
            opaque_loop=frozenset(),
            pipeline_internal_tags=frozenset(),
            route=None,
            domain_prior=None,
            nd_domains=None,
        )
        for tag in reverted:
            desired = post_pulse_snap.get(tag)
            for ni in _implicated_writers(fork, tag, pdg):
                node = pdg.rung_nodes[ni]
                ro = resolve_rung(program, node)
                if ro is None:
                    continue
                # Honesty boundary (mirrors trace's ``_preserve_children``): only
                # suppress a writer that *provably* drives the tag off the desired
                # value.  A writer that could still produce it (the seal-in OTE)
                # is an establishment case for the fallback, not a clobberer.
                if _can_produce(_written_value_for_tag(ro, tag), desired):
                    continue
                holds = break_guard_holds(ro, settled_snap, mini_ctx)
                if holds is None:
                    # Live-word guard: enumeration punted -> isolated skiff probes.
                    holds = _skiff_suppression_nominations(
                        work,
                        tag,
                        desired,
                        node,
                        applied_actions,
                        pdg,
                        steerable,
                        pilot_rungs,
                    )
                for hold in holds or ():
                    if hold not in seen:
                        seen.add(hold)
                        candidate_holds.append(hold)

    # Fallback: cause-chain walk.
    if not candidate_holds:
        for tag in reverted:
            _, holds = chase_cause_roots(fork, tag, steerable)
            for h in holds:
                if h not in seen:
                    seen.add(h)
                    candidate_holds.append(h)

            try:
                chain = fork.cause(tag, deep=False)
            except Exception:  # noqa: BLE001
                continue
            if chain is None:
                continue
            for step in chain.steps:
                for enabler in step.enablers:
                    if enabler.tag_name not in steerable:
                        continue
                    if not isinstance(enabler.value, bool):
                        continue
                    hold = (enabler.tag_name, not enabler.value)
                    if hold not in seen:
                        seen.add(hold)
                        candidate_holds.append(hold)

    action_tags = {t for t, _ in applied_actions}
    candidate_holds = [(t, v) for t, v in candidate_holds if t not in action_tags]
    if ctx is not None:
        candidate_holds = [hold for hold in candidate_holds if _hold_allowed(ctx, hold)]
    if not candidate_holds:
        return ExcursionResult(reverted=reverted)

    replay_fork = fork_with_pilot_rungs(work, pilot_rungs)
    replay_pilot_rungs = list(pilot_rungs)
    from pyrung.core.analysis.pilot.coast import CoastSession
    from pyrung.core.condition import CompareEq

    preserved_tag = reverted[0]
    preserved = replay_fork._known_tags_by_name[preserved_tag]
    scope = CompareEq(preserved, post_pulse_snap[preserved_tag])
    confirmed_pilot_rungs = tuple(_pilot_rungs_from_proposals(candidate_holds, scope))
    replay_pilot_rungs.extend(confirmed_pilot_rungs)
    _set_pilot_rungs(replay_fork, replay_pilot_rungs)
    kickoff = list(applied_actions)
    kickoff.extend((t, v) for t, v in candidate_holds if t not in {a for a, _ in applied_actions})
    session = CoastSession(replay_fork, kind="excursion-replay")
    if program is not None:
        session.arm_pens(
            owner.profile.done.name
            for owner in iter_advance_owners(program)
            if owner.profile.done is not None
        )
    _apply_pulse(replay_fork, kickoff, resting, edge_tags, session=session)
    _settle_delayed_effects(replay_fork, scan_budget=scan_budget, session=session)
    replay_snap = dict(replay_fork.state.tags)
    replay_key = _pilot_state_key(replay_snap, cfg)

    if replay_key != pre_key:
        return ExcursionResult(
            reverted=reverted,
            correction=_ConfirmedCorrection(
                identity=correction_identity(confirmed_pilot_rungs),
                pilot_rungs=confirmed_pilot_rungs,
                sources=tuple(dict.fromkeys((*reverted, *(tag for tag, _ in candidate_holds)))),
                justification="excursion replay preserved the pulse-established state",
            ),
            replay_fork=replay_fork,
            replay_timeline=session.events,
        )
    return ExcursionResult(reverted=reverted)


def _implicated_writers(plc: PLC, tag: str, pdg: Any) -> list[int]:
    """PDG writer-node indices of *tag* causally implicated in its deviation.

    Dispatch by causal implication, never by instruction class: ``cause()``
    attributes the reverted tag's change to the rung(s) that actually wrote it in
    the settled window; those are the antagonists worth suppressing.  A writer
    that never fired is not in the chain and is left alone.  Maps the chain's
    ``(rung_index, subroutine)`` back to the PDG writer nodes.  ``[]`` when
    ``cause()`` is unavailable, allowing the cause-chain fallback to run.
    """
    try:
        chain = plc.cause(tag, deep=False)
    except Exception:  # noqa: BLE001
        chain = None
    if chain is None:
        return []
    implicated = {
        (step.rung_index, step.subroutine)
        for step in chain.steps
        if step.transition.tag_name == tag
    }
    if not implicated:
        return []
    out: list[int] = []
    for ni in pdg.writers_of.get(tag, frozenset()):
        node = pdg.rung_nodes[ni]
        if (node.rung_index, node.subroutine) in implicated:
            out.append(ni)
    return out


def _skiff_suppression_nominations(
    work: PLC,
    tag: str,
    desired: Any,
    node: Any,
    applied_actions: Sequence[ActionPair],
    pdg: Any,
    steerable: frozenset[str],
    pilot_rungs: Sequence[PilotRung],
) -> list[ActionPair]:
    """Compatibility facade for bounded pinned suppression nominations."""

    return _refinement._skiff_suppression_nominations(
        work,
        tag,
        desired,
        node,
        applied_actions,
        pdg,
        steerable,
        pilot_rungs,
        run_pinned=run_pinned_scan,
    )


# ---------------------------------------------------------------------------
# Incident construction
# ---------------------------------------------------------------------------


def _first_timeline_departure(
    timeline: Sequence[Any],
    tag: str,
    value: Any,
) -> int | None:
    """The recorded scan of *tag*'s first transition off *value*, or ``None``.

    Read straight off the session timeline — the pen mark IS the departure
    scan; no history window is re-scanned.
    """
    for event in timeline:
        for t, before, after in event.transitions:
            if t == tag and _values_match(before, value) and not _values_match(after, value):
                return event.scan
    return None


def build_deviation_incident(
    *,
    anchor_scan: int,
    end_scan: int,
    action: tuple[ActionPair, ...],
    bearing: tuple[ActionPair, ...],
    before_snap: Mapping[str, Any],
    after_snap: Mapping[str, Any],
    timeline: Sequence[Any] = (),
    channel_tag: str | None = None,
) -> DeviationIncident:
    """Capture the facts inside the known off-course window.

    *timeline* is the recorded session evidence for the window (the committed
    steps' pen marks and trigger landings): ``changed_tags`` membership and every
    departure scan are read off it, never re-derived from history.  A
    fire-then-reset watchdog pulse is two recorded transitions. That exact
    evidence identifies which accumulator owner completed; correction then asks
    that owner for its reset operation.

    ``changed_tags`` is factual incident evidence: every recorded transition
    plus every endpoint difference.  Consumers such as the timer correction
    engine select their own relevant profile tags from this complete set;
    incident construction never discards evidence on a consumer's behalf.

    """
    changed: set[str] = {t for event in timeline for t, _b, _a in event.transitions}
    changed.update(
        t
        for t in set(before_snap) | set(after_snap)
        if not _values_match(before_snap.get(t), after_snap.get(t))
    )
    departures = tuple(
        BearingDeparture(tag, value, _first_timeline_departure(timeline, tag, value))
        for tag, value in bearing
        if not _values_match(after_snap.get(tag), value)
    )
    departure_scans = [d.scan for d in departures if d.scan is not None]
    return DeviationIncident(
        anchor_scan=anchor_scan,
        departure_scan=min(departure_scans) if departure_scans else None,
        end_scan=end_scan,
        action=action,
        bearing=bearing,
        before_snap=before_snap,
        after_snap=after_snap,
        changed_tags=tuple(sorted(changed)),
        departures=departures,
        channel_tag=channel_tag,
        timeline=tuple(timeline),
    )


# ---------------------------------------------------------------------------
# Investigation engine
# ---------------------------------------------------------------------------


def _hold_is_noop(
    tag: str,
    value: Any,
    snap: Mapping[str, Any],
    pdg: Any,
    program: Any,
    incident_movers: frozenset[str] = frozenset(),
    after_snap: Mapping[str, Any] | None = None,
    synthesis_rungs: Sequence[PilotRung] = (),
) -> bool:
    """A hold that changes nothing cannot be a correction.

    Pinning *tag* at a value it already holds is inert when no program writer
    can move it off that value (every writer stamps a literal matching it —
    the clear-only idiom: holding ``Heat_xPause`` at its rest 0 counters
    nothing, because the program only ever writes 0).  A FREEZE survives this
    test: it either drives the tag OFF its current value or pins against a
    writer that can produce a different one.  Oscillating (``PilotRung``)
    values are never no-ops.

    A tag recorded as moving during this incident, whose endpoint differs from
    its anchor, or which is written by the installed synthesis overlay is not a
    no-op even when the proposed correction equals its anchor value.  The
    overlay is executable writer evidence outside the program PDG; replay, not
    this cheap prefilter, decides whether a different scoped rule is useful.
    """
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.analysis.steerable import _literal_write

    if tag in incident_movers:
        return False
    if after_snap is not None and not _values_match(after_snap.get(tag), value):
        return False
    if getattr(value, "rules", None) is not None:
        return False
    if any(rung.dest == tag for rung in synthesis_rungs):
        return False
    if not _values_match(snap.get(tag), value):
        return False
    for ri in pdg.writers_of.get(tag, frozenset()):
        ro = resolve_rung(program, pdg.rung_nodes[ri])
        if ro is None:
            return False  # unreadable writer — assume it could move the tag
        lw = _literal_write(ro, tag)
        if lw is None or not _values_match(lw, value):
            return False  # a write that can move the tag — the hold pins something
    return True


def _rank_hypotheses(
    plc: PLC,
    hypotheses: Sequence[CorrectionHypothesis],
    incident: DeviationIncident,
    primal_extra: frozenset[str] = frozenset(),
) -> list[CorrectionHypothesis]:
    """Order competing hypotheses by **causal primacy**, not generation order.

    The channel departure (``incident.channel_tag`` — the ejection itself)
    is the incident; other departures are collateral downstream of it (the
    state-8 shared-init resetting ``Heat_CurStep``).  Two primacy signals,
    strongest first:

    * **chain membership** — the hypothesis's tags sit inside the cause chain
      of the channel departure.  ``chase_chain_tags`` follows the native deep
      cause chain across held pipeline enablers, so on a PackML-shaped program
      the chain reaches the starved watchdog directly.
    * **temporal precedence** — how close the hypothesis's most recent source
      transition sits to the channel departure scan.  Pure scan-log
      observation, no inversion: the ejecting watchdog's Done rises *at* the
      ejection; a bystander (``Test_Simulate_1st_Scan``'s alarm timer) fired
      somewhere earlier in a 1000-scan coast, and a collateral symptom
      (``Heat_CurStep`` at 1810 vs the ejection at 1855) trails by the same
      measure.

    Ties break by lightest intervention, then generation order.
    """
    chan = incident.channel_tag
    dep_scan = {d.tag: d.scan for d in incident.departures if d.scan is not None}
    primal: set[str] = set()
    chan_scan = incident.end_scan
    if chan is not None:
        if dep_scan.get(chan) is not None:
            chan_scan = dep_scan[chan]
        # All tags on the chain, not just steerable roots: an absence-caused
        # ejection (a sensor that never moved) has no steerable mover at all.
        # The native deep chain crosses held pipeline enablers and reaches the
        # true root, making causal primacy exact rather than won on temporal
        # proximity.
        primal = {chan} | chase_chain_tags(plc, chan, scan=dep_scan.get(chan))
    # Deep-walk roots of the channel departure (``primal_extra``) are chain
    # members by construction — an absence root has no transition for the
    # proximity signal to see, so without this it would rank dead last behind
    # every temporally-nearby bystander.
    primal |= primal_extra

    big = 1 << 30

    def _proximity(tags: set[str]) -> int:
        best = big
        for t in tags:
            last = _last_transition_scan(plc, t, incident.anchor_scan, chan_scan)
            if last is not None:
                best = min(best, chan_scan - last)
        return best

    def _key(pair: tuple[int, CorrectionHypothesis]) -> tuple[int, int, int, int, int, int]:
        idx, h = pair
        tags = set(h.sources) | {_proposal_pair(p)[0] for p in h.holds}
        in_chain = 0 if (primal and tags & primal) else 1
        incident_specific = 0 if (h.kind == "latch-exposure" or h.incident_local) else 1
        if h.kind == "absence-root":
            # A recorded external terminal is a true causal leaf (the Sail
            # permissive); a never-written default is a broad explanation of
            # the cold world (the factory-reset command). Exact fired-chain
            # evidence belongs between those two strengths.
            family = 0 if h.history_origin == "external" else 2
        elif h.kind == "precise-cause":
            family = 1
        else:
            family = 3
        # Chain membership establishes relevance; it does not erase temporal
        # precision within a hypothesis family. Exact latch evidence gets its
        # own stronger tier, while a true absence root remains the explanation
        # rather than losing to a downstream precise suppressor on the same
        # chain. Within liveness/precise siblings, the incident-nearest owner
        # still beats unrelated first-scan noise.
        proximity = _proximity(tags)
        return (in_chain, incident_specific, family, proximity, len(h.holds), idx)

    return [h for _, h in sorted(enumerate(hypotheses), key=_key)]


def _compose_hypotheses(
    base: CorrectionHypothesis,
    addition: CorrectionHypothesis,
) -> CorrectionHypothesis | None:
    """Create a newly replayable union, declining contradictory operations."""
    # A composed replacement is a new causal closure. Keep the exact forms of
    # any individually widened members; a union of separately complete
    # envelopes is not itself proof that their combined cascade is complete.
    base_holds = base.fallback_holds or base.holds
    addition_holds = addition.fallback_holds or addition.holds
    holds = list(base_holds)
    seen = {_proposal_identity(hold) for hold in holds}
    by_dest = {dest: value for dest, value in map(_proposal_pair, holds)}
    for hold in addition_holds:
        dest, value = _proposal_pair(hold)
        if dest in by_dest and not _values_match(by_dest[dest], value):
            return None
        identity = _proposal_identity(hold)
        if identity not in seen:
            seen.add(identity)
            holds.append(hold)
            by_dest.setdefault(dest, value)
    if len(holds) == len(base_holds):
        return None

    producer_cuts = list(base.producer_cuts)
    seen_cuts = {
        (
            hold[0],
            _semantic_key(hold[1]),
            tag,
            _semantic_key(value),
        )
        for hold, tag, value in producer_cuts
    }
    cut_assignments = {tag: value for _hold, tag, value in producer_cuts}
    cut_conflict = False
    for hold, tag, value in addition.producer_cuts:
        if tag in cut_assignments and not _values_match(cut_assignments[tag], value):
            cut_conflict = True
            break
        key = (hold[0], _semantic_key(hold[1]), tag, _semantic_key(value))
        if key not in seen_cuts:
            seen_cuts.add(key)
            producer_cuts.append((hold, tag, value))
        cut_assignments.setdefault(tag, value)
    if cut_conflict:
        # The exact nested correction may still be valid, but these two pieces
        # cannot support one coordinated structural widening.
        producer_cuts = []

    kind = base.kind if base.kind == addition.kind else "nested-cause"
    return CorrectionHypothesis(
        kind=kind,
        holds=tuple(holds),
        sources=tuple(dict.fromkeys((*base.sources, *addition.sources))),
        detail=f"nested causal closure: {base.detail}; then {addition.detail}",
        constraint=base.constraint or addition.constraint,
        incident_local=base.incident_local and addition.incident_local,
        history_origin=(
            base.history_origin if base.history_origin == addition.history_origin else None
        ),
        producer_envelope=False,
        fallback_holds=(),
        producer_cuts=tuple(producer_cuts),
        producer_sources=tuple(dict.fromkeys((*base.producer_sources, *addition.producer_sources))),
        producer_causal_spine=(base.producer_causal_spine | addition.producer_causal_spine),
    )


def _reprove_composite_producer_envelope(
    hypothesis: CorrectionHypothesis,
    ctx: Any,
    channel_tag: str | None,
) -> CorrectionHypothesis:
    """Promote one exact nested closure only after a fresh coordinated proof."""

    if (
        channel_tag is None
        or not hypothesis.producer_cuts
        or not hypothesis.producer_sources
        or not hypothesis.producer_causal_spine
    ):
        return hypothesis
    exact_holds = hypothesis.holds
    exact_pairs = tuple(_proposal_pair(hold) for hold in exact_holds)
    if not all(
        any(
            owned[0] == hold[0] and _values_match(owned[1], hold[1])
            for owned, _tag, _value in hypothesis.producer_cuts
        )
        for hold in exact_pairs
    ):
        # Mixed causal closures (for example latch + timer operation) retain
        # their exact composite; only a fully-owned cut may be widened.
        return hypothesis
    widened = producer_envelope_correction_holds(
        exact_pairs,
        hypothesis.producer_sources,
        ctx,
        channel_tag,
        hypothesis.producer_cuts,
        hypothesis.producer_causal_spine,
    )
    if not widened:
        return hypothesis
    return replace(
        hypothesis,
        holds=widened,
        producer_envelope=True,
        fallback_holds=exact_holds,
    )


def investigate_deviation(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
    replay: ReplayFn,
    *,
    needed: Sequence[tuple[str, Any]] = (),
    installed_pilot_rungs: Sequence[Any] = (),
    correction_pilot_rungs: Sequence[Any] = (),
    correction_progress_mark: tuple[tuple[str, Any], ...] = (),
    occurrence_requirements: tuple[tuple[str, Any], ...] = (),
    excluded_corrections: frozenset[CorrectionIdentity] = frozenset(),
    regression_witness: RegressionWitness | None = None,
) -> InvestigationResult:
    """Investigate an incident with precise hypothesis generation.

    Three hypothesis families, all instrument-derived:

    1. Absence roots — a deep cause walk finds required signals that never
       transitioned inside the incident.
    2. Precise fired-chain cuts — recorded writer/cause chains identify a
       steerable trigger or a pinned causal cut.
    3. Enabler correction — the producer asks a harmful writer for a
       guard-breaking assignment or an owner-declared accumulator operation.

    No upstream cone sweep.

    Hypotheses are ranked by causal primacy. A counterfactual replacement with
    the same bounded channel outcome and exact participating pipeline composes
    another cut into the current candidate; the composite is then replayed
    from the original checkpoint. This bounded inner loop is an orientation
    optimization: it returns one candidate to the ordinary orchestration loop
    and never commits a world or installs a correction itself. Hypotheses whose
    holds are *already installed*
    (*installed*) are skipped, not re-confirmed: they were active when the
    incident happened, so a repeat regression at the same key escalates to the
    runner-up instead of re-anointing the incumbent.
    """
    # Absence roots generate FIRST: rank ties inside the causal chain break by
    # generation order, and when a never-moved terminal (the stuck permissive)
    # and a mid-chain suppressor (the abort rung's ~Suspend enabler) both
    # survive the bounded replay, the terminal names the cause while the
    # suppressor merely mutes the response.
    installed_pilot_rungs = tuple(installed_pilot_rungs)
    # Ask the overlay compiler which installed rule actually owned each
    # destination at the incident anchor.  Observing the full overlay before
    # filtering correction provenance preserves start/continuation precedence
    # and prevents an eligible, shadowed, or dormant sibling from claiming the
    # write. Persistent rules are not removed merely because they are inactive.
    overlay = _pilot_rung_execution_receipt(installed_pilot_rungs, dict(incident.before_snap))
    installed_active = {rung.dest: rung.value for rung in overlay.effective}
    correction_ids = {_rung_identity(rung) for rung in correction_pilot_rungs}
    correction_active = {
        rung.dest: rung.value
        for rung in overlay.effective
        if _rung_identity(rung) in correction_ids
    }
    causal_spine = (
        regression_witness.causal_spine if regression_witness is not None else frozenset()
    )

    def _initial_hypotheses() -> Iterator[CorrectionHypothesis]:
        """Try exact live-incident evidence before expanding older ancestry.

        This is deliberately lazy. If an incident-local latch correction
        replays successfully, the generator is never resumed and no global
        deep walk is issued. If it fails, the ordinary unbounded causal
        families remain available and may follow a specific support through
        any parent epoch.
        """
        local, _ = derive_correction_hypotheses(
            plc,
            incident,
            ctx,
            installed=correction_active,
            incident_local_only=True,
            causal_spine=causal_spine,
        )
        local = tuple(_rank_hypotheses(plc, local, incident))
        local_ids = {_hypothesis_identity(item.holds) for item in local}
        yield from local

        # A moved trigger frontier belongs to the exact departure occurrence,
        # just as a latch exposure does.  Try it before asking which inputs
        # were cold across the accumulated history.  If it confirms, this
        # generator is never resumed and the broad held-since walk is skipped;
        # if it fails, older epochs remain available below.
        transition_local, _ = derive_correction_hypotheses(
            plc,
            incident,
            ctx,
            installed=correction_active,
            incident_transition_only=True,
            causal_spine=causal_spine,
        )
        transition_local = tuple(
            item
            for item in _rank_hypotheses(plc, transition_local, incident)
            if _hypothesis_identity(item.holds) not in local_ids
        )
        local_ids.update(_hypothesis_identity(item.holds) for item in transition_local)
        yield from transition_local

        produced, absence_tags = derive_correction_hypotheses(
            plc,
            incident,
            ctx,
            installed=correction_active,
            causal_spine=causal_spine,
        )
        for item in _rank_hypotheses(
            plc,
            produced,
            incident,
            primal_extra=absence_tags,
        ):
            if _hypothesis_identity(item.holds) not in local_ids:
                yield item

    hypotheses = _initial_hypotheses()
    observed_hypotheses: list[CorrectionHypothesis] = []
    confirmed: list[CorrectionHypothesis] = []
    confirmed_correction: _ConfirmedCorrection | None = None
    rejected: list[InvestigationRejection] = []
    pdg = getattr(ctx, "pdg", None)
    program = getattr(ctx, "program", None)
    # A proposed hold at the anchor value is meaningful when the complete
    # incident record says that tag moved away (including an installed guard
    # expiring).  Correction engines filter this factual set locally.
    recorded_incident_movers = frozenset(incident.changed_tags)

    def _reject(hyp: CorrectionHypothesis, slug: str, detail: str) -> None:
        rejected.append(InvestigationRejection(hyp, slug, detail))

    def _compose_replacement_candidate(
        current: CorrectionHypothesis,
        evidence: ReplacementEvidence,
    ) -> CorrectionHypothesis | None:
        """Derive the next cut from the retained fork and add it to *current*."""
        nested_raw, nested_absence = derive_correction_hypotheses(
            evidence.plc,
            evidence.incident,
            ctx,
            causal_spine=evidence.witness.causal_spine,
        )
        nested = _rank_hypotheses(
            evidence.plc,
            nested_raw,
            evidence.incident,
            primal_extra=nested_absence,
        )
        for candidate in nested:
            identity = _hypothesis_identity(candidate.holds)
            equivalent = next(
                (
                    known
                    for known in observed_hypotheses
                    if _hypothesis_identity(known.holds) == identity
                ),
                None,
            )
            chosen = equivalent or candidate
            if equivalent is None:
                observed_hypotheses.append(candidate)
            composite = _compose_hypotheses(current, chosen)
            if composite is not None:
                return _reprove_composite_producer_envelope(
                    composite,
                    ctx,
                    incident.channel_tag,
                )
        return None

    def _replay_candidate(
        hypothesis: CorrectionHypothesis,
        holds: tuple[Any, ...],
    ) -> ReplayOutcome:
        """Use the staged continuation probe only when refinement consumes it."""
        with_continuation = getattr(replay, "with_continuation", None)
        if hypothesis.constraint is not None and with_continuation is not None:
            return with_continuation(holds)
        return replay(holds)

    for hypothesis in hypotheses:
        if not any(
            _hypothesis_identity(known.holds) == _hypothesis_identity(hypothesis.holds)
            for known in observed_hypotheses
        ):
            observed_hypotheses.append(hypothesis)
        if not hypothesis.holds:
            _reject(hypothesis, "no-holds", "no holds proposed")
            continue
        # A hypothesis made entirely of PilotRungs already owns its executable
        # scope.  Raw pairs acquire one only after their exploratory replay.
        if all(isinstance(hold, PilotRung) for hold in hypothesis.holds) and (
            correction_identity(hypothesis.holds) in excluded_corrections
        ):
            _reject(
                hypothesis,
                "correction-revoked",
                "correction was previously revoked after causing a later regression",
            )
            continue
        if installed_active and all(
            ht in installed_active
            and (installed_active[ht] == hv or _values_match(installed_active[ht], hv))
            for ht, hv in map(_proposal_pair, hypothesis.holds)
        ):
            # Skip only when an installed rung *actively covered* every proposed
            # pair at the incident anchor: it was truly active when the incident
            # happened, so a repeat regression escalates to the runner-up.  A rung
            # installed but guard-expired (a door hold released in Execute) is
            # absent here, so its hypothesis proceeds to replay instead.
            continue
        if (
            pdg is not None
            and program is not None
            and all(
                not any(
                    action_tag == ht and not _values_match(action_value, hv)
                    for action_tag, action_value in incident.action
                )
                and _hold_is_noop(
                    ht,
                    hv,
                    incident.before_snap,
                    pdg,
                    program,
                    recorded_incident_movers,
                    incident.after_snap,
                    installed_pilot_rungs,
                )
                for ht, hv in map(_proposal_pair, hypothesis.holds)
            )
        ):
            # Every hold pins a value already in place that the program cannot
            # move — the "correction" changes nothing, so its replay pass is
            # vacuous and installing it burns the round on a byte-identical
            # re-coast.
            _reject(
                hypothesis,
                "vacuous-hold",
                "vacuous no-op hold: every proposed value is already stable in the incident anchor",
            )
            continue

        def _attempt_composition(
            candidate: _InvestigationCompositionCandidate,
            _attempt_ctx: AttemptContext,
        ):
            """Replay one candidate until it extends, confirms, or rejects.

            Relational refinement and producer-envelope fallback are retries of
            the same correction, so they do not consume causal-composition
            budget.  Only an admitted replacement returns ``Extend``.
            """

            current = candidate.hypothesis
            refinement_receipt = candidate.refinement

            def _fallback_candidate() -> _InvestigationCompositionCandidate | None:
                if not current.producer_envelope or not current.fallback_holds:
                    return None
                return _InvestigationCompositionCandidate(
                    replace(
                        current,
                        holds=current.fallback_holds,
                        producer_envelope=False,
                        fallback_holds=(),
                        detail=f"{current.detail}; producer envelope declined",
                    ),
                    refinement_receipt,
                )

            def _replacement_decision(
                phase: Literal["exploratory", "guarded"],
                outcome: ReplayOutcome,
            ):
                if not outcome.accepted:
                    return Reject(
                        InvestigationRejection(
                            current,
                            f"{phase}-replay-failed",
                            f"{phase} replay rejected: "
                            + (outcome.reason or "no replay reason supplied"),
                        )
                    )
                replacement = outcome.replacement
                if replacement is None or not replacement.shared_suffix:
                    return None
                fingerprint = _replacement_identity(replacement)
                cycle_ground = (
                    "counterfactual replacement cause repeated inside one investigation"
                    if phase == "exploratory"
                    else "guarded replay repeated a counterfactual replacement cause"
                )
                unresolved_prefix = (
                    "replacement" if phase == "exploratory" else "guarded replacement"
                )
                fallback = _fallback_candidate() if phase == "guarded" else None

                def _build():
                    extended = _compose_replacement_candidate(current, replacement)
                    if extended is None:
                        return None
                    return _InvestigationCompositionCandidate(
                        extended,
                        refinement_receipt,
                    )

                return Extend(
                    fingerprint,
                    _build,
                    (
                        Retry(fallback)
                        if fallback is not None
                        else Reject(
                            InvestigationRejection(
                                current,
                                "nested-cause-cycle",
                                cycle_ground,
                            )
                        )
                    ),
                    (
                        Retry(fallback)
                        if fallback is not None
                        else Reject(
                            InvestigationRejection(
                                current,
                                "nested-cause-unresolved",
                                f"{unresolved_prefix} reproduced the same bounded outcome "
                                "and pipeline but yielded no additional corrective cut",
                            )
                        )
                    ),
                )

            while True:
                exploratory_holds = (
                    current.fallback_holds
                    if current.producer_envelope and current.fallback_holds
                    else current.holds
                )
                exploratory = _exploratory_correction_rungs(
                    plc,
                    exploratory_holds,
                    incident,
                    correction_progress_mark,
                    ctx,
                )
                preflight = _continuation_with_active_correction(
                    exploratory,
                    incident.before_snap,
                    ctx,
                )
                if isinstance(preflight, NoRoute):
                    return Reject(InvestigationRejection(current, "target-cut", preflight.proof))
                outcome = _replay_candidate(current, exploratory)
                if current.constraint is not None and not isinstance(
                    outcome.continuation,
                    Reachable,
                ):
                    refined, ground = _refine_unknown_continuation(
                        current,
                        outcome,
                        ctx,
                        refinement_receipt,
                    )
                    if refined is not None:
                        current = refined
                        observed_hypotheses.append(refined)
                        continue
                    return Reject(
                        InvestigationRejection(
                            current,
                            "relational-continuation-unknown",
                            ground,
                        )
                    )
                resolution = _replacement_decision("exploratory", outcome)
                if resolution is not None:
                    return resolution

                # A target-work correction belongs to the exact earned-work occurrence.
                # A correction that directly discharges this producer occurrence's
                # recorded external supports instead belongs to the already-derived
                # channel-source lifetime.  Its final installed form is still
                # replayed below; this only selects between the two existing scopes.
                scoped_progress_mark = (
                    ()
                    if _discharges_occurrence_requirements(
                        current.holds,
                        occurrence_requirements,
                    )
                    else correction_progress_mark
                )
                scoped = _scoped_correction_rungs(
                    plc,
                    current.holds,
                    incident,
                    outcome,
                    ctx,
                    scoped_progress_mark,
                    current.producer_envelope,
                )

                def _fall_back_from_envelope() -> bool:
                    nonlocal current
                    fallback = _fallback_candidate()
                    if fallback is None:
                        return False
                    current = fallback.hypothesis
                    return True

                if correction_identity(scoped) in excluded_corrections:
                    if _fall_back_from_envelope():
                        continue
                    return Reject(
                        InvestigationRejection(
                            current,
                            "correction-revoked",
                            "correction was previously revoked after causing a later regression",
                        )
                    )
                scoped_preflight = _continuation_with_active_correction(
                    scoped,
                    incident.before_snap,
                    ctx,
                )
                if isinstance(scoped_preflight, NoRoute):
                    if _fall_back_from_envelope():
                        continue
                    return Reject(
                        InvestigationRejection(current, "target-cut", scoped_preflight.proof)
                    )
                required_progress = (*incident.bearing, *needed)
                if (
                    pdg is not None
                    and program is not None
                    and _active_pilot_rungs_defeat_needed(
                        scoped,
                        required_progress,
                        incident.before_snap,
                        pdg,
                        program,
                    )
                ):
                    if _fall_back_from_envelope():
                        continue
                    return Reject(
                        InvestigationRejection(
                            current,
                            "self-defeat",
                            "guarded correction defeats requested progress: "
                            f"needed={required_progress!r}, correction={tuple(scoped)!r}",
                        )
                    )
                # Operation-owned proposals and already-exact guards can survive
                # scoping unchanged. Replay is deterministic from the retained
                # incident checkpoint, so an identical executable correction has
                # already proved its installed form in the exploratory pass.
                same_executable = all(
                    isinstance(rung, PilotRung) for rung in exploratory
                ) and correction_identity(scoped) == correction_identity(exploratory)
                installed_outcome = (
                    outcome if same_executable else _replay_candidate(current, scoped)
                )
                if current.constraint is not None and not isinstance(
                    installed_outcome.continuation,
                    Reachable,
                ):
                    refined, ground = _refine_unknown_continuation(
                        current,
                        installed_outcome,
                        ctx,
                        refinement_receipt,
                    )
                    if refined is not None:
                        current = refined
                        observed_hypotheses.append(refined)
                        continue
                    if _fall_back_from_envelope():
                        continue
                    return Reject(
                        InvestigationRejection(
                            current,
                            "relational-continuation-unknown",
                            ground,
                        )
                    )
                resolution = _replacement_decision("guarded", installed_outcome)
                if resolution is not None:
                    if isinstance(resolution, Reject) and _fall_back_from_envelope():
                        continue
                    return resolution

                confirmed_hypothesis = CorrectionHypothesis(
                    kind=current.kind,
                    holds=scoped,
                    sources=current.sources,
                    detail=current.detail,
                    incident_local=current.incident_local,
                    history_origin=current.history_origin,
                    producer_envelope=current.producer_envelope,
                    fallback_holds=current.fallback_holds,
                    producer_cuts=current.producer_cuts,
                    producer_sources=current.producer_sources,
                    producer_causal_spine=current.producer_causal_spine,
                )
                installed_justification = (
                    installed_outcome.justification.value
                    if installed_outcome.justification is not None
                    else installed_outcome.reason or "replay-confirmed"
                )
                confirmed_candidate = _ConfirmedCorrection(
                    identity=correction_identity(scoped),
                    pilot_rungs=scoped,
                    sources=confirmed_hypothesis.sources,
                    justification=(
                        installed_justification
                        if isinstance(installed_outcome.continuation, Reachable)
                        else (
                            "legacy-local-replay; target continuation unknown: "
                            f"{_continuation_ground(installed_outcome.continuation)}; "
                            f"{installed_justification}"
                        )
                    ),
                )
                return Succeed(
                    _InvestigationConfirmation(
                        confirmed_hypothesis,
                        confirmed_candidate,
                    )
                )

        composition = compose_corrections(
            _InvestigationCompositionCandidate(
                hypothesis,
                _RelationalRefinementReceipt(),
            ),
            budget=CompositionBudget(_MAX_CANDIDATE_COMPOSITIONS + 1),
            attempt=_attempt_composition,
            budget_exhausted=lambda candidate: InvestigationRejection(
                candidate.hypothesis,
                "nested-cause-budget",
                "candidate composition exceeded "
                f"{_MAX_CANDIDATE_COMPOSITIONS} replacement branches",
            ),
        )
        if composition.termination is CompositionTermination.SUCCESS:
            confirmation = composition.value
            assert isinstance(confirmation, _InvestigationConfirmation)
            confirmed.append(confirmation.hypothesis)
            confirmed_correction = confirmation.correction
            break  # return one confirmed composite candidate to the outer loop
        rejection = composition.value
        assert isinstance(rejection, InvestigationRejection)
        rejected.append(rejection)

    return InvestigationResult(
        correction=confirmed_correction,
        regression_nogoods=frozenset(),
        hypotheses=tuple(observed_hypotheses),
        confirmed=tuple(confirmed),
        rejected=tuple(rejected),
        unresolved=incident.changed_tags if not confirmed else (),
    )


# ---------------------------------------------------------------------------
# Investigation-local correction checks
# ---------------------------------------------------------------------------


def _active_pilot_rungs_defeat_needed(
    pilot_rungs: Sequence[PilotRung],
    needed: Sequence[tuple[str, Any]],
    snapshot: Mapping[str, Any],
    pdg: Any,
    program: Any,
) -> bool:
    """Whether the guarded correction provably pins a checkpoint need.

    The compiler-owned receipt evaluates the exact pre-incident world because
    synthesized PilotRung branches read one frozen rung-entry snapshot. Only
    effective owners are checked as one assignment, so a coordinated correction
    that forces an ``And``-gated reset is caught even when no member defeats
    progress alone; dormant and shadowed siblings cannot manufacture a pin.
    """
    overlay = _pilot_rung_execution_receipt(pilot_rungs, snapshot)
    active = [(rung.dest, rung.value) for rung in overlay.effective]
    return _holds_defeat_needed(active, needed, pdg, program)


def _continuation_with_active_correction(
    pilot_rungs: Sequence[Any],
    snapshot: Mapping[str, Any],
    ctx: Any,
) -> FrontierStatus:
    """Classify static target continuation under one executable correction.

    This is the negative-write counterpart of
    :func:`_active_pilot_rungs_defeat_needed`: a pin can defeat progress by
    making every producer guard false, even though no conflicting write fires.
    Only an explicit action contradiction on every enumerated route proves the
    cut. A viable static trace is still ``Unknown`` because only execution can
    witness the target; an opaque or incomplete trace stays ``Unknown`` too.
    """

    if not all(isinstance(rung, PilotRung) for rung in pilot_rungs):
        return Unknown("correction has no executable scope for continuation analysis")
    if getattr(ctx.target, "predicate", None) is not None:
        return Unknown("predicate target continuation requires execution")
    overlay = _pilot_rung_execution_receipt(pilot_rungs, snapshot)
    active = {rung.dest: rung.value for rung in overlay.effective}
    if not active:
        return Unknown("correction is inactive at the incident anchor")
    projected = {**dict(snapshot), **active}
    choices = enumerate_trace_choices(
        ctx.target.tag,
        ctx.target.value,
        projected,
        ctx.pdg,
        ctx.program,
        steerable=ctx.steerable,
        clear_only=getattr(ctx, "clear_only", frozenset()),
    )
    routes: tuple[Any, ...] = tuple(choices) if choices else (None,)
    saw_complete = False
    for route in routes:
        rejected_actions: frozenset[tuple[str, Any]] = frozenset()
        route_blocked = False
        for _ in range(16):
            try:
                tree = trace_back(
                    ctx.target.tag,
                    ctx.target.value,
                    projected,
                    ctx.pdg,
                    ctx.program,
                    ctx.steerable,
                    clear_only=getattr(ctx, "clear_only", frozenset()),
                    opaque_loop=getattr(ctx, "opaque_loop", frozenset()),
                    pipeline_internal_tags=getattr(ctx, "pipeline_internal_tags", frozenset()),
                    route=route,
                    prior=getattr(ctx, "domain_prior", None),
                    rejected_actions=rejected_actions,
                )
            except UnsupportedConstruct:
                return Unknown("target trace contains an unsupported construct")
            # A dead-end leaf is incomplete evidence, not proof that the active
            # correction cut a route.  Keep Unknown distinct from NoRoute by
            # declining the static rejection and allowing bounded replay to judge.
            if not _route_has_no_dead_end([tree]):
                frontier = tuple(
                    (leaf.tag, leaf.value) for leaf in tree.leaves() if not leaf.satisfied
                )
                return Unknown("target trace has an unreadable frontier", frontier)
            actions = tree.ordered_action_details()
            if not actions:
                return Unknown("target trace has no executable continuation")
            conflicts = frozenset(
                action.pair
                for action in actions
                if action.tag in active and not _values_match(active[action.tag], action.value)
            )
            if not conflicts:
                return Unknown(
                    "a target continuation remains compatible with the correction",
                    tuple(action.pair for action in actions),
                )
            novel = conflicts - rejected_actions
            if not novel:
                route_blocked = True
                saw_complete = True
                break
            rejected_actions |= novel
        if not route_blocked:
            # Alternative enumeration is bounded.  Exhaustion is Unknown, not
            # proof that the route is absent.
            return Unknown("target alternative enumeration exhausted its bound")
    if saw_complete:
        return NoRoute("active correction conflicts with every complete target trace")
    return Unknown("target continuation could not be classified")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _last_transition_scan(
    plc: PLC,
    tag: str,
    start_scan: int,
    end_scan: int,
) -> int | None:
    """The latest scan in the window where *tag* changed value, or ``None``.

    The temporal-precedence signal for hypothesis ranking: the watchdog Done
    that ejected the bearing rises *at* the channel departure; a bystander
    fired somewhere earlier in a long coast window.
    """
    try:
        states = plc.history.range(start_scan, end_scan + 1)
    except Exception:  # noqa: BLE001
        return None
    last: int | None = None
    for prev, cur in zip(states, states[1:], strict=False):
        if not _values_match(prev.tags.get(tag), cur.tags.get(tag)):
            last = cur.scan_id
    return last
