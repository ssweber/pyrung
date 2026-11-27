# Changelog

## Unreleased

## 0.19.0 - 2026-11-26

- Align the packaged debugger version with pyrung 0.19.0.

## 0.18.1 - 2026-11-25

- Align the packaged debugger version with pyrung 0.18.1.

## 0.18.0 - 2026-11-20

- Align the packaged debugger version with pyrung 0.18.0.

## 0.17.0 - 2026-11-18

- The debugger shows live tag values inside a `with rung(...)` block. 0.16.0 showed them only inside `with Rung(...)`.

## 0.12.0 - 2026-09-01

- Align the packaged debugger version with pyrung 0.12.0.

## 0.11.0 - 2026-08-24

- **Tag flag badges** — `RO` and `P` badges next to tag names in the Data View
- **Read-only lock/unlock** — readonly tags start locked (inputs and Force disabled); click the lock icon to unlock for debugging
- **Public filter** — checkbox above the tag table filters to only `public=True` tags; disabled until the debugger starts, resets when the session ends
- **Choice instant write** — selecting a value from a choices dropdown writes immediately (no "Write Values" click needed)
- **Fix "Add to Data View"** — command now works with `with rung(` (lowercase), single-word selections, and auto-focuses the Data View panel

## 0.1.0

Initial release.

- Source-line, conditional, and hit-count breakpoints
- Logpoints and snapshot logpoints (`Snapshot: label`)
- Data breakpoints for monitored tags
- Monitor values in the Variables panel (`PLC Monitors` scope)
- Trace decorations and inline condition annotations
- History slider webview for time-travel debugging
- Debug Console force commands (`force`, `unforce`, `clear_forces`)
- Inline values provider for tag lookups
