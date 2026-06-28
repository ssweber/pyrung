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
class LivenessHold:
    """A hold that must *oscillate*, not pin — the complement of a steady hold.

    Some prerequisites are satisfied only by a *changing* input: a watchdog
    (``on_delay`` reset by an input, ``rotate.py`` R10/R11) trips if that input
    sits at either polarity too long.  A steady force can never satisfy it; the
    input must dwell True for ``on_dwell`` scans, then False for ``off_dwell``
    scans, repeating.  Carried as the *value* of a ``(tag, value)`` hold pair so
    it flows through the same plumbing as steady holds, but ``_install_holds``
    skips forcing it and the coast animates it instead.

    Mirrors ``on_delay``/``off_delay``: ``on_dwell`` bounds the True phase (kept
    under the watchdog that resets on the False edge), ``off_dwell`` the False
    phase (under the watchdog that resets on the True edge).
    """

    on_dwell: int
    off_dwell: int

    def value_at(self, scan: int) -> bool:
        """The polarity this hold should drive at absolute scan index *scan*."""
        period = max(1, self.on_dwell + self.off_dwell)
        return (scan % period) < self.on_dwell


def _split_holds(
    holds: list[tuple[str, Any]],
) -> tuple[list[tuple[str, Any]], dict[str, LivenessHold]]:
    """Partition a hold list into steady ``(tag, value)`` pairs and liveness holds."""
    steady: list[tuple[str, Any]] = []
    liveness: dict[str, LivenessHold] = {}
    for tag, val in holds:
        if isinstance(val, LivenessHold):
            liveness[tag] = val
        else:
            steady.append((tag, val))
    return steady, liveness


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
    liveness: dict[str, LivenessHold] | None = None,
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

    # With liveness holds the coast can't fold — a folded scan would freeze the
    # animated input, tripping the very watchdog the hold exists to satisfy.  So
    # step one scan at a time, driving each liveness input to its scheduled
    # polarity, until target / ejection / budget.
    if liveness:
        for tag, lh in liveness.items():
            plc.force(tag, lh.value_at(plc.state.scan_id))
        for _ in range(budget):
            plc.step()
            for tag, lh in liveness.items():
                plc.force(tag, lh.value_at(plc.state.scan_id))
            if _reached(plc.state) or _ejected(plc.state):
                break
        return _values_match(plc.state.tags.get(target_tag), target_value)

    guard = plc.when(_ejected).pause()
    try:
        plc.run_until(_reached, max_cycles=budget, fold=True)
    finally:
        guard.remove()
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

    Liveness holds are recorded in ``forced_holds`` but NOT forced — a steady
    force can't animate them; the coast reads them back and toggles per scan.
    """
    for hold_tag, hold_val in holds:
        if hold_tag not in forced_holds:
            forced_holds[hold_tag] = hold_val
            if isinstance(hold_val, LivenessHold):
                logger.info("pilot: liveness-hold %s=%r", hold_tag, hold_val)
                continue
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
