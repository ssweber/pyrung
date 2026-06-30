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
) -> bool:
    """Generalized terminal let-run: coast toward the *global* target while
    holding the current macro-state.

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

    def _reached(s: Any) -> bool:
        return _values_match(s.tags.get(target_tag), target_value)

    def _ejected(s: Any) -> bool:
        return any(not _values_match(s.tags.get(t), start[t]) for t in role_tags)

    # Conditional holds animate via runner-native reactive breakpoints (patch on
    # an off-target guard); steady holds were already forced by ``_install_holds``.
    # The coast folds (``fold=True``) regardless: a fold cannot skip a scan the
    # oscillator must run, because an active oscillator patches a *visible* change
    # every scan and so ends every plateau, while a dormant one emits no change
    # and folding the dwell is sound.  This is the single mechanism for "hold
    # heading and let scans pass" — the live zoom and the investigation replay
    # coast identically, so a replay reproduces the live zoom.
    handles = _install_reactive_holds(plc, conditional) if conditional else []
    handles.append(plc.when(_ejected).pause())
    try:
        plc.run_until(_reached, max_cycles=budget, fold=True)
    finally:
        for h in handles:
            h.remove()
    return _values_match(plc.state.tags.get(target_tag), target_value)


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


def _install_holds(
    plc: PLC,
    holds: list[tuple[str, Any]],
    forced_holds: dict[str, Any],
) -> None:
    """Force hold inputs on *plc*, skipping already-held ones.

    Conditional holds are recorded in ``forced_holds`` but NOT forced — a steady
    force can't animate them; the coast reads them back and drives them per scan.
    A conditional hold for a tag that already carries one **merges its rules**
    (see :func:`_merge_hold`) so liveness polarities accumulate across rounds
    rather than the second one being dropped as "already held".
    """
    for hold_tag, hold_val in holds:
        if isinstance(hold_val, ConditionalHold):
            forced_holds[hold_tag] = _merge_hold(forced_holds.get(hold_tag), hold_val)
            logger.info("pilot: conditional-hold %s=%r", hold_tag, forced_holds[hold_tag])
            continue
        if hold_tag not in forced_holds:
            forced_holds[hold_tag] = hold_val
            plc.force(hold_tag, hold_val)
            logger.info("pilot: hold %s=%r", hold_tag, hold_val)


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
