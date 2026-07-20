"""Measure reusable rung snapshots and capture outcomes on BurnerLoop.

This probe monkeypatches instrumentation around the existing executor. It does
not reuse views or alter capture results, so the PLC follows the production
execution path while we count the opportunity.

Run:

    uv run python -u -m scratchpad.burner.probe_condition_view_reuse
"""

from __future__ import annotations

import argparse
import importlib
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pyrung import PLC
from pyrung.core import executor
from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.context import ConditionView, ScanContext


@dataclass
class ContextGeneration:
    """Conservative mutation generation and last view generation per scope."""

    generation: int = 0
    scope_generations: dict[object, int] = field(default_factory=dict)


@dataclass
class ViewStats:
    """Condition-view creation and potential-reuse counters."""

    calls: int = 0
    no_prior_view: int = 0
    scope_mismatch: int = 0
    exact_reusable: int = 0
    generation_reusable: int = 0
    exact_after_setter: int = 0
    copied_shallow_bytes: int = 0
    reusable_shallow_bytes: int = 0
    pending_sizes: Counter[tuple[int, int]] = field(default_factory=Counter)
    reusable_pending_sizes: Counter[tuple[int, int]] = field(default_factory=Counter)


@dataclass
class CaptureStats:
    """Capture-journal sizes and finalized result categories."""

    calls: int = 0
    retain_all_calls: int = 0
    journal_entries: int = 0
    returned_entries: int = 0
    empty_journal: int = 0
    no_effective_writes: int = 0
    filtered_to_empty: int = 0
    returned_writes: int = 0
    journal_sizes: Counter[int] = field(default_factory=Counter)
    result_sizes: Counter[int] = field(default_factory=Counter)


def _percent(part: int, whole: int) -> str:
    return f"{part / whole:.2%}" if whole else "n/a"


def _print_histogram(title: str, counts: Counter[Any], total: int, limit: int = 12) -> None:
    print(f"\n{title}")
    print("value                 calls       share")
    print("--------------------  ----------  -------")
    for value, count in counts.most_common(limit):
        print(f"{str(value):20}  {count:10,d}  {_percent(count, total):>7}")
    omitted = len(counts) - limit
    if omitted > 0:
        print(f"... {omitted} less-common values")


def run_probe(max_scans: int, wall_seconds: float) -> None:
    generations: dict[ScanContext, ContextGeneration] = {}
    view_stats = ViewStats()
    capture_stats = CaptureStats()

    def generation_for(ctx: ScanContext) -> ContextGeneration:
        generation = generations.get(ctx)
        if generation is None:
            generation = ContextGeneration()
            generations[ctx] = generation
        return generation

    original_new_view = executor._new_condition_view
    original_finalize = ScanContext._finalize_capture
    setter_names = (
        "set_tag",
        "set_tags",
        "_set_tag_internal",
        "_set_tags_internal",
        "set_memory",
        "set_memory_bulk",
    )
    original_setters = {name: getattr(ScanContext, name) for name in setter_names}

    def observed_new_view(ctx: ScanContext) -> ConditionView:
        stats = view_stats
        stats.calls += 1
        prior = ctx._condition_snapshot
        same_scope = prior is not None and prior.scope_token is ctx._condition_scope_token
        generation = generation_for(ctx)
        sizes = (len(ctx._tags_pending), len(ctx._memory_pending))
        stats.pending_sizes[sizes] += 1
        copied_bytes = sys.getsizeof(ctx._tags_pending) + sys.getsizeof(ctx._memory_pending)
        stats.copied_shallow_bytes += copied_bytes

        if prior is None:
            stats.no_prior_view += 1
        elif not same_scope:
            stats.scope_mismatch += 1
        else:
            exact = (
                prior._tags_snapshot == ctx._tags_pending
                and prior._memory_snapshot == ctx._memory_pending
            )
            prior_generation = generation.scope_generations.get(ctx._condition_scope_token)
            conservative = prior_generation == generation.generation
            if exact:
                stats.exact_reusable += 1
                stats.reusable_pending_sizes[sizes] += 1
                stats.reusable_shallow_bytes += copied_bytes
                if not conservative:
                    stats.exact_after_setter += 1
            if conservative:
                stats.generation_reusable += 1
                if not exact:
                    raise AssertionError("unchanged generation produced a changed snapshot")

        view = original_new_view(ctx)
        generation.scope_generations[ctx._condition_scope_token] = generation.generation
        return view

    def observed_finalize(
        self: ScanContext,
        journal: dict[str, Any],
        *,
        retain_all_writes: bool = False,
    ) -> dict[str, Any] | None:
        result = original_finalize(
            self,
            journal,
            retain_all_writes=retain_all_writes,
        )
        stats = capture_stats
        stats.calls += 1
        stats.retain_all_calls += retain_all_writes
        journal_size = len(journal)
        stats.journal_entries += journal_size
        stats.journal_sizes[journal_size] += 1
        if journal_size == 0:
            stats.empty_journal += 1
        if result is None:
            stats.no_effective_writes += 1
        else:
            result_size = len(result)
            stats.returned_entries += result_size
            stats.result_sizes[result_size] += 1
            if result_size == 0:
                stats.filtered_to_empty += 1
            else:
                stats.returned_writes += 1
        return result

    def tracked_setter(
        original: Callable[..., Any],
    ) -> Callable[..., Any]:
        def tracked(self: ScanContext, *args: Any, **kwargs: Any) -> Any:
            result = original(self, *args, **kwargs)
            generation_for(self).generation += 1
            return result

        return tracked

    executor._new_condition_view = observed_new_view
    ScanContext._finalize_capture = observed_finalize
    for name, original in original_setters.items():
        setattr(ScanContext, name, tracked_setter(original))

    started = time.perf_counter()
    finished = False
    finish_scan: int | None = None
    try:
        logic = importlib.import_module("tests.fixtures.tumbler").logic
        plc = PLC(logic, dt=0.010)
        plc.step()
        target = plc._known_tags_by_name["y_BurnerLoop"]
        for event in pilot_events(plc, target, max_scans=max_scans):
            if event.kind == "finished":
                finished = True
                finish_scan = event.scan
                break
            if time.perf_counter() - started > wall_seconds:
                break
    finally:
        executor._new_condition_view = original_new_view
        ScanContext._finalize_capture = original_finalize
        for name, original in original_setters.items():
            setattr(ScanContext, name, original)

    elapsed = time.perf_counter() - started
    print(
        f"BurnerLoop {'finished' if finished else 'stopped'} in {elapsed:.1f}s"
        + (f" at scan {finish_scan:,}" if finish_scan is not None else "")
    )

    views = view_stats
    print("\ncondition views")
    print(f"new-view calls:                    {views.calls:12,d}")
    print(
        f"exactly reusable:                  {views.exact_reusable:12,d}"
        f"  {_percent(views.exact_reusable, views.calls)}"
    )
    print(
        f"conservative generation reusable: {views.generation_reusable:12,d}"
        f"  {_percent(views.generation_reusable, views.calls)}"
    )
    print(
        f"exact only after setter activity:  {views.exact_after_setter:12,d}"
        f"  {_percent(views.exact_after_setter, views.calls)}"
    )
    print(f"no prior view in scope:            {views.no_prior_view:12,d}")
    print(f"scope mismatch:                    {views.scope_mismatch:12,d}")
    print(f"shallow bytes copied:              {views.copied_shallow_bytes:12,d}")
    print(
        f"shallow bytes exactly reusable:    {views.reusable_shallow_bytes:12,d}"
        f"  {_percent(views.reusable_shallow_bytes, views.copied_shallow_bytes)}"
    )
    _print_histogram(
        "pending sizes at view creation (tags, memory)", views.pending_sizes, views.calls
    )
    _print_histogram(
        "pending sizes for exactly reusable views (tags, memory)",
        views.reusable_pending_sizes,
        views.exact_reusable,
    )

    captures = capture_stats
    print("\ncapture finalization")
    print(f"calls:                  {captures.calls:12,d}")
    print(
        f"empty journals:         {captures.empty_journal:12,d}"
        f"  {_percent(captures.empty_journal, captures.calls)}"
    )
    print(
        f"no effective writes:    {captures.no_effective_writes:12,d}"
        f"  {_percent(captures.no_effective_writes, captures.calls)}"
    )
    print(
        f"filtered to empty:      {captures.filtered_to_empty:12,d}"
        f"  {_percent(captures.filtered_to_empty, captures.calls)}"
    )
    print(
        f"returned writes:        {captures.returned_writes:12,d}"
        f"  {_percent(captures.returned_writes, captures.calls)}"
    )
    print(f"retain-all calls:       {captures.retain_all_calls:12,d}")
    print(f"journal entries:        {captures.journal_entries:12,d}")
    print(f"returned entries:       {captures.returned_entries:12,d}")
    _print_histogram("capture journal sizes", captures.journal_sizes, captures.calls)
    finalized = captures.calls - captures.no_effective_writes
    _print_histogram("non-None capture result sizes", captures.result_sizes, finalized)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-scans", type=int, default=40_000)
    parser.add_argument("--wall-seconds", type=float, default=300.0)
    args = parser.parse_args()
    run_probe(args.max_scans, args.wall_seconds)


if __name__ == "__main__":
    main()
