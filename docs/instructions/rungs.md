# Rungs

For an introduction to the DSL vocabulary, see [Core Concepts](../getting-started/concepts.md).

## Rung comments

Attach a comment to a rung using the `as` variable:

```python
with Rung(Button) as r:
    r.comment = "Initialize the light system."
    out(Light)
```

Multi-line comments use triple-quoted strings (automatically dedented and stripped):

```python
with Rung(Button) as r:
    r.comment = """
        This rung controls the main light.
        It activates when Button is pressed.
    """
    out(Light)
```

Comments are limited to 1400 characters. Exceeding this raises `ValueError`.

## Branching

`branch()` creates a parallel path within a rung. The branch condition is ANDed with the parent rung's condition.

```python
with Rung(First):          # ① Evaluate: First
    out(Third)             # ③ Execute
    with branch(Second):   # ② Evaluate: First AND Second
        out(Fourth)        # ④ Execute
    out(Fifth)             # ⑤ Execute
```

Three rules:

1. **Conditions evaluate before instructions.** ① and ② are resolved before ③ ④ ⑤ run. A branch ANDs its own condition with the parent rung's.
2. **Instructions execute in source order.** ③ → ④ → ⑤, as written — not "all rung, then all branch."
3. **Each rung starts fresh.** The next rung sees the state as it was left after the previous rung's instructions.

### Nested branches

Branches can nest inside other branches. All conditions at every depth evaluate against the same rung-entry snapshot.

```python
with Rung(A):
    out(X)
    with branch(B):
        out(Y)
        with branch(C):
            out(Z)  # Executes when A AND B AND C
```

This exists so codegen can faithfully represent imported ladder topologies. For hand-written logic, flat branches are clearer.

## Continued rungs

`Rung.continued()` tells a rung to reuse the previous rung's condition snapshot instead of freezing a fresh one. All conditions in the continued rung evaluate against the pre-instruction state from the original rung.

```python
with Rung(A):
    out(X)
with Rung(B).continued():
    out(Y)  # B evaluated against pre-X state (same snapshot as A)
```

This models the Click ladder editor pattern where a single visual rung has multiple independent wires to the right power rail. Without `.continued()`, splitting into separate `Rung` blocks would give each its own snapshot — changing behavior if the first rung's instructions mutate a tag that the second rung's conditions reference.

Multiple continued rungs chain — they all share the original snapshot:

```python
with Rung(A):
    copy(10, Counter)
with Rung(Counter == 0).continued():
    out(X)  # True: Counter was 0 at snapshot time
with Rung(Counter == 10).continued():
    out(Y)  # False: Counter was 0 at snapshot time, not 10 yet
```

A normal (non-continued) rung breaks the chain and takes a fresh snapshot.

Constraints:

- `continued()` cannot be the first rung in a program or subroutine.
- Continued rungs cannot have their own `.comment`.

Like nested branches, this exists primarily for codegen round-tripping. For hand-written logic, separate rungs with fresh snapshots are easier to reason about.
