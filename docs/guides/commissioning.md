# Testing and Commissioning

pyrung started as a way to write and test ladder logic before touching hardware. This guide covers the tools that close the loop: declare what your program talks to, prove it's correct, and commission it with confidence.

## Declare: tell pyrung about the real world

Annotate your tags with physical metadata so validators and the autoharness know what they're working with.

**[Physical Annotations and Autoharness](physical-harness.md)** — `Physical` describes feedback characteristics (timing for bool sensors, named profiles for analog). `link=` connects a feedback field to the command that drives it. `min=`/`max=`/`uom=` declare valid operating ranges. The autoharness reads these annotations and synthesizes feedback patches in tests automatically. Tag flags (`readonly`, `external`, `final`, `public`) declare intent and are enforced by static validators. See [Tag Structures — Tag flags](tag-structures.md#tag-flags) for flag details.

## Analyze: prove it's correct

Three layers of analysis, each building on the last. All work in plain pytest — no VS Code required.

**[Analysis](analysis.md)** covers:

- **`plc.dataview`** — what tags exist, how they connect, what role they play. Chainable queries with role and dependency filters.
- **`plc.cause()` / `plc.effect()`** — what caused a transition, what it caused downstream, and what-if projections. Uses SP-tree attribution to classify proximate causes vs enabling conditions.
- **`plc.query`** — whole-program survey: cold rungs, hot rungs, stranded bits with blocker diagnostics.
- **Pytest coverage plugin** — merge `CoverageReport` across a test suite, gate CI on cold rungs or stranded bits.

**[Testing](testing.md)** covers deterministic testing patterns, forces as fixtures, and runtime bounds checking for `min`/`max`/`choices` violations.

Static validators run at build time via `logic.validate()` — conflicting outputs, stuck bits, readonly writes, choices violations, final-tag multiple writers, and physical realism checks. See [Analysis — Static validators](analysis.md#static-validators).

## Commission: run it against reality

**[Physical Harness](physical-harness.md#using-the-autoharness)** — the autoharness eliminates feedback toggling boilerplate. Declare the physics once on your UDT fields; tests and debug sessions both use the same declarations.

**[VS Code Debugger](dap-vscode.md)** — step through scans, set breakpoints, watch tags live. The debug console supports typed commands for stepping, forcing, causal queries, and monitoring. The [Data View](dap-vscode.md#data-view) panel watches and edits tags with live values. The [Graph View](dap-vscode.md#graph-view) shows the tag dependency graph interactively. The [History panel](dap-vscode.md#history) scrubs through retained scans with a Chain tab for interactive causal queries.

**[pyrung live](dap-vscode.md#pyrung-live)** — attach to a running debug session from another terminal or script. Chain commands with semicolons, force tags, run causal queries — everything the Debug Console can do, from the command line.

**[Session capture](dap-vscode.md#session-capture)** — record a debug session as a replayable transcript. Condense it to a minimal reproducer, mine invariants, and generate pytest test files from accepted findings.
