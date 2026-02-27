## Click XD/YD Join Semantics (X/Y Bit-Image Mirroring)

### Summary
Implement runtime joining between `X/Y` bit banks and `XD/YD` word banks in `ClickDataProvider` so that:
1. `XD*` reads are computed from current `X*` bit state.
2. `YD*` reads are computed from current `Y*` bit state.
3. `YD*` writes fan out to underlying `Y*` bits (applied via `runner.patch`, so rung logic can overwrite on next scan).
4. `XD*` writes are rejected as read-only input-register behavior.

Reference source used for slot model: `https://cdn.automationdirect.com/static/manuals/c0userm/ch2.pdf` (Chapter 2 memory addresses; `XD0` slot0, `XD0u` slot1, `XD1..XD8` expansion).  
Confirmed decision from you: each slot is 16 bits.

### Implementation Scope
1. Update `src/pyrung/click/data_provider.py`.
2. Replace/expand XD/YD behavior tests in `tests/click/test_data_provider.py`.
3. Update Click dialect spec note in `spec/dialects/click.md` for the new runtime contract (remove ambiguity around XD/YD behavior).

### Detailed Design

#### 1. Slot Mapping Model
Use `BANKS["X"].valid_ranges` / `BANKS["Y"].valid_ranges` as canonical 10 slot windows:
- Slot 0: `001..016`
- Slot 1: `021..036`
- Slots 2..9: `101..116`, `201..216`, ..., `801..816`

Map XD/YD MDB indexes to slot index as:
- `0 -> slot 0` (`XD0/YD0`)
- `1 -> slot 1` (`XD0u/YD0u`)
- `2 -> slot 2` (`XD1/YD1`)
- `4 -> slot 3` (`XD2/YD2`)
- ...
- `16 -> slot 9` (`XD8/YD8`)

Bit packing order inside each word:
- bit 0 = first address in slot window (`*01`)
- bit 15 = last address in slot window (`*16`)

#### 2. ClickDataProvider Read Path
In `read(address)`:
1. Normalize address with existing parse/format flow.
2. If bank is `XD` or `YD`, return joined word value from corresponding `X`/`Y` bits.
3. Otherwise keep existing mapped-slot/fallback behavior.

Joined read helper behavior:
- For each of 16 slot bit addresses, resolve bit value by existing mapped logic first, fallback second.
- Compose a 16-bit integer word.

#### 3. ClickDataProvider Write Path
In `write(address, value)`:
1. Normalize address and validate with `assert_runtime_value`.
2. If bank is `YD`:
   - decode the 16 bits,
   - write each to the corresponding `Y` bit address using existing mapped-or-fallback logic,
   - mapped writes use `runner.patch`, fallback writes go to fallback provider.
3. If bank is `XD`:
   - raise `ValueError` (read-only input register semantics).
4. Otherwise keep existing mapped-slot/fallback behavior.

#### 4. Internal Refactor (in same file)
Add private helpers to keep behavior clear and testable:
1. Address-to-slot resolution for XD/YD.
2. Slot-to-16-bit-address expansion for X/Y.
3. Single-address read/write abstraction that applies current mapped/fallback precedence.
4. Word pack/unpack helpers for 16 bools.

### Public API / Interface Changes
1. No new public classes/functions.
2. Behavioral change in `ClickDataProvider`:
   - `XD/YD` are no longer independent fallback-only storage.
   - `XD/YD` become joined views over `X/Y`.
   - `provider.write("XD...")` now raises `ValueError`.
   - `provider.write("YD...")` updates `Y` bits (visible after next `runner.step()` when mapped).

### Test Plan

#### Update existing tests
1. Replace `test_xd_and_yd_addresses_are_fallback_only_even_if_mapped` with join-focused tests.

#### Add/adjust scenarios in `tests/click/test_data_provider.py`
1. `XD0` reflects `X001..X016` bit image.
2. `XD0u` reflects `X021..X036` bit image.
3. `XD1` reflects `X101..X116` bit image.
4. `YD0` reflects `Y001..Y016` bit image.
5. Writing `YD0` sets `Y001..Y016` bits correctly after next scan.
6. Writing `YD0u` sets `Y021..Y036` bits correctly after next scan.
7. Writing `XD0` raises `ValueError`.
8. Rung-overwrite behavior: `YD` write is applied at scan start and can be overridden by rung outputs in same scan.
9. Case/format normalization still works (`yd0`, `YD0`, `yd0u`).
10. Non-X/Y banks (`C`, `DS`, `TXT`) preserve existing behavior unchanged.

### Assumptions and Defaults Chosen
1. `XD/YD` join behavior dominates runtime semantics (explicit logical mappings to `XD/YD` are not used as independent storage).
2. Slot mapping is 10 slots x 16 bits based on sparse `X/Y` ranges and your confirmation.
3. `XD` is treated read-only at provider layer for consistency with input-register semantics.
4. `YD` writes target `Y` bit image only; `YD` reads are always recomputed from `Y` bits.
