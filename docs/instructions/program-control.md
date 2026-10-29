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

The count can be a positive literal or a tag, read once when the loop starts.
As on CLICK, a tag count is read as an unsigned 16-bit number: -1 runs 65535
iterations (a scan of a second or more on a CLICK), and 0 runs none. Whether
CLICK runs 0 or 65536 iterations for a count of 0 is not yet confirmed on
hardware; pyrung runs none (`pyrung.core.instruction.control.FOR_ZERO_COUNT_ITERATIONS`).

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
body does not run at all, as on CLICK: its `out()` coils keep their values, its
timers and counters neither time, count nor reset, and its one-shots do not
re-arm (only the For's own `oneshot=True` does). To turn a body coil off when
the loop stops, `reset()` it from a rung of its own.

`branch()` is not allowed inside a loop, and a body rung cannot hold another
`with rung()`.

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
