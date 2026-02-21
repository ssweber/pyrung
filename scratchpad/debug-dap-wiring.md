# Debug DAP Wiring Plan

## Context

Core debug APIs are now available on `PLCRunner` / `History`:

- Monitors: `runner.monitor(tag, callback)` with handles (`id/remove/enable/disable`)
- Predicate breakpoints: `runner.when(predicate).pause()`
- Snapshot labels: `runner.when(predicate).snapshot(label)`
- Label lookup: `history.find(label)`, `history.find_all(label)`

Current DAP adapter (`src/pyrung/dap/adapter.py`) already supports:

- Source-line breakpoints (`setBreakpoints`)
- Step/continue/pause flows (`scan_steps_debug`)
- Force commands via `evaluate`
- Trace events (`pyrungTrace`)

What is missing is wiring the new core APIs into adapter protocol + VS Code UX.

---

## Goals

1. Expose core predicate breakpoints, snapshot labels, and monitors through DAP.
2. Keep existing source-line breakpoint behavior unchanged.
3. Preserve adapter threading rules (worker thread never writes directly).
4. Provide clear test coverage in `tests/dap/test_adapter.py`.

## Non-goals (initial wiring)

1. New timeline UI widgets in VS Code (tree/webview).
2. Changing DAP standard requests beyond additive custom requests/events.
3. Replacing existing `setBreakpoints` source-line mapping behavior.

---

## Design Decisions

1. Use **custom DAP requests** for new controls instead of overloading `evaluate`.
2. Keep core API semantics:
   - Source-line breakpoints are step-boundary execution stops.
   - Predicate breakpoints are committed-scan conditions.
3. Emit custom events for asynchronous updates (monitor/snapshot activity).
4. Track adapter-owned handle metadata so we can show/list/remove registrations safely.

---

## Custom Protocol Surface

### Requests

1. `pyrungAddMonitor`
- args: `{ "tag": "TagName" }`
- returns: `{ "id": 3, "tag": "TagName", "enabled": true }`

2. `pyrungEnableMonitor` / `pyrungDisableMonitor` / `pyrungRemoveMonitor`
- args: `{ "id": 3 }`
- returns: `{ "id": 3, "enabled": true|false, "removed": true|false }`

3. `pyrungListMonitors`
- returns: `{ "monitors": [{ "id": 3, "tag": "TagName", "enabled": true, "removed": false }] }`

4. `pyrungAddPredicate`
- args:
  - `action`: `"pause"` or `"snapshot"`
  - `label`: required for `"snapshot"`, omitted for `"pause"`
  - `predicate`: JSON predicate spec (see below)
- returns:
  - `{ "id": 7, "action": "pause", "enabled": true, "predicate": { ... } }`
  - or `{ "id": 8, "action": "snapshot", "label": "fault_triggered", "enabled": true, ... }`

5. `pyrungEnablePredicate` / `pyrungDisablePredicate` / `pyrungRemovePredicate`
- args: `{ "id": 7 }`
- returns shape similar to monitor control responses.

6. `pyrungListPredicates`
- returns list with `id/action/label/enabled/removed/predicate`.

7. `pyrungFindLabel`
- args: `{ "label": "fault_triggered" }`
- returns:
  - `{ "match": null }` when absent
  - `{ "match": { "scanId": 42, "timestamp": 12.3 } }` when present

8. `pyrungFindAllLabels`
- args: `{ "label": "fault_triggered" }`
- returns: `{ "matches": [{ "scanId": 10, "timestamp": 1.0 }, ...] }`

### Events

1. `pyrungMonitor`
- body: `{ "id": 3, "tag": "TagName", "current": "...", "previous": "...", "scanId": 42, "timestamp": 12.3 }`

2. `pyrungSnapshot`
- body: `{ "id": 8, "label": "fault_triggered", "scanId": 42, "timestamp": 12.3 }`

### Predicate JSON Spec (v1)

Use a safe adapter-compiled spec (no `eval`):

1. Truthy tag
```json
{ "kind": "tag_truthy", "tag": "Fault" }
```

2. Tag compare
```json
{ "kind": "tag_compare", "tag": "MotorTemp", "op": ">", "value": 100 }
```

Supported `op`: `==`, `!=`, `<`, `<=`, `>`, `>=`

---

## Adapter Implementation Plan

## A. Runtime state + lifecycle

1. Add registries in `src/pyrung/dap/adapter.py`:
- `_monitor_handles: dict[int, Any]`
- `_monitor_meta: dict[int, dict[str, Any]]`
- `_predicate_handles: dict[int, Any]`
- `_predicate_meta: dict[int, dict[str, Any]]`
- `_pending_predicate_pause: bool` (adapter-local stop signal during continue)

2. On `launch` and `_shutdown`, clear all adapter-owned registrations:
- call `remove()` on all stored handles (idempotent)
- clear metadata maps

3. Keep all map mutation under `_state_lock`.

## B. Predicate compilation + registration

1. Add `_compile_predicate(spec) -> Callable[[SystemState], bool]`.
2. Validate spec shape and operator support; raise `DAPAdapterError` on bad input.
3. For `pyrungAddPredicate`:
- compile predicate
- wrap predicate to emit adapter events and pause signal:
  - on true + action `snapshot`: queue `pyrungSnapshot`
  - on true + action `pause`: set `_pending_predicate_pause = True`
- register on runner:
  - `runner.when(wrapped).pause()` or `.snapshot(label)`
- store returned handle + metadata.

## C. Continue-loop stop integration

1. In `_continue_worker`, after each `_advance_one_step_locked()`:
- if `_pending_predicate_pause` set, emit stopped event with `reason="breakpoint"` and return.
2. Clear `_pending_predicate_pause` when continue starts and when worker exits.
3. Keep source breakpoint checks unchanged; predicate pause is additional stop condition.

## D. Monitor callback integration

1. On `pyrungAddMonitor`, register callback that:
- captures current `runner.current_state.scan_id`/`timestamp`
- queues internal event `{kind: "internal_event", event: "pyrungMonitor", body: ...}`
- never raises
2. Store monitor handle/meta for list/remove/enable/disable requests.

## E. Label query endpoints

1. `pyrungFindLabel`: call `runner.history.find(label)`.
2. `pyrungFindAllLabels`: call `runner.history.find_all(label)`.
3. Return lightweight summaries (`scanId`, `timestamp`) only.

---

## VS Code Extension Wiring

Target files:

- `editors/vscode/pyrung-debug/extension.js`
- `editors/vscode/pyrung-debug/package.json`
- optional lightweight controller module for debug actions

## Commands (v1)

1. `pyrung.addMonitor`
2. `pyrung.removeMonitor`
3. `pyrung.addPausePredicate`
4. `pyrung.addSnapshotPredicate`
5. `pyrung.listLabelMatches`

Implementation pattern:

- Prompt via `showInputBox` / `showQuickPick`
- Call `vscode.debug.activeDebugSession.customRequest(...)`
- Print results to an output channel (initial UX)

## Event handling

1. Extend adapter tracker to listen for:
- `pyrungMonitor`
- `pyrungSnapshot`
2. Surface concise notifications/output entries:
- monitor delta lines
- snapshot hit lines with label + scan id

---

## Testing Plan

## Adapter tests (`tests/dap/test_adapter.py`)

1. `pyrungAddMonitor` registration + list response.
2. monitor callback emits `pyrungMonitor` event on tag change.
3. monitor enable/disable/remove behavior.
4. `pyrungAddPredicate` pause action stops continue with `stopped(reason="breakpoint")`.
5. snapshot predicate emits `pyrungSnapshot` and labels retained in history.
6. label query requests return expected scan IDs.
7. request validation errors for malformed predicate specs.

## Extension smoke tests (manual)

1. Start session, register monitor, verify `pyrungMonitor` output on change.
2. Register pause predicate, run continue, verify stop near triggering scan boundary.
3. Register snapshot predicate, verify snapshot event and label query results.

---

## Rollout Order

1. Adapter protocol + runtime wiring (no extension changes yet).
2. Adapter tests green.
3. Extension command wiring and event display.
4. Docs update (`docs/guides/dap-vscode.md`) for custom commands.

---

## Acceptance Criteria

1. New core APIs are controllable through DAP custom requests.
2. Continue can stop on predicate pause hits without regressing source breakpoints.
3. Snapshot hits are observable and queryable by label through adapter.
4. Monitor changes are emitted as asynchronous events.
5. Existing DAP stepping, trace, and force workflows remain unchanged.
