# Know Python? Learn Ladder Logic.

> A guided introduction to PLC programming for Python developers, using [pyrung](https://ssweber.github.io/pyrung/).

You know Python. You've never touched a PLC. This guide teaches you ladder logic, the dominant programming language of industrial automation, using tools you already have: Python, VS Code, and pytest. No hardware. No proprietary software. No Windows VM.

pyrung won't let you cheat: if you try to write a `for` loop where a scan cycle belongs, it'll tell you. That's the point. You're learning a different way of thinking about programs, and the guardrails are there to keep you honest.

---

## What you're building

Every lesson adds a feature to the same project: a **conveyor sorting station**. Boxes arrive on a belt, get measured, and a diverter gate routes them to the correct bin. By the end, you'll have a system with start/stop/e-stop, auto and manual modes, a state-driven sorting sequence, structured tags for the equipment, a full test suite, and a path to real hardware.

Each lesson introduces one ladder logic concept, shows you the Python you'd instinctively reach for, then shows you the ladder logic way and *why* it works that way in a machine that controls physical things. Every lesson ends with an exercise you can run and test.

**Prerequisites:** Python 3.11+, basic pytest knowledge, a text editor.

```bash
pip install pyrung
```

## Lessons

1. [The Scan Cycle](scan-cycle.md) -- A button runs the conveyor motor.
2. [Tags](tags.md) -- Speed setpoint and an over-speed alarm.
3. [Latch and Reset](latch-reset.md) -- Start, stop, and emergency stop.
4. [Assignment](assignment.md) -- Record box sizes and keep a running total.
5. [Timers](timers.md) -- Hold the diverter gate open for 2 seconds.
6. [Counters](counters.md) -- Count boxes into each bin.
7. [State Machines](state-machines.md) -- The full sorting sequence.
8. [Branches and OR Logic](branches.md) -- Auto and manual modes.
9. [Structured Tags and Blocks](structured-tags.md) -- A Bin UDT and a sort log.
10. [Testing Like You Mean It](testing.md) -- A pytest suite for the whole system.
11. [From Simulation to Hardware](hardware.md) -- Map to a Click PLC or P1AM-200.

---

*Built with [pyrung](https://github.com/ssweber/pyrung). Write ladder logic in Python, simulate it, test it, deploy it.*
