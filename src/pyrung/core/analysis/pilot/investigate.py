"""Build and replay bounded hypotheses for departures and excursions.

The module constructs incident windows and replay functions, derives candidate
holds from causal roots, writer enablers, and pinned scans, ranks those
hypotheses, closes the first explanation over sibling causes exposed by its
counterfactual replay, resolves each attempt as accepted, extended, or rejected,
and returns the first composite that survives. It also provides the shorter
excursion investigation used by trial verification.

Investigation confirms a proposed correction but does not install it; recovery
and installation belong to ``progress.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.analysis.pilot._ops import (
    _ZOOM_BUDGET,
    PilotRung,
    _apply_pulse,
    _coast_holding_state,
    _coast_to_value,
    _hold_allowed,
    _pilot_state_key,
    _rung_execution_receipt,
    _rung_identity,
    _rungs_from_proposals,
    _semantic_key,
    _set_rungs,
    _settle_delayed_effects,
    _target_unresolved_condition,
    _union_conditions,
    fork_with_rungs,
)
from pyrung.core.analysis.pilot.advance import iter_advance_owners
from pyrung.core.analysis.pilot.causal import (
    _shared_cause,
    chase_cause_roots,
    chase_chain_tags,
    empirical_program_writes,
)
from pyrung.core.analysis.pilot.corrections import (
    break_guard_holds,
    correct_enablers,
    guard_correction_holds,
)
from pyrung.core.analysis.pilot.gauge import GaugeMovement
from pyrung.core.analysis.pilot.options import _holds_defeat_needed
from pyrung.core.analysis.pilot.skiff import run_pinned_scan
from pyrung.core.analysis.pilot.trace import _can_produce, trace_back
from pyrung.core.analysis.pilot.types import (
    BearingDeparture,
    DeviationIncident,
    MotionKind,
    _AcceptedTrial,
    _ActionPair,
    _ConfirmedCorrection,
    _IterationFrame,
    _Step,
    _StepContext,
)
from pyrung.core.analysis.sp_values import (
    _values_match,
    _writer_for_tag,
    _written_value_for_tag,
)
from pyrung.core.context import RungId

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.types import _PilotContext
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)

# Skiff escalation for a live-word-gated antagonist (excursion suppression).
_SKIFF_SCANS = 4  # pulse -> staged register -> gated clobber, all in one window
_SKIFF_MAX_PROBES = 8  # bounded per-excursion — forks are cheap, not free
_NESTED_MAX_BRANCHES = 8

ActionPair = tuple[str, Any]
CorrectionIdentity = tuple[tuple[Any, ...], ...]


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


def correction_identity(rungs: Iterable[PilotRung]) -> CorrectionIdentity:
    """Exact identity of an executable, replay-confirmed correction.

    ``_rung_identity`` owns executable identity, including the guard and any
    owner-issued operation boundary.  A generated pair is only a hypothesis;
    it cannot become durable negative evidence until investigation gives it a
    scope and confirms that exact installed form.
    """
    identities: list[tuple[Any, ...]] = []
    for rung in rungs:
        if not isinstance(rung, PilotRung):
            raise TypeError("correction identity requires executable PilotRungs")
        identities.append(_rung_identity(rung))
    return tuple(sorted(identities, key=repr))


ReplayFn = Callable[[tuple[Any, ...]], "ReplayOutcome"]


@dataclass(frozen=True)
class ReplayStep:
    """One recorded journey step with its session spec, replay-ready.

    ``kind`` is the *recorded* coast kind (``"pulse"`` / ``"zoom"`` /
    ``"letrun"`` / ``"dwell"``), read off the committed step context — never
    inferred from position or input emptiness.  A zoom step re-arms its own
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
        MotionKind.COAST_TO_BEARING: "zoom",
        MotionKind.COAST_HOLDING_WORLD: "letrun",
    }[context.motion]
    if kind == "zoom" and context.channel_tag is None:
        kind = "dwell"
    return ReplayStep(
        inputs=tuple(step.inputs.items()),
        scans=step.scans,
        kind=kind,
        channel_tag=context.channel_tag,
        channel_target=context.channel_target,
    )


def _deviation_bearing(
    trial: _AcceptedTrial,
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
        if not _values_match(frame.snap.get(tag), trial.fork_snap.get(tag))
        and not any(
            _values_match(trial.fork_snap.get(tag), needed) for needed in needed_by_tag.get(tag, ())
        )
    ]
    channel = trial.channel_motion.channel_tag
    if channel is not None:
        source = trial.before_snap.get(channel)
        landed = trial.fork_snap.get(channel)
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
    progress_gauge: Any = None
    progress_anchor: Mapping[str, Any] | None = None
    regression_progress_floor: Mapping[str, Any] | None = None


# ---------------------------------------------------------------------------
# Incident / hypothesis / result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InvestigationHypothesis:
    """A replay-testable explanation for an incident."""

    kind: str
    holds: tuple[Any, ...]
    sources: tuple[str, ...] = ()
    detail: str = ""


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


@dataclass(frozen=True)
class InvestigationRejection:
    """One rejected hypothesis and the exact ground that rejected it."""

    hypothesis: InvestigationHypothesis
    slug: str
    ground: str


@dataclass(frozen=True)
class _ReplayAccepted:
    """A replay attempt accepted without exposing another causal cut."""

    outcome: ReplayOutcome


@dataclass(frozen=True)
class _HypothesisExtended:
    """A replay exposed another causal cut and extended the hypothesis."""

    hypothesis: InvestigationHypothesis


@dataclass(frozen=True)
class _ReplayRejected:
    """A replay attempt rejected the current hypothesis on an exact ground."""

    rejection: InvestigationRejection


_ReplayResolution = _ReplayAccepted | _HypothesisExtended | _ReplayRejected


@dataclass(frozen=True)
class InvestigationResult:
    """Replay-confirmed corrective information."""

    correction: _ConfirmedCorrection | None = None
    regression_nogoods: frozenset[ActionPair] = frozenset()
    hypotheses: tuple[InvestigationHypothesis, ...] = ()
    confirmed: tuple[InvestigationHypothesis, ...] = ()
    # A rejection is one artifact: its hypothesis, stable machine-readable
    # classification, and human ground cannot become index-desynchronized.
    rejected: tuple[InvestigationRejection, ...] = ()
    unresolved: tuple[str, ...] = ()


def _resolve_replay_attempt(
    *,
    phase: Literal["exploratory", "guarded"],
    current: InvestigationHypothesis,
    outcome: ReplayOutcome,
    seen_replacements: set[tuple[Any, ...]],
    extend: Callable[
        [InvestigationHypothesis, ReplacementEvidence],
        InvestigationHypothesis | None,
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

    fingerprint = tuple(
        (item.rung, item.tag, _semantic_key(item.value)) for item in replacement.witness.cause
    )
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
    return _HypothesisExtended(extended)


def _scoped_correction_rungs(
    plc: PLC,
    proposals: tuple[Any, ...],
    incident: DeviationIncident,
    outcome: ReplayOutcome,
    ctx: Any,
    progress_mark: tuple[tuple[str, Any], ...] = (),
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
    if all(
        isinstance(proposal, PilotRung) and proposal.operation is not None for proposal in proposals
    ):
        return tuple(proposals)

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
                # landing corridor until the Gauge coordinate advances.
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
    return tuple(_rungs_from_proposals(plc, list(scoped_proposals), scope))


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
) -> tuple[Any, ...]:
    """Test a raw correction only where the incident was observed.

    A global exploratory hold is a broader intervention than the guarded fact
    PILOT would install. It can therefore erase earlier, compatible work and
    reject a locally-correct hypothesis for behavior the hypothesis would never
    own. An exact Gauge mark is enough to keep that first replay at the incident;
    the accepted outcome still supplies the narrower channel lifetime below.
    """

    from pyrung.core.condition import AllCondition, AnyCondition, CompareEq

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
        result.extend(_rungs_from_proposals(plc, [proposal], AllCondition(*guard_terms)))
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
    prior_neutralized: bool = False,
) -> _RegressionOwnership:
    """Judge the recorded branch and any replacement inside its bounded replay.

    Replay does not chase a future stable landing. It does own departures
    already visible inside the recorded horizon: a proposal that replaces one
    departure with a departure on its own causal spine has disproved itself.
    A distinct sibling cause remains probationary evidence for the live loop.
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
    cause_silenced = changed_writes_silenced or branch_replaced
    unrelated_departure = unrelated_departure and not shared_suffix
    del prior_neutralized
    return _RegressionOwnership(
        source_preserved=source_preserved,
        cause_silenced=cause_silenced,
        replacement_cause=replacement_cause,
        replacement_owned=replacement_owned,
        replacement_replays_recorded=replacement_replays_recorded,
        unrelated_departure=unrelated_departure,
        neutralized=(source_preserved and cause_silenced) or branch_replaced or unrelated_departure,
        shared_suffix=shared_suffix,
    )


def build_replay_fn(
    cp_fork: PLC,
    cp_trend: int,
    rungs: Sequence[Any],
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

    * **Channel incident** (``zoom_channel_tag`` set — a channel coast or a
      terminal let-run holding a macro-state) — a hold is *good* iff the
      channel register sits at its target/held value instead of ejecting.  The
      coast differs by shape: a **zoom** coast is unbounded and ejection-guarded
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
    zoom_channel_tag = incident.channel_tag
    zoom_target_value = incident.channel_target
    terminal_letrun_role_tags = incident.terminal_role_tags
    replay_watch_roles = incident.watch_roles
    departure_bearing = incident.departure_bearing
    regression_witness = incident.regression_witness
    progress_gauge = incident.progress_gauge
    progress_anchor = incident.progress_anchor
    regression_progress_floor = incident.regression_progress_floor

    def _replay(holds: tuple[Any, ...]) -> ReplayOutcome:
        from pyrung.core.analysis.pilot.coast import CoastSession

        probe = fork_with_rungs(cp_fork, rungs)
        probe_rungs = list(rungs)
        scope = _target_unresolved_condition(probe, target_tag, target_value)
        probe_rungs.extend(_rungs_from_proposals(probe, list(holds), scope))
        _set_rungs(probe, probe_rungs)
        # One session spans the whole replay. The channel pen proves whether the
        # incident's source context was preserved; exact rung-firing timelines
        # independently prove whether its recorded causal branch replayed.
        # ReplayStep.scans is the recorded incident's logical window.  Active
        # hypothesis holds must not reinterpret it as a fresh kernel-work
        # allowance and coast beyond the evidence being replayed.
        session = CoastSession(probe, kind="replay", kernel_budget=False)
        # The last coast step IS the incident's eject coast; its receipt is the
        # bump-local verdict ("did the recorded departure reproduce?") the
        # judgment below reads alongside the endpoint snapshot.
        eject_receipt: Any = None
        if zoom_channel_tag is not None:
            session.arm_pens((zoom_channel_tag,))
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
            elif step.kind == "zoom" and step.channel_tag is not None:
                # Reproduce the recorded incident, not the rest of its route.
                # A correction only has to neutralize the causal regression
                # inside this bounded step; extending replay to the zoom's
                # distant destination lets a later unrelated fault reuse the
                # same state executor and falsely refute the correction.
                eject_receipt = _coast_to_value(
                    probe,
                    step.channel_tag,
                    step.channel_target,
                    budget=(max(1, step.scans) if regression_witness is not None else _ZOOM_BUDGET),
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
            and progress_gauge is not None
            and regression_progress_floor is not None
            and progress_gauge.receipt(
                regression_progress_floor,
                snap,
            ).movement
            is GaugeMovement.BACKWARD
        )
        # A correction owns the recorded operation, not merely its outer
        # channel. Keeping Execute while erasing the Step/phase receipt that
        # identified this incident is suppression by rollback, not
        # neutralization. Check the floor at the bounded incident horizon.
        neutralized = ownership is not None and ownership.neutralized and not progress_erased
        source_preserved = ownership is not None and ownership.source_preserved
        if logger.isEnabledFor(logging.DEBUG):
            roles = terminal_letrun_role_tags or ()
            logger.debug(
                "replay probe: cp_scan=%s end_scan=%s steps=%d shape=%s channel=%s=%r roles=%s",
                cp_fork.state.scan_id,
                probe.state.scan_id,
                len(steps),
                ("letrun" if terminal_letrun_role_tags is not None else "zoom"),
                zoom_channel_tag,
                snap.get(zoom_channel_tag) if zoom_channel_tag else None,
                {t: snap.get(t) for t in roles},
            )

        # Channel incident (channel coast OR terminal let-run hold): the hold is
        # good iff it reaches the requested zoom destination, advances the
        # target-relative gauge, or suppresses the incident's exact departure
        # causal branch within the incident window. A terminal let-run's
        # "target" is the state it was trying to hold, so equality alone is not
        # success there: a direct channel override could mask a still-firing
        # cause. Exact firing testimony distinguishes suppression from masking.
        if zoom_channel_tag is not None:
            reached = terminal_letrun_role_tags is None and _values_match(
                snap.get(zoom_channel_tag), zoom_target_value
            )
            neutralized_reason = None
            if neutralized and regression_witness is not None:
                neutralized_reason = (
                    "recorded regression neutralized: "
                    f"preserved {zoom_channel_tag}={regression_witness.source!r} "
                    f"and suppressed its {len(regression_witness.cause)}-write causal branch"
                    if source_preserved
                    else "recorded cause silenced before an unrelated replacement departure"
                )
            progressed = neutralized_reason
            gauge_advanced = False
            if (
                not reached
                and progressed is None
                and progress_gauge is not None
                and progress_anchor is not None
                and progress_gauge.receipt(progress_anchor, snap).movement is GaugeMovement.FORWARD
            ):
                gauge_advanced = True
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
                        f"{zoom_channel_tag} -> {zoom_target_value!r} reached={reached}"
                        + (
                            f" (eject coast: {eject_receipt.stop_reason})"
                            if eject_receipt is not None
                            else ""
                        )
                    )
                )
            )
            accepted = (
                gauge_advanced or (not cause_repeated and (reached or progressed is not None))
            ) and not progress_erased
            return ReplayOutcome(
                accepted=accepted,
                trend=None,
                snapshot=snap,
                reason=(progressed if accepted else rejection_reason) or rejection_reason,
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
                        if reached
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

        # Terminal let-run without a channel register (no recognized state
        # machine): judge the global target at the bounded point.
        if terminal_letrun_role_tags is not None:
            reached = _values_match(snap.get(target_tag), target_value)
            return ReplayOutcome(
                accepted=reached,
                trend=None,
                snapshot=snap,
                reason=f"{target_tag} -> {target_value!r} reached={reached}",
                justification=ReplayJustification.REACHED if reached else None,
            )

        # Command incident: no register to coast toward — judge the bounded
        # bearing-held directly.
        if departure_bearing:
            held = all(_values_match(snap.get(t), v) for t, v in departure_bearing)
            return ReplayOutcome(
                accepted=held,
                trend=None,
                snapshot=snap,
                reason=f"bearing {'held' if held else 'departed'} at bounded replay",
                justification=ReplayJustification.BEARING_HELD if held else None,
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
        return ReplayOutcome(
            accepted=trend <= cp_trend,
            trend=trend,
            snapshot=snap,
            reason=f"trend {trend} <= checkpoint {cp_trend}",
            justification=ReplayJustification.ADVANCED if trend < cp_trend else None,
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
    retry_fork: Any = None
    # The retry pulse's recorded session events — the timeline the retry trial
    # carries forward (its Done-bit pen marks must stay visible to a later
    # incident window).
    retry_timeline: tuple[Any, ...] = ()


def investigate_excursion(
    work: PLC,
    fork: PLC,
    pre_snap: dict[str, Any],
    post_pulse_snap: dict[str, Any],
    pre_key: tuple[Any, ...],
    action: list[ActionPair],
    *,
    cfg: Any,
    steerable: frozenset[str],
    rungs: Sequence[Any],
    resting: dict[str, Any],
    edge_tags: set[str],
    scan_budget: int,
    pdg: Any = None,
    program: Any = None,
    ctx: Any = None,
) -> ExcursionResult:
    """Diagnose an excursion and replay-validate candidate holds.

    Verify detected that the state key changed during the pulse but
    reverted after settling.  This function finds *why* and *validates*.

    Primary path: suppress the *antagonist* — any writer of a reverted register
    that is **causally implicated** in the deviation (``cause()`` attributes the
    tag's change to it) and that **provably drives the tag away** from the value
    the pulse established (``_can_produce`` False).  Dispatch is by causal
    implication + producibility, never by instruction class name: a plain
    clobbering ``copy`` is suppressed exactly like a ``reset``.  Each implicated
    writer's guard is forced FALSE by the inverted-polarity forcing enumeration
    (``break_guard_holds``); when that punts on a genuinely-live word guard, the
    skiff runs bounded isolated probes for a suppressing lever (nominations only).

    Fallback: cause-chain walk and cause() enablers (original path — this is what
    resolves seal-in establishment cases, where the writer *can* still produce the
    desired value so it is not a suppression antagonist).

    The successful result carries the exact guarded rungs used by retry. The
    caller may admit and install that correction, but must not reconstruct its
    lifetime from the bare input values.
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
    # punt.  Every hold is confirmed by the retry gate below — nothing unverified.
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
            for ni in _implicated_writers(fork, tag, pdg, program):
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
                        action,
                        pdg,
                        steerable,
                        rungs,
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

    action_tags = {t for t, _ in action}
    candidate_holds = [(t, v) for t, v in candidate_holds if t not in action_tags]
    if ctx is not None:
        candidate_holds = [hold for hold in candidate_holds if _hold_allowed(ctx, hold)]
    if not candidate_holds:
        return ExcursionResult(reverted=reverted)

    retry = fork_with_rungs(work, rungs)
    retry_rungs = list(rungs)
    from pyrung.core.analysis.pilot.coast import CoastSession
    from pyrung.core.condition import CompareEq

    preserved_tag = reverted[0]
    preserved = retry._known_tags_by_name[preserved_tag]
    scope = CompareEq(preserved, post_pulse_snap[preserved_tag])
    confirmed_rungs = tuple(_rungs_from_proposals(retry, candidate_holds, scope))
    retry_rungs.extend(confirmed_rungs)
    _set_rungs(retry, retry_rungs)
    kickoff = list(action)
    kickoff.extend((t, v) for t, v in candidate_holds if t not in {a for a, _ in action})
    session = CoastSession(retry, kind="excursion-retry")
    if program is not None:
        session.arm_pens(
            owner.profile.done.name
            for owner in iter_advance_owners(program)
            if owner.profile.done is not None
        )
    _apply_pulse(retry, kickoff, resting, edge_tags, session=session)
    _settle_delayed_effects(retry, pre_snap, cfg, scan_budget=scan_budget, session=session)
    retry_snap = dict(retry.state.tags)
    retry_key = _pilot_state_key(retry_snap, cfg)

    if retry_key != pre_key:
        return ExcursionResult(
            reverted=reverted,
            correction=_ConfirmedCorrection(
                identity=correction_identity(confirmed_rungs),
                rungs=confirmed_rungs,
                sources=tuple(dict.fromkeys((*reverted, *(tag for tag, _ in candidate_holds)))),
                justification="excursion replay preserved the pulse-established state",
            ),
            retry_fork=retry,
            retry_timeline=session.events,
        )
    return ExcursionResult(reverted=reverted)


def _implicated_writers(plc: PLC, tag: str, pdg: Any, program: Any) -> list[int]:
    """PDG writer-node indices of *tag* causally implicated in its deviation.

    Dispatch by causal implication, never by instruction class: ``cause()``
    attributes the reverted tag's change to the rung(s) that actually wrote it in
    the settled window; those are the antagonists worth suppressing.  A writer
    that never fired is not in the chain and is left alone.  Maps the chain's
    ``(rung_index, subroutine)`` back to the PDG writer nodes.  ``[]`` when
    ``cause()`` is unavailable — the fallback path then runs unchanged.
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
    action: list[ActionPair],
    pdg: Any,
    steerable: frozenset[str],
    rungs: Sequence[PilotRung],
) -> list[ActionPair]:
    """Bounded isolated probes for a live-word-gated antagonist — nominations only.

    ``break_guard_holds`` punted (the antagonist's guard reads a genuinely-live
    word with no forceable finite domain).  Probe each **condition-read**
    steerable Bool lever in the antagonist guard's upstream cone: hold it, replay
    the pulse in a pinned fork over the deviation window (``run_pinned_scan``),
    and keep the levers under which the antagonist does **not** fire — the reverted
    register ends at its desired (pulse-established) value.

    Only Bool levers are probed (flipped off their current antagonist-firing
    value); a wide/unknown word offers no sound probe value (the skiff never
    guesses).  The returned holds are nominations: they ride the same retry gate
    as any static hold and are never applied unconfirmed.
    """
    action_tags = {t for t, _ in action}
    condition_read = {t for n in pdg.rung_nodes for t in getattr(n, "condition_reads", ())}
    cone: set[str] = set()
    for guard_tag in node.condition_reads:
        cone |= set(pdg.upstream_slice(guard_tag, follow_calls=True))
        cone.add(guard_tag)
    levers = sorted((cone & steerable & condition_read) - action_tags)

    snap = dict(work.state.tags)
    allowed = set(pdg.upstream_slice(tag, follow_calls=True))
    allowed.add(tag)
    allowed.update(action_tags)

    nominations: list[ActionPair] = []
    budget = _SKIFF_MAX_PROBES
    for lever in levers:
        if budget <= 0:
            break
        cur = snap.get(lever)
        if not isinstance(cur, bool):
            continue  # only Bool levers — never guess a word value
        budget -= 1
        val = not cur  # flip off the polarity under which the antagonist fires
        probe_actions = tuple({**dict(action), lever: val}.items())
        result = run_pinned_scan(
            work,
            frozenset(allowed | {lever}),
            pdg,
            rungs=rungs,
            actions=probe_actions,
            scans=_SKIFF_SCANS,
        )
        if _values_match(result.after.get(tag), desired):
            nominations.append((lever, val))
    return nominations


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
    steps' pen marks and bump landings): ``changed_tags`` membership and every
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
    hypotheses: Sequence[InvestigationHypothesis],
    incident: DeviationIncident,
    ctx: Any,
    primal_extra: frozenset[str] = frozenset(),
) -> list[InvestigationHypothesis]:
    """Order competing hypotheses by **causal primacy**, not generation order.

    The channel departure (``incident.channel_tag`` — the ejection itself)
    is the incident; other departures are collateral downstream of it (the
    state-8 shared-init resetting ``Heat_CurStep``).  Two primacy signals,
    strongest first:

    * **chain membership** — the hypothesis's tags sit inside the cause chain
      of the channel departure.  ``chase_chain_tags(..., bridge=ctx)`` crosses
      the opaque-pipeline hop by route inversion (the compass bridge): where the
      recorded-history walk dead-ends at a held ``S_StateRequested`` /
      ``isStateEnbl_Yes`` enabler, the bridge consults ``ctx.compass.graphs`` for
      the requesters of the observed destination transition, confirms which route
      fired against recorded history, and resumes the walk from that route's
      guard tags — so on a PackML-shaped program the chain reaches the starved
      watchdog directly instead of stopping short of it.
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
        # ``bridge=ctx`` crosses the opaque-pipeline hop by route inversion, so
        # the chain reaches the true root (the starved watchdog) instead of
        # dead-ending at the held ``S_StateRequested`` enabler — making causal
        # primacy exact rather than won on temporal proximity.
        primal = {chan} | chase_chain_tags(plc, chan, scan=dep_scan.get(chan), bridge=ctx)
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

    def _key(pair: tuple[int, InvestigationHypothesis]) -> tuple[int, int, int, int]:
        idx, h = pair
        tags = set(h.sources) | {_proposal_pair(p)[0] for p in h.holds}
        in_chain = 0 if (primal and tags & primal) else 1
        proximity = 0 if in_chain == 0 else _proximity(tags)
        return (in_chain, proximity, len(h.holds), idx)

    return [h for _, h in sorted(enumerate(hypotheses), key=_key)]


def _generate_deviation_hypotheses(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
    *,
    installed: Mapping[str, Any] | None = None,
) -> tuple[list[InvestigationHypothesis], frozenset[str]]:
    """Generate and rank one incident's evidence-derived hypotheses."""
    absence_hyps, absence_tags = _absence_root_correctives(
        plc,
        incident,
        ctx,
        exclude=frozenset(tag for tag, _value in incident.action),
        installed=installed or {},
    )
    raw: list[InvestigationHypothesis] = list(absence_hyps)
    raw.extend(_precise_causes(plc, incident, ctx))
    raw.extend(
        InvestigationHypothesis(kind=c.kind, holds=c.holds, sources=c.sources, detail=c.detail)
        for c in correct_enablers(plc, incident, ctx)
    )
    ranked = _rank_hypotheses(
        plc,
        _dedupe_hypotheses(raw),
        incident,
        ctx,
        primal_extra=absence_tags,
    )
    return ranked, absence_tags


def _compose_hypotheses(
    base: InvestigationHypothesis,
    addition: InvestigationHypothesis,
) -> InvestigationHypothesis | None:
    """Create a newly replayable union, declining contradictory operations."""
    holds = list(base.holds)
    seen = {_proposal_identity(hold) for hold in holds}
    by_dest = {dest: value for dest, value in map(_proposal_pair, holds)}
    for hold in addition.holds:
        dest, value = _proposal_pair(hold)
        if dest in by_dest and not _values_match(by_dest[dest], value):
            return None
        identity = _proposal_identity(hold)
        if identity not in seen:
            seen.add(identity)
            holds.append(hold)
            by_dest.setdefault(dest, value)
    if len(holds) == len(base.holds):
        return None
    kind = base.kind if base.kind == addition.kind else "nested-cause"
    return InvestigationHypothesis(
        kind=kind,
        holds=tuple(holds),
        sources=tuple(dict.fromkeys((*base.sources, *addition.sources))),
        detail=f"nested causal closure: {base.detail}; then {addition.detail}",
    )


def investigate_deviation(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
    replay: ReplayFn,
    *,
    needed: Sequence[tuple[str, Any]] = (),
    installed_rungs: Sequence[Any] = (),
    correction_rungs: Sequence[Any] = (),
    correction_progress_mark: tuple[tuple[str, Any], ...] = (),
    occurrence_requirements: tuple[tuple[str, Any], ...] = (),
    excluded_corrections: frozenset[CorrectionIdentity] = frozenset(),
) -> InvestigationResult:
    """Investigate an incident with precise hypothesis generation.

    Two sources, both instrument-derived:
    1. Precise cause walk — single cause()-chain from the first departure
       that reaches a steerable input (the *trigger-found* case).
    2. Enabler correction — when cause finds no steerable trigger, the held
       enablers are the cause; ``correct_enablers`` asks the writer for either
       a guard-breaking assignment or an owner-declared accumulator operation.
    No upstream cone sweep.

    Hypotheses are ranked by causal primacy. A counterfactual replacement with
    the same bounded channel outcome and exact participating pipeline extends
    the current hypothesis inside this investigation; the composite is then
    replayed from the original checkpoint. Hypotheses whose holds are *already installed*
    (*installed*) are skipped, not re-confirmed: they were active when the
    incident happened, so a repeat regression at the same key escalates to the
    runner-up instead of re-anointing the incumbent.
    """
    # Absence roots generate FIRST: rank ties inside the causal chain break by
    # generation order, and when a never-moved terminal (the stuck permissive)
    # and a mid-chain suppressor (the abort rung's ~Suspend enabler) both
    # survive the bounded replay, the terminal names the cause while the
    # suppressor merely mutes the response.
    installed_rungs = tuple(installed_rungs)
    # Ask the overlay compiler which installed rule actually owned each
    # destination at the incident anchor.  Observing the full overlay before
    # filtering correction provenance preserves start/continuation precedence
    # and prevents an eligible, shadowed, or dormant sibling from claiming the
    # write. Persistent rules are not removed merely because they are inactive.
    overlay = _rung_execution_receipt(installed_rungs, dict(incident.before_snap))
    installed_active = {rung.dest: rung.value for rung in overlay.effective}
    correction_ids = {_rung_identity(rung) for rung in correction_rungs}
    correction_active = {
        rung.dest: rung.value
        for rung in overlay.effective
        if _rung_identity(rung) in correction_ids
    }
    hypotheses, _absence_tags = _generate_deviation_hypotheses(
        plc,
        incident,
        ctx,
        installed=correction_active,
    )
    observed_hypotheses = list(hypotheses)
    confirmed: list[InvestigationHypothesis] = []
    confirmed_correction: _ConfirmedCorrection | None = None
    rejected: list[InvestigationRejection] = []
    pdg = getattr(ctx, "pdg", None)
    program = getattr(ctx, "program", None)
    # A proposed hold at the anchor value is meaningful when the complete
    # incident record says that tag moved away (including an installed guard
    # expiring).  Correction engines filter this factual set locally.
    recorded_incident_movers = frozenset(incident.changed_tags)

    def _reject(hyp: InvestigationHypothesis, slug: str, detail: str) -> None:
        rejected.append(InvestigationRejection(hyp, slug, detail))

    def _extend_from_replacement(
        current: InvestigationHypothesis,
        evidence: ReplacementEvidence,
    ) -> InvestigationHypothesis | None:
        """Derive the next cut from the retained fork and add it to *current*."""
        nested, _absence = _generate_deviation_hypotheses(
            evidence.plc,
            evidence.incident,
            ctx,
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
                return composite
        return None

    for hypothesis in hypotheses:
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
                    installed_rungs,
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
        current = hypothesis
        seen_replacements: set[tuple[Any, ...]] = set()
        for _nested_depth in range(_NESTED_MAX_BRANCHES + 1):
            exploratory = _exploratory_correction_rungs(
                plc,
                current.holds,
                incident,
                correction_progress_mark,
            )
            outcome = replay(exploratory)
            resolution = _resolve_replay_attempt(
                phase="exploratory",
                current=current,
                outcome=outcome,
                seen_replacements=seen_replacements,
                extend=_extend_from_replacement,
            )
            if isinstance(resolution, _ReplayRejected):
                rejected.append(resolution.rejection)
                break
            if isinstance(resolution, _HypothesisExtended):
                current = resolution.hypothesis
                continue
            outcome = resolution.outcome

            # A target-work correction belongs to the exact Gauge occurrence.
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
            )
            if correction_identity(scoped) in excluded_corrections:
                _reject(
                    current,
                    "correction-revoked",
                    "correction was previously revoked after causing a later regression",
                )
                break
            required_progress = (*incident.bearing, *needed)
            if (
                pdg is not None
                and program is not None
                and _active_rungs_defeat_needed(
                    scoped,
                    required_progress,
                    incident.before_snap,
                    pdg,
                    program,
                )
            ):
                # Replay windows are deliberately bounded to the incident. A
                # correction can silence that incident yet pin a slower progress
                # register behind the checkpoint frontier after the window ends.
                # Screen the exact guarded form that would be installed; the
                # guard limits where the pin applies, but cannot make it harmless
                # while that context is active.
                _reject(
                    current,
                    "self-defeat",
                    "guarded correction defeats requested progress: "
                    f"needed={required_progress!r}, correction={tuple(scoped)!r}",
                )
                break
            # Operation-owned proposals and already-exact guards can survive
            # scoping unchanged. Replay is deterministic from the retained
            # incident checkpoint, so an identical executable correction has
            # already proved its installed form in the exploratory pass.
            installed_outcome = outcome if scoped == exploratory else replay(scoped)
            resolution = _resolve_replay_attempt(
                phase="guarded",
                current=current,
                outcome=installed_outcome,
                seen_replacements=seen_replacements,
                extend=_extend_from_replacement,
            )
            if isinstance(resolution, _ReplayRejected):
                rejected.append(resolution.rejection)
                break
            if isinstance(resolution, _HypothesisExtended):
                current = resolution.hypothesis
                continue
            installed_outcome = resolution.outcome
            confirmed_hypothesis = InvestigationHypothesis(
                kind=current.kind,
                holds=scoped,
                sources=current.sources,
                detail=current.detail,
            )
            confirmed.append(confirmed_hypothesis)
            confirmed_correction = _ConfirmedCorrection(
                identity=correction_identity(scoped),
                rungs=scoped,
                sources=confirmed_hypothesis.sources,
                justification=(
                    installed_outcome.justification.value
                    if installed_outcome.justification is not None
                    else installed_outcome.reason or "replay-confirmed"
                ),
            )
            break
        else:
            _reject(
                current,
                "nested-cause-budget",
                f"nested causal closure exceeded {_NESTED_MAX_BRANCHES} replacement branches",
            )
        if confirmed:
            break  # first confirmed composite wins — one intervention per incident

    return InvestigationResult(
        correction=confirmed_correction,
        regression_nogoods=frozenset(),
        hypotheses=tuple(observed_hypotheses),
        confirmed=tuple(confirmed),
        rejected=tuple(rejected),
        unresolved=incident.changed_tags if not confirmed else (),
    )


# ---------------------------------------------------------------------------
# Hypothesis generation — precise pass
# ---------------------------------------------------------------------------


def _active_rungs_defeat_needed(
    rungs: Sequence[PilotRung],
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
    overlay = _rung_execution_receipt(rungs, snapshot)
    active = [(rung.dest, rung.value) for rung in overlay.effective]
    return _holds_defeat_needed(active, needed, pdg, program)


def _precise_causes(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
) -> list[InvestigationHypothesis]:
    """Minimal controllable cuts of the exact deep fired chain.

    For each departure, ``cause(deep=True)`` supplies the rungs that actually
    fired, their exact transitions, and their steady enablers. The walk derives
    two forms of cut from that one record:

    * revert a steerable transition at its pre-incident value;
    * force a fired rung's guard false with the cheapest drivable assignment.

    Program-written condition tags are never terminal levers merely because
    static steerability includes them; guard solving follows their
    observed/static writers to an external lever. Cuts that oppose requested
    progress are still hypotheses: the investigation layer proves them harmful
    against the incident bearing/checkpoint frontier and records a
    ``self-defeat`` rejection. Every returned hypothesis names the fired rung
    whose conductive path it cuts.
    """
    steerable = getattr(ctx, "steerable", frozenset())
    if not steerable:
        return []
    pdg = getattr(ctx, "pdg", None)
    program = getattr(ctx, "program", None)
    if pdg is None or program is None:
        return []
    empirical_writes = empirical_program_writes(
        plc,
        steerable,
        start_scan=incident.anchor_scan,
        end_scan=incident.end_scan,
    )
    hypotheses: list[InvestigationHypothesis] = []

    # The channel departure is the incident's causal effect. Bearing aliases
    # (``Sts_State_Starting`` falling because the channel already left Starting)
    # are downstream symptoms and must not seed cuts of their observer/mapping
    # rungs. Coast receipts retain the exact channel transition even when the
    # requested destination was never reached and therefore has no ordinary
    # ``BearingDeparture.scan``.
    seeds = list(incident.departures)
    if incident.channel_tag is not None:
        channel_scan = next(
            (
                event.scan
                for event in reversed(incident.timeline)
                if any(
                    tag == incident.channel_tag and not _values_match(before, after)
                    for tag, before, after in getattr(event, "transitions", ())
                )
            ),
            None,
        )
        if channel_scan is not None:
            desired = next(
                (value for tag, value in incident.bearing if tag == incident.channel_tag),
                incident.before_snap.get(incident.channel_tag),
            )
            seeds = [BearingDeparture(incident.channel_tag, desired, channel_scan)]

    for departure in seeds:
        chain = _shared_cause(plc, departure.tag, departure.scan)
        if chain is None:
            continue

        steps_by_tag: dict[str, list[Any]] = {}
        for step in chain.steps:
            steps_by_tag.setdefault(step.transition.tag_name, []).append(step)

        # Static steerability is narrowed only by exact evidence that the user
        # program/plant authored the transition.  A recorded synthesis writer
        # does not by itself turn its external destination into an internal
        # intermediate; the causal walk may still terminate at that lever.
        effective_steerable = frozenset(steerable) - empirical_writes

        origin_memo: dict[str, frozenset[str]] = {}

        def _origins(
            name: str,
            visiting: frozenset[str] = frozenset(),
            *,
            _steps_by_tag: dict[str, list[Any]] = steps_by_tag,
            _origin_memo: dict[str, frozenset[str]] = origin_memo,
        ) -> frozenset[str]:
            if name in _origin_memo:
                return _origin_memo[name]
            if name in visiting:
                return frozenset()
            next_visiting = visiting | {name}
            found: set[str] = set()
            for step in _steps_by_tag.get(name, ()):
                links = step.triggers or step.enablers
                for link in links:
                    found.update(_origins(link.tag_name, next_visiting))
            result = frozenset(found or {name})
            _origin_memo[name] = result
            return result

        def _step_label(step: Any) -> str:
            return f"{step.subroutine + ':' if step.subroutine else ''}R{step.rung_index + 1}"

        # The undesired path is the trigger spine from the effect. Deep enabler
        # expansion supplies origins for conditions on that spine, but those
        # supporting writer rungs are not themselves antagonists to cut.
        trigger_spine: set[int] = set()

        def _mark_trigger_spine(
            transition: Any,
            visiting: frozenset[tuple[str, int]] = frozenset(),
            *,
            _steps_by_tag: dict[str, list[Any]] = steps_by_tag,
            _trigger_spine: set[int] = trigger_spine,
        ) -> None:
            key = (transition.tag_name, transition.scan_id)
            if key in visiting:
                return
            next_visiting = visiting | {key}
            for step in _steps_by_tag.get(transition.tag_name, ()):
                if step.transition.scan_id != transition.scan_id or not _values_match(
                    step.transition.to_value,
                    transition.to_value,
                ):
                    continue
                _trigger_spine.add(id(step))
                for trigger in step.triggers:
                    _mark_trigger_spine(trigger, next_visiting)

        _mark_trigger_spine(chain.effect)

        # First candidate: exact transitioned leaves. This is the same causal
        # frontier as the rung cuts below, not a separate heuristic; it is
        # preferred because preserving the pre-transition physical value is the
        # lightest faithful correction.
        nogoods, mover_holds = chase_cause_roots(
            plc,
            departure.tag,
            effective_steerable,
            scan=departure.scan,
            bridge=ctx,
        )
        moved_tags = {
            tr.tag_name
            for step in chain.steps
            if id(step) in trigger_spine
            for tr in step.triggers
            if not _values_match(tr.from_value, tr.to_value)
        }
        mover_holds_filtered = tuple(
            pair
            for pair in _dedupe_pairs(mover_holds)
            if pair[0] in moved_tags and _hold_allowed(ctx, pair)
        )
        if mover_holds_filtered:
            mover_names = {tag for tag, _value in mover_holds_filtered}
            common: list[tuple[int, Any]] = []
            for index, step in enumerate(chain.steps):
                if id(step) not in trigger_spine:
                    continue
                leaves: set[str] = set()
                for trigger in step.triggers:
                    leaves.update(_origins(trigger.tag_name))
                if mover_names <= leaves:
                    common.append((index, step))
            frontier = common[-1][1] if common else chain.steps[0]
            sources = tuple(sorted(nogoods | mover_names | {departure.tag}))
            hypotheses.append(
                InvestigationHypothesis(
                    kind="precise-cause",
                    holds=guard_correction_holds(
                        plc,
                        mover_holds_filtered,
                        sources,
                        incident,
                        ctx,
                    ),
                    sources=sources,
                    detail=(
                        f"{_step_label(frontier)} fired at scan "
                        f"{frontier.transition.scan_id}; revert exact trigger frontier"
                    ),
                )
            )

        if departure.scan is None:
            frame = dict(plc.state.tags)
        else:
            try:
                frame = dict(plc.history.at(departure.scan).tags)
            except Exception:  # noqa: BLE001
                frame = dict(plc.state.tags)

        # Then enumerate minimal guard cuts for every actual fired rung. Reads
        # are all eligible hypotheses, including cuts through requested
        # progress. The investigation layer, not the generator, records why a
        # progress-damaging cut is harmful.
        from pyrung.core.analysis.pdg import resolve_rung

        for step in reversed(chain.steps):
            if id(step) not in trigger_spine:
                continue
            direct_values = {
                **{tr.tag_name: tr.to_value for tr in step.triggers},
                **{ec.tag_name: ec.value for ec in step.enablers},
            }
            if not direct_values:
                continue
            node = next(
                (
                    pdg.rung_nodes[node_idx]
                    for node_idx in sorted(
                        pdg.writers_of.get(step.transition.tag_name, frozenset())
                    )
                    if (
                        pdg.rung_nodes[node_idx].rung_index,
                        pdg.rung_nodes[node_idx].subroutine,
                    )
                    == (step.rung_index, step.subroutine)
                ),
                None,
            )
            if node is None:
                continue
            rung_obj = resolve_rung(program, node)
            if rung_obj is None:
                continue
            writer = _writer_for_tag(rung_obj, step.transition.tag_name)
            if writer is None:
                continue
            if not getattr(writer, "INERT_WHEN_DISABLED", True):
                # A false rung is not necessarily a suppressed writer. OUT
                # actively writes False when disabled; timers/counters and
                # drums also have instruction-specific disabled behavior.
                # Only the ordinary OUT-to-True case has the generic
                # "make guard false" inverse. Other non-inert writers belong
                # to their instruction-specific correction machinery.
                from pyrung.core.instruction.coils import OutInstruction

                if not (isinstance(writer, OutInstruction) and step.transition.to_value is True):
                    continue
            guard_reads = set(getattr(node, "condition_reads", ())) & set(direct_values)
            fixed: dict[str, Any] = {}
            changeable = guard_reads
            if not changeable:
                continue
            fire_frame = {**frame, **direct_values}
            holds = break_guard_holds(
                rung_obj,
                fire_frame,
                ctx,
                changeable=changeable,
                fixed=fixed,
                steerable=effective_steerable,
            )
            holds_filtered = tuple(
                pair for pair in _dedupe_pairs(holds or ()) if _hold_allowed(ctx, pair)
            )
            if not holds_filtered:
                continue
            sources = tuple(
                sorted(
                    {
                        departure.tag,
                        step.transition.tag_name,
                        *direct_values,
                        *(tag for tag, _value in holds_filtered),
                    }
                )
            )
            hypotheses.append(
                InvestigationHypothesis(
                    kind="precise-cause",
                    holds=guard_correction_holds(
                        plc,
                        holds_filtered,
                        sources,
                        incident,
                        ctx,
                    ),
                    sources=sources,
                    detail=(
                        f"{_step_label(step)} fired at scan "
                        f"{step.transition.scan_id}; minimal conductive cut"
                    ),
                )
            )
    return list(_dedupe_hypotheses(hypotheses))


# ---------------------------------------------------------------------------
# Hypothesis generation — absence roots (deep cause walk)
# ---------------------------------------------------------------------------

_ABSENCE_ROOT_KINDS = frozenset({"external", "never_written"})

#: logical negation of an ordered-comparison form — the analog "flip".
_NEGATE_FORM = {"lt": "ge", "le": "gt", "gt": "le", "ge": "lt"}


def _ordered_truth(form: str, lhs: Any, rhs: Any) -> bool | None:
    """Truth of ``lhs <form> rhs``, or ``None`` when the pair doesn't order."""
    try:
        return {
            "lt": lhs < rhs,
            "le": lhs <= rhs,
            "gt": lhs > rhs,
            "ge": lhs >= rhs,
        }[form]
    except TypeError:
        return None


def _analog_boundary_hold(
    plc: PLC,
    root: Any,
    chain: Any,
    ctx: Any,
) -> tuple[tuple[str, Any], str] | None:
    """The analog analogue of the Bool flip: ``(hold, note)`` for a wide root.

    A Bool absence root flips to its complement; a wide word has none — but
    the chain knows what the stuck value *does*: the root supports the fault
    path through an ordered comparison on one of the chain's rungs.  So flip
    the comparison's truth instead: solve the boundary of the flipped atom
    against the current snapshot (the same stage-2/stage-3 resolvers as the
    trace's relational levers) and propose that value as the corrective hold.
    A guess is fine because it is replay-verified; a root with no ordered
    comparison on the chain's rungs still yields nothing (fail closed), and
    the comparison search never leaves the recorded chain — a program-wide
    sweep would invent levers from rungs that played no part in the incident.
    """
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.analysis.pilot.static_expressions import (
        _atom_text,
        _heuristic_inequality_target,
        _resolve_inequality_target,
    )
    from pyrung.core.analysis.simplified import And, Atom, Or, _conditions_list_to_expr
    from pyrung.core.analysis.sp_values import _FLIP_FORM

    name = root.tag_name
    pdg = getattr(ctx, "pdg", None)
    program = getattr(ctx, "program", None)
    if pdg is None or program is None:
        return None
    snapshot = dict(plc.state.tags)
    steerable = getattr(ctx, "steerable", frozenset())
    prior = getattr(ctx, "domain_prior", None)

    def _iter_atoms(expr: Any) -> Any:
        if isinstance(expr, Atom):
            yield expr
        elif isinstance(expr, (And, Or)):
            for term in expr.terms:
                yield from _iter_atoms(term)

    step_keys = {(s.rung_index, s.subroutine) for s in chain.steps}
    seen: set[tuple[str, str, Any, bool]] = set()
    for node in pdg.rung_nodes:
        if name not in getattr(node, "condition_reads", ()):
            continue
        if (node.rung_index, node.subroutine) not in step_keys:
            continue
        rung = resolve_rung(program, node)
        if rung is None:
            continue
        for atom in _iter_atoms(_conditions_list_to_expr(getattr(rung, "_conditions", []))):
            # Key the atom on the root (operand side flips via A>B ⟺ B<A).
            if atom.tag == name:
                atom_on_root = atom
            elif atom.operand_is_tag and atom.operand == name and atom.form in _FLIP_FORM:
                atom_on_root = Atom(
                    tag=name,
                    form=_FLIP_FORM[atom.form],
                    operand=atom.tag,
                    operand_is_tag=True,
                )
            else:
                continue
            if atom_on_root.form not in _NEGATE_FORM or atom_on_root._key() in seen:
                continue
            seen.add(atom_on_root._key())
            operand = atom_on_root.operand
            threshold = snapshot.get(operand) if atom_on_root.operand_is_tag else operand
            truth = _ordered_truth(atom_on_root.form, root.value, threshold)
            if truth is None:
                continue
            # Cross the boundary AWAY from the value's current contribution:
            # satisfy the negation of whatever the stuck value makes true.
            goal = (
                Atom(
                    tag=name,
                    form=_NEGATE_FORM[atom_on_root.form],
                    operand=operand,
                    operand_is_tag=atom_on_root.operand_is_tag,
                )
                if truth
                else atom_on_root
            )
            target = _resolve_inequality_target(goal, snapshot, prior, pdg)
            marker = ""
            if target is None or target[0] != name:
                hit = _heuristic_inequality_target(goal, snapshot, steerable, pdg)
                if hit is None:
                    continue
                value, marker = hit
                target = (name, value)
            tag, value = target
            if _values_match(snapshot.get(tag), value):
                continue  # not a move
            note = f"cross {_atom_text(goal)} (e.g., {tag} = {value!r}"
            if marker:
                note += f"; {marker}"
            note += ")"
            return (tag, value), note
    return None


def _absence_root_correctives(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
    exclude: frozenset[str] = frozenset(),
    installed: Mapping[str, Any] | None = None,
) -> tuple[list[InvestigationHypothesis], frozenset[str]]:
    """Corrective holds from the deep walk's never-moved roots.

    The shallow chase cannot reach a cause that never transitioned — a
    permissive held open since cold, buffered behind an intermediate error
    register and laundered through a block sum (the sail trap).  The deep
    recorded walk (``cause(deep=True)``) names exactly those terminals:
    ``RootCause`` entries with ``held_since_scan=None``.  Each steerable,
    never-moved Bool root becomes a FLIP hold hypothesis, replay-tested
    like any other — a guess is fine because it is replay-verified.

    Returns the hypotheses plus the root tag names, which the caller feeds
    to ``_rank_hypotheses`` as ``primal_extra``: an absence root produces no
    transition for the temporal-proximity signal, so without chain-member
    standing it would rank behind every temporally-nearby bystander whose
    hold merely defers the fault past the bounded replay window.

    *exclude* carries the action that launched this incident: flipping that
    input would be self-investigation.  *installed* names values owned by prior
    confirmed corrections.  Those roots are not blindly flipped, but a wide
    value may be recomputed from a different ordered boundary on this incident's
    recorded chain; replay and correction revocation then verify the handoff.
    """
    chan = incident.channel_tag
    dep = None
    if chan is not None:
        dep = next((d for d in incident.departures if d.tag == chan), None)
    if dep is None:
        dep = next(iter(incident.departures), None)
    if dep is None:
        return [], frozenset()
    chain = _shared_cause(plc, dep.tag, dep.scan)
    if chain is None:
        return [], frozenset()

    steerable = getattr(ctx, "steerable", frozenset())
    installed = installed or {}
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "absence-root: %s@%s roots=%s",
            dep.tag,
            dep.scan,
            [(r.tag_name, r.value, r.kind, r.held_since_scan) for r in chain.ranked_roots()],
        )
    keyed: list[tuple[int, InvestigationHypothesis]] = []
    root_tags: set[str] = set()
    for root in chain.ranked_roots():
        if root.kind not in _ABSENCE_ROOT_KINDS:
            continue
        if root.tag_name in exclude:
            continue  # the incident-launching action, not a program absence
        if root.tag_name not in steerable:
            continue
        installed_owner = root.tag_name in installed and _values_match(
            installed[root.tag_name], root.value
        )
        if root.held_since_scan is not None and not installed_owner:
            continue  # it moved during the run and has no prior correction owner
        if installed_owner:
            # An installed value appearing on the new fault chain is evidence
            # only when that chain computes a different ordered boundary.  In
            # particular, never complement a prior Boolean correction merely
            # because replay made its write visible in history.
            analog = _analog_boundary_hold(plc, root, chain, ctx)
            if analog is None:
                continue
            hold, note = analog
            relation_note = f"; recompute installed owner: {note}"
        elif isinstance(root.value, bool):
            hold = (root.tag_name, not root.value)
            relation_note = ""
        else:
            # A wide word offers no complement, but the chain knows what the
            # stuck value does — flip the truth of the ordered comparison it
            # supports and propose the boundary value (replay-verified).
            analog = _analog_boundary_hold(plc, root, chain, ctx)
            if analog is None:
                continue  # no ordered comparison on the chain — still no sound value
            hold, note = analog
            relation_note = f"; {note}"
        if not _hold_allowed(ctx, hold):
            continue
        root_tags.add(root.tag_name)
        keyed.append(
            (
                len(root.via),
                InvestigationHypothesis(
                    kind="absence-root",
                    holds=(hold,),
                    sources=(root.tag_name,),
                    detail=(
                        f"{root.tag_name} held {root.value!r} "
                        f"{'by PILOT' if installed_owner else 'since cold'} "
                        f"on {dep.tag}'s deep cause chain [{root.kind}]{relation_note}"
                    ),
                ),
            )
        )
    # Deepest terminal first: the fault-generation side (a permissive buffered
    # behind an error register and an alarm chain) sits deeper in the chain
    # than a response-side gate on the abort rung itself, and the bounded
    # replay cannot distinguish them — both keep the channel in place.  The
    # hop-provenance length is the depth proxy; ties keep ranked_roots order.
    keyed.sort(key=lambda kv: -kv[0])
    return [h for _, h in keyed], frozenset(root_tags)


# ---------------------------------------------------------------------------
# Hypothesis generation — structural
#
# The enabler-correction families (latch-exposure FLIP + accumulator
# OSCILLATE/stop-hold) live in ``corrections.py`` behind ``correct_enablers``,
# the single ``no-steerable-trigger -> corrective hold`` pass.
# ---------------------------------------------------------------------------


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


def _dedupe_pairs(pairs: Iterable[ActionPair]) -> list[ActionPair]:
    out: list[ActionPair] = []
    seen: set[ActionPair] = set()
    for pair in pairs:
        if pair in seen:
            continue
        seen.add(pair)
        out.append(pair)
    return out


def _dedupe_hypotheses(
    hypotheses: list[InvestigationHypothesis],
) -> tuple[InvestigationHypothesis, ...]:
    out: list[InvestigationHypothesis] = []
    seen: set[tuple[ActionPair, ...]] = set()
    for hypothesis in hypotheses:
        key = hypothesis.holds
        if key in seen:
            continue
        seen.add(key)
        out.append(hypothesis)
    return tuple(out)
