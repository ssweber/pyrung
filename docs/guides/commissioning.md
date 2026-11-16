# The Commissioning Workflow

You wrote logic and tested it. Now close the loop. Declare what your program talks to, test every fault path, and commission it.

pyrung's commissioning workflow has four stages. Each builds on the last.

## Declare: tell pyrung about the real world

Annotate your UDT fields with `physical=` to describe how feedback behaves, and `link=` to connect it to the command that drives it. The autoharness reads these annotations and synthesizes feedback patches in tests automatically.

```python
@udt()
class ConveyorIO:
    Motor: Bool = Field(public=True)
    MotorFb: Bool = Field(
        external=True,
        physical=Physical("MotorFb", on_delay="500ms", off_delay="200ms"),
        link="Motor",
    )
```

`physical=` describes timing, which is how long the real device takes to respond. `link=` tells the harness which command drives this feedback. Tag flags (`public`, `external`, `readonly`, `final`) declare intent and are enforced by ladder lints. Standalone tags can use `link=` too. This is useful for process-level physics like "diverter fires → box arrives at sensor."

See [Physical Annotations and Autoharness](physical-harness.md) for the full guide: standalone tags, value triggers, profile functions, validation, and forces override behavior.

## Analyze: inspect the program

Three layers of static and dynamic analysis, all usable from plain pytest:

```python
with PLC(logic) as plc:
    dv = plc.dataview
    dv.inputs()                   # what the program reads
    dv.upstream("MotorOut")       # everything that feeds MotorOut

    chain = plc.cause(Running)    # why did Running turn on?
    plc.query.cold_rungs()        # which rungs never fired?
```

- **`plc.dataview`** — chainable queries over the program's dependency graph. Filter by role, name, or upstream/downstream slice.
- **`plc.cause()` / `plc.effect()`** — causal chain analysis. What caused a transition, what it caused downstream, and what-if projections with `assume=`.
- **`plc.query`** — test coverage surveys. Cold rungs, hot rungs, stranded bits with blocker diagnostics. Merge across a test suite with the pytest plugin.

[Ladder lints](ladder-lints.md) run when you call `logic.check()`. By default they cover contradictory rungs, conflicting outputs, readonly writes and suspicious comparisons. The stuck-coil checks (`COIL_STUCK_*`) and the `PHYS_*` checks are opt-in (`extend_select=`).

See [Analysis](analysis.md) for the full guide — program structure, diagnosis, cause/effect, and test coverage.

## Test: check every fault path

```python
for coupling in harness.couplings():
    plc2 = PLC(logic, dt=0.001)
    Harness(plc2).install()
    plc2.force(coupling.en_name, True)
    plc2.run_for(1.5)
    plc2.force(coupling.fb_name, False)
    plc2.run_for(6.0)
    with plc2:
        assert AlarmExtent.value != 0, f"{coupling.fb_name}: no alarm in time"
```

`harness.couplings()` lists every device the harness drives. The loop holds each device's feedback off and checks that an alarm trips in time. See [Fault coverage](physical-harness.md#fault-coverage) and the [Testing Guide](testing.md).

## Commission: run against hardware

With physical annotations in place, the VS Code debugger auto-installs the harness. Step through scans, force tags, and watch values live.

- **[VS Code Debugger](dap-vscode.md)** — breakpoints, monitors, Data View, Graph View, history scrubber with Chain tab for causal queries.
- **[pyrung live](console.md#pyrung-live)** — attach to a running debug session from another terminal. Chain commands, force tags, run causal queries. Pair VS Code (human stepping through scans) with `pyrung live` (an LLM or script running causal queries and forcing tags) for assisted commissioning.
- **[Session capture](console.md#session-capture)** — record a debug session as a transcript that `replay` runs again.

For hardware deployment, see [CLICK PLC](../dialects/click.md) (TagMap, validation, soft-PLC) or [CircuitPython](../dialects/circuitpy.md) (P1AM code generation).

## Where to go from here

- [Physical Annotations](physical-harness.md) — declare device behavior, autoharness
- [Analysis](analysis.md) — program structure, diagnosis, cause/effect, test coverage
- [Testing](testing.md) — pytest patterns, forces, bounds checking
- [VS Code Debugger](dap-vscode.md) — step through scans live
