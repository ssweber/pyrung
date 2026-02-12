# System Points Core + Click Auto-Mapping Plan

## Summary

Implement a vendor-agnostic system-points layer in core, exposed through grouped namespaces (`system.sys`, `system.rtc`, `system.fault`, `system.firmware`), and automatically map those points to Click `SC/SD` addresses in `TagMap`.

This plan is implementation-ready and locks all open decisions from the design discussion:

- API shape: grouped namespaces under `system`.
- RTC model: absolute staged apply, wall-clock-backed (`datetime.now()`) with offset.
- Read-only policy: writes to read-only system points raise `ValueError`.
- Scope: lean core only (no comm counters, no analog, no WLAN/EIP/module telemetry in v1).

---

## Locked Decisions

1. Core naming is canonical and vendor-agnostic.
2. Click naming remains address + official system nickname.
3. `TagMap` auto-injects system mappings by default.
4. Read-only system points reject writes from user logic and external writes.
5. RTC `apply_date/apply_time` means apply staged absolute values, not deltas.
6. Default RTC source is Python wall clock; tests can use `freezegun`.

---

## Scope Matrix (v1)

### Include in core v1

- Scan lifecycle + clocks:
  - `SC1-SC9`, `SC202`, `SD9-SD14`
- Runtime mode/control:
  - `SC10`, `SC11`, `SC50`, `SC51`
- Fault diagnostics:
  - `SC19`, `SC40`, `SC43`, `SC44`, `SC46`, `SD1`
- RTC + apply handshake:
  - `SC53-SC56`, `SD19-SD26`, `SD29/31/32/34/35/36`
- Firmware identity:
  - `SD5-SD8`

### Defer from core v1

- Comm heartbeat counters (`SD40/41/42/50/51/60/61/140-147/214/215`)
- Analog CPU channels (`SD71-SD76`)
- WLAN/Port/IP/MAC status (`SC80-103`, `SD80-91`, `SD188-218`)
- EtherNet/IP detail (`SC111-116`, `SD101-114`)
- SD/Bluetooth (`SC60-70`, `SD62-70`)
- Security/allow list (`SC131-136`, `SD131-135`)
- Intelligent module slot telemetry/control (`SC301-335`, `SD301-453`)
- PTO flags (`SC150-152`)

---

## Public API (Core)

## Module additions

- `src/pyrung/core/system_points.py`

## Exports

- Add `system` export in `src/pyrung/core/__init__.py`
- Do **not** export bare `sys`, `rtc`, `fault`, `firmware` names at module top-level.

## Access pattern

```python
from pyrung.core import system

with Rung(system.sys.first_scan):
    copy(1, SomeFlag)

with Rung(system.fault.division_error):
    out(system_alarm)
```

## Namespace objects

Use immutable namespace dataclasses (or frozen simple classes) with Tag fields:

- `SystemNamespaces`
  - `.sys: SysNamespace`
  - `.rtc: RtcNamespace`
  - `.fault: FaultNamespace`
  - `.firmware: FirmwareNamespace`

Each field is a standard `Tag` instance with canonical name, type, default, retentive policy.

---

## Canonical Names and Click Mapping

`core_name -> click_address (click_nickname)`

### Sys namespace

- `sys.always_on` -> `SC1` (`_Always_ON`)
- `sys.first_scan` -> `SC2` (`_1st_SCAN`)
- `sys.scan_clock_toggle` -> `SC3` (`_SCAN_Clock`)
- `sys.clock_10ms` -> `SC4` (`_10ms_Clock`)
- `sys.clock_100ms` -> `SC5` (`_100ms_Clock`)
- `sys.clock_500ms` -> `SC6` (`_500ms_Clock`)
- `sys.clock_1s` -> `SC7` (`_1sec_Clock`)
- `sys.clock_1m` -> `SC8` (`_1min_Clock`)
- `sys.clock_1h` -> `SC9` (`_1hour_Clock`)
- `sys.mode_switch_run` -> `SC10` (`_Mode_Switch`)
- `sys.mode_run` -> `SC11` (`_PLC_Mode`)
- `sys.cmd_mode_stop` -> `SC50` (`_PLC_Mode_Change_to_STOP`)
- `sys.cmd_watchdog_reset` -> `SC51` (`_Watchdog_Timer_Reset`)
- `sys.fixed_scan_mode` -> `SC202` (`_Fixed_Scan_Mode`)
- `sys.scan_counter` -> `SD9` (`_Scan_Counter`)
- `sys.scan_time_current_ms` -> `SD10` (`_Current_Scan_Time`)
- `sys.scan_time_min_ms` -> `SD11` (`_Minimum_Scan_Time`)
- `sys.scan_time_max_ms` -> `SD12` (`_Maximum_Scan_Time`)
- `sys.scan_time_fixed_setup_ms` -> `SD13` (`_Fixed_Scan_Time_Setup`)
- `sys.interrupt_scan_time_ms` -> `SD14` (`_Interrupt_Scan_Time`)

### Fault namespace

- `fault.plc_error` -> `SC19` (`_PLC_Error`)
- `fault.division_error` -> `SC40` (`_Division_Error`)
- `fault.out_of_range` -> `SC43` (`_Out_of_Range`)
- `fault.address_error` -> `SC44` (`_Address_Error`)
- `fault.math_operation_error` -> `SC46` (`_Math_Operation_Error`)
- `fault.code` -> `SD1` (`_PLC_Error_Code`)

### RTC namespace

- `rtc.year4` -> `SD19` (`_RTC_Year (4 digits)`)
- `rtc.year2` -> `SD20` (`_RTC_Year (2 digits)`)
- `rtc.month` -> `SD21` (`_RTC_Month`)
- `rtc.day` -> `SD22` (`_RTC_Day`)
- `rtc.weekday` -> `SD23` (`_RTC_Day_of_the_Week`)
- `rtc.hour` -> `SD24` (`_RTC_Hour`)
- `rtc.minute` -> `SD25` (`_RTC_Minute`)
- `rtc.second` -> `SD26` (`_RTC_Second`)
- `rtc.new_year4` -> `SD29` (`_RTC_New_Year(4 digits)`)
- `rtc.new_month` -> `SD31` (`_RTC_New_Month`)
- `rtc.new_day` -> `SD32` (`_RTC_New_Day`)
- `rtc.new_hour` -> `SD34` (`_RTC_New_Hour`)
- `rtc.new_minute` -> `SD35` (`_RTC_New_Minute`)
- `rtc.new_second` -> `SD36` (`_RTC_New_Second`)
- `rtc.apply_date` -> `SC53` (`_RTC_Date_Change`)
- `rtc.apply_date_error` -> `SC54` (`_RTC_Date_Change_Error`)
- `rtc.apply_time` -> `SC55` (`_RTC_Time_Change`)
- `rtc.apply_time_error` -> `SC56` (`_RTC_Time_Change_Error`)

### Firmware namespace

- `firmware.main_ver_low` -> `SD5` (`_Firmware_Version_L`)
- `firmware.main_ver_high` -> `SD6` (`_Firmware_Version_H`)
- `firmware.sub_ver_low` -> `SD7` (`_Sub_Firmware_Version_L`)
- `firmware.sub_ver_high` -> `SD8` (`_Sub_Firmware_Version_H`)

---

## Runtime Semantics

## Wall clock + offset model (locked)

Keep a memory key:

- `_sys.rtc.offset: timedelta` (default `timedelta()`)

Derived RTC now:

- `rtc_now = datetime.now() + offset`

Rationale:

- Minimal complexity.
- `timedelta` avoids float drift from repeated `total_seconds()` round-trips.
- Compatible with external `freezegun` test control.
- No changes to timer math model.

## RTC staged apply behavior (absolute, not delta)

On scan, treat `rtc.apply_date` and `rtc.apply_time` as command bits:

- If `rtc.apply_date` transitions to true:
  - Read staged `new_year4/new_month/new_day`.
  - Validate date.
  - If valid: compute new absolute target datetime preserving current time components.
  - If invalid: set `rtc.apply_date_error = True`.
- If `rtc.apply_time` transitions to true:
  - Read staged `new_hour/new_minute/new_second`.
  - Validate time.
  - If valid: compute new absolute target datetime preserving current date components.
  - If invalid: set `rtc.apply_time_error = True`.

Offset update rule for valid apply:

- `new_offset = target_datetime - datetime.now()`

Apply command bits auto-clear behavior:

- `rtc.apply_date` and `rtc.apply_time` clear to `False` after processing.

Error bits behavior:

- `rtc.apply_date_error` and `rtc.apply_time_error` are scan-visible status bits and clear at scan start.

## Lifecycle/fault/mode

- `sys.always_on`: always `True`.
- `sys.first_scan`: `True` only when `scan_id == 0` for current evaluation cycle.
- `sys.scan_clock_toggle`: `(scan_counter % 2) == 1`. Toggles every scan.
- Time clocks (`10ms/100ms/500ms/1s/1m/1h`): wall-clock-based 50/50 square waves,
  derived from simulation timestamp. Each clock's named period is the **full cycle**;
  the half-period (ON duration = OFF duration) is half that value:

  | Clock | Half-period | Full cycle |
  |-------|-------------|------------|
  | `clock_10ms` | 5 ms | 10 ms |
  | `clock_100ms` | 50 ms | 100 ms |
  | `clock_500ms` | 250 ms | 500 ms |
  | `clock_1s` | 500 ms | 1 s |
  | `clock_1m` | 30 s | 60 s |
  | `clock_1h` | 30 min | 60 min |

  Derivation formula (zero storage, deterministic in `FIXED_STEP` mode):

  ```python
  phase = int(timestamp / half_period_seconds)
  value = (phase % 2) == 1
  ```

  All clocks start OFF at `timestamp == 0.0`. This naturally aliases when
  scan time exceeds the clock period, matching real hardware behavior.

## Fault / error clear timing

Three distinct clear policies, ordered by severity:

1. **Scan-start auto-clear** (transient instruction-level flags):
   - `fault.division_error` (SC40), `fault.out_of_range` (SC43), `fault.address_error` (SC44)
   - `rtc.apply_date_error` (SC54), `rtc.apply_time_error` (SC56)
   - Cleared in `on_scan_start`, **before** patch application or logic evaluation.
   - Set by instructions during logic evaluation; visible for the remainder of that scan only.

2. **Latched / fatal** (requires PLC restart to clear):
   - `fault.math_operation_error` (SC46)
   - When set, also sets `sys.cmd_mode_stop` (SC50), transitioning the PLC to Stop mode.
   - Not auto-cleared. v1 policy: keep latched once set; runner stops scanning.

3. **Reflects active system state** (not instruction-level):
   - `fault.plc_error` (SC19): ON while any system-level error condition is active.
   - `fault.code` (SD1): holds error code while error persists, 0 otherwise.
   - These track hardware/config faults (I/O bus, watchdog, etc.), not math/copy errors.
   - v1: default to OFF/0 since we don't simulate hardware fault injection.

## `on_scan_start` clear order

```
1. Clear transient fault flags (SC40, SC43, SC44)
2. Clear RTC error status bits (SC54, SC56)
3. Process RTC apply commands (SC53, SC55) -- may re-set error bits
4. Auto-clear RTC apply command bits (SC53, SC55)
5. Clear/process mode command bits (SC50, SC51)
```

## Mode / command bits

- `sys.mode_run` and `sys.mode_switch_run`:
  - derived from runner mode state and stop command handling.
- `sys.cmd_mode_stop`:
  - command bit; when true, force run-mode false and auto-clear command bit.
- `sys.cmd_watchdog_reset`:
  - command bit; v1 no-op action except auto-clear.

---

## Memory Pressure Policy (Store vs Derive)

## Derived on read (not persisted in `state.tags`)

- `sys.always_on`
- `sys.first_scan`
- `sys.scan_clock_toggle`
- `sys.clock_10ms`, `sys.clock_100ms`, `sys.clock_500ms`, `sys.clock_1s`, `sys.clock_1m`, `sys.clock_1h`
- `sys.mode_switch_run`, `sys.mode_run`, `sys.fixed_scan_mode`
- `sys.scan_time_current_ms`
- `sys.scan_time_fixed_setup_ms`
- `sys.interrupt_scan_time_ms` (v1 returns 0)
- `rtc.year4/year2/month/day/weekday/hour/minute/second`
- `firmware.*`

## Persisted state (memory + selected tags)

- `sys.scan_counter`
- `sys.scan_time_min_ms`, `sys.scan_time_max_ms`
- `fault.*` bits and `fault.code`
- RTC staging fields: `rtc.new_*`
- RTC apply command/status bits: `rtc.apply_*`, `rtc.apply_*_error`
- mode command bits: `sys.cmd_mode_stop`, `sys.cmd_watchdog_reset`
- `_sys.rtc.offset` (timedelta)

---

## Engine Integration Design

## New internal runtime component

Add `SystemPointRuntime` (in `system_points.py`) with responsibilities:

- Provide all namespace tags and metadata.
- Provide derived value resolver:
  - `resolve(name: str, ctx_or_state) -> tuple[bool, value]`
- Provide read-only metadata:
  - `is_read_only(name: str) -> bool`
- Handle scan lifecycle hooks:
  - `on_scan_start(ctx)`
  - `on_scan_end(ctx)`

## ScanContext changes

Add read-only guard support:

- `ScanContext.__init__(..., read_only_tags: frozenset[str] = frozenset())`
- `set_tag(name, value)`:
  - raise `ValueError` if `name in read_only_tags`
- add private bypass for runtime/system updates (not importable from user code):
  - `_set_tag_internal(name, value)`
  - `_set_tags_internal(updates)`

Add derived resolver support:

- `ScanContext.__init__(..., resolver: TagResolver | None = None)`
- `get_tag(name, default)` fallback order:
  1. pending writes
  2. state.tags
  3. resolver
  4. default

## PLCRunner changes

- Build a `SystemPointRuntime` instance.
- Pass runtime resolver + read-only tag set into `ScanContext`.
- In `step()`:
  1. `runtime.on_scan_start(ctx)`
  2. apply patch
  3. inject `_dt`
  4. evaluate logic
  5. edge snapshot `_prev:*`
  6. `runtime.on_scan_end(ctx)`
  7. commit

## Patch guard

- `PLCRunner.patch()` rejects keys that are read-only system points (`ValueError`).

---

## Write Protection Rules (Locked)

## Writable system points

Only these are writable by user logic:

- `rtc.new_year4`, `rtc.new_month`, `rtc.new_day`
- `rtc.new_hour`, `rtc.new_minute`, `rtc.new_second`
- `rtc.apply_date`, `rtc.apply_time`
- `sys.cmd_mode_stop`, `sys.cmd_watchdog_reset`

Everything else in `system.*` is read-only.

## Enforcement

- Logic writes (`out/latch/reset/copy/math/fill/blockcopy/...`) fail on protected target names at execution via `ScanContext.set_tag` guard.
- `runner.patch` writes fail on protected names.
- Click provider writes fail on protected mapped addresses.

Error message format:

- `ValueError("Tag '<name>' is read-only system point and cannot be written")`

---

## Click Dialect Integration

## TagMap (`src/pyrung/click/tag_map.py`)

Add constructor option:

- `TagMap(..., include_system_points: bool = True)`

Behavior:

- When `True`, merge system mappings into map entries.
- User-provided mappings to same hardware addresses are allowed if they point to the
  same logical name (e.g. nickname file aliasing `SC2` -> `_1st_SCAN`). Only error
  if a user mapping maps the same address to a **different** logical tag name.
- User-provided mappings for same logical system names are rejected (system names reserved).

Augment mapped slot metadata:

- Extend `MappedSlot` with:
  - `read_only: bool`
  - `source: Literal["user", "system"]`

## ClickDataProvider (`src/pyrung/click/data_provider.py`)

Read path:

- For mapped system slots, resolve via `SystemPointRuntime` first.
- Fallback to runner state only for persisted writable system points.

Write path:

- If mapped slot is read-only system slot: raise `ValueError`.
- If writable system slot: route via `runner.patch` (which still guards read-only).

---

## Tests to Add

## Core tests

Add `tests/core/test_system_points.py`:

1. Namespace shape and tag names:
   - `system.sys.first_scan` etc resolve to canonical tag names.
2. Derived points:
   - `always_on`, `first_scan`, scan clock, fixed scan mode.
3. Scan stats:
   - `scan_counter` increments, min/max update correctly.
4. RTC wall-clock derived fields:
   - values match frozen wall clock under `freezegun`.
5. RTC apply_date valid path:
   - offset changes, current fields match staged date.
6. RTC apply_time valid path:
   - offset changes, current fields match staged time.
7. RTC invalid staging:
   - apply error flags set.
8. Read-only write rejection:
   - logic write and patch write both raise.

## Click tests

Add `tests/click/test_system_points_mapping.py`:

1. `TagMap(include_system_points=True)` contains expected SC/SD mappings.
2. `include_system_points=False` excludes them.
3. Mapped slot metadata flags (`read_only`, `source`).
4. Provider read returns derived system values.
5. Provider write to read-only system address raises.
6. Provider write to writable system command/staging address succeeds.

---

## Implementation Order (TDD)

Write tests alongside implementation. Each step includes its tests before moving on.

1. Add `system_points.py` with namespaces + canonical mapping table.
   - Test: namespace shape, tag names, canonical name resolution.
2. Add runtime resolver + `SystemPointRuntime`.
   - Test: derived points (`always_on`, `first_scan`, scan clock, timed clocks).
3. Export `system` from `core.__init__`.
4. Extend `ScanContext` (resolver + read-only guard + `_set_tag_internal`/`_set_tags_internal`).
   - Test: read-only write rejection (logic write and patch write both raise).
5. Wire `PLCRunner` lifecycle hooks (`on_scan_start`/`on_scan_end`).
   - Test: scan stats (`scan_counter` increments, min/max), fault flag clear timing.
   - Test: RTC wall-clock derived fields under `freezegun`.
   - Test: RTC apply valid/invalid paths, error flag lifecycle.
6. Extend `TagMap` with auto system mapping and slot metadata.
   - Test: `include_system_points=True/False`, mapped slot metadata, same-address tolerance.
7. Extend `ClickDataProvider` read/write behavior for system points.
   - Test: provider read returns derived values, write to read-only raises, write to writable succeeds.
8. Run `make lint` and `make test` (full suite).

---

## Acceptance Criteria

1. `system.sys`, `system.rtc`, `system.fault`, `system.firmware` are available and usable in programs.
2. Lean system points are available without manual mapping.
3. Click `TagMap` auto-maps included system points by default.
4. Read-only writes are rejected consistently across logic, patching, and provider writes.
5. RTC apply uses staged absolute values and wall-clock + offset model.
6. All new and existing tests pass.

---

## Non-Goals (v1)

1. No comm counter modeling.
2. No analog channel modeling.
3. No WLAN/EIP/module telemetry modeling.
4. No change to instruction semantics outside read-only enforcement.
5. No backfilling full SC/SD parity.
