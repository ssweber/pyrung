# Know Python? Learn Ladder Logic.

> A guided introduction to PLC programming for Python developers, using [pyrung](https://ssweber.github.io/pyrung/).

You know Python. You've never touched a PLC. This guide teaches you ladder logic, the dominant programming language of industrial automation, using tools you already have: Python, VS Code, and pytest. No hardware. No proprietary software. No Windows VM.

pyrung won't let you cheat: if you try to write a `for` loop where a scan cycle belongs, it'll tell you. That's the point. You're learning a different way of thinking about programs, and the guardrails are there to keep you honest.

---

## How this guide works

Each lesson introduces one ladder logic concept, shows you the Python you'd instinctively reach for, then shows you the ladder logic way and *why* it works that way in a machine that controls physical things. Every lesson ends with an exercise you can run and test.

**Prerequisites:** Python 3.11+, basic pytest knowledge, a text editor.

```bash
pip install pyrung
```

## Lessons

1. [The Scan Cycle](scan-cycle.md) -- How PLCs execute: always running, every rung, every scan.
2. [Tags](tags.md) -- Typed, sized, mapped to memory. Not just variables.
3. [Latch and Reset](latch-reset.md) -- Sticky outputs for momentary inputs.
4. [Assignment](assignment.md) -- Moving data with `copy` and `calc`.
5. [Timers](timers.md) -- Non-blocking time accumulation across scans.
6. [Counters](counters.md) -- Edge-triggered counting without loops.
7. [State Machines](state-machines.md) -- Timer-driven transitions without `while` or `sleep`.
8. [Branches and OR Logic](branches.md) -- Parallel paths and combined conditions.
9. [Structured Tags and Blocks](structured-tags.md) -- UDTs, arrays, and contiguous memory.
10. [Testing Like You Mean It](testing.md) -- pytest, forces, history, and the debugger.
11. [From Simulation to Hardware](hardware.md) -- Modbus, Click PLC export, CircuitPython deployment.

---

*Built with [pyrung](https://github.com/ssweber/pyrung). Write ladder logic in Python, simulate it, test it, deploy it.*
