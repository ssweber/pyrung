# Plan: Spec → Real Docs Transition

## Context

The `spec/` files were written as internal architecture decision records ("Handoff" status), not user documentation. Now that milestones 1-6 are mostly complete and the DAP/debug work is in progress, the project needs user-facing documentation: tutorials, guides, and a proper API reference. The spec files will be deleted once their content is absorbed into docstrings and guide pages. The project already has mkdocs + mkdocstrings wired up — the infrastructure exists, it just needs content.

---

## Target Documentation Architecture

### Final `docs/` structure and mkdocs nav

```
docs/
  index.md                         # Landing: what pyrung is, 10-line example, links
  getting-started/
    installation.md                # pip/uv install, Python >=3.11
    quickstart.md                  # End-to-end: define tags → write logic → run → read state
    concepts.md                    # Redux mental model, scan cycle, immutability
  guides/
    ladder-logic.md                # DSL: Rung, conditions, branches, all instructions
    runner.md                      # PLCRunner: step/run/patch, FIXED_STEP vs REALTIME
    testing.md                     # Deterministic tests with FIXED_STEP + pytest
    forces-debug.md                # add_force, force context, implemented debug API
    dap-vscode.md                  # DAP adapter, launch.json, inline decorations
  dialects/
    click.md                       # Click blocks, TagMap, nickname file, validation
    circuitpy.md                   # Stub: "planned"
  reference/
    index.md                       # Auto-generated (gen_reference.py)
    api/                           # Auto-generated (not_in_nav)
  click_reference/                 # Kept as-is, excluded from nav (hardware manual mirror)
```

### mkdocs.yml nav section (add):

```yaml
nav:
  - Home: index.md
  - Getting Started:
      - Installation: getting-started/installation.md
      - Quickstart: getting-started/quickstart.md
      - Core Concepts: getting-started/concepts.md
  - Guides:
      - Writing Ladder Logic: guides/ladder-logic.md
      - Running and Stepping: guides/runner.md
      - Testing with FIXED_STEP: guides/testing.md
      - Forces and Debug Overrides: guides/forces-debug.md
      - DAP Debugger in VS Code: guides/dap-vscode.md
  - Click PLC Dialect: dialects/click.md
  - API Reference:
      - Overview: reference/index.md
```

### mkdocs.yml extensions to add (for code blocks with annotations):

```yaml
markdown_extensions:
  - admonition
  - attr_list
  - pymdownx.highlight:
      anchor_linenums: true
  - pymdownx.superfences
  - pymdownx.inlinehilite
```

---

## User Stories → Guide Pages

| User Story | Guide Page | Primary Spec Source |
|---|---|---|
| I want to simulate a PLC program | `getting-started/quickstart.md` | `spec/overview.md` |
| I want to write ladder logic in Python | `guides/ladder-logic.md` | `spec/core/dsl.md`, `spec/core/types.md`, `spec/core/instructions.md` |
| I want to step through execution and inspect state | `guides/runner.md` | `spec/core/engine.md` |
| I want to test my logic deterministically | `guides/testing.md` | `spec/core/engine.md` (FIXED_STEP section) |
| I want to use the debug/force API | `guides/forces-debug.md` | `spec/core/debug.md` (Phase 1+2 only) |
| I want to use DAP with VS Code | `guides/dap-vscode.md` | `spec/core/debug.md` (DAP sections) |
| I want to use the Click PLC dialect | `dialects/click.md` | `spec/dialects/click.md` |

---

## Docstring Gaps to Fill (Priority Order)

### P1 — Blocks the quickstart and ladder-logic guide from linking to useful API reference

**`src/pyrung/core/tag.py`**
- `Bool`, `Int`, `Dint`, `Real`, `Word`, `Char`: Add class docstrings — what type it creates, default retentive behavior, one-line example.
- `AutoTag`: Docstring explaining the class-body auto-naming pattern.
- `LiveTag.value`: Note that `.value` requires `runner.active()` scope.
- `InputTag.immediate`, `OutputTag.immediate`: Two-sentence docstring on scan-cycle bypass.

**`src/pyrung/core/memory_block.py`**
- `Block.select()`: Inclusive bounds, sparse-block behavior, static vs indirect return type.
- `InputBlock`, `OutputBlock`: Class docstrings distinguishing them from `Block`.
- `Block.__getitem__` (Tag/Expression keys): Document indirect addressing path.

### P2 — Instruction implementation classes (sparse, but auto-rendered by mkdocstrings)

In `src/pyrung/core/instruction/`: `CopyInstruction`, `CalcInstruction`, `OnDelayInstruction`, `OffDelayInstruction`, `CountUpInstruction`, `CountDownInstruction`, `ShiftInstruction`, `SearchInstruction`. One-paragraph class docstring + key parameter/behavioral notes (overflow semantics, clamping, has_reset, etc.).

### P3 — PLCRunner debug/force API

**`src/pyrung/core/runner.py`**
- `add_force`, `remove_force`, `clear_forces`: Force semantics (persists across scans, pre/post-logic application).
- `force` context manager: temporary/nested-safe semantics.
- `scan_steps` vs `scan_steps_debug`: When to use each.

### P4 — Click dialect (all near-zero docstrings)

**`src/pyrung/click/`**
- `TagMap` class + `from_nickname_file`, `to_nickname_file`, `map_to` — the pivot of the Click guide.
- Module docstring for `__init__.py` listing pre-built blocks (`x`, `y`, `c`, `ds`, etc.) with types.
- `ClickDataProvider`, `send`, `receive`.

---

## Spec → Docs Migration Map

| Spec file | Destination | Notes |
|---|---|---|
| `spec/overview.md` | `quickstart.md`, `concepts.md`, `ladder-logic.md` | "Layer Architecture" diagram and "Dependency Graph" → drop entirely (internal). |
| `spec/core/types.md` | `ladder-logic.md` + Tag/Block docstrings | "Needs Specification" sections → delete. |
| `spec/core/dsl.md` | `ladder-logic.md` | Code examples translate directly. "Needs Specification" → delete. |
| `spec/core/instructions.md` | `ladder-logic.md` + instruction class docstrings | Instruction Index table → guide. Math overflow tables → `CalcInstruction` docstring. "Needs Specification" → delete. |
| `spec/core/engine.md` | `runner.md` + PLCRunner/SystemState docstrings | Scan Cycle Phases 0-8 → `runner.md` and `SystemState` class docstring. "Needs Specification" → delete. |
| `spec/core/debug.md` | `forces-debug.md` + `dap-vscode.md` | Phase 1+2 fully guide-ready. For Phase 3, `history`, `seek/rewind/playhead`, `diff`, and `fork_from` are implemented; keep `inspect`, monitors, breakpoints, and labels in planned sections until shipped. (Track in `scratchpad/debug-api-next-steps.md`.) |
| `spec/dialects/click.md` | `dialects/click.md` + click module docstrings | "Needs Specification" → delete. |
| `spec/dialects/circuitpy.md` | `dialects/circuitpy.md` | Retired — dialect doc written. Spec and internal handoff brief deleted. |

### After migration, `spec/` collapses to:
- `spec/core/debug.md` (Phase 3 remainder — internal reference for next implementation batch)
- Or: move to `docs/internal/` and add to `exclude_docs` in mkdocs.yml.

---

## Sequencing (Critical Path)

### Phase A: Infrastructure (1 day)
1. Expand `mkdocs.yml`: full nav, pymdownx extensions, Material theme features
2. Update `docs/index.md`: proper landing page with working example

### Phase B: Getting Started (2-3 days)
3. Write `docs/getting-started/installation.md`
4. Write `docs/getting-started/quickstart.md` (absorb `spec/overview.md`)
5. Write `docs/getting-started/concepts.md` (Redux model, scan cycle)
6. Fill **P1 docstring gaps** (tag.py, memory_block.py)

### Phase C: Core Guide (3-4 days)
7. Write `docs/guides/ladder-logic.md` (absorb `spec/core/dsl.md`, `spec/core/types.md`, `spec/core/instructions.md`)
8. Fill **P2 docstring gaps** (instruction classes)

### Phase D: Runner + Testing (2-3 days)
9. Write `docs/guides/runner.md` (absorb `spec/core/engine.md`)
10. Write `docs/guides/testing.md` (new content; FIXED_STEP + pytest patterns)
11. Fill **P3 docstring gaps** (runner force/debug API)
12. Write `docs/guides/forces-debug.md` (Phase 1+2 from `spec/core/debug.md`)

### Phase E: Click Dialect (2-3 days)
13. Write `docs/dialects/click.md` (absorb `spec/dialects/click.md`)
14. Fill **P4 docstring gaps** (TagMap, ClickDataProvider, send/receive)

### Phase F: DAP Guide (2 days — after DAP Phase 2 is code-complete)
15. Write `docs/guides/dap-vscode.md` (absorb DAP sections from `spec/core/debug.md`)
16. Fill `DAPAdapter` class docstring

### Phase G: Cleanup
17. Delete `spec/overview.md`, `spec/core/types.md`, `spec/core/dsl.md`, `spec/core/instructions.md`, `spec/core/engine.md`, `spec/dialects/click.md`
18. Archive remaining spec files or move to `docs/internal/`

---

## Key Files to Modify

- `mkdocs.yml` — nav + extensions
- `docs/index.md` — proper landing page
- `src/pyrung/core/tag.py` — P1 docstrings
- `src/pyrung/core/memory_block.py` — P1 docstrings
- `src/pyrung/core/runner.py` — P3 docstrings
- `src/pyrung/core/instruction/*.py` — P2 docstrings
- `src/pyrung/click/tag_map.py` — P4 docstrings
- `src/pyrung/click/__init__.py` — P4 module docstring
- New guide files under `docs/` (all new)

---

## Verification

- `mkdocs build --strict` — confirms no broken cross-references or missing pages
- `mkdocs serve` — visual inspection of each guide page and API reference
- All code examples in guides should be copy-paste runnable (extract to `docs/examples/` or `src/pyrung/examples/` and test with `make test`)
- `make` (lint + test) must stay green after any docstring or source changes

