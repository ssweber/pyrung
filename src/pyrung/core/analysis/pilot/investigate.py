"""Compose and confirm bounded corrective hypotheses.

``investigation_replay.py`` owns incident construction, replay evidence,
causal regression comparison, and excursion replay.
``corrections.py`` derives the hypothesis families; investigation ranks,
composes, and confirms them with the replay engine.
``refinement.py`` owns bounded relational counterexample refinement.
``correction_candidates.py`` owns candidate identity, ordering, composition,
materialization, and self-defeat checks.
``_resolve_replay_attempt`` either accepts, rejects, or composes a candidate;
bounded candidate composition always replays the composite from the original
checkpoint.  This is an orient-phase
optimization, not another PILOT iteration: it neither commits a world nor
installs a correction. A surviving exploratory result receives an
evidence-derived lifetime from ``correction_candidates.py`` and must survive a
guarded replay before the first confirmed composite is returned.

Departure investigation does not install a correction; installation belongs
to the orchestration/recovery owner.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

import pyrung.core.analysis.pilot.correction_candidates as _candidates
import pyrung.core.analysis.pilot.investigation_replay as _replay
import pyrung.core.analysis.pilot.refinement as _refinement
from pyrung.core.analysis.pilot.avoid import _hold_allowed as _hold_allowed
from pyrung.core.analysis.pilot.constrained_reachability import (
    NoRoute,
    Reachable,
)
from pyrung.core.analysis.pilot.correction_records import _ConfirmedCorrection
from pyrung.core.analysis.pilot.corrections import (
    CorrectionHypothesis,
    derive_correction_hypotheses,
    refine_relational_hypothesis,
)
from pyrung.core.analysis.pilot.incidents import DeviationIncident
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _pilot_rung_execution_receipt,
)
from pyrung.core.analysis.pilot.overlay import (
    _set_pilot_rungs as _set_pilot_rungs,
)
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
from pyrung.core.analysis.pilot.world_key import (
    _rung_identity,
    _semantic_key,
)
from pyrung.core.analysis.sp_values import (
    _values_match,
)

if TYPE_CHECKING:
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)

_MAX_CANDIDATE_COMPOSITIONS = 8

ActionPair = tuple[str, Any]


@dataclass(frozen=True)
class InvestigationRejection:
    """One rejected hypothesis and the exact ground that rejected it."""

    hypothesis: CorrectionHypothesis
    slug: str
    ground: str


@dataclass(frozen=True)
class _ReplayAccepted:
    """A replay attempt accepted without exposing another causal cut."""

    outcome: _replay.ReplayOutcome


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
    refinement: _refinement._RelationalRefinementReceipt


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
    outcome: _replay.ReplayOutcome,
    seen_replacements: set[tuple[Any, ...]],
    extend: Callable[
        [CorrectionHypothesis, _replay.ReplacementEvidence],
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


def _replacement_identity(replacement: _replay.ReplacementEvidence) -> tuple[Any, ...]:
    """Exact identity of one replacement cause inside a composition chain."""

    return tuple(
        (item.rung, item.tag, _semantic_key(item.value)) for item in replacement.witness.cause
    )


# ---------------------------------------------------------------------------
# Investigation engine
# ---------------------------------------------------------------------------


def investigate_deviation(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
    replay: _replay.ReplayFn,
    *,
    needed: Sequence[tuple[str, Any]] = (),
    installed_pilot_rungs: Sequence[Any] = (),
    correction_pilot_rungs: Sequence[Any] = (),
    correction_progress_mark: tuple[tuple[str, Any], ...] = (),
    occurrence_requirements: tuple[tuple[str, Any], ...] = (),
    excluded_corrections: frozenset[_candidates.CorrectionIdentity] = frozenset(),
    regression_witness: _replay.RegressionWitness | None = None,
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
        local = tuple(_candidates._rank_hypotheses(plc, local, incident))
        local_ids = {_candidates._hypothesis_identity(item.holds) for item in local}
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
            for item in _candidates._rank_hypotheses(plc, transition_local, incident)
            if _candidates._hypothesis_identity(item.holds) not in local_ids
        )
        local_ids.update(_candidates._hypothesis_identity(item.holds) for item in transition_local)
        yield from transition_local

        produced, absence_tags = derive_correction_hypotheses(
            plc,
            incident,
            ctx,
            installed=correction_active,
            causal_spine=causal_spine,
        )
        for item in _candidates._rank_hypotheses(
            plc,
            produced,
            incident,
            primal_extra=absence_tags,
        ):
            if _candidates._hypothesis_identity(item.holds) not in local_ids:
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
        evidence: _replay.ReplacementEvidence,
    ) -> CorrectionHypothesis | None:
        """Derive the next cut from the retained fork and add it to *current*."""
        nested_raw, nested_absence = derive_correction_hypotheses(
            evidence.plc,
            evidence.incident,
            ctx,
            causal_spine=evidence.witness.causal_spine,
        )
        nested = _candidates._rank_hypotheses(
            evidence.plc,
            nested_raw,
            evidence.incident,
            primal_extra=nested_absence,
        )
        for candidate in nested:
            identity = _candidates._hypothesis_identity(candidate.holds)
            equivalent = next(
                (
                    known
                    for known in observed_hypotheses
                    if _candidates._hypothesis_identity(known.holds) == identity
                ),
                None,
            )
            chosen = equivalent or candidate
            if equivalent is None:
                observed_hypotheses.append(candidate)
            composite = _candidates._compose_hypotheses(current, chosen)
            if composite is not None:
                return _candidates._reprove_composite_producer_envelope(
                    composite,
                    ctx,
                    incident.channel_tag,
                )
        return None

    def _replay_candidate(
        hypothesis: CorrectionHypothesis,
        holds: tuple[Any, ...],
    ) -> _replay.ReplayOutcome:
        """Use the staged continuation probe only when refinement consumes it."""
        with_continuation = getattr(replay, "with_continuation", None)
        if hypothesis.constraint is not None and with_continuation is not None:
            return with_continuation(holds)
        return replay(holds)

    for hypothesis in hypotheses:
        if not any(
            _candidates._hypothesis_identity(known.holds)
            == _candidates._hypothesis_identity(hypothesis.holds)
            for known in observed_hypotheses
        ):
            observed_hypotheses.append(hypothesis)
        if not hypothesis.holds:
            _reject(hypothesis, "no-holds", "no holds proposed")
            continue
        # A hypothesis made entirely of PilotRungs already owns its executable
        # scope.  Raw pairs acquire one only after their exploratory replay.
        if all(isinstance(hold, PilotRung) for hold in hypothesis.holds) and (
            _candidates.correction_identity(hypothesis.holds) in excluded_corrections
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
            for ht, hv in map(_candidates._proposal_pair, hypothesis.holds)
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
                and _candidates._hold_is_noop(
                    ht,
                    hv,
                    incident.before_snap,
                    pdg,
                    program,
                    recorded_incident_movers,
                    incident.after_snap,
                    installed_pilot_rungs,
                )
                for ht, hv in map(_candidates._proposal_pair, hypothesis.holds)
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
                outcome: _replay.ReplayOutcome,
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
                exploratory = _candidates._exploratory_correction_rungs(
                    plc,
                    exploratory_holds,
                    incident,
                    correction_progress_mark,
                    ctx,
                )
                preflight = _candidates._continuation_with_active_correction(
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
                    refined, ground = _refinement._refine_unknown_continuation(
                        current,
                        outcome,
                        ctx,
                        refinement_receipt,
                        refiner=refine_relational_hypothesis,
                        identity=_candidates._hypothesis_identity,
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
                    if _candidates._discharges_occurrence_requirements(
                        current.holds,
                        occurrence_requirements,
                    )
                    else correction_progress_mark
                )
                scoped = _candidates._scoped_correction_rungs(
                    plc,
                    current.holds,
                    incident,
                    outcome,
                    ctx,
                    scoped_progress_mark,
                    current.producer_envelope,
                    neutralized=(outcome.justification is _replay.ReplayJustification.NEUTRALIZED),
                )

                def _fall_back_from_envelope() -> bool:
                    nonlocal current
                    fallback = _fallback_candidate()
                    if fallback is None:
                        return False
                    current = fallback.hypothesis
                    return True

                if _candidates.correction_identity(scoped) in excluded_corrections:
                    if _fall_back_from_envelope():
                        continue
                    return Reject(
                        InvestigationRejection(
                            current,
                            "correction-revoked",
                            "correction was previously revoked after causing a later regression",
                        )
                    )
                scoped_preflight = _candidates._continuation_with_active_correction(
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
                    and _candidates._active_pilot_rungs_defeat_needed(
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
                ) and _candidates.correction_identity(scoped) == _candidates.correction_identity(
                    exploratory
                )
                installed_outcome = (
                    outcome if same_executable else _replay_candidate(current, scoped)
                )
                if current.constraint is not None and not isinstance(
                    installed_outcome.continuation,
                    Reachable,
                ):
                    refined, ground = _refinement._refine_unknown_continuation(
                        current,
                        installed_outcome,
                        ctx,
                        refinement_receipt,
                        refiner=refine_relational_hypothesis,
                        identity=_candidates._hypothesis_identity,
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
                    identity=_candidates.correction_identity(scoped),
                    pilot_rungs=scoped,
                    sources=confirmed_hypothesis.sources,
                    justification=(
                        installed_justification
                        if isinstance(installed_outcome.continuation, Reachable)
                        else (
                            "legacy-local-replay; target continuation unknown: "
                            f"{_refinement._continuation_ground(installed_outcome.continuation)}; "
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
                _refinement._RelationalRefinementReceipt(),
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
