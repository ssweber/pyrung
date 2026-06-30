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

import logging
from collections.abc import Mapping
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
    #   polarities[phys] — the resetting polarities its watchdogs need (the toggle
    #     must visit each, so each becomes a guarded rule).
    #   fired — inputs whose accumulator actually completed this incident; only
    #     these get a correction (keeps it incident-relevant).
    polarities: dict[str, set[bool]] = {}
    fired: set[str] = set()
    for profile, _instr in iter_profiles(program):
        reset = profile.reset
        if reset is None:
            continue
        reset_reads = _extract_reads_from_condition(reset, {})
        if len(reset_reads) != 1:
            continue
        read_tag = next(iter(reset_reads))
        reset_val = _resetting_polarity(reset, read_tag, after)
        if not isinstance(reset_val, bool):
            continue
        resolved = _resolve_steerable_driver(read_tag, reset_val, after, ctx)
        if resolved is None:
            continue
        phys, polarity = resolved
        if not isinstance(polarity, bool) or not _hold_allowed(ctx, (phys, polarity)):
            continue
        polarities.setdefault(phys, set()).add(polarity)
        if profile.done.name in changed:
            fired.add(phys)

    for phys in sorted(fired):
        pols = sorted(polarities.get(phys, set()))
        if not pols:
            continue
        rules = tuple(
            _HoldRule(value=pol, guard_tag=phys, guard_op="ne", guard_value=pol) for pol in pols
        )
        corrections.append(
            EnablerCorrection(
                correction=Correction.OSCILLATE,
                kind="liveness",
                holds=((phys, ConditionalHold(rules=rules)),),
                sources=(phys,),
                detail=f"oscillate {phys} between {pols} (complement-reset watchdog)",
            )
        )

    # Inputs already owned by an oscillation rule must not also get a (contrary)
    # steady cannot-hold.
    osc_inputs = set(polarities)

    def _emit_cannot_hold(match: AccumulatorMatch, *, threshold: int | None, why: str) -> None:
        for hold in _cannot_hold_pairs(match.profile, after, ctx):
            phys, value = hold
            if phys in osc_inputs or not _hold_allowed(ctx, hold):
                continue
            detail = f"stop holding {phys}={value!r} ({why}"
            scans = scans_to_eject(match, plc, threshold=threshold)
            if scans is not None:
                detail += f" in ~{scans} scans"
            corrections.append(
                EnablerCorrection(
                    correction=Correction.FREEZE,
                    kind="done-boundary",
                    holds=(hold,),
                    sources=(match.profile.done.name, phys),
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
    try:
        tree = trace_back(
            read_tag,
            value,
            dict(snap),
            pdg,
            program,
            steerable,
            opaque_loop=getattr(ctx, "opaque_loop", frozenset()),
            pipeline_internal_tags=getattr(ctx, "pipeline_internal_tags", frozenset()),
            route=getattr(ctx, "route", None),
            prior=getattr(ctx, "domain_prior", None),
        )
    except Exception:  # noqa: BLE001
        return None
    leaves = list(tree.steerable_leaves())
    return leaves[0] if leaves else None


def _advance_stop_polarity(profile: Any, read_tag: str, snap: Mapping[str, Any]) -> bool | None:
    """The value of *read_tag* that makes ``profile.advance`` *stop* advancing
    the accumulator (evaluates to ``!= advance_value``)."""
    advance = profile.advance
    if advance is None:
        return None
    for value in (True, False):
        try:
            evaluated = advance.evaluate(_SnapView({**snap, read_tag: value}))
        except Exception:  # noqa: BLE001
            return None
        if evaluated != profile.advance_value:
            return value
    return None


def _cannot_hold_pairs(profile: Any, snap: Mapping[str, Any], ctx: Any) -> list[tuple[str, Any]]:
    """Steerable holds that *stop* a single-read accumulator from advancing.

    Resolves the advancing input to its steerable driver and the polarity that
    breaks advancement.  Multi-read advance conditions are skipped (no single
    unambiguous lever) — they degrade to the other hypothesis families.
    """
    from pyrung.core.analysis.pdg import _extract_reads_from_condition

    if profile.advance is None:
        return []
    reads = _extract_reads_from_condition(profile.advance, {})
    if len(reads) != 1:
        return []
    read_tag = next(iter(reads))
    stop_val = _advance_stop_polarity(profile, read_tag, snap)
    if not isinstance(stop_val, bool):
        return []
    resolved = _resolve_steerable_driver(read_tag, stop_val, snap, ctx)
    return [resolved] if resolved is not None else []


class _SnapView:
    """Minimal ``ConditionView`` over a dict — just enough to evaluate a reset
    condition's resetting polarity (``Condition.evaluate`` only calls
    ``get_tag(name, default)``)."""

    def __init__(self, snap: Mapping[str, Any]):
        self._snap = snap

    def get_tag(self, name: str, default: Any = None) -> Any:
        return self._snap.get(name, default)


def _resetting_polarity(
    reset_condition: Any, read_tag: str, base_snap: Mapping[str, Any]
) -> bool | None:
    """The value of *read_tag* that *satisfies* the reset condition (resets the
    watchdog), evaluated over *base_snap* so any other reads still resolve."""
    for value in (True, False):
        try:
            if reset_condition.evaluate(_SnapView({**base_snap, read_tag: value})):
                return value
        except Exception:  # noqa: BLE001
            return None
    return None
