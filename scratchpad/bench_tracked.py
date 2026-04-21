"""Measure _TrackedList overhead for block-heavy programs.

Run with: uv run python scratchpad/bench_tracked.py
"""

from __future__ import annotations

import statistics
import time
from typing import cast

from pyrung import Bool, Int, Rung, TagType, copy, shift
from pyrung.circuitpy.codegen import compile_kernel
from pyrung.core import Block, CompiledPLC, Program
from pyrung.core.compiled_plc import _KernelRuntimeContext, _TrackedList
from pyrung.core.context import ScanContext


def main():
    # Block-heavy program: 4 shift registers + block copies
    C = Block("C", TagType.BOOL, 1, 200)
    DS = Block("DS", TagType.INT, 1, 100)
    Clock = Bool("Clock")
    Reset = Bool("Reset")
    Enable = Bool("Enable")

    with Program(strict=False) as block_logic:
        for i in range(4):
            base = i * 50 + 1
            with Rung(Enable):
                shift(C.select(base, base + 49)).clock(Clock).reset(Reset)
        for i in range(1, 21):
            with Rung(Enable):
                copy(DS[i] + 1, DS[i + 20])

    compiled = compile_kernel(block_logic)
    print(f"block_specs: {len(compiled.block_specs)}")
    for sym, spec in compiled.block_specs.items():
        print(f"  {sym}: {spec.size} elements, {len(spec.tag_names)} tags")

    WARMUP = 50
    ITERS = 2000

    # --- With _TrackedList (current step_replay) ---
    plc = CompiledPLC(block_logic, dt=0.010, compiled=compiled)
    plc.patch({"Enable": True, "Clock": False, "Reset": False})
    for _ in range(WARMUP):
        plc.step_replay()

    times = []
    for _ in range(10):
        t0 = time.perf_counter()
        for _ in range(ITERS):
            plc.step_replay()
        times.append(time.perf_counter() - t0)
    med_tracked = statistics.median(times)

    # --- Without _TrackedList (skip wrapping, flush all) ---
    def step_replay_no_track(self):
        self._ensure_running()
        ctx = _KernelRuntimeContext(
            tags=self._kernel.tags,
            memory=self._kernel.memory,
            scan_id=self._kernel.scan_id,
            timestamp=self._kernel.timestamp,
        )
        scan_ctx = cast(ScanContext, ctx)
        self._system_runtime.on_scan_start(scan_ctx)
        self._input_overrides.apply_pre_scan(scan_ctx)
        if self._kernel.memory.get("_dt") != self._dt:
            ctx.set_memory("_dt", self._dt)
        self._materialize_system_tags(ctx)

        for spec in self._compiled.block_specs.values():
            self._kernel.load_block_from_tags(spec)
        self._compiled.step_fn(
            self._kernel.tags,
            self._kernel.blocks,
            self._kernel.memory,
            self._kernel.prev,
            self._dt,
        )
        for spec in self._compiled.block_specs.values():
            self._kernel.flush_block_to_tags(spec)

        self._input_overrides.apply_post_logic(scan_ctx)
        for name in self._compiled.edge_tags:
            if name in self._kernel.tags:
                self._kernel.prev[name] = self._kernel.tags[name]
        self._system_runtime.on_scan_end(scan_ctx)
        self._kernel.scan_id += 1
        self._kernel.timestamp += self._dt

    original = CompiledPLC.step_replay
    CompiledPLC.step_replay = step_replay_no_track

    plc2 = CompiledPLC(block_logic, dt=0.010, compiled=compiled)
    plc2.patch({"Enable": True, "Clock": False, "Reset": False})
    for _ in range(WARMUP):
        plc2.step_replay()

    times = []
    for _ in range(10):
        t0 = time.perf_counter()
        for _ in range(ITERS):
            plc2.step_replay()
        times.append(time.perf_counter() - t0)
    med_no_track = statistics.median(times)

    CompiledPLC.step_replay = original

    per_tracked = med_tracked / ITERS * 1e6
    per_no_track = med_no_track / ITERS * 1e6
    overhead = per_tracked - per_no_track

    print(f"\n  With _TrackedList:     {per_tracked:.1f} us/step")
    print(f"  Without _TrackedList:  {per_no_track:.1f} us/step")
    print(f"  Overhead:              {overhead:.1f} us/step  ({overhead/per_tracked*100:.0f}%)")
    print(f"  Speedup:               {med_tracked/med_no_track:.2f}x")


if __name__ == "__main__":
    main()
