"""Shared PLC operations and action-admission helpers for PILOT.

The module projects search/world keys, installs guarded input rungs, forks PLC
state, applies pulses, settles delayed effects, and adapts common coasts to
``CoastReceipt`` results. It also contains the shared avoid and hold/route
admission checks used at execution boundaries.

It does not choose candidates, judge trial outcomes, or manage checkpoints and
reverts.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from pyrsistent import pvector

from pyrung.core.analysis.pilot.coast import CoastReceipt

if TYPE_CHECKING:
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PilotRung:
    """One scoped piece of PILOT steering.

    ``guard`` is deliberately required: steering without a reason to release is
    a permanent force wearing ladder syntax.  The proposer owns this condition;
    installation only preserves its meaning and order.
    """

    dest: str
    value: Any
    guard: Any

    def __post_init__(self) -> None:
        if self.guard is None:
            raise ValueError("PilotRung.guard is required")


def _until_unresolved_condition(plc: PLC, atom: Any) -> Any:
    """Lower a trace completion ``Atom`` to its still-unresolved condition."""
    from pyrung.core.condition import (
        CompareEq,
        CompareGe,
        CompareGt,
        CompareLe,
        CompareLt,
        CompareNe,
    )
    from pyrung.core.crossing import Cmp, Eq

    tag = plc._known_tags_by_name.get(atom.tag)
    if tag is None:
        # Static block ranges are intentionally lazy in the runner's tag
        # inventory. An advance profile still owns concrete Tag objects for its
        # channels, so use that authoritative channel metadata for the guard.
        from pyrung.core.analysis.pilot.advance import build_advance_index

        owner = build_advance_index(plc.program, getattr(plc, "_harness", None)).resolve(atom.tag)
        if owner is not None:
            tag = next(
                (channel for channel in owner.profile.channels if channel.name == atom.tag),
                None,
            )
    if tag is None:
        raise KeyError(f"pilot rung guard tag {atom.tag!r} is not a program tag")
    if isinstance(atom, Eq):
        if len(atom.values) != 1:
            raise ValueError("a multi-value advance boundary cannot scope a PilotRung")
        return CompareNe(tag, next(iter(atom.values)))
    if isinstance(atom, Cmp):
        operand = (
            plc._known_tags_by_name.get(str(atom.bound), atom.bound)
            if atom.bound_is_tag
            else atom.bound
        )
        inverse = {
            "==": CompareNe,
            "!=": CompareEq,
            "<": CompareGe,
            "<=": CompareGt,
            ">": CompareLe,
            ">=": CompareLt,
            "eq": CompareNe,
            "ne": CompareEq,
            "lt": CompareGe,
            "le": CompareGt,
            "gt": CompareLe,
            "ge": CompareLt,
        }.get(atom.op)
        if inverse is None:
            raise ValueError(f"advance predicate {atom.op!r} cannot scope a PilotRung")
        return inverse(tag, operand)
    form = atom.form
    operand = atom.operand
    if form in ("xic", "truthy"):
        return CompareEq(tag, False)
    if form == "xio":
        return CompareEq(tag, True)
    inverse = {
        "eq": CompareNe,
        "ne": CompareEq,
        "lt": CompareGe,
        "le": CompareGt,
        "gt": CompareLe,
        "ge": CompareLt,
    }.get(form)
    if inverse is None:
        raise ValueError(f"trace predicate {form!r} cannot scope a PilotRung")
    return inverse(tag, operand)


def _atom_condition(plc: PLC, atom: Any) -> Any:
    """Lower a trace ``Atom`` to the condition it states, without inversion."""
    from pyrung.core.condition import (
        CompareEq,
        CompareGe,
        CompareGt,
        CompareLe,
        CompareLt,
        CompareNe,
    )
    from pyrung.core.tag import Bool

    tag = plc._known_tags_by_name.get(atom.tag)
    if tag is None:
        raise KeyError(f"pilot rung guard tag {atom.tag!r} is not a program tag")
    form = atom.form
    operand = atom.operand
    if form in ("xic", "truthy"):
        return tag
    if form == "xio":
        return ~tag
    if form == "eq" and isinstance(tag, Bool) and isinstance(operand, bool):
        return tag if operand else ~tag
    direct = {
        "eq": CompareEq,
        "ne": CompareNe,
        "lt": CompareLt,
        "le": CompareLe,
        "gt": CompareGt,
        "ge": CompareGe,
    }.get(form)
    if direct is None:
        raise ValueError(f"trace predicate {form!r} cannot guard a PilotRung")
    return direct(tag, operand)


def _target_unresolved_condition(
    plc: PLC,
    target_tag: str,
    target_value: Any,
    target_predicate: Any = None,
) -> Any:
    """The honest outer lifetime for a target-directed corrective rung."""
    if target_predicate is not None:
        return _until_unresolved_condition(plc, target_predicate)
    from pyrung.core.condition import CompareNe

    tag = plc._known_tags_by_name.get(target_tag)
    if tag is None:
        raise KeyError(f"pilot target guard tag {target_tag!r} is not a program tag")
    return CompareNe(tag, target_value)


def _rungs_from_proposals(
    plc: PLC,
    proposals: list[Any],
    scope: Any,
) -> list[PilotRung]:
    """Normalize pair-shaped or ``PilotRung`` proposals to scoped pilot rungs."""
    result: list[PilotRung] = []
    for proposal in proposals:
        if isinstance(proposal, PilotRung):
            result.append(proposal)
            continue
        dest, proposed = proposal
        result.append(PilotRung(dest, proposed, scope))
    return result


def _set_rungs(plc: PLC, rungs: Iterable[PilotRung]) -> None:
    """Replace PILOT's overlay from its ordered, guarded rung records."""
    from pyrung.core.synthesis import guarded_copy_rung

    rules: list[tuple[Any, Any, Any]] = []
    for rung in rungs:
        dest = plc._known_tags_by_name.get(rung.dest)
        if dest is None:
            raise KeyError(f"pilot rung destination {rung.dest!r} is not a program tag")
        rules.append((dest, rung.value, rung.guard))
    _set_synth_holds(plc, [guarded_copy_rung(rules)] if rules else [])


def _append_rungs(
    plc: PLC,
    proposed: list[PilotRung],
    rungs: Iterable[PilotRung],
) -> Any:
    """Append new evidence and install the resulting ordered overlay.

    The returned persistent vector is the new world value.  Mutating a plain
    list remains supported for the low-level public seam and older callers, but
    PILOT itself always assigns the returned value into ``_World.rungs``.
    """
    updated_list = list(rungs)
    seen = {
        (rung.dest, _semantic_key(rung.value), _semantic_key(rung.guard)) for rung in updated_list
    }
    for rung in proposed:
        identity = (rung.dest, _semantic_key(rung.value), _semantic_key(rung.guard))
        if identity not in seen:
            updated_list.append(rung)
            seen.add(identity)
    if isinstance(rungs, list):
        list_rungs = cast(list[PilotRung], rungs)
        list_rungs[:] = updated_list
        updated = list_rungs
    else:
        updated = pvector(updated_list)
    _set_rungs(plc, list(updated))
    return pvector(updated)


def fork_with_rungs(source: PLC, rungs: Iterable[PilotRung]) -> PLC:
    """Fork *source* and rebuild its scoped steering overlay verbatim."""
    fork = source.fork()
    _set_rungs(fork, rungs)
    return fork


# A zoom/coast gets a generous budget of its own — timer dwell is waiting, not
# searching, so it does not consume the pilot's iteration budget.
_ZOOM_BUDGET = 10_000


def _coast_to_value(
    plc: PLC,
    channel_tag: str | None,
    target_value: Any,
    *,
    budget: int = _ZOOM_BUDGET,
    session: Any = None,
) -> CoastReceipt:
    """Coast *plc* forward (folding) until ``channel_tag == target_value``.

    Arms two bumps — the target and a departure (the channel leaving its
    start value for anything but the target) — so the coast lands on the
    exact scan either fires and the receipt says which.  This is the single
    mechanism for "hold heading and let scans pass": the live zoom
    (``steer``) and the investigation replay (``investigate``) both coast
    through timer dwell identically, so a replay reproduces the live zoom.

    *conditional* holds animate during the coast exactly as in
    :func:`_coast_holding_state` — a confirmed oscillation corrective (a
    watchdog pet) that only the terminal let-run animated would silently drop
    out of every coast, re-tripping the watchdog it exists to feed.

    ``receipt.reached`` is the legacy bool ("target reached, no ejection").
    """
    from pyrung.core.analysis.pilot.coast import (
        TARGET,
        CoastSession,
        departure_bump,
        value_bump,
    )

    if channel_tag is None:
        return CoastReceipt(
            kind=session.kind if session is not None else "zoom",
            start_scan=plc.state.scan_id,
            end_scan=plc.state.scan_id,
            stop_reason="skipped",
            fired=(),
            events=(),
            budget=0,
        )

    start = plc.state.tags.get(channel_tag)
    bumps = [
        value_bump(plc, "target", TARGET, channel_tag, target_value),
        departure_bump(
            plc,
            "ejected",
            {channel_tag: start},
            excluding={channel_tag: target_value},
        ),
    ]
    if session is None:
        session = CoastSession(plc, kind="zoom")
    assert session.plc is plc
    return session.seek(bumps, budget=budget)


def _coast_holding_state(
    plc: PLC,
    target_tag: str,
    target_value: Any,
    role_tags: tuple[str, ...],
    *,
    budget: int = _ZOOM_BUDGET,
    reached_fn: Callable[[Any], bool] | None = None,
    session: Any = None,
) -> CoastReceipt:
    """Generalized terminal let-run: coast toward the *global* target while
    holding the current macro-state.

    *reached_fn* overrides the stop condition — supply it for a **relational**
    target (``Temp >= 5.0``), where the goal is the predicate holding, not the
    register hitting an exact ``target_value``.  Defaults to exact-value match.

    Heading is the global target itself — no intermediate bearing or channel
    register is assumed.  The ejection guard is "the macro-state I am parked in
    changed on its own": any recognized state-machine role register
    (``role_tags``) leaving the value it held at coast start pauses the coast at
    that scan, so an ejection (Execute -> Aborting) hands a tight incident to
    investigation instead of burning the whole budget.

    With no roles (a program without a recognized state machine) the departure
    bump never fires and the coast simply runs to the target or the budget —
    still safe.

    ``receipt.reached`` is the legacy bool ("target reached, no ejection").
    """
    from pyrung.core.analysis.pilot.coast import (
        TARGET,
        CoastSession,
        departure_bump,
        predicate_bump,
        value_bump,
    )

    # Conditional holds become guarded / oscillating rungs in the coast fork's
    # holds overlay (the rung form of the old reactive breakpoints); steady holds
    # are already rungs from ``fork_with_rungs``.  Both run every scan under the
    # session's fold dispatch — the single mechanism for "hold heading and let
    # scans pass", identical for the live zoom and the investigation replay coast.
    if reached_fn is not None:
        # Relational target: the predicate is the goal; opaque to the fold by
        # design (plateau guard + watched-tag protection only).
        target = predicate_bump("target", TARGET, reached_fn, watched=(target_tag,))
    else:
        target = value_bump(plc, "target", TARGET, target_tag, target_value)

    bumps = [target]
    if role_tags:
        start = {t: plc.state.tags.get(t) for t in role_tags}
        bumps.append(departure_bump(plc, "ejected", start))
    if session is None:
        session = CoastSession(plc, kind="letrun")
    assert session.plc is plc
    return session.seek(bumps, budget=budget)


_THRESHOLD_DOWN_KINDS = frozenset({"count_down", "int_down", "real_down"})
_THRESHOLD_FORM_GT = "gt"


@dataclass(frozen=True)
class _StateKeyConfig:
    """Projection dimensions for the pilot state key.

    When built from the prover's ``_ExploreContext``, ``stateful_names``
    contains every cross-scan tag, ``done_specs`` carries the Done-bit
    three-valued abstraction, ``threshold_vector_specs`` carries
    accumulator crossing vectors, and ``acc_indices`` marks raw
    accumulator positions to mask.

    When the prover pipeline is unavailable, the fallback uses
    ``pivot_tags`` from the trace tree with empty absorption specs.
    """

    stateful_names: tuple[str, ...]
    done_specs: tuple[Any, ...]
    threshold_vector_specs: tuple[Any, ...]
    acc_indices: frozenset[int]


def _threshold_crossed_snap(
    snap: dict[str, Any],
    kind: str,
    acc_name: str,
    threshold: int | float | str,
    form: str,
) -> bool:
    """Threshold-vector bit from a PLC snapshot (mirrors kernel._threshold_crossed)."""
    acc_value = snap.get(acc_name)
    threshold_value = snap.get(threshold) if isinstance(threshold, str) else threshold
    if (
        type(acc_value) is bool
        or type(threshold_value) is bool
        or not isinstance(acc_value, (int, float))
        or not isinstance(threshold_value, (int, float))
    ):
        return False
    if kind in _THRESHOLD_DOWN_KINDS:
        acc_value = -acc_value
        threshold_value = -threshold_value
    if form == _THRESHOLD_FORM_GT:
        return acc_value > threshold_value
    return acc_value >= threshold_value


def _pilot_state_key(snap: dict[str, Any], cfg: _StateKeyConfig) -> tuple[Any, ...]:
    """Project a PLC snapshot onto the state key dimensions."""
    parts: list[Any] = list(map(snap.get, cfg.stateful_names))
    if cfg.done_specs:
        from pyrung.core.analysis.prove.absorb import _done_acc_state

        for spec in cfg.done_specs:
            parts[spec.index] = _done_acc_state(
                spec.kind, parts[spec.index], snap.get(spec.acc_name)
            )
    for idx in cfg.acc_indices:
        parts[idx] = None
    for spec in cfg.threshold_vector_specs:
        parts.append(
            tuple(
                _threshold_crossed_snap(snap, spec.kind, spec.acc_name, atom.threshold, atom.form)
                for atom in spec.atoms
            )
        )
    return tuple(parts)


def wait_edge_nogood(channel_tag: str, from_value: Any, to_value: Any) -> tuple[str, Any]:
    """The world-keyed nogood for a completion (WAIT) edge that proved sterile.

    A completion edge carries no action, so the ordinary ``(tag, value)``
    action nogood can never name it.  This synthetic pair — keyed by the
    channel and the exact ``from -> to`` claim — lets a rejected wait be
    remembered at its world key and filtered out of the next iteration's route
    query, exactly like a failed press.
    """
    return (f"wait::{channel_tag}", (from_value, to_value))


def _semantic_key(value: Any) -> Any:
    """A stable, hashable identity for a rung operand or guard.

    Conditions deliberately compare by identity, which is right while executing
    a ladder but wrong for search identity: rebuilding ``State != Execute`` must
    describe the same world.  Keep only semantic public fields and normalize tag
    references by name; source locations and derived caches are intentionally
    absent.
    """
    from enum import Enum

    from pyrung.core.tag import ImmediateRef, Tag

    if value is None or isinstance(value, bool | int | float | str | bytes):
        return value
    if isinstance(value, Tag):
        return ("tag", value.name)
    if isinstance(value, ImmediateRef):
        return ("immediate", _semantic_key(value.value))
    if isinstance(value, Enum):
        return (type(value).__module__, type(value).__qualname__, value.name)
    if isinstance(value, tuple | list):
        return tuple(_semantic_key(item) for item in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((_semantic_key(item) for item in value), key=repr))
    if isinstance(value, dict):
        return tuple(
            sorted(
                ((_semantic_key(k), _semantic_key(v)) for k, v in value.items()),
                key=repr,
            )
        )
    attrs = getattr(value, "__dict__", None)
    if attrs is not None:
        semantic = tuple(
            (name, _semantic_key(member))
            for name, member in sorted(attrs.items())
            if not name.startswith("_") and name not in {"source_file", "source_line"}
        )
        return (type(value).__module__, type(value).__qualname__, semantic)
    return (type(value).__module__, type(value).__qualname__, str(value))


def _pilot_world_key(
    snap: dict[str, Any],
    cfg: _StateKeyConfig,
    rungs: Any,
) -> tuple[Any, ...]:
    """Identity of an executable PILOT world: PLC projection plus PilotRungs."""
    rung_key = tuple(
        (rung.dest, _semantic_key(rung.value), _semantic_key(rung.guard)) for rung in rungs
    )
    return (_pilot_state_key(snap, cfg), rung_key)


def _set_synth_holds(plc: PLC, rungs: list[Any]) -> None:
    """Replace the plc's synthesis holds overlay and invalidate the derived caches."""
    from pyrung.core.synthesis import Synthesis

    if plc._synthesis is None:
        plc._synthesis = Synthesis()
    plc._synthesis.holds = rungs
    plc._fold_context_cache = None
    plc._compiled_replay_kernel = None
    plc._soft_exec_program_cache = None


def _apply_pulse(
    plc: PLC,
    actions: list[tuple[str, Any]],
    resting: dict[str, Any],
    edge_tags: set[str],
    session: Any = None,
) -> int:
    """Apply *actions* with rising-edge semantics where needed.

    Returns the number of scans consumed.  *session*, when given, records the
    pulse onto that session's timeline (pens ticked after every raw scan, the
    settle dwell run on the session) so a Done that fires inside the pulse
    window is a recorded pen mark, not history-only.
    """
    from pyrung.core.analysis.pilot.coast import LIMITS, CoastSession

    if session is None:
        session = CoastSession(plc, kind="pulse")
    assert session.plc is plc

    patch = {t: v for t, v in actions}
    needs_edge = any(t in edge_tags for t in patch)

    if needs_edge:
        release = {t: resting.get(t, False) for t in patch if t in edge_tags}
        if release:
            plc.patch(release)
            plc.step()
            session.note_pens()

    plc.patch(patch)
    plc.step()
    session.note_pens()

    # Fixed settle window — the one waiting shape with no predicate (an
    # explicit dwell, never disguised as a bump).
    session.dwell(LIMITS.pulse_settle_scans)

    return 6 if needs_edge else 5


def _settle_delayed_effects(
    fork: PLC,
    before_snap: dict[str, Any],
    cfg: _StateKeyConfig | None,
    *,
    scan_budget: int = 2000,
    session: Any = None,
) -> list[CoastReceipt]:
    """Fast-forward *fork* past pending timers and harness feedback.

    Two chained seeks on one session (each with an honest stop_reason),
    plus the one-scan plant-latency dwell between them:

    Phase 1 — harness feedback: if the harness has scheduled patches
    (Physical on_delay/off_delay), seek harness quiescence
    (``pending_count == 0``), then dwell one scan — the plant commits
    feedback the scan it settles; the program that reads it reacts the
    *next* scan (the scan boundary is the plant latency).

    Phase 2 — timer accumulation: if any Timer/Counter done-bit moved
    ``False → PENDING``, seek every pending timing (TT) bit clear (folding
    past the ticks).
    """
    from pyrung.core.analysis.pilot.coast import QUIESCENT, CoastSession, predicate_bump

    budget = scan_budget
    receipts: list[CoastReceipt] = []
    if session is None:
        session = CoastSession(fork, kind="delayed-effects")
    assert session.plc is fork

    harness = getattr(fork, "_harness", None)
    if harness is not None and harness.pending_count > 0:
        scan_before = fork.state.scan_id
        receipt = session.seek(
            [
                predicate_bump(
                    "harness_quiescent",
                    QUIESCENT,
                    lambda s: harness.pending_count == 0,
                )
            ],
            budget=budget,
        )
        receipts.append(receipt)
        if harness.pending_count == 0 and fork.state.scan_id - scan_before < budget:
            session.dwell(1)
        budget -= fork.state.scan_id - scan_before

    if cfg is not None and budget > 0:
        from pyrung.core.analysis.pilot.advance import iter_advance_owners

        program = fork.program
        cur_snap = dict(fork.state.tags)
        pending_tts: list[str] = []
        if program is not None:
            for owner in iter_advance_owners(program, harness):
                # Resolve the timing (TT) register semantically off the owning
                # instruction's profile — never by name surgery on the done bit,
                # which silently misses any timer not named ``<base>_Done``.
                active = owner.profile.active
                tt_name = getattr(active, "name", None)
                if (
                    tt_name is not None
                    and cur_snap.get(tt_name) is True
                    and before_snap.get(tt_name) is not True
                ):
                    pending_tts.append(tt_name)

        if pending_tts:
            receipts.append(
                session.seek(
                    [
                        predicate_bump(
                            "tt_clear",
                            QUIESCENT,
                            lambda s: all(not s.tags.get(tt) for tt in pending_tts),
                            watched=tuple(pending_tts),
                        )
                    ],
                    budget=budget,
                )
            )
    return receipts


def _has_pending_effects(fork: PLC) -> bool:
    """True if the fork has unsettled harness feedback.

    Bool dwell reports via ``pending_count``; an analog coupling is "pending"
    while its enable is active — its plant rung is still driving the feedback
    register this scan.
    """
    harness = getattr(fork, "_harness", None)
    if harness is None:
        return False
    if harness.pending_count > 0:
        return True
    snap = fork.current_state.tags
    for c in getattr(harness, "_profile_couplings", ()):
        en_raw = snap.get(c.en_name, False)
        enabled = en_raw == c.trigger_value if c.trigger_value is not None else bool(en_raw)
        if enabled:
            return True
    return False


# ---------------------------------------------------------------------------
# Hold policy — whether a proposed (tag, value) hold is allowed for this ctx.
# Pure duck-typed reads off the pilot context (no imports); shared by
# investigation's precise-cause walk and the enabler-correction arms so neither
# has to depend on the other.
# ---------------------------------------------------------------------------


def _route_allowed(ctx: Any, pair: tuple[str, Any]) -> bool:
    route_allowed = getattr(ctx, "route_allowed", None)
    return bool(route_allowed(pair)) if route_allowed is not None else True


def _avoid_snap_names(avoid: Any, snap: dict[str, Any]) -> tuple[str, ...]:
    """Names of the avoid conditions *snap* trips (``()`` when avoid is None).

    A ``_AvoidPredicate`` reports its violated member names; a bare callable
    (someone passed ``avoid_pred=`` a raw predicate) reports a generic name.
    """
    if avoid is None:
        return ()
    violated = getattr(avoid, "violated", None)
    if violated is not None:
        try:
            return tuple(violated(snap))
        except Exception:
            return ()
    try:
        return ("avoided condition",) if bool(avoid(snap)) else ()
    except Exception:
        return ()


def _avoid_violations(
    ctx: Any,
    pairs: list[tuple[str, Any]] | tuple[tuple[str, Any], ...],
    snapshot: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    """Names of the avoid conditions that *pairs* would force.

    Static: overlays each ``(tag, value)`` onto *snapshot* (or the resting
    baseline when no snapshot is given — the neutral world a hold asserts its
    tag against) and evaluates the avoid predicate.  This is the action gate's
    primitive (a candidate/hold whose overlay trips avoid depends on it).
    """
    avoid = getattr(ctx, "avoid_pred", None)
    if avoid is None:
        return ()
    base = dict(snapshot) if snapshot is not None else dict(getattr(ctx, "resting", {}) or {})
    for tag, value in pairs:
        base[tag] = value
    return _avoid_snap_names(avoid, base)


def _avoid_forces(
    ctx: Any,
    pairs: list[tuple[str, Any]] | tuple[tuple[str, Any], ...],
    snapshot: dict[str, Any] | None = None,
) -> bool:
    return bool(_avoid_violations(ctx, pairs, snapshot))


def _hold_allowed(ctx: Any, pair: tuple[str, Any]) -> bool:
    tag, _value = pair
    compass = getattr(ctx, "compass", None)
    action_tags = getattr(compass, "action_tags", frozenset())
    if tag in action_tags or not _route_allowed(ctx, pair):
        return False
    # A hold that drives an avoided tag is a path that depends on it — inadmissible.
    return not _avoid_forces(ctx, [pair])
