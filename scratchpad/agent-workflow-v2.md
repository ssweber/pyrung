# Agent Workflow — Skills, Scenarios, and ClickNick Integration

## Overview

ClickNick generates a workspace directory that a coding agent (Claude Code, etc.) can use to interact with a pyrung program. The agent gets access to diagnosis, simulation, verification, and code generation — all grounded in the engineer's actual program. The engineer talks; the agent reasons formally and produces verified, paste-ready output.

The key insight: the agent doesn't need the Click GUI. pyrung IS the program. The agent works against it directly, and ClickNick handles the bridge back to Click Programming Software.

### Two workflows

**Click-first** — Click Programming Software is the source of truth. ClickNick generates the pyrung project from the .ckp project's Scr*.tmp files. When the engineer saves in Click, the project auto-regenerates (ScrWatcher). Annotations live in ClickNick's own layer (the address editor), not in the Python source — they survive regeneration. The agent annotates via ClickNick Live CLI commands. This is the lower bar: the engineer stays in their familiar tool, never touches Python.

**pyrung-first** — pyrung is the source of truth. The engineer writes Python, tests with pytest, runs CI, uses `always()`/`never()` directly. Annotations live in the source. Click is a deployment target. This is the higher bar: full engineering workflow with version control, test suites, and lock files.

This document describes the Click-first workflow. The agent works through ClickNick's layer because that's where the data lives when Click is the source of truth. See [The pyrung Way](#the-pyrung-way--graduating-to-a-permanent-project) for the on-ramp from Click-first to pyrung-first.

---

## Implementation Status

### What's built and working

**ClickNick Live** (IPC bridge — fully implemented):
- TCP server embedded in the GUI, drains commands on Tk main thread
- Session discovery by `.ckp` filename (label file), stale port pruning
- `DispatchContext` rebuilt each drain cycle (store, analysis, tag resolver, annotation service)
- `get`/`set` commands with pyrung tag name resolution (falls back to Click display address)
- **Tag commands** (13 subcommands): flags, choices, range, UOM, physical devices, queries
- **Rung commands**: `list`, `preview` (with rung selection), `apply` (pyrung → ladder CSVs)
- All edits land as unsaved changes in the address editor — engineer reviews and saves

**AnalysisService** (pyrung project generation — fully implemented):
- Builds ProgramGraph from Scr*.tmp via `ladder_to_pyrung()` pipeline
- Persists `pyrung_project/` to disk on every ScrWatcher-triggered rebuild
- Output: `tags.py`, `main.py`, `subroutines/*.py`, `csv/`, `project_to_csv.py`, `.vscode/`
- Bidirectional `tag_to_addr_key` map for tag name ↔ addr_key resolution

**DAP / Simulation** (GUI-managed, pyrung-live for agent interaction):
- DapService manages pyrung DAP subprocess (pure Python, no tkinter)
- GUI toggle button starts/stops DAP (lifecycle in app.py, not dispatch layer)
- pyrung-live (in pyrung repo) provides out-of-process console attachment to DAP
- Agent talks to pyrung-live for simulation; clicknick-live for annotations/rung edits
- VS Code extension provides visual debugging for engineers who want to watch
- **Snapshot loading not yet in GUI** — pyrung supports `PLC(logic, initial_state=state)` and `TagMap.load_snapshot()`, but ClickNick doesn't expose a file selector for loading a tag dump before starting DAP

**pyrung** (core engine — fully implemented):
- Full DSL: tags, rungs, conditions, coils, timers, counters, math, data movement, comms
- `ladder_to_pyrung()` and `pyrung_to_ladder()` round-trip conversion
- `ladder_to_pyrung_project()` generates multi-file projects
- DAP server with breakpoints, stepping, force/patch, causal analysis, history
- State-space exploration, `always()`/`never()`, `why()`, `how()`, `cause()`, `effect()`
- Lock file system (`pyrung lock` / `pyrung check`) for CI verification
- Static click-cheatsheet in `docs/guides/click-cheatsheet.md`

### What's NOT built yet (next phase)

The workspace that ClickNick generates is missing the agent-facing artifacts:

| Artifact | Purpose | Status |
|----------|---------|--------|
| Snapshot file selector | Load tag dump from PLC before starting DAP | Not in GUI (pyrung API exists) |
| `CLAUDE.md` | Tells the agent what it has and how to use it | Not generated |
| `click-cheatsheet.md` | pyrung DSL reference for reading/writing code | Static doc only, not emitted into workspace |
| `.claude/skills/` | Structured skill definitions for agent workflows | Not generated |
| `.claude/settings.json` | Permissions and MCP tool config | Not generated |
| `tests/` | Pytest scaffold with smoke test and coverage | Not generated |

The project_emitter generates `tags.py`, `main.py`, `subroutines/`, `run.py`, `project_to_csv.py`, `.vscode/launch.json`, `pyproject.toml`, `README.md` — but none of the agent-facing files.

---

## The Escalation

The agent's tools form a gradient. Each level adds cost and capability. Most questions are answered in the first three levels.

### Level 0 — Read the cheatsheet

Before the agent can reason about any program, it needs the pyrung DSL vocabulary. `click-cheatsheet.md` is the Rosetta Stone — what `rung()`, `latch()`, `reset()`, `out()`, `And()`, `branch()` mean, how timers and counters work, what the memory banks are, and what the common patterns look like (state machines, EMA filters, shift logs).

The agent reads this once. Without it, every subsequent step is guessing at syntax.

### Level 1 — Read the code

The agent is an LLM. It can read `main.py`, `subroutines/*.py`, and `tags.py` and reason about the logic directly. For a 20-rung program, this might answer the question without calling any tool. Read the code first; reach for tools when the program is too large or the causal chain is too deep to trace mentally.

### Level 2 — Theorize with static tools

These tools work from the program structure and a state snapshot. No scans needed, no annotations needed, instant answers.

**`why(tag)`** — walks backward from a tag and explains how it reached its current value. Shows which contacts are blocking, which latches are held, which roots are contributing. Works in both directions (why is this ON? why is this OFF?). Multiple tags merge into one unified explanation. For diagnosis, the simulation must be loaded with a snapshot from the actual machine — without it, `why()` is explaining default state, not the fault.

**`simplified(tag)`** — resolves a terminal tag's condition chain back to inputs, eliminating intermediate pivots. A 14-rung interlock chain through 10 intermediate tags becomes a two-term Boolean expression over the 8 inputs that actually matter.

**`cause(tag, to=value)`** — projected mode. "What would it take to make this tag reach this value?" Returns the structural path without running any scans. Identifies blockers — inputs the test suite has never demonstrated.

**`effect(tag, from_=value)`** — projected mode. "What would happen if this tag changed?" What-if analysis without mutating state.

**`recovers(tag)`** — convenience predicate. "Can this latched bit clear?" Quick sweep across all fault tags.

**`assume={}`** — scenario pinning on projected queries. Override tag values during analysis without touching the actual state. Sweep scenarios: `for tag in fault_tags: plc.recovers(tag, assume={"ResetBtn": True})`.

**`dataview`** — chainable queries over the dependency graph. `inputs()`, `pivots()`, `terminals()`, `upstream(tag)`, `downstream(tag)`, `contains(name)`. Discover I/O, trace dependencies, slice the program.

### Level 3 — Simulate

Run actual scans to test hypotheses with temporal behavior.

**`patch`** is the default verb. It sets a tag's value once — the program takes over from there. Use it for simulating events: "operator pressed Start," "sensor detected a part," "HMI toggled auto mode." The program processes the patched value and runs normally from there.

```
pyrung live "patch HMI_on true; step 1; why fill_stepNumber"
```

**`force`** is for failure modes. It pins a tag's value — the program can't overwrite it, every scan. Use it for simulating stuck conditions: "sensor failed and stays failed," "relay welded closed," "communication link down."

```
pyrung live "force FlowSensor false; step 10; why FaultAlarm"
```

The verbs tell the agent (and the engineer reading the transcript) what kind of hypothesis is being tested — a transient event vs. a persistent failure.

**`cause(tag)` / `effect(tag)` recorded mode** — after running scans, these explain what actually happened. Recorded cause distinguishes proximate triggers from enabling conditions. Recorded effect traces forward with counterfactual pruning. Richer than `why()` because history distinguishes what changed from what was already true.

### Level 4 — Search

`explore()` builds the full reachable-state graph via BFS. Call once (expensive); query cheaply afterward. Works on any program — no annotations strictly required.

**`how(condition)`** — minimum sequence of external input changes to reach a target state. Turns a diagnosis into action: "how do I get this machine from FAULTED to RUNNING?" Returns step-by-step input changes with scan counts.

```
pyrung live "explore; how State == RUNNING"
```

`how()` earns its cost on big programs — 12-step state machines with 30 inputs where you can't trace the path by reading code. For a 4-rung motor start/stop, `why()` already told you the answer.

**How domains are resolved:** `explore()` auto-enables heuristic domain seeding. For tags with declared bounds (`min`/`max`/`choices`), the domain comes from those bounds. For tags with comparison-derived boundaries (`temp > 50.0`), the domain comes from the program's literal constants. For tag-vs-tag comparisons with no literal anchor (`Pressure > PressureSetpoint`), behavioral bisection discovers the comparison thresholds automatically. The result: programs with unbounded Real tags are explorable — the seeder finds the values that matter.

**Annotations improve quality, not access.** Declared `min`/`max`/`choices` give tighter, more meaningful domains. Without them, the seeder works but may produce arbitrary boundary values. The prover logs which tags were auto-promoted and which domains were heuristically seeded, so the agent can suggest annotations that would improve the results.

### Level 5 — Prove (requires gradual typing)

Exhaustively verify a property over all reachable states. The only tool that gives guarantees, not just tests. Unlike `explore()`, proofs require sound domains — tags without declared bounds or comparison-derived domains will produce `Intractable` with blocker hints identifying exactly which tags need constraints.

```
pyrung live "prove always not (OverTemp and not CoolingPump)"
pyrung live "prove never OverTemp ~CoolingPump"
```

Returns `Proven`, `Counterexample` (with replayable trace), or `Intractable` (with blocker hints).

**The proof obligation:** `always()`/`never()` every logic change before preparing output. The fix isn't done until the proof passes. The engineer is the decision-maker (is this the behavior I want?), not the verifier (is this code correct?). The prover is the verifier.

---

## Basic and Advanced

The escalation divides cleanly at the gradual typing boundary.

### Basic — no annotations needed

Levels 0–3. Everything the agent needs for diagnosis, explanation, and interactive simulation. Works out of the box on any program, regardless of tag metadata.

Tools: `why()`, `simplified()`, `cause()` (recorded and projected), `effect()` (recorded and projected), `recovers()`, `assume={}`, `dataview`, `upstream/downstream`, `patch`, `force`, `step`.

Coverage tools also fit here: `query.cold_rungs()`, `query.stranded_bits()`, `query.hot_rungs()`. These survey recorded scan history without needing annotations.

Handles: "my machine faulted," "why won't this start," "what happens if this sensor fails," "review this program," and most diagnostic questions.

### Exploration — works without annotations, improved with them

Level 4. `explore()` and `how()` work on any program thanks to heuristic domain seeding. Tags without declared bounds get domains via behavioral bisection (the seeder discovers comparison thresholds automatically). Annotations (`min`/`max`/`choices`) improve domain quality and make paths more interpretable, but aren't a hard gate.

Tools: `explore()`, `how()`, `reachable_states()`.

Handles: "how do I get from FAULTED to RUNNING?", "is this state reachable?", "what's the minimum input sequence?"

### Formal verification — requires gradual typing

Level 5. Proofs require sound, bounded domains. Tags without declared bounds or comparison-derived domains produce `Intractable` with blocker hints. The agent can use `explore()` first to understand the program, then guide the engineer through annotating the specific tags the prover needs.

Tools: `always()`, `never()`, `reachable_states()`, lock files, fault coverage via `harness.couplings()`.

Handles: "fix this so it can't happen again" (with proof), "prove this interlock is correct," "behavioral regression testing in CI."

The boundary is about what the verifier needs, not what the agent needs. The agent can propose fixes at the Basic level — simulate the bad scenario, confirm the fix blocks it. Exploration finds the path. Formal verification guarantees the fix is correct across *all* states, not just the ones the agent tested.

---

## Workspace Structure

### Current output (project_emitter)

```
pyrung_project/
├── tags.py                # Tag declarations + TagMap
├── main.py                # Program with main rungs + call() statements
├── subroutines/           # Individual subroutine files
│   ├── __init__.py
│   └── <name>.py
├── run.py                 # Instantiate PLC and step logic
├── project_to_csv.py      # Export back to Click CSV
├── csv/                   # Ladder CSVs (source of truth for regeneration)
│   ├── Scr0.csv
│   └── nicknames.csv
├── csv_output/            # Created by `rung apply` (pending changes)
├── .vscode/
│   ├── launch.json        # DAP launch configuration
│   └── extensions.json    # pyrung-debug recommendation
├── pyproject.toml         # Dependencies (pyrung >= version)
└── README.md              # Setup instructions
```

### Target output (next phase — pyrung emits these)

```
pyrung_project/
├── ... (existing files above)
├── CLAUDE.md              # Agent instructions: escalation, tools, workflows
├── click-cheatsheet.md    # pyrung DSL quick reference (step zero)
├── tests/
│   ├── conftest.py        # PLC fixture with coverage plugin
│   └── test_smoke.py      # Program loads and steps without error
└── .claude/
    ├── settings.json      # Permissions for clicknick-live, pyrung-live
    └── skills/
        ├── diagnose.md    # "My machine faulted" / "Why won't this start?"
        ├── fix.md         # "Fix this so it can't happen again"
        ├── review.md      # "Review / explain this program"
        └── failure.md     # "What happens if this sensor fails?"
```

---

## CLAUDE.md — what the agent needs to know

Generated by pyrung's project_emitter, tailored to the specific program.

```markdown
# Machine: [name from .ckp project]

## Before you start

Read `click-cheatsheet.md` — it's the pyrung DSL reference. You need it to
understand `tags.py`, `main.py`, and `subroutines/`. Read it before touching
any tool.

Then read the program:
- `tags.py` — tag declarations, types, metadata, TagMap
- `main.py` — main rungs and subroutine calls
- `subroutines/` — individual subroutine files

## Program shape

[Generated by introspecting the program]
- Main: N rungs, M subroutines
- Tags: X inputs, Y pivots, Z terminals
- Types: N Bool, M Int (K with choices), P Real
- Formal verification: [likely tractable / limited by N Real tags / limited by M unconstrained Int tags]

## Tools

Two CLI tools. Chain commands with `;` to avoid repeated process launches.

- **pyrung live** — simulation and analysis (patch, force, step, why, explore, how, prove)
- **clicknick-live** — push annotations and rung edits back to Click

## Diagnose — why is this happening?

`why()` explains how a tag reached its current value. For diagnosis to be
useful, the simulation must be loaded with a snapshot from the faulted machine.
If `why()` shows everything at defaults, ask the engineer to load a tag dump
(Data → Read Data from PLC → All → Save in Click, then select in ClickNick).

    pyrung live "why FaultAlarm"                         # what's blocking?
    pyrung live "why FaultAlarm MotorStall"              # unified across tags
    pyrung live "simplified ConveyorMotor"               # resolve to inputs

Discover what exists:

    pyrung live "dataview i:"                            # all inputs
    pyrung live "dataview fill"                          # tags matching 'fill'
    pyrung live "upstream ConveyorMotor"                 # what feeds this tag

## Test a hypothesis — patch, step, observe

`patch` sets a value once. The program runs normally from there.

    pyrung live "patch StartBtn true; step 1; why Running"

For failure modes, `force` pins the value across scans:

    pyrung live "force FlowSensor false; step 10; why FaultAlarm"

Each `why()` shows what remains. Iterate until you understand the causal chain.

## Projected queries — what-if without running scans

    pyrung live "cause Running to=false"                 # what would clear this?
    pyrung live "effect StartBtn from=false"             # what would toggling this do?
    pyrung live "recovers FaultLatch"                    # can this bit clear?

## Annotate tags (via clicknick-live)

Annotations constrain the state space for formal verification and survive
program regeneration:

    clicknick-live "tag set-choices StateCurrent IDLE:0 FILLING:1 DRAINING:2 FAULTED:3"
    clicknick-live "tag set-range FillLevel 0 1000"
    clicknick-live "tag show StateCurrent"

All edits land as unsaved changes — engineer reviews and saves in the address
editor. Batch annotations before asking the engineer to save (two sync points:
save in ClickNick, then save in Click to trigger regeneration).

## Explore and search — no annotations required

    pyrung live "explore; how fill_stepNumber == 5"
    pyrung live "explore; how State == RUNNING"

explore() auto-discovers domains for tags without bounds via heuristic seeding.
Annotations (min/max/choices) improve domain quality and path readability but
aren't required. Always try explore() before asking for annotations.

## Formal verification (requires annotations for soundness)

    pyrung live "prove always not (OverTemp and not CoolingPump)"
    pyrung live "prove never OverTemp ~CoolingPump"

always()/never() require bounded domains — tags without bounds will produce
Intractable with hints identifying exactly which tags need constraints.
Always prove after making logic changes.

## Read and edit program structure (via clicknick-live)

    clicknick-live rung list                  # summary per rung
    clicknick-live rung list init             # subroutine rungs
    clicknick-live rung preview --select r3   # before/after diff
    clicknick-live rung apply                 # convert edits to ladder CSVs

## Generate paste-ready output

Edit pyrung source (main.py, subroutines/) directly. Then:

1. `clicknick-live rung preview` — see what changed as a diff
2. `clicknick-live rung apply` — convert to ladder CSVs in csv_output/
3. Engineer reviews, applies in Click, saves
4. ScrWatcher detects save → auto-regenerates pyrung_project/
5. Re-prove against regenerated source (round-trip check)

## Reference

- `tags.py`, `main.py`, `subroutines/` — pyrung model of this machine's logic
- `click-cheatsheet.md` — pyrung DSL quick reference (read first)
- `pyrung live help` — full command list
```

---

## Scenarios

### 1. "My machine faulted"

**Trigger:** Engineer reports a fault, alarm, or unexpected state.

**Prerequisite:** A tag dump from the faulted machine must be loaded. If the simulation is running from defaults, `why()` has nothing to diagnose. Ask the engineer: "Did you load a snapshot from the PLC? (Data → Read Data from PLC → All → Save in Click, then select in ClickNick before starting DAP.)"

**Workflow:**
1. Read the relevant rungs in `main.py` / subroutines — understand the fault logic
2. `pyrung live "dataview t:"` — see what outputs/alarms are active
3. `pyrung live "why FaultAlarm"` — backward walk, what's causing it?
4. If multiple tags are alarming — `why FaultAlarm MotorStall` for a unified explanation
5. Explain the causal tree in plain English — name the specific tags and rungs
6. If engineer asks "how do I clear it?" — projected query first:
   `pyrung live "cause FaultAlarm to=false"` — what would clear it structurally?
7. If the projected answer isn't enough, patch-and-step loop:
   `pyrung live "patch EstopOK true; step 1; why FaultAlarm"` — see what remains
8. Iterate: patch the next blocker, step, why again, until the path is clear

**Agent should:** Start by reading the code and using `why()`. Use projected `cause(to=)` before falling back to interactive simulation. `how()` works without annotations — use it when the engineer wants a concrete recovery plan. Use `always()`/`never()` only when the engineer needs a formal guarantee and the program is annotated.

**Output:** Natural language explanation + specific tags/rungs to check on the machine.

### 2. "Why won't this start?" / "It's stuck at step 5"

**Trigger:** Machine is stuck mid-sequence, not faulted but not advancing.

**Prerequisite:** Same as scenario 1 — a tag dump from the stuck machine. Without it, `why()` can't see what state the sequence is actually in.

**Workflow:**
1. Read the sequence logic in the source
2. `pyrung live "dataview step"` or `dataview fill` — find the sequence tag
3. `pyrung live "simplified StepCurrent"` — what does advancement actually depend on?
4. `pyrung live "why StepCurrent"` — what's blocking advancement?
5. `pyrung live "cause StepCurrent to=6"` — what would it take to advance? (projected)
6. If the answer needs temporal verification: patch the blocking input, step, check
7. `why()` again on anything that didn't change as expected

**Output:** "ConveyorMotor is OFF because Running is blocked by EstopOK(False) on rung 2. Check the e-stop circuit."

### 3. "Fix this so it can't happen again"

**Trigger:** Engineer understands the fault and wants a logic change.

**Workflow:**
1. Discuss the fix — what permissive/interlock is missing?
2. Draft the new rung(s) by editing pyrung source (main.py or subroutines/)
3. Simulate — force the scenario that caused the fault, step through, confirm new behavior blocks it
4. `explore()` — confirm the bad state is no longer reachable via `how()`. Works without annotations.
5. If a formal guarantee is needed: `always()`/`never()` the bad state (may require annotations if Intractable)
6. `clicknick-live rung apply` to prepare ladder CSVs
7. Engineer reviews via `clicknick-live rung preview`, applies in Click, saves
8. ScrWatcher triggers regeneration — re-verify against the regenerated source

**Agent must:** Verify every logic change before preparing output. `explore()` + `how()` to confirm the fix works (no annotations needed). `always()`/`never()` when the program is tractable and a formal guarantee is needed. The engineer is the decision-maker (is this the behavior I want?), not the verifier.

**Output:** Verified rung edits + plain English description of what changed and why.

### 4. "Add a new feature"

**Trigger:** Engineer wants new logic (new sequence step, new alarm, new mode).

**Workflow:**
1. Understand requirements
2. Read the existing code to understand where the new logic fits
3. `pyrung live "dataview i:"` — what inputs are available? What tags are free?
4. Draft the new rungs in pyrung source
5. Simulate: patch inputs through the new scenarios, confirm expected behavior
6. `explore()` — confirm the new feature is reachable via `how()`. Prove relevant properties with `always()`/`never()` if tractable.
7. `clicknick-live rung apply` to prepare output

**Output:** Verified rung edits + simulation walkthrough or prove results.

### 5. "Review / explain this program"

**Trigger:** Engineer is unfamiliar with a program, or doing a handoff.

**Workflow:**
1. Read the pyrung source (main.py, subroutines/, tags.py) — understand the full program
2. `pyrung live "dataview i:; dataview t:"` — understand the I/O boundary
3. `pyrung live "upstream CriticalOutput"` — trace what drives key outputs
4. `pyrung live "simplified CriticalOutput"` — resolve pivot chains to readable expressions
5. Recognize common patterns from the cheatsheet (state machines, EMA filters, timer-driven sequences)
6. `pyrung live "query cold_rungs"` — which rungs have never fired in tests?
7. `pyrung live "query stranded_bits"` — which latched bits can't be cleared?
8. Identify gaps — alarms without coverage, interlocks that can be bypassed
9. Explain the logic in plain English — what each section does, what the sequence is, what the interlocks are

**Output:** Program narrative with I/O map, dependency analysis, pattern identification, and coverage gaps.

### 6. "What happens if this sensor fails?"

**Trigger:** Engineer wants to understand failure modes.

**Snapshot:** Useful if loaded — the failure simulation starts from the machine's actual operating state, so the cascade is realistic. Without a snapshot, the agent can still simulate from defaults, but the results may not reflect a mid-process failure.

**Workflow:**
1. Read the relevant logic to understand how the sensor is used
2. `pyrung live "dataview i:"` — find the sensor tag
3. `pyrung live "effect FlowSensor from=false"` — projected: what would failure cause? (no scans needed)
4. `pyrung live "force FlowSensor false; step 10; dataview alarm"` — simulate failure over time
5. `pyrung live "why FaultAlarm"` — explain the cascade after simulation
6. `pyrung live "effect FlowSensor"` — recorded: what did the failure actually trigger? (needs the scan history from step 4)
7. `explore()` — does `how()` still reach the alarm from this failure state? Confirms coverage without annotations.
8. If the engineer wants a formal guarantee: `always()`/`never()` that the alarm catches the failure across all states (may require annotations)

**Output:** "If FlowSensor goes FALSE while FillEnable is TRUE, the watchdog timer starts. After 5s, FlowAlarm latches."

---

## Reverse Path — Pasting Back to Click

The agent edits pyrung source files directly (main.py, subroutines/). The reverse path converts those edits back to Click's format.

### Rung-level workflow (via clicknick-live)

```
clicknick-live rung list                  # see program structure
# Agent edits pyrung source files...
clicknick-live rung preview --select r3   # diff showing what changed
clicknick-live rung apply                 # convert to ladder CSVs in csv_output/
```

The engineer reviews the diff, applies in Click (paste/import), and saves. ScrWatcher detects the save and auto-regenerates pyrung_project/ — the agent re-proves against the regenerated source as a round-trip check.

### Which approach to use

- Fix to one rung, adding a contact, modifying a condition → edit the rung in main.py, apply
- Adding a new section, new alarm logic → edit/add source files, apply
- Restructuring logic, new program, major refactor → full program paste via ClickNick export
- When in doubt, smaller changes are lower risk — the engineer sees and applies each change individually

---

## DAP Integration — Simulation

ClickNick manages the DAP subprocess lifecycle via a GUI toggle button. The engineer starts DAP when they want simulation available; the agent connects via pyrung-live.

### Architecture

```
                                        ┌──────────────┐
                                        │  Click PLC   │
                                        │  (hardware)  │
                                        │              │
                                        │  Read Data   │
                                        │  → CSV dump  │
                                        └──────┬───────┘
                                               │ tag dump
┌─────────────────────┐     ┌──────────────────▼┐     ┌──────────────┐
│  ClickNick GUI      │     │  pyrung DAP        │     │  Agent       │
│                     │     │  (subprocess)      │     │  (Claude)    │
│  [Load Snapshot]────┼────▶│  initial_state     │     │              │
│  [Start/Stop DAP]───┼────▶│  adapter.py        │◀────┼──pyrung live │
│  DapService manages │     │  breakpoints       │     │  console     │
│  subprocess lifecycle│     │  stepping          │     │              │
│                     │     │  force/patch        │     │              │
│  LiveServer─────────┼─────┼──clicknick-live────┼─────┼──annotations │
│  (tag/rung cmds)    │     │                    │     │  rung edits  │
└─────────────────────┘     └────────────────────┘     └──────────────┘
```

- **pyrung-live** — agent uses for simulation: step, patch, force, why, how, prove, cause, effect
- **clicknick-live** — agent uses for data: tag annotations, rung list/preview/apply, get/set fields
- **VS Code** — optional, engineer can watch the debug session visually

### Launch flow

1. Engineer opens project in ClickNick (connects to Click instance or opens .ckp)
2. ScrWatcher triggers AnalysisService → generates pyrung_project/ on disk
3. **If diagnosing a faulted machine:** engineer loads a tag dump (see below)
4. Engineer clicks Start DAP → DapService launches pyrung subprocess (with snapshot if provided)
5. Engineer points agent at the pyrung_project/ directory
6. Agent reads `click-cheatsheet.md`, then `CLAUDE.md`, then the program source — ready to go

The engineer doesn't configure anything beyond clicking Start. ClickNick sets up the workspace and the simulation. The agent discovers what's available from CLAUDE.md.

### Loading a snapshot

`why()` is only as useful as the state it's explaining. Without a tag dump, the simulation starts from defaults — everything false/zero — and `why()` just tells you nothing has happened yet.

For diagnosis (scenarios 1, 2, and 6), the engineer needs to load a snapshot from the faulted machine:

1. In Click Programming Software: **Data → Read Data from PLC → All → Save** → produces a CSV
2. In ClickNick: select the CSV in the file picker on the DAP panel (or drag-and-drop)
3. ClickNick passes it to the DAP subprocess as `initial_state` via `TagMap.load_snapshot()`
4. The agent's `why()` calls now reflect the actual machine state

**The file selector is optional.** If no snapshot is provided, DAP starts from defaults. This is fine for scenarios where the engineer is building up state through simulation (adding features, testing fixes). But for "my machine faulted" — the snapshot is what makes diagnosis possible.

For pyrung-first workflows, the equivalent is:

```python
state = mapping.load_snapshot("data.csv")
plc = PLC(logic, initial_state=state)
```

---

## ClickNick Live — Annotation Interface

ClickNick Live exposes structured CLI commands for annotating tags directly. The agent uses these instead of editing Python source — no syntax errors, no wrong files, no merge conflicts. Every command lands as an unsaved change in the ClickNick address editor.

**The agent can edit. It cannot save.** Saving is always the engineer's action. The unsaved-changes workflow already exists for human edits — ClickNick Live just writes into the same pending state via CLI. The agent proposes; the engineer reviews the diff and commits.

This means the agent can run the full tractability loop autonomously — `explore()`, read blocker, annotate via clicknick-live, retry, next blocker, annotate, retry, success — without asking permission at each step. The engineer reviews the batch at the end: "The agent added six annotations to close the state space. Review in the address editor." The engineer glances at the diff, corrects the one wrong sensor range, saves. Ten seconds.

### Commands (implemented)

**Flags:**
```
clicknick-live tag set-flag <tag> <flag>      # readonly, external, final, public, lock
clicknick-live tag clear-flag <tag> <flag>
```

**Value constraints:**
```
clicknick-live tag set-choices <tag> <Label:val> ...   # or Bool
clicknick-live tag set-range <tag> <min> <max>
clicknick-live tag clear-constraints <tag>
clicknick-live tag set-uom <tag> <unit>
clicknick-live tag clear-uom <tag>
```

**Physical devices:**
```
clicknick-live tag set-physical <tag> <name> [--on-delay D] [--off-delay D] [--profile P] [--system S]
clicknick-live tag set-link <tag> <link>
clicknick-live tag clear-physical <tag>
```

**Queries:**
```
clicknick-live tag show <tag>
```

**Rung operations:**
```
clicknick-live rung list [file]
clicknick-live rung preview [file] [--select r3,r7]
clicknick-live rung apply [file]
```

**Basic operations:**
```
clicknick-live ping
clicknick-live info
clicknick-live get <tag-or-addr>
clicknick-live set <tag-or-addr> <field> <value>
```

All tag/rung identifiers resolve by pyrung tag name first, Click display address (DS1) as fallback.

Both `pyrung live` and `clicknick-live` support `;` chaining — batch commands in one call:

```
clicknick-live "tag set-range LevelPV 0 100; tag set-flag LevelPV external; tag show LevelPV"
pyrung live "patch HMI_on true; step 5; dataview fill; why fill_stepNumber"
```

---

## The Sync Model — Commit and Push

When the agent pushes annotations through clicknick-live, they flow through a two-step pipeline — analogous to commit + push in git.

**Step 1 — Commit (save in ClickNick).** The engineer reviews unsaved changes in the address editor and saves to the MDB. Annotations are persisted but not yet visible to the pyrung project.

**Step 2 — Push (save in Click).** The engineer saves in Click Programming Software. ScrWatcher detects the save, triggers AnalysisService, and regenerates pyrung_project/ with annotations baked into the source. Now the agent's `explore()` and `always()`/`never()` can see them.

The two-step model is deliberate — the engineer might want to batch several annotations before triggering regeneration, the same way you'd make several commits before pushing. The sync points keep the engineer in control without making them a bottleneck.

### Staleness detection

ClickNick already watches the Scr files via ScrWatcher. After the agent syncs annotations (step 1), ClickNick can compare its state against the Scr files and surface a notification:

- **"Click project unsaved"** — annotations committed to MDB but not yet propagated. Appears after step 1, clears after step 2 triggers regeneration.
- **`clicknick-live info` returns `project_stale: true`** — the agent can query this programmatically instead of hoping the engineer saved.

Basic tools (why, cause, effect, patch, force) work against the live DAP session, so staleness doesn't matter. Advanced tools (explore, how, always, never) work against the regenerated source, so staleness is a gate. The agent checks `project_stale` before retrying `explore()` and waits for the engineer to complete the push.

### The tractability conversation

`explore()` and `how()` now work without annotations — heuristic seeding discovers domains automatically. The annotation conversation arises in two cases:

**Improving explore() quality.** The heuristic seeder finds *some* boundary values, but they may be arbitrary (e.g. Pressure=-10000.001 instead of a meaningful operating range). If the engineer wants interpretable paths, annotations help. The agent should: try `explore()` first, show the result, and suggest annotations only if the path values look meaningless.

**Enabling formal verification.** `always()`/`never()` require sound, bounded domains. When proofs are needed:

1. Agent runs `always()` via pyrung-live, gets Intractable with blocker hints
2. Agent reads the program to infer constraints, asks the engineer to confirm unknowns
3. Agent batch-annotates via clicknick-live (`tag set-choices`, `tag set-range`, etc.)
4. **Commit — engineer reviews and saves in ClickNick** (writes annotations to MDB)
5. **Push — engineer saves in Click** (triggers ScrWatcher → pyrung_project/ regenerates with annotations in source)
6. Agent checks `clicknick-live info` for `project_stale: false`, retries `always()`

The agent should batch as many annotations as possible before asking the engineer to sync, rather than one-at-a-time round trips. Use `pyrung live "dataview i:"` to see all inputs, read the program to infer constraints, annotate the full batch, then ask for one save cycle.

### Why CLI commands instead of source edits

- In Click-first mode, pyrung source is regenerated on every save — source edits to annotations would be overwritten
- Annotations live in ClickNick's layer (address editor comments), which survives regeneration
- Structured commands can't produce syntax errors or break the program
- Edits land as unsaved changes — the engineer reviews before committing
- The agent can work autonomously without the engineer gating each annotation

The annotation is a fact about the machine, not a line of code. CLI commands treat it that way. The edit/save boundary keeps the engineer in control without making them a bottleneck.

---

## Next Phase — Workspace Emission

### Goal

`ladder_to_pyrung_project()` (via project_emitter.py) already generates the program files. Extend it to also emit the agent-facing workspace artifacts so that the pyrung_project/ directory is a complete, self-contained agent workspace.

### What pyrung needs to emit

**1. click-cheatsheet.md** — the existing static cheatsheet from `docs/guides/click-cheatsheet.md`, copied into the workspace. The agent needs this before anything else. Literal copy keeps one source of truth.

**2. CLAUDE.md** — generated from a template, populated with:
- Machine name (from .ckp filename or project metadata)
- "Read `click-cheatsheet.md` first" as the opening instruction
- Program shape: rung count, subroutine list, tag type distribution, tractability estimate
- Available tools and their commands (clicknick-live, pyrung-live)
- Workflow escalation: read → theorize → simulate → search → prove

**3. .claude/settings.json** — permissions for the CLI tools:
```json
{
  "permissions": {
    "allow": [
      "Bash(clicknick-live *)",
      "Bash(pyrung live *)"
    ]
  }
}
```

**4. .claude/skills/** — structured workflow definitions for each scenario. Each skill tells the agent when to trigger, what tools to use, and how to escalate. Maps to the scenarios in this document.

**5. tests/** — pytest scaffold:
- `conftest.py` — PLC fixture with coverage plugin wired up, fixed-step time mode
- `test_smoke.py` — program loads, steps once without error, basic tag assertions
- Generated from the program structure (known tags, known inputs)

### Where this lives in pyrung

The project_emitter already has `_generate_project() → dict[str, str]`. The new artifacts are additional entries in that dict:
- `_generate_cheatsheet()` — copy the static reference
- `_generate_claude_md(...)` — template with program-specific metadata
- `_generate_claude_settings()` — permissions JSON
- `_generate_skills()` — skill markdown files
- `_generate_tests()` — conftest + smoke test from program introspection

The AnalysisService in ClickNick calls `ladder_to_pyrung_project()` and writes the result to disk. No changes needed in ClickNick — pyrung emits the files, ClickNick persists them.

---

## The pyrung Way — Graduating to a Permanent Project

The auto-generated pyrung_project/ is ephemeral — ScrWatcher overwrites it on every Click save. That's by design for Click-first mode: Click is the source of truth, the pyrung project is a derived artifact.

But when the engineer starts wanting things that don't survive regeneration — custom tests, refactored subroutines, version control, CI — they're outgrowing Click-first mode.

The agent should recognize this and offer the on-ramp:

1. **Copy the project** to a permanent directory outside ClickNick's watch path
2. **At that point, the engineer owns the source.** Annotations live in `tags.py` instead of the ClickNick address editor. The two sync points disappear. `explore()`, `always()`, `never()` run directly against the working tree.
3. **Tests persist.** The generated `tests/` scaffold is a starting point; the engineer adds test cases, the coverage plugin tracks what's covered, `pyrung check` catches behavioral regressions in PRs.
4. **Click becomes a deployment target.** `project_to_csv.py` exports back to Click's format. The engineer validates against Click constraints, pastes into Click, and deploys.

The agent doesn't push this. It mentions it when the engineer asks for something that doesn't fit Click-first — "I'd like to add a test for this edge case" or "can we version-control these changes?" That's the signal.

A `.claude/skills/pyrung-way.md` skill can guide the agent through the transition: what to copy, what to change, how to set up pytest and CI, how to use lock files for regression testing.

---

## What makes this different

Every other AI-for-PLC tool generates code and stops. The engineer is the verification layer.

This stack generates code, simulates it, explores it, and — when annotations are available — proves it correct, before handing it to the engineer as paste-ready output. The engineer is the decision-maker — is this the behavior I want? — not the verifier — is this code correct? The prover is the verifier.

The agent is allowed to be wrong. It will draft bad fixes sometimes. But it can check its own work — `explore()` + `how()` immediately, `always()`/`never()` when the program is annotated. The engineer never sees unverified output for safety-critical changes.

And when the machine is down right now, the agent doesn't need any of that infrastructure. `why()` works from a tag dump and the program — no annotations, no history, no simulation session. The cheapest tool answers the most urgent question.

That's not AI-assisted programming. That's AI with a proof obligation.
