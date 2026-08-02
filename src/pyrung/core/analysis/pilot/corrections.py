"""Derive correction hypotheses from recorded departure evidence.

The producer emits absence-root, precise fired-chain, and enabler hypotheses in
that order. These are hypotheses, not installed corrections:
``correction_candidates.py`` ranks and materializes them, ``investigate.py``
replay-validates them, and ``progress.py`` installs at most one confirmed result.

Coil writers produce guard-breaking candidates; accumulating instructions ask
their owner for the operation that prevents or clears an unwanted completion.
Both enabler paths preserve the exposure-derived scope that can be proved from
program structure.

An accumulator correction asks only the owner that completed in the recorded
incident for its reset operation.  A plain trace handoff is a one-scan
operation; an intermediate instruction contributes its own boundary and
progress witness.  Later opposite operations compose as temporal phases when
their owner boundaries differ; bare contradictory holds still revoke.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot.advance import (
    AdvanceOwner,
    build_advance_index,
    iter_advance_owners,
)
from pyrung.core.analysis.pilot.avoid import _hold_allowed
from pyrung.core.analysis.pilot.causal import (
    _shared_cause,
    chase_cause_roots,
    chase_chain_tags,
    empirical_program_writes,
)
from pyrung.core.analysis.pilot.overlay import (
    OperationReceipt,
    PilotRung,
    _union_conditions,
    _until_unresolved_condition,
)
from pyrung.core.analysis.pilot.trace import (
    TraceAction,
    UnsupportedConstruct,
    _constraint_atom,
    _inequality_levers,
    trace_back,
)
from pyrung.core.analysis.pilot.types import BearingDeparture
from pyrung.core.analysis.pilot.world_key import _semantic_key
from pyrung.core.analysis.sp_values import _values_match, _writer_for_tag
from pyrung.core.crossing import AffineCmp, Cmp, Eq

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.types import DeviationIncident
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)

# Local alias — corrections imports nothing at runtime from investigate, so the
# investigate <-> corrections dependency stays one-directional (investigate
# depends on this module, never the reverse).
ActionPair = tuple[str, Any]


@dataclass(frozen=True)
class CorrectionHypothesis:
    """One replay-testable explanation for a recorded incident.

    Executable shape belongs to each hold's structural operation receipt, not
    to a behavior-category enum.
    """

    kind: str
    holds: tuple[Any, ...]
    sources: tuple[str, ...] = ()
    detail: str = ""
    # Relational condition this correction keeps false/true.  Unlike ``kind``,
    # this is executable analysis evidence: investigation can re-solve it
    # against a later counterexample snapshot without knowing which instruction
    # produced it.
    constraint: Any = None
    # True when the proposal is obtained from a transition that occurred in
    # this bounded incident, rather than inferred from older steady history.
    # This is evidence provenance, not a presentation/category distinction:
    # investigation uses it to exhaust exact occurrence evidence before it
    # asks the broader held-since question.
    incident_local: bool = False
    # For steady-history roots, retain the recorder's structural origin. An
    # external terminal and an unwritten default are both absence evidence,
    # but the former is a causal leaf while the latter is a broad fallback.
    history_origin: str | None = None
    # The executable guards already name every channel-pipeline producer that
    # this correction structurally disables.  Investigation may therefore
    # replay that producer envelope without intersecting it with the incidental
    # EarnedWork coordinate where the first occurrence was observed.
    producer_envelope: bool = False
    # Exact incident-local form proved before a producer-envelope widening.
    # If the broader guarded replay finds an escape hatch, investigation falls
    # back to this form rather than discarding a valid correction.
    fallback_holds: tuple[Any, ...] = ()
    # Structural provenance for re-proving a nested causal closure. Each item
    # maps one physical hold to the image/contact assignment that makes a
    # recorded producer non-conductive: ``((hold_tag, hold_value), tag, value)``.
    # A composite may combine these assignments, but the resulting executable
    # scopes remain individual PilotRungs.
    producer_cuts: tuple[tuple[ActionPair, str, Any], ...] = ()
    producer_sources: tuple[str, ...] = ()
    producer_causal_spine: frozenset[str] = frozenset()


def _complement_constraint(constraint: Any) -> Any | None:
    """Logical complement of one scalar owner boundary."""

    complements = {
        "==": "!=",
        "!=": "==",
        "<": ">=",
        "<=": ">",
        ">": "<=",
        ">=": "<",
    }
    if isinstance(constraint, Cmp):
        op = complements.get(constraint.op)
        return (
            None
            if op is None
            else Cmp(
                constraint.tag,
                op,
                constraint.bound,
                bound_is_tag=constraint.bound_is_tag,
            )
        )
    if isinstance(constraint, AffineCmp):
        op = complements.get(constraint.op)
        return (
            None
            if op is None
            else AffineCmp(
                constraint.tag,
                op,
                constraint.bound_tag,
                scale=constraint.scale,
                offset=constraint.offset,
            )
        )
    return None


def _authoritative_relational_holds(
    constraint: Any,
    snapshot: Mapping[str, Any],
    ctx: Any,
) -> tuple[ActionPair, ...]:
    """Direct authoritative operands that satisfy a relational constraint."""

    atom = _constraint_atom(constraint)
    if atom is None:
        return ()
    levers = _inequality_levers(
        atom,
        dict(snapshot),
        getattr(ctx, "steerable", frozenset()),
        ctx.pdg,
        getattr(ctx, "domain_prior", None),
        ctx.program,
    )
    return tuple(
        (lever.tag, lever.value)
        for lever in levers
        if lever.tag in getattr(ctx, "steerable", frozenset())
    )


def refine_relational_hypothesis(
    hypothesis: CorrectionHypothesis,
    snapshot: Mapping[str, Any],
    ctx: Any,
) -> CorrectionHypothesis | None:
    """Re-solve one relational correction at a counterexample snapshot."""

    if hypothesis.constraint is None:
        return None
    candidates = _authoritative_relational_holds(hypothesis.constraint, snapshot, ctx)
    if not candidates:
        return None
    prior = (
        dict(map(lambda hold: (hold.dest, hold.value), hypothesis.holds))
        if all(isinstance(hold, PilotRung) for hold in hypothesis.holds)
        else dict(hypothesis.holds)
    )
    for pair in candidates:
        if pair[0] not in prior or not _values_match(prior[pair[0]], pair[1]):
            return replace(
                hypothesis,
                holds=(pair,),
                sources=tuple(dict.fromkeys((*hypothesis.sources, pair[0]))),
                detail=f"{hypothesis.detail}; refined at {pair[0]}={pair[1]!r}",
            )
    return None


def correct_enablers(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
    *,
    causal_spine: frozenset[str] = frozenset(),
) -> list[CorrectionHypothesis]:
    """The single ``no-steerable-trigger -> corrective hold`` pass.

    Runs both dispatch arms and returns their shared
    :class:`CorrectionHypothesis` stream. Coil corrections precede accumulator
    corrections, matching the latch-exposure → done-boundary ordering.
    """
    coil = _coil_corrections(plc, incident, ctx, causal_spine=causal_spine)
    accumulator = _accumulator_corrections(plc, incident, ctx)
    operation_inputs = {
        getattr(hold, "dest", hold[0] if isinstance(hold, tuple) else None)
        for correction in accumulator
        for hold in correction.holds
        if isinstance(hold, PilotRung) and hold.operation is not None
    }
    if operation_inputs:
        coil = [
            correction
            for correction in coil
            if not correction.holds
            or any(
                getattr(hold, "dest", hold[0] if isinstance(hold, tuple) else None)
                not in operation_inputs
                for hold in correction.holds
            )
        ]
    corrections = [*coil, *accumulator]
    return [correction for correction in corrections if correction.holds]


def derive_correction_hypotheses(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
    *,
    installed: Mapping[str, Any] | None = None,
    incident_local_only: bool = False,
    incident_transition_only: bool = False,
    causal_spine: frozenset[str] = frozenset(),
) -> tuple[tuple[CorrectionHypothesis, ...], frozenset[str]]:
    """Produce one incident's ordered, deduplicated correction hypotheses.

    Family order is absence roots, precise fired-chain cuts, then enabler
    corrections. Duplicate hold sets keep their first producer. The returned
    absence-root tags are causal evidence for investigation's ranking pass.
    """
    if incident_transition_only:
        precise = _precise_causes(plc, incident, ctx)
        return (
            _dedupe_hypotheses(hypothesis for hypothesis in precise if hypothesis.incident_local),
            frozenset(),
        )

    enablers = correct_enablers(plc, incident, ctx, causal_spine=causal_spine)
    incident_changes = set(getattr(incident, "changed_tags", ()))
    channel_tag = getattr(incident, "channel_tag", None)
    channel_chain: set[str] | None = None
    if channel_tag is not None:
        channel_scan = next(
            (
                departure.scan
                for departure in getattr(incident, "departures", ())
                if departure.tag == channel_tag and departure.scan is not None
            ),
            None,
        )
        channel_chain = {channel_tag, *chase_chain_tags(plc, channel_tag, scan=channel_scan)}
    incident_local = [
        hypothesis
        for hypothesis in enablers
        if (
            hypothesis.kind == "latch-exposure"
            or (
                hypothesis.incident_local
                and (channel_chain is None or bool(channel_chain.intersection(hypothesis.sources)))
            )
        )
        and incident_changes.intersection(hypothesis.sources)
    ]
    if incident_local_only:
        return _dedupe_hypotheses(incident_local), frozenset()
    # The full producer remains complete: after an exact local probe fails,
    # investigation may need a support from any older epoch. Laziness and
    # suppression belong to investigation's staged consumption, not to an
    # amputated hypothesis stream.
    absence_hypotheses, absence_tags = _absence_root_correctives(
        plc,
        incident,
        ctx,
        exclude=frozenset(tag for tag, _value in incident.action),
        installed=installed or {},
    )
    precise = _precise_causes(plc, incident, ctx)
    hypotheses = [*absence_hypotheses, *precise, *enablers]
    return _dedupe_hypotheses(hypotheses), absence_tags


# ---------------------------------------------------------------------------
# Exposure lifetime — exact conditions from the recorded deep cause
# ---------------------------------------------------------------------------


def causal_channel_guard(
    plc: PLC,
    source_tags: tuple[str, ...],
    incident: DeviationIncident,
    ctx: Any,
) -> Any | None:
    """Actual channel conditions on the deep chains that fired *source_tags*.

    A latch correction belongs to the state in which its latch rung actually
    fired.  The deep chain already expands state aliases such as
    ``Sts_State_Starting`` through the observed writer that established them.
    Resolve that chain step back to its rung and retain the rung's actual
    channel-reading condition (``Sts_StateCurrent == 3``), rather than
    synthesizing equality from a sampled value or widening to a replay
    source-to-landing corridor.
    """
    from pyrung.core.analysis.pdg import _extract_reads_from_condition, resolve_rung
    from pyrung.core.condition import AllCondition

    channel_name = incident.channel_tag
    pdg = getattr(ctx, "pdg", None)
    program = getattr(ctx, "program", None)
    if channel_name is None or pdg is None or program is None:
        return None

    terms: list[Any] = []
    seen_terms: set[tuple[Any, ...]] = set()
    for source in source_tags:
        source_scan = next(
            (
                event.scan
                for event in incident.timeline
                if any(
                    tag == source and before is not True and after is True
                    for tag, before, after in getattr(event, "transitions", ())
                )
            ),
            None,
        )
        if source_scan is None:
            try:
                states = plc._causal_history_range(
                    max(0, incident.anchor_scan - 1),
                    incident.end_scan + 1,
                )
            except Exception:  # noqa: BLE001
                states = ()
            source_scan = next(
                (
                    current.scan_id
                    for previous, current in zip(states, states[1:], strict=False)
                    if previous.tags.get(source) is not True and current.tags.get(source) is True
                ),
                None,
            )
            # A trial fork can retain history from the first post-action scan,
            # while the incident's anchor snapshot predates it.  In that
            # shape the first retained state already contains the latch.  The
            # endpoint evidence still brackets the activation exactly:
            # false at the incident anchor, true at this first recorded scan.
            if source_scan is None and incident.before_snap.get(source) is not True:
                source_scan = next(
                    (state.scan_id for state in states if state.tags.get(source) is True),
                    None,
                )
        if source_scan is None:
            logger.debug(
                "exact exposure: no activation scan for %s in [%s, %s]",
                source,
                incident.anchor_scan,
                incident.end_scan,
            )
            continue
        try:
            chain = plc.cause(source, scan=source_scan, deep=True)
        except Exception:  # noqa: BLE001
            logger.debug(
                "causal exposure: cause(%s@%s) raised",
                source,
                source_scan,
                exc_info=True,
            )
            continue
        if (
            chain is None
            or chain.effect.scan_id < incident.anchor_scan
            or chain.effect.scan_id > incident.end_scan
        ):
            logger.debug(
                "exact exposure: unusable chain for %s@%s (effect=%r, window=[%s, %s])",
                source,
                source_scan,
                None if chain is None else chain.effect,
                incident.anchor_scan,
                incident.end_scan,
            )
            continue
        for step in chain.steps:
            links = (*step.triggers, *step.enablers)
            if not any(link.tag_name == channel_name for link in links):
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
            conditions = [
                condition
                for condition in tuple(getattr(rung_obj, "_conditions", ()) or ())
                if channel_name in _extract_reads_from_condition(condition, {})
            ]
            if not conditions:
                continue
            # Two rungs can carry the same channel context (``StateCurrent == 3``
            # from Starting's mapper and from its own hold).  Conditions compare
            # by object identity, so keying on ``id`` admits both and the guard
            # renders a duplicated disjunct.  ``_semantic_key`` is the search
            # identity for exactly this.
            term_key = tuple(_semantic_key(condition) for condition in conditions)
            term = conditions[0] if len(conditions) == 1 else AllCondition(*conditions)
            if term_key not in seen_terms:
                seen_terms.add(term_key)
                terms.append(term)
            # ``chain.steps`` walks effect -> causes. The first rung whose
            # actual links read the channel is the conductive context that
            # admitted the undesired effect (for example Starting's
            # ``StateCurrent == 3`` mapper). Older channel-bearing steps explain
            # how that context was reached (Idle's ``StateCurrent == 4``); they
            # are ancestry, not additional correction lifetimes.
            break

    return _union_conditions(terms)


def guard_correction_holds(
    plc: PLC,
    holds: tuple[ActionPair, ...],
    source_tags: tuple[str, ...],
    incident: DeviationIncident,
    ctx: Any,
) -> tuple[Any, ...]:
    """Attach the recorded writer context to a correction when it is readable.

    Causal cuts and instruction-specific corrections are alternative ways to
    discover the same intervention.  Their lifetime belongs to the harmful
    writer occurrence, not to the hypothesis category that found it.
    """
    guard = causal_channel_guard(plc, source_tags, incident, ctx)
    if guard is None:
        return tuple(holds)
    return tuple(PilotRung(tag, value, guard) for tag, value in holds)


def _condition_conjunction(conditions: Iterable[Any]) -> Any | None:
    """Executable conjunction, with ``None`` representing logical True."""

    from pyrung.core.condition import AllCondition

    terms = tuple(conditions)
    if not terms:
        return None
    return terms[0] if len(terms) == 1 else AllCondition(*terms)


def _condition_disjunction(conditions: Iterable[Any]) -> Any | None:
    """Executable disjunction; callers handle an empty/True result explicitly."""

    from pyrung.core.condition import AnyCondition

    terms = tuple(conditions)
    if not terms:
        return None
    return terms[0] if len(terms) == 1 else AnyCondition(*terms)


def _producer_caller_scope(program: Any, subroutine: str) -> tuple[Any | None, bool, bool]:
    """Return ``(guard, reachable, complete)`` for a subroutine call path.

    ``guard is None`` with ``reachable`` means an unconditional path.  Raw call
    conditions are retained so the resulting scope is directly executable;
    recursive/unknown call structure declines the widening rather than
    manufacturing a partial outer guard.
    """

    from pyrung.core.condition import AllCondition, AnyCondition
    from pyrung.core.validation._common import _build_caller_map

    caller_map = _build_caller_map(program)
    memo: dict[str, tuple[Any | None, bool, bool]] = {}
    visiting: set[str] = set()

    def visit(name: str) -> tuple[Any | None, bool, bool]:
        if name in memo:
            return memo[name]
        if name in visiting:
            return None, False, False
        callers = caller_map.get(name, ())
        if not callers:
            result = (None, False, True)
            memo[name] = result
            return result
        visiting.add(name)
        alternatives: list[Any] = []
        unconditional = False
        for scope, caller_sub, _ri, _branch, conditions in callers:
            local = _condition_conjunction(conditions)
            if scope == "subroutine" and caller_sub is not None:
                parent, reachable, complete = visit(caller_sub)
                if not complete:
                    visiting.discard(name)
                    return None, False, False
                if not reachable:
                    continue
                if parent is None:
                    term = local
                elif local is None:
                    term = parent
                else:
                    term = AllCondition(parent, local)
            else:
                term = local
            if term is None:
                unconditional = True
                break
            alternatives.append(term)
        visiting.discard(name)
        if unconditional:
            result = (None, True, True)
        elif not alternatives:
            result = (None, False, True)
        else:
            result = (
                alternatives[0] if len(alternatives) == 1 else AnyCondition(*alternatives),
                True,
                True,
            )
        memo[name] = result
        return result

    return visit(subroutine)


@dataclass(frozen=True)
class _ProducerEnvelopeTerm:
    """One producer context and the corrective contacts needed to cut it."""

    guard: Any
    cut_tags: frozenset[str]
    writes: frozenset[str]


def _producer_envelope_terms(
    ctx: Any,
    channel_tag: str,
    cut_assignments: Mapping[str, Any],
    causal_spine: frozenset[str],
) -> tuple[_ProducerEnvelopeTerm, ...] | None:
    """Producer contexts cut by a coordinated corrective assignment.

    Starting from the image/contact values resolved by ``trace_back``, inspect
    only readers that write onto the recorded channel cascade. Each writer is
    assigned one deterministic inclusion-minimal corrective cut. Direct
    conjuncts owned wholly by that cut are projected out; every other local
    condition and recursive caller guard is retained.
    """

    from pyrung.core.analysis.pdg import _extract_reads_from_condition, resolve_rung
    from pyrung.core.analysis.simplified import _conditions_list_to_expr, _expr_forced_true
    from pyrung.core.condition import AllCondition

    pdg = getattr(ctx, "pdg", None)
    program = getattr(ctx, "program", None)
    if pdg is None or program is None or not cut_assignments or not causal_spine:
        return None

    # ``RegressionWitness.causal_spine`` is the exact cascade that produced the
    # observed channel departure. A whole static upstream cone is not a
    # substitute: shared status plumbing can pull in unrelated writers.
    pipeline = set(causal_spine) | {channel_tag}
    candidate_nodes = {
        node_index for tag in cut_assignments for node_index in pdg.readers_of.get(tag, frozenset())
    }
    terms: list[_ProducerEnvelopeTerm] = []
    seen_terms: set[tuple[Any, frozenset[str]]] = set()
    for node_index in sorted(candidate_nodes):
        node = pdg.rung_nodes[node_index]
        if not (set(node.writes) & pipeline):
            continue
        rung = resolve_rung(program, node)
        if rung is None:
            return None
        conditions = tuple(getattr(rung, "_conditions", ()) or ())
        expr = _conditions_list_to_expr(list(conditions))
        if _expr_forced_true(expr, dict(cut_assignments)) is not False:
            continue

        # The joint Execute producer ``~Door OR ~Lint`` needs both contacts;
        # a door-only alarm drops the irrelevant lint assignment. The proof is
        # coordinated, while the resulting executable scopes stay individual.
        minimal = dict(cut_assignments)
        for tag in sorted(tuple(minimal)):
            trial = {name: value for name, value in minimal.items() if name != tag}
            if _expr_forced_true(expr, trial) is False:
                minimal = trial
        cut_tags = frozenset(minimal)
        if not cut_tags:
            continue

        retained: list[Any] = []
        projected = False
        for condition in conditions:
            reads = set(_extract_reads_from_condition(condition, {}))
            overlap = reads & cut_tags
            if not overlap:
                retained.append(condition)
                continue
            if not reads <= cut_tags:
                # A nested expression mixing one lever with retained context
                # cannot be projected without reconstructing its Boolean form.
                return None
            projected = True
        if not projected:
            return None

        caller = None
        if node.subroutine is not None:
            caller, reachable, complete = _producer_caller_scope(program, node.subroutine)
            if not complete:
                return None
            if not reachable:
                continue
        local = _condition_conjunction(retained)
        if caller is None:
            term = local
        elif local is None:
            term = caller
        else:
            term = AllCondition(caller, local)
        # A globally-active envelope has no principled release boundary.
        if term is None:
            return None
        key = (_semantic_key(term), cut_tags)
        if key not in seen_terms:
            seen_terms.add(key)
            terms.append(_ProducerEnvelopeTerm(term, cut_tags, frozenset(node.writes)))
    return tuple(terms)


def _producer_envelope_guard(
    ctx: Any,
    channel_tag: str,
    cut_assignments: Mapping[str, Any],
    required_sources: tuple[str, ...],
    causal_spine: frozenset[str],
) -> Any | None:
    """Complete shared scope for compatibility with single-scope consumers."""

    terms = _producer_envelope_terms(ctx, channel_tag, cut_assignments, causal_spine)
    if terms is None:
        return None
    covered_sources = set().union(*(term.writes for term in terms)) if terms else set()
    if not set(required_sources) <= covered_sources:
        return None
    return _condition_disjunction(term.guard for term in terms)


def producer_envelope_correction_holds(
    holds: tuple[ActionPair, ...],
    source_tags: tuple[str, ...],
    ctx: Any,
    channel_tag: str,
    producer_cuts: tuple[tuple[ActionPair, str, Any], ...],
    causal_spine: frozenset[str],
) -> tuple[PilotRung, ...]:
    """Re-prove individual hold scopes from one coordinated producer cut."""

    assignments: dict[str, Any] = {}
    for _hold, tag, value in producer_cuts:
        if tag in assignments and not _values_match(assignments[tag], value):
            return ()
        assignments[tag] = value
    if not assignments:
        return ()

    terms = _producer_envelope_terms(ctx, channel_tag, assignments, causal_spine)
    if terms is None:
        return ()
    covered_sources = set().union(*(term.writes for term in terms)) if terms else set()
    if not set(source_tags) <= covered_sources:
        return ()

    widened: list[PilotRung] = []
    for hold in holds:
        owned_tags = {
            tag
            for owned_hold, tag, _value in producer_cuts
            if owned_hold[0] == hold[0] and _values_match(owned_hold[1], hold[1])
        }
        guard = _condition_disjunction(term.guard for term in terms if term.cut_tags & owned_tags)
        if guard is None:
            return ()
        widened.append(PilotRung(hold[0], hold[1], guard))
    return tuple(widened)


def producer_scoped_correction_holds(
    plc: PLC,
    holds: tuple[ActionPair, ...],
    source_tags: tuple[str, ...],
    incident: DeviationIncident,
    ctx: Any,
    cut_assignments: Mapping[str, Any],
    causal_spine: frozenset[str],
    producer_cuts: tuple[tuple[ActionPair, str, Any], ...] = (),
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """Prefer a complete producer envelope, falling back to exact exposure."""

    fallback = guard_correction_holds(plc, holds, source_tags, incident, ctx)
    if incident.channel_tag is None:
        return fallback, ()
    if producer_cuts:
        widened = producer_envelope_correction_holds(
            holds,
            source_tags,
            ctx,
            incident.channel_tag,
            producer_cuts,
            causal_spine,
        )
    else:
        guard = _producer_envelope_guard(
            ctx,
            incident.channel_tag,
            cut_assignments,
            source_tags,
            causal_spine,
        )
        widened = (
            tuple(PilotRung(tag, value, guard) for tag, value in holds) if guard is not None else ()
        )
    if not widened:
        return fallback, ()
    return widened, fallback


# ---------------------------------------------------------------------------
# Coil arm — latches that fired during the incident  (FLIP a non-state guard)
# ---------------------------------------------------------------------------


def _coil_corrections(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
    *,
    causal_spine: frozenset[str] = frozenset(),
) -> list[CorrectionHypothesis]:
    """Latch-exposure: alarm latches that fired as a consequence of our action.

    The durable evidence is the latch edge itself: false before this incident,
    true afterward. Requiring its state guard to have been true in the *before*
    snapshot misses transition-state safety checks (Held -> Unholding -> Alarm),
    because the guard exists only inside the incident. Each fired latch's
    non-channel guard inputs are preconditions we failed to establish; flip each
    to the value that breaks the latch and resolve it to its steerable driver via
    ``trace_back`` (including input-image coils such as physical Door -> i_Door).

    The holds are proposed both per-latch *and* as one conjunction: when several
    alarms fire together (door AND lint), no single hold reaches the bearing —
    only clearing every active latch does.
    """
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.instruction.coils import LatchInstruction

    pdg = getattr(ctx, "pdg", None)
    steerable = getattr(ctx, "steerable", frozenset())
    opaque_loop = getattr(ctx, "opaque_loop", frozenset())
    program = getattr(ctx, "program", None)
    if pdg is None or program is None:
        return []
    pipeline_internal = getattr(ctx, "pipeline_internal_tags", frozenset())
    route = getattr(ctx, "route", None)

    def _steerable_holds(guard: str, safe: Any) -> list[ActionPair]:
        """Resolve guard=safe to (steerable_input, value) holds."""
        if guard in steerable:
            return [(guard, safe)]
        try:
            tree = trace_back(
                guard,
                safe,
                dict(incident.after_snap),
                pdg,
                program,
                steerable,
                clear_only=getattr(ctx, "clear_only", frozenset()),
                opaque_loop=opaque_loop,
                pipeline_internal_tags=pipeline_internal,
                route=route,
                prior=getattr(ctx, "domain_prior", None),
            )
        except UnsupportedConstruct:
            raise
        except Exception:  # noqa: BLE001
            return []
        return list(tree.steerable_leaves())

    def _latch_guard_holds(tag: str) -> list[tuple[ActionPair, str, Any]]:
        """Corrective steerable holds for an active latch *tag*, or [].

        The trace may bridge an image-level contact such as
        ``i_DoorClosed`` to its physical ``x_DoorClosed`` lever.
        """
        from pyrung.core.analysis.simplified import _conditions_list_to_expr, _expr_forced_true

        holds: list[tuple[ActionPair, str, Any]] = []
        seen: set[ActionPair] = set()
        for ri in pdg.writers_of.get(tag, frozenset()):
            node = pdg.rung_nodes[ri]
            ro = resolve_rung(program, node)
            if ro is None or not any(isinstance(i, LatchInstruction) for i in ro._instructions):
                continue
            # The PDG node's condition_reads is subroutine-aware; the resolved
            # rung's sp_tree() has no tag-name accessor.  The safe value per
            # guard is computed exactly: the value that three-valued-FORCES the
            # latch rung's condition false regardless of the other reads
            # (``i_DoorClosed=True`` kills the door rung; a watchdog
            # ``Done=False`` kills the alarm rung).  Flipping off the guard's
            # ``after_snap`` value is insufficient for a
            # fire-then-reset guard: the Done bit that latched the alarm has
            # already reset by the after snapshot, so the flip proposed the
            # latch-CAUSING polarity and the hypothesis silently vanished.
            # When no forcing value exists (guard absent from the resolved
            # expression), flip the guard's current value and let replay judge
            # the proposal.
            expr = _conditions_list_to_expr(getattr(ro, "_conditions", []))
            condition_tags = set(node.condition_reads)
            state_tags = condition_tags & opaque_loop
            for guard in sorted(condition_tags - state_tags):
                cur = incident.after_snap.get(guard)
                if not isinstance(cur, bool):
                    continue
                safe = next(
                    (v for v in (False, True) if _expr_forced_true(expr, {guard: v}) is False),
                    not cur,
                )
                resolved = [
                    hold
                    for hold in _steerable_holds(guard, safe)
                    if hold not in seen and _hold_allowed(ctx, hold)
                ]
                for hold in resolved:
                    seen.add(hold)
                    holds.append((hold, guard, safe))
        return holds

    def _guarded(
        holds: list[ActionPair],
        source_tags: tuple[str, ...],
        cut_assignments: Mapping[str, Any],
        producer_cuts: tuple[tuple[ActionPair, str, Any], ...],
    ) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        """Wrap holds in the complete cut envelope when structurally provable."""
        return producer_scoped_correction_holds(
            plc,
            tuple(holds),
            source_tags,
            incident,
            ctx,
            cut_assignments,
            causal_spine,
            producer_cuts,
        )

    corrections: list[CorrectionHypothesis] = []
    conj_latches: list[str] = []
    latch_alternatives: list[tuple[tuple[ActionPair, str, Any], ...]] = []
    for tag, val in sorted(incident.after_snap.items()):
        if val is not True or incident.before_snap.get(tag) is True:
            continue
        latch_holds = _latch_guard_holds(tag)
        if not latch_holds:
            continue
        # Each guard assignment above independently forces this latch rung
        # false. They are alternative minimal cuts, not one coordinated hold.
        # Coordination belongs between distinct latches that all fired.
        for hold, guard_tag, safe in latch_holds:
            cuts = ((hold, guard_tag, safe),)
            guarded_holds, fallback = _guarded(
                [hold],
                (tag,),
                {guard_tag: safe},
                cuts,
            )
            corrections.append(
                CorrectionHypothesis(
                    kind="latch-exposure",
                    holds=guarded_holds,
                    sources=(tag, hold[0]),
                    detail=f"prevent latch {tag} via {hold[0]}",
                    incident_local=True,
                    producer_envelope=bool(fallback),
                    fallback_holds=fallback,
                    producer_cuts=cuts,
                    producer_sources=(tag,),
                    producer_causal_spine=causal_spine,
                )
            )
        conj_latches.append(tag)
        latch_alternatives.append(tuple(latch_holds))

    if len(latch_alternatives) > 1:
        # Each latch may admit alternative minimal cuts (e.g. close Door *or*
        # leave Starting). A coordinated repair chooses one cut per latch; it
        # must not union every alternative into an over-constrained batch.
        seen_joint: set[tuple[ActionPair, ...]] = set()
        joint_candidates: list[tuple[tuple[ActionPair, str, Any], ...]] = []
        for selected in itertools.product(*latch_alternatives):
            conjunction = tuple(dict.fromkeys(item[0] for item in selected))
            if conjunction in seen_joint:
                continue
            seen_joint.add(conjunction)
            joint_candidates.append(selected)
        minimal_joint = [
            candidate
            for candidate in joint_candidates
            if not any(
                {item[0] for item in other} < {item[0] for item in candidate}
                for other in joint_candidates
            )
        ]
        for selected in minimal_joint:
            conjunction = tuple(dict.fromkeys(item[0] for item in selected))
            assignments = {guard: safe for _hold, guard, safe in selected}
            guarded_holds, fallback = _guarded(
                list(conjunction),
                tuple(conj_latches),
                assignments,
                selected,
            )
            corrections.append(
                CorrectionHypothesis(
                    kind="latch-exposure",
                    holds=guarded_holds,
                    sources=(*conj_latches, *(h[0] for h in conjunction)),
                    detail=(f"clear {len(conj_latches)} active latches: {', '.join(conj_latches)}"),
                    incident_local=True,
                    producer_envelope=bool(fallback),
                    fallback_holds=fallback,
                    producer_cuts=selected,
                    producer_sources=tuple(conj_latches),
                    producer_causal_spine=causal_spine,
                )
            )
    return corrections


# ---------------------------------------------------------------------------
# Accumulator arm — owner operations that completed during coast
# ---------------------------------------------------------------------------


def _accumulator_corrections(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
) -> list[CorrectionHypothesis]:
    """Derive corrections for accumulator completion during a coast.

    While PILOT coasts, an accumulating instruction (timer/counter) can *complete
    on its own* and eject the bearing — its ``Done`` bit rises, or a rung fires on
    ``Acc > Target``.  The held input *driving* the accumulator is the cause.
    Three structural cases, all replay-validated:

    * **Resettable owner** — trace the owner's reset demand to a steerable
      operation without flattening its intermediate boundary. A direct contact
      is a one-scan operation; a conditioned contact carries its timer boundary
      and progress receipt.
    * **Plain held-advance -> Done** — no input-driven reset (or a counter hitting
      ``preset``): the advancing input must not *stay held*.  Emit a steady hold
      driving it off the advancing value.
    * **``Acc > Target`` threshold** — a bearing fact departed because the
      accumulator crossed a comparison threshold; ``trace_back`` surfaces the
      accumulator as a self-advancing leaf.  Stop holding whatever advances it.
    """
    from pyrung.core.analysis.pdg import _extract_reads_from_condition

    pdg = getattr(ctx, "pdg", None)
    program = getattr(ctx, "program", None)
    if pdg is None or program is None:
        return []

    changed = set(incident.changed_tags)
    after = dict(incident.after_snap)
    corrections: list[CorrectionHypothesis] = []

    # --- Sub-case A: execute the recorded owner's reset operation ---
    operation_inputs: set[str] = set()
    owners = tuple(iter_advance_owners(program))
    for owner in owners:
        profile = owner.profile
        if profile.done is None or profile.done.name not in changed:
            continue
        # Ask only the owner that actually completed in this incident how its
        # asserted Done bit clears. A later opposite fault is a new recorded
        # occurrence whose operation can compose with this one; it is not a
        # license to invent an oscillator pre-emptively.
        clear_snapshot = {**after, profile.done.name: True}
        clear_step = profile.plan(
            Eq(profile.done.name, frozenset((False,))),
            clear_snapshot,
        )
        reset_demands = (
            (*clear_step.holds, *((clear_step.pulse,) if clear_step.pulse is not None else ()))
            if clear_step is not None
            else ()
        )
        if len(reset_demands) != 1:
            continue
        reset_demand = reset_demands[0]
        forward_snapshot = {**after, profile.done.name: False}
        forward_step = profile.plan(
            Eq(profile.done.name, frozenset((True,))),
            forward_snapshot,
        )
        forward_demands = (
            (
                *forward_step.holds,
                *((forward_step.pulse,) if forward_step.pulse is not None else ()),
            )
            if forward_step is not None
            else ()
        )
        if len(forward_demands) == 1 and forward_demands[0].condition is reset_demand.condition:
            # Ordinary enable-off settlement is the inverse of advancement,
            # not an independent watchdog reset.
            continue
        reset = reset_demand.condition
        reset_reads = _extract_reads_from_condition(reset, {})
        if not reset_reads:
            continue
        # Minimal coordinated driver operations that satisfy the reset. The
        # TraceAction retains any intermediate timer owner instead of reducing
        # it to a bare physical value.
        reset_actions = _best_forcing_actions(
            reset,
            reset_reads,
            after,
            ctx,
            satisfies=lambda value, wanted=reset_demand.value: bool(value) is wanted,
        )
        if not reset_actions:
            continue
        if not all(_hold_allowed(ctx, action.pair) for action in reset_actions):
            continue  # a required lever is off-limits — the reset is unsteerable
        from pyrung.core.condition import AllCondition, CompareEq

        done_name = profile.done.name
        done = plc._known_tags_by_name[done_name]
        scope = CompareEq(done, incident.before_snap.get(done_name, False))
        operation_holds: list[PilotRung] = []
        for action in reset_actions:
            receipt = action.operation
            if receipt is None:
                continue
            guard = AllCondition(scope, _until_unresolved_condition(plc, receipt.until))
            operation_holds.append(PilotRung(action.tag, action.value, guard, operation=receipt))
            operation_inputs.add(action.tag)
        if not operation_holds:
            continue
        detail = ", ".join(
            f"{rung.dest}={rung.value!r} until {rung.operation.until!r}"
            for rung in operation_holds
            if rung.operation is not None
        )
        corrections.append(
            CorrectionHypothesis(
                kind="liveness",
                holds=tuple(operation_holds),
                # The completed owner rides in sources even when its physical
                # lever never changed during the incident. Causal ranking can
                # therefore distinguish this operation from a bystander timer.
                sources=(done_name, *(r.dest for r in operation_holds)),
                detail=f"reset {done_name}: {detail}",
                incident_local=True,
            )
        )

    def _emit_cannot_hold(
        owner: AdvanceOwner,
        constraint: Any,
        *,
        why: str,
    ) -> None:
        # A multi-read advance yields the coordinated set of levers that *break*
        # advancement (a minimal forcing assignment).  They must ride one
        # correction — for an ``Or``-driven advance no single lever stops it, so
        # splitting them into separate FREEZEs would propose holds that each fail
        # replay alone.
        step = owner.profile.plan(constraint, incident.before_snap)
        if step is None:
            return
        demands = (*step.holds, *((step.pulse,) if step.pulse is not None else ()))
        holds: list[tuple[str, Any]] = []
        for demand in demands:
            holds.extend(_cannot_hold_pairs(demand, after, ctx))
            if holds:
                break
        holds = [hold for hold in holds if hold[0] not in operation_inputs]
        if not holds or not all(_hold_allowed(ctx, h) for h in holds):
            return
        stops = ", ".join(f"{phys}={value!r}" for phys, value in holds)
        detail = f"stop holding {stops} ({why}"
        scans = None
        if owner.profile.linear is not None:
            scans = owner.profile.linear.estimate_scans(
                constraint,
                incident.before_snap,
                float(getattr(plc, "_dt", 0.01) or 0.01),
            )
        if scans is not None:
            detail += f" in ~{scans} scans"
        done_name = owner.profile.done.name if owner.profile.done is not None else constraint.tag
        corrections.append(
            CorrectionHypothesis(
                kind="done-boundary",
                holds=tuple(holds),
                sources=(done_name, *(phys for phys, _ in holds)),
                detail=detail + ")",
                incident_local=True,
            )
        )

    # --- Sub-case B: plain held-advance -> Done ---
    for owner in owners:
        profile = owner.profile
        if profile.done is None or profile.done.name not in changed:
            continue
        desired = bool(after.get(profile.done.name))
        _emit_cannot_hold(
            owner,
            Eq(profile.done.name, frozenset((desired,))),
            why=f"drives {profile.done.name} to done",
        )
        # The owner's completion boundary is a relation, not an instruction
        # category.  Its complement may have another authoritative operand:
        # ``Acc >= Limit`` can be prevented by driving Acc down *or* Limit up.
        # Surface every direct external operand and let replay decide how far it
        # must move; this applies equally to counters, analog limits, and any
        # future owner exposing a tag-bound scalar boundary.
        boundary = (
            profile.completion_boundary(incident.before_snap)
            if profile.completion_boundary is not None
            else None
        )
        if boundary is None:
            step = owner.profile.plan(
                Eq(profile.done.name, frozenset((desired,))),
                incident.before_snap,
            )
            boundary = step.until if step is not None else None
        complement = _complement_constraint(boundary)
        if complement is not None:
            for hold in _authoritative_relational_holds(
                complement,
                incident.before_snap,
                ctx,
            ):
                if not _hold_allowed(ctx, hold):
                    continue
                corrections.append(
                    CorrectionHypothesis(
                        kind="boundary-complement",
                        holds=(hold,),
                        sources=(profile.done.name, hold[0]),
                        detail=(
                            f"keep {profile.done.name} from completing by satisfying {complement!r}"
                        ),
                        constraint=complement,
                        incident_local=True,
                    )
                )

    # --- Sub-case C: Acc > Target threshold ejection ---
    # A bearing fact departed because an accumulator crossed a comparison
    # threshold.  trace_back surfaces the accumulator as a self-advancing leaf;
    # resolve it to its owning profile and stop holding whatever advances it.
    advance_index = build_advance_index(program)
    acc_names = {
        owner.profile.accumulator.name for owner in owners if owner.profile.accumulator is not None
    }
    handled_done = {
        owner.profile.done.name
        for owner in owners
        if owner.profile.done is not None and owner.profile.done.name in changed
    }
    seen_acc: set[str] = set()
    for departure in incident.departures:
        if not acc_names:
            break
        try:
            tree = trace_back(
                departure.tag,
                departure.value,
                after,
                pdg,
                program,
                getattr(ctx, "steerable", frozenset()),
                clear_only=getattr(ctx, "clear_only", frozenset()),
                opaque_loop=getattr(ctx, "opaque_loop", frozenset()),
                pipeline_internal_tags=getattr(ctx, "pipeline_internal_tags", frozenset()),
                route=getattr(ctx, "route", None),
                prior=getattr(ctx, "domain_prior", None),
            )
        except UnsupportedConstruct:
            raise
        except Exception:  # noqa: BLE001
            continue
        for leaf in tree.leaves():
            if getattr(leaf, "advance", None) is None or leaf.tag not in acc_names:
                continue
            if leaf.tag not in changed:
                continue  # only accumulators that actually advanced this incident
            if leaf.tag in seen_acc:
                continue
            seen_acc.add(leaf.tag)
            owner = advance_index.resolve(leaf.tag)
            if owner is None or (
                owner.profile.done is not None and owner.profile.done.name in handled_done
            ):
                continue  # done-bit ejection (Sub-case B) already owns this accumulator
            _emit_cannot_hold(
                owner,
                leaf.advance.until,
                why=f"{leaf.tag} crossed {leaf.value!r} -> {departure.tag} departed",
            )

    return corrections


# ---------------------------------------------------------------------------
# Steerable-driver resolution + accumulator-advance helpers (moved here intact;
# used only by the accumulator arm)
# ---------------------------------------------------------------------------


def _resolve_steerable_action(
    read_tag: str,
    value: Any,
    snap: Mapping[str, Any],
    ctx: Any,
    *,
    steerable: frozenset[str] | None = None,
) -> TraceAction | None:
    """Structural action that drives ``read_tag == value``.

    Either *read_tag* is itself steerable, or ``trace_back`` bridges it to its
    nearest steerable driver (e.g. the ``i_DoorClosed`` PIVOT to physical
    ``x_DoorClosed``). The returned action keeps the intermediate owner's
    boundary and progress receipt.
    """
    steerable = getattr(ctx, "steerable", frozenset()) if steerable is None else steerable
    if read_tag in steerable:
        boundary = Eq(read_tag, frozenset((value,)))
        return TraceAction(
            read_tag,
            value,
            until=boundary,
            operation=OperationReceipt(boundary),
        )
    pdg = getattr(ctx, "pdg", None)
    program = getattr(ctx, "program", None)
    if pdg is None or program is None:
        return None

    def _actions(tag: str, val: Any, view: dict[str, Any]) -> list[TraceAction]:
        tree = trace_back(
            tag,
            val,
            view,
            pdg,
            program,
            steerable,
            clear_only=getattr(ctx, "clear_only", frozenset()),
            opaque_loop=getattr(ctx, "opaque_loop", frozenset()),
            pipeline_internal_tags=getattr(ctx, "pipeline_internal_tags", frozenset()),
            route=getattr(ctx, "route", None),
            prior=getattr(ctx, "domain_prior", None),
        )
        return tree.ordered_action_details()

    try:
        view = dict(snap)
        if isinstance(value, bool) and _values_match(view.get(read_tag), value):
            # Already satisfied at the snapshot — the driver exists regardless,
            # but tracing a satisfied target returns a leafless stub (this is
            # what made a complement-reset "oscillation" one-way: the resting
            # polarity resolved to None and its watchdog contributed nothing).
            # Discover the driver via the opposite polarity — naturally
            # unsatisfied at the original snapshot — then flip read and driver
            # in the view so the wanted-polarity trace walks the writer and
            # does the polarity math itself.
            probe = _actions(read_tag, not value, dict(snap))
            view[read_tag] = not value
            for action in probe:
                view[action.tag] = action.value
        actions = _actions(read_tag, value, view)
    except UnsupportedConstruct:
        raise
    except Exception:  # noqa: BLE001
        return None
    if not actions:
        return None
    action = actions[0]
    if action.operation is not None:
        return action
    # Ordinary trace edges (for example physical input -> mapped contact) have
    # no intermediate cross-scan owner. Their trace handoff, when present, is
    # still the honest operation boundary; otherwise the physical assignment
    # itself is a one-scan boundary. Do not discard these actions merely because
    # no timer sits between the physical lever and the watchdog reset.
    boundary = action.until or Eq(action.tag, frozenset((action.value,)))
    return replace(
        action,
        until=boundary,
        operation=OperationReceipt(boundary),
    )


def _cannot_hold_pairs(demand: Any, snap: Mapping[str, Any], ctx: Any) -> list[tuple[str, Any]]:
    """Coordinated steerable holds that stop one demanded condition.

    Enumerates the advance condition over its reads' value spaces to find the
    minimal lever assignment that makes it evaluate ``!= advance_value`` (stops
    advancing), then resolves each participating read to its steerable driver.
    A single-read advance yields one lever; a conjunction yields the cheapest
    single conjunct to break; an ``Or`` yields every arm as a
    coordinated set. Returns ``[]`` when no steerable stopping assignment exists.
    """
    from pyrung.core.analysis.pdg import _extract_reads_from_condition

    if demand is None or demand.condition is None:
        return []
    reads = _extract_reads_from_condition(demand.condition, {})
    if not reads:
        return []
    holds = _best_forcing_holds(
        demand.condition,
        reads,
        snap,
        ctx,
        satisfies=lambda evaluated: bool(evaluated) is not demand.value,
    )
    return holds or []


# ---------------------------------------------------------------------------
# Condition enumeration — the minimal-lever primitive shared by both arms
# ---------------------------------------------------------------------------


def _read_domains(
    reads: set[str], snap: Mapping[str, Any], ctx: Any
) -> dict[str, tuple[Any, ...]] | None:
    """Finite value domain per read, or ``None`` if any read's domain is unknown.

    Bool reads resolve to ``(False, True)``; int reads use the prover's
    ``nd_domains`` (``ctx.domain_prior``) or the tag's declared ``choices``, then
    the producible-value resolution ``tide_tables._guard_operand_domain``
    already implements.  Reusing that resolver keeps the Bool+int domain handling
    identical to the tide tables rather than reinventing it.
    """
    from pyrung.core.analysis.pilot.tide_tables import _guard_operand_domain

    pdg = getattr(ctx, "pdg", None)
    program = getattr(ctx, "program", None)
    if pdg is None or program is None:
        return None
    prior = getattr(ctx, "domain_prior", None)
    base = dict(getattr(prior, "nd_domains", None) or {})
    tags = getattr(pdg, "tags", {})
    for tag in reads:
        if tag in base:
            continue
        tag_ref = tags.get(tag)
        choices = getattr(tag_ref, "choices", None) if tag_ref is not None else None
        if choices:
            base[tag] = tuple(choices)
    domains: dict[str, tuple[Any, ...]] = {}
    for tag in reads:
        cur = snap.get(tag)
        if isinstance(cur, bool):
            domains[tag] = (False, True)
            continue
        dom = _guard_operand_domain(tag, dict(snap), pdg, program, base)
        if not dom:
            return None
        domains[tag] = dom
    return domains


def _minimal_forcing_sets(
    condition: Any,
    order: tuple[str, ...],
    domains: dict[str, tuple[Any, ...]],
    satisfies: Callable[[Any], bool],
    base: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Minimal partial assignments that *force* ``satisfies(condition.evaluate)``.

    A partial assignment forces iff every completion over the remaining reads'
    domains evaluates to a satisfying value (an undecidable term disqualifies
    it).  Minimal = no already-found forcing set is a subset.  This is
    prime-implicant enumeration: for a conjunction the sole forcing set is every
    conjunct; for a disjunction each arm is its own single-literal set.  Returned
    smallest-first.
    """
    base_values = dict(base or {})

    def _eval(assignment: dict[str, Any]) -> bool | None:
        try:
            return bool(satisfies(condition.evaluate(_SnapView({**base_values, **assignment}))))
        except Exception:  # noqa: BLE001
            return None

    minimal: list[dict[str, Any]] = []
    for k in range(1, len(order) + 1):
        for subset in itertools.combinations(order, k):
            others = [t for t in order if t not in subset]
            other_doms = [domains[t] for t in others]
            for vcombo in itertools.product(*(domains[t] for t in subset)):
                partial = dict(zip(subset, vcombo, strict=True))
                if any(m.items() <= partial.items() for m in minimal):
                    continue  # a smaller forcing subset already covers this
                forced = True
                for ocombo in itertools.product(*other_doms):
                    completion = {**partial, **dict(zip(others, ocombo, strict=True))}
                    if _eval(completion) is not True:
                        forced = False
                        break
                if forced:
                    minimal.append(partial)
    return minimal


def _resolve_partial_actions(
    partial: dict[str, Any],
    snap: Mapping[str, Any],
    ctx: Any,
    *,
    steerable: frozenset[str] | None = None,
) -> list[TraceAction] | None:
    """Resolve each literal without discarding its intermediate operation owner.

    ``None`` if any read resolves to no steerable driver (the assignment is
    unsteerable) or two literals demand conflicting values of one driver.
    """
    actions: dict[str, TraceAction] = {}
    for read, value in partial.items():
        action = _resolve_steerable_action(
            read,
            value,
            snap,
            ctx,
            steerable=steerable,
        )
        if action is None:
            return None
        existing = actions.get(action.tag)
        if existing is not None and not _values_match(existing.value, action.value):
            return None
        if existing is None or (existing.operation is None and action.operation is not None):
            actions[action.tag] = action
    return list(actions.values())


def _best_forcing_actions(
    condition: Any,
    reads: set[str],
    snap: Mapping[str, Any],
    ctx: Any,
    *,
    satisfies: Callable[[Any], bool],
    base: Mapping[str, Any] | None = None,
    steerable: frozenset[str] | None = None,
) -> list[TraceAction] | None:
    """Cheapest structural driver operations that force *condition* to satisfy.

    Enumerates the reads' finite domains (capped like ``tide_tables``), finds
    the minimal forcing assignments, and among the steerable ones prefers (a)
    fewest levers that differ from the current snapshot, then (b) fewest levers
    total. ``None`` means no forcing assignment is steerable.
    """
    from pyrung.core.analysis.pilot.tide_tables import bounded_product

    order = tuple(sorted(reads))
    if not order:
        return None
    domains = _read_domains(reads, snap, ctx)
    if domains is None:
        return None
    if bounded_product(domains.values()) is None:
        return None

    sets = _minimal_forcing_sets(condition, order, domains, satisfies, base)
    if not sets:
        return None

    def _rank(partial: dict[str, Any]) -> tuple[int, int]:
        changes = sum(1 for t, v in partial.items() if not _values_match(snap.get(t), v))
        return (changes, len(partial))

    for partial in sorted(sets, key=_rank):
        actions = _resolve_partial_actions(partial, snap, ctx, steerable=steerable)
        if actions:
            return actions
    return None


def _best_forcing_holds(
    condition: Any,
    reads: set[str],
    snap: Mapping[str, Any],
    ctx: Any,
    *,
    satisfies: Callable[[Any], bool],
    base: Mapping[str, Any] | None = None,
    steerable: frozenset[str] | None = None,
) -> list[ActionPair] | None:
    """Return the minimum steady pair pins that force the requested truth."""
    actions = _best_forcing_actions(
        condition,
        reads,
        snap,
        ctx,
        satisfies=satisfies,
        base=base,
        steerable=steerable,
    )
    return None if actions is None else [action.pair for action in actions]


def break_guard_holds(
    rung_obj: Any,
    snap: Mapping[str, Any],
    ctx: Any,
    *,
    changeable: set[str] | frozenset[str] | None = None,
    fixed: Mapping[str, Any] | None = None,
    steerable: frozenset[str] | None = None,
) -> list[ActionPair] | None:
    """Minimal steerable lever set that forces *rung_obj*'s enable guard FALSE.

    The **suppression dual** of the accumulator arm's satisfy-the-reset
    enumeration: the same :func:`_best_forcing_holds` machinery with the polarity
    inverted (``satisfies=lambda v: not v`` instead of ``bool``).  This
    *suppresses* a clobbering writer — forces its guard false so the deviated
    register keeps the value the pulse established.

    ``changeable`` narrows the correction frontier to selected guard reads,
    while every other read stays at ``snap``.  ``fixed`` overlays at-fire values
    for protected reads (normally the actual triggers that represent intended
    progress). ``steerable`` optionally supplies the exact terminal lever set
    for this incident, so program-written or protected tags are traversed rather
    than mistaken for free inputs.

    Returns coordinated ``(phys, value)`` holds, or ``None`` when the guard is
    unreadable/unsteerable — no candidate reads, an unknown (live-word) domain,
    or no steerable forcing assignment. ``None`` is the **punt signal** the
    caller escalates to the skiff on.  Rejection stays over COMPLETE finite
    domains only (inherited from ``_best_forcing_holds`` / ``_read_domains``);
    it never fabricates a hold it cannot read.
    """
    from pyrung.core.analysis.pdg import _extract_reads_from_condition

    guard = rung_obj._get_combined_condition()
    if guard is None:
        return None
    reads = _extract_reads_from_condition(guard, {})
    if not reads:
        return None
    if changeable is not None:
        reads &= set(changeable)
    if not reads:
        return None
    base = dict(snap)
    if fixed:
        base.update(fixed)
    return _best_forcing_holds(
        guard,
        reads,
        snap,
        ctx,
        satisfies=lambda evaluated: not evaluated,
        base=base,
        steerable=steerable,
    )


class _SnapView:
    """Minimal ``ConditionView`` over a dict — just enough to evaluate a reset or
    advance condition over a trial assignment (``Condition.evaluate`` only calls
    ``get_tag(name, default)``)."""

    def __init__(self, snap: Mapping[str, Any]):
        self._snap = snap

    def get_tag(self, name: str, default: Any = None) -> Any:
        return self._snap.get(name, default)


# ---------------------------------------------------------------------------
# Precise fired-chain hypotheses
# ---------------------------------------------------------------------------


def _precise_causes(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
) -> list[CorrectionHypothesis]:
    """Minimal controllable cuts of the exact deep fired chain.

    For each departure, ``cause(deep=True)`` supplies the rungs that actually
    fired, their exact transitions, and their steady enablers. The walk derives
    two forms of cut from that one record:

    * revert a steerable transition at its pre-incident value;
    * force a fired rung's guard false with the cheapest steerable assignment.

    Program-written condition tags are never terminal levers merely because
    static steerability includes them; guard solving follows their
    observed/static writers to an external lever. Cuts that oppose requested
    progress remain hypotheses for investigation to reject against the
    incident bearing/checkpoint frontier. Every returned hypothesis names the
    fired rung whose conductive path it cuts.
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
    hypotheses: list[CorrectionHypothesis] = []

    # The channel departure is the incident's causal effect. Bearing aliases
    # are downstream symptoms and must not seed cuts of their mapping rungs.
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

        nogoods, mover_holds = chase_cause_roots(
            plc,
            departure.tag,
            effective_steerable,
            scan=departure.scan,
        )
        moved_tags = {
            transition.tag_name
            for step in chain.steps
            if id(step) in trigger_spine
            for transition in step.triggers
            if not _values_match(transition.from_value, transition.to_value)
        }
        mover_holds_filtered = tuple(
            pair
            for pair in _dedupe_pairs(mover_holds)
            if pair[0] in moved_tags and _hold_allowed(ctx, pair)
        )
        mover_values: dict[str, Any] = {}
        mover_contradiction = False
        for mover_tag, mover_value in mover_holds_filtered:
            if mover_tag in mover_values and not _values_match(
                mover_values[mover_tag], mover_value
            ):
                mover_contradiction = True
                break
            mover_values[mover_tag] = mover_value
        if mover_holds_filtered and not mover_contradiction:
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
                CorrectionHypothesis(
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
                    incident_local=True,
                )
            )

        if departure.scan is None:
            frame = dict(plc.state.tags)
        else:
            try:
                frame = dict(plc.history.at(departure.scan).tags)
            except Exception:  # noqa: BLE001
                frame = dict(plc.state.tags)

        from pyrung.core.analysis.pdg import resolve_rung

        for step in reversed(chain.steps):
            if id(step) not in trigger_spine:
                continue
            direct_values = {
                **{transition.tag_name: transition.to_value for transition in step.triggers},
                **{enabler.tag_name: enabler.value for enabler in step.enablers},
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
                from pyrung.core.instruction.coils import OutInstruction

                if not (isinstance(writer, OutInstruction) and step.transition.to_value is True):
                    continue
            guard_reads = set(getattr(node, "condition_reads", ())) & set(direct_values)
            if not guard_reads:
                continue
            fire_frame = {**frame, **direct_values}
            holds = break_guard_holds(
                rung_obj,
                fire_frame,
                ctx,
                changeable=guard_reads,
                fixed={},
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
                CorrectionHypothesis(
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
# Absence-root hypotheses
# ---------------------------------------------------------------------------

_ABSENCE_ROOT_KINDS = frozenset({"external", "never_written"})
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
) -> tuple[ActionPair, str] | None:
    """The analog analogue of the Bool flip: ``(hold, note)`` for a wide root.

    A Bool absence root flips to its complement; a wide word has none — but
    the chain knows what the stuck value *does*: the root supports the fault
    path through an ordered comparison on one of the chain's rungs. So flip
    the comparison's truth instead: solve the boundary of the flipped atom
    against the current snapshot and propose that value as the corrective
    hold. A guess is acceptable because replay verifies it. A root with no
    ordered comparison on the recorded chain yields nothing.
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

    step_keys = {(step.rung_index, step.subroutine) for step in chain.steps}
    seen: set[tuple[str, str, Any, bool, int | float, int | float]] = set()
    for node in pdg.rung_nodes:
        if name not in getattr(node, "condition_reads", ()):
            continue
        if (node.rung_index, node.subroutine) not in step_keys:
            continue
        rung = resolve_rung(program, node)
        if rung is None:
            continue
        for atom in _iter_atoms(_conditions_list_to_expr(getattr(rung, "_conditions", []))):
            if atom.tag == name:
                atom_on_root = atom
            elif (
                atom.operand_is_tag
                and atom.operand == name
                and atom.form in _FLIP_FORM
                and atom.operand_scale != 0
            ):
                atom_on_root = Atom(
                    tag=name,
                    form=(_FLIP_FORM[atom.form] if atom.operand_scale > 0 else atom.form),
                    operand=atom.tag,
                    operand_is_tag=True,
                    operand_scale=1 / atom.operand_scale,
                    operand_offset=-atom.operand_offset / atom.operand_scale,
                )
            else:
                continue
            if atom_on_root.form not in _NEGATE_FORM or atom_on_root._key() in seen:
                continue
            seen.add(atom_on_root._key())
            operand = atom_on_root.operand
            if atom_on_root.operand_is_tag:
                raw_threshold = snapshot.get(operand)
                try:
                    if raw_threshold is None:
                        raise TypeError
                    threshold = (
                        atom_on_root.operand_scale * raw_threshold + atom_on_root.operand_offset
                    )
                except TypeError:
                    threshold = None
            else:
                threshold = operand
            truth = _ordered_truth(atom_on_root.form, root.value, threshold)
            if truth is None:
                continue
            goal = (
                Atom(
                    tag=name,
                    form=_NEGATE_FORM[atom_on_root.form],
                    operand=operand,
                    operand_is_tag=atom_on_root.operand_is_tag,
                    operand_scale=atom_on_root.operand_scale,
                    operand_offset=atom_on_root.operand_offset,
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
                continue
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
) -> tuple[list[CorrectionHypothesis], frozenset[str]]:
    """Corrective holds from the deep walk's never-moved roots.

    The shallow chase cannot reach a cause that never transitioned. The deep
    recorded walk names those terminals as roots with no held-since scan. Each
    steerable, never-moved Bool root becomes a flip hypothesis, replay-tested
    like any other.

    The returned root names become causal rank evidence because an absence root
    has no transition for temporal proximity to observe. ``exclude`` carries
    the action that launched this incident, preventing self-investigation.
    ``installed`` names values owned by prior confirmed corrections; those
    roots are only reconsidered when the current chain computes a different
    ordered boundary.
    """
    channel = incident.channel_tag
    departure = None
    if channel is not None:
        departure = next((item for item in incident.departures if item.tag == channel), None)
    if departure is None:
        departure = next(iter(incident.departures), None)
    if departure is None:
        return [], frozenset()
    chain = _shared_cause(plc, departure.tag, departure.scan)
    if chain is None:
        return [], frozenset()

    steerable = getattr(ctx, "steerable", frozenset())
    installed = installed or {}
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "absence-root: %s@%s roots=%s",
            departure.tag,
            departure.scan,
            [
                (root.tag_name, root.value, root.kind, root.held_since_scan)
                for root in chain.ranked_roots()
            ],
        )
    keyed: list[tuple[int, CorrectionHypothesis]] = []
    root_tags: set[str] = set()
    for root in chain.ranked_roots():
        if root.kind not in _ABSENCE_ROOT_KINDS:
            continue
        if root.tag_name in exclude:
            continue
        if root.tag_name not in steerable:
            continue
        installed_owner = root.tag_name in installed and _values_match(
            installed[root.tag_name], root.value
        )
        if root.held_since_scan is not None and not installed_owner:
            continue
        if installed_owner:
            analog = _analog_boundary_hold(plc, root, chain, ctx)
            if analog is None:
                continue
            hold, note = analog
            relation_note = f"; recompute installed owner: {note}"
        elif isinstance(root.value, bool):
            hold = (root.tag_name, not root.value)
            relation_note = ""
        else:
            analog = _analog_boundary_hold(plc, root, chain, ctx)
            if analog is None:
                continue
            hold, note = analog
            relation_note = f"; {note}"
        if not _hold_allowed(ctx, hold):
            continue
        root_tags.add(root.tag_name)
        keyed.append(
            (
                len(root.via),
                CorrectionHypothesis(
                    kind="absence-root",
                    holds=(hold,),
                    sources=(root.tag_name,),
                    detail=(
                        f"{root.tag_name} held {root.value!r} "
                        f"{'by PILOT' if installed_owner else 'since cold'} "
                        f"on {departure.tag}'s deep cause chain "
                        f"[{root.kind}]{relation_note}"
                    ),
                    history_origin=root.kind,
                ),
            )
        )
    keyed.sort(key=lambda item: -item[0])
    return [hypothesis for _, hypothesis in keyed], frozenset(root_tags)


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
    hypotheses: Iterable[CorrectionHypothesis],
) -> tuple[CorrectionHypothesis, ...]:
    out: list[CorrectionHypothesis] = []
    seen: set[tuple[Any, ...]] = set()
    for hypothesis in hypotheses:
        key = hypothesis.holds
        if key in seen:
            continue
        seen.add(key)
        out.append(hypothesis)
    return tuple(out)
