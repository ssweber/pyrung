"""Attribute BurnerLoop CPU time to committed scans, replay, and analysis.

The probe wraps runner-level boundaries only. It does not change scan results,
cache policy, replay evidence, or PILOT decisions.

Run:

    uv run python -u -m scratchpad.burner.probe_scan_mode_costs
"""

from __future__ import annotations

import argparse
import importlib
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pyrung.core.runner as runner_module
from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.context import ScanContext
from pyrung.core.executor import NOOP_OBSERVER


@dataclass
class Timing:
    calls: int = 0
    cpu_ns: int = 0

    def add(self, started_ns: int) -> None:
        self.calls += 1
        self.cpu_ns += time.process_time_ns() - started_ns

    @property
    def cpu_seconds(self) -> float:
        return self.cpu_ns / 1e9


@dataclass
class ScanMode:
    scans: Timing = field(default_factory=Timing)
    prepare: Timing = field(default_factory=Timing)
    program: Timing = field(default_factory=Timing)
    commit: Timing = field(default_factory=Timing)
    observed_program_calls: int = 0


@dataclass(frozen=True)
class ProgramWriteCall:
    plc_id: int
    start_scan: int
    end_scan: int
    candidates: frozenset[str]
    cpu_ns: int
    nested_cause_ns: int


@dataclass(frozen=True)
class CycleSnapshotSample:
    tags: Mapping[str, Any]
    ignore: frozenset[str]


@dataclass
class ProbeStats:
    modes: dict[str, ScanMode] = field(default_factory=lambda: defaultdict(ScanMode))
    replay_capture_calls: int = 0
    replay_capture_hits: int = 0
    replay_capture_misses: int = 0
    replay_capture_outer: Timing = field(default_factory=Timing)
    cause_outer: Timing = field(default_factory=Timing)
    state_at: Timing = field(default_factory=Timing)
    slab_fill: Timing = field(default_factory=Timing)
    reconstructed_fork: Timing = field(default_factory=Timing)
    compiled_kernel: Timing = field(default_factory=Timing)
    compiled_step: Timing = field(default_factory=Timing)
    compiled_step_replay: Timing = field(default_factory=Timing)
    prover_context: Timing = field(default_factory=Timing)
    program_written_changes: Timing = field(default_factory=Timing)
    program_write_calls: list[ProgramWriteCall] = field(default_factory=list)
    cycle_fold: Timing = field(default_factory=Timing)
    cycle_fold_scan_ns: int = 0
    cycle_predicate: Timing = field(default_factory=Timing)
    cycle_detection: Timing = field(default_factory=Timing)
    cycle_surface: Timing = field(default_factory=Timing)
    cycle_snapshot_samples: list[CycleSnapshotSample] = field(default_factory=list)
    cycle_real_scans: int = 0
    trace_trees: Timing = field(default_factory=Timing)
    trace_roots: Counter[tuple[str, str]] = field(default_factory=Counter)
    trace_contexts: Counter[tuple[int, str, str]] = field(default_factory=Counter)
    replay_consumer_calls: Counter[str] = field(default_factory=Counter)
    replay_consumer_misses: Counter[str] = field(default_factory=Counter)


def _scan_mode(plc: PLC) -> str:
    if plc._replay_mode:
        return "interpreted replay commit"
    if plc._causal_parent is not None:
        return "PILOT fork commit"
    return "root runner commit"


def _print_timing(label: str, timing: Timing, total_cpu: float) -> None:
    share = timing.cpu_seconds / total_cpu if total_cpu else 0.0
    print(f"{label:30} {timing.calls:8,d}  {timing.cpu_seconds:9.3f}s  {share:7.2%}")


def run_probe(max_scans: int, wall_seconds: float) -> None:
    causal_module = importlib.import_module("pyrung.core.analysis.pilot.causal")
    cyclefold_module = importlib.import_module("pyrung.core.analysis.pilot.cyclefold")
    pilot_module = importlib.import_module("pyrung.core.analysis.pilot.pilot")
    trace_module = importlib.import_module("pyrung.core.analysis.pilot.trace")
    logic = importlib.import_module("tests.fixtures.tumbler").logic
    plc = PLC(logic, dt=0.010)
    plc.step()
    target = plc._known_tags_by_name["y_BurnerLoop"]

    stats = ProbeStats()
    mode_stack: list[str] = []

    @contextmanager
    def in_mode(mode: str):
        mode_stack.append(mode)
        try:
            yield
        finally:
            popped = mode_stack.pop()
            if popped != mode:
                raise RuntimeError("scan-mode instrumentation stack corrupted")

    def current_mode() -> str:
        return mode_stack[-1] if mode_stack else "unscoped"

    original_run_scan = PLC._run_single_scan
    original_prepare = PLC._prepare_scan
    original_commit = PLC._commit_scan
    original_execute_program = runner_module.execute_program
    original_capture = PLC._replay_capture_at
    original_cause = PLC.cause
    original_state_at = PLC._state_at
    original_slab_fill = PLC._replay_slab_fill
    original_reconstructed_fork = PLC._fork_from_reconstructed_state
    original_compiled_kernel = PLC._compiled_replay_supported_kernel
    original_compiled_step = CompiledPLC.step
    original_compiled_step_replay = CompiledPLC.step_replay
    original_node_views = PLC._replay_node_views_at
    original_rung_runs = PLC._replay_rung_runs_at
    original_node_reads = PLC._replay_node_reads_at
    original_prover_context = pilot_module._build_prover_context
    original_program_written_changes = causal_module._program_written_changes
    original_cycle_fold = cyclefold_module.cycle_fold_until
    original_detect_cycle = cyclefold_module.detect_cycle
    original_monotone_surface = cyclefold_module._monotone_read_surface
    original_trace_back = trace_module._trace_back
    cause_depth = 0
    trace_depth = 0

    def committed_scan_ns() -> int:
        return sum(mode.scans.cpu_ns for mode in stats.modes.values())

    def observed_run_scan(self: PLC, *, consume_pause_request: bool):
        mode = _scan_mode(self)
        started = time.process_time_ns()
        with in_mode(mode):
            try:
                return original_run_scan(self, consume_pause_request=consume_pause_request)
            finally:
                stats.modes[mode].scans.add(started)

    def observed_prepare(self: PLC, *args: Any, **kwargs: Any):
        mode = current_mode()
        started = time.process_time_ns()
        try:
            return original_prepare(self, *args, **kwargs)
        finally:
            stats.modes[mode].prepare.add(started)

    def observed_commit(self: PLC, ctx: ScanContext, dt: float) -> None:
        mode = current_mode()
        started = time.process_time_ns()
        try:
            return original_commit(self, ctx, dt)
        finally:
            stats.modes[mode].commit.add(started)

    def observed_execute_program(*args: Any, **kwargs: Any) -> None:
        mode = current_mode()
        observer = kwargs.get("observer", NOOP_OBSERVER)
        started = time.process_time_ns()
        try:
            return original_execute_program(*args, **kwargs)
        finally:
            mode_stats = stats.modes[mode]
            mode_stats.program.add(started)
            if observer is not NOOP_OBSERVER:
                mode_stats.observed_program_calls += 1

    def observed_capture(self: PLC, target_scan_id: int):
        stats.replay_capture_calls += 1
        cached = self._cached_replay_capture
        if cached is not None and cached[0] == target_scan_id:
            stats.replay_capture_hits += 1
        else:
            stats.replay_capture_misses += 1

        if current_mode() == "cause observed target":
            return original_capture(self, target_scan_id)

        started = time.process_time_ns()
        with in_mode("cause observed target"):
            try:
                return original_capture(self, target_scan_id)
            finally:
                stats.replay_capture_outer.add(started)

    def observed_cause(self: PLC, *args: Any, **kwargs: Any):
        nonlocal cause_depth
        outer = cause_depth == 0
        cause_depth += 1
        started = time.process_time_ns()
        try:
            return original_cause(self, *args, **kwargs)
        finally:
            cause_depth -= 1
            if outer:
                stats.cause_outer.add(started)

    def timed_call(timing: Timing, original: Any, *args: Any, **kwargs: Any):
        started = time.process_time_ns()
        try:
            return original(*args, **kwargs)
        finally:
            timing.add(started)

    def observed_state_at(self: PLC, scan_id: int):
        return timed_call(stats.state_at, original_state_at, self, scan_id)

    def observed_slab_fill(self: PLC, scan_id: int):
        return timed_call(stats.slab_fill, original_slab_fill, self, scan_id)

    def observed_reconstructed_fork(self: PLC, *args: Any, **kwargs: Any):
        return timed_call(
            stats.reconstructed_fork, original_reconstructed_fork, self, *args, **kwargs
        )

    def observed_compiled_kernel(self: PLC):
        return timed_call(stats.compiled_kernel, original_compiled_kernel, self)

    def observed_compiled_step(self: CompiledPLC):
        return timed_call(stats.compiled_step, original_compiled_step, self)

    def observed_compiled_step_replay(self: CompiledPLC) -> None:
        return timed_call(stats.compiled_step_replay, original_compiled_step_replay, self)

    def observed_capture_consumer(
        name: str,
        original: Any,
        self: PLC,
        target_scan_id: int,
    ):
        stats.replay_consumer_calls[name] += 1
        cached = self._cached_replay_capture
        if cached is None or cached[0] != target_scan_id:
            stats.replay_consumer_misses[name] += 1
        return original(self, target_scan_id)

    def observed_node_views(self: PLC, target_scan_id: int):
        return observed_capture_consumer(
            "node views",
            original_node_views,
            self,
            target_scan_id,
        )

    def observed_rung_runs(self: PLC, target_scan_id: int):
        return observed_capture_consumer(
            "rung runs",
            original_rung_runs,
            self,
            target_scan_id,
        )

    def observed_node_reads(self: PLC, target_scan_id: int):
        return observed_capture_consumer(
            "node reads",
            original_node_reads,
            self,
            target_scan_id,
        )

    def observed_prover_context(*args: Any, **kwargs: Any):
        return timed_call(stats.prover_context, original_prover_context, *args, **kwargs)

    def observed_program_written_changes(
        self: PLC,
        start_scan: int,
        end_scan: int,
        relevant: frozenset[str],
    ):
        cause_before = stats.cause_outer.cpu_ns
        started = time.process_time_ns()
        try:
            return original_program_written_changes(self, start_scan, end_scan, relevant)
        finally:
            elapsed = time.process_time_ns() - started
            nested_cause_ns = stats.cause_outer.cpu_ns - cause_before
            stats.program_written_changes.calls += 1
            stats.program_written_changes.cpu_ns += elapsed
            stats.program_write_calls.append(
                ProgramWriteCall(
                    plc_id=id(self),
                    start_scan=start_scan,
                    end_scan=end_scan,
                    candidates=relevant,
                    cpu_ns=elapsed,
                    nested_cause_ns=nested_cause_ns,
                )
            )

    def observed_cycle_fold(
        self: PLC,
        predicate: Callable[[Any], bool],
        *args: Any,
        **kwargs: Any,
    ):
        scans_before = committed_scan_ns()
        started = time.process_time_ns()
        fold_ctx = kwargs.get("fold_ctx")
        call_stats = kwargs.get("stats")

        def observed_predicate(state: Any) -> bool:
            return timed_call(stats.cycle_predicate, predicate, state)

        try:
            return original_cycle_fold(
                self,
                observed_predicate,
                *args,
                **kwargs,
            )
        finally:
            stats.cycle_fold.add(started)
            stats.cycle_fold_scan_ns += committed_scan_ns() - scans_before
            if isinstance(call_stats, dict):
                stats.cycle_real_scans += call_stats.get("real_scans", 0)
            if fold_ctx is not None:
                ignore = (
                    fold_ctx.frozen_writes
                    | fold_ctx.churn_excluded
                    | fold_ctx.profile_fb_names
                )
                stats.cycle_snapshot_samples.append(
                    CycleSnapshotSample(tags=self.state.tags, ignore=ignore)
                )

    def observed_detect_cycle(*args: Any, **kwargs: Any):
        return timed_call(stats.cycle_detection, original_detect_cycle, *args, **kwargs)

    def observed_monotone_surface(*args: Any, **kwargs: Any):
        return timed_call(
            stats.cycle_surface,
            original_monotone_surface,
            *args,
            **kwargs,
        )

    def observed_trace_back(
        env: Any,
        tag: str,
        value: Any,
        *args: Any,
        **kwargs: Any,
    ):
        nonlocal trace_depth
        outer = trace_depth == 0
        trace_depth += 1
        started = time.process_time_ns() if outer else 0
        if outer:
            value_key = repr(value)
            stats.trace_roots[(tag, value_key)] += 1
            stats.trace_contexts[(id(env.snapshot), tag, value_key)] += 1
        try:
            return original_trace_back(env, tag, value, *args, **kwargs)
        finally:
            trace_depth -= 1
            if outer:
                stats.trace_trees.add(started)

    PLC._run_single_scan = observed_run_scan
    PLC._prepare_scan = observed_prepare
    PLC._commit_scan = observed_commit
    runner_module.execute_program = observed_execute_program
    PLC._replay_capture_at = observed_capture
    PLC.cause = observed_cause
    PLC._state_at = observed_state_at
    PLC._replay_slab_fill = observed_slab_fill
    PLC._fork_from_reconstructed_state = observed_reconstructed_fork
    PLC._compiled_replay_supported_kernel = observed_compiled_kernel
    CompiledPLC.step = observed_compiled_step
    CompiledPLC.step_replay = observed_compiled_step_replay
    PLC._replay_node_views_at = observed_node_views
    PLC._replay_rung_runs_at = observed_rung_runs
    PLC._replay_node_reads_at = observed_node_reads
    pilot_module._build_prover_context = observed_prover_context
    causal_module._program_written_changes = observed_program_written_changes
    cyclefold_module.cycle_fold_until = observed_cycle_fold
    cyclefold_module.detect_cycle = observed_detect_cycle
    cyclefold_module._monotone_read_surface = observed_monotone_surface
    trace_module._trace_back = observed_trace_back

    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    event_count = 0
    finished = False
    finish_scan: int | None = None
    try:
        for event in pilot_events(plc, target, max_scans=max_scans):
            event_count += 1
            if event.kind == "finished":
                finished = True
                finish_scan = event.scan
                break
            if time.perf_counter() - wall_started > wall_seconds:
                break
    finally:
        PLC._run_single_scan = original_run_scan
        PLC._prepare_scan = original_prepare
        PLC._commit_scan = original_commit
        runner_module.execute_program = original_execute_program
        PLC._replay_capture_at = original_capture
        PLC.cause = original_cause
        PLC._state_at = original_state_at
        PLC._replay_slab_fill = original_slab_fill
        PLC._fork_from_reconstructed_state = original_reconstructed_fork
        PLC._compiled_replay_supported_kernel = original_compiled_kernel
        CompiledPLC.step = original_compiled_step
        CompiledPLC.step_replay = original_compiled_step_replay
        PLC._replay_node_views_at = original_node_views
        PLC._replay_rung_runs_at = original_rung_runs
        PLC._replay_node_reads_at = original_node_reads
        pilot_module._build_prover_context = original_prover_context
        causal_module._program_written_changes = original_program_written_changes
        cyclefold_module.cycle_fold_until = original_cycle_fold
        cyclefold_module.detect_cycle = original_detect_cycle
        cyclefold_module._monotone_read_surface = original_monotone_surface
        trace_module._trace_back = original_trace_back

    wall = time.perf_counter() - wall_started
    cpu = time.process_time() - cpu_started
    print(
        f"BurnerLoop {'finished' if finished else 'stopped'}: {event_count} events, "
        f"{wall:.3f}s wall, {cpu:.3f}s CPU"
        + (f", scan {finish_scan:,}" if finish_scan is not None else "")
    )

    print("\ncommitted scan modes")
    print("mode                              calls        CPU     total")
    print("--------------------------------  --------  ---------  -------")
    committed_cpu = 0.0
    for name, mode in sorted(stats.modes.items()):
        if mode.scans.calls == 0:
            continue
        _print_timing(name, mode.scans, cpu)
        committed_cpu += mode.scans.cpu_seconds
        accounted = mode.prepare.cpu_seconds + mode.program.cpu_seconds + mode.commit.cpu_seconds
        residual = mode.scans.cpu_seconds - accounted
        print(
            f"  prepare/program/commit/residual: "
            f"{mode.prepare.cpu_seconds:.3f}s / {mode.program.cpu_seconds:.3f}s / "
            f"{mode.commit.cpu_seconds:.3f}s / {residual:.3f}s"
        )

    cause_mode = stats.modes["cause observed target"]
    print("\ncausal replay")
    print("operation                         calls        CPU     total")
    print("--------------------------------  --------  ---------  -------")
    _print_timing("capture envelope", stats.replay_capture_outer, cpu)
    print(
        f"  requested/hit/miss: {stats.replay_capture_calls:,} / "
        f"{stats.replay_capture_hits:,} / {stats.replay_capture_misses:,}"
    )
    _print_timing("observed target prepare", cause_mode.prepare, cpu)
    _print_timing("observed target program", cause_mode.program, cpu)
    print(f"  observed program calls: {cause_mode.observed_program_calls:,}")
    _print_timing("historical state_at", stats.state_at, cpu)
    _print_timing("replay slab fill", stats.slab_fill, cpu)
    _print_timing("reconstructed fork", stats.reconstructed_fork, cpu)
    _print_timing("compiled-kernel ensure", stats.compiled_kernel, cpu)
    _print_timing("compiled materialized step", stats.compiled_step, cpu)
    _print_timing("compiled lightweight step", stats.compiled_step_replay, cpu)
    print("\nreplay capture consumers")
    for name, calls in stats.replay_consumer_calls.items():
        misses = stats.replay_consumer_misses[name]
        print(f"  {name:12} calls={calls:,} first-on-target={misses:,}")

    cause_reasoning_cpu = stats.cause_outer.cpu_seconds - stats.replay_capture_outer.cpu_seconds
    print("\ncause envelope")
    _print_timing("outer cause()", stats.cause_outer, cpu)
    print(f"  replay capture: {stats.replay_capture_outer.cpu_seconds:.3f}s")
    print(f"  remaining causal reasoning: {cause_reasoning_cpu:.3f}s")

    top_level_accounted = committed_cpu + stats.cause_outer.cpu_seconds
    other_cpu = cpu - top_level_accounted
    print("\nadditive top-level CPU")
    print(f"committed scans:       {committed_cpu:9.3f}s  {committed_cpu / cpu:7.2%}")
    print(
        f"cause incl. replay:    {stats.cause_outer.cpu_seconds:9.3f}s  "
        f"{stats.cause_outer.cpu_seconds / cpu:7.2%}"
    )
    print(f"other analysis/control:{other_cpu:9.3f}s  {other_cpu / cpu:7.2%}")

    program_write_nested_cause = sum(
        call.nested_cause_ns for call in stats.program_write_calls
    )
    program_write_exclusive = (
        stats.program_written_changes.cpu_ns - program_write_nested_cause
    ) / 1e9
    cycle_fold_exclusive = (
        stats.cycle_fold.cpu_ns - stats.cycle_fold_scan_ns
    ) / 1e9
    print("\nselected analysis/control boundaries")
    print("operation                         calls        CPU     total")
    print("--------------------------------  --------  ---------  -------")
    _print_timing("one-time prover context", stats.prover_context, cpu)
    _print_timing("trace trees", stats.trace_trees, cpu)
    _print_timing("empirical program writes", stats.program_written_changes, cpu)
    print(
        f"  excluding nested cause(): {program_write_exclusive:.3f}s; "
        f"nested cause(): {program_write_nested_cause / 1e9:.3f}s"
    )
    _print_timing("cycle-fold envelope", stats.cycle_fold, cpu)
    print(
        f"  excluding committed scans: {cycle_fold_exclusive:.3f}s; "
        f"committed scans: {stats.cycle_fold_scan_ns / 1e9:.3f}s"
    )
    _print_timing("  cycle predicates", stats.cycle_predicate, cpu)
    _print_timing("  cycle detection", stats.cycle_detection, cpu)
    _print_timing("  crossing surface", stats.cycle_surface, cpu)
    known_cycle_control = (
        stats.cycle_predicate.cpu_ns
        + stats.cycle_detection.cpu_ns
        + stats.cycle_surface.cpu_ns
    ) / 1e9
    print(
        f"  remaining loop/snapshot control: "
        f"{cycle_fold_exclusive - known_cycle_control:.3f}s over "
        f"{stats.cycle_real_scans:,} real scans"
    )

    if stats.program_write_calls:
        print("\nempirical program-write call shapes")
        for index, call in enumerate(stats.program_write_calls, start=1):
            print(
                f"{index:2d}. plc={call.plc_id} scans={call.start_scan:,}.."
                f"{call.end_scan:,} ({call.end_scan - call.start_scan + 1:,}) "
                f"candidates={len(call.candidates):,} "
                f"cpu={call.cpu_ns / 1e9:.3f}s "
                f"cause={call.nested_cause_ns / 1e9:.3f}s"
            )
        exact_shapes = Counter(
            (call.plc_id, call.start_scan, call.end_scan, call.candidates)
            for call in stats.program_write_calls
        )
        repeated_shapes = sum(count - 1 for count in exact_shapes.values())
        print(f"  exact repeated call shapes: {repeated_shapes:,}")

    repeated_roots = sum(count - 1 for count in stats.trace_roots.values())
    repeated_contexts = sum(count - 1 for count in stats.trace_contexts.values())
    print("\ntrace reuse evidence")
    print(
        f"  root requests / repeated tag-value roots: "
        f"{stats.trace_trees.calls:,} / {repeated_roots:,}"
    )
    print(f"  repeated exact snapshot/tag/value contexts: {repeated_contexts:,}")
    for (tag, value), count in stats.trace_roots.most_common(8):
        print(f"  {count:5,d}x {tag}={value}")

    if stats.cycle_snapshot_samples:
        sample = max(stats.cycle_snapshot_samples, key=lambda item: len(item.tags))
        tags = sample.tags
        ignore = sample.ignore
        keep = tuple(key for key in tags if key not in ignore)
        iterations = 1_000

        def current_snapshot() -> dict[str, Any]:
            return {key: value for key, value in tags.items() if key not in ignore}

        def indexed_snapshot() -> dict[str, Any]:
            return {key: tags[key] for key in keep}

        current_started = time.process_time_ns()
        for _ in range(iterations):
            current_snapshot()
        current_ns = time.process_time_ns() - current_started

        indexed_started = time.process_time_ns()
        for _ in range(iterations):
            indexed_snapshot()
        indexed_ns = time.process_time_ns() - indexed_started

        print("\ncycle snapshot microbenchmark (outside run total)")
        print(
            f"  tags / ignored / retained: {len(tags):,} / {len(ignore):,} / "
            f"{len(keep):,}"
        )
        print(
            f"  current items+filter: {current_ns / 1e9:.3f}s / "
            f"{iterations:,} copies"
        )
        print(
            f"  precomputed keys:     {indexed_ns / 1e9:.3f}s / "
            f"{iterations:,} copies"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-scans", type=int, default=40_000)
    parser.add_argument("--wall-seconds", type=float, default=300.0)
    args = parser.parse_args()
    run_probe(args.max_scans, args.wall_seconds)


if __name__ == "__main__":
    main()
