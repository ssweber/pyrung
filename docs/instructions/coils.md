# Coils

For an introduction to the DSL vocabulary, see [Core Concepts](../getting-started/concepts.md).

## `out` — energize output

```python
with rung(Button):
    out(Light)      # Light = True while rung is True; False when rung is False

with rung(Button):
    out(Light, oneshot=True)   # True for ONE scan on rung rising edge, then False
```

`out` follows rung power. It is True when the rung is True and False when the rung is False. The last rung to write a tag wins within a scan.

With `oneshot=True`, the output is True for only one scan on the rung's rising edge.

## Out coil variants

CLICK v4's Out coil has four output types. The other three own one or two timers each, given as `(timer, preset)` in `on_delay` order, with one `unit` for the coil (`"ms"` by default).

### `toggle` — flip on each press

```python
with rung(PushButton):
    toggle(Light)   # each OFF->ON of the rung inverts Light; it holds while the rung is false
```

`toggle` takes `immediate(...)` like `out`. A rung that is already true on the first scan counts as a rising edge.

### `debounce` — follow the rung once it holds steady

```python
with rung(LevelSwitch):
    debounce(TankFull, on=(Timer[1], 500), off=(Timer[2], 200))
```

`TankFull` turns on after the rung has been true for 500 ms and off after it has been false for 200 ms. A shorter blip changes nothing. The off side takes one scan longer than an `off_delay` with the same preset. CLICK requires both sides.

### `flasher` — blink while the rung is true

```python
with rung(Alarm):
    flasher(Beacon, on=(Timer[3], 500), off=(Timer[4], 500))
```

The on phase starts when the rung goes true. When a phase reaches its preset, the other phase starts. The rung going false turns `Beacon` off, and the next true rung starts again with an on phase. CLICK's editor refuses a constant preset of 0 or below.

### `timed` — on for a set time

```python
with rung(CallButton):
    timed(Horn, Timer[5], 1000)                        # exactly 1 s, even if the button is held
    timed(Lamp, Timer[6], 3, "sec", maintained=True)   # at least 3 s, then follows the rung
```

A rung edge starts the on time. The on time runs out whatever the rung does, and an edge during it does not restart it. Without `maintained=True`, the next pulse needs the rung to go false and true again.

These coils never turn on their timer's `Done` bit, so a contact on `Timer[3].Done` won't tell you a `flasher` or `timed` coil has finished. Read the coil itself, or compare the `Acc`. Only `toggle` takes `immediate(...)`. Don't reuse a coil's timer elsewhere. An `on_delay` on the same timer is a conflicting-output finding, as it is in CLICK.

## `latch` — set and hold (SET)

```python
with rung(Start):
    latch(Motor)    # Motor becomes True and stays True until reset
```

## `reset` — clear latch (RESET)

```python
with rung(Stop):
    reset(Motor)    # Motor becomes False
```

## Immediate I/O

For `InputTag` / `OutputTag` elements (from `InputBlock` / `OutputBlock`), `.immediate` marks a contact or coil as immediate in the CLICK export:

```python
with rung(SensorA.immediate):
    out(ValveB.immediate)
```

pyrung's simulation and the generated CircuitPython code treat it as a plain tag.
