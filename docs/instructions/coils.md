# Coils

For an introduction to the DSL vocabulary, see [Core Concepts](../getting-started/concepts.md).

## `out` — energize output

```python
with Rung(Button):
    out(Light)      # Light = True while rung is True; False when rung is False

with Rung(Button):
    out(Light, oneshot=True)   # True for ONE scan on rung rising edge, then False
```

`out` follows rung power: True when rung is True, False when False. Last rung to write a tag wins within a scan.

With `oneshot=True`, the output is True for only one scan on the rung's rising edge.

## `latch` — set and hold (SET)

```python
with Rung(Start):
    latch(Motor)    # Motor becomes True and stays True until reset
```

## `reset` — clear latch (RESET)

```python
with Rung(Stop):
    reset(Motor)    # Motor becomes False
```

## Immediate I/O

For `InputTag` / `OutputTag` elements (from `InputBlock` / `OutputBlock`), `.immediate` bypasses the scan-cycle image table:

```python
with Rung(SensorA.immediate):
    out(ValveB.immediate)
```
