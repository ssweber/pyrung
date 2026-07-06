"""Unified enabler-correction classifier for PILOT investigation.

When ``cause()`` finds no steerable *trigger* for a bearing departure, the
*enablers* that held the writer's path open are the real cause.  This module
owns the single decision — given such a writer, what is the corrective hold? —
and dispatches by writer instruction:

  * coil (latch)             -> FLIP a non-state guard       (``_coil_corrections``)
  * accumulating instruction -> OSCILLATE / steady stop-hold (``_accumulator_corrections``)

The two arms enumerate *different* work-sets (the coil arm sweeps ``after_snap``
for latches that fired on state entry; the accumulator arm sweeps instruction
profiles for watchdogs that completed during the coast), so they are not one
loop.  What unifies them is the shared **output vocabulary**
(:class:`EnablerCorrection`): both arms emit it, and ``correct_enablers`` hands
``investigate_deviation`` a single stream regardless of which arm produced it.

Replaces the former ``_latch_exposure_hypotheses`` and
``_done_boundary_hypotheses`` passes in ``investigate.py``.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot._ops import ConditionalHold, _hold_allowed, _HoldRule
from pyrung.core.analysis.pilot.accumulators import (
    AccumulatorMatch,
    iter_profiles,
    resolve_profile,
    scans_to_eject,
)
from pyrung.core.analysis.pilot.trace import trace_back
from pyrung.core.analysis.sp_values import _values_match

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
    ``OSCILLATE`` — a guarded toggling hold (:class:`ConditionalHold`) for a
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
    holds: tuple[ActionPair, ...]
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
    corrections: list[EnablerCorrection] = [
        *_coil_corrections(plc, incident, ctx),
        *_accumulator_corrections(plc, incident, ctx),
    ]
    return [c for c in corrections if c.correction is not Correction.NONE and c.holds]


# ---------------------------------------------------------------------------
# Coil arm — latches that fired on state entry  (FLIP a non-state guard)
# ---------------------------------------------------------------------------


def _coil_corrections(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
) -> list[EnablerCorrection]:
    """Latch-exposure: alarm latches that fired as a consequence of our action.

    A latch that is *active* (True after the regression) and *gated by a state
    we were already in* (True in ``before_snap``) latched because of the move we
    made into that state — the door/lint alarms latch the instant we enter
    Starting.  Each such latch's non-state guard inputs are preconditions we
    failed to establish; we flip each to the value that breaks the latch and
    resolve it to its steerable driver via ``trace_back`` (bridging the
    ``i_DoorClosed`` PIVOT to the physical ``x_DoorClosed``).

    The holds are proposed both per-latch *and* as one conjunction: when several
    alarms fire together (door AND lint), no single hold reaches the corridor —
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
                opaque_loop=opaque_loop,
                pipeline_internal_tags=pipeline_internal,
                route=route,
                prior=getattr(ctx, "domain_prior", None),
            )
        except Exception:  # noqa: BLE001
            return []
        return list(tree.steerable_leaves())

    def _latch_guard_holds(tag: str) -> list[ActionPair]:
        """Corrective steerable holds for an active latch *tag*, or []."""
        holds: list[ActionPair] = []
        seen: set[ActionPair] = set()
        for ri in pdg.writers_of.get(tag, frozenset()):
            node = pdg.rung_nodes[ri]
            ro = resolve_rung(program, node)
            if ro is None or not any(isinstance(i, LatchInstruction) for i in ro._instructions):
                continue
            # The PDG node's condition_reads is subroutine-aware; the resolved
            # rung's sp_tree() has no tag-name accessor.  Polarity is irrelevant
            # here — we flip each guard off its current value and let the replay
            # judge — so the read set is all we need.
            condition_tags = set(node.condition_reads)
            state_tags = condition_tags & opaque_loop
            # Fired on our action only if gated by a state we were already in.
            if not any(_values_match(incident.before_snap.get(s), True) for s in state_tags):
                continue
            for guard in sorted(condition_tags - state_tags):
                cur = incident.after_snap.get(guard)
                if not isinstance(cur, bool):
                    continue
                for hold in _steerable_holds(guard, not cur):
                    if hold not in seen and _hold_allowed(ctx, hold):
                        seen.add(hold)
                        holds.append(hold)
        return holds

    corrections: list[EnablerCorrection] = []
    conjunction: list[ActionPair] = []
    conj_seen: set[ActionPair] = set()
    conj_latches: list[str] = []
    for tag, val in incident.after_snap.items():
        if val is not True:
            continue
        latch_holds = _latch_guard_holds(tag)
        if not latch_holds:
            continue
        corrections.append(
            EnablerCorrection(
                correction=Correction.FLIP,
                kind="latch-exposure",
                holds=tuple(latch_holds),
                sources=(tag, *(h[0] for h in latch_holds)),
                detail=f"latch {tag} active in entered state",
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
                holds=tuple(conjunction),
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
    """Generalized accumulator-completion handler (subsumes the old liveness pass).

    While PILOT coasts, an accumulating instruction (timer/counter) can *complete
    on its own* and eject the bearing — its ``Done`` bit rises, or a rung fires on
    ``Acc > Target``.  The held input *driving* the accumulator is the cause.
    Three corrective shapes, all replay-validated:

    * **Complement-reset watchdog** — reset driven by a single input held at the
      wrong polarity (``rotate.py`` ``SensorOnWD``/``SensorOffWD``): a steady hold
      can never satisfy it, so the input must *oscillate*.  Emit a guarded
      :class:`ConditionalHold`, one rule per resetting polarity.  (This is the old
      ``_liveness_hypotheses``, now keyed off any accumulator's profile rather
      than just ``OnDelayInstruction``.)
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
    for profile, _instr in iter_profiles(program):
        reset = profile.reset
        if reset is None:
            continue
        reset_reads = _extract_reads_from_condition(reset, {})
        if not reset_reads:
            continue
        # Minimal coordinated levers whose held values *satisfy* the reset.
        reset_holds = _best_forcing_holds(reset, reset_reads, after, ctx, satisfies=bool)
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
        osc_holds: list[ActionPair] = []
        for phys in levers:
            vals = sorted(lever_values.get(phys, set()))
            if not vals:
                break
            rules = tuple(
                _HoldRule(value=v, guard_tag=phys, guard_op="ne", guard_value=v) for v in vals
            )
            osc_holds.append((phys, ConditionalHold(rules=rules)))
        if len(osc_holds) != len(levers):
            continue
        key = tuple(osc_holds)
        if key in seen_osc:
            continue  # a complement pair yields the same coordinated hold twice
        seen_osc.add(key)
        detail = ", ".join(f"{phys} between {sorted(lever_values[phys])}" for phys, _ in osc_holds)
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
                sources=(done_name, *(phys for phys, _ in osc_holds)),
                detail=f"oscillate {detail} (complement-reset watchdog)",
            )
        )

    # Inputs already owned by an oscillation rule must not also get a (contrary)
    # steady cannot-hold.
    osc_inputs = set(lever_values)

    def _emit_cannot_hold(match: AccumulatorMatch, *, threshold: int | None, why: str) -> None:
        # A multi-read advance now yields the coordinated set of levers that
        # *break* advancement (a minimal forcing assignment).  They must ride one
        # correction — for an ``Or``-driven advance no single lever stops it, so
        # splitting them into separate FREEZEs would propose holds that each fail
        # replay alone.
        holds = [h for h in _cannot_hold_pairs(match.profile, after, ctx) if h[0] not in osc_inputs]
        if not holds or not all(_hold_allowed(ctx, h) for h in holds):
            return
        stops = ", ".join(f"{phys}={value!r}" for phys, value in holds)
        detail = f"stop holding {stops} ({why}"
        scans = scans_to_eject(match, plc, threshold=threshold)
        if scans is not None:
            detail += f" in ~{scans} scans"
        corrections.append(
            EnablerCorrection(
                correction=Correction.FREEZE,
                kind="done-boundary",
                holds=tuple(holds),
                sources=(match.profile.done.name, *(phys for phys, _ in holds)),
                detail=detail + ")",
            )
        )

    # --- Sub-case B: plain held-advance -> Done ---
    for profile, instr in iter_profiles(program):
        if profile.done.name not in changed:
            continue
        _emit_cannot_hold(
            AccumulatorMatch(profile, instr, via_done=True),
            threshold=None,
            why=f"drives {profile.done.name} to done",
        )

    # --- Sub-case C: Acc > Target threshold ejection ---
    # A bearing fact departed because an accumulator crossed a comparison
    # threshold.  trace_back surfaces the accumulator as a self-advancing leaf;
    # resolve it to its owning profile and stop holding whatever advances it.
    acc_names = {p.accumulator.name for p, _ in iter_profiles(program)}
    handled_done = {p.done.name for p, _ in iter_profiles(program) if p.done.name in changed}
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
                opaque_loop=getattr(ctx, "opaque_loop", frozenset()),
                pipeline_internal_tags=getattr(ctx, "pipeline_internal_tags", frozenset()),
                route=getattr(ctx, "route", None),
                prior=getattr(ctx, "domain_prior", None),
            )
        except Exception:  # noqa: BLE001
            continue
        for leaf in tree.leaves():
            if not getattr(leaf, "self_advancing", False) or leaf.tag not in acc_names:
                continue
            if leaf.tag not in changed:
                continue  # only accumulators that actually advanced this incident
            if leaf.tag in seen_acc:
                continue
            seen_acc.add(leaf.tag)
            match = resolve_profile(leaf.tag, program)
            if match is None or match.profile.done.name in handled_done:
                continue  # done-bit ejection (Sub-case B) already owns this accumulator
            threshold = leaf.value if isinstance(leaf.value, int) else None
            _emit_cannot_hold(
                match,
                threshold=threshold,
                why=f"{leaf.tag} crossed {leaf.value!r} -> {departure.tag} departed",
            )

    return corrections


# ---------------------------------------------------------------------------
# Steerable-driver resolution + accumulator-advance helpers (moved here intact;
# used only by the accumulator arm)
# ---------------------------------------------------------------------------


def _resolve_steerable_driver(
    read_tag: str, value: Any, snap: Mapping[str, Any], ctx: Any
) -> tuple[str, Any] | None:
    """Steerable ``(phys, polarity)`` that drives ``read_tag == value``.

    Either *read_tag* is itself steerable, or ``trace_back`` bridges it to its
    nearest steerable driver (e.g. the ``i_DoorClosed`` PIVOT to physical
    ``x_DoorClosed``).  Shared by the oscillation and cannot-hold sub-cases.
    """
    steerable = getattr(ctx, "steerable", frozenset())
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


def _cannot_hold_pairs(profile: Any, snap: Mapping[str, Any], ctx: Any) -> list[tuple[str, Any]]:
    """Coordinated steerable holds that *stop* an accumulator from advancing.

    Enumerates the advance condition over its reads' value spaces to find the
    minimal lever assignment that makes it evaluate ``!= advance_value`` (stops
    advancing), then resolves each participating read to its steerable driver.
    A single-read advance yields one lever (the old behaviour); a conjunction
    yields the cheapest single conjunct to break; an ``Or`` yields every arm as a
    coordinated set.  Returns ``[]`` when no drivable stopping assignment exists.
    """
    from pyrung.core.analysis.pdg import _extract_reads_from_condition

    if profile.advance is None:
        return []
    reads = _extract_reads_from_condition(profile.advance, {})
    if not reads:
        return []
    advance_value = profile.advance_value
    holds = _best_forcing_holds(
        profile.advance,
        reads,
        snap,
        ctx,
        satisfies=lambda evaluated: evaluated != advance_value,
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
    the producible-value resolution ``table_oracle._guard_operand_domain``
    already implements.  Reusing that resolver keeps the Bool+int domain handling
    identical to the guard oracle rather than reinventing it.
    """
    from pyrung.core.analysis.pilot.table_oracle import _guard_operand_domain

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
) -> list[dict[str, Any]]:
    """Minimal partial assignments that *force* ``satisfies(condition.evaluate)``.

    A partial assignment forces iff every completion over the remaining reads'
    domains evaluates to a satisfying value (an undecidable term disqualifies
    it).  Minimal = no already-found forcing set is a subset.  This is
    prime-implicant enumeration: for a conjunction the sole forcing set is every
    conjunct; for a disjunction each arm is its own single-literal set.  Returned
    smallest-first.
    """

    def _eval(assignment: dict[str, Any]) -> bool | None:
        try:
            return bool(satisfies(condition.evaluate(_SnapView(assignment))))
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
    partial: dict[str, Any], snap: Mapping[str, Any], ctx: Any
) -> list[ActionPair] | None:
    """Resolve each ``read == value`` literal to its steerable ``(phys, value)``.

    ``None`` if any read resolves to no steerable driver (the assignment is
    undrivable) or two literals demand conflicting values of one driver.
    """
    holds: dict[str, Any] = {}
    for read, value in partial.items():
        resolved = _resolve_steerable_driver(read, value, snap, ctx)
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
) -> list[ActionPair] | None:
    """Cheapest drivable coordinated holds that force *condition* to satisfy.

    Enumerates the reads' finite domains (capped like ``table_oracle``), finds
    the minimal forcing assignments, and among the drivable ones prefers (a)
    fewest levers that differ from the current snapshot, then (b) fewest levers
    total.  ``None`` when no assignment is drivable — the honest decline the
    single-read path made for a missing lever, now generalized to conjunctions.
    """
    from pyrung.core.analysis.pilot.table_oracle import _MAX_COMBOS, _MAX_FREE_INDICES

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

    sets = _minimal_forcing_sets(condition, order, domains, satisfies)
    if not sets:
        return None

    def _rank(partial: dict[str, Any]) -> tuple[int, int]:
        changes = sum(1 for t, v in partial.items() if not _values_match(snap.get(t), v))
        return (changes, len(partial))

    for partial in sorted(sets, key=_rank):
        holds = _resolve_partial(partial, snap, ctx)
        if holds:
            return holds
    return None


def break_guard_holds(rung_obj: Any, snap: Mapping[str, Any], ctx: Any) -> list[ActionPair] | None:
    """Minimal drivable lever set that forces *rung_obj*'s enable guard FALSE.

    The **suppression dual** of the accumulator arm's satisfy-the-reset
    enumeration: the same :func:`_best_forcing_holds` machinery with the polarity
    inverted (``satisfies=lambda v: not v`` instead of ``bool``).  Used to
    *suppress* a clobbering writer — force its guard false so the deviated
    register keeps the value the pulse established.

    Returns coordinated ``(phys, value)`` holds, or ``None`` when the guard is
    unreadable/undrivable — no reads, an unknown (live-word) domain, or no
    drivable forcing assignment.  ``None`` is the **punt signal** the caller
    escalates to the skiff on.  Rejection stays over COMPLETE finite domains only
    (inherited from ``_best_forcing_holds`` / ``_read_domains``); it never
    fabricates a hold it cannot read.
    """
    from pyrung.core.analysis.pdg import _extract_reads_from_condition

    guard = rung_obj._get_combined_condition()
    if guard is None:
        return None
    reads = _extract_reads_from_condition(guard, {})
    if not reads:
        return None
    return _best_forcing_holds(guard, reads, snap, ctx, satisfies=lambda evaluated: not evaluated)


class _SnapView:
    """Minimal ``ConditionView`` over a dict — just enough to evaluate a reset or
    advance condition over a trial assignment (``Condition.evaluate`` only calls
    ``get_tag(name, default)``)."""

    def __init__(self, snap: Mapping[str, Any]):
        self._snap = snap

    def get_tag(self, name: str, default: Any = None) -> Any:
        return self._snap.get(name, default)
