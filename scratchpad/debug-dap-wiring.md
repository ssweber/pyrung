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
2. Use standard DAP features wherever possible — conditional breakpoints,
   data breakpoints, logpoints — instead of custom protocol.
3. Use the pyrung DSL as the condition expression language so users don't
   learn a second syntax.
4. Keep existing source-line breakpoint behavior unchanged.
5. Preserve adapter threading rules (worker thread never writes directly).
6. Provide clear test coverage in `tests/dap/test_adapter.py`.

## Non-goals (initial wiring)

1. New timeline UI widgets in VS Code (tree/webview).
2. `rise()` / `fall()` edge-detection functions (stateful — add later).
3. Dot-notation tag paths (add later if needed).

---

## Design Decisions

1. **Condition expressions use the pyrung DSL.** Users type the same
   syntax in breakpoint conditions as in `with Rung(...)`. The adapter
   parses condition strings with a small recursive-descent parser that
   produces an AST and compiles to `Callable[[SystemState], bool]`.

2. **Monitors surface as a Variables scope.** A "PLC Monitors" scope in
   the standard Variables panel via `scopesRequest` / `variablesRequest`.
   Custom requests only for add/remove lifecycle.

3. **Predicate-pause breakpoints surface as Data Breakpoints.** Implement
   `dataBreakpointInfoRequest` and `setDataBreakpointsRequest` so
   predicate breakpoints appear in the standard Breakpoints panel.

4. **Snapshots surface as logpoints.** DAP logpoints (`setBreakpoints`
   with `logMessage`) map to `runner.when(...).snapshot(label)`. A
   `Snapshot: label` message convention triggers snapshot registration.
   Logpoints can also have conditions.

5. **Minimal custom protocol.** Only 3 custom requests remain (monitor
   add/remove, label query). Everything else uses standard DAP.

---

## Expression Language

### Grammar

Same as pyrung DSL rung conditions. Commas are implicit AND.
Operator precedence matches Python: `&`/`|` bind tighter than
comparisons, so comparisons need parens when used with `&`/`|`.
`~` negates a single tag only.

```
expr       = item ("," item)*              # comma = implicit AND
item       = or_item
or_item    = and_item ("|" and_item)*
and_item   = atom ("&" atom)*
atom       = "~" TAG                       # negate single tag
           | "(" expr ")"                  # grouping
           | TAG comp_op VALUE             # comparison
           | TAG                           # truthy
           | "all_of(" expr ")"            # explicit AND
           | "any_of(" expr ")"            # explicit OR
comp_op    = "==" | "!=" | "<" | "<=" | ">" | ">="
VALUE      = NUMBER | BOOL | STRING
TAG        = valid tag reference (e.g. Fault, Motor1.Running, c[1])
```

### Examples

```
Fault                          tag is truthy
~Fault                         tag is falsy
MotorTemp > 100                comparison
Fault, Pump                    comma = implicit AND
Fault, MotorTemp > 100         implicit AND with comparison
Fault & Pump                   & works for truthy tags
Running | ~Estop               | and ~ work for truthy tags
Fault & (MotorTemp > 100)      & with comparison needs parens
Running | (Mode == 1)          | with comparison needs parens
Running | ~Estop, Mode == 1    mix commas and operators freely
all_of(Fault, Pump, Valve)     explicit AND (same as commas)
any_of(Low, High, Emergency)   explicit OR
```

### Parser

New file: `src/pyrung/dap/expressions.py`

- `parse(source: str) -> Expr` — hand-written recursive descent, ~150 lines.
- `compile(expr: Expr) -> Callable[[SystemState], bool]` — AST walker.
- `validate(source: str) -> list[str]` — returns parse errors without compiling.

No DAP dependency. Pure logic, easily unit tested with `SystemState` fixtures.

Parse errors produce clear messages with position:
`"Expected operator or end of expression, got '$$' at position 12"`

### AST

```python
@dataclass
class TagRef:
    name: str

@dataclass
class Literal:
    value: int | float | bool | str

@dataclass
class Compare:
    tag: TagRef
    op: str | None           # None means truthy
    right: Literal | None

@dataclass
class Not:
    child: TagRef            # ~ only applies to single tags

@dataclass
class And:
    children: list[Expr]

@dataclass
class Or:
    children: list[Expr]

Expr = Compare | Not | And | Or
```

---

## DAP Protocol Surface

### Standard DAP (doing the heavy lifting)

| DAP Feature | pyrung Use |
|---|---|
| `setBreakpoints` | Source-line rung breakpoints |
| `setBreakpoints` + `condition` | Conditional rung breakpoints (DSL expression) |
| `setBreakpoints` + `logMessage` | Snapshot predicates (`Snapshot: label`) |
| `setBreakpoints` + `hitCondition` | Nth-hit breakpoints (free) |
| `setDataBreakpointsRequest` | Tag-change breakpoints |
| `dataBreakpointInfoRequest` | Enable data breakpoints from Variables panel |
| `scopesRequest` / `variablesRequest` | Monitor values in "PLC Monitors" scope |
| Breakpoints panel | Enable/disable/remove everything |
| Inline condition editor | User types DSL expressions directly |

### Custom Requests (3 total)

| Request | Args | Returns |
|---|---|---|
| `pyrungAddMonitor` | `{ "tag": "TagName" }` | `{ "id": 3, "tag": "TagName", "enabled": true }` |
| `pyrungRemoveMonitor` | `{ "id": 3 }` | `{ "id": 3, "removed": true }` |
| `pyrungFindLabel` | `{ "label": "name", "all": false }` | `{ "matches": [{ "scanId": 42, "timestamp": 12.3 }] }` |

`pyrungListMonitors` can stay for diagnostics but the Variables scope
is the primary display.

### Custom Events (2 total)

| Event | Body |
|---|---|
| `pyrungMonitor` | `{ "id": 3, "tag": "TagName", "current": "...", "previous": "...", "scanId": 42, "timestamp": 12.3 }` |
| `pyrungSnapshot` | `{ "label": "fault_triggered", "scanId": 42, "timestamp": 12.3 }` |

`pyrungMonitor` fires asynchronously between stops for the output channel.
The Variables scope shows values at stop-time (standard DAP refresh).

---

## Adapter Implementation Plan

### A. Runtime state + lifecycle

1. Add registries in `src/pyrung/dap/adapter.py`:
   - `_monitor_handles: dict[int, Any]`
   - `_monitor_meta: dict[int, dict[str, Any]]`
   - `_monitor_scope_ref: int` (variablesReference for the monitors scope)
   - `_monitor_values: dict[int, str]` (latest value per monitor, updated on callback)
   - `_data_bp_handles: dict[str, Any]` (keyed by `dataId`)
   - `_data_bp_meta: dict[str, dict[str, Any]]`
   - `_pending_predicate_pause: bool` (adapter-local stop signal during continue)

2. On `launch` and `_shutdown`, clear all adapter-owned registrations:
   - Call `remove()` on all stored handles (idempotent).
   - Clear metadata maps.

3. Keep all map mutation under `_state_lock`.

4. **Threading note:** `_pending_predicate_pause` is a simple boolean set
   `True` by a predicate callback (worker thread) and checked/cleared by
   the continue loop (also worker thread). Python's GIL makes bool
   assignment atomic, so no lock is needed for this flag. Do not promote
   this to a more complex structure without reconsidering synchronization.

### B. Expression parser module

New file: `src/pyrung/dap/expressions.py` (see Expression Language above).

No DAP dependency. Rolled out and tested first before any adapter integration.

### C. Conditional source-line breakpoints

Extend existing `setBreakpoints` handler:

1. If `breakpoint.condition` is present, parse and compile it.
2. Store the compiled predicate alongside the breakpoint.
3. In the continue loop, when a source-line breakpoint is hit, evaluate
   the condition. Skip the stop if condition is false.
4. If parsing fails, return the breakpoint as `verified: false` with
   `message: "<parse error>"`. VS Code shows this inline.

### D. Logpoint → snapshot mapping

Extend `setBreakpoints` handler:

1. If `breakpoint.logMessage` starts with `Snapshot:`, extract the label.
2. If a `condition` is also present, parse and compile it.
3. Register `runner.when(condition_if_any).snapshot(label)`.
4. On snapshot hit:
   - Emit `pyrungSnapshot` event.
   - Print to debug console: `"Snapshot taken: fault_triggered (scan 42)"`.
   The logpoint behaves like a real logpoint (visible output) with the
   snapshot as a side effect.
5. If `logMessage` doesn't start with `Snapshot:`, treat as standard log
   message (evaluate and print to debug console — standard DAP behavior).

### E. Variables scope for monitors

1. In `scopesRequest`, append a scope when `_monitor_handles` is non-empty:
   ```python
   {"name": "PLC Monitors", "variablesReference": self._monitor_scope_ref,
    "namedVariables": len(self._monitor_handles)}
   ```

2. In `variablesRequest`, when `variablesReference == self._monitor_scope_ref`:
   - Return one `Variable` per active monitor from `_monitor_meta` + `_monitor_values`.
   - Set `value` to the cached last-known value.

### F. Data Breakpoints for tag-change stops

1. `dataBreakpointInfoRequest`:
   - For variables in the PLC Monitors scope, return a valid `dataId`
     (tag name) and a human-readable `description`.
   - For other variables, return `dataId: null`.

2. `setDataBreakpointsRequest`:
   - Receive full list of desired data breakpoints (replace-all semantics).
   - Diff against `_data_bp_handles` by `dataId`:
     - **New:** register a wrapper that sets `_pending_predicate_pause = True`.
       If `condition` is present, parse/compile and gate the trigger on it.
     - **Removed:** call `remove()` on handle, delete from maps.
     - **Unchanged:** keep as-is.
   - Return verification array.
   - **Important:** `dataId` serialization must be deterministic so diff
     identity matching works reliably.

3. `_pending_predicate_pause` is the sole mechanism that stops the
   continue loop for data breakpoints. The core `runner.when(...).pause()`
   is not used — avoids double-stop race.

### G. Continue-loop stop integration

1. In `_continue_worker`, after each `_advance_one_step_locked()`:
   - Drain the internal event queue (flush `pyrungMonitor` / `pyrungSnapshot`
     events to the client).
   - If `_pending_predicate_pause` is set, emit
     `stopped(reason="data breakpoint")` and return.
2. Clear `_pending_predicate_pause` when continue starts and when worker exits.
3. Source-line breakpoint checks (with conditions) remain unchanged;
   predicate pause is an additional stop condition.

### H. Monitor callback integration

1. On `pyrungAddMonitor`, register callback that:
   - Updates `_monitor_values[id]` with the new value.
   - Captures current `runner.current_state.scan_id` / `timestamp`.
   - Queues internal event for `pyrungMonitor`.
   - Never raises.
2. Event queue is drained in the continue loop (section G) and also
   on stop.

### I. Label query endpoint

1. `pyrungFindLabel`: call `runner.history.find(label)` or
   `runner.history.find_all(label)` based on `all` flag.
2. Return lightweight summaries (`scanId`, `timestamp`) only.

---

## VS Code Extension Wiring

Target files:

- `editors/vscode/pyrung-debug/extension.js`
- `editors/vscode/pyrung-debug/package.json`

### Commands (3 total)

| Command | Implementation |
|---|---|
| `pyrung.addMonitor` | `showInputBox` → tag name → `pyrungAddMonitor` custom request |
| `pyrung.removeMonitor` | `showQuickPick` from active monitors → `pyrungRemoveMonitor` |
| `pyrung.findLabel` | `showInputBox` → label → `pyrungFindLabel` → show result |

No predicate commands needed — users set conditions through standard
breakpoint UI.

### Status bar

- Single `StatusBarItem`: `"🔍 M:3"` (3 monitors).
- Click opens quick-pick: add monitor, remove monitor, find label.
- Updates on debug session start/stop and after add/remove.

### Event handling

- `DebugAdapterTrackerFactory` listens for `pyrungMonitor` and `pyrungSnapshot`.
- Output channel `pyrung: Debug Events`:
  - Monitor: formatted delta line with tag, old value, new value, scan ID.
  - Snapshot: hit line with label + scan ID.

### Inline decorations (stretch)

- For monitors on tags that map to specific rungs, show current value as
  inline `TextEditorDecorationType` next to the source line.
- Requires tag → source location mapping.
- Scoped as stretch for v1.

---

## Testing Plan

### Expression parser tests (`tests/dap/test_expressions.py`)

1. Truthy: `"Fault"` → `Compare(TagRef("Fault"), None, None)`.
2. Negation: `"~Fault"` → `Not(TagRef("Fault"))`.
3. Comparison: `"MotorTemp > 100"` → correct AST.
4. Comma implicit AND: `"Fault, Pump"` → `And([..., ...])`.
5. Comma with comparison: `"Fault, MotorTemp > 100"` → correct AST.
6. `&` truthy: `"Fault & Pump"` → `And(...)`.
7. `&` with parens: `"Fault & (MotorTemp > 100)"` → correct AST.
8. `|` truthy: `"Running | ~Estop"` → `Or(...)`.
9. `|` with parens: `"Running | (Mode == 1)"` → correct AST.
10. Mixed: `"Running | ~Estop, Mode == 1"` → `And(Or(...), Compare(...))`.
11. `all_of` / `any_of`: correct grouping.
12. Parse errors: `"MotorTemp >>"`, `"&"`, `""`, `"~(A | B)"` → clear messages.
13. Compile + evaluate against mock `SystemState`.

### Conditional breakpoint tests (`tests/dap/test_adapter.py`)

1. `setBreakpoints` with condition → verified, stops only when true.
2. `setBreakpoints` with bad condition → `verified: false`, `message` set.
3. `setBreakpoints` with `Snapshot:` logpoint → snapshot registered, event emitted, message printed to debug console.
4. `setBreakpoints` with `Snapshot:` logpoint + condition → conditional snapshot.
5. `setBreakpoints` with plain logpoint → message printed to debug console.
6. `hitCondition` → stops on Nth hit.

### Data breakpoint tests (`tests/dap/test_adapter.py`)

7. `dataBreakpointInfoRequest` returns valid `dataId` for monitor variables.
8. `setDataBreakpointsRequest` registers breakpoint, continue stops with `stopped(reason="data breakpoint")`.
9. `setDataBreakpointsRequest` diff: add new, remove stale, keep unchanged.
10. Data breakpoint with condition → stops on change + condition true.

### Monitor tests (`tests/dap/test_adapter.py`)

11. `scopesRequest` includes "PLC Monitors" after monitor registration.
12. `variablesRequest` returns current monitor values.
13. Monitor callback emits `pyrungMonitor` event on tag change.
14. `pyrungAddMonitor` / `pyrungRemoveMonitor` lifecycle.

### Other

15. Label query returns expected scan IDs.
16. Shutdown clears all registrations.

---

## Rollout Order

1. Expression parser module + unit tests.
2. Conditional source-line breakpoints in adapter + tests.
3. Logpoint → snapshot mapping + tests.
4. Monitor Variables scope + custom requests + tests.
5. Data breakpoint support + tests.
6. Extension: commands, status bar, event display.
7. Inline decorations (stretch).
8. Docs update (`docs/guides/dap-vscode.md`).

---

## Acceptance Criteria

1. Condition expressions use pyrung DSL syntax — no second language to learn.
2. Conditional breakpoints work through standard VS Code inline editor.
3. Monitor values visible in the standard Variables panel under "PLC Monitors" scope.
4. Tag-change breakpoints manageable through standard Breakpoints panel via Data Breakpoints.
5. Snapshot logpoints register labels and emit events.
6. Continue stops on conditional breakpoint / data breakpoint hits without regressing source-line breakpoints.
7. Monitor changes emitted as async events to output channel.
8. Label queries return expected scan IDs.
9. Only 3 custom requests, 2 custom events — everything else is standard DAP.
10. Existing DAP stepping, trace, and force workflows remain unchanged.
