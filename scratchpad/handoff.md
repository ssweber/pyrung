# Handoff: blockcopy/fill Instructions Added

## What Just Happened (on `dev`)

Added `blockcopy()` and `fill()` block operation instructions per `spec/core/instructions.md`:

- `BlockCopyInstruction` — copies all values from source `BlockRange` to dest `BlockRange`
- `FillInstruction` — writes a single value to every element in a `BlockRange`
- Both accept `BlockRange` or `IndirectBlockRange` (runtime-resolved bounds)
- Both support `oneshot=` parameter
- DSL functions `blockcopy(source, dest)` and `fill(value, dest)` added to program.py
- `resolve_block_range_ctx()` helper shared by both instructions
- 13 new tests, 349 total pass, lint clean

### API

```python
with Rung(CopyEnable):
    blockcopy(DS.select(1, 10), DD.select(1, 10))

with Rung(ClearEnable):
    fill(0, DS.select(1, 100))

# With tag as fill value
with Rung(InitEnable):
    fill(DefaultVal, DS.select(1, 50))
```

### Design Decision: dest is BlockRange, not dest_start

The spec says `blockcopy(source_block, dest_start)` but Tags don't carry block references, so there's no way to derive sequential destination tags from a single Tag. Instead, both source and dest are `BlockRange`s with validated matching lengths. This is consistent with `fill(value, dest_block)` which already uses BlockRange.

## Files Changed

- `src/pyrung/core/instruction.py` — `BlockCopyInstruction`, `FillInstruction`, `resolve_block_range_ctx()`
- `src/pyrung/core/program.py` — `blockcopy()`, `fill()` DSL functions
- `src/pyrung/core/__init__.py` — updated exports
- `tests/core/test_instruction.py` — 13 new tests

## Suggested Next Steps

1. **Other missing instructions** — `loop()`, `search()`, `shift_register()`, `pack_bits()`/`unpack_to_bits()`, `math()`
2. **Spec alignment audit** — check `dsl.md`, `engine.md`, `instructions.md` for divergences like types.md had
3. **Debug API** (`spec/core/debug.md`) — `force()`, `when()`, `monitor()`, history — larger effort

## Key Design Decisions Made (cumulative)

- Tag name format stays `"DS100"` (no underscore separator)
- Retentive defaults kept as-is in IEC constructors (spec updated to match code, not vice versa)
- `Block.__getitem__` raises `IndexError` (not `ValueError`) for out-of-range
- Click-specific features (nicknames, register, read_only) deferred to future `dialects/click` `TagMap`
- `blockcopy` dest is `BlockRange` (not bare Tag) because Tags don't carry block context
