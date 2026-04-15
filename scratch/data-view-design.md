# Data View Design: Execution-Aware Tag Views

## Problem

Data View watched tags are ephemeral — gone when the session ends. The naive fix
is save/load config, but the deeper question is: the program already *knows*
which tags matter. Can the engine surface that knowledge automatically?

## What the engine already sees

- Rungs evaluate top-to-bottom, sequentially (write-after-read within a scan)
- ConditionTraceEngine records which tags were read to evaluate each rung condition
- Instructions have known write targets (statically knowable from rung structure)
- `runner.diff(scan_a, scan_b)` gives every tag that changed between scans
- tagGroups, tagHints, tagTypes already flow from Python → DAP → VS Code

**Gap:** The engine doesn't connect reads/writes into a graph today, but the
information is there.

## Core concept: influence, not just structure

Tags aren't equally important. They have roles based on how they participate
in execution flow:

```
with Rung(StartButton):
    out(RunMode)          # StartButton = INPUT (external origin)

with Rung(RunMode):
    on_delay(CycleTimer, preset=500)  # RunMode = PIVOT (gates logic)

with Rung(CycleTimer.Done):
    out(Conveyor)         # Conveyor = TERMINAL (endpoint)
    out(SealerOn)
```

## Static analysis: dependency graph at load time

Walk every rung. Extract read set (condition tags) and write set (instruction
outputs). Build a directed graph: tag → rung → tag.

| Role       | Signature                                                   |
|------------|-------------------------------------------------------------|
| **Input**    | Read in conditions, never written by instructions           |
| **Pivot**    | Written by some rungs, read as condition by others          |
| **Terminal** | Written by instructions, never read as condition            |
| **Isolated** | Written and read only within one rung                       |

Pivots are the highest-value debugger tags — they're where the program makes
decisions. A pivot that gates a subroutine call is a pivot for an entire
subgraph.

## Dynamic analysis: cascade tracing at runtime

Static fan-out = what *could* happen. Runtime tracking = what *did* happen.

Track read/write sets per rung during execution. Reconstruct cascade chains:

> Scan 47: StartButton True → rung 1 → RunMode True → rung 2 → CycleTimer
> starts → (3 scans later) CycleTimer.Done → rung 3 → Conveyor, SealerOn True

"Hits" = a tag that just participated in a cascade. Not just "value changed"
but "value changed and it mattered."

## Three Data View modes this enables

### 1. Cascade view (data flow debugger)
Click a tag → see upstream (what causes it) and downstream (what it affects).
During execution, active cascade paths light up as they fire.

### 2. Pivots view (auto-generated, no config needed)
Static analysis identifies pivotal tags. Auto-populate Data View grouped by
subroutine. The program's structure *is* the view.

### 3. Heat map / hits view (dynamic)
Tags accumulate "heat" based on downstream changes caused. Tags causing
cascades right now float to the top. "Flurry of events" detector.

## Why ladder logic makes this feasible

Ladder logic's control flow graph IS the data flow graph. Every `Rung(condition)`
is an explicit edge declaration. The engine doesn't need to infer the dependency
graph — it's literally the program structure. This kind of analysis would require
sophisticated program analysis in other languages; here it falls out naturally.

## Prior art & established models

What we're describing has deep roots in program analysis research. Ladder logic
makes all of these dramatically simpler than in general-purpose languages (no
pointers, no aliasing, no dynamic dispatch, no recursion — just a flat scan
with explicit read/write sets).

### Program Slicing (Weiser, 1981)

The foundational concept. A **slice** is the subset of program statements that
can affect the value of a variable at a given point.

- **Backward slice**: "what could have caused this value?" — trace upstream.
  Exactly our "what causes this tag to change?"
- **Forward slice**: "what does this value affect?" — trace downstream.
  Exactly our "what happens when this tag changes?"

In pyrung terms: backward slice of `Conveyor` = {RunMode, StartButton, CycleTimer}.
Forward slice of `StartButton` = {RunMode, CycleTimer, Conveyor, SealerOn}.

The "cascade view" is literally a forward slice visualization.

Ref: Weiser, "Program Slicing" (1981)
     https://cse.msu.edu/~cse870/Public/Homework/SS2003/HW5/p439-weiser.pdf

### Program Dependence Graph / PDG (Ferrante, Ottenstein, Warren, 1987)

The standard data structure for slicing. A directed graph where:
- Nodes = program statements (for us: rungs/instructions)
- Edges = dependencies, two kinds:
  - **Data dependence**: statement A defines variable X, statement B uses X
  - **Control dependence**: statement A controls whether B executes

In ladder logic, these collapse into one thing. A rung's condition (control) IS
its data dependency — `Rung(RunMode)` means "rung is control-dependent on
RunMode" AND "rung data-depends on RunMode." This is why ladder logic is
uniquely suited to this analysis: the PDG is trivial to construct.

Our input/pivot/terminal taxonomy falls out of PDG node properties:
- Input = source node (in-degree 0 in data deps)
- Terminal = sink node (out-degree 0 in data deps)
- Pivot = high out-degree interior node

Ref: Ferrante et al., "The Program Dependence Graph and Its Use in Optimization"
     https://www.cs.utexas.edu/~pingali/CS395T/2009fa/papers/ferrante87.pdf

### Dynamic Program Slicing (Agrawal & Horgan, 1990)

Static slicing answers "what COULD affect X?" Dynamic slicing answers "what
DID affect X in THIS execution?" — only the statements that actually executed
and contributed to the observed value.

This is the difference between our static analysis (load-time dependency graph)
and our runtime cascade tracing. A dynamic slice for a specific scan would show
exactly the chain of rungs that fired and the tags that changed, pruning away
branches that were disabled.

Dynamic slices are more precise but execution-specific — perfect for a debugger
that wants to show "here's the cascade that just happened."

Ref: Agrawal & Horgan, "Dynamic Program Slicing" (1990)
     https://www.cs.purdue.edu/homes/xyzhang/fall07/Papers/p246-agrawal.pdf

### Taint Analysis / Information Flow Tracking

Runtime technique that tracks propagation of metadata "tags" (taint labels)
through program execution. A taint source marks data, and the system traces
how that taint flows through operations to taint sinks.

Directly analogous to: "StartButton changed (taint source) → trace which tags
it contaminates through the scan (taint propagation) → these outputs changed
(taint sinks)." The "heat" concept in our heat map view is essentially a taint
count — how much influence has flowed through this variable.

In general-purpose languages, taint tracking has 30-50x overhead due to shadow
memory. In pyrung, the overhead is near-zero because we already have the
immutable state diff — we just need to attribute causality.

### PLC-Specific: Ladder Diagram Slicing & Petri Net Verification

There IS direct research on applying program slicing to ladder diagrams:

- **Static slicing for PLC programs** uses ladder-to-graph transformation and
  then standard slicing algorithms. Research confirms that "data dependence
  almost represents control dependence" in ladder diagrams — validating our
  observation that the PDG is trivially constructible.

- **Time Petri Nets** are used to model ladder logic for formal verification
  and model checking. This is the heavier formal methods approach — proving
  properties about the program rather than observing execution.

Ref: "Static slicing for PLC program with ladder transformation"
     https://www.researchgate.net/publication/251951624
Ref: "Ladder Metamodeling & PLC Program Validation through Time Petri Nets"
     https://hal.science/hal-00369887v1/document

### Relevance to pyrung

The key insight from this research: **what we're designing is a dynamic forward
slicer with visualization.** That's the formal name for "click a tag, see what
it affects, watch the cascade light up at runtime."

The taxonomy maps cleanly:

| Our concept         | Formal model                              |
|---------------------|-------------------------------------------|
| Dependency graph    | Program Dependence Graph (PDG)            |
| Input/Pivot/Terminal| Source/Interior/Sink nodes in PDG         |
| Cascade view        | Forward slice visualization               |
| "What caused this?" | Backward slice                            |
| Runtime cascade     | Dynamic forward slice                     |
| Heat/hits           | Taint propagation count                   |
| Fan-out             | Out-degree in data dependence subgraph    |

And ladder logic makes all of it dramatically cheaper to compute than in
general-purpose languages, because the program is already a flat dependency
declaration.

## Open questions

- Is this annotations on the existing Data View, or a new panel?
- How much static analysis at load time vs. dynamic tracking at runtime?
- How does the subroutine boundary interact with cascade/slice boundaries?
- Does the cascade view compose with the existing tagGroups/tagHints metadata?
- Save/load of manual watch lists is still useful alongside auto views — what's
  the interaction model?
- Should slicing be a core engine feature or a debug-only analysis layer?
- How to handle timer/counter accumulator tags (they change every scan but
  their .Done transition is the real event)?
