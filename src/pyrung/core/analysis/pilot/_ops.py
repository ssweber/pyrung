"""Low-level PLC manipulation helpers shared across pilot modules.

Pure operational primitives — state-key projection, hold installation,
pulse application, delayed-effect settlement.  Depend only on the PLC
interface and prove/absorb (lazily), never on pilot loop logic, verify
gates, or investigation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)

_DebugFn = Callable[[str], None]


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

    tag = plc._known_tags_by_name.get(atom.tag)
    if tag is None:
        raise KeyError(f"pilot rung guard tag {atom.tag!r} is not a program tag")
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
    """Lower legacy investigation proposals at the boundary to scoped rungs.

    This is intentionally the sole transitional seam while correction producers
    move from pair-shaped hypotheses to ``PilotRung`` directly.
    """
    result: list[PilotRung] = []
    for proposal in proposals:
        if isinstance(proposal, PilotRung):
            result.append(proposal)
            continue
        dest, proposed = proposal
        result.append(PilotRung(dest, proposed, scope))
    return result


def _set_rungs(plc: PLC, rungs: list[PilotRung]) -> None:
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
    rungs: list[PilotRung],
) -> None:
    """Append new evidence and install the resulting ordered overlay."""
    rungs.extend(proposed)
    _set_rungs(plc, rungs)


def fork_with_rungs(source: PLC, rungs: list[PilotRung]) -> PLC:
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
) -> bool:
    """Coast *plc* forward (folding) until ``channel_tag == target_value``.

    Installs a pause-guard that stops immediately if the channel tag ejects
    to an unexpected value (neither its start value nor the target).  This is
    the single mechanism for "hold heading and let scans pass": the live zoom
    (``steer``) and the investigation replay (``investigate``) both coast
    through timer dwell identically, so a replay reproduces the live zoom.

    *conditional* holds animate during the coast exactly as in
    :func:`_coast_holding_state` — a confirmed oscillation corrective (a
    watchdog pet) that only the terminal let-run animated would silently drop
    out of every corridor coast, re-tripping the watchdog it exists to feed.

    Returns ``True`` if the target value was reached (no ejection).
    """
    if channel_tag is None:
        return False

    def _reached(s: Any) -> bool:
        return _values_match(s.tags.get(channel_tag), target_value)

    start = plc.state.tags.get(channel_tag)

    def _ejected(s: Any) -> bool:
        cur = s.tags.get(channel_tag)
        return not _values_match(cur, start) and not _values_match(cur, target_value)

    active_rungs = bool(plc._synthesis is not None and plc._synthesis.holds)
    guard = plc.when(_ejected).pause()
    scan_before = plc.state.scan_id
    try:
        if active_rungs:
            # Active-hold soak: the oscillation must run every scan, so the
            # runner fold can't skip the dwell — same rationale as the
            # terminal let-run's conditional branch.
            from pyrung.core.analysis.pilot.cyclefold import cycle_fold_until

            stats: dict[str, int] = {}
            cycle_fold_until(plc, _reached, budget=budget, stats=stats)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "coast_to_value %s==%r: %d scan-ids in %d real scans, %d folds",
                    channel_tag,
                    target_value,
                    plc.state.scan_id - scan_before,
                    stats.get("real_scans", 0),
                    stats.get("folds", 0),
                )
        else:
            plc.run_until(_reached, max_cycles=budget, fold=True)
    finally:
        guard.remove()
    return _values_match(plc.state.tags.get(channel_tag), target_value)


def _coast_holding_state(
    plc: PLC,
    target_tag: str,
    target_value: Any,
    role_tags: tuple[str, ...],
    *,
    budget: int = _ZOOM_BUDGET,
    reached_fn: Callable[[Any], bool] | None = None,
) -> bool:
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

    With no roles (a program without a recognized state machine) the guard never
    fires and the coast simply runs to the target or the budget — still safe.

    Returns ``True`` if the target value was reached (no ejection).
    """
    start = {t: plc.state.tags.get(t) for t in role_tags}

    _reached = reached_fn or (lambda s: _values_match(s.tags.get(target_tag), target_value))

    def _ejected(s: Any) -> bool:
        return any(not _values_match(s.tags.get(t), start[t]) for t in role_tags)

    # Conditional holds become guarded / oscillating rungs in the coast fork's
    # holds overlay (the rung form of the old reactive breakpoints); steady holds
    # are already rungs from ``fork_with_rungs``.  Both run every scan under the
    # fold below — the single mechanism for "hold heading and let scans pass",
    # identical for the live zoom and the investigation replay coast.
    active_rungs = bool(plc._synthesis is not None and plc._synthesis.holds)
    guard = plc.when(_ejected).pause()
    scan_before = plc.state.scan_id
    try:
        if active_rungs:
            # Active-hold soak: an oscillating hold (watchdog pet, liveness toggle)
            # must run every scan, so the runner fold can't skip the dwell — the
            # oscillation breaks every plateau, and the dt-knob would over-advance
            # the very timer the oscillation keeps reset.  cycle_fold_until folds
            # the limit cycle the engineer's way — patch the soak accumulator
            # forward by whole periods and step the remainder at normal dt — so the
            # sub-cycle is preserved and the landing is bit-equal to scan-by-scan.
            from pyrung.core.analysis.pilot.cyclefold import cycle_fold_until

            stats: dict[str, int] = {}
            cycle_fold_until(plc, _reached, budget=budget, stats=stats)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "coast_holding_state %s==%r: %d scan-ids in %d real scans, %d folds",
                    target_tag,
                    target_value,
                    plc.state.scan_id - scan_before,
                    stats.get("real_scans", 0),
                    stats.get("folds", 0),
                )
        else:
            # Pure soak / steady holds: the runner fold (dt-knob through plateaus)
            # already handles this.
            plc.run_until(_reached, max_cycles=budget, fold=True)
    finally:
        guard.remove()
    return bool(_reached(plc.state))


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
) -> int:
    """Apply *actions* with rising-edge semantics where needed.

    Returns the number of scans consumed.
    """
    patch = {t: v for t, v in actions}
    needs_edge = any(t in edge_tags for t in patch)

    if needs_edge:
        release = {t: resting.get(t, False) for t in patch if t in edge_tags}
        if release:
            plc.patch(release)
            plc.step()

    plc.patch(patch)
    plc.step()

    for _ in range(4):
        plc.step()

    return 6 if needs_edge else 5


def _settle_delayed_effects(
    fork: PLC,
    before_snap: dict[str, Any],
    cfg: _StateKeyConfig | None,
    *,
    scan_budget: int = 2000,
) -> None:
    """Fast-forward *fork* past pending timers and harness feedback.

    Phase 1 — harness feedback: if the harness has scheduled patches
    (Physical on_delay/off_delay), ``run_until(pending_count == 0)``.

    Phase 2 — timer accumulation: if any Timer/Counter done-bit moved
    ``False → PENDING``, ``run_until(~TT, fold=True)`` to skip ticks.
    """
    budget = scan_budget

    harness = getattr(fork, "_harness", None)
    if harness is not None and harness.pending_count > 0:
        scan_before = fork.state.scan_id
        fork.run_until(
            lambda s: harness.pending_count == 0,
            max_cycles=budget,
        )
        # The plant commits feedback the scan it settles; the program that reads
        # that feedback reacts the *next* scan (the scan boundary is the plant
        # latency).  Advance once more so the settled feedback's downstream
        # program effect is visible — what the caller is fast-forwarding *to*.
        if harness.pending_count == 0 and fork.state.scan_id - scan_before < budget:
            fork.step()
        budget -= fork.state.scan_id - scan_before

    if cfg is not None and cfg.done_specs and budget > 0:
        from pyrung.core.analysis.pilot.accumulators import resolve_profile
        from pyrung.core.analysis.prove.absorb import _done_acc_state
        from pyrung.core.analysis.prove.results import PENDING

        program = fork.program
        cur_snap = dict(fork.state.tags)
        pending_tts: list[str] = []
        for spec in cfg.done_specs:
            done_name = cfg.stateful_names[spec.index]
            old = _done_acc_state(
                spec.kind, before_snap.get(done_name), before_snap.get(spec.acc_name)
            )
            new = _done_acc_state(spec.kind, cur_snap.get(done_name), cur_snap.get(spec.acc_name))
            if new == PENDING and old != PENDING:
                # Resolve the timing (TT) register semantically off the owning
                # instruction's profile — never by name surgery on the done bit,
                # which silently misses any timer not named ``<base>_Done``.
                match = (
                    resolve_profile(done_name, program, harness) if program is not None else None
                )
                timing = getattr(match.profile, "timing", None) if match is not None else None
                tt_name = getattr(timing, "name", None)
                if tt_name is not None and cur_snap.get(tt_name) is True:
                    pending_tts.append(tt_name)

        if pending_tts:
            fork.run_until(
                lambda s: all(not s.tags.get(tt) for tt in pending_tts),
                max_cycles=budget,
                fold=True,
            )


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
