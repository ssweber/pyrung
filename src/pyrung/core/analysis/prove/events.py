"""Hidden-event scheduling helpers for prove BFS."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.kernel import ReplayKernel

from . import PENDING
from .absorb import (
    _DONE_KIND_COUNT_UP,
    _DONE_KIND_OFF_DELAY,
    _DONE_KIND_ON_DELAY,
    _PROGRESS_KIND_INT_UP,
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
    preset: int


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


def _timer_total(kernel: ReplayKernel, acc_name: str) -> float:
    """Return timer progress as accumulator plus fractional remainder."""
    frac_key = f"_frac:{acc_name}"
    acc = int(kernel.tags.get(acc_name, 0) or 0)
    frac = float(kernel.memory.get(frac_key, 0.0) or 0.0)
    return acc + frac


def _scans_until_done_event(
    kind: str,
    preset: int,
    acc_name: str,
    before: _KernelSnapshot,
    kernel: ReplayKernel,
) -> int | None:
    """Estimate scans until this pending timer/counter reaches its next Done event."""
    acc_before = int(before.tags.get(acc_name, 0) or 0)
    acc_after = int(kernel.tags.get(acc_name, 0) or 0)

    if kind in {_DONE_KIND_ON_DELAY, _DONE_KIND_OFF_DELAY}:
        before_total = acc_before + float(before.memory.get(f"_frac:{acc_name}", 0.0) or 0.0)
        after_total = _timer_total(kernel, acc_name)
        delta = after_total - before_total
        remaining = preset - after_total
    elif kind == _DONE_KIND_COUNT_UP:
        delta = acc_after - acc_before
        remaining = preset - acc_after
    else:
        delta = acc_before - acc_after
        remaining = preset + acc_after

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
    acc_before = int(before.tags.get(acc_name, 0) or 0)
    acc_after = int(kernel.tags.get(acc_name, 0) or 0)

    if kind in {_DONE_KIND_ON_DELAY, _DONE_KIND_OFF_DELAY}:
        before_total = acc_before + float(before.memory.get(f"_frac:{acc_name}", 0.0) or 0.0)
        after_total = _timer_total(kernel, acc_name)
        return after_total - before_total, after_total

    if kind in {_DONE_KIND_COUNT_UP, _PROGRESS_KIND_INT_UP}:
        return float(acc_after - acc_before), float(acc_after)

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

    acc_before = int(before.tags.get(acc_name, 0) or 0)
    acc_after = int(kernel.tags.get(acc_name, 0) or 0)

    if kind in {_DONE_KIND_ON_DELAY, _DONE_KIND_OFF_DELAY}:
        before_total = acc_before + float(before.memory.get(f"_frac:{acc_name}", 0.0) or 0.0)
        after_total = _timer_total(kernel, acc_name)
        delta = after_total - before_total
        target_total = after_total + (skipped_scans * delta)
        target_acc = int(target_total)
        kernel.tags[acc_name] = target_acc
        kernel.memory[f"_frac:{acc_name}"] = target_total - target_acc
        return

    if kind in {_DONE_KIND_COUNT_UP, _PROGRESS_KIND_INT_UP}:
        delta = acc_after - acc_before
        kernel.tags[acc_name] = acc_after + (skipped_scans * delta)
        return

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


def _resolve_nearest_exact_hidden_event(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _KernelSnapshot,
    key: tuple[Any, ...],
    edge_comp: _EdgeCompressor,
) -> tuple[tuple[Any, ...], int] | None:
    """Advance to the nearest hidden Done/threshold event and step once.

    Returns ``(new_key, additional_scans)``, or ``None`` if no pending events
    can be resolved. ``additional_scans`` is the skipped scan count beyond the
    caller's already-executed step. *before_snap* must precede the current
    kernel state by one step.
    """
    pending_sources: dict[tuple[str, str], int] = {}
    pending_scans: list[int] = []

    for spec in context.done_event_specs:
        if key[spec.state_index] != PENDING:
            continue
        scans = _scans_until_done_event(spec.kind, spec.preset, spec.acc_name, before_snap, kernel)
        if scans is not None:
            pending_scans.append(scans)
            pending_sources[(spec.kind, spec.acc_name)] = scans

    vector_offset = len(context.stateful_names)
    for spec in context.threshold_event_specs:
        if spec.mode != _THRESHOLD_MODE_EXACT:
            continue
        vector = key[vector_offset + spec.vector_index]
        if vector[spec.atom_index]:
            continue
        scans = _scans_until_threshold_event(spec, before_snap, kernel)
        if scans is not None:
            pending_scans.append(scans)
            pending_sources[(spec.kind, spec.acc_name)] = scans

    if not pending_scans:
        return None

    next_event_scans = min(pending_scans)
    skipped_scans = max(next_event_scans - 1, 0)
    for kind, acc_name in pending_sources:
        _advance_hidden_progress(kind, acc_name, skipped_scans, before_snap, kernel)

    _step_kernel(context, kernel)
    return edge_comp.state_key(kernel), skipped_scans


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
        key, additional_scans = resolved
        total_additional_scans += additional_scans
        before_snap = _snapshot_kernel(kernel)
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
    skipped_scans = max(scans - 1, 0)
    _advance_hidden_progress(spec.kind, spec.acc_name, skipped_scans, before_snap, kernel)
    _step_kernel(context, kernel)
    if not _threshold_crossed(kernel, spec.acc_name, spec.threshold, spec.form):
        return None
    return _HiddenEventOutcome(
        snapshot=_snapshot_kernel(kernel),
        key=edge_comp.state_key(kernel),
        additional_scans=1,
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
) -> list[_HiddenEventOutcome]:
    """Resolve pending exact events and emit abstract threshold branches."""
    key = edge_comp.state_key(kernel)
    outcomes: list[_HiddenEventOutcome] = []
    seen_keys: set[tuple[Any, ...]] = set()

    exact = _settle_exact_pending(context, kernel, before_snap, edge_comp)
    if exact is not None and exact.key not in seen_keys:
        seen_keys.add(exact.key)
        outcomes.append(exact)

    for outcome in _abstract_threshold_outcomes(context, kernel, before_snap, key, edge_comp):
        if outcome.key in seen_keys:
            continue
        seen_keys.add(outcome.key)
        outcomes.append(outcome)

    return outcomes


def _maybe_jump_hidden_event(
    context: _ExploreContext,
    kernel: ReplayKernel,
    snap: _KernelSnapshot,
    visited: set[tuple[Any, ...]],
    new_key: tuple[Any, ...],
    edge_comp: _EdgeCompressor,
) -> list[_HiddenEventOutcome]:
    """Jump from a revisited hidden pending plateau to future hidden-event states."""
    if not (context.done_event_specs or context.threshold_event_specs) or new_key not in visited:
        return []

    base_snap = _snapshot_kernel(kernel)
    outcomes: list[_HiddenEventOutcome] = []
    seen_keys: set[tuple[Any, ...]] = set()

    resolved = _resolve_nearest_exact_hidden_event(context, kernel, snap, new_key, edge_comp)
    if resolved is not None:
        resolved_key, additional_scans = resolved
        outcome = _HiddenEventOutcome(_snapshot_kernel(kernel), resolved_key, additional_scans)
        seen_keys.add(outcome.key)
        outcomes.append(outcome)
        _restore_kernel(kernel, base_snap)

    for outcome in _abstract_threshold_outcomes(context, kernel, snap, new_key, edge_comp):
        if outcome.key in seen_keys:
            continue
        seen_keys.add(outcome.key)
        outcomes.append(outcome)

    _restore_kernel(kernel, base_snap)
    return outcomes
