# Blockless kernel for prove BFS

## Problem

Block sync (`_compile_inline_step`) copies 7,664 tags ↔ block arrays every BFS step.
The kernel only accesses ~13,600 block positions per step but via fast list indexing.
Sync cost: ~450s/682s (66%). TagBackedArray proxy made it worse (Python __getitem__ dispatch).

## Fix

Post-process the compiled kernel source to replace block array access with direct
tags dict access. No new codegen mode — transform the source string before exec().

### Static access (309 positions detected)

```python
# Before
_b_DS[42] = value        # write
x = _b_DS[42]            # read

# After
tags["DS43"] = value
x = tags.get("DS43", 0)
```

### Dynamic access (DS, C, DH, DF, Cmd_MLk, Sts_MLk)

```python
# Before
_b_DS[_resolve_index_b_DS(int(expr))]

# After — inject a name-lookup tuple per block
tags[_b_DS_names[_resolve_index_b_DS(int(expr))]]
```

Where `_b_DS_names = ("DS1", "DS2", ..., "DS4500")` is injected into the exec namespace.

### Block variable elimination

Remove `_b_XX = blocks["_b_XX"]` assignments. Remove `blocks` parameter if unused.

## Implementation

In `prove/kernel.py`, add `_compile_blockless_step(compiled, block_specs)`:

1. Parse `compiled.source` to find block var aliases (`_b_XX = blocks["_b_XX"]`)
2. Build tag-name tuples per block from block_specs
3. For each block:
   a. Replace static writes `_b_XX[N] = expr` → `tags["tag_name"] = expr`
   b. Replace static reads `_b_XX[N]` → `tags.get("tag_name", default)`
   c. Replace dynamic access `_b_XX[expr]` → `tags.get(_b_XX_names[expr], default)` (reads)
      and `_b_XX_names[expr]` for writes (need to detect write vs read context)
   d. Remove the `_b_XX = blocks[...]` line
4. Inject `_b_XX_names` tuples into exec namespace for blocks with dynamic access
5. Compile and exec the modified source
6. Return a thin wrapper: `memory["_dt"] = dt; modified_step(tags, blocks, memory, prev, dt)`

Replace `_compile_inline_step` call in `passes.py:103` with `_compile_blockless_step`.

## Write vs read detection for dynamic access

Write: `_b_XX[expr] = ...` — the block access is on the LHS of assignment
Read: everything else (`_b_XX[expr]` in expressions, function args, RHS)

Regex approach:
- Write pattern: `^(\s*)_b_XX\[([^\]]+)\]\s*=\s*(.+)$` → `\1tags[_b_XX_names[\2]] = \3`
- Read pattern (after writes handled): `_b_XX\[([^\]]+)\]` → `tags.get(_b_XX_names[\1], default)`

Process writes first (full-line match), then reads (inline replacement).

## Expected impact

- Eliminates 450s block sync entirely
- Adds ~2ms/step for dict.get/set vs list indexing (~13,600 accesses × 150ns)
- Net: ~145k steps × 2ms = ~290s overhead, vs ~450s saved → ~160s net savings
- Actually overhead may be less since dict.get replaces both list[int] AND the sync dict.get

## Risks

- Source transformation is fragile — regex must handle all codegen patterns
- Multi-line expressions with block access (unlikely from codegen but check)
- Nested block access like `_b_DS[_b_DS[i]]` (unlikely but check)
- `_store_copy_value_to_type` helper accesses blocks — need to check if it's a compiled
  helper or called with the block var as argument
