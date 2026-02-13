# Escape Hatch: `custom()` + `acustom()` in pyrung DSL

## Context

Strict DSL mode intentionally forbids Python control flow inside `Program` / `Rung` scopes (`ForbiddenControlFlowError`). That should remain unchanged.

Some real PLC behaviors still need a sanctioned escape hatch:
- synchronous multi-step logic that does not map cleanly to existing primitives
- asynchronous state machines (email/protocol requests) that must be polled across scans

The previous single-API plan was insufficient for async because rung-gated instructions do not execute while rung power is false, so pending async work cannot be polled reliably.

## Design Decisions

### 1. Two APIs (explicit split)

- `custom(fn, oneshot=False)` for synchronous rung-gated logic
- `acustom(fn)` for async/stateful scan-to-scan logic

This avoids conflating two different execution models.

### 2. Execution contracts

- `custom` callback signature: `fn(ctx: ScanContext) -> None`
- `acustom` callback signature: `fn(ctx: ScanContext, enabled: bool) -> None`

`enabled` is the rung combined condition value. `acustom` callback runs every scan (via `always_execute()`), including when rung power is false, so it can poll/cancel pending work and clear status.

### 3. Validation and error behavior

Both DSL entry points perform eager validation:
- callback must be callable
- callback must be compatible with required arguments
- callback must be synchronous (reject coroutine functions)

Callback exceptions propagate (same failure behavior as existing instruction errors).

## Locked Contracts

### Public API additions

```python
def custom(fn: Callable[[ScanContext], None], oneshot: bool = False) -> None
def acustom(fn: Callable[[ScanContext, bool], None]) -> None
```

### Validation algorithm (decision-complete)

Both APIs use one shared private validator with `required_args` set to `1` (`custom`) or `2` (`acustom`):

1. `if not callable(fn)`: raise `TypeError`
2. reject coroutine callbacks:
   - `inspect.iscoroutinefunction(fn)` OR
   - `inspect.iscoroutinefunction(getattr(fn, "__call__", None))`
3. inspect signature:
   - `inspect.signature(fn)`
   - if unavailable (`TypeError` / `ValueError`), raise `TypeError`
4. arity compatibility check:
   - call `sig.bind(*sentinel_args)` where sentinel args length equals required args
   - if binding fails, raise `TypeError`

### Callable compatibility policy (strict)

Invocation is positional only:
- `custom` invokes `fn(ctx)`
- `acustom` invokes `fn(ctx, enabled)`

Accepted callback signatures:
- exact positional forms: `(ctx)` and `(ctx, enabled)`
- positional-with-default extras: `(ctx, extra=...)`, `(ctx, enabled, extra=...)`
- varargs forms: `(ctx, *args)`, `(ctx, enabled, *args)`
- bound methods and callable instances that satisfy the same bind check

Rejected callback signatures:
- missing required args: `()`, `(ctx)` for `acustom`
- extra required args: `(ctx, required2)`, `(ctx, enabled, required3)`
- keyword-only required parameters: `(ctx, *, required_kw)` or `(*, ctx)`
- coroutine callbacks (`async def ...`)

Resulting behavior is deterministic:
- if `inspect.signature(fn).bind(*sentinel_args)` succeeds, callback is accepted
- otherwise rejected with the existing incompatible-signature `TypeError` string

### Exact error text

- non-callable:
  - `custom() fn must be callable, got {type(fn).__name__}`
  - `acustom() fn must be callable, got {type(fn).__name__}`
- coroutine callback:
  - `custom() callback must be synchronous (async def is not supported)`
  - `acustom() callback must be synchronous (async def is not supported)`
- signature unavailable:
  - `custom() could not inspect callback signature`
  - `acustom() could not inspect callback signature`
- incompatible signature:
  - `custom() expects callable compatible with (ctx)`
  - `acustom() expects callable compatible with (ctx, enabled)`

### Memory key namespace conventions

Reserved existing runtime prefixes (must not be used by examples):
- `_dt`
- `_prev:`
- `_sys.`

New documented convention for escape-hatch callback state:
- Prefix: `_custom:`
- Format: `_custom:{feature}:{instance}:{field}`
- Example instance key anchor: a required status tag name (for email: `sending.name`)

For `click_email` example, exact keys:
- `base = f"_custom:email:{sending.name}"`
- `pending_key = f"{base}:pending"` -> stores `Future | None`
- `prev_enabled_key = f"{base}:prev_enabled"` -> stores `bool`
- `attempt_key = f"{base}:attempt"` -> stores `int` (monotonic attempt counter for debug/test observability)

## Plan

### 1. Add instruction types

**File:** `src/pyrung/core/instruction.py`

Add:

1. `LambdaInstruction(OneShotMixin, Instruction)`
- Constructor: `fn: Callable[[ScanContext], None], oneshot: bool = False`
- `execute`: apply one-shot gate, then call `fn(ctx)`
- Rung-gated (normal instruction behavior)

2. `AsyncLambdaInstruction(Instruction)`
- Constructor: `fn: Callable[[ScanContext, bool], None], enable_condition: Condition | None`
- `always_execute() -> True`
- `execute`: compute `enabled` from captured condition and call `fn(ctx, enabled)`

Implementation note:
- import `Callable` from `collections.abc`
- import `Condition` under `TYPE_CHECKING` to avoid runtime cycles

### 2. Add callback validation helpers

**File:** `src/pyrung/core/program.py`

Add private helper(s), e.g. `_validate_custom_callback(...)`, that:
- enforce `callable(fn)`
- reject coroutine functions (`inspect.iscoroutinefunction`)
- verify callable accepts required positional args using `inspect.signature(...).bind(...)`
- use exact error strings from `Locked Contracts`

### 3. Add DSL functions

**File:** `src/pyrung/core/program.py`

Add:

```python
def custom(fn: Callable[[ScanContext], None], oneshot: bool = False) -> None: ...
def acustom(fn: Callable[[ScanContext, bool], None]) -> None: ...
```

Behavior:

1. `custom(...)`
- requires rung context via `_require_rung_context("custom")`
- validates callback
- adds `LambdaInstruction(fn, oneshot)`

2. `acustom(...)`
- requires rung context via `_require_rung_context("acustom")`
- validates callback
- captures rung condition with `ctx._rung._get_combined_condition()`
- adds `AsyncLambdaInstruction(fn, enable_condition)`

Intentional choice:
- `acustom` does not expose `oneshot`; edge-trigger/start-once logic should be handled in callback memory state using `enabled`.

Exact runtime context errors remain delegated to `_require_rung_context`:
- `custom() must be called inside a Rung context`
- `acustom() must be called inside a Rung context`

### 4. Public exports

**File:** `src/pyrung/core/__init__.py`

- add `custom`, `acustom` to imports from `pyrung.core.program`
- add both names to `__all__` in instruction section

### 5. Add examples package and two example modules

Create new package:
- `src/pyrung/examples/__init__.py`

Add synchronous example:
- `src/pyrung/examples/custom_math.py`
- `weighted_average(...)->Callable[[ScanContext], None]`
- DSL usage sample uses `custom(weighted_average(...))`

Add asynchronous example:
- `src/pyrung/examples/click_email.py`
- `email_instruction(...)->Callable[[ScanContext, bool], None]`
- uses `ThreadPoolExecutor` + `Future` pending state
- stores state in the exact `_custom:email:{sending.name}:*` keys defined above
- state machine:
  - on rising `enabled`: start request if none pending, set `sending=True`
  - while pending and not done: keep `sending=True`
  - when done: set `success/error/error_code`, clear pending
  - when disabled: best-effort cancel pending and clear status
- attempt counter increments only when a new send is submitted
- DSL usage sample uses `acustom(email_instruction(...))`

### 6. Tests

#### Core tests: custom

**File:** `tests/core/test_custom_instruction.py` (new)

- executes when rung true
- skipped when rung false
- oneshot fires once per activation and resets after rung false
- reads + writes tags
- uses memory (`get_memory` / `set_memory`)
- `custom()` outside rung raises `RuntimeError`
- non-callable callback raises `TypeError`
- wrong callback arity raises `TypeError`
- async-def callback rejected with exact `TypeError` message
- keyword-only required parameter signature rejected (`def f(*, ctx): ...`)
- extra required positional parameter rejected (`def f(ctx, x): ...`)
- varargs signature accepted (`def f(ctx, *args): ...`)

#### Core tests: acustom

**File:** `tests/core/test_acustom_instruction.py` (new)

- callback invoked every scan with correct `enabled` flag transitions
- callback still invoked when rung false (via `always_execute`)
- async-style pending memory flow works across true->false scans
- `acustom()` outside rung raises `RuntimeError`
- non-callable / wrong-arity callbacks raise `TypeError`
- async-def callback rejected with exact `TypeError` message
- missing `enabled` arg rejected (`def f(ctx): ...`)
- keyword-only required parameter signature rejected (`def f(ctx, *, enabled): ...`)
- varargs signature accepted (`def f(ctx, enabled, *args): ...`)

#### Example tests

**File:** `tests/examples/test_click_email_example.py` (new)

- monkeypatch submit helper to control returned `Future`
- verify exact scan-by-scan transitions:
  - scan 1 (`enabled=True` rising): submit once, `sending=True`, `success=False`, `error=False`
  - scan 2 (`enabled=True`, future pending): still `sending=True`, no resubmit
  - scan 3 (`enabled=True`, future done ok): `sending=False`, `success=True`, `error=False`, `error_code=0`
  - scan 4 (`enabled=False`): pending canceled/cleared, status tags cleared
- verify memory keys are written under `_custom:email:{sending.name}:*`
- verify `attempt` increments only on new submission

**File:** `tests/examples/test_custom_math_example.py` (new)

- weighted average output is correct
- zero total weight returns `0`

### 7. Verification

```bash
make lint
make test
```

Acceptance criteria:
- all existing tests stay green
- new tests pass
- strict DSL guard behavior unchanged
- async example is able to progress pending work even when rung is false
- callback validation errors match exact strings in `Locked Contracts`

## Files to Modify

| File | Change |
|------|--------|
| `src/pyrung/core/instruction.py` | Add `LambdaInstruction` and `AsyncLambdaInstruction` |
| `src/pyrung/core/program.py` | Add callback validation helpers + `custom()` + `acustom()` |
| `src/pyrung/core/__init__.py` | Export `custom` and `acustom` |
| `src/pyrung/examples/__init__.py` | **New** package marker |
| `src/pyrung/examples/click_email.py` | **New** async example for `acustom` |
| `src/pyrung/examples/custom_math.py` | **New** sync example for `custom` |
| `tests/core/test_custom_instruction.py` | **New** tests for `custom` |
| `tests/core/test_acustom_instruction.py` | **New** tests for `acustom` |
| `tests/examples/test_click_email_example.py` | **New** async example tests |
| `tests/examples/test_custom_math_example.py` | **New** sync example tests |

## Assumptions

- Async support means scan-to-scan polling/cancel behavior, not native `async def` callbacks.
- Status-tag patterns in async examples should mirror existing send/receive instruction behavior.
- No backward compatibility impact because APIs are additive.
