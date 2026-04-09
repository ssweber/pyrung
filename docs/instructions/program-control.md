# Program Control

For an introduction to the DSL vocabulary, see [Core Concepts](../getting-started/concepts.md).

## Programs

Two equivalent ways to define a program:

```python
# Context manager
with Program() as logic:
    with Rung(Start):
        latch(Running)

# Decorator
@program
def logic():
    with Rung(Start):
        latch(Running)
```

Both produce a `Program` you pass to `PLC`. See [Core Concepts — Programs](../getting-started/concepts.md#programs) for details.

## Subroutines

### Context-manager style

```python
with Program() as logic:
    with subroutine("startup"):
        with Rung(Step == 0):
            out(InitLight)

    with Rung(AutoMode):
        call("startup")
```

### Decorator style

```python
@subroutine("init")
def init_sequence():
    with Rung():
        out(InitLight)

with Program() as logic:
    with Rung(Button):
        call(init_sequence)     # auto-registers and calls
```

## For loops

`forloop` repeats a block of instructions N times within a single scan:

```python
with Rung():
    with forloop(5):
        copy(Counter + 1, Counter)
```

The count can be a literal or a tag (resolved each scan):

```python
with Rung():
    with forloop(LoopCount):
        copy(Counter + 1, Counter)
```

Use `loop.idx` for indirect addressing inside the loop body:

```python
with Rung():
    with forloop(3) as loop:
        copy(Src[loop.idx + 1], Dst[loop.idx + 1])
```

End and return instructions aren't needed — Python indentation handles scope.
