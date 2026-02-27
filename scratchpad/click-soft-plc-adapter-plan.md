# Soft PLC Adapter v1 (`ClickDataProvider`) for `pyrung.click`

## Summary

`TagMap`, map-time type compatibility, and nickname round-trip are already in place. Remaining work is a runtime bridge: map hardware addresses to logical tags, read from immutable runner state, and queue writes for the next scan.

This work is split into two PRs to keep review surface small:
1. **Prep PR (done):** broaden core tag-value typing to include `str`.
2. **Adapter PR (next):** add `ClickDataProvider` + tests.

## Prep PR (done)

Broaden core signatures from `bool | int | float` to `bool | int | float | str`:
- `src/pyrung/core/runner.py` (`_pending_patches`, `patch(...)`)
- `src/pyrung/core/state.py` (`with_tags(...)`)

Status:
- Completed in `src/pyrung/core/runner.py`.
- Completed in `src/pyrung/core/state.py`.
- Added test coverage for string tag writes in:
  - `tests/core/test_system_state.py`
  - `tests/core/test_plc_runner.py`

## Public API / Interface Changes

1. Add `ClickDataProvider` in `src/pyrung/click/data_provider.py`.
2. Export `ClickDataProvider` from `src/pyrung/click/__init__.py`.
3. Add a small public TagMap slot-iteration API so adapters do not depend on private entry internals.
   - Provide mapped slot records sufficient for runtime binding:
     - normalized hardware address
     - logical tag name
     - effective default value (override-aware)
     - bank/index (or enough info to derive them)
4. `ClickDataProvider` constructor:
   - `runner: PLCRunner` (required)
   - `tag_map: TagMap` (required)
   - `fallback: DataProvider | None = None` (optional; defaults to `pyclickplc.server.MemoryDataProvider`)
5. Implements `pyclickplc.server.DataProvider`:
   - `read(address: str) -> PlcValue`
   - `write(address: str, value: PlcValue) -> None`

## Detailed Behavior Spec

### 1) Initialization

- Build an internal reverse index in `ClickDataProvider` from the new public TagMap slot-iteration API:
  - hardware address string -> mapped slot record.
- Include both standalone tags and block slots.
- Address normalization for adapter lookup uses `pyclickplc.parse_address` + `format_address_display` (same normalization pattern as `MemoryDataProvider`).
- Default resolution order for each mapped slot:
  1. override default when set
  2. otherwise logical slot default (`Tag.default`, already type-zero unless explicitly set)

### 2) `read(address)`

- Normalize address.
- If mapped: return `runner.current_state.tags.get(logical_name, effective_default)`.
- If unmapped: delegate to fallback provider.

### 3) `write(address, value)`

- Normalize address.
- If mapped:
  - validate runtime value with the same pyclickplc runtime validation used by memory provider (`bank/index` aware).
  - queue via `runner.patch({logical_name: value})`.
  - Deferred semantics: no immediate state mutation.
- If unmapped: delegate to fallback provider.

### 4) Concurrency model (v1)

- `DataProvider` methods are synchronous.
- v1 assumes single thread/task access between server requests and runner stepping.
- No cross-thread locking in v1.

### 5) XD/YD handling (v1)

- No derivation from X/Y.
- Serve through fallback provider only.

### 6) Error policy

- Do not raise for valid-but-unmapped addresses; fallback handles them.
- Let fallback/provider validation raise for invalid runtime values where appropriate.

## Implementation Steps

1. **Prep PR:** broaden core value typing to include `str` (done):
   - `src/pyrung/core/runner.py`
   - `src/pyrung/core/state.py`
2. Add public TagMap slot-iteration API for hardware-facing consumers.
3. Create `src/pyrung/click/data_provider.py` with:
   - internal mapped-slot record
   - reverse-index builder from TagMap public slot iteration
   - `read`/`write` methods
4. Export from `src/pyrung/click/__init__.py`.
5. Add tests in `tests/click/test_data_provider.py`.

## Tests and Scenarios

1. `read` mapped standalone tag returns runner state value.
2. `read` mapped slot returns logical default when tag absent in state.
3. `write` mapped tag is deferred:
   - before `step()`: state unchanged
   - after `step()`: patched value visible.
4. Multiple writes before scan: last write wins.
5. Block-slot mapping reads/writes resolve to correct logical slot names.
6. Unmapped reads/writes go through fallback (`MemoryDataProvider`) and round-trip.
7. XD/YD addresses are served by fallback (default/read-write compatibility).
8. TXT/CHAR mapped write uses string value and appears after next scan.
9. Address normalization works (`c1`, `C1`, `x1`, `X001` forms).
10. Default precedence for mapped read fallback:
   - override default is used when set
   - otherwise slot `Tag.default` is used
11. Mapped runtime value validation matches fallback behavior (invalid writes raise consistently).

## Assumptions and Defaults Chosen

1. Write visibility: deferred to next scan (no immediate readback overlay).
2. Address scope: mapped + shadow fallback (fallback provider for unmapped valid addresses).
3. Concurrency: synchronous provider with single-thread/task assumption in v1.
4. XD/YD: fallback-only in v1.
5. Reverse map ownership: runtime reverse index lives in adapter; mapping semantics live in `TagMap` public APIs.
6. Address normalization helper is not added to `TagMap` in v1; adapter normalizes directly with pyclickplc address helpers.
7. Click validation rules remain out of scope for this adapter iteration.
