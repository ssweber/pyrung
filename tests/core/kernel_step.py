"""Run one scan on a compiled kernel, for tests that compare it with the other engines."""

from __future__ import annotations

from pyrung.core.kernel import CompiledKernel, ReplayKernel


def step_compiled_kernel(compiled: CompiledKernel, kernel: ReplayKernel, *, dt: float) -> None:
    """Run one scan of *compiled* on *kernel*.

    A kernel compiled with blocks reads them from the tags first and writes
    them back after.
    """
    tags = kernel.tags
    if not compiled.blockless:
        for spec in compiled.block_specs.values():
            kernel.load_block_from_tags(spec)
    compiled.step_fn(tags, kernel.blocks, kernel.memory, kernel.prev, dt)
    if not compiled.blockless:
        for spec in compiled.block_specs.values():
            kernel.flush_block_to_tags(spec)
    for name in compiled.edge_tags:
        if name in tags:
            kernel.prev[name] = tags[name]
    kernel.advance(dt)
