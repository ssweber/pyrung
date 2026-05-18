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


@dataclass(frozen=True)
class _StateKeyDoneSpec:
    index: int
    acc_name: str
    kind: str


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


@dataclass(frozen=True)
class _EventAdvanceState:
    """Intermediate state after accumulator advance, before the event step."""

    pre_event_snapshot: _KernelSnapshot
    before_snap: _KernelSnapshot
    pre_advance_counter_acc: dict[str, int]
    pending_sources: set[tuple[str, str]]
    source_scans: dict[tuple[str, str], int]
    next_event_scans: int


_ABSTRACT_THRESHOLD_TRACE_CAVEAT = (
    "Counterexample trace uses an abstract threshold witness hidden from the BFS state key; "
    "replaying TraceStep.inputs alone may not reproduce the violation.",
)

_EVENT_INPUT_VARIANT_CAVEAT = (
    "Counterexample reached via hidden-event input variant; "
    "the trace inputs on the crossing scan differ from the fast-forwarded scans.",
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
                spec.mode == _THRESHOLD_MODE_EXACT
                and isinstance(spec.threshold, str)
                and spec.threshold not in self._stateful_names
                and spec.threshold not in seen_thresholds
            ):
                seen_thresholds.add(spec.threshold)
                hidden_thresholds.append((spec.threshold, kernel.tags.get(spec.threshold)))

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
    if kind in {_DONE_KIND_ON_DELAY, _DONE_KIND_OFF_DELAY, _DONE_KIND_TIME_DRUM}:
        before_acc = int(before_snap.tags.get(acc_name, 0) or 0)
        after_acc = int(kernel.tags.get(acc_name, 0) or 0)
        before_frac = float(before_snap.memory.get(f"_frac:{acc_name}", 0.0) or 0.0)
        after_frac = float(kernel.memory.get(f"_frac:{acc_name}", 0.0) or 0.0)
        return (kind, acc_name, before_acc, before_frac, after_acc, after_frac)
    if kind in {_PROGRESS_KIND_REAL_UP, _PROGRESS_KIND_REAL_DOWN}:
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
) -> int | None:
    """Estimate scans until this pending timer/counter reaches its next Done event."""
    resolved_preset = _resolve_done_preset(preset, preset_memory_key, kernel)
    if resolved_preset is None:
        return None
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
) -> int | None:
    """Estimate scans until an uncrossed threshold atom crosses."""
    threshold_value = _threshold_value(kernel, spec.threshold)
    if not _is_numeric_literal(threshold_value):
        return None

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
) -> None:
    """Advance a hidden timer/counter through skipped scans before the event scan."""
    if skipped_scans <= 0:
        return

    if kind in {_DONE_KIND_ON_DELAY, _DONE_KIND_OFF_DELAY, _DONE_KIND_TIME_DRUM}:
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


def _fixup_unfired_counters(
    context: _ExploreContext,
    before_snap: _KernelSnapshot,
    pre_advance_acc: dict[str, int],
    pre_event_snapshot: _KernelSnapshot,
    kernel: ReplayKernel,
) -> None:
    """Apply missing delta for counter sources that didn't fire during the event step.

    Counter instructions have ``ALWAYS_EXECUTES = True`` — they recompute
    Done from current Acc every scan.  But Acc only changes when the rung
    fires.  For edge-triggered counters (e.g. ``rise(pulse)``), the event
    step may not satisfy the edge condition, leaving Acc and Done unchanged.

    Detect this by comparing Acc before and after the step.  When the
    counter didn't fire, manually apply one per-scan delta and set Done
    to the correct value.
    """
    if not pre_advance_acc:
        return
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

        preset = _resolve_done_preset(spec.preset, spec.preset_memory_key, kernel)
        if preset is None:
            continue
        done_name = context.stateful_names[spec.state_index]
        if spec.kind == _DONE_KIND_COUNT_UP:
            kernel.tags[done_name] = new_acc >= preset
        else:
            kernel.tags[done_name] = new_acc <= -preset


def _fixup_unfired_drums(
    context: _ExploreContext,
    before_snap: _KernelSnapshot,
    pre_advance_acc: dict[str, int],
    pre_event_snapshot: _KernelSnapshot,
    kernel: ReplayKernel,
) -> None:
    """Apply missing delta for drum sources that didn't fire during the event step.

    Same principle as ``_fixup_unfired_counters`` but handles the drum's
    multi-step crossing: when the accumulator crosses the current step's
    preset, advance the step, reset the accumulator, and apply the new
    step's output pattern.
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
                for i, out_name in enumerate(meta.output_names):
                    kernel.tags[out_name] = meta.pattern[new_step - 1][i]
                kernel.tags[done_name] = False
            else:
                kernel.tags[spec.acc_name] = new_acc
                kernel.tags[done_name] = True
        else:
            kernel.tags[spec.acc_name] = new_acc


def _reset_during_event(
    context: _ExploreContext,
    pre_event_snapshot: _KernelSnapshot,
    kernel: ReplayKernel,
    pending_sources: set[tuple[str, str]] | None = None,
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


def _pending_event_source_scans(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _KernelSnapshot,
    key: tuple[Any, ...],
) -> dict[tuple[str, str], int]:
    """Return pending hidden event sources and their scan distances."""
    pending_sources: dict[tuple[str, str], int] = {}

    def _record(source: tuple[str, str], scans: int | None) -> None:
        if scans is None:
            return
        previous = pending_sources.get(source)
        if previous is None or scans < previous:
            pending_sources[source] = scans

    for spec in context.done_event_specs:
        if key[spec.state_index] != PENDING:
            continue
        _record(
            (spec.kind, spec.acc_name),
            _scans_until_done_event(
                spec.kind,
                spec.preset,
                spec.preset_memory_key,
                spec.acc_name,
                before_snap,
                kernel,
            ),
        )

    vector_offset = len(context.stateful_names)
    for spec in context.threshold_event_specs:
        if spec.mode != _THRESHOLD_MODE_EXACT:
            continue
        vector = key[vector_offset + spec.vector_index]
        if vector[spec.atom_index]:
            continue
        _record(
            (spec.kind, spec.acc_name),
            _scans_until_threshold_event(spec, before_snap, kernel),
        )

    return pending_sources


def _advance_to_event_threshold(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _KernelSnapshot,
    key: tuple[Any, ...],
) -> _EventAdvanceState | None:
    """Advance accumulators to just before the nearest event crossing.

    Returns the intermediate state needed by ``_step_event_from_advance``,
    or ``None`` if no pending events can be resolved.  *before_snap* must
    precede the current kernel state by one step.
    """
    source_scans = _pending_event_source_scans(context, kernel, before_snap, key)
    if not source_scans:
        return None

    next_event_scans = min(source_scans.values())
    skipped_scans = max(next_event_scans - 1, 0)
    pre_advance_counter_acc: dict[str, int] = {}
    for kind, acc_name in source_scans:
        if kind in {_DONE_KIND_COUNT_UP, _DONE_KIND_COUNT_DOWN, _DONE_KIND_TIME_DRUM}:
            pre_advance_counter_acc[acc_name] = int(kernel.tags.get(acc_name, 0) or 0)
        _advance_hidden_progress(kind, acc_name, skipped_scans, before_snap, kernel)

    due_sources = {
        source for source, scans in source_scans.items() if scans == next_event_scans
    }
    return _EventAdvanceState(
        pre_event_snapshot=_snapshot_kernel(kernel),
        before_snap=before_snap,
        pre_advance_counter_acc=dict(pre_advance_counter_acc),
        pending_sources=due_sources,
        source_scans=dict(source_scans),
        next_event_scans=next_event_scans,
    )


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
    _fixup_unfired_counters(
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
    if _reset_during_event(
        context, advance.pre_event_snapshot, kernel, pending_sources=advance.pending_sources
    ):
        return None
    return _HiddenEventOutcome(
        snapshot=_snapshot_kernel(kernel),
        key=edge_comp.state_key(kernel),
        additional_scans=advance.next_event_scans,
        pre_event_snapshot=advance.pre_event_snapshot,
    )


def _resolve_nearest_exact_hidden_event(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _KernelSnapshot,
    key: tuple[Any, ...],
    edge_comp: _EdgeCompressor,
) -> _HiddenEventOutcome | None:
    """Advance to the nearest hidden Done/threshold event and step once.

    Returns one outcome, or ``None`` if no pending events can be resolved.
    ``additional_scans`` counts all concrete scans beyond the caller's
    already-executed step. *before_snap* must precede the current
    kernel state by one step.
    """
    advance = _advance_to_event_threshold(context, kernel, before_snap, key)
    if advance is None:
        return None
    return _step_event_from_advance(context, kernel, advance, edge_comp)


def _settle_exact_pending(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _KernelSnapshot,
    edge_comp: _EdgeCompressor,
) -> _HiddenEventOutcome | None:
    """Resolve all pending timers/counters so the system reaches a stable state.

    *before_snap* must be from before the most recent ``_step_kernel`` call
    so that the per-scan delta can be computed (acc_after − acc_before).
    """
    base_snap = _snapshot_kernel(kernel)
    key = edge_comp.state_key(kernel)
    total_additional_scans = 0
    event_count = len(context.done_event_specs) + sum(
        1 for spec in context.threshold_event_specs if spec.mode == _THRESHOLD_MODE_EXACT
    )
    changed = False
    for _ in range(event_count + 1):
        resolved = _resolve_nearest_exact_hidden_event(context, kernel, before_snap, key, edge_comp)
        if resolved is None:
            break
        changed = True
        key = resolved.key
        total_additional_scans += resolved.additional_scans
        assert resolved.pre_event_snapshot is not None
        before_snap = resolved.pre_event_snapshot
    if not changed:
        _restore_kernel(kernel, base_snap)
        return None
    outcome = _HiddenEventOutcome(_snapshot_kernel(kernel), key, total_additional_scans)
    _restore_kernel(kernel, base_snap)
    return outcome


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
    scans = _scans_until_threshold_event(spec, before_snap, kernel)
    if scans is None:
        return None
    pre_advance_counter_acc: dict[str, int] = {}
    if spec.kind in {_DONE_KIND_COUNT_UP, _DONE_KIND_COUNT_DOWN}:
        pre_advance_counter_acc[spec.acc_name] = int(kernel.tags.get(spec.acc_name, 0) or 0)
    skipped_scans = max(scans - 1, 0)
    _advance_hidden_progress(spec.kind, spec.acc_name, skipped_scans, before_snap, kernel)
    pre_event_snapshot = _snapshot_kernel(kernel)
    _step_kernel(context, kernel)
    _fixup_unfired_counters(
        context, before_snap, pre_advance_counter_acc, pre_event_snapshot, kernel
    )
    if not _threshold_crossed(kernel, spec.kind, spec.acc_name, spec.threshold, spec.form):
        return None
    return _HiddenEventOutcome(
        snapshot=_snapshot_kernel(kernel),
        key=edge_comp.state_key(kernel),
        additional_scans=scans,
        pre_event_snapshot=pre_event_snapshot,
        caveats=_ABSTRACT_THRESHOLD_TRACE_CAVEAT,
    )


def _abstract_threshold_outcomes(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _KernelSnapshot,
    key: tuple[Any, ...],
    edge_comp: _EdgeCompressor,
) -> list[_HiddenEventOutcome]:
    """Emit abstract threshold-crossing branches from the current plateau."""
    vector_offset = len(context.stateful_names)
    base_snap = _snapshot_kernel(kernel)
    outcomes: list[_HiddenEventOutcome] = []
    seen_keys: set[tuple[Any, ...]] = set()

    for spec in context.threshold_event_specs:
        if spec.mode == _THRESHOLD_MODE_EXACT:
            continue
        vector = key[vector_offset + spec.vector_index]
        if vector[spec.atom_index]:
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

    base_snap = _snapshot_kernel(kernel)
    outcomes: list[_HiddenEventOutcome] = []
    seen_keys: set[tuple[Any, ...]] = set()

    exact = _settle_exact_pending(context, kernel, before_snap, edge_comp)
    if exact is not None and exact.key not in seen_keys:
        seen_keys.add(exact.key)
        outcomes.append(exact)

    for outcome in _abstract_threshold_outcomes(context, kernel, before_snap, key, edge_comp):
        _restore_kernel(kernel, outcome.snapshot)
        if outcome.pre_event_snapshot is not None:
            settled = _settle_exact_pending(
                context,
                kernel,
                outcome.pre_event_snapshot,
                edge_comp,
            )
            if settled is not None:
                outcome = _HiddenEventOutcome(
                    snapshot=settled.snapshot,
                    key=settled.key,
                    additional_scans=outcome.additional_scans + settled.additional_scans,
                    caveats=_merge_caveats(outcome.caveats, settled.caveats),
                )
        if outcome.key in seen_keys:
            continue
        seen_keys.add(outcome.key)
        outcomes.append(outcome)

    _restore_kernel(kernel, base_snap)
    if outcomes and active_cache is not None and cache_key is not None:
        active_cache._settle_cache[cache_key] = tuple(outcomes)
    return outcomes


def _maybe_jump_hidden_event(
    context: _ExploreContext,
    kernel: ReplayKernel,
    snap: _KernelSnapshot,
    visited: set[tuple[Any, ...]] | dict[tuple[Any, ...], set[tuple[Any, ...]]],
    new_key: tuple[Any, ...],
    edge_comp: _EdgeCompressor,
    cache: _HiddenEventCache | None = None,
    visited_key: tuple[Any, ...] | None = None,
) -> list[_HiddenEventOutcome]:
    """Jump from a revisited hidden pending plateau to future hidden-event states.

    When the event fires, the final crossing scan is explored with ALL
    nondeterministic input combinations — not just the inputs that
    triggered the revisit.  This is necessary because edge inputs (e.g.
    rise/fall sources) can change during the multi-scan accumulation
    period, and combinational outputs on the crossing scan depend on
    which edges are active.
    """
    membership_key = visited_key if visited_key is not None else new_key
    if (
        not (context.done_event_specs or context.threshold_event_specs)
        or membership_key not in visited
    ):
        return []

    cache_key = cache.plateau_key(context, snap, kernel, new_key) if cache is not None else None
    active_cache = cache if cache_key is not None else None
    if active_cache is not None and cache_key is not None:
        cached = active_cache._jump_cache.get(cache_key)
        if cached is not None:
            return list(cached)

    base_snap = _snapshot_kernel(kernel)
    outcomes: list[_HiddenEventOutcome] = []
    seen_keys: set[tuple[Any, ...]] = set()

    advance = _advance_to_event_threshold(context, kernel, snap, new_key)
    if advance is not None:
        nd_dims = context.nondeterministic_dims
        edge_names = tuple(n for n in context.edge_tag_names if n in nd_dims)
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
                    if outcome is not None and outcome.key not in seen_keys:
                        seen_keys.add(outcome.key)
                        any_variant = is_input_variant or is_prev_variant
                        caveats = (
                            _merge_caveats(outcome.caveats, _EVENT_INPUT_VARIANT_CAVEAT)
                            if any_variant
                            else outcome.caveats
                        )
                        outcomes.append(
                            _HiddenEventOutcome(
                                snapshot=outcome.snapshot,
                                key=outcome.key,
                                additional_scans=outcome.additional_scans,
                                pre_event_snapshot=outcome.pre_event_snapshot,
                                caveats=caveats,
                                event_inputs=variant_inputs if any_variant else None,
                            )
                        )
        else:
            _restore_kernel(kernel, advance.pre_event_snapshot)
            outcome = _step_event_from_advance(context, kernel, advance, edge_comp)
            if outcome is not None:
                seen_keys.add(outcome.key)
                outcomes.append(outcome)
        _restore_kernel(kernel, base_snap)

    for outcome in _abstract_threshold_outcomes(context, kernel, snap, new_key, edge_comp):
        if outcome.key in seen_keys:
            continue
        seen_keys.add(outcome.key)
        outcomes.append(outcome)

    _restore_kernel(kernel, base_snap)
    if active_cache is not None and cache_key is not None:
        active_cache._jump_cache[cache_key] = tuple(outcomes)
    return outcomes
