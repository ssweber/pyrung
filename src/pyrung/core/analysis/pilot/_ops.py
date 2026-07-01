"""Low-level PLC manipulation helpers shared across pilot modules.

Pure operational primitives — state-key projection, hold installation,
pulse application, delayed-effect settlement.  Depend only on the PLC
interface and prove/absorb (lazily), never on pilot loop logic, verify
gates, or investigation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)

_DebugFn = Callable[[str], None]


@dataclass(frozen=True)
class _HoldRule:
    """One guarded drive: force the held input to ``value`` while the guard holds.

    The guard reads ``guard_tag`` and compares it to ``guard_value`` with
    ``guard_op`` (``"ne"`` / ``"eq"``).  For liveness the guard is the input's own
    *off-target* test — drive ``value`` while ``tag != value`` — so the rule
    fires only when the input has drifted to the dangerous polarity.
    """

    value: Any
    guard_tag: str
    guard_op: str  # "ne" | "eq"
    guard_value: Any

    def active(self, snap: Mapping[str, Any]) -> bool:
        cur = snap.get(self.guard_tag)
        return cur != self.guard_value if self.guard_op == "ne" else cur == self.guard_value


@dataclass(frozen=True)
class ConditionalHold:
    """A hold that drives its tag *while* a guard holds, instead of pinning it.

    Replaces the dwell-guessing ``LivenessHold``.  Carried as the *value* of a
    ``(tag, value)`` hold pair so it flows through the same plumbing as steady
    holds, but ``_install_holds`` skips forcing it and the coast evaluates the
    rules each scan, forcing the value of the first active rule.

    Liveness is two complementary rules under one hold — "drive True while
    ``!= True``" and "drive False while ``!= False``".  Their guards are mutually
    exclusive, so the input alternates each scan: the oscillation a complement-
    reset watchdog needs, with **no dwell** to guess.  Both rules live in one
    hold value, so a single ``forced_holds`` entry per tag still suffices.
    """

    rules: tuple[_HoldRule, ...]

    def value_for(self, snap: Mapping[str, Any]) -> tuple[bool, Any]:
        """``(active, value)`` for this scan — the first rule whose guard holds."""
        for rule in self.rules:
            if rule.active(snap):
                return True, rule.value
        return False, None


def _rule_guard(rule: _HoldRule) -> tuple[str, str, Any]:
    """Identity of a rule's *guard* — the condition under which it fires."""
    return (rule.guard_tag, rule.guard_op, rule.guard_value)


def _merge_hold(existing: Any, new: Any) -> Any:
    """Compose a prior hold value with a freshly-proposed one for the same tag.

    Two :class:`ConditionalHold`\\ s compose **by guard**:

    * a rule with a *new* guard is **added** — this is what makes liveness
      round-by-round, accumulating one polarity per round: round 1 contributes
      "drive True while != True", round 2 (after that hold re-ejects on the
      complement watchdog) contributes "drive False while != False", and the
      merged hold oscillates.
    * a rule with a guard already present **supersedes** it (latest evidence
      wins) rather than leaving a dead rule shadowed behind the earlier one,
      since ``value_for`` returns the first active rule.

    Any other pairing keeps the new value: a :class:`ConditionalHold` supersedes
    a stale steady force for the same tag (the revert re-installs from
    ``forced_holds``, so the steady force does not linger), and steady holds do
    not accumulate.
    """
    if isinstance(existing, ConditionalHold) and isinstance(new, ConditionalHold):
        by_guard: dict[tuple[str, str, Any], _HoldRule] = {}
        order: list[tuple[str, str, Any]] = []
        for rule in (*existing.rules, *new.rules):
            guard = _rule_guard(rule)
            if guard not in by_guard:
                order.append(guard)
            by_guard[guard] = rule  # later rule supersedes an earlier same-guard one
        return ConditionalHold(rules=tuple(by_guard[g] for g in order))
    return new


def _split_holds(
    holds: list[tuple[str, Any]],
) -> tuple[list[tuple[str, Any]], dict[str, ConditionalHold]]:
    """Partition a hold list into steady ``(tag, value)`` pairs and conditional holds."""
    steady: list[tuple[str, Any]] = []
    conditional: dict[str, ConditionalHold] = {}
    for tag, val in holds:
        if isinstance(val, ConditionalHold):
            conditional[tag] = val
        else:
            steady.append((tag, val))
    return steady, conditional


def _reactive_guard(ch: ConditionalHold) -> Callable[[Any], bool]:
    """Predicate: some rule of *ch* is active in the post-scan state."""
    return lambda s: ch.value_for(s.tags)[0]


def _reactive_patch(plc: PLC, tag: str, ch: ConditionalHold) -> Callable[[Any], None]:
    """After-scan side effect: patch *tag* to the first active rule's value."""

    def _act(s: Any) -> None:
        active, value = ch.value_for(s.tags)
        if active:
            plc.patch({tag: value})

    return _act


def _install_reactive_holds(plc: PLC, conditional: Mapping[str, ConditionalHold]) -> list[Any]:
    """Register a runner-native reactive oscillator per conditional hold.

    Each :class:`ConditionalHold` becomes a ``when(<rule active>).do(patch)``
    breakpoint: after every committed scan where a rule's guard holds, the held
    input is **patched** (one-shot) — not forced — to that rule's value.  Using
    ``patch`` lets the program drift the tag between asserts; the reactive
    re-assert fires only when the input has drifted off-target.  That is what
    makes the coast fold-safe: an active oscillator patches a *visible* change
    every scan and so ends every plateau, while a dormant one emits no change
    and folding the dwell is sound.

    An eager first assertion (mirroring the old force-drive's pre-step pass)
    patches the active rule for the coast's opening scan.  Returns the breakpoint
    handles for the caller to remove when the coast ends.
    """
    handles = [
        plc.when(_reactive_guard(ch)).do(_reactive_patch(plc, tag, ch))
        for tag, ch in conditional.items()
    ]
    for tag, ch in conditional.items():
        active, value = ch.value_for(plc.state.tags)
        if active:
            plc.patch({tag: value})
    return handles


# A zoom/coast gets a generous budget of its own — timer dwell is waiting, not
# searching, so it does not consume the pilot's iteration budget.
_ZOOM_BUDGET = 10_000


def _coast_to_value(
    plc: PLC,
    governing_tag: str | None,
    target_value: Any,
    *,
    budget: int = _ZOOM_BUDGET,
) -> bool:
    """Coast *plc* forward (folding) until ``governing_tag == target_value``.

    Installs a pause-guard that stops immediately if the governing tag ejects
    to an unexpected value (neither its start value nor the target).  This is
    the single mechanism for "hold heading and let scans pass": the live zoom
    (``steer``) and the investigation replay (``investigate``) both coast
    through timer dwell identically, so a replay reproduces the live zoom.

    Returns ``True`` if the target value was reached (no ejection).
    """
    if governing_tag is None:
        return False

    def _reached(s: Any) -> bool:
        return _values_match(s.tags.get(governing_tag), target_value)

    start = plc.state.tags.get(governing_tag)

    def _ejected(s: Any) -> bool:
        cur = s.tags.get(governing_tag)
        return not _values_match(cur, start) and not _values_match(cur, target_value)

    guard = plc.when(_ejected).pause()
    try:
        plc.run_until(_reached, max_cycles=budget, fold=True)
    finally:
        guard.remove()
    return _values_match(plc.state.tags.get(governing_tag), target_value)


def _coast_holding_state(
    plc: PLC,
    target_tag: str,
    target_value: Any,
    role_tags: tuple[str, ...],
    *,
    conditional: dict[str, ConditionalHold] | None = None,
    budget: int = _ZOOM_BUDGET,
    reached_fn: Callable[[Any], bool] | None = None,
) -> bool:
    """Generalized terminal let-run: coast toward the *global* target while
    holding the current macro-state.

    *reached_fn* overrides the stop condition — supply it for a **relational**
    target (``Temp >= 5.0``), where the goal is the predicate holding, not the
    register hitting an exact ``target_value``.  Defaults to exact-value match.

    Heading is the global target itself — no intermediate bearing or governing
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
    # are already rungs from ``fork_with_holds``.  Both run every scan under the
    # fold below — the single mechanism for "hold heading and let scans pass",
    # identical for the live zoom and the investigation replay coast.
    if conditional:
        _add_conditional_hold_rungs(plc, conditional)
    guard = plc.when(_ejected).pause()
    try:
        if conditional:
            # Active-hold soak: an oscillating hold (watchdog pet, liveness toggle)
            # must run every scan, so the runner fold can't skip the dwell — the
            # oscillation breaks every plateau, and the dt-knob would over-advance
            # the very timer the oscillation keeps reset.  cycle_fold_until folds
            # the limit cycle the engineer's way — patch the soak accumulator
            # forward by whole periods and step the remainder at normal dt — so the
            # sub-cycle is preserved and the landing is bit-equal to scan-by-scan.
            from pyrung.core.analysis.pilot.cyclefold import cycle_fold_until

            cycle_fold_until(plc, _reached, budget=budget)
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


def _hold_guard_condition(plc: PLC, guard_tag: str, guard_op: str, guard_value: Any) -> Any | None:
    """The Condition under which a :class:`_HoldRule` drives — ``guard_tag`` (n)eq value."""
    from pyrung.core.condition import CompareEq, CompareNe

    tag = plc._known_tags_by_name.get(guard_tag)
    if tag is None:
        return None
    return CompareNe(tag, guard_value) if guard_op == "ne" else CompareEq(tag, guard_value)


def _conditional_hold_rung(plc: PLC, tag_name: str, ch: ConditionalHold) -> Any | None:
    """Build the holds rung for one :class:`ConditionalHold`.

    One rule → a self-releasing guarded copy (``with Rung(guard): copy(value)``);
    multiple (mutually-exclusive) rules → one multi-branch oscillator rung whose
    branch guards read the **rung-entry snapshot**, so the polarities stay
    mutually exclusive with no mid-scan chaining.  ``None`` if the held tag isn't
    in the index.
    """
    from pyrung.core.synthesis import conditional_hold_rung, copy_hold_rung

    dest = plc._known_tags_by_name.get(tag_name)
    if dest is None:
        return None
    rules = [
        (r.value, _hold_guard_condition(plc, r.guard_tag, r.guard_op, r.guard_value))
        for r in ch.rules
    ]
    if len(rules) == 1:
        value, guard = rules[0]
        return copy_hold_rung(value=value, dest=dest, guard=guard)
    return conditional_hold_rung(dest=dest, rules=rules)


def _set_synth_holds(plc: PLC, rungs: list[Any]) -> None:
    """Replace the plc's synthesis holds overlay and invalidate the derived caches."""
    from pyrung.core.synthesis import Synthesis

    if plc._synthesis is None:
        plc._synthesis = Synthesis()
    plc._synthesis.holds = rungs
    plc._fold_context_cache = None
    plc._compiled_replay_kernel = None
    plc._soft_exec_program_cache = None


def _sync_holds(plc: PLC, forced_holds: Mapping[str, Any]) -> None:
    """(Re)build the plc's *steady* hold rungs from the authoritative registry.

    A steady hold becomes a ``copy_hold_rung`` (drive the input every scan) in the
    synthesis holds overlay — the rung form of the old steady force, but visible to
    fold / compile / causal and carried across ``fork()`` as a program reference.
    Conditional holds are **coast-only**: recorded in the registry (rule-merged) but
    installed as rungs by :func:`_coast_holding_state` when a coast begins, so the
    main working PLC does not oscillate them during ordinary drive.  Rebuilt from
    the registry each call (the dict dedups by tag), so re-installs are idempotent.

    A held tag missing from the index keeps the old steady *force* as a fallback
    (logged), so a hold is never silently dropped.
    """
    from pyrung.core.synthesis import copy_hold_rung

    rungs: list[Any] = []
    for tag_name, val in forced_holds.items():
        if isinstance(val, ConditionalHold):
            continue  # coast-only (installed by _coast_holding_state)
        dest = plc._known_tags_by_name.get(tag_name)
        if dest is None:
            plc.force(tag_name, val)
            logger.info("pilot: hold %s=%r (force fallback — tag not in index)", tag_name, val)
            continue
        rungs.append(copy_hold_rung(value=val, dest=dest, guard=None))
    _set_synth_holds(plc, rungs)


def _install_holds(
    plc: PLC,
    holds: list[tuple[str, Any]],
    forced_holds: dict[str, Any],
) -> None:
    """Merge *holds* into the registry and (re)install the steady-hold rungs.

    Steady holds are driven by ``copy_hold_rung`` rungs in the synthesis holds
    overlay (the rung form of the old force — see :func:`_sync_holds`).  Conditional
    holds accumulate in the registry (rule-merged so liveness polarities stack
    across rounds) and are installed as rungs per coast, not here.
    """
    for hold_tag, hold_val in holds:
        if isinstance(hold_val, ConditionalHold):
            forced_holds[hold_tag] = _merge_hold(forced_holds.get(hold_tag), hold_val)
            logger.info("pilot: conditional-hold %s=%r", hold_tag, forced_holds[hold_tag])
        elif hold_tag not in forced_holds:
            forced_holds[hold_tag] = hold_val
            logger.info("pilot: hold %s=%r", hold_tag, hold_val)
    _sync_holds(plc, forced_holds)


def _add_conditional_hold_rungs(plc: PLC, conditional: Mapping[str, ConditionalHold]) -> None:
    """Append conditional-hold rungs to the plc's holds overlay (the coast install).

    The guarded / oscillating rung form of the old reactive ``when().do()`` — each
    self-drives every scan its guard holds.  The fold steps each scan, so an active
    oscillator ends every plateau exactly as the reactive patch did.
    """
    extra = [
        rung
        for tag, ch in conditional.items()
        if (rung := _conditional_hold_rung(plc, tag, ch)) is not None
    ]
    if not extra:
        return
    from pyrung.core.synthesis import Synthesis

    if plc._synthesis is None:
        plc._synthesis = Synthesis()
    plc._synthesis.holds = [*plc._synthesis.holds, *extra]
    plc._fold_context_cache = None
    plc._compiled_replay_kernel = None
    plc._soft_exec_program_cache = None


def fork_with_holds(source: PLC, forced_holds: Mapping[str, Any]) -> PLC:
    """Fork *source* and re-establish PILOT's steady holds on the fork.

    ``fork()`` is a clean state copy, so every speculative fork re-installs the
    steady holds from the authoritative registry — the single seam that does it.
    Holds are now :func:`_sync_holds` rungs in the fork's synthesis overlay (not
    forces): they steer the input pre-logic and survive the fork as a program
    reference.  Callers layer per-trial holds/pulses on top; conditional (reactive)
    holds are installed as rungs by :func:`_coast_holding_state` during a coast.
    """
    fork = source.fork()
    _sync_holds(fork, forced_holds)
    return fork


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
        from pyrung.core.analysis.prove.absorb import _done_acc_state
        from pyrung.core.analysis.prove.results import PENDING

        cur_snap = dict(fork.state.tags)
        pending_tts: list[str] = []
        for spec in cfg.done_specs:
            done_name = cfg.stateful_names[spec.index]
            old = _done_acc_state(
                spec.kind, before_snap.get(done_name), before_snap.get(spec.acc_name)
            )
            new = _done_acc_state(spec.kind, cur_snap.get(done_name), cur_snap.get(spec.acc_name))
            if new == PENDING and old != PENDING:
                tt_name = done_name.rsplit("_Done", 1)[0] + "_TT"
                if cur_snap.get(tt_name) is True:
                    pending_tts.append(tt_name)

        if pending_tts:
            fork.run_until(
                lambda s: all(not s.tags.get(tt) for tt in pending_tts),
                max_cycles=budget,
                fold=True,
            )


def _has_pending_effects(fork: PLC) -> bool:
    """True if the fork has pending harness feedback or active analog profiles."""
    harness = getattr(fork, "_harness", None)
    if harness is None:
        return False
    if harness.pending_count > 0:
        return True
    for c in getattr(harness, "_profile_couplings", ()):
        if c.active:
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


def _hold_allowed(ctx: Any, pair: tuple[str, Any]) -> bool:
    tag, _value = pair
    compass = getattr(ctx, "compass", None)
    action_tags = getattr(compass, "action_tags", frozenset())
    return tag not in action_tags and _route_allowed(ctx, pair)
