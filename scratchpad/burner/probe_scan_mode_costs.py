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
from collections import defaultdict
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
    cause_depth = 0

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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-scans", type=int, default=40_000)
    parser.add_argument("--wall-seconds", type=float, default=300.0)
    args = parser.parse_args()
    run_probe(args.max_scans, args.wall_seconds)


if __name__ == "__main__":
    main()
