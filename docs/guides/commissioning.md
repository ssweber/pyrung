# The Commissioning Workflow

You wrote logic and tested it. Now close the loop: declare what your program talks to, prove it's correct, and commission it with confidence.

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

`physical=` describes timing — how long the real device takes to respond. `link=` tells the harness which command drives this feedback. Tag flags (`public`, `external`, `readonly`, `final`) declare intent and are enforced by static validators. Standalone tags can use `link=` too — useful for process-level physics like "diverter fires → box arrives at sensor."

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

Static validators run at build time via `logic.validate()` — conflicting outputs, stuck bits, readonly writes, choices violations, and physical realism checks.

See [Analysis](analysis.md) for the full guide.

## Verify: prove it holds

Analysis answers questions about recorded history. Verification answers a different question: does a property hold across **every** reachable state?

```python
from pyrung.core.analysis import prove, Proven

result = prove(logic, Or(~Running, EstopOK))
assert isinstance(result, Proven)
```

`prove()` exhaustively explores every reachable state via BFS. Pair it with `harness.couplings()` for automated fault coverage — batch all conditions into a single `prove()` call to share work across properties:

```python
couplings = list(harness.couplings())
conditions = [
    Or(~plc.tags[c.en_name], plc.tags[c.fb_name], AlarmExtent != 0)
    for c in couplings
]
results = prove(logic, conditions)
```

Lock files capture reachable behavior as a committed artifact. `pyrung lock` writes it; `pyrung check` diffs against it in CI. Behavioral changes show up in PRs.

See [Verification](verification.md) for the full guide: condition syntax, result types, fault coverage workflows, lock file configuration, and CLI reference.

## Commission: run against hardware

With physical annotations in place, the VS Code debugger auto-installs the harness. Step through scans, force tags, and watch values live.

- **[VS Code Debugger](dap-vscode.md)** — breakpoints, monitors, Data View, Graph View, history scrubber with Chain tab for causal queries.
- **[pyrung live](dap-vscode.md#pyrung-live)** — attach to a running debug session from another terminal. Chain commands, force tags, run causal queries. Pair VS Code (human stepping through scans) with `pyrung live` (an LLM or script running causal queries and forcing tags) for assisted commissioning.
- **[Session capture](dap-vscode.md#session-capture)** — record a debug session as a replayable transcript. Condense to a minimal reproducer, mine invariants, generate pytest files.

For hardware deployment, see [Click PLC](../dialects/click.md) (TagMap, validation, soft-PLC) or [CircuitPython](../dialects/circuitpy.md) (P1AM code generation).

## Where to go from here

- [Physical Annotations](physical-harness.md) — declare device behavior, autoharness
- [Analysis](analysis.md) — dataview, cause/effect, coverage queries, static validators
- [Verification](verification.md) — prove(), fault coverage, lock files
- [Testing](testing.md) — pytest patterns, forces, bounds checking
- [VS Code Debugger](dap-vscode.md) — step through scans live
