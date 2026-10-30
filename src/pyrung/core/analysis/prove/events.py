"""Hidden-event scheduling helpers for prove BFS.

Timers/counters accumulate over many scans but the BFS would revisit the
same PENDING state repeatedly.  The event scheduler accelerates this:

1. ``_scans_until_done_event`` / ``_scans_until_threshold_event`` —
   compute scans to next crossing from the per-scan delta.
2. ``_advance_hidden_progress`` — fast-forward accumulator by skipped
   scans.
3. ``_settle_pending`` — cascade: resolve nearest event, re-check,
   repeat (bounded by event count).  Abstract threshold branches that
   arm later exact timers must keep settling until no exact pending
   work remains.
4. ``_maybe_jump_hidden_event`` — when BFS revisits a known PENDING
   state, jump directly to the crossed successor.
5. ``_stop_point_outcome`` — the plateau partway to that crossing where an
   input that starts another timer would race a running one to a tie.

Stop points (races between timers started at different times):
A jump skips every plateau between the node and the crossing, so an input
flip is only ever tried at the node and at the crossing.  When the flip
starts another timer, *when* it comes decides which of the two crosses
first: ``Rung(A): latch(L1)``, ``Rung(B): latch(L2)``, ``T1`` (100) and
``T3`` (120) under ``L1``, ``T2`` (50) under ``L2``; ``T2`` lands between
the other two only when ``B`` comes while ``T1`` is between 50 and 70.
Within one key the running accumulators advance together, so the order of
future crossings changes only where a running source and an idle one would
cross on the same scan: ``s_i - m_j`` scans ahead, for a source ``i`` that
crosses in ``s_i`` scans and an idle one ``j`` that needs ``m_j`` scans once
started.  The BFS gets a plateau node on each side of every such tie (and on
it), for a start that takes up to ``_STOP_START_DELAY`` scans to reach the
idle accumulator, and tries every input flip there as at any node.

Abstract thresholds (dynamic presets):
``_materialize_abstract_threshold_outcome`` creates a representative
crossed state without knowing the concrete preset value.
Counterexamples that depend on this representative witness surface a
caveat because replaying ``TraceStep.inputs`` alone may not reproduce
the violation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import product as _product
from typing import TYPE_CHECKING, Any

from pyrung.core.fold import _carry_run, _timer_grid, _timer_step
from pyrung.core.kernel import ReplayKernel

from .absorb import (
    _DONE_KIND_COUNT_DOWN,
    _DONE_KIND_COUNT_UP,
    _DONE_KIND_OFF_DELAY,
    _DONE_KIND_ON_DELAY,
    _DONE_KIND_TIME_DRUM,
    _PROGRESS_KIND_INT_DOWN,
    _PROGRESS_KIND_INT_UP,
    _PROGRESS_KIND_REAL_DOWN,
    _PROGRESS_KIND_REAL_UP,
    _THRESHOLD_FORM_GE,
    _THRESHOLD_MODE_EXACT,
    _is_numeric_literal,
)
from .kernel import (
    _EdgeCompressor,
    _KernelSnapshot,
    _restore_kernel,
    _snapshot_kernel,
    _step_kernel,
    _threshold_crossed,
    _threshold_value,
)
from .results import PENDING

if TYPE_CHECKING:
    from . import _ExploreContext

# Timer-like done kinds (accumulator carries a fractional remainder) and
# real-valued progress kinds. Hoisted to module-level frozensets so the
# membership tests on the hot hidden-event path don't rebuild set literals.
_TIMER_DELAY_KINDS = frozenset((_DONE_KIND_ON_DELAY, _DONE_KIND_OFF_DELAY, _DONE_KIND_TIME_DRUM))
_REAL_PROGRESS_KINDS = frozenset((_PROGRESS_KIND_REAL_UP, _PROGRESS_KIND_REAL_DOWN))


@dataclass(frozen=True)
class _StateKeyDoneSpec:
    index: int
    acc_name: str
    kind: str
    # ``_frac:<acc>`` memory key for timers: a timer in a unit coarser than
    # the scan (``"sec"`` at 10 ms) runs for many scans on its fractional
    # carry alone, with ``Acc`` still 0.
    frac_key: str | None = None


@dataclass(frozen=True)
class _DoneEventSpec:
    state_index: int
    acc_name: str
    kind: str
    preset: int | str
    preset_memory_key: str | None = None


@dataclass(frozen=True)
class _ThresholdEventSpec:
    vector_index: int
    atom_index: int
    acc_name: str
    kind: str
    threshold: int | float | str
    form: str
    mode: str


@dataclass(frozen=True)
class _HiddenEventOutcome:
    snapshot: _KernelSnapshot
    key: tuple[Any, ...]
    additional_scans: int
    pre_event_snapshot: _KernelSnapshot | None = None
    caveats: tuple[str, ...] = ()
    event_inputs: dict[str, Any] | None = None
    # A plateau partway to a crossing, still at the node's key (see
    # ``_stop_point_outcome``): kept as a node of its own, not as a revisit.
    stop_point: bool = False


@dataclass(frozen=True)
class _PendingSource:
    kind: str
    acc_name: str
    scans: int
    spec_index: int


_SourceKey = tuple[Any, ...]


@dataclass(frozen=True)
class _SimultaneityGroup:
    """A set of exact events that all cross on the same scan.

    Only exact events (known preset, tracked accumulator) carry a real
    scan-count, so only they are partitioned temporally.  Abstract
    thresholds and co-firing witnesses are phase-free and handled by
    materialization instead.
    """

    scans: int
    exact_sources: frozenset[_SourceKey]


@dataclass(frozen=True)
class _EventAdvanceState:
    """Intermediate state after accumulator advance, before the event step."""

    pre_event_snapshot: _KernelSnapshot
    before_snap: _KernelSnapshot
    pre_advance_counter_acc: dict[str, int]
    pending_sources: set[_SourceKey]
    next_event_scans: int
    firing_group: _SimultaneityGroup | None = None


_ABSTRACT_THRESHOLD_TRACE_CAVEAT = (
    "Counterexample trace uses an abstract threshold witness hidden from the BFS state key; "
    "replaying TraceStep.inputs alone may not reproduce the violation.",
)

_EVENT_INPUT_VARIANT_CAVEAT = (
    "Counterexample reached via hidden-event input variant; "
    "the trace inputs on the crossing scan differ from the fast-forwarded scans.",
)

_COFIRE_CAVEAT = (
    "Counterexample reached via a co-firing hidden-event witness; "
    "two pending timers/counters were aligned to cross on the same scan, "
    "which TraceStep.inputs alone may not reproduce.",
)

_HIDDEN_EVENT_CAVEAT = (
    "Transition fast-forwarded over hidden accumulator scans; "
    "concrete replay requires cycling inputs, not a single fixed-input run.",
)


def _merge_caveats(*groups: tuple[str, ...]) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for caveat in group:
            if caveat in seen:
                continue
            seen.add(caveat)
            merged.append(caveat)
    return tuple(merged)


class _HiddenEventCache:
    """Memoize hidden-event outcomes for repeated pending plateaus."""

    __slots__ = (
        "_jump_cache",
        "_settle_cache",
        "_stateful_names",
        "jump_hits",
        "jump_misses",
        "settle_hits",
        "settle_misses",
    )

    def __init__(self, context: _ExploreContext) -> None:
        self._jump_cache: dict[tuple[Any, ...], tuple[_HiddenEventOutcome, ...]] = {}
        self._settle_cache: dict[tuple[Any, ...], tuple[_HiddenEventOutcome, ...]] = {}
        self._stateful_names = frozenset(context.stateful_names)
        self.jump_hits = 0
        self.jump_misses = 0
        self.settle_hits = 0
        self.settle_misses = 0

    def plateau_key(
        self,
        context: _ExploreContext,
        before_snap: _KernelSnapshot,
        kernel: ReplayKernel,
        key: tuple[Any, ...],
    ) -> tuple[Any, ...]:
        """Build a conservative signature for one hidden pending plateau.

        The visible BFS identity lives in *key*.  The extra payload captures
        the hidden progress state that drives event scheduling:

        - the current/previous hidden accumulator values (plus timer fractions)
          for every pending Done/threshold source
        - the current value of any hidden exact-threshold tag

        This keeps memoization sound for repeated ``PENDING`` plateaus that
        share a visited-state key but differ in hidden distance-to-event.
        """

        progress_sources: list[tuple[Any, ...]] = []
        hidden_thresholds: list[tuple[str, Any]] = []
        seen_sources: set[tuple[str, str]] = set()
        seen_thresholds: set[str] = set()

        for spec in context.done_event_specs:
            if key[spec.state_index] != PENDING:
                continue
            source = (spec.kind, spec.acc_name)
            if source in seen_sources:
                continue
            seen_sources.add(source)
            progress_sources.append(
                _hidden_progress_signature(spec.kind, spec.acc_name, before_snap, kernel)
            )
            if spec.preset_memory_key is not None:
                hidden_thresholds.append(
                    (spec.preset_memory_key, kernel.memory.get(spec.preset_memory_key))
                )
            elif (
                isinstance(spec.preset, str)
                and spec.preset not in self._stateful_names
                and spec.preset not in seen_thresholds
            ):
                seen_thresholds.add(spec.preset)
                hidden_thresholds.append((spec.preset, kernel.tags.get(spec.preset)))

        vector_offset = len(context.stateful_names)
        for spec in context.threshold_event_specs:
            vector = key[vector_offset + spec.vector_index]
            if vector[spec.atom_index]:
                continue
            source = (spec.kind, spec.acc_name)
            if source not in seen_sources:
                seen_sources.add(source)
                progress_sources.append(
                    _hidden_progress_signature(spec.kind, spec.acc_name, before_snap, kernel)
                )
            if (
                isinstance(spec.threshold, str)
                and spec.threshold not in self._stateful_names
                and spec.threshold not in seen_thresholds
            ):
                if spec.mode == _THRESHOLD_MODE_EXACT:
                    seen_thresholds.add(spec.threshold)
                    hidden_thresholds.append((spec.threshold, kernel.tags.get(spec.threshold)))
                else:
                    val = kernel.tags.get(spec.threshold)
                    if _is_numeric_literal(val):
                        seen_thresholds.add(spec.threshold)
                        hidden_thresholds.append((spec.threshold, val))

        return (
            key,
            tuple(progress_sources),
            tuple(hidden_thresholds),
        )


def _hidden_progress_signature(
    kind: str,
    acc_name: str,
    before_snap: _KernelSnapshot,
    kernel: ReplayKernel,
) -> tuple[Any, ...]:
    """Capture the hidden progress data that determines jump scheduling."""
    if kind in _TIMER_DELAY_KINDS:
        before_acc = int(before_snap.tags.get(acc_name, 0) or 0)
        after_acc = int(kernel.tags.get(acc_name, 0) or 0)
        before_frac = float(before_snap.memory.get(f"_frac:{acc_name}", 0.0) or 0.0)
        after_frac = float(kernel.memory.get(f"_frac:{acc_name}", 0.0) or 0.0)
        return (kind, acc_name, before_acc, before_frac, after_acc, after_frac)
    if kind in _REAL_PROGRESS_KINDS:
        before_acc = float(before_snap.tags.get(acc_name, 0.0) or 0.0)
        after_acc = float(kernel.tags.get(acc_name, 0.0) or 0.0)
        return (kind, acc_name, before_acc, after_acc)
    before_acc = int(before_snap.tags.get(acc_name, 0) or 0)
    after_acc = int(kernel.tags.get(acc_name, 0) or 0)
    return (kind, acc_name, before_acc, after_acc)


def _timer_total(kernel: ReplayKernel, acc_name: str) -> float:
    """Return timer progress as accumulator plus fractional remainder."""
    frac_key = f"_frac:{acc_name}"
    acc = int(kernel.tags.get(acc_name, 0) or 0)
    frac = float(kernel.memory.get(frac_key, 0.0) or 0.0)
    return acc + frac


# A timer accumulator is an INT that clamps here.
_TIMER_ACC_MAX = 32767


def _timer_reading(snap: _KernelSnapshot | ReplayKernel, acc_name: str) -> tuple[int, float]:
    return (
        int(snap.tags.get(acc_name, 0) or 0),
        float(snap.memory.get(f"_frac:{acc_name}", 0.0) or 0.0),
    )


def _exact_timer_move(
    acc_name: str,
    quantum: float | None,
    before: _KernelSnapshot,
    kernel: ReplayKernel,
) -> tuple[int, float, float] | None:
    """``(acc, frac, quantum)`` when the last scan ran this timer once.

    A timer adds ``quantum + frac`` units a scan, keeps the integer part and
    carries the float remainder (``timers.py``).  When the scan from *before*
    to *kernel* was exactly one such step, later scans repeat it, and the
    carry recurrence (``fold._carry_run``) says where the timer is after any
    number of them, float rounding included: ``acc + frac + k * delta`` in
    one aggregate floors differently, so a jump planned on it lands a scan
    early or late (or, for a quantum far below one unit, nowhere near).
    """
    if quantum is None or not quantum > 0.0:
        return None
    acc_b, frac_b = _timer_reading(before, acc_name)
    acc, frac = _timer_reading(kernel, acc_name)
    if acc >= _TIMER_ACC_MAX:
        return None
    if (acc, frac) != _timer_step(acc_b, frac_b, quantum, _TIMER_ACC_MAX):
        return None
    return acc, frac, quantum


def _timer_after(acc: int, frac: float, quantum: float, scans: int) -> tuple[int, float] | None:
    """The timer's ``(acc, frac)`` after *scans* more steps, or None past the work cap."""
    if scans <= 0:
        return acc, frac
    grid = _timer_grid(frac, quantum)
    if grid is not None:
        big_q, big_f, big_n, g = grid
        total = big_f + scans * big_q
        return min(acc + total // big_n, _TIMER_ACC_MAX), (total % big_n) * g
    steps, acc, frac, _status = _carry_run(acc, frac, quantum, _TIMER_ACC_MAX, scans)
    if steps != scans:
        return None
    return acc, frac


def _timer_scans_to_reach(acc: int, frac: float, quantum: float, target: int) -> int | None:
    """Scans ``k >= 1`` until the accumulator is ``>= target``.

    None when it never gets there (*target* past the clamp) or the search
    ran past the work cap.
    """
    if acc >= target:
        return 1
    if target > _TIMER_ACC_MAX:
        return None
    grid = _timer_grid(frac, quantum)
    if grid is not None:
        big_q, big_f, big_n, _g = grid
        need = (target - acc) * big_n - big_f
        return max(1, -(-need // big_q))
    steps, _acc, _frac, status = _carry_run(acc, frac, quantum, _TIMER_ACC_MAX, None, target)
    return steps if status == "target" else None


def _timer_integer_target(threshold: float, form: str) -> int | None:
    """The least integer accumulator value that crosses ``(threshold, form)``."""
    if not math.isfinite(threshold):
        return None
    return math.ceil(threshold) if form == _THRESHOLD_FORM_GE else math.floor(threshold) + 1


def _resolve_done_preset(
    preset: int | str,
    preset_memory_key: str | None,
    kernel: ReplayKernel,
) -> int | None:
    """Resolve the effective preset for a Done event.

    Dynamic presets are scheduled from the value observed by the owning
    instruction during the most recent scan, not from the tag's post-scan
    value. If that observed value is unavailable, hidden-event jumping must
    decline the branch rather than guess.
    """
    if preset_memory_key is not None:
        resolved = kernel.memory.get(preset_memory_key)
    elif isinstance(preset, str):
        resolved = kernel.tags.get(preset)
    else:
        resolved = preset
    if not _is_numeric_literal(resolved):
        return None
    assert isinstance(resolved, (int, float))
    return int(resolved)


def _scans_until_done_event(
    kind: str,
    preset: int | str,
    preset_memory_key: str | None,
    acc_name: str,
    before: _KernelSnapshot,
    kernel: ReplayKernel,
    quantum: float | None = None,
) -> int | None:
    """Estimate scans until this pending timer/counter reaches its next Done event."""
    resolved_preset = _resolve_done_preset(preset, preset_memory_key, kernel)
    if resolved_preset is None:
        return None
    if kind in _TIMER_DELAY_KINDS:
        move = _exact_timer_move(acc_name, quantum, before, kernel)
        if move is not None:
            acc, frac, q = move
            exact = _timer_scans_to_reach(acc, frac, q, resolved_preset)
            if exact is not None or resolved_preset > _TIMER_ACC_MAX:
                return exact
    acc_before = int(before.tags.get(acc_name, 0) or 0)
    acc_after = int(kernel.tags.get(acc_name, 0) or 0)

    if kind in {_DONE_KIND_ON_DELAY, _DONE_KIND_OFF_DELAY, _DONE_KIND_TIME_DRUM}:
        before_total = acc_before + float(before.memory.get(f"_frac:{acc_name}", 0.0) or 0.0)
        after_total = _timer_total(kernel, acc_name)
        delta = after_total - before_total
        remaining = resolved_preset - after_total
    elif kind == _DONE_KIND_COUNT_UP:
        delta = acc_after - acc_before
        remaining = resolved_preset - acc_after
    else:
        delta = acc_before - acc_after
        remaining = resolved_preset + acc_after

    if delta <= 0:
        return None
    if remaining <= 0:
        return 1
    return max(1, int(math.ceil(remaining / delta)))


def _progress_delta_and_current(
    kind: str,
    acc_name: str,
    before: _KernelSnapshot,
    kernel: ReplayKernel,
) -> tuple[float, float] | None:
    """Return per-scan progress in the same normalized space as threshold vectors.

    ``count_down`` is the easy case to get wrong: raw ``Acc`` becomes more
    negative as progress advances, but threshold vectors and event scheduling
    both reason in monotone progress coordinates, so this function reports
    ``current = -Acc`` and compares against ``-threshold`` downstream.
    """
    if kind in {_DONE_KIND_ON_DELAY, _DONE_KIND_OFF_DELAY, _DONE_KIND_TIME_DRUM}:
        acc_before = int(before.tags.get(acc_name, 0) or 0)
        before_total = acc_before + float(before.memory.get(f"_frac:{acc_name}", 0.0) or 0.0)
        after_total = _timer_total(kernel, acc_name)
        return after_total - before_total, after_total

    if kind in {_DONE_KIND_COUNT_UP, _PROGRESS_KIND_INT_UP}:
        acc_before = int(before.tags.get(acc_name, 0) or 0)
        acc_after = int(kernel.tags.get(acc_name, 0) or 0)
        return float(acc_after - acc_before), float(acc_after)

    if kind in {_DONE_KIND_COUNT_DOWN, _PROGRESS_KIND_INT_DOWN}:
        acc_before = int(before.tags.get(acc_name, 0) or 0)
        acc_after = int(kernel.tags.get(acc_name, 0) or 0)
        delta = float(acc_before - acc_after)
        current = float(-acc_after)
        return delta, current

    if kind == _PROGRESS_KIND_REAL_UP:
        acc_before = float(before.tags.get(acc_name, 0.0) or 0.0)
        acc_after = float(kernel.tags.get(acc_name, 0.0) or 0.0)
        return acc_after - acc_before, acc_after

    if kind == _PROGRESS_KIND_REAL_DOWN:
        acc_before = float(before.tags.get(acc_name, 0.0) or 0.0)
        acc_after = float(kernel.tags.get(acc_name, 0.0) or 0.0)
        return acc_before - acc_after, -acc_after

    return None


def _scans_until_threshold_event(
    spec: _ThresholdEventSpec,
    before: _KernelSnapshot,
    kernel: ReplayKernel,
    quantum: float | None = None,
) -> int | None:
    """Estimate scans until an uncrossed threshold atom crosses."""
    threshold_value = _threshold_value(kernel, spec.threshold)
    if not _is_numeric_literal(threshold_value):
        return None
    if spec.kind in _TIMER_DELAY_KINDS:
        move = _exact_timer_move(spec.acc_name, quantum, before, kernel)
        target = _timer_integer_target(float(threshold_value), spec.form)
        if move is not None and target is not None:
            acc, frac, q = move
            exact = _timer_scans_to_reach(acc, frac, q, target)
            if exact is not None or target > _TIMER_ACC_MAX:
                return exact

    delta_current = _progress_delta_and_current(spec.kind, spec.acc_name, before, kernel)
    if delta_current is None:
        return None
    delta, current = delta_current
    if delta <= 0:
        return None

    threshold = float(threshold_value)
    if spec.kind in {_DONE_KIND_COUNT_DOWN, _PROGRESS_KIND_REAL_DOWN}:
        threshold = -threshold

    if spec.form == _THRESHOLD_FORM_GE:
        if current >= threshold:
            return 1
        return max(1, int(math.ceil((threshold - current) / delta)))

    if current > threshold:
        return 1
    return max(1, int(math.floor((threshold - current) / delta)) + 1)


def _advance_hidden_progress(
    kind: str,
    acc_name: str,
    skipped_scans: int,
    before: _KernelSnapshot,
    kernel: ReplayKernel,
    quantum: float | None = None,
) -> None:
    """Advance a hidden timer/counter through skipped scans before the event scan."""
    if skipped_scans <= 0:
        return

    if kind in _TIMER_DELAY_KINDS:
        move = _exact_timer_move(acc_name, quantum, before, kernel)
        if move is not None:
            acc, frac, q = move
            landed = _timer_after(acc, frac, q, skipped_scans)
            if landed is not None:
                kernel.tags[acc_name] = landed[0]
                kernel.memory[f"_frac:{acc_name}"] = landed[1]
                return
        acc_before = int(before.tags.get(acc_name, 0) or 0)
        before_total = acc_before + float(before.memory.get(f"_frac:{acc_name}", 0.0) or 0.0)
        after_total = _timer_total(kernel, acc_name)
        delta = after_total - before_total
        target_total = after_total + (skipped_scans * delta)
        target_acc = int(target_total)
        kernel.tags[acc_name] = target_acc
        kernel.memory[f"_frac:{acc_name}"] = target_total - target_acc
        return

    if kind in {_DONE_KIND_COUNT_UP, _PROGRESS_KIND_INT_UP}:
        acc_before = int(before.tags.get(acc_name, 0) or 0)
        acc_after = int(kernel.tags.get(acc_name, 0) or 0)
        delta = acc_after - acc_before
        kernel.tags[acc_name] = acc_after + (skipped_scans * delta)
        return

    if kind == _PROGRESS_KIND_REAL_UP:
        acc_before = float(before.tags.get(acc_name, 0.0) or 0.0)
        acc_after = float(kernel.tags.get(acc_name, 0.0) or 0.0)
        delta = acc_after - acc_before
        kernel.tags[acc_name] = acc_after + (skipped_scans * delta)
        return

    if kind == _PROGRESS_KIND_REAL_DOWN:
        acc_before = float(before.tags.get(acc_name, 0.0) or 0.0)
        acc_after = float(kernel.tags.get(acc_name, 0.0) or 0.0)
        delta = acc_before - acc_after
        kernel.tags[acc_name] = acc_after - (skipped_scans * delta)
        return

    acc_before = int(before.tags.get(acc_name, 0) or 0)
    acc_after = int(kernel.tags.get(acc_name, 0) or 0)
    delta = acc_before - acc_after
    kernel.tags[acc_name] = acc_after - (skipped_scans * delta)


def _has_pending_done(context: _ExploreContext, key: tuple[Any, ...]) -> bool:
    """True if any timer/counter Done bit in *key* is PENDING."""
    return any(key[spec.state_index] == PENDING for spec in context.done_event_specs)


def _has_uncrossed_threshold_event(context: _ExploreContext, key: tuple[Any, ...]) -> bool:
    """True if any threshold vector bit is currently false."""
    offset = len(context.stateful_names)
    for spec in context.threshold_event_specs:
        vector = key[offset + spec.vector_index]
        if not vector[spec.atom_index]:
            return True
    return False


def _has_pending_hidden_event(context: _ExploreContext, key: tuple[Any, ...]) -> bool:
    return _has_pending_done(context, key) or _has_uncrossed_threshold_event(context, key)


def _collect_all_pending_sources(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _KernelSnapshot,
    key: tuple[Any, ...],
) -> dict[_SourceKey, _PendingSource]:
    """Collect pending *exact* event sources with their scan-to-crossing estimates.

    Abstract thresholds are deliberately excluded.  An abstract threshold's
    tag value is dynamic (an HMI setpoint, an external input) — the value the
    kernel happens to hold is just one of many possibilities, so scheduling
    the crossing at that value's scan position would silently drop every
    state where the crossing happens earlier.  Abstract thresholds are
    instead handled by ``_materialize_unresolvable_abstracts``, which builds
    a representative "crosses as soon as possible" witness.
    """
    sources: dict[_SourceKey, _PendingSource] = {}

    for i, spec in enumerate(context.done_event_specs):
        if key[spec.state_index] != PENDING:
            continue
        scans = _scans_until_done_event(
            spec.kind,
            spec.preset,
            spec.preset_memory_key,
            spec.acc_name,
            before_snap,
            kernel,
            context.timer_quanta.get(spec.acc_name),
        )
        if scans is None:
            continue
        source_key: _SourceKey = (spec.kind, spec.acc_name)
        existing = sources.get(source_key)
        if existing is None or scans < existing.scans:
            sources[source_key] = _PendingSource(
                kind=spec.kind,
                acc_name=spec.acc_name,
                scans=scans,
                spec_index=i,
            )

    vector_offset = len(context.stateful_names)
    for j, spec in enumerate(context.threshold_event_specs):
        if spec.mode != _THRESHOLD_MODE_EXACT:
            continue
        vector = key[vector_offset + spec.vector_index]
        if vector[spec.atom_index]:
            continue
        scans = _scans_until_threshold_event(
            spec, before_snap, kernel, context.timer_quanta.get(spec.acc_name)
        )
        if scans is None:
            continue
        source_key = (spec.kind, spec.acc_name, spec.vector_index, spec.atom_index)
        existing = sources.get(source_key)
        if existing is None or scans < existing.scans:
            sources[source_key] = _PendingSource(
                kind=spec.kind,
                acc_name=spec.acc_name,
                scans=scans,
                spec_index=j,
            )

    return sources


def _partition_pending_sources(
    sources: dict[_SourceKey, _PendingSource],
) -> tuple[_SimultaneityGroup, ...]:
    """Group pending sources by scan count, sorted ascending."""
    by_scans: dict[int, set[_SourceKey]] = {}
    for key, src in sources.items():
        by_scans.setdefault(src.scans, set()).add(key)

    return tuple(
        _SimultaneityGroup(scans=scans, exact_sources=frozenset(members))
        for scans, members in sorted(by_scans.items())
    )


def _detect_resets_in_group(
    context: _ExploreContext,
    pre_event_snapshot: _KernelSnapshot,
    kernel: ReplayKernel,
    firing_group: _SimultaneityGroup,
) -> frozenset[_SourceKey]:
    """Return which sources in the firing group got reset during the event step."""
    reset_sources: set[_SourceKey] = set()
    all_members = firing_group.exact_sources
    acc_keys = {(k[0], k[1]) for k in all_members}

    for spec in context.done_event_specs:
        if spec.kind == _DONE_KIND_TIME_DRUM:
            continue
        done_key = (spec.kind, spec.acc_name)
        if done_key not in acc_keys:
            continue

        pre_acc = int(pre_event_snapshot.tags.get(spec.acc_name, 0) or 0)
        post_acc = int(kernel.tags.get(spec.acc_name, 0) or 0)

        if spec.kind == _DONE_KIND_COUNT_DOWN:
            reversed_ = post_acc > pre_acc
        else:
            reversed_ = post_acc < pre_acc

        if reversed_:
            done_name = context.stateful_names[spec.state_index]
            if not kernel.tags.get(done_name):
                for mk in all_members:
                    if mk[0] == spec.kind and mk[1] == spec.acc_name:
                        reset_sources.add(mk)

    return frozenset(reset_sources)


def _advance_group_to_threshold(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _KernelSnapshot,
    all_sources: dict[_SourceKey, _PendingSource],
    groups: tuple[_SimultaneityGroup, ...],
) -> _EventAdvanceState | None:
    """Advance accumulators to just before the nearest group's crossing.

    Does NOT pin abstract thresholds — pinning is the caller's responsibility.
    """
    if not groups:
        return None

    firing_group = groups[0]
    next_event_scans = firing_group.scans
    skipped_scans = max(next_event_scans - 1, 0)

    pre_advance_counter_acc: dict[str, int] = {}
    advanced: set[tuple[str, str]] = set()
    for src in all_sources.values():
        acc_key = (src.kind, src.acc_name)
        if acc_key in advanced:
            continue
        advanced.add(acc_key)
        if src.kind in {_DONE_KIND_COUNT_UP, _DONE_KIND_COUNT_DOWN, _DONE_KIND_TIME_DRUM}:
            pre_advance_counter_acc[src.acc_name] = int(kernel.tags.get(src.acc_name, 0) or 0)
        _advance_hidden_progress(
            src.kind,
            src.acc_name,
            skipped_scans,
            before_snap,
            kernel,
            context.timer_quanta.get(src.acc_name),
        )

    return _EventAdvanceState(
        pre_event_snapshot=_snapshot_kernel(
            kernel, context.mutable_tag_names, context.base_tag_keys
        ),
        before_snap=before_snap,
        pre_advance_counter_acc=pre_advance_counter_acc,
        pending_sources=set(all_sources),
        next_event_scans=next_event_scans,
        firing_group=firing_group,
    )


def _advance_all_to_cofire(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _KernelSnapshot,
    all_sources: dict[_SourceKey, _PendingSource],
) -> _EventAdvanceState | None:
    """Advance every pending source so it crosses on the same upcoming scan.

    The BFS state key collapses timer/counter accumulators, so when two or
    more timers are simultaneously PENDING their *relative* phase is not
    pinned by the key.  A concrete state where both are one scan from
    crossing is therefore reachable (and the verifier must over-approximate
    toward it).  ``_advance_group_to_threshold`` only ever resolves the
    nearest source; this builds the co-firing witness the BFS would
    otherwise never reach, since the intermediate accumulator values are
    skipped by hidden-event jumps.
    """
    by_acc: dict[tuple[str, str], _PendingSource] = {}
    for src in all_sources.values():
        acc_key = (src.kind, src.acc_name)
        existing = by_acc.get(acc_key)
        if existing is None or src.scans < existing.scans:
            by_acc[acc_key] = src
    if len(by_acc) < 2:
        return None

    pre_advance_counter_acc: dict[str, int] = {}
    for (kind, acc_name), src in by_acc.items():
        if kind in {_DONE_KIND_COUNT_UP, _DONE_KIND_COUNT_DOWN, _DONE_KIND_TIME_DRUM}:
            pre_advance_counter_acc[acc_name] = int(kernel.tags.get(acc_name, 0) or 0)
        _advance_hidden_progress(
            kind,
            acc_name,
            max(src.scans - 1, 0),
            before_snap,
            kernel,
            context.timer_quanta.get(acc_name),
        )

    cofire_group = _SimultaneityGroup(scans=1, exact_sources=frozenset(all_sources))
    return _EventAdvanceState(
        pre_event_snapshot=_snapshot_kernel(
            kernel, context.mutable_tag_names, context.base_tag_keys
        ),
        before_snap=before_snap,
        pre_advance_counter_acc=pre_advance_counter_acc,
        pending_sources=set(all_sources),
        next_event_scans=max(src.scans for src in by_acc.values()),
        firing_group=cofire_group,
    )


def _fixup_unfired_counters(
    context: _ExploreContext,
    before_snap: _KernelSnapshot,
    pre_advance_acc: dict[str, int],
    pre_event_snapshot: _KernelSnapshot,
    kernel: ReplayKernel,
) -> bool:
    """Apply missing delta for counter sources that didn't fire during the event step.

    Counter instructions have ``ALWAYS_EXECUTES = True`` — they recompute
    Done from current Acc every scan.  But Acc only changes when the rung
    fires.  For edge-triggered counters (e.g. ``rise(pulse)``), the event
    step may not satisfy the edge condition, leaving Acc and Done unchanged.

    Detect this by comparing Acc before and after the step.  When the
    counter didn't fire, manually apply one per-scan delta and set Done
    to the correct value.

    Returns True if any counter was fixed up (i.e. the event step alone
    did not reproduce the accumulation).
    """
    applied = False
    if not pre_advance_acc:
        return applied
    for spec in context.done_event_specs:
        if spec.kind not in {_DONE_KIND_COUNT_UP, _DONE_KIND_COUNT_DOWN}:
            continue
        if spec.acc_name not in pre_advance_acc:
            continue
        pre_acc = int(pre_event_snapshot.tags.get(spec.acc_name, 0) or 0)
        post_acc = int(kernel.tags.get(spec.acc_name, 0) or 0)
        if pre_acc != post_acc:
            continue

        original_after = pre_advance_acc[spec.acc_name]
        original_before = int(before_snap.tags.get(spec.acc_name, 0) or 0)
        if spec.kind == _DONE_KIND_COUNT_UP:
            per_scan = original_after - original_before
        else:
            per_scan = original_before - original_after
        if per_scan <= 0:
            continue

        if spec.kind == _DONE_KIND_COUNT_UP:
            new_acc = post_acc + per_scan
        else:
            new_acc = post_acc - per_scan
        kernel.tags[spec.acc_name] = new_acc
        applied = True

        preset = _resolve_done_preset(spec.preset, spec.preset_memory_key, kernel)
        if preset is None:
            continue
        done_name = context.stateful_names[spec.state_index]
        if spec.kind == _DONE_KIND_COUNT_UP:
            kernel.tags[done_name] = new_acc >= preset
        else:
            kernel.tags[done_name] = new_acc <= -preset
    return applied


def _fixup_unfired_drums(
    context: _ExploreContext,
    before_snap: _KernelSnapshot,
    pre_advance_acc: dict[str, int],
    pre_event_snapshot: _KernelSnapshot,
    kernel: ReplayKernel,
) -> None:
    """Apply missing delta for drum sources that didn't fire during the event step.

    Same principle as ``_fixup_unfired_counters`` but handles the drum's
    multi-step crossing, as the drum's own enabled scan does: when the
    accumulator crosses the current step's preset, advance the step, reset
    the accumulator, end a completion (``_drum_done``) and apply the new
    step's output pattern, leaving the completion flag as it is (only a reset
    clears it); on the last step, set the flag and ``_drum_done`` and turn
    every output off.
    """
    if not pre_advance_acc:
        return
    for spec in context.done_event_specs:
        if spec.kind != _DONE_KIND_TIME_DRUM:
            continue
        if spec.acc_name not in pre_advance_acc:
            continue
        pre_acc = int(pre_event_snapshot.tags.get(spec.acc_name, 0) or 0)
        post_acc = int(kernel.tags.get(spec.acc_name, 0) or 0)
        if pre_acc != post_acc:
            continue

        original_after = pre_advance_acc[spec.acc_name]
        original_before = int(before_snap.tags.get(spec.acc_name, 0) or 0)
        per_scan = original_after - original_before
        if per_scan <= 0:
            continue

        new_acc = post_acc + per_scan

        preset = _resolve_done_preset(spec.preset, spec.preset_memory_key, kernel)
        if preset is None:
            continue

        done_name = context.stateful_names[spec.state_index]
        meta = context.drum_event_meta.get(done_name)
        if meta is None:
            kernel.tags[spec.acc_name] = new_acc
            continue

        step = int(kernel.tags.get(meta.step_name, 1) or 1)
        if new_acc >= preset:
            if step < meta.step_count:
                new_step = step + 1
                kernel.tags[meta.step_name] = new_step
                kernel.tags[spec.acc_name] = 0
                kernel.memory[f"_frac:{spec.acc_name}"] = 0.0
                kernel.memory[meta.done_memory_key] = False
                for i, out_name in enumerate(meta.output_names):
                    kernel.tags[out_name] = meta.pattern[new_step - 1][i]
            else:
                kernel.tags[spec.acc_name] = new_acc
                kernel.tags[done_name] = True
                kernel.memory[meta.done_memory_key] = True
                for out_name in meta.output_names:
                    kernel.tags[out_name] = False
        else:
            kernel.tags[spec.acc_name] = new_acc


def _reset_during_event(
    context: _ExploreContext,
    pre_event_snapshot: _KernelSnapshot,
    kernel: ReplayKernel,
    pending_sources: set[_SourceKey] | None = None,
) -> bool:
    """Detect if a reset undid the accumulator advance during the event step.

    When *pending_sources* is provided, only counters/timers that were
    actually being advanced are checked.  Side-effect resets on other
    counters (e.g. a counter reset by a Done-bit of the event target)
    are ignored.  For pending counters whose accumulator reversed but
    whose Done bit fired, the event is still considered valid
    (self-resetting counter/timer pattern).
    """
    for spec in context.done_event_specs:
        if spec.kind == _DONE_KIND_TIME_DRUM:
            continue
        if pending_sources is not None and (spec.kind, spec.acc_name) not in pending_sources:
            continue
        pre_acc = int(pre_event_snapshot.tags.get(spec.acc_name, 0) or 0)
        post_acc = int(kernel.tags.get(spec.acc_name, 0) or 0)
        reversed_ = False
        if spec.kind == _DONE_KIND_COUNT_DOWN:
            reversed_ = post_acc > pre_acc
        else:
            reversed_ = post_acc < pre_acc
        if reversed_:
            done_name = context.stateful_names[spec.state_index]
            if not kernel.tags.get(done_name):
                return True
    return False


def _step_event_from_advance(
    context: _ExploreContext,
    kernel: ReplayKernel,
    advance: _EventAdvanceState,
    edge_comp: _EdgeCompressor,
) -> _HiddenEventOutcome | None:
    """Execute the event step from a pre-advanced kernel state.

    The kernel must be restored to ``advance.pre_event_snapshot`` (with
    desired inputs set) before calling.  Runs the step, applies fixups,
    and checks for resets.
    """
    _step_kernel(context, kernel)
    counter_fixup = _fixup_unfired_counters(
        context,
        advance.before_snap,
        advance.pre_advance_counter_acc,
        advance.pre_event_snapshot,
        kernel,
    )
    _fixup_unfired_drums(
        context,
        advance.before_snap,
        advance.pre_advance_counter_acc,
        advance.pre_event_snapshot,
        kernel,
    )
    if counter_fixup:
        _step_kernel(context, kernel)
    if advance.firing_group is not None:
        reset_set = _detect_resets_in_group(
            context,
            advance.pre_event_snapshot,
            kernel,
            advance.firing_group,
        )
        if reset_set == advance.firing_group.exact_sources:
            return None
    elif _reset_during_event(
        context, advance.pre_event_snapshot, kernel, pending_sources=advance.pending_sources
    ):
        return None
    settle_scans = 1 if counter_fixup else 0
    return _HiddenEventOutcome(
        snapshot=_snapshot_kernel(kernel, context.mutable_tag_names, context.base_tag_keys),
        key=edge_comp.state_key(kernel),
        additional_scans=advance.next_event_scans + settle_scans,
        pre_event_snapshot=advance.pre_event_snapshot,
        caveats=_HIDDEN_EVENT_CAVEAT if counter_fixup else (),
    )


def _materialize_abstract_threshold_outcome(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _KernelSnapshot,
    spec: _ThresholdEventSpec,
    edge_comp: _EdgeCompressor,
) -> _HiddenEventOutcome | None:
    """Build one representative crossed successor for an abstract threshold."""
    threshold_name = spec.threshold if isinstance(spec.threshold, str) else None
    if threshold_name is None:
        return None

    delta_current = _progress_delta_and_current(spec.kind, spec.acc_name, before_snap, kernel)
    if delta_current is None:
        return None
    delta, _current = delta_current
    if delta <= 0:
        return None

    acc_value = kernel.tags.get(spec.acc_name)
    if not _is_numeric_literal(acc_value):
        return None
    assert isinstance(acc_value, (int, float))

    kernel.tags[threshold_name] = acc_value
    quantum = context.timer_quanta.get(spec.acc_name)
    scans = _scans_until_threshold_event(spec, before_snap, kernel, quantum)
    if scans is None:
        return None
    pre_advance_counter_acc: dict[str, int] = {}
    if spec.kind in {_DONE_KIND_COUNT_UP, _DONE_KIND_COUNT_DOWN}:
        pre_advance_counter_acc[spec.acc_name] = int(kernel.tags.get(spec.acc_name, 0) or 0)
    skipped_scans = max(scans - 1, 0)
    _advance_hidden_progress(spec.kind, spec.acc_name, skipped_scans, before_snap, kernel, quantum)
    pre_event_snapshot = _snapshot_kernel(kernel, context.mutable_tag_names, context.base_tag_keys)
    _step_kernel(context, kernel)
    _fixup_unfired_counters(
        context, before_snap, pre_advance_counter_acc, pre_event_snapshot, kernel
    )
    if not _threshold_crossed(kernel, spec.kind, spec.acc_name, spec.threshold, spec.form):
        return None
    return _HiddenEventOutcome(
        snapshot=_snapshot_kernel(kernel, context.mutable_tag_names, context.base_tag_keys),
        key=edge_comp.state_key(kernel),
        additional_scans=scans,
        pre_event_snapshot=pre_event_snapshot,
        caveats=_ABSTRACT_THRESHOLD_TRACE_CAVEAT,
    )


def _materialize_unresolvable_abstracts(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _KernelSnapshot,
    key: tuple[Any, ...],
    edge_comp: _EdgeCompressor,
    resolved_sources: dict[_SourceKey, _PendingSource],
) -> list[_HiddenEventOutcome]:
    """Handle abstract thresholds with non-numeric tags (couldn't join the partition)."""
    vector_offset = len(context.stateful_names)
    base_snap = _snapshot_kernel(kernel, context.mutable_tag_names, context.base_tag_keys)
    outcomes: list[_HiddenEventOutcome] = []
    seen_keys: set[tuple[Any, ...]] = set()

    for spec in context.threshold_event_specs:
        if spec.mode == _THRESHOLD_MODE_EXACT:
            continue
        vector = key[vector_offset + spec.vector_index]
        if vector[spec.atom_index]:
            continue
        spec_key: _SourceKey = (spec.kind, spec.acc_name, spec.vector_index, spec.atom_index)
        if spec_key in resolved_sources:
            continue
        _restore_kernel(kernel, base_snap)
        outcome = _materialize_abstract_threshold_outcome(
            context,
            kernel,
            before_snap,
            spec,
            edge_comp,
        )
        if outcome is None or outcome.key in seen_keys:
            continue
        seen_keys.add(outcome.key)
        outcomes.append(outcome)

    _restore_kernel(kernel, base_snap)
    return outcomes


def _settle_unified(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _KernelSnapshot,
    edge_comp: _EdgeCompressor,
    total_additional_scans: int = 0,
    accumulated_caveats: tuple[str, ...] = (),
    depth: int = 0,
    max_depth: int | None = None,
) -> list[_HiddenEventOutcome]:
    """Recursively resolve all pending exact events in temporal order.

    Returns the fully-settled leaf states — every pending timer/counter
    resolved, every chained event that one crossing arms also resolved.
    ``_settle_pending`` wants the settled state, not the plateaus on the way
    there; the BFS enumerates intermediate plateaus via ``_maybe_jump_hidden_event``.
    """
    if max_depth is None:
        max_depth = len(context.done_event_specs) + len(context.threshold_event_specs) + 1

    if depth >= max_depth:
        return [
            _HiddenEventOutcome(
                snapshot=_snapshot_kernel(kernel, context.mutable_tag_names, context.base_tag_keys),
                key=edge_comp.state_key(kernel),
                additional_scans=total_additional_scans,
                caveats=accumulated_caveats,
            )
        ]

    key = edge_comp.state_key(kernel)
    all_sources = _collect_all_pending_sources(context, kernel, before_snap, key)
    groups = _partition_pending_sources(all_sources)

    unresolvable = _materialize_unresolvable_abstracts(
        context,
        kernel,
        before_snap,
        key,
        edge_comp,
        all_sources,
    )

    if not groups and not unresolvable:
        return [
            _HiddenEventOutcome(
                snapshot=_snapshot_kernel(kernel, context.mutable_tag_names, context.base_tag_keys),
                key=edge_comp.state_key(kernel),
                additional_scans=total_additional_scans,
                caveats=accumulated_caveats,
            )
        ]

    outcomes: list[_HiddenEventOutcome] = []
    seen_keys: set[tuple[Any, ...]] = set()
    base_snap = _snapshot_kernel(kernel, context.mutable_tag_names, context.base_tag_keys)

    def _emit(outcome: _HiddenEventOutcome, branch_caveats: tuple[str, ...]) -> None:
        """Recurse from one event crossing to collect the settled leaf states."""
        emitted_scans = total_additional_scans + outcome.additional_scans
        emitted_caveats = _merge_caveats(accumulated_caveats, outcome.caveats, branch_caveats)
        _restore_kernel(kernel, outcome.snapshot)
        for sub in _settle_unified(
            context,
            kernel,
            before_snap=outcome.pre_event_snapshot or before_snap,
            edge_comp=edge_comp,
            total_additional_scans=emitted_scans,
            accumulated_caveats=emitted_caveats,
            depth=depth + 1,
            max_depth=max_depth,
        ):
            if sub.key not in seen_keys:
                seen_keys.add(sub.key)
                outcomes.append(sub)

    if groups:
        advance = _advance_group_to_threshold(
            context,
            kernel,
            before_snap,
            all_sources,
            groups,
        )
        if advance is not None:
            outcome = _step_event_from_advance(context, kernel, advance, edge_comp)
            if outcome is not None:
                _emit(outcome, ())
        _restore_kernel(kernel, base_snap)

    for ua_outcome in unresolvable:
        _emit(ua_outcome, ())

    _restore_kernel(kernel, base_snap)
    return outcomes


def _settle_pending(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _KernelSnapshot,
    edge_comp: _EdgeCompressor,
    cache: _HiddenEventCache | None = None,
) -> list[_HiddenEventOutcome]:
    """Resolve pending exact events and settle exact work behind abstract branches."""
    key = edge_comp.state_key(kernel)
    cache_key = cache.plateau_key(context, before_snap, kernel, key) if cache is not None else None
    active_cache = cache if cache_key is not None else None
    if active_cache is not None and cache_key is not None:
        cached = active_cache._settle_cache.get(cache_key)
        if cached is not None:
            active_cache.settle_hits += 1
            return list(cached)
        active_cache.settle_misses += 1

    base_snap = _snapshot_kernel(kernel, context.mutable_tag_names, context.base_tag_keys)
    outcomes = _settle_unified(context, kernel, before_snap, edge_comp)
    _restore_kernel(kernel, base_snap)

    if outcomes and active_cache is not None and cache_key is not None:
        active_cache._settle_cache[cache_key] = tuple(outcomes)
    return outcomes


# Scans a flip may take to start an idle timer or counter (a latch, then the
# rung that runs it, may sit a scan or two behind the input).
_STOP_START_DELAY = 2
_IDLE_COUNTER_KINDS = frozenset((_DONE_KIND_COUNT_UP, _DONE_KIND_COUNT_DOWN))


def _idle_scans_to_cross(
    context: _ExploreContext,
    kernel: ReplayKernel,
    key: tuple[Any, ...],
    running: set[str],
) -> list[int] | None:
    """Scans each idle timer or counter needs to cross a threshold once started.

    Idle: its Done is not pending in *key*, it does not advance on a held
    scan, and it is still below the threshold.  None when one of them cannot
    be timed (no single per-scan quantum, a preset that does not resolve).
    """
    needs: list[int] = []
    for spec in context.done_event_specs:
        if spec.acc_name in running or key[spec.state_index] == PENDING:
            continue
        if spec.kind not in (_TIMER_DELAY_KINDS | _IDLE_COUNTER_KINDS):
            continue
        if spec.kind == _DONE_KIND_TIME_DRUM:
            continue
        preset = _resolve_done_preset(spec.preset, spec.preset_memory_key, kernel)
        if preset is None:
            return None
        if spec.kind in _TIMER_DELAY_KINDS:
            acc, frac = _timer_reading(kernel, spec.acc_name)
            if acc >= preset:
                continue
            quantum = context.timer_quanta.get(spec.acc_name)
            if quantum is None or not quantum > 0.0:
                return None
            scans = _timer_scans_to_reach(acc, frac, quantum, preset)
            if scans is None:
                continue
        else:
            acc = int(kernel.tags.get(spec.acc_name, 0) or 0)
            progress = acc if spec.kind == _DONE_KIND_COUNT_UP else -acc
            if progress >= preset:
                continue
            scans = preset - progress
        needs.append(scans)

    vector_offset = len(context.stateful_names)
    for spec in context.threshold_event_specs:
        if spec.acc_name in running or spec.mode != _THRESHOLD_MODE_EXACT:
            continue
        if spec.kind not in _TIMER_DELAY_KINDS or spec.kind == _DONE_KIND_TIME_DRUM:
            continue
        if key[vector_offset + spec.vector_index][spec.atom_index]:
            continue
        value = _threshold_value(kernel, spec.threshold)
        if not _is_numeric_literal(value):
            return None
        target = _timer_integer_target(float(value), spec.form)
        quantum = context.timer_quanta.get(spec.acc_name)
        if target is None or quantum is None or not quantum > 0.0:
            return None
        acc, frac = _timer_reading(kernel, spec.acc_name)
        if acc >= target:
            continue
        scans = _timer_scans_to_reach(acc, frac, quantum, target)
        if scans is not None:
            needs.append(scans)
    return needs


def _stop_point_outcome(
    context: _ExploreContext,
    kernel: ReplayKernel,
    snap: _KernelSnapshot,
    new_key: tuple[Any, ...],
    edge_comp: _EdgeCompressor,
) -> _HiddenEventOutcome | None:
    """The nearest plateau before the next crossing where a started timer could tie.

    *kernel* is one held scan past the node in *snap*, back at its key.  The
    outcome is the plateau that many held scans further on (0: *kernel*
    itself, whose own revisit would be covered), still at that key, with every
    running accumulator advanced; None when no tie falls before the crossing.
    See the module docstring.
    """
    if not context.done_event_specs and not context.threshold_event_specs:
        return None
    all_sources = _collect_all_pending_sources(context, kernel, snap, new_key)
    if not all_sources:
        return None
    nearest = min(src.scans for src in all_sources.values())
    running = {src.acc_name for src in all_sources.values()}
    needs = _idle_scans_to_cross(context, kernel, new_key, running)
    if needs is None:
        # An idle source that cannot be timed: stop at every scan.
        stop = 0
    else:
        # A flip on the scan after a plateau ``e`` scans on starts an idle
        # source that crosses ``e + delay + m_j`` scans on; a running one
        # crosses ``s_i`` scans on.  Plateaus on, before and after each tie.
        found: int | None = None
        for s_i in {src.scans for src in all_sources.values()}:
            for m_j in needs:
                for offset in range(-_STOP_START_DELAY - 1, 2):
                    candidate = s_i - m_j + offset
                    if 0 <= candidate < nearest and (found is None or candidate < found):
                        found = candidate
        if found is None:
            return None
        stop = found
    base_snap = _snapshot_kernel(kernel, context.mutable_tag_names, context.base_tag_keys)
    if stop:
        advanced: set[tuple[str, str]] = set()
        for src in all_sources.values():
            acc_key = (src.kind, src.acc_name)
            if acc_key in advanced:
                continue
            advanced.add(acc_key)
            _advance_hidden_progress(
                src.kind,
                src.acc_name,
                stop - 1,
                snap,
                kernel,
                context.timer_quanta.get(src.acc_name),
            )
        _step_kernel(context, kernel)
    key = edge_comp.state_key(kernel)
    outcome = None
    if key == new_key:
        outcome = _HiddenEventOutcome(
            snapshot=_snapshot_kernel(kernel, context.mutable_tag_names, context.base_tag_keys),
            key=key,
            additional_scans=stop,
            stop_point=True,
        )
    _restore_kernel(kernel, base_snap)
    return outcome


def _maybe_jump_hidden_event(
    context: _ExploreContext,
    kernel: ReplayKernel,
    snap: _KernelSnapshot,
    visited: set[tuple[Any, ...]] | dict[tuple[Any, ...], set[tuple[Any, ...]]],
    new_key: tuple[Any, ...],
    edge_comp: _EdgeCompressor,
    cache: _HiddenEventCache | None = None,
) -> list[_HiddenEventOutcome]:
    """Jump from a revisited hidden pending plateau to future hidden-event states.

    When the event fires, the final crossing scan is explored with ALL
    nondeterministic input combinations — not just the inputs that
    triggered the revisit.  This is necessary because edge inputs (e.g.
    rise/fall sources) can change during the multi-scan accumulation
    period, and combinational outputs on the crossing scan depend on
    which edges are active.
    """
    if not (context.done_event_specs or context.threshold_event_specs) or new_key not in visited:
        return []

    cache_key = cache.plateau_key(context, snap, kernel, new_key) if cache is not None else None
    active_cache = cache if cache_key is not None else None
    if active_cache is not None and cache_key is not None:
        cached = active_cache._jump_cache.get(cache_key)
        if cached is not None:
            return list(cached)

    base_snap = _snapshot_kernel(kernel, context.mutable_tag_names, context.base_tag_keys)
    outcomes: list[_HiddenEventOutcome] = []
    seen_keys: set[tuple[Any, ...]] = set()

    all_sources = _collect_all_pending_sources(context, kernel, snap, new_key)
    groups = _partition_pending_sources(all_sources)

    advance = _advance_group_to_threshold(context, kernel, snap, all_sources, groups)

    if advance is not None:
        nd_dims = context.nondeterministic_dims
        edge_names = tuple(n for n in context.edge_tag_names if n in nd_dims)

        def _settle_step_outcome(
            outcome: _HiddenEventOutcome,
            variant_caveats: tuple[str, ...],
            variant_inputs: dict[str, Any] | None,
        ) -> None:
            # Emit the nearest crossing only.  The BFS drives multi-event
            # cascades by revisiting each plateau and jumping again — settling
            # the whole chain here would collapse the intermediate states the
            # BFS needs to enumerate.
            if outcome.key not in seen_keys:
                seen_keys.add(outcome.key)
                outcomes.append(
                    _HiddenEventOutcome(
                        snapshot=outcome.snapshot,
                        key=outcome.key,
                        additional_scans=outcome.additional_scans,
                        pre_event_snapshot=outcome.pre_event_snapshot,
                        caveats=_merge_caveats(outcome.caveats, variant_caveats),
                        event_inputs=variant_inputs,
                    )
                )

        if edge_names:
            edge_values = [nd_dims[n] for n in edge_names]
            pre_snap = advance.pre_event_snapshot
            for combo in _product(*edge_values):
                for prev_combo in _product(*edge_values):
                    _restore_kernel(kernel, pre_snap)
                    variant_inputs = dict(zip(edge_names, combo, strict=True))
                    is_input_variant = any(
                        pre_snap.tags.get(n) != v for n, v in variant_inputs.items()
                    )
                    is_prev_variant = any(
                        pre_snap.prev.get(n) != v
                        for n, v in zip(edge_names, prev_combo, strict=True)
                    )
                    for name, val in variant_inputs.items():
                        kernel.tags[name] = val
                    for name, val in zip(edge_names, prev_combo, strict=True):
                        kernel.prev[name] = val
                    outcome = _step_event_from_advance(context, kernel, advance, edge_comp)
                    if outcome is not None:
                        any_variant = is_input_variant or is_prev_variant
                        caveats = (
                            _merge_caveats(outcome.caveats, _EVENT_INPUT_VARIANT_CAVEAT)
                            if any_variant
                            else outcome.caveats
                        )
                        _settle_step_outcome(
                            outcome,
                            caveats,
                            variant_inputs if any_variant else None,
                        )
        else:
            _restore_kernel(kernel, advance.pre_event_snapshot)
            outcome = _step_event_from_advance(context, kernel, advance, edge_comp)
            if outcome is not None:
                _settle_step_outcome(outcome, (), None)

        _restore_kernel(kernel, base_snap)

    # Co-firing witness: when two or more timers/counters are pending at once,
    # their collapsed accumulators leave the relative phase free, so a scan
    # where they all cross together is reachable.  _advance_group_to_threshold
    # only ever resolves the nearest one, so this branch supplies the
    # coincidence the BFS would otherwise skip past.
    cofire = _advance_all_to_cofire(context, kernel, snap, all_sources)
    if cofire is not None:
        cofire_outcome = _step_event_from_advance(context, kernel, cofire, edge_comp)
        if cofire_outcome is not None and cofire_outcome.key not in seen_keys:
            seen_keys.add(cofire_outcome.key)
            outcomes.append(
                _HiddenEventOutcome(
                    snapshot=cofire_outcome.snapshot,
                    key=cofire_outcome.key,
                    additional_scans=cofire_outcome.additional_scans,
                    pre_event_snapshot=cofire_outcome.pre_event_snapshot,
                    caveats=_merge_caveats(cofire_outcome.caveats, _COFIRE_CAVEAT),
                )
            )
    _restore_kernel(kernel, base_snap)

    # Unresolvable abstract thresholds (non-numeric tags)
    unresolvable = _materialize_unresolvable_abstracts(
        context,
        kernel,
        snap,
        new_key,
        edge_comp,
        all_sources if advance is not None else {},
    )
    for ua_outcome in unresolvable:
        if ua_outcome.key not in seen_keys:
            seen_keys.add(ua_outcome.key)
            outcomes.append(ua_outcome)

    _restore_kernel(kernel, base_snap)
    if active_cache is not None and cache_key is not None:
        active_cache._jump_cache[cache_key] = tuple(outcomes)
    return outcomes
