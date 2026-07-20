"""Derive scoped corrective-hold candidates from writer enablers.

Coil writers produce guard-flip candidates; accumulating instructions produce
steady or oscillating holds that prevent an unwanted completion. Both paths
return ``EnablerCorrection`` values with the exposure-derived scope that can be
proved from program structure.

These are hypotheses, not installed corrections. ``investigate.py`` replay
validates them and ``progress.py`` installs at most one confirmed result.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot._ops import (
    PilotRung,
    _hold_allowed,
)
from pyrung.core.analysis.pilot.advance import (
    AdvanceOwner,
    build_advance_index,
    iter_advance_owners,
)
from pyrung.core.analysis.pilot.trace import trace_back
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.crossing import Eq

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.types import DeviationIncident
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)

# Local alias — corrections imports nothing at runtime from investigate, so the
# investigate <-> corrections dependency stays one-directional (investigate
# depends on this module, never the reverse).
ActionPair = tuple[str, Any]


# ---------------------------------------------------------------------------
# Shared output vocabulary — the spine
# ---------------------------------------------------------------------------


class Correction(Enum):
    """The *shape* of a corrective hold, stable across the reason that proposed it.

    ``FLIP``      — a steady hold at the value that *breaks* an enabling
                    condition (coil guard-break: ``(lever, not enabling_value)``).
    ``FREEZE``    — a steady directed hold that *stops* advancement / maintains a
                    value (accumulator stop-hold: ``(lever, stop_value)``).
    ``OSCILLATE`` — a guarded toggling hold (:class:`PilotRung`) for a
                    complement-reset watchdog where no single steady value works.
    ``NONE``      — diagnostic only; no actionable steerable lever.
    """

    FREEZE = "freeze"
    FLIP = "flip"
    OSCILLATE = "oscillate"
    NONE = "none"


@dataclass(frozen=True)
class EnablerCorrection:
    """One corrective proposal — the type both dispatch arms converge on.

    ``correction`` is the action shape; ``kind`` is the preserved telemetry
    label (``"latch-exposure"`` / ``"liveness"`` / ``"done-boundary"``) carried
    through to :class:`InvestigationHypothesis` so existing consumers are
    unchanged.
    """

    correction: Correction
    kind: str
    holds: tuple[Any, ...]
    sources: tuple[str, ...]
    detail: str


def correct_enablers(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
) -> list[EnablerCorrection]:
    """The single ``no-steerable-trigger -> corrective hold`` pass.

    Runs both dispatch arms and returns their shared :class:`EnablerCorrection`
    stream (the caller — ``investigate_deviation`` — adapts it to its own
    ``InvestigationHypothesis`` at the module boundary, so this module never
    imports investigation types at runtime).  Coil corrections precede
    accumulator corrections, matching the former ``_latch_exposure`` →
    ``_done_boundary`` ordering.
    """
    coil = _coil_corrections(plc, incident, ctx)
    accumulator = _accumulator_corrections(plc, incident, ctx)
    pulsed_inputs = {
        getattr(hold, "dest", hold[0] if isinstance(hold, tuple) else None)
        for correction in accumulator
        if correction.correction is Correction.OSCILLATE
        for hold in correction.holds
    }
    if pulsed_inputs:
        coil = [
            correction
            for correction in coil
            if not correction.holds
            or any(
                getattr(hold, "dest", hold[0] if isinstance(hold, tuple) else None)
                not in pulsed_inputs
                for hold in correction.holds
            )
        ]
    corrections = [*coil, *accumulator]
    return [c for c in corrections if c.correction is not Correction.NONE and c.holds]


# ---------------------------------------------------------------------------
# Exposure lifetime — exact conditions from the recorded deep cause
# ---------------------------------------------------------------------------


def _causal_channel_guard(
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
    from pyrung.core.condition import AllCondition, AnyCondition

    channel_name = incident.channel_tag
    pdg = getattr(ctx, "pdg", None)
    program = getattr(ctx, "program", None)
    if channel_name is None or pdg is None or program is None:
        return None

    terms: list[Any] = []
    seen_terms: set[tuple[int, ...]] = set()
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
            term_key = tuple(id(condition) for condition in conditions)
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

    if not terms:
        return None
    return terms[0] if len(terms) == 1 else AnyCondition(*terms)


# ---------------------------------------------------------------------------
# Coil arm — latches that fired during the incident  (FLIP a non-state guard)
# ---------------------------------------------------------------------------


def _coil_corrections(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
) -> list[EnablerCorrection]:
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
        except Exception:  # noqa: BLE001
            return []
        return list(tree.steerable_leaves())

    def _latch_guard_holds(tag: str) -> list[ActionPair]:
        """Corrective steerable holds for an active latch *tag*, or [].

        The trace may bridge an image-level contact such as
        ``i_DoorClosed`` to its physical ``x_DoorClosed`` lever.
        """
        from pyrung.core.analysis.simplified import _conditions_list_to_expr, _expr_forced_true

        holds: list[ActionPair] = []
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
            # ``after_snap`` value — the old heuristic — lies for a
            # fire-then-reset guard: the Done bit that latched the alarm has
            # already reset by the after snapshot, so the flip proposed the
            # latch-CAUSING polarity and the hypothesis silently vanished.
            # Fallback when no forcing value exists (guard absent from the
            # resolved expr): the legacy flip, judged by replay as before.
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
                    holds.append(hold)
        return holds

    def _guarded(
        holds: list[ActionPair],
        source_tags: tuple[str, ...],
    ) -> tuple[Any, ...]:
        """Wrap holds in the exact recorded conductive context when readable."""
        guard = _causal_channel_guard(plc, source_tags, incident, ctx)
        if guard is None:
            return tuple(holds)
        return tuple(PilotRung(tag, value, guard) for tag, value in holds)

    corrections: list[EnablerCorrection] = []
    conjunction: list[ActionPair] = []
    conj_seen: set[ActionPair] = set()
    conj_latches: list[str] = []
    for tag, val in sorted(incident.after_snap.items()):
        if val is not True or incident.before_snap.get(tag) is True:
            continue
        latch_holds = _latch_guard_holds(tag)
        if not latch_holds:
            continue
        corrections.append(
            EnablerCorrection(
                correction=Correction.FLIP,
                kind="latch-exposure",
                holds=_guarded(latch_holds, (tag,)),
                sources=(tag, *(h[0] for h in latch_holds)),
                detail=f"latch {tag} fired during incident",
            )
        )
        conj_latches.append(tag)
        for hold in latch_holds:
            if hold not in conj_seen:
                conj_seen.add(hold)
                conjunction.append(hold)

    if len(conjunction) > 1:
        corrections.append(
            EnablerCorrection(
                correction=Correction.FLIP,
                kind="latch-exposure",
                holds=_guarded(conjunction, tuple(conj_latches)),
                sources=(*conj_latches, *(h[0] for h in conjunction)),
                detail=f"clear {len(conj_latches)} active latches: {', '.join(conj_latches)}",
            )
        )
    return corrections


# ---------------------------------------------------------------------------
# Accumulator arm — watchdogs that completed during coast  (OSCILLATE / FREEZE)
# ---------------------------------------------------------------------------


def _accumulator_corrections(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
) -> list[EnablerCorrection]:
    """Derive corrections for accumulator completion during a coast.

    While PILOT coasts, an accumulating instruction (timer/counter) can *complete
    on its own* and eject the bearing — its ``Done`` bit rises, or a rung fires on
    ``Acc > Target``.  The held input *driving* the accumulator is the cause.
    Three corrective shapes, all replay-validated:

    * **Complement-reset watchdog** — reset driven by a single input held at the
      wrong polarity (``rotate.py`` ``SensorOnWD``/``SensorOffWD``): a steady hold
      can never satisfy it, so the input must *oscillate*. Emit a guarded
      :class:`PilotRung`, one rule per resetting polarity.
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
    corrections: list[EnablerCorrection] = []

    # --- Sub-case A: complement-reset oscillation (generalized old liveness) ---
    #   lever_values[phys] — the values each lever must visit across *all*
    #     watchdogs (a lever shared by a complement pair collects both polarities,
    #     so its hold oscillates; a lever a conjunction pins collects one, so its
    #     hold is a steady drive).  Aggregated globally, not per watchdog, because
    #     the oscillation only emerges from combining complementary resets.
    #   fired_levers — for each watchdog whose Done actually completed this
    #     incident, the coordinated set of levers that satisfy *its* reset (a
    #     minimal forcing assignment over the reset's reads).  A reset gated by a
    #     conjunction of inputs now yields a multi-lever set instead of being
    #     skipped for having no single unambiguous lever.
    lever_values: dict[str, set[Any]] = {}
    fired_levers: list[tuple[str, tuple[str, ...]]] = []
    owners = tuple(iter_advance_owners(program))
    for owner in owners:
        profile = owner.profile
        if profile.done is None:
            continue
        # Ask how this owner clears an asserted Done bit.  A complementary
        # watchdog may still be clear in the incident snapshot, but its reset
        # polarity is part of the same liveness rule.
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
        # Minimal coordinated levers whose held values *satisfy* the reset.
        reset_holds = _best_forcing_holds(
            reset,
            reset_reads,
            after,
            ctx,
            satisfies=lambda value, wanted=reset_demand.value: bool(value) is wanted,
        )
        if not reset_holds:
            continue
        if not all(_hold_allowed(ctx, (phys, val)) for phys, val in reset_holds):
            continue  # a required lever is off-limits — the reset is undrivable
        for phys, val in reset_holds:
            lever_values.setdefault(phys, set()).add(val)
        if profile.done.name in changed:
            fired_levers.append((profile.done.name, tuple(phys for phys, _ in reset_holds)))

    seen_osc: set[tuple[ActionPair, ...]] = set()
    for done_name, levers in fired_levers:
        osc_holds: list[Any] = []
        from pyrung.core.condition import AllCondition, CompareEq, CompareNe

        done = plc._known_tags_by_name[done_name]
        scope = CompareEq(done, incident.before_snap.get(done_name, False))
        for phys in levers:
            vals = sorted(lever_values.get(phys, set()))
            if not vals:
                break
            source = plc._known_tags_by_name[phys]
            osc_holds.extend(
                PilotRung(phys, v, AllCondition(scope, CompareNe(source, v))) for v in vals
            )
        if not osc_holds:
            continue
        key = tuple((r.dest, r.value) for r in osc_holds)
        if key in seen_osc:
            continue  # a complement pair yields the same coordinated hold twice
        seen_osc.add(key)
        detail = ", ".join(
            f"{phys} between {sorted(lever_values[phys])}" for phys in dict.fromkeys(levers)
        )
        corrections.append(
            EnablerCorrection(
                correction=Correction.OSCILLATE,
                kind="liveness",
                holds=tuple(osc_holds),
                # The fired Done rides in sources: an oscillating input never
                # *changes* in the incident, so the cause chain of the ejection
                # can only meet this hypothesis through the watchdog Done it
                # feeds — that is what lets causal-primacy ranking tell the
                # ejecting watchdog's lever from a bystander watchdog's.
                sources=(done_name, *(r.dest for r in osc_holds)),
                detail=f"oscillate {detail} (complement-reset watchdog)",
            )
        )

    # Inputs already owned by an oscillation rule must not also get a (contrary)
    # steady cannot-hold.
    osc_inputs = set(lever_values)

    def _emit_cannot_hold(
        owner: AdvanceOwner,
        constraint: Any,
        *,
        why: str,
    ) -> None:
        # A multi-read advance now yields the coordinated set of levers that
        # *break* advancement (a minimal forcing assignment).  They must ride one
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
        holds = [hold for hold in holds if hold[0] not in osc_inputs]
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
            EnablerCorrection(
                correction=Correction.FREEZE,
                kind="done-boundary",
                holds=tuple(holds),
                sources=(done_name, *(phys for phys, _ in holds)),
                detail=detail + ")",
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


def _resolve_steerable_driver(
    read_tag: str,
    value: Any,
    snap: Mapping[str, Any],
    ctx: Any,
    *,
    steerable: frozenset[str] | None = None,
) -> tuple[str, Any] | None:
    """Steerable ``(phys, polarity)`` that drives ``read_tag == value``.

    Either *read_tag* is itself steerable, or ``trace_back`` bridges it to its
    nearest steerable driver (e.g. the ``i_DoorClosed`` PIVOT to physical
    ``x_DoorClosed``).  Shared by the oscillation and cannot-hold sub-cases.
    """
    steerable = getattr(ctx, "steerable", frozenset()) if steerable is None else steerable
    if read_tag in steerable:
        return (read_tag, value)
    pdg = getattr(ctx, "pdg", None)
    program = getattr(ctx, "program", None)
    if pdg is None or program is None:
        return None

    def _leaves(tag: str, val: Any, view: dict[str, Any]) -> list[tuple[str, Any]]:
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
        return list(tree.steerable_leaves())

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
            probe = _leaves(read_tag, not value, dict(snap))
            view[read_tag] = not value
            for drv, dval in probe:
                view[drv] = dval
        leaves = _leaves(read_tag, value, view)
    except Exception:  # noqa: BLE001
        return None
    return leaves[0] if leaves else None


def _cannot_hold_pairs(demand: Any, snap: Mapping[str, Any], ctx: Any) -> list[tuple[str, Any]]:
    """Coordinated steerable holds that stop one demanded condition.

    Enumerates the advance condition over its reads' value spaces to find the
    minimal lever assignment that makes it evaluate ``!= advance_value`` (stops
    advancing), then resolves each participating read to its steerable driver.
    A single-read advance yields one lever (the old behaviour); a conjunction
    yields the cheapest single conjunct to break; an ``Or`` yields every arm as a
    coordinated set.  Returns ``[]`` when no drivable stopping assignment exists.
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


def _resolve_partial(
    partial: dict[str, Any],
    snap: Mapping[str, Any],
    ctx: Any,
    *,
    steerable: frozenset[str] | None = None,
) -> list[ActionPair] | None:
    """Resolve each ``read == value`` literal to its steerable ``(phys, value)``.

    ``None`` if any read resolves to no steerable driver (the assignment is
    undrivable) or two literals demand conflicting values of one driver.
    """
    holds: dict[str, Any] = {}
    for read, value in partial.items():
        resolved = _resolve_steerable_driver(
            read,
            value,
            snap,
            ctx,
            steerable=steerable,
        )
        if resolved is None:
            return None
        phys, polarity = resolved
        if phys in holds and not _values_match(holds[phys], polarity):
            return None
        holds[phys] = polarity
    return list(holds.items())


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
    """Cheapest drivable coordinated holds that force *condition* to satisfy.

    Enumerates the reads' finite domains (capped like ``tide_tables``), finds
    the minimal forcing assignments, and among the drivable ones prefers (a)
    fewest levers that differ from the current snapshot, then (b) fewest levers
    total.  ``None`` when no assignment is drivable — the honest decline the
    single-read path made for a missing lever, now generalized to conjunctions.
    """
    from pyrung.core.analysis.pilot.tide_tables import _MAX_COMBOS, _MAX_FREE_INDICES

    order = tuple(sorted(reads))
    if not order or len(order) > _MAX_FREE_INDICES:
        return None
    domains = _read_domains(reads, snap, ctx)
    if domains is None:
        return None
    total = 1
    for dom in domains.values():
        total *= len(dom)
    if total > _MAX_COMBOS:
        return None

    sets = _minimal_forcing_sets(condition, order, domains, satisfies, base)
    if not sets:
        return None

    def _rank(partial: dict[str, Any]) -> tuple[int, int]:
        changes = sum(1 for t, v in partial.items() if not _values_match(snap.get(t), v))
        return (changes, len(partial))

    for partial in sorted(sets, key=_rank):
        holds = _resolve_partial(partial, snap, ctx, steerable=steerable)
        if holds:
            return holds
    return None


def break_guard_holds(
    rung_obj: Any,
    snap: Mapping[str, Any],
    ctx: Any,
    *,
    changeable: set[str] | frozenset[str] | None = None,
    fixed: Mapping[str, Any] | None = None,
    steerable: frozenset[str] | None = None,
) -> list[ActionPair] | None:
    """Minimal drivable lever set that forces *rung_obj*'s enable guard FALSE.

    The **suppression dual** of the accumulator arm's satisfy-the-reset
    enumeration: the same :func:`_best_forcing_holds` machinery with the polarity
    inverted (``satisfies=lambda v: not v`` instead of ``bool``).  Used to
    *suppress* a clobbering writer — force its guard false so the deviated
    register keeps the value the pulse established.

    ``changeable`` narrows the correction frontier to selected guard reads,
    while every other read stays at ``snap``.  ``fixed`` overlays at-fire values
    for protected reads (normally the actual triggers that represent intended
    progress). ``steerable`` optionally supplies the exact terminal lever set
    for this incident, so program-written or protected tags are traversed rather
    than mistaken for free inputs.

    Returns coordinated ``(phys, value)`` holds, or ``None`` when the guard is
    unreadable/undrivable — no candidate reads, an unknown (live-word) domain,
    or no drivable forcing assignment.  ``None`` is the **punt signal** the
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
