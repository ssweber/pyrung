# Conditions

For an introduction to the DSL vocabulary, see [Core Concepts](../getting-started/concepts.md).

Everything that goes inside `Rung(...)`. All forms can be mixed freely.

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
with Rung(Button):          # True when Button is True
    out(Light)
```

## Normally closed (examine-off)

```python
with Rung(~Button):      # True when Button is False
    out(FaultLight)
```

## Rising and falling edge

```python
with Rung(rise(Button)):    # True for ONE scan on False→True transition
    latch(Motor)

with Rung(fall(Button)):    # True for ONE scan on True→False transition
    reset(Motor)
```

## Multiple conditions (AND)

```python
# Comma syntax — all must be True
with Rung(Button, ~Fault, AutoMode):
    out(Motor)

# And() — explicit AND
with Rung(And(Button, ~Fault, AutoMode)):
    out(Motor)
```

## OR conditions

```python
# Or() — at least one must be True
with Rung(Or(Start, RemoteStart)):
    latch(Motor)
```

## Nested AND/OR

```python
with Rung(Or(Start, And(AutoMode, Ready), RemoteStart)):
    latch(Motor)
```

## Comparisons

```python
with Rung(Step == 0):
    out(InitDone)

with Rung(Temperature >= 100.0):
    latch(OverTempFault)

with Rung(Counter != 5):
    out(NotAtTarget)
```

## INT truthiness

INT tags are True when non-zero:

```python
with Rung(Step):                    # True if Step != 0
    out(StepActive)

with Rung(Or(Step, AlarmCode)):
    out(AnyActive)
```

## Inline expressions

```python
with Rung((PressureA + PressureB) > 100):
    latch(HighPressureFault)
```

Inline expressions work in simulation. The Click dialect validator will flag them if targeting Click hardware — rewrite as `calc()` instructions instead.
