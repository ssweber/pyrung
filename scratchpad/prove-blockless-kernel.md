# Blockless Prove Kernel

## Summary
- Implement `blockless` as a native kernel codegen mode.
- Use `blockless=True` by default throughout the prove stack.
- Do not add any CLI flag or user-facing toggle for this phase.
- Remove dead prove-only compatibility code once the migration is complete; we do not need to preserve unused internals for external consumers.

## Interface Changes
- Add `blockless: bool = False` to `compile_kernel()` and `CodegenContext.for_kernel()`.
- Add `CompiledKernel.blockless: bool`.
- Keep the compiled step signature unchanged: `step_fn(tags, blocks, memory, prev, dt)`.
- Prove-owned compile sites explicitly opt into `blockless=True`:
  - `prove()`
  - `reachable_states()`
  - `program_hash()`
  - pre-BFS kernel compilation in `passes.py`
  - prove elision’s forced-kernel / concrete-kernel compile paths
- Leave non-prove internal callers on the existing default unless they are updated as part of the same refactor.

## Implementation Changes
- Codegen:
  - Add a kernel-only blockless mode on the codegen context.
  - In blockless mode, emit direct `tags` access for block-backed tags instead of block-array access.
  - Static reads: `tags.get("DS43", default)`.
  - Static writes: `tags["DS43"] = value`.
  - Dynamic reads/writes: resolve through emitted name tuples such as `_b_DS_names[_resolve_index_b_DS(...)]`.
  - Rework range/block helpers in blockless mode to iterate over tag-name sequences rather than list-backed block arrays.
  - Preserve rung-entry snapshot semantics for block-backed condition inputs by snapshotting from `tags`, not live block arrays.
  - Do not emit per-step `blocks["..."]` aliases in blockless kernel source.
- Prove execution:
  - Introduce one shared prove-side helper for “execute one compiled scan” that branches on `compiled.blockless`.
  - Use that helper everywhere prove currently does manual block sync:
    - BFS stepping
    - pilot sweep / domain discovery
    - memory-key discovery
    - concrete elision / forced-true coverage
  - Delete `_compile_inline_step` and related prove-only wrapper plumbing if they become unused after the migration.
- Compatibility boundary:
  - Keep `ReplayKernel.blocks` and `BlockSpec` for now, because other internal runtime/replay paths still use block arrays.
  - Do not keep any dead prove-only sync code solely for hypothetical third-party callers.

## Test Plan
- Codegen/source tests:
  - Assert `compile_kernel(..., blockless=True)` emits direct `tags` access and no step-local `blocks[...]` alias lines.
  - Cover static block access, indirect single-cell access, and indirect/range-based block operations.
- Execution parity:
  - Compare legacy vs blockless compiled kernels on representative programs using:
    - static block tags
    - indirect reads/writes
    - `fill`, `blockcopy`, `search`, `shift`
    - edge conditions
    - oneshot memory behavior
- Prove regression:
  - Assert prove-built contexts use blockless kernels by default.
  - Verify reachable-state sets and proof outcomes stay identical.
  - Cover auxiliary prove paths so pilot sweep and elision also avoid the old sync path.
- Cleanup validation:
  - Ensure no remaining prove callers reference `_compile_inline_step` or the old wrapper path after migration.

## Assumptions
- `blockless=False` remains the compiler default for now; only prove defaults to blockless.
- Dead prove-only internals should be removed immediately once unused.
- Runtime/compiled replay paths are out of scope unless the blockless refactor naturally simplifies them without changing behavior.
