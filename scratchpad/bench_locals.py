"""Step 4: Locals experiment — measure dict access vs local variable access.

Run with: uv run python scratchpad/bench_locals.py
"""

from __future__ import annotations

import statistics
import time

from pyrung import (
    Bool,
    Counter,
    Int,
    Real,
    Rung,
    Timer,
    branch,
    copy,
    count_up,
    on_delay,
    out,
    program,
    reset,
    rise,
)
from pyrung.circuitpy.codegen import compile_kernel
from pyrung.core import CompiledPLC


def _make_busy_program():
    enables = [Bool(f"E{i}") for i in range(20)]
    outputs = [Bool(f"O{i}") for i in range(20)]
    timers = [Timer.clone(f"T{i}") for i in range(8)]
    counters = [Counter.clone(f"C{i}") for i in range(4)]
    accum = [Int(f"A{i}") for i in range(4)]
    temp = [Real(f"R{i}") for i in range(4)]

    @program(strict=False)
    def logic():
        for i in range(8):
            with Rung(enables[i]):
                on_delay(timers[i], preset=200 + i * 50)
        for i in range(4):
            with Rung(rise(enables[8 + i])):
                count_up(counters[i], preset=999).reset(enables[12 + i])
        for i in range(4):
            with Rung(enables[16 + i]):
                with branch(timers[i].Done):
                    out(outputs[i])
                with branch(timers[i + 4].Done):
                    out(outputs[i + 4])
                copy(accum[i] + 1, accum[i])
                copy(temp[i] + 0.1, temp[i])
        for i in range(8, 20):
            with Rung(enables[i % 8]):
                out(outputs[i])

    return logic


def _rewrite_to_locals(source: str) -> str:
    """Transform step_fn to use local variables instead of dict lookups."""
    import re

    lines = source.split("\n")

    tag_reads: set[str] = set()
    for line in lines:
        for m in re.finditer(r'tags\["([^"]+)"\]', line):
            tag_reads.add(m.group(1))

    memory_reads: set[str] = set()
    for line in lines:
        for m in re.finditer(r'memory\["([^"]+)"\]', line):
            memory_reads.add(m.group(1))

    prev_reads: set[str] = set()
    for line in lines:
        for m in re.finditer(r'prev\["([^"]+)"\]', line):
            prev_reads.add(m.group(1))

    def _safe_name(s: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_]", "_", s)

    # Find the function body start
    func_start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("def _kernel_step("):
            func_start = i
            break

    if func_start is None:
        return source

    # Build prologue (loads) and epilogue (stores)
    indent = "    "
    prologue = []
    epilogue = []

    for name in sorted(tag_reads):
        local = f"_t_{_safe_name(name)}"
        prologue.append(f'{indent}{local} = tags["{name}"]')
        epilogue.append(f'{indent}tags["{name}"] = {local}')

    for name in sorted(memory_reads):
        local = f"_m_{_safe_name(name)}"
        prologue.append(f'{indent}{local} = memory.get("{name}")')
        epilogue.append(f'{indent}memory["{name}"] = {local}')

    for name in sorted(prev_reads):
        local = f"_p_{_safe_name(name)}"
        prologue.append(f'{indent}{local} = prev["{name}"]')

    # Rewrite all dict accesses in the body
    new_lines = lines[: func_start + 1]  # up to and including def line
    new_lines.extend(prologue)

    for line in lines[func_start + 1 :]:
        new_line = line
        for name in tag_reads:
            local = f"_t_{_safe_name(name)}"
            new_line = new_line.replace(f'tags["{name}"]', local)
        for name in memory_reads:
            local = f"_m_{_safe_name(name)}"
            new_line = new_line.replace(f'memory["{name}"]', local)
            new_line = new_line.replace(f'memory.get("{name}")', local)
        for name in prev_reads:
            local = f"_p_{_safe_name(name)}"
            new_line = new_line.replace(f'prev["{name}"]', local)
        new_lines.append(new_line)

    # Add epilogue before capture_prev
    # Find the capture_prev call
    result_lines = []
    for line in new_lines:
        if "def capture_prev" in line:
            for e in epilogue:
                result_lines.append(e)
            result_lines.append("")
        result_lines.append(line)

    return "\n".join(result_lines)


def main():
    prog = _make_busy_program()
    compiled = compile_kernel(prog)

    print("Generated kernel source (first 20 lines):")
    for i, line in enumerate(compiled.source.splitlines()[:20], 1):
        print(f"  {i:3d}: {line}")

    print(f"\n  Total lines: {len(compiled.source.splitlines())}")
    print(f"  Tag dict accesses: {compiled.source.count('tags[')}")
    print(f"  Memory dict accesses: {compiled.source.count('memory[')}")
    print(f"  Prev dict accesses: {compiled.source.count('prev[')}")

    # Benchmark original step_fn in isolation
    kernel = compiled.create_kernel()
    kernel.tags.update({f"E{i}": True for i in range(20)})
    kernel.memory["_dt"] = 0.010

    WARMUP = 100
    ITERS = 5000

    for _ in range(WARMUP):
        compiled.step_fn(kernel.tags, kernel.blocks, kernel.memory, kernel.prev, 0.010)

    t0 = time.perf_counter()
    for _ in range(ITERS):
        compiled.step_fn(kernel.tags, kernel.blocks, kernel.memory, kernel.prev, 0.010)
    elapsed_orig = time.perf_counter() - t0

    print(f"\n  Original step_fn:  {elapsed_orig/ITERS*1e6:.1f} us/call  ({ITERS} calls)")

    # Try locals rewrite
    locals_source = _rewrite_to_locals(compiled.source)

    ns: dict = {}
    try:
        exec(compile(locals_source, "<locals-kernel>", "exec"), ns)  # noqa: S102
        locals_step = ns["_kernel_step"]
    except Exception as e:
        print(f"\n  Locals rewrite failed to compile: {e}")
        print("  (This is expected — the rewrite is a rough prototype)")
        return

    kernel2 = compiled.create_kernel()
    kernel2.tags.update({f"E{i}": True for i in range(20)})
    kernel2.memory["_dt"] = 0.010

    for _ in range(WARMUP):
        locals_step(kernel2.tags, kernel2.blocks, kernel2.memory, kernel2.prev, 0.010)

    t0 = time.perf_counter()
    for _ in range(ITERS):
        locals_step(kernel2.tags, kernel2.blocks, kernel2.memory, kernel2.prev, 0.010)
    elapsed_locals = time.perf_counter() - t0

    print(f"  Locals step_fn:    {elapsed_locals/ITERS*1e6:.1f} us/call  ({ITERS} calls)")
    print(f"  Speedup:           {elapsed_orig/elapsed_locals:.2f}x")


if __name__ == "__main__":
    main()
