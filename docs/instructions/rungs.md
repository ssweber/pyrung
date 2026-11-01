# Rungs

For an introduction to the DSL vocabulary, see [Core Concepts](../getting-started/concepts.md).

## rung comments

Use `comment()` before a rung to attach a comment:

```python
comment("Initialize the light system.")
with rung(Button):
    out(Light)
```

Multi-line comments use triple-quoted strings (automatically dedented and stripped):

```python
comment("""
    This rung controls the main light.
    It activates when Button is pressed.
""")
with rung(Button):
    out(Light)
```

Comments are limited to 1400 characters. Exceeding this raises `ValueError`.

## Branching

`branch()` creates a parallel path within a rung. The branch condition is ANDed with the parent rung's condition.

```python
with rung(First):          # ① Read: First
    out(Third)             # ② Execute
    with branch(Second):   # ③ Read: Second (First's power is held)
        out(Fourth)        # ④ Execute
    out(Fifth)             # ⑤ Execute
```

Three rules:

1. **A rung is commonly referred to as evaluated left to right, top to bottom; actually it's a little more nuanced than that.** Each instruction solves only the path from the rail to it. The rung's own contacts (①) are read once and hold for everything below. A branch reads its own contacts (③) when the first instruction in it needs them, so it sees what the instructions above it wrote (②). A branch ANDs its own condition with the parent rung's.
2. **Instructions execute in source order.** ② → ④ → ⑤, as written — not "all rung, then all branch."
3. **Each rung starts fresh.** The next rung sees the state as it was left after the previous rung's instructions.

```python
with rung(A):
    latch(B)
    with branch(B):
        out(Y)  # On in the same scan: the branch reads B after the latch
```

A box with pins (a counter's `.reset()` or `.down()`, a shift register's `.clock()`, a drum's events) reads its enable and every pin together, before it writes anything.

### Nested branches

Branches can nest inside other branches. Each one reads its own contacts where it stands, under the power of the branches around it.

```python
with rung(A):
    out(X)
    with branch(B):
        out(Y)
        with branch(C):
            out(Z)  # Executes when A AND B AND C
```

This exists so codegen can faithfully represent imported ladder topologies. For hand-written logic, flat branches are clearer.

## Continued rungs

`rung.continued()` draws a rung as a further wire of the rung above it: in CLICK, one visual rung with several independent wires to the right power rail. It does not change what the rung does: a continued rung is solved like a rung of its own, and its contacts read the state the instructions above it left.

```python
with rung(A):
    copy(10, Counter)
with rung(Counter == 10).continued():
    out(X)  # True: Counter was set to 10 above
```

`.continued()` exists so CLICK projects round-trip: a CLICK rung with two wires imports as a rung and a continued rung, and exports back as one CLICK rung. The exporter also uses it when it has to split a rung (a box with pins needs a CLICK rung of its own; see the [CLICK dialect](../dialects/click.md)).

Constraints:

- `continued()` cannot be the first rung in a program or subroutine.
- Continued rungs cannot have their own `.comment`.

For hand-written logic, separate rungs are easier to read and behave the same.
