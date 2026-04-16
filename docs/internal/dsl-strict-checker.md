# DSL Strict Checker

`Program()`, `@subroutine()`, and `@program()` all default to `strict=True`. When strict mode is on, pyrung AST-walks the body at construction time and raises `ForbiddenControlFlowError` for Python constructs that don't belong in ladder logic.

## What it catches

| Construct | Hint |
|---|---|
| `if`/`elif`/`else` | Use `Rung(condition)` to express conditional logic |
| `and`/`or` | Use `And()` / `Or()` for compound conditions |
| `not` | Use `~Tag` for normally-closed contacts |
| `for`/`while` | Each rung is independent; express repeated patterns as separate rungs |
| assignment (`=`, `:=`, `+=`) | DSL instructions write to tags directly; no intermediate Python variables needed |
| `try`/`except` | Errors in DSL scope are programming mistakes; no recovery logic in ladder logic |
| comprehensions/generators | Build tag collections outside the Program scope, then reference them in rungs |
| `global`/`nonlocal` | DSL scope should not mutate external Python state |
| `yield`/`await`/`return` | Use `return_early()` for early subroutine exit |
| `import` | Move imports outside the Program/subroutine scope |
| `assert`/`raise`/`del` | Not valid in ladder logic; handle validation outside DSL scope |
| function/class defs | Define functions and classes outside the Program/subroutine scope |
| non-call expression statements | Only bare call expressions are allowed as statements |

## What's allowed

- `with Rung(...):` and nested `with branch(...):` — the core DSL
- Bare function calls — `out(tag)`, `latch(tag)`, `on_delay(timer, 1000)`, etc.
- `pass` — empty rungs

## How it works

The checker uses `inspect.getsourcelines()` to get the source of the `with Program():` block or `@subroutine`-decorated function, parses it with `ast.parse()`, then walks the statement list. `with` statements recurse into their bodies. Everything else is checked against the forbidden-node table.

For `with Program():` blocks (which may be at module level or inside a function), the checker finds the enclosing `with` node by matching the caller's line number against the parsed AST.

## Opting out

```python
with Program(strict=False) as logic:
    ...

@subroutine("sub1", strict=False)
def sub1():
    ...

@program(strict=False)
def my_logic():
    ...
```

Use this for generated code, metaprogramming, or cases where you know what you're doing. The error message always includes the opt-out hint.

## Error format

```
filename.py:42: forbidden Python construct 'if/elif/else' in strict DSL scope.
Use `Rung(condition)` to express conditional logic. Opt out with Program(strict=False).
```

Errors include the source file and line number, the construct name, the DSL-idiomatic alternative, and how to opt out.
