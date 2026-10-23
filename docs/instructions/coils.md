# Coils

For an introduction to the DSL vocabulary, see [Core Concepts](../getting-started/concepts.md).

## `out` — energize output

```python
with rung(Button):
    out(Light)      # Light = True while rung is True; False when rung is False

with rung(Button):
    out(Light, oneshot=True)   # True for ONE scan on rung rising edge, then False
```

`out` follows rung power: True when rung is True, False when False. Last rung to write a tag wins within a scan.

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

`TankFull` turns on after the rung has been true for 500 ms and off after it has been false for 200 ms; a shorter blip changes nothing. Each side is an on-delay timer on the rung (or on its negation): it restarts when the rung changes, and its `Acc` keeps counting past the preset while the rung holds, as `on_delay`'s does. CLICK requires both sides.

### `flasher` — blink while the rung is true

```python
with rung(Alarm):
    flasher(Beacon, on=(Timer[3], 500), off=(Timer[4], 500))
```

The on phase starts when the rung goes true. `Timer[3].Done` is set during the off phase and `Timer[4].Done` during the later on phases. The rung going false turns `Beacon` off and clears both timers, so the next start is a full on phase.

### `timed` — on for a set time

```python
with rung(CallButton):
    timed(Horn, Timer[5], 1000)                        # exactly 1 s, even if the button is held
    timed(Lamp, Timer[6], 3, "sec", maintained=True)   # at least 3 s, then follows the rung
```

A rung edge starts the on time, and the on time runs out whatever the rung does; an edge during it does not restart it. Not maintained, the next pulse needs the rung to go false and true again. `Done` is set when the on time is up and clears once the rung is false.

The first phase counts the rung's first scan, as `on_delay` does, and every phase shows for at least one scan, so an on time shorter than a scan still pulses once. None of the variants writes a Timer's `EN` or `TT`, and none takes `immediate(...)` except `toggle`. A timer used by a variant belongs to it: `on_delay` on the same timer is a conflicting-output finding, as CLICK reports it.

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

For `InputTag` / `OutputTag` elements (from `InputBlock` / `OutputBlock`), `.immediate` bypasses the scan-cycle image table:

```python
with rung(SensorA.immediate):
    out(ValveB.immediate)
```
