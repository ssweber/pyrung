<proposed_plan>
## Strict DSL Control-Flow Guard (Default On)

### Summary
- Add a best-effort AST/inspect check for DSL-definition scopes.
- Default `on`, opt-out per program via `Program(strict=False)`.
- Support decorator forms: `@program` and `@program(strict=False)`.
- Subroutines checked at decoration time: `@subroutine("name")` (default on), opt-out via `@subroutine("name", strict=False)`.
- On source-unavailable contexts, `warnings.warn(..., RuntimeWarning)` and skip check.
- Add `ForbiddenControlFlowError(RuntimeError)` for violations.

### Allowed AST Nodes (Allowlist)

Only these AST node types are permitted inside DSL scope (Program body, subroutine body):

| AST Node | Purpose |
|----------|---------|
| `With`   | `Rung(...)`, `branch(...)`, `subroutine(...)` context managers |
| `Expr` containing a `Call` | Bare DSL calls: `out()`, `latch()`, `call()`, etc. |
| `Pass`   | No-op (harmless) |

Everything else is rejected. This is future-proof against new Python syntax —
new constructs are denied by default unless explicitly allowed.

### Hints for Common Violations

When a forbidden construct is detected, the error message should include a
DSL-specific hint pointing the user to the correct alternative:

| Construct | Hint |
|-----------|------|
| `if`/`elif`/`else`, ternary | "Use `Rung(condition)` to express conditional logic" |
| `and`/`or` | "Use `all_of()` / `any_of()` for compound conditions" |
| `not` | "Use `nc()` for normally-closed contacts" |
| `for`/`while` | "Each rung is independent; express repeated patterns as separate rungs" |
| `=`, `+=`, `:=`, annotated assign | "DSL instructions write to tags directly; no intermediate Python variables needed" |
| `try`/`except` | "Errors in DSL scope are programming mistakes; no recovery logic in ladder logic" |
| Comprehensions/generators | "Build tag collections outside the Program scope, then reference them in rungs" |
| `global`/`nonlocal` | "DSL scope should not mutate external Python state" |
| `yield`/`await`/`return` | "Use `return_()` for early subroutine exit; no Python control flow in DSL scope" |
| `import` | "Move imports outside the Program/subroutine scope" |
| `assert`/`raise`/`del` | "Not valid in ladder logic; handle validation outside DSL scope" |
| `FunctionDef`/`ClassDef` | "Define functions and classes outside the Program/subroutine scope" |

All error messages also include: file:line, the construct name, and an opt-out hint
(`Program(strict=False)` or `@subroutine("name", strict=False)`).

### AST Walk Scope

The checker walks the **full function body** (strict), including nested scopes.
Nested function/class definitions inside a DSL scope are themselves forbidden —
there is no valid reason to define them inside ladder logic.

### Implementation
1. Add internal AST allowlist checker in `src/pyrung/core/program.py`.
2. Run check in `Program.__enter__` when `strict=True`.
3. Run function-body check for `@program` decorated functions when enabled.
4. Run function-body check for `@subroutine` decorated functions **at decoration time** (not at `call()` time). Subroutine bodies are always DSL scope.
5. Raise `ForbiddenControlFlowError` with construct + file:line + DSL hint + opt-out hint.
6. Warn-and-skip on inspect/parse failure.

### API Changes
- `Program(*, strict: bool = True)`
- `program` decorator supports dual-call pattern:
  - `@program` — bare decorator, checks enabled (default)
  - `@program(strict=False)` — called with keyword args
  - Implementation: `def program(fn=None, *, strict=True)` with `fn=None` sentinel to distinguish the two calling conventions.
- `subroutine` decorator gains opt-out parameter:
  - `@subroutine("name")` — checks enabled (default)
  - `@subroutine("name", strict=False)` — opt-out
  - Check runs at decoration time, independent of any active Program context.

### Tests
- Add guard tests in `tests/core/test_program.py` for representative forbidden constructs (one per AST category, not exhaustive per-node).
- Add tests verifying that all three allowed node types (`With`, `Expr(Call)`, `Pass`) are accepted.
- Add tests for opt-out behavior (`Program(strict=False)`, `@program(strict=False)`, `@subroutine("name", strict=False)`).
- Add fallback test (inspect failure => warning, no guard exception).
- Update `tests/core/test_program.py:281`: remove `ret = search(...)` assignment; call `search(...)` bare and assert on `Result` directly.

### Assumptions
- This is feedback-first, best-effort static checking, not complete proof of valid ladder logic.
- The allowlist approach means new Python syntax is denied by default — no maintenance burden per Python release.
</proposed_plan>
