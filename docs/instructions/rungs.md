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
