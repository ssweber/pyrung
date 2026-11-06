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

`TankFull` turns on after the rung has been true for 500 ms and off after it has been false for 200 ms; a shorter blip changes nothing. The on `Acc` restarts from 0 when the rung goes true, keeps counting past the preset while it holds, as `on_delay`'s does, and holds while the rung is false. The off `Acc` is 0 while the rung is true and counts while it is false. The off side takes a scan more than an `off_delay` of its preset: the coil drops on the scan after the off `Acc` reaches it. CLICK requires both sides.

### `flasher` — blink while the rung is true

```python
with rung(Alarm):
    flasher(Beacon, on=(Timer[3], 500), off=(Timer[4], 500))
```

The on phase starts when the rung goes true. Each phase runs until its own `Acc` reaches its preset, which starts the other phase from 0; the other phase's `Acc` holds meanwhile. The rung going false turns `Beacon` off and freezes both `Acc` values where they are; when the rung comes back, both restart from 0 with an on phase. CLICK's editor refuses a constant preset of 0 or below; a DS preset of 0 or below keeps `Beacon` off and both `Acc` values at 0 (measured for the off preset; for the on preset only the coil was read dark, and that no first on phase shows before the reset is pyrung's choice, not yet measured on a CLICK).

### `timed` — on for a set time

```python
with rung(CallButton):
    timed(Horn, Timer[5], 1000)                        # exactly 1 s, even if the button is held
    timed(Lamp, Timer[6], 3, "sec", maintained=True)   # at least 3 s, then follows the rung
```

A rung edge starts the on time, and the on time runs out whatever the rung does; an edge during it does not restart it. Not maintained, the next pulse needs the rung to go false and true again. The coil has timed out while its `Acc` holds at or past the preset, and the `Acc` keeps its value after the rung falls; the next edge starts it from 0. The timed-out state is read from `Acc >= preset` each scan, so lowering a register preset below a running `Acc` reads as timed out on the next scan.

An on preset of 0 or below gives no pulse at all (a `flasher`'s from a DS: the editor refuses a constant one). The first phase counts the rung's first scan, as `on_delay` does, so a `timed` or first `flasher` on time of a scan or less never shows (pyrung's rule; not yet measured on a CLICK). None of the variants writes a Timer's `Done`, `EN` or `TT` (a CLICK Out coil never turns `T<n>` on, so a contact on that `Done` is always off), and none takes `immediate(...)` except `toggle`. A timer used by a variant belongs to it: `on_delay` on the same timer is a conflicting-output finding, as CLICK reports it.

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
