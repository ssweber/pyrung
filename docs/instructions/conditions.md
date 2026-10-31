# Conditions

For an introduction to the DSL vocabulary, see [Core Concepts](../getting-started/concepts.md).

Everything that goes inside `rung(...)`. All forms can be mixed freely.

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

As on a CLICK, each `rise()` or `fall()` contact keeps its own memory: the bit as that contact last saw it. The memory is updated every time the contact's row is scanned, whether the rung has power or not, and starts off at RUN (and again on STOP→RUN). So:

- A bit already on at the first scan rises on that scan.
- Two `rise(Button)` contacts each see the edge, wherever they sit.
- A `rise()` above the rung that writes its bit sees the write one scan later, and still sees it once.
- A contact in a rung whose other contacts are open keeps watching: an edge that happens while the rung is off is spent, not saved for later.
- Inside a `forloop()` body, a contact compares with the bit at the previous iteration; in a subroutine called twice a scan, with the bit at the previous call.
- A contact the scan skips (a subroutine not called, a loop that does not run, rungs passed over by `return_early()`) keeps its memory until it is scanned again.

The memory lives in `SystemState.memory` as `_edge:e1`, `_edge:e2`, ... one key per contact.

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

Inline expressions work in simulation. The CLICK dialect validator will flag them if targeting CLICK hardware — rewrite as `calc()` instructions instead.

An inline expression is permissive: an operation that hits a math error (a division by zero, a math function outside its domain, a bitwise operation on Inf or NaN) gives 0 and sets no fault flag. See [Math outside `calc()`](math.md#math-outside-calc).
