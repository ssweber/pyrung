# Plan: Replace `custom`/`acustom` with `run_function`/`run_enabled_function`

## Context

The `custom(fn)` and `acustom(fn)` escape hatches (commit a88efb0) give raw `ScanContext` access. This is too low-level - PLC functions pass arguments in and get results back, like Python functions. We want proper function call instructions with formal input/output parameter mapping and copy-in/execute/copy-out semantics (Do-More "User Defined Instruction" / IEC 61131-3 FUNCTION semantics).

Library is unpublished - no deprecation period. Remove `custom`/`acustom` entirely.

## Target API

```python
# --- run_function: stateless, rung-gated ---
def weighted_average(temp, pressure):
    return {'result': (temp + pressure) / 2}

with Rung(Enable):
    run_function(weighted_average,
        ins={'temp': SensorA, 'pressure': SensorB},
        outs={'result': Average})

# --- run_enabled_function: always-execute, enabled flag, class for state ---
class EmailHandler:
    def __init__(self, smtp_host):
        self.pending = None
        self.smtp_host = smtp_host

    def __call__(self, enabled, subject, body):
        if not enabled:
            cancel(self.pending); self.pending = None
            return {'sending': False, 'success': False, 'error': False}
        if self.pending is None:
            self.pending = submit_email(subject, body, self.smtp_host)
            return {'sending': True, 'success': False, 'error': False}
        if self.pending.done():
            ok = self.pending.result().ok
            self.pending = None
            return {'sending': False, 'success': ok, 'error': not ok}
        return {'sending': True, 'success': False, 'error': False}

email = EmailHandler("smtp.example.com")
with Rung(Enable):
    run_enabled_function(email,
        ins={'subject': Subject, 'body': Body},
        outs={'sending': Sending, 'success': Success, 'error': Error})
```

## Execution Semantics

**`run_function`** (rung-gated):
1. Only executes when rung conditions are true
2. Resolve input tags â†’ `kwargs = {name: resolve(source) for each ins}`
3. Call `result = fn(**kwargs)`
4. Write `result[key]` â†’ output tags (with copy-style type coercion)

**`run_enabled_function`** (always-execute):
1. Executes every scan, regardless of rung state
2. Compute `enabled` from captured rung condition
3. Resolve input tags â†’ kwargs
4. Call `result = fn(enabled, **kwargs)` â€” `enabled` is first positional arg
5. Write `result[key]` â†’ output tags

State management: class instances (or closures). State lives outside SystemState â€” the user manages it. This is simple and Pythonic.

## DSL Signatures

```python
def run_function(
    fn: Callable[..., dict[str, Any]],
    ins: dict[str, Tag | IndirectRef | IndirectExprRef | Any] | None = None,
    outs: dict[str, Tag | IndirectRef | IndirectExprRef] | None = None,
    *,
    oneshot: bool = False,
) -> None

def run_enabled_function(
    fn: Callable[..., dict[str, Any]],
    ins: dict[str, Tag | IndirectRef | IndirectExprRef | Any] | None = None,
    outs: dict[str, Tag | IndirectRef | IndirectExprRef] | None = None,
) -> None
```

## File Changes

### 1. `src/pyrung/core/instruction.py` â€” Replace instruction classes

**Remove:** `LambdaInstruction`, `AsyncLambdaInstruction`

**Add `FunctionCallInstruction`** (after `OneShotMixin`, ~line 235):
```python
class FunctionCallInstruction(OneShotMixin, Instruction):
    """Stateless function call: copy-in / execute / copy-out."""

    def __init__(self, fn, ins, outs, oneshot=False):
        OneShotMixin.__init__(self, oneshot)
        self._fn = fn
        self._ins = ins or {}
        self._outs = outs or {}

    def execute(self, ctx):
        if not self.should_execute():
            return
        kwargs = {name: resolve_tag_or_value_ctx(src, ctx) for name, src in self._ins.items()}
        result = self._fn(**kwargs)
        if not self._outs:
            return
        if result is None:
            raise TypeError(f"run_function: {_fn_name(self._fn)!r} returned None but outs were declared")
        for key, target in self._outs.items():
            if key not in result:
                raise KeyError(f"run_function: {_fn_name(self._fn)!r} missing key {key!r}; got {sorted(result)}")
            resolved = resolve_tag_ctx(target, ctx)
            ctx.set_tag(resolved.name, _store_copy_value_to_tag_type(result[key], resolved))
```

**Add `AsyncFunctionCallInstruction`**:
```python
class AsyncFunctionCallInstruction(Instruction):
    """Always-execute function call with enabled flag."""

    def __init__(self, fn, ins, outs, enable_condition):
        self._fn = fn
        self._ins = ins or {}
        self._outs = outs or {}
        self._enable_condition = enable_condition

    def always_execute(self):
        return True

    def execute(self, ctx):
        enabled = True
        if self._enable_condition is not None:
            enabled = bool(self._enable_condition.evaluate(ctx))
        kwargs = {name: resolve_tag_or_value_ctx(src, ctx) for name, src in self._ins.items()}
        result = self._fn(enabled, **kwargs)
        if not self._outs:
            return
        if result is None:
            raise TypeError(f"run_enabled_function: {_fn_name(self._fn)!r} returned None but outs were declared")
        for key, target in self._outs.items():
            if key not in result:
                raise KeyError(f"run_enabled_function: {_fn_name(self._fn)!r} missing key {key!r}; got {sorted(result)}")
            resolved = resolve_tag_ctx(target, ctx)
            ctx.set_tag(resolved.name, _store_copy_value_to_tag_type(result[key], resolved))
```

Add small helper: `def _fn_name(fn): return getattr(fn, '__name__', type(fn).__name__)`

### 2. `src/pyrung/core/program.py` â€” Replace DSL functions

**Remove:** `custom()`, `acustom()`, `_validate_custom_callback()`

**Add `_validate_function_call()`** (near line 93):
```python
def _validate_function_call(fn, ins, outs, *, func_name):
    if not callable(fn):
        raise TypeError(f"{func_name}() fn must be callable, got {type(fn).__name__}")
    call_attr = getattr(fn, "__call__", None)
    if inspect.iscoroutinefunction(fn) or inspect.iscoroutinefunction(call_attr):
        raise TypeError(f"{func_name}() fn must be synchronous (async def is not supported)")
    if ins is not None:
        if not isinstance(ins, dict):
            raise TypeError(f"{func_name}() ins must be a dict, got {type(ins).__name__}")
        try:
            sig = inspect.signature(fn)
            sig.bind(**{k: object() for k in ins})
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"{func_name}() ins keys {sorted(ins.keys())} incompatible with "
                f"{getattr(fn, '__name__', repr(fn))!r} signature"
            ) from exc
    if outs is not None and not isinstance(outs, dict):
        raise TypeError(f"{func_name}() outs must be a dict, got {type(outs).__name__}")
```

Note: For `run_enabled_function`, validation needs to account for `enabled` as first arg â€” bind check becomes `sig.bind(object(), **{k: object() for k in ins})`.

**Add DSL functions:**
```python
def run_function(fn, ins=None, outs=None, *, oneshot=False):
    ctx = _require_rung_context("run_function")
    _validate_function_call(fn, ins, outs, func_name="run_function")
    ctx._rung.add_instruction(FunctionCallInstruction(fn, ins, outs, oneshot))

def run_enabled_function(fn, ins=None, outs=None):
    ctx = _require_rung_context("run_enabled_function")
    _validate_function_call(fn, ins, outs, func_name="run_enabled_function", has_enabled=True)
    enable_condition = ctx._rung._get_combined_condition()
    ctx._rung.add_instruction(AsyncFunctionCallInstruction(fn, ins, outs, enable_condition))
```

### 3. `src/pyrung/core/__init__.py` â€” Update exports

- Remove `custom`, `acustom` from imports and `__all__`
- Add `run_function`, `run_enabled_function` to imports and `__all__`

### 4. `src/pyrung/examples/custom_math.py` â€” Rewrite for `run_function`

Rewrite `weighted_average` as a plain function returning a dict. Update DSL usage to `run_function(...)`.

### 5. `src/pyrung/examples/click_email.py` â€” Rewrite for `run_enabled_function`

Rewrite `email_instruction` as a callable class. State in instance attributes. Function receives `enabled` as first arg, tag values as kwargs, returns output dict.

### 6. Remove old test files, create new ones

**Remove:**
- `tests/core/test_custom_instruction.py`
- `tests/core/test_acustom_instruction.py`

**Create `tests/core/test_run_function.py`:**
- Basic copy-in/execute/copy-out with tag inputs and outputs
- Skipped when rung false
- Literal values in `ins`
- Mixed tag + literal inputs
- Oneshot fires once per activation
- `ins=None` (no-argument function)
- `outs=None` (return value discarded)
- Output type coercion (INT clamping)
- Missing output key â†’ KeyError
- Extra output keys silently ignored
- Function returning None with outs â†’ TypeError
- Outside rung â†’ RuntimeError
- Non-callable â†’ TypeError
- Async function rejected
- ins keys not matching function params â†’ TypeError
- ins not a dict â†’ TypeError
- Expression and IndirectRef in `ins`
- Function exception propagates

**Create `tests/core/test_run_enabled_function.py`:**
- Callback invoked every scan with correct `enabled` transitions
- Callback receives resolved tag values as kwargs
- Runs when rung false (always_execute)
- Class instance state persists across scans
- Output tags written correctly
- Outside rung â†’ RuntimeError
- Non-callable â†’ TypeError
- Async rejected
- ins keys + enabled arg validation

**Update:**
- `tests/examples/test_custom_math_example.py` â†’ test rewritten example
- `tests/examples/test_click_email_example.py` â†’ test rewritten example

## Reused Utilities (from instruction.py)

- `resolve_tag_or_value_ctx(source, ctx)` â€” line 63 â€” resolves Tag/IndirectRef/Expression/literal
- `resolve_tag_ctx(target, ctx)` â€” line 98 â€” resolves target to concrete Tag
- `_store_copy_value_to_tag_type(value, tag)` â€” line 835 â€” copy-style type coercion
- `OneShotMixin` â€” line 218 â€” oneshot behavior
- `_get_combined_condition()` â€” rung.py line 58 â€” capture rung condition for async

## Verification

```bash
make    # install + lint + test
```

- All existing tests pass (except removed custom/acustom tests)
- New run_function tests pass
- New run_enabled_function tests pass
- Updated example tests pass
- No references to custom/acustom remain in exports

