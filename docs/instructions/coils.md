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

CLICK v4's Out coil has four output types. The three besides `toggle` each use one or two timers. You give each timer as `(timer, preset)`, in the same form as `on_delay`. The coil has one `unit` for all its timers (`"ms"` by default).

### `toggle` — flip on each press

```python
with rung(PushButton):
    toggle(Light)   # each OFF->ON of the rung inverts Light; it holds while the rung is false
```

`toggle` accepts `immediate(...)`, like `out`. A rung that is already true on the first scan counts as a rising edge.

### `debounce` — follow the rung once it holds steady

```python
with rung(LevelSwitch):
    debounce(TankFull, on=(Timer[1], 500), off=(Timer[2], 200))
```

`TankFull` turns on after the rung has been true for 500 ms. It turns off after the rung has been false for 200 ms. A shorter blip changes nothing. The off side takes one scan longer than an `off_delay` with the same preset. CLICK requires both sides.

### `flasher` — blink while the rung is true

```python
with rung(Alarm):
    flasher(Beacon, on=(Timer[3], 500), off=(Timer[4], 500))
```

The on phase starts when the rung goes true. When a phase reaches its preset, the other phase starts. When the rung goes false, `Beacon` turns off. The next time the rung is true, the coil starts again with an on phase. CLICK's editor rejects a constant preset of 0 or below. A register preset of 0 or below turns `Beacon` off and freezes both `Acc` values.

### `timed` — on for a set time

```python
with rung(CallButton):
    timed(Horn, Timer[5], 1000)                        # exactly 1 s, even if the button is held
    timed(Lamp, Timer[6], 3, "sec", maintained=True)   # at least 3 s, then follows the rung
```

A rung edge starts the on time. The on time runs out whatever the rung does. An edge during the on time does not restart it. Without `maintained=True`, the rung must go false and true again before the next pulse. A preset of 0 or below gives no pulse.

These coils never turn on their timer's `Done` bit. A contact on `Timer[3].Done` therefore does not tell you that a `flasher` or `timed` coil has finished. Read the coil itself, or compare the `Acc`. Only `toggle` accepts `immediate(...)`.

Do not reuse a coil's timer elsewhere. An `on_delay` on the same timer is a conflicting-output finding, as it is in CLICK.

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
