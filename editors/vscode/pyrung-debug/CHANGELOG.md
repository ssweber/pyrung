# Changelog

## Unreleased

- **Tag flag badges** — `RO` and `P` badges next to tag names in the Data View
- **Read-only lock/unlock** — readonly tags start locked (inputs and Force disabled); click the lock icon to unlock for debugging
- **Public filter** — checkbox above the tag table filters to only `public=True` tags; disabled until the debugger starts, resets when the session ends
- **Choice instant write** — selecting a value from a choices dropdown writes immediately (no "Write Values" click needed)

## 0.1.0

Initial release.

- Source-line, conditional, and hit-count breakpoints
- Logpoints and snapshot logpoints (`Snapshot: label`)
- Data breakpoints for monitored tags
- Monitor values in the Variables panel (`PLC Monitors` scope)
- Trace decorations and inline condition annotations
- Rapid auto-step mode (`next` / `stepIn` / `scan`)
- History slider webview for time-travel debugging
- Debug Console force commands (`force`, `unforce`, `clear_forces`)
- Inline values provider for tag lookups
