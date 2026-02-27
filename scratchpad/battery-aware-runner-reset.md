# Runner Reset & Restart Semantics (Runner-Only Scope)

## Summary
Implement explicit mode transitions and power-cycle simulation aligned to real Click PLC behavior:
1. **`runner.stop()`** — Transition to STOP mode. Logic stops executing.
2. **`runner.run()`** — Transition to RUN mode. STOP→RUN clears non-retentive tags.
3. **`runner.reboot()`** — Power cycle simulation. Battery presence drives SRAM survival.
4. Add battery presence as a core system point (`sys.battery_present`) mapped to Click `SC203`.
5. Do not implement `tag.reset()` or `block.reset()` in this phase.

## Hardware-Verified Behavior (tested on physical Click PLC)

| Event | Non-retentive tags | Retentive tags |
|-------|-------------------|----------------|
| **Power cycle + battery** (SRAM intact) | Preserve | Preserve |
| **Power cycle + no battery** (SRAM lost) | Reset to default | Reset to default |
| **RUN→STOP→RUN** | Reset to default | Preserve |

Key insight: the `retentive` flag controls **STOP→RUN behavior**, not power-cycle survival.
Battery/SRAM is a blunt hardware question — either everything survived or nothing did.

## Public API Changes
1. `PLCRunner.stop() -> None` — Transition to STOP mode
2. `PLCRunner.reboot() -> SystemState` — Power cycle simulation
3. `PLCRunner.set_battery_present(value: bool) -> None`
4. New core system tag: `system.sys.battery_present` (read-only, derived)

No new `start()` or `run()` method — existing execution methods (`step()`, `run()`, `run_for()`,
`run_until()`, `scan_steps()`) auto-restart from STOP mode by performing the STOP→RUN transition
before proceeding. The existing `run(cycles)` signature is unchanged.

## Semantics

### 1. `runner.stop()` — Enter STOP mode
- Sets `_running = False`, updates `_MODE_RUN_KEY` → SC11 (`_PLC_Mode`) OFF.
- Tag values are preserved — STOP does not clear anything.
- Maps to Click SC50 (`_PLC_Mode_Change_to_STOP`).
- Idempotent: calling `stop()` while already stopped is a no-op.

### 2. Auto-restart from STOP (STOP→RUN transition)
Any execution method (`step()`, `run()`, `run_for()`, `run_until()`, `scan_steps()`) called
while stopped automatically performs the STOP→RUN transition before proceeding.

Tag behavior on STOP→RUN:
- Non-retentive tags: reset to `tag.default` (fallback to type default).
- Retentive tags: preserve current value.
- Battery presence is irrelevant — this is a software transition.

Runtime scope on STOP→RUN:
- Reset `scan_id=0`, `timestamp=0.0`.
- Clear internal memory.
- Clear pending patches and forces.
- Reset history to initial snapshot and set playhead to 0.
- Clear retained debug-trace caches.
- Preserve current time mode and dt.
- Preserve monitor/breakpoint registrations.
- Exclude system tags from manual seeding; system runtime derives/manages them.
- `SC2` (`_1st_SCAN`) ON for the first scan (automatic — `scan_id == 0`).

### 3. `runner.reboot()` — Power cycle
Tag behavior depends on battery:
- Battery present (`True`): ALL tags preserve (SRAM intact, PLC "wakes up").
- Battery absent (`False`): ALL tags reset to `tag.default` (SRAM lost, total wipe).

Runtime scope: same as STOP→RUN (scan_id, memory, patches, forces, history, etc. all clear).
After reboot, PLC is in RUN mode (`_running = True`).

### 5. Battery status
- `sys.battery_present` is read-only.
- Default for a new runner: `True`.
- Simulation-controlled via `runner.set_battery_present(...)`.

### 6. Initial runner state
- New runner starts in RUN mode.

### 7. Unknown ad-hoc keys
- String-only keys without known tag metadata are dropped during STOP→RUN and reboot rebuild.

### 8. Click system mapping
- `runner.stop()` ↔ SC50 (`_PLC_Mode_Change_to_STOP`) — writable from Modbus via ClickDataProvider.
- Auto-restart ↔ SC11 (`_PLC_Mode`) going ON when next execution method called.
- `sys.battery_present` ↔ SC203 (`_Battery_Installed`).

### 9. Integration with existing `_process_mode_commands()`
The existing system already handles `sys.cmd_mode_stop` writes within a scan:
- `_process_mode_commands()` sets `_MODE_RUN_KEY = False` when `cmd_mode_stop` is True.
- After this change, the runner must check `_MODE_RUN_KEY` at the end of each scan
  and sync `_running` accordingly, so a tag-level stop command takes effect.

## Implementation Plan
1. Update core system points
File: [system_points.py](c:/Users/Sam/Documents/GitHub/pyrung/src/pyrung/core/system_points.py)
- Add `battery_present` to `SysNamespace` and `system.sys`.
- Add resolver path as derived/read-only.
- Add runtime backing state default (`True`).

2. Update runner with mode control and reboot
File: [runner.py](c:/Users/Sam/Documents/GitHub/pyrung/src/pyrung/core/runner.py)
- Add `_running: bool` state flag (default `True`).
- Add battery config field and `set_battery_present`.
- Implement `stop()` — set `_running = False`, update `_MODE_RUN_KEY`.
- Add `_stop_to_run_transition()` private method — performs STOP→RUN (clear non-retentive, reset runtime, set `_running = True`).
- Wire auto-restart into `step()` (and by extension `run()`, `run_for()`, `run_until()`, `scan_steps()`): if `_running == False`, call `_stop_to_run_transition()` first.
- Implement `reboot()` — power-cycle with battery-aware tag handling, transition to RUN.
- Maintain known-tag metadata index for rebuild.
- Integrate with existing `_process_mode_commands()` — `sys.cmd_mode_stop` tag write should also trigger stop.

3. Update Click system mapping
File: [system_mappings.py](c:/Users/Sam/Documents/GitHub/pyrung/src/pyrung/click/system_mappings.py)
- Map `system.sys.battery_present` to `SC203` (`_Battery_Installed`), read-only.
- Wire SC50 write to `runner.stop()` in ClickDataProvider.

4. Update documentation
Files:
- [concepts.md](c:/Users/Sam/Documents/GitHub/pyrung/docs/getting-started/concepts.md)
- [runner.md](c:/Users/Sam/Documents/GitHub/pyrung/docs/guides/runner.md)
- Document `stop()` / `run()` / `reboot()` semantics and mode behavior.

## Test Cases
1. Core system points
File: [test_system_points.py](c:/Users/Sam/Documents/GitHub/pyrung/tests/core/test_system_points.py)
- `sys.battery_present` exists, read-only, default `True`.
- `set_battery_present` changes resolved value.

2. Click mapping
File: [test_system_points_mapping.py](c:/Users/Sam/Documents/GitHub/pyrung/tests/click/test_system_points_mapping.py)
- `sys.battery_present` resolves to `SC203`.
- Slot metadata is read-only and system-sourced.

3. Runner mode control (stop + auto-restart)
File: [test_runner_mode.py](c:/Users/Sam/Documents/GitHub/pyrung/tests/core/test_runner_mode.py)
- `stop()` sets SC11 OFF (`sys.mode_run` resolves to False).
- `stop()` while already stopped is a no-op.
- `step()` after `stop()` performs STOP→RUN transition, then executes one scan:
  - Non-retentive tags reset to default.
  - Retentive tags preserve current value.
  - scan_id, timestamp, memory, patches, forces, history, traces cleared.
  - First scan has `first_scan == True` (scan_id == 0).
- Same auto-restart behavior for `run()`, `run_for()`, `run_until()`, `scan_steps()`.
- Config preservation across stop/restart (`time_mode`, `dt`, debug registrations).
- Unknown ad-hoc keys are dropped on STOP→RUN.
- `sys.cmd_mode_stop` tag write during a scan also triggers stop (existing `_process_mode_commands` integration).

4. Runner reboot behavior (power cycle)
File: [test_runner_reboot.py](c:/Users/Sam/Documents/GitHub/pyrung/tests/core/test_runner_reboot.py)
- Battery present: ALL tags preserve (SRAM intact).
- Battery absent: ALL tags reset to default (SRAM lost).
- After reboot, PLC is in RUN mode.
- Runtime reboot scope same as STOP→RUN.
- Battery default is `True`.

## Click PLC Retention Reference

### Battery / SRAM Architecture
- **Flash ROM** stores the ladder program and project file (non-volatile, survives indefinitely).
- **SRAM** stores all runtime data (tag values, accumulators, etc.). Volatile — requires backup.
- **Supercapacitor** provides short-term SRAM retention (~7 days basic CPUs, ~1 hour Ethernet/PLUS).
- **Battery** (CR2354/CR2032) provides long-term SRAM retention (~3 years).
- SC203 (`_Battery_Installed`) is a **manual config bit** — the PLC cannot auto-detect battery presence.

### Power-Up Behavior (hardware-verified)
- **SRAM intact** (supercap/battery held): ALL registers preserve — retentive flag is irrelevant. PLC "wakes up."
- **SRAM lost** (no backup): ALL registers reinitialize from project file + initial values in Flash ROM.
- **RUN→STOP→RUN**: retentive registers preserve; non-retentive reset to initial values. This is where the retentive flag matters.

### Retention by Memory Type

| Type | Retentive | Notes |
|------|-----------|-------|
| DS, DD, DH, DF | Yes | Data registers |
| CT, CTD | Yes | Counter done-bits + accumulators |
| TXT | Yes | Text registers |
| C | Yes | Control relays — useful for alarm latching (alarm stays ON through power cycle, use `rise()`/`fall()` to detect edges) |
| T, TD | No (default) | Timer done-bits + accumulators — configurable per-instruction |
| X, Y, XD, YD | No | Physical I/O — refreshed from hardware |
| SC, SD | No | System-managed, read-only |

### Two Meanings of “Retentive” for Timers
These are orthogonal:
1. **Runtime behavior** (TON vs RTON): What happens when the enable input goes FALSE.
   - TON: accumulator resets to 0.
   - RTON: accumulator pauses, requires explicit reset.
2. **Memory retention**: Whether the value survives a RUN→STOP→RUN transition.
   - Configured per-address in Click memory settings (T1 and TD1 are linked — toggling one toggles both).
   - Timers default to non-retentive; counters default to retentive.
   - Note: this does NOT control power-cycle survival — that's purely a battery/SRAM question.

The `retentive` flag on pyrung tags maps to meaning 2 (STOP→RUN behavior). Runtime behavior is determined by instruction type (TON vs RTON).

## Internal Memory Audit

All keys in `SystemState.memory` use a leading `_` prefix (internal engine state):

| Key Pattern | Type | Purpose | Safe to Clear on Reset? |
|-------------|------|---------|------------------------|
| `_dt` | float | Current scan time delta | Yes — re-set every scan |
| `_prev:{tag}` | varies | Previous-scan value for `rise()`/`fall()` | Yes — no edges on first scan is correct power-up behavior |
| `_frac:{acc_tag}` | float | Timer sub-unit fractional remainder | Yes — even retentive timers lose fractional state on power-up |
| `_sys.rtc.offset` | timedelta | Simulated RTC clock offset | Yes — fresh boot resets clock |
| `_sys.mode.run` | bool | PLC run/stop mode flag | Yes — boots into RUN (`True`) |
| `_shift_prev_clock:{id}` | bool | Shift register clock edge detection | Yes — no edges on first scan |

**Conclusion:** All retentive-relevant state (done-bits, accumulators) lives in **tags**, not memory. Counters don't use memory at all. Timers only use memory for fractional remainder. Clearing all memory unconditionally on reset is safe — the tag-level retention rules cover everything that matters.

## Assumptions and Defaults
1. Scope excludes `tag.reset()` and `block.reset()`.
2. `runner.stop()` / `runner.run()` are the canonical mode-transition APIs (maps to Click SC50/SC11).
3. `runner.reboot()` is the power-cycle simulation API (battery determines outcome).
4. Battery default is present (`True`).
5. New runner starts in RUN mode.
6. “Unless otherwise specified” is modeled through `tag.default` for initialization paths.
