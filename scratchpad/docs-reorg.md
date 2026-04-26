# Docs reorganization: surface the v0.6.0 workflow

## The problem

The v0.6.0 story is a workflow (Declare → Analyze → Commission) but the docs teach it as scattered features. Physical annotations, prove(), coverage, harness, fault coverage, lock files — they're all chapters of the same story but live in three different guides (physical-harness.md, analysis.md, testing.md) with no clear reading order.

commissioning.md was supposed to be the hub but it's 34 lines of links — a table of contents, not a guide. Fault coverage is the most valuable workflow in the project and it's buried at the bottom of physical-harness.md behind "Tag metadata: min, max, uom."

## Current state

```
Guides/
  runner.md              265 lines  — execution, time modes, history, fork
  testing.md             357 lines  — pytest patterns, forces, forking, monitors, bounds
  forces-debug.md         81 lines  — force vs patch, scan cycle interaction
  commissioning.md        34 lines  — link hub (Declare/Analyze/Commission)
  physical-harness.md    373 lines  — Physical, link=, harness, fault coverage (buried)
  analysis.md            692 lines  — dataview, cause/effect, coverage, prove(), lock files
  tag-structures.md      327 lines  — UDTs, named arrays, cloning, blocks, flags
  click-cheatsheet.md    399 lines  — Click instruction lookup table
  click-quickstart.md    119 lines  — Click end-to-end workflow
  circuitpy-quickstart.md 107 lines — P1AM end-to-end workflow
  dap-vscode.md          451 lines  — VS Code debugger
  architecture.md        130 lines  — engine internals
```

### Issues

1. **analysis.md does double duty** — static inspection (dataview, cause/effect, coverage queries) and exhaustive proof (prove(), lock files). Different audiences, different points in the workflow.

2. **Fault coverage is hidden** — the two-pass workflow (structural with prove() + timing with force) is the payoff of the whole Declare/Analyze/Commission story but it's a section at the bottom of physical-harness.md.

3. **forces-debug.md is orphaned** — 81 lines that overlap with testing.md. Not enough to stand alone, too useful to delete.

4. **commissioning.md is a skeleton** — the workflow needs a real introduction that walks the reader through the progression, not just links.

5. **No reading order** — a user who finishes testing.md doesn't know whether to go to physical-harness.md, analysis.md, or commissioning.md next.

6. **Click cheatsheet is in Guides, not Dialects** — it's a lookup table, not a guide.

## Proposed structure

```
Guides/
  Essentials
    runner.md              — execution, time modes, history, fork
    testing.md             — pytest patterns, forces, forking, monitors, bounds
                             (absorb forces-debug.md content)
    tag-structures.md      — UDTs, named arrays, cloning, blocks, flags

  Declare, Analyze, Commission
    commissioning.md       — rewrite: real intro, walks the workflow, explains
                             when/why to use each step. Entry point.
    physical-harness.md    — declare physical behavior, harness, feedback synthesis
                             (shed fault coverage — point to verification.md)
    analysis.md            — dataview, cause/effect, simplified forms, coverage
                             queries, static validators. "Inspect your program."
    verification.md        — NEW: prove(), lock files, fault coverage.
                             "Prove it's correct." The payoff.

  Platform
    click-quickstart.md    — Click end-to-end
    click-cheatsheet.md    — Click instruction lookup (move from top-level guides)
    circuitpy-quickstart.md — P1AM end-to-end

  Tools
    dap-vscode.md          — VS Code debugger
    architecture.md        — engine internals
```

### Key moves

**1. Split analysis.md → analysis.md + verification.md**

analysis.md currently covers:
- DataView queries (inspection)
- Simplified forms (inspection)
- Cause/effect (inspection)
- Coverage queries (inspection)
- Static validators (inspection)
- prove() (verification)
- Lock files (verification)

Split at "Verification: prove it holds." Everything above stays in analysis.md ("inspect your program"). Everything from prove() down moves to verification.md ("prove it's correct"). Fault coverage moves from physical-harness.md to verification.md.

verification.md outline:
- prove() — exhaustive state-space checking
- Condition syntax, result types, scoping
- Settle-pending semantics (timer-gated alarms)
- Fault coverage — the two-pass workflow
  - Structural with prove() + harness.couplings()
  - Timing with force + run_for
  - Link to examples/fault_coverage.py
- Lock files — behavioral regression in PRs
  - pyrung lock / pyrung check
  - __lock__ configuration
  - CI integration

**2. Rewrite commissioning.md as a real intro**

Current: 34 lines of links.
Proposed: ~100-150 lines. Walks the progression:
- You wrote logic and tested it (Testing guide). Now what?
- Declare: annotate physical behavior so the harness can drive your tests
- Analyze: inspect the program graph, run coverage, find gaps
- Verify: prove properties hold across all reachable states
- Commission: run against real hardware with confidence

Each section is 2-3 sentences + a code snippet + a link to the deep guide. The reader understands the workflow without reading 1400 lines across three guides.

**3. Fold forces-debug.md into testing.md**

forces-debug.md content:
- Force vs patch semantics (21 lines)
- Adding/removing forces (15 lines)
- forced() context manager (10 lines)
- Inspecting active forces (8 lines)
- Scan cycle interaction (12 lines)
- Force/patch interaction (8 lines)

testing.md already has "Using forces as test fixtures." Merge the force-vs-patch semantics and scan cycle details there. The forced() context manager and inspection APIs go in runner.md's "Injecting inputs" section.

Delete forces-debug.md after merge. Update all cross-references.

**4. Trim physical-harness.md**

Remove the "Fault coverage" section (now in verification.md). Add a one-line pointer: "For fault coverage — proving every device has an alarm path — see [Verification](verification.md#fault-coverage)."

Keep everything else: Physical declarations, link= syntax, value triggers, harness usage, profile functions, validation, forces override, tag metadata. This is the "Declare" step.

**5. Move click-cheatsheet.md to Platform group**

It's a lookup table for the Click dialect. Lives next to click-quickstart.md and the Click dialect reference.

## Nav changes (mkdocs.yml)

```yaml
nav:
  - Home: index.md
  - Learn: ...
  - Getting Started: ...
  - Instruction Reference: ...
  - Guides:
    - Essentials:
      - Execution Engine: guides/runner.md
      - Testing: guides/testing.md
      - Tag Structures: guides/tag-structures.md
    - Declare, Analyze, Commission:
      - Overview: guides/commissioning.md
      - Physical Annotations: guides/physical-harness.md
      - Analysis: guides/analysis.md
      - Verification: guides/verification.md
    - Platform:
      - Click Quickstart: guides/click-quickstart.md
      - Click Cheatsheet: guides/click-cheatsheet.md
      - CircuitPython Quickstart: guides/circuitpy-quickstart.md
    - Tools:
      - VS Code Debugger: guides/dap-vscode.md
      - Architecture: guides/architecture.md
```

## Migration checklist

- [ ] Create verification.md from analysis.md prove()/lock sections + physical-harness.md fault coverage section
- [ ] Trim analysis.md (remove prove/lock content, add pointer to verification.md)
- [ ] Trim physical-harness.md (remove fault coverage section, add pointer)
- [ ] Merge forces-debug.md into testing.md and runner.md
- [ ] Delete forces-debug.md
- [ ] Rewrite commissioning.md as workflow introduction
- [ ] Update mkdocs.yml nav
- [ ] Update all cross-references (grep for forces-debug, analysis.md#verification, etc.)
- [ ] Update CLAUDE.md docs section to reflect new structure
- [ ] Run docs build, verify no broken links
