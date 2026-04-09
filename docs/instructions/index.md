# Instruction Reference

For the scan model and DSL vocabulary, start with [Core Concepts](../getting-started/concepts.md). This section is the reference for writing rungs.

```python
with Rung(Start, ~Fault):
    out(MotorRunning)
    on_delay(RunDelay, preset=500)
```

Read it like a ladder diagram: conditions go on `Rung(...)`, instructions go in the body. Use the pages below by instruction family.

## Start here

- [Rungs](rungs.md) — comments, branching, source order, and how power flows through a rung
- [Conditions](conditions.md) — contacts, comparisons, AND/OR logic, and edge detection
- [Coils](coils.md) — `out`, `latch`, `reset`, and immediate I/O

## Data movement

- [Data Movement](copy.md) — `copy`, `blockcopy`, `fill`, converters, `pack_*`, and `unpack_*`
- [Math](math.md) — `calc()`, overflow behavior, division rules, and range sums

## Time and control

- [Timers & Counters](timers-counters.md) — `on_delay`, `off_delay`, `count_up`, and `count_down`
- [Drum, Shift & Search](drum-shift-search.md) — step sequencers, shift registers, and range search
- [Program Control](program-control.md) — `Program`, subroutines, `call`, and `forloop`

## Communication

- [Communication](communication.md) — `send`, `receive`, Modbus targets, and status tags

## Exact signatures

If you want signatures and parameters instead of examples, use the [Instruction Set API](../reference/api/instruction-set.md). For the rest of the generated API surface, start at the [API Reference](../reference/index.md).
