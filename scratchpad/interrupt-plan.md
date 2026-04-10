# Task Programs — Implementation Plan

## Overview

Tasks are named blocks of ladder logic with execution policies. Instead of CLICK's "interrupt" framing, we use the more general "task" concept (inspired by Productivity Suite's task management) where the trigger is configured via `.when()`.

Core is permissive — `.when()` accepts any condition or timer preset. Dialect validators (Click, CircuitPython) restrict to their supported trigger types.

Two built-in trigger types:
1. **Timer task** — fires on a periodic time interval (preset + unit)
2. **Event task** — fires on a condition (e.g. `rise(tag)`, `fall(tag)`)

These generalize to cover:
- CLICK's software interrupts (timer task)
- CLICK's input interrupts (event task with `rise()`/`fall()`)
- Productivity Suite's "Run Every Second" (timer task with `preset=1, unit="Ts"`)
- Productivity Suite's "Run First Scan Only" (event task with `first_scan` — future)

## DSL Syntax

```python
with Program() as logic:
    # Main logic — runs every scan (unchanged)
    with Rung(Button):
        out(Light)

    # Timer task (default unit="Tms")
    with task("fast_count").when(preset=100):
        with Rung():
            count_up(rise(Always), counter, 1)

    # Timer task with explicit unit
    with task("slow_poll").when(preset=5, unit="Ts"):
        with Rung():
            ...

    # Event task — condition trigger
    with task("emergency").when(rise(EStop)):
        with Rung():
            out(Alarm)
```

### `.when()` API

`task("name")` returns a `Task` object. `.when()` configures the trigger and returns a context manager:

```python
# Timer triggers
task("name").when(preset=100)               # every 100ms (unit="Tms" default)
task("name").when(preset=5, unit="Ts")      # every 5 seconds

# Condition triggers
task("name").when(rise(EStop))              # on rising edge
task("name").when(fall(Sensor))             # on falling edge
task("name").when(some_condition)           # core allows any condition
```

`preset=` and a positional condition are mutually exclusive. `unit=` defaults to `"Tms"`, only valid with `preset=`.

### Decorator form

```python
@task("fast_count").when(preset=100)
def fast_count():
    with Rung():
        count_up(rise(Always), counter, 1)

@task("emergency").when(rise(EStop))
def emergency_stop():
    with Rung():
        out(Alarm)
```

## Restrictions

Same as subroutines:
- Cannot `call()` subroutines from within a task
- Supports `return_()` for early exit
- A task cannot trigger another task (no nesting)

## Semantics

- Tasks run **once per scan**, after `_prepare_scan()` and before the main logic loop
- This is simpler and more predictable for testing than per-rung-boundary checking; the granularity difference doesn't matter for pyrung's use cases
- Task routines execute on the **same `ScanContext`** as the main program — writes are visible to all main-program rungs and commit together at scan end
- Timer tasks fire **at most once per scan** — accumulate simulation time, fire when threshold crossed, reset accumulator
- Event tasks fire when their condition evaluates true at the start of the scan
- If `dt` is set appropriately (at or below task period), timer behavior is correct. Setting `dt` higher than the task period is user error — we don't engineer around it
- Priority ordering when multiple tasks are pending: timer tasks first (in definition order), then event tasks (in definition order)

## Architecture

### Storage: `Program`

Tasks replace `subroutines` — one unified dict on `Program`:

```python
class Program:
    def __init__(self):
        self.rungs: list[RungLogic] = []
        self.tasks: dict[str, TaskDef] = {}  # replaces subroutines
```

`TaskDef` holds the trigger config (optional) and the list of rungs:

```python
@dataclass
class TaskDef:
    name: str
    rungs: list[RungLogic]
    # Timer trigger (mutually exclusive with condition_trigger)
    preset: int | Tag | None = None
    unit: str = "Tms"
    # Condition trigger (mutually exclusive with preset)
    condition_trigger: Condition | None = None
    # No trigger (preset=None, condition_trigger=None) → callable via call()
```

### DSL: `_control_flow.py`

Two classes — `Task` (the builder) and `TaskWhen` (the configured context manager):

```python
class Task:
    """Builder for a task. Must call .when() to configure trigger."""

    def __init__(self, name: str, *, strict: bool = True):
        self._name = name
        self._strict = strict

    def when(self, condition=None, *, preset=None, unit="Tms") -> TaskWhen:
        """Configure the task trigger. Returns a context manager."""
        # Validate: condition and preset are mutually exclusive
        # Validate: must provide one or the other
        return TaskWhen(self._name, condition=condition, preset=preset,
                        unit=unit, strict=self._strict)


class TaskWhen:
    """Configured task — usable as context manager or decorator."""

    def __init__(self, name, *, condition=None, preset=None, unit="Tms", strict=True):
        self._name = name
        self._condition = condition
        self._preset = preset
        self._unit = unit
        self._strict = strict

    def __enter__(self):
        # Register with Program, similar to Subroutine.__enter__
        # Set flag so call() is rejected inside
        ...

    def __exit__(self, ...):
        # Finalize, similar to Subroutine.__exit__
        ...

    def __call__(self, fn):
        # Decorator form — returns TaskFunc (mirrors SubroutineFunc)
        ...
```

### Runner: Task Execution

The runner needs:

1. **Timer accumulators** — one per timer task, tracking elapsed simulation time
2. **Task check once per scan** — before main logic in `_scan_steps()`
3. **Task routine execution** — same pattern as `_call_subroutine_ctx()`

#### Timer state in runner

```python
# In PLC.__init__ or _start():
self._task_accumulators: dict[str, float] = {}  # name -> accumulated seconds
```

#### Modified scan loop

```python
def _scan_steps(self):
    ctx, dt = self._prepare_scan()
    self._run_pending_tasks(ctx, dt)  # once, before main logic
    for i, rung in enumerate(self._logic):
        rung.evaluate(ctx)
        yield i, rung, ctx
    self._commit_scan(ctx, dt)
```

#### Task trigger logic

```python
def _run_pending_tasks(self, ctx: ScanContext, dt: float) -> None:
    if not self._task_defs:
        return  # no tasks defined — basically free

    # Accumulate dt for timer tasks
    self._accumulate_task_timers(dt)

    for name, task_def in self._task_defs.items():
        if self._should_fire_task(name, task_def, ctx):
            self._run_task(name, task_def, ctx)

def _should_fire_task(self, name, task_def, ctx) -> bool:
    if task_def.preset is not None:
        # Timer: check if accumulator exceeded preset (converted to seconds)
        threshold = TimeUnit[task_def.unit].to_seconds(preset_value)
        if self._task_accumulators[name] >= threshold:
            self._task_accumulators[name] -= threshold  # reset
            return True
    elif task_def.condition_trigger is not None:
        # Condition: evaluate against ctx
        return task_def.condition_trigger.evaluate(ctx)
    return False

def _run_task(self, name, task_def, ctx) -> None:
    # Same pattern as _call_subroutine_ctx
    saved_snapshot = ctx._condition_snapshot
    ctx._condition_snapshot = None
    try:
        for rung in task_def.rungs:
            rung.evaluate(ctx)
    except SubroutineReturnSignal:
        pass
    finally:
        ctx._condition_snapshot = saved_snapshot
```

### Debug support: `_scan_steps_debug()`

`_scan_steps_debug()` should yield `ScanStep` objects for task rungs with a new kind like `"task"` and include the task name, so the DAP adapter and trace formatter can display them properly.

## Validation

### Core validation
- Task names must be unique (no collision with subroutine names either)
- `.when()` must be called — bare `task("name")` is not a valid context manager
- `preset=` and positional condition are mutually exclusive in `.when()`
- `unit=` only valid with `preset=`
- `call()` inside a task body raises error
- Nesting tasks is not allowed

### Click dialect validation
- Timer tasks: preset must be 1–60000 (Tms) or 1–60 (Ts)
- Event tasks: condition must be `rise()` or `fall()` on a Bool tag mapped to a DC input
- Up to 4 timer tasks and limited input tasks per DC CPU module

### Productivity dialect validation (future)
- Timer tasks restricted to preset=1, unit="Ts" (the fixed "Run Every Second")
- Event tasks not supported (Productivity has no input interrupts)

## Testing Strategy

### Timer task tests
- Basic: task fires after accumulated time exceeds preset
- Fires at most once per scan
- Accumulator resets after firing
- Multiple timer tasks with different periods
- Task writes are visible to subsequent main-program rungs
- Different time units (Tms, Ts)

### Event task tests
- `rise()` trigger fires on 0->1 transition
- `fall()` trigger fires on 1->0 transition
- Does not fire while input is steady
- Trigger via `patch()` and `force()`

### DSL tests
- `.when()` required — bare `task()` errors
- Mutually exclusive params in `.when()`
- Decorator form works
- Context manager form works
- `return_()` exits task early

### Restriction tests
- `call()` inside task raises error
- Nested task raises error

### Integration tests
- Timer + event tasks coexisting
- Task modifying tags read by main program
- Task with timer/counter instructions inside
- Debug/trace support for task rungs

## Implementation Order

1. `TaskDef` dataclass and `Program.tasks` storage
2. `Task` builder + `TaskWhen` context manager + `task()` function in `_control_flow.py`
3. Decorator form (`TaskFunc`)
4. Validation (`.when()` required, mutually exclusive params, no `call()` inside, unique names)
5. Runner: timer accumulators + `_run_pending_tasks()` in `_scan_steps()`
6. Runner: condition trigger evaluation
7. `_scan_steps_debug()` support (ScanStep with `kind="task"`)
8. Tests for all of the above
9. Click dialect validation (interrupt limits, trigger type restrictions)
10. Click ladder export support (interrupt routines with pink background)

## Unification: subroutine as task

A subroutine is just a task with no `.when()` — triggered explicitly via `call()` rather than automatically.

```python
# These are equivalent:
with subroutine("convert_units"):
    with Rung(): ...

with task("convert_units"):  # no .when() — called explicitly via call()
    with Rung(): ...
```

### Storage unification

`Program.subroutines` and `Program.tasks` merge into a single dict:

```python
class Program:
    def __init__(self):
        self.rungs: list[RungLogic] = []
        self.tasks: dict[str, TaskDef] = {}  # unified — replaces subroutines dict
```

Each `TaskDef` has an optional trigger. Tasks with a trigger run automatically each scan (if condition met). Tasks without a trigger are callable via `call()`.

```python
@dataclass
class TaskDef:
    name: str
    rungs: list[RungLogic]
    # Optional trigger — None means "call() only" (subroutine)
    preset: int | Tag | None = None
    unit: str = "Tms"
    condition_trigger: Condition | None = None
```

### Backwards compatibility

`subroutine()` stays as sugar — it creates a `TaskDef` with no trigger. `call()` works on any task without a trigger. Calling a triggered task raises an error (it runs automatically, not on demand).

### Validation rules

- `call()` targets must be tasks with no trigger (i.e. subroutine-style)
- Triggered tasks cannot be `call()`'d
- Tasks with triggers cannot contain `call()` to other tasks (same restriction as current subroutines)
- A task body (triggered or not) cannot nest another task definition

## Relationship to existing concepts

| Concept | pyrung | CLICK | Productivity Suite |
|---|---|---|---|
| Main logic | `Program.rungs` | Main program | Run Every Scan |
| Called task | `task("x")` / `subroutine("x")` + `call()` | Subroutine | Run When Called |
| Periodic task | `task("x").when(preset=N)` | Software interrupt | Run Every Second |
| Event task | `task("x").when(rise(tag))` | Input interrupt | N/A |
| Init task | Future: `task("x").when(first_scan)` | N/A | Run First Scan Only |
