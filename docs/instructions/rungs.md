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
with rung(Running):
    out(Motor)
    with branch(HighSpeed):
        out(FastLamp)  # On when Running AND HighSpeed
    out(RunLamp)
```

Each instruction evaluates only the path from the rail to itself. Instructions run in the order written, so rung and branch instructions interleave (not "all rung, then all branch"). A rung's own contacts are read once and hold for everything below:

```python
with rung(A):
    reset(A)
    out(Y)      # Still on this scan: A was already read
```

A branch reads its own contacts when execution reaches it, so it sees what the instructions above it wrote:

```python
with rung(A):
    latch(B)
    with branch(B):
        out(Y)  # On this scan: the branch reads B after the latch
```

Each rung starts fresh. The next rung sees the state as the previous rung's instructions left it.

A box with pins reads its enable and every pin together, before it writes anything. Examples are a counter's `.reset()` or `.down()`, a shift register's `.clock()` and a drum's events. A box with pins ends its rung. No other output or branch shares the rung with it, so put what follows it in a rung of its own.

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

For hand-written logic, flat branches are clearer.
