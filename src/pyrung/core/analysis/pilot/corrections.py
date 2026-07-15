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

from pyrung.core.analysis.pilot._ops import (
    PilotRung,
    _hold_allowed,
)
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
    corrections: list[EnablerCorrection] = [
        *_coil_corrections(plc, incident, ctx),
        *_accumulator_corrections(plc, incident, ctx),
    ]
    return [c for c in corrections if c.correction is not Correction.NONE and c.holds]


# ---------------------------------------------------------------------------
# Exposure lifetime — the guard is read off the antagonist rungs themselves
# ---------------------------------------------------------------------------

_EXPOSURE_DEPTH = 3


def _is_state_read(tag: str, opaque_loop: frozenset[str], pdg: Any) -> bool:
    """Whether *tag* is channel-ish: an opaque-loop register or a one-hop alias.

    The alias test is the ``sm_MapVal2State`` idiom: every writer of the tag is
    gated by an opaque-loop read (``S_Starting`` written under
    ``S_StateCurrent == STARTINGREF``).  Bounded to one hop and a handful of
    writers — anything wider is not a state alias and must not be treated as
    context.
    """
    if tag in opaque_loop:
        return True
    writer_idxs = pdg.writers_of.get(tag, frozenset())
    if not writer_idxs or len(writer_idxs) > 4:
        return False
    return all(pdg.rung_nodes[ri].condition_reads & opaque_loop for ri in writer_idxs)


def _state_conjuncts(ro: Any, opaque_loop: frozenset[str], pdg: Any) -> list[Any]:
    """Top-level conjuncts of *ro* whose every read is channel-ish.

    These are the ladder's own statement of *where* this rung can fire —
    ``Or(S_Starting, S_Unholding, S_Unsuspending)`` on a door alarm.  Branch
    conditions are not visited: top-level conjuncts are necessary conditions,
    so the projection can only be at least as wide as the true exposure.
    """
    from pyrung.core.analysis.pdg import _extract_reads_from_condition

    out: list[Any] = []
    for cond in tuple(getattr(ro, "_conditions", ()) or ()):
        reads = _extract_reads_from_condition(cond, {})
        if reads and all(_is_state_read(r, opaque_loop, pdg) for r in reads):
            out.append(cond)
    return out


def _silenced_by(ro: Any, assignment: Mapping[str, Any]) -> bool:
    """The correction assignment provably prevents *ro* from firing.

    Proof = some top-level conjunct reads only assigned tags and evaluates
    falsy under them (``~i_DoorClosed`` with the door held True).  A conjunct
    with unassigned reads proves nothing; fail closed.
    """
    for cond in tuple(getattr(ro, "_conditions", ()) or ()):
        from pyrung.core.analysis.pdg import _extract_reads_from_condition

        reads = _extract_reads_from_condition(cond, {})
        if not reads or not reads <= set(assignment):
            continue
        try:
            if not bool(cond.evaluate(_SnapView(assignment))):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _exposure_guard(
    assignment_pairs: tuple[ActionPair, ...],
    incident: DeviationIncident,
    ctx: Any,
) -> Any | None:
    """The evidence-derived lifetime of a corrective input assignment.

    WWTD: keep the door closed *wherever the door alarm can trip* — and the
    ladder names those places itself.  An **antagonist** is a rung the
    correction assignment provably silences whose consequence reaches the
    machine: it latched an incident alarm, or its writes flow into the opaque
    channel pipeline (a Hold/Abort command copy).  A warning lamp that reads
    the same input qualifies for neither and contributes nothing.

    The guard is the Or, over every silenced antagonist's qualifying
    consequence chain (bounded depth), of the state conjuncts collected along
    that chain (And within a chain — each gate must pass for the consequence
    to propagate).  A qualifying chain with *no* state conjunct means the
    exposure is unconditional: return ``None`` and let the caller keep the
    legacy pair shape (landing-scoped downstream) rather than invent a wider
    guard than the ladder states.
    """
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.condition import AllCondition, AnyCondition
    from pyrung.core.instruction.coils import LatchInstruction

    maybe_pdg = getattr(ctx, "pdg", None)
    maybe_program = getattr(ctx, "program", None)
    opaque_loop = getattr(ctx, "opaque_loop", frozenset())
    if maybe_pdg is None or maybe_program is None or not opaque_loop or not assignment_pairs:
        return None
    pdg: Any = maybe_pdg
    program: Any = maybe_program
    assignment = dict(assignment_pairs)

    fired_latches = {
        tag
        for tag, val in incident.after_snap.items()
        if val is True and incident.before_snap.get(tag) is not True
    }

    def _machine_write(node: Any, ro: Any) -> bool:
        """This rung is itself the damage: it latched an incident alarm or
        writes an opaque-loop register directly."""
        if any(isinstance(i, LatchInstruction) for i in ro._instructions) and (
            set(node.all_writes) & fired_latches
        ):
            return True
        return bool(set(node.all_writes) & opaque_loop)

    def _slice_reaches_machine(node: Any) -> bool:
        return any(
            pdg.downstream_slice(written, follow_calls=True) & opaque_loop
            for written in node.all_writes
        )

    def _chain_contributions(
        ri: int, conjuncts: tuple[Any, ...], depth: int, seen: frozenset[int]
    ) -> list[tuple[Any, ...]] | None:
        """Qualifying chains' conjunct tuples from rung *ri* onward.

        A chain settles at a **machine write** (fired latch / direct opaque
        write): with gates collected it contributes them; with none it returns
        ``None`` and poisons the whole derivation — a context-free antagonist
        means no honest state guard exists.  A chain that has collected a gate
        *and* provably flows on toward the machine settles early on those
        gates; a gate-less chain keeps walking for the gate and is discarded
        (fail closed) if the depth budget runs out first.
        """
        node = pdg.rung_nodes[ri]
        ro = resolve_rung(program, node)
        if ro is None:
            return []
        here = (*conjuncts, *_state_conjuncts(ro, opaque_loop, pdg))
        if _machine_write(node, ro):
            if not here:
                return None
            return [here]
        if here and _slice_reaches_machine(node):
            return [here]
        if depth >= _EXPOSURE_DEPTH:
            return []
        found: list[tuple[Any, ...]] = []
        for written in sorted(node.all_writes):
            for reader in sorted(pdg.readers_of.get(written, frozenset())):
                if reader in seen:
                    continue
                sub = _chain_contributions(reader, here, depth + 1, seen | {reader})
                if sub is None:
                    return None
                found.extend(sub)
        return found

    contributions: list[tuple[Any, ...]] = []
    seen_rungs: set[int] = set()
    for tag in sorted(assignment):
        for ri in sorted(pdg.readers_of.get(tag, frozenset())):
            if ri in seen_rungs:
                continue
            seen_rungs.add(ri)
            ro = resolve_rung(program, pdg.rung_nodes[ri])
            if ro is None or not _silenced_by(ro, assignment):
                continue
            chains = _chain_contributions(ri, (), 0, frozenset({ri}))
            if chains is None:
                return None
            contributions.extend(chains)

    if not contributions:
        return None
    terms: list[Any] = []
    seen_terms: set[tuple[int, ...]] = set()
    for chain in contributions:
        key = tuple(sorted(id(c) for c in chain))
        if key in seen_terms:
            continue
        seen_terms.add(key)
        terms.append(chain[0] if len(chain) == 1 else AllCondition(*chain))
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

    def _latch_guard_holds(tag: str) -> tuple[list[ActionPair], list[ActionPair]]:
        """Corrective steerable holds for an active latch *tag*, or [].

        Returns ``(holds, image_pairs)``: the resolved steerable levers plus
        the image-level ``(guard, safe)`` assignments the trace bridged them
        from (``i_DoorClosed=True`` for a ``x_DoorClosed`` lever) — the reads
        the antagonist rungs actually consume, already derived by the same
        ``trace_back`` that produced the holds.
        """
        holds: list[ActionPair] = []
        image_pairs: list[ActionPair] = []
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
            for guard in sorted(condition_tags - state_tags):
                cur = incident.after_snap.get(guard)
                if not isinstance(cur, bool):
                    continue
                resolved = [
                    hold
                    for hold in _steerable_holds(guard, not cur)
                    if hold not in seen and _hold_allowed(ctx, hold)
                ]
                if resolved:
                    image_pairs.append((guard, not cur))
                for hold in resolved:
                    seen.add(hold)
                    holds.append(hold)
        return holds, image_pairs

    def _guarded(holds: list[ActionPair], image_pairs: list[ActionPair]) -> tuple[Any, ...]:
        """Wrap holds into exposure-guarded rungs; keep pairs when unreadable.

        The silencing assignment is the image pairs the trace already bridged
        plus the levers themselves — consumers read either level.
        """
        assignment = tuple(dict((*image_pairs, *holds)).items())
        guard = _exposure_guard(assignment, incident, ctx)
        if guard is None:
            return tuple(holds)
        return tuple(PilotRung(tag, value, guard) for tag, value in holds)

    corrections: list[EnablerCorrection] = []
    conjunction: list[ActionPair] = []
    conj_seen: set[ActionPair] = set()
    conj_latches: list[str] = []
    conj_image: list[ActionPair] = []
    conj_image_seen: set[ActionPair] = set()
    for tag, val in incident.after_snap.items():
        if val is not True or incident.before_snap.get(tag) is True:
            continue
        latch_holds, latch_image = _latch_guard_holds(tag)
        if not latch_holds:
            continue
        corrections.append(
            EnablerCorrection(
                correction=Correction.FLIP,
                kind="latch-exposure",
                holds=_guarded(latch_holds, latch_image),
                sources=(tag, *(h[0] for h in latch_holds)),
                detail=f"latch {tag} fired during incident",
            )
        )
        conj_latches.append(tag)
        for hold in latch_holds:
            if hold not in conj_seen:
                conj_seen.add(hold)
                conjunction.append(hold)
        for pair in latch_image:
            if pair not in conj_image_seen:
                conj_image_seen.add(pair)
                conj_image.append(pair)

    if len(conjunction) > 1:
        corrections.append(
            EnablerCorrection(
                correction=Correction.FLIP,
                kind="latch-exposure",
                holds=_guarded(conjunction, conj_image),
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
      :class:`PilotRung`, one rule per resetting polarity.  (This is the old
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
                clear_only=getattr(ctx, "clear_only", frozenset()),
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
