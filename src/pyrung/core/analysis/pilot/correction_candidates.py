"""Select and materialize correction candidates for bounded recovery.

This module owns proposal identity, hypothesis ordering and composition,
evidence-derived PilotRung scopes, and static self-defeat checks. It reads
recorded or replay-produced evidence but never executes a replay, drives PILOT,
installs a correction, or owns a recovery transaction.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any, Protocol

from pyrung.core.analysis.pilot.candidate_policy import _holds_defeat_needed
from pyrung.core.analysis.pilot.causal import chase_chain_tags
from pyrung.core.analysis.pilot.constrained_reachability import (
    FrontierStatus,
    NoRoute,
    Unknown,
)
from pyrung.core.analysis.pilot.corrections import (
    CorrectionHypothesis,
    producer_envelope_correction_holds,
)
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _pilot_rung_execution_receipt,
    _pilot_rungs_from_proposals,
    _target_unresolved_condition,
    _union_conditions,
)
from pyrung.core.analysis.pilot.route_judgment import route_has_no_dead_end
from pyrung.core.analysis.pilot.trace import trace_back
from pyrung.core.analysis.pilot.trace_read import UnsupportedConstruct
from pyrung.core.analysis.pilot.trace_routes import enumerate_trace_choices
from pyrung.core.analysis.pilot.world_key import _rung_identity, _semantic_key
from pyrung.core.analysis.sp_values import _values_match

ActionPair = tuple[str, Any]
CorrectionIdentity = tuple[tuple[Any, ...], ...]


class _DepartureEvidence(Protocol):
    @property
    def tag(self) -> str: ...

    @property
    def scan(self) -> int | None: ...


class CandidateIncident(Protocol):
    """Incident evidence consumed by candidate decisions."""

    @property
    def anchor_scan(self) -> int: ...

    @property
    def end_scan(self) -> int: ...

    @property
    def channel_tag(self) -> str | None: ...

    @property
    def departures(self) -> Sequence[_DepartureEvidence]: ...

    @property
    def before_snap(self) -> Mapping[str, Any]: ...

    @property
    def occurrence_conditions(self) -> Sequence[Any]: ...


class CandidateOutcome(Protocol):
    """Replay landing evidence consumed only for correction scoping."""

    @property
    def snapshot(self) -> Mapping[str, Any]: ...

    @property
    def landed(self) -> bool: ...


class UnsupportedOccurrenceScope(RuntimeError):
    """A retained writer condition cannot be projected without widening it."""


def _proposal_pair(proposal: Any) -> ActionPair:
    if isinstance(proposal, PilotRung):
        return proposal.dest, proposal.value
    return proposal


def _proposal_identity(proposal: Any) -> tuple[str, Any]:
    """Pre-install identity used only to compare generated hypotheses."""

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
    """Exact identity of an executable, replay-confirmed correction."""

    identities: list[tuple[Any, ...]] = []
    for rung in pilot_rungs:
        if not isinstance(rung, PilotRung):
            raise TypeError("correction identity requires executable PilotRungs")
        identities.append(_rung_identity(rung))
    return tuple(sorted(identities, key=repr))


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
    """Whether a proposed hold changes no executable writer behavior."""

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
    for rung_index in pdg.writers_of.get(tag, frozenset()):
        rung = resolve_rung(program, pdg.rung_nodes[rung_index])
        if rung is None:
            return False
        literal = _literal_write(rung, tag)
        if literal is None or not _values_match(literal, value):
            return False
    return True


def _last_transition_scan(plc: Any, tag: str, start_scan: int, end_scan: int) -> int | None:
    """Return the latest recorded scan in the bounded window where *tag* changed."""

    try:
        states = plc.history.range(start_scan, end_scan + 1)
    except Exception:  # noqa: BLE001
        return None
    last: int | None = None
    for previous, current in zip(states, states[1:], strict=False):
        if not _values_match(previous.tags.get(tag), current.tags.get(tag)):
            last = current.scan_id
    return last


def _rank_hypotheses(
    plc: Any,
    hypotheses: Sequence[CorrectionHypothesis],
    incident: CandidateIncident,
    primal_extra: frozenset[str] = frozenset(),
) -> list[CorrectionHypothesis]:
    """Order candidates by causal primacy, temporal proximity, and scope size."""

    channel = incident.channel_tag
    departure_scans = {
        departure.tag: departure.scan
        for departure in incident.departures
        if departure.scan is not None
    }
    primal: set[str] = set()
    channel_scan = incident.end_scan
    if channel is not None:
        departure_scan = departure_scans.get(channel)
        if departure_scan is not None:
            channel_scan = departure_scan
        primal = {channel} | chase_chain_tags(
            plc,
            channel,
            scan=departure_scans.get(channel),
        )
    primal |= primal_extra

    unreachable = 1 << 30

    def proximity(tags: set[str]) -> int:
        best = unreachable
        for tag in tags:
            last = _last_transition_scan(plc, tag, incident.anchor_scan, channel_scan)
            if last is not None:
                best = min(best, channel_scan - last)
        return best

    def key(
        pair: tuple[int, CorrectionHypothesis],
    ) -> tuple[int, int, int, int, int, int]:
        index, hypothesis = pair
        tags = set(hypothesis.sources) | {
            _proposal_pair(proposal)[0] for proposal in hypothesis.holds
        }
        in_chain = 0 if (primal and tags & primal) else 1
        incident_specific = (
            0 if hypothesis.kind == "latch-exposure" or hypothesis.incident_local else 1
        )
        if hypothesis.kind == "absence-root":
            family = 0 if hypothesis.history_origin == "external" else 2
        elif hypothesis.kind == "precise-cause":
            family = 1
        else:
            family = 3
        return (
            in_chain,
            incident_specific,
            family,
            proximity(tags),
            len(hypothesis.holds),
            index,
        )

    return [hypothesis for _, hypothesis in sorted(enumerate(hypotheses), key=key)]


def _compose_hypotheses(
    base: CorrectionHypothesis,
    addition: CorrectionHypothesis,
) -> CorrectionHypothesis | None:
    """Create a newly replayable union, declining contradictory operations."""

    base_holds = base.fallback_holds or base.holds
    addition_holds = addition.fallback_holds or addition.holds
    holds = list(base_holds)
    seen = {_proposal_identity(hold) for hold in holds}
    by_destination = {destination: value for destination, value in map(_proposal_pair, holds)}
    for hold in addition_holds:
        destination, value = _proposal_pair(hold)
        if destination in by_destination and not _values_match(by_destination[destination], value):
            return None
        identity = _proposal_identity(hold)
        if identity not in seen:
            seen.add(identity)
            holds.append(hold)
            by_destination.setdefault(destination, value)
    if len(holds) == len(base_holds):
        return None

    producer_cuts = list(base.producer_cuts)
    seen_cuts = {
        (hold[0], _semantic_key(hold[1]), tag, _semantic_key(value))
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
    """Promote an exact nested closure only after a fresh coordinated proof."""

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


def _retained_occurrence_scope(
    proposals: tuple[Any, ...],
    incident: CandidateIncident,
) -> Any:
    """Project corrected direct conjuncts out of an observed writer guard."""

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
    plc: Any,
    proposals: tuple[Any, ...],
    occurrence_scope: Any,
    ctx: Any,
) -> tuple[PilotRung, ...]:
    """Start at an exact retained occurrence and self-continue to the target."""

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
        destination = plc._known_tags_by_name.get(tag)
        if destination is None:
            raise KeyError(f"retained correction tag {tag!r} is not a program tag")
        continuation = CompareEq(destination, value)
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
    """Whether proposals directly falsify every recorded support demand."""

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
    plc: Any,
    proposals: tuple[Any, ...],
    incident: CandidateIncident,
    progress_mark: tuple[tuple[str, Any], ...],
    ctx: Any,
) -> tuple[Any, ...]:
    """Materialize a raw correction only where its incident was observed."""

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


def _scoped_correction_rungs(
    plc: Any,
    proposals: tuple[Any, ...],
    incident: CandidateIncident,
    outcome: CandidateOutcome,
    ctx: Any,
    progress_mark: tuple[tuple[str, Any], ...] = (),
    producer_envelope: bool = False,
    *,
    neutralized: bool = False,
) -> tuple[PilotRung, ...]:
    """Give a replay-successful correction its evidence-derived lifetime."""

    if incident.occurrence_conditions:
        occurrence_scope = _retained_occurrence_scope(proposals, incident)
        return _retained_scoped_rungs(plc, proposals, occurrence_scope, ctx)

    if all(
        isinstance(proposal, PilotRung) and proposal.operation is not None for proposal in proposals
    ):
        return tuple(proposals)

    if producer_envelope and all(isinstance(proposal, PilotRung) for proposal in proposals):
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
        and (neutralized or outcome.landed)
    ):
        from pyrung.core.condition import CompareEq, CompareNe

        before = incident.before_snap.get(channel_tag)
        if neutralized and not outcome.landed and exposure_guards:
            scope = _union_conditions((CompareEq(channel, before), *exposure_guards))
        else:
            landing = outcome.snapshot.get(channel_tag) if outcome.landed else before
            if outcome.landed and progress_mark and exposure_guards:
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
    if progress_mark:
        from pyrung.core.condition import AllCondition, CompareEq

        coordinates = []
        for tag_name, value in progress_mark:
            tag = plc._known_tags_by_name.get(tag_name)
            if tag is None:
                raise KeyError(f"progress receipt tag {tag_name!r} is not a program tag")
            coordinates.append(CompareEq(tag, value))
        scope = AllCondition(scope, *coordinates)
    return tuple(_pilot_rungs_from_proposals(list(scoped_proposals), scope))


def _active_pilot_rungs_defeat_needed(
    pilot_rungs: Sequence[PilotRung],
    needed: Sequence[tuple[str, Any]],
    snapshot: Mapping[str, Any],
    pdg: Any,
    program: Any,
) -> bool:
    """Whether effective correction writes provably pin a checkpoint need."""

    overlay = _pilot_rung_execution_receipt(pilot_rungs, snapshot)
    active = [(rung.dest, rung.value) for rung in overlay.effective]
    return _holds_defeat_needed(active, needed, pdg, program)


def _continuation_with_active_correction(
    pilot_rungs: Sequence[Any],
    snapshot: Mapping[str, Any],
    ctx: Any,
) -> FrontierStatus:
    """Classify static target continuation under an executable correction."""

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
            if not route_has_no_dead_end([tree]):
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
            return Unknown("target alternative enumeration exhausted its bound")
    if saw_complete:
        return NoRoute("active correction conflicts with every complete target trace")
    return Unknown("target continuation could not be classified")
