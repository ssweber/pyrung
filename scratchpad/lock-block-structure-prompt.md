# Thread `lock` (and `band`) through Block slots and structured tags

## Context

`lock=True` on a Tag opts it into `reachable_states()` projection (the lock file). Currently it only works on standalone tags (`Int("X", lock=True)`). Block slots and structured tag fields (`@udt`, `@named_array`) can't be marked `lock=True`, which means you can't lock-project e.g. a named_array of alarm bits or a UDT's status field without a separate standalone tag.

`band=` has the same gap — standalone tags only. Once `lock` flows through blocks/structures, `band` should ride the same change.

## Reference commit

`ba875285` added `lock` to standalone tags and Click codegen/nickname CSV. It shows exactly which files need updating for a new boolean flag on Tag. Use it as the pattern, but note that `band` is a `dict[str, int|float|str]` not a `bool`, so nickname CSV serialization is more complex.

## What needs to happen

### 1. Block slots (`memory_block.py`)

- Add `lock` (and `band`) to `_SlotHints` NamedTuple
- Add `_slot_lock_overrides` / `_slot_band_overrides` dicts on `Block`
- Add `lock` / `band` properties + `lock_overridden` / `band_overridden` on `SlotView`
- Wire through `_effective_slot_hints()`, `_apply_slot_hint()`, `_make_tag()`
- Add to `SlotView.clear_overrides()`
- Add `_pyrung_field_lock` / `_pyrung_field_band` block-level defaults

### 2. Structured tags (`structure.py` + `structure.pyi`)

- `@udt()` field definitions need to accept and propagate `lock` and `band`
- `@named_array()` same — both singleton and counted modes
- The `_build_tags` / `_make_field_tag` internals need to pass through

### 3. Click codegen (`click/codegen/`)

- `models.py` — `_TagMetadata` and `_BlockSlotDecl` get `lock`/`band` fields
- `collector.py` — `_enrich_with_ownership` reads and propagates them
- `emitter.py` — `_has_metadata`, `_append_metadata_kwargs`, `_emit_plain_block_decl` emit them

### 4. Nickname CSV (`click/tag_map/`)

- `_parsers.py` — `TagMeta` gets `lock`/`band`, `_BOOL_FLAG_TOKENS` includes `lock`, add `band` to `_VALUE_TOKENS` or a new dict-style token parser
- `_parsers.py` — `parse_tag_meta` / `format_tag_meta` round-trip `lock` and `band`
- `_nickname_io.py` — `_tag_meta_from_hints` and all tag reconstruction paths propagate both
- For `band`: decide on a CSV comment syntax, e.g. `[lock, band=ZERO:0|POSITIVE:>0]`

### 5. DAP console (`dap/console.py`)

- If the console displays tag metadata, include `lock` and `band`

### 6. Tests

- `tests/click/test_tag_map.py` — nickname CSV round-trip tests for `lock` and `band` on block slots
- `tests/core/test_memory_block.py` — SlotView property tests
- `tests/core/test_structure.py` — UDT/named_array field propagation
- `tests/core/analysis/test_prove.py` — reachable_states with lock/band on block slots

## Design decision: `band` in nickname CSV

`band` is a dict with predicate strings (`{"ZERO": 0, "POSITIVE": ">0"}`). Options for CSV comment serialization:

1. `[band=ZERO:0|POSITIVE:>0]` — pipe-delimited key:value pairs
2. `[band=ZERO:0,POSITIVE:>0]` — comma-delimited (conflicts with existing comma parsing?)
3. Don't serialize `band` in CSV — keep it code-only, not round-trippable

Recommendation: option 1 or 3. The predicate syntax is rich enough that CSV serialization may not be worth the parser complexity. Code-only is fine if bands are always declared in Python source.
