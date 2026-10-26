# Program Control

For an introduction to the DSL vocabulary, see [Core Concepts](../getting-started/concepts.md).

## Programs

Two equivalent ways to define a program:

```python
# Context manager
with Program() as logic:
    with rung(Start):
        latch(Running)

# Decorator
@program
def logic():
    with rung(Start):
        latch(Running)
```

Both produce a `Program` you pass to `PLC`. See [Core Concepts — Programs](../getting-started/concepts.md#programs) for details.

## Subroutines

### Context-manager style

```python
with Program() as logic:
    with subroutine("startup"):
        with rung(Step == 0):
            out(InitLight)

    with rung(AutoMode):
        call("startup")
```

### Decorator style

```python
@subroutine("init")
def init_sequence():
    with rung():
        out(InitLight)

with Program() as logic:
    with rung(Button):
        call(init_sequence)     # auto-registers and calls
```

## For loops

`forloop` repeats a block of instructions N times within a single scan:

```python
with rung():
    with forloop(5):
        copy(Counter + 1, Counter)
```

The count can be a positive literal or a tag (resolved each scan). If a tag
resolves to 0 or a negative value, the loop executes once.

```python
with rung():
    with forloop(LoopCount):
        copy(Counter + 1, Counter)
```

Use `loop.idx` for indirect addressing inside the loop body:

```python
with rung():
    with forloop(3) as loop:
        copy(Src[loop.idx + 1], Dst[loop.idx + 1])
```

End and return instructions aren't needed — Python indentation handles scope.

### Body rungs

The loop body is a run of rungs between For and Next. A bare instruction is a
body rung with no contacts. A body rung with contacts is a `with rung(...)`
inside the loop; its contacts are evaluated on every iteration, against the
values the earlier body rungs just wrote:

```python
with rung(Enable):
    with forloop(10) as loop:
        with rung(Src[loop.idx] == 0):
            continue_loop()
        copy(Src[loop.idx], Dst[loop.idx])
        with rung(Fault):
            break_loop()
```

`continue_loop()` skips the rest of the current iteration. `break_loop()` leaves
the innermost loop; the scan carries on after its Next. Both need a body rung
with at least one contact, as in CLICK. When the loop's own rung is false, the
body runs once with power off: its `out()` coils turn off and its one-shots
re-arm.

A body rung can have branches, as any rung can. A branch's contacts see the
same per-iteration snapshot as its body rung's:

```python
with rung(Enable):
    with forloop(10) as loop:
        with rung(Src[loop.idx] > 0):
            copy(Src[loop.idx], Dst[loop.idx])
            with branch(Alarm):
                out(Flag)
```

`branch()` must sit in a body rung, not directly in the loop body, and a body
rung cannot hold another `with rung()`. `break_loop()` and `continue_loop()`
go on the body rung itself, not in its branches.

### Nested loops

Loops nest up to three deep. Each one has its own index (`_forloop_idx`,
`_forloop_idx2`, `_forloop_idx3`), and `break_loop()` / `continue_loop()` act
on the innermost:

```python
with rung(Enable):
    with forloop(Rows) as row:
        with forloop(Cols) as col:
            calc(Total + Grid[row.idx * 8 + col.idx], Total)
```

A nested loop is a body rung of the outer loop, so it can have contacts:
`with rung(Ready): with forloop(3): ...` inside the outer loop.
