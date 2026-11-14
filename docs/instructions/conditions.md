# Conditions

For an introduction to the DSL vocabulary, see [Core Concepts](../getting-started/concepts.md).

These forms go inside `rung(...)`. All of them can be mixed freely.

```
Fault                          tag is truthy
~Fault                         tag is falsy
MotorTemp > 100                comparison  (==  !=  <  <=  >  >=)
Fault, Pump                    comma = implicit AND
Fault, MotorTemp > 100         implicit AND with comparison
And(Fault, Pump, Valve)        explicit AND (same as commas)
Or(Low, High, Emergency)       explicit OR
Or(Start, And(Auto, Ready))    nested AND inside OR
```

## Normally open (examine-on)

```python
with rung(Button):          # True when Button is True
    out(Light)
```

## Normally closed (examine-off)

```python
with rung(~Button):      # True when Button is False
    out(FaultLight)
```

## Rising and falling edge

```python
with rung(rise(Button)):    # True for ONE scan on False→True transition
    latch(Motor)

with rung(fall(Button)):    # True for ONE scan on True→False transition
    reset(Motor)
```

As on a CLICK, each `rise()` or `fall()` contact keeps its own memory of the bit. The memory updates every scan the contact is reached, whether the rung has power or not. It starts as off at RUN, so:

- A bit already on at the first scan rises on that scan.
- Two `rise(Button)` contacts each see the edge, wherever they sit.
- An edge that happens while the rung is off is spent, not saved for later.

## Multiple conditions (AND)

```python
# Comma syntax — all must be True
with rung(Button, ~Fault, AutoMode):
    out(Motor)

# And() — explicit AND
with rung(And(Button, ~Fault, AutoMode)):
    out(Motor)
```

## OR conditions

```python
# Or() — at least one must be True
with rung(Or(Start, RemoteStart)):
    latch(Motor)
```

## Nested AND/OR

```python
with rung(Or(Start, And(AutoMode, Ready), RemoteStart)):
    latch(Motor)
```

## Comparisons

```python
with rung(Step == 0):
    out(InitDone)

with rung(Temperature >= 100.0):
    latch(OverTempFault)

with rung(Counter != 5):
    out(NotAtTarget)
```

A TXT compared with a literal of more than one character compares the TXTs that follow it too, as a CLICK does. For example, `Txt[1] == "AB"` is true when TXT1 is "A" and TXT2 is "B".

A comparison may read a block element addressed at scan time, through a pointer or an index expression:

```python
with rung(DS[Step] == 0):
    out(StepEmpty)

with rung(Setpoint < DS[Step + 1]):
    out(NextIsHigher)
```

An address outside the block, or in a gap of a sparse block, reads as its type's zero value, and no fault flag is set. `copy()` and `math()` treat a source below the block differently. They set `system.fault.address_error` and write nothing (see [`copy`](copy.md)). A CLICK takes a pointer only in Copy, and the CLICK validator flags one in a contact (`CLK_PTR_CONTEXT_ONLY_COPY`).

## INT truthiness

INT tags are True when non-zero:

```python
with rung(Step):                    # True if Step != 0
    out(StepActive)

with rung(Or(Step, AlarmCode)):
    out(AnyActive)
```

## Inline expressions

```python
with rung((PressureA + PressureB) > 100):
    latch(HighPressureFault)
```

Inline expressions work in simulation. The CLICK dialect validator will flag them if targeting CLICK hardware. Rewrite them as `math()` instructions instead.

An inline expression is permissive. An operation that hits a math error (a division by zero, a math function outside its domain) gives 0 and sets no fault flag. See [Math outside `math()`](math.md#math-outside-math).
