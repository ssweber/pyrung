# Lesson 7: State Machines

## The Python instinct

```python
state = "green"
while True:
    if state == "green":
        time.sleep(3)
        state = "yellow"
    elif state == "yellow":
        # ...
```

## The ladder logic way

State machines in ladder logic use a tag for the current state, timers for durations, and `copy` for transitions. No `while`, no `sleep`, no blocking.

```python
from pyrung import Char, Bool, Int, Program, Rung, Tms, on_delay, copy

State      = Char("State")
GreenDone  = Bool("GreenDone")
GreenAcc   = Int("GreenAcc")
YellowDone = Bool("YellowDone")
YellowAcc  = Int("YellowAcc")
RedDone    = Bool("RedDone")
RedAcc     = Int("RedAcc")

with Program() as logic:
    with Rung(State == "g"):
        on_delay(GreenDone, GreenAcc, preset=3000, unit=Tms)
    with Rung(GreenDone):
        copy("y", State)

    with Rung(State == "y"):
        on_delay(YellowDone, YellowAcc, preset=1000, unit=Tms)
    with Rung(YellowDone):
        copy("r", State)

    with Rung(State == "r"):
        on_delay(RedDone, RedAcc, preset=3000, unit=Tms)
    with Rung(RedDone):
        copy("g", State)
```

Each state has two rungs: one to run its timer, one to handle the transition. Clean, readable, testable.

## Exercise

Extend the traffic light to include a "walk request" button. When pressed during the green phase, the light should complete its current green time, go through yellow, then hold red for 5 seconds (instead of the normal 3) before returning to green. Test the normal cycle and the walk-request cycle.

---

> If you're a visual person, this is a good time to set up the [VS Code debugger](../guides/dap-vscode.md). From here on, the logic gets complex enough that stepping through scans and watching tags update live can be more useful than reading assertions.
