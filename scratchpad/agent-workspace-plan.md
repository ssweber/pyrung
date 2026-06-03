# Agent Workspace Emission — Implementation Plan

## Goal

Extend `project_emitter.py` to emit agent-facing workspace files alongside the existing program files.

## Files to emit

```
pyrung_project/
├── AGENTS.md              ← Cross-tool: machine name, tools, workflows, escalation
├── CLAUDE.md              ← Shim: "@AGENTS.md" import (auto-loads into Claude Code context)
├── click-cheatsheet.md    ← Verbatim copy of docs/guides/click-cheatsheet.md
├── .claude/
│   ├── settings.json      ← Permissions for clicknick-live and pyrung live
│   └── skills/
│       ├── diagnose.md    ← Faults, alarms, stuck sequences
│       ├── fix.md         ← Logic changes with verification
│       ├── review.md      ← Program review / explain / handoff
│       └── failure.md     ← Failure mode analysis
```

All new files added to `_SCAFFOLDING_FILES` (skip-if-exists on regeneration).

## AGENTS.md content (template)

Dynamic parts:
- Machine name (new `machine_name` parameter)
- Program structure: main rung count, subroutine names + rung counts

Static parts (from agent-workflow.md lines 194-268):
- Tools section (pyrung live, clicknick-live)
- Discover + diagnose (dataview, why)
- Hypothesis testing (force, step, why loop)
- Annotate tags (clicknick-live tag commands)
- Formal verification (explore, how) — **NOT prove, see below**
- Rung editing (clicknick-live rung commands)
- Generate paste-ready output workflow
- Escalation gradient: why → how → cause → prove
- Reference section (files in project)

## Blocking issue: `prove` is not a pyrung live command

The workflow doc references `pyrung live "prove ..."` but `prove` is Python-only
(`pyrung.core.analysis.prove(program, *conditions)`). It is NOT registered in the
DAP console dispatcher.

**Options:**
1. Add `prove` as a live console command (separate PR, touches dap/console.py + expressions.py)
2. Document prove as Python-only in AGENTS.md (agent writes a script, runs with uv)
3. Omit prove from AGENTS.md for now, add when the command exists

Recommend option 1 as a prerequisite — the agent workflow depends heavily on prove.
Same for `log` output formatting if we want agents to read scan history.

## Code changes

### project_emitter.py

- Add `from pathlib import Path` import
- Add new paths to `_SCAFFOLDING_FILES`
- Add `machine_name: str = ""` kwarg to `_generate_project()`
- Add file entries in `_generate_project()` body
- New functions:
  - `_generate_agents_md(machine_name, rungs, subroutines)` — f-string template
  - `_generate_claude_md()` — one-liner shim with `@AGENTS.md`
  - `_generate_cheatsheet()` — read from `docs/guides/click-cheatsheet.md` via `__file__` path
  - `_generate_claude_settings()` — JSON with allow rules
  - `_generate_skill_diagnose()` — skill markdown
  - `_generate_skill_fix()`
  - `_generate_skill_review()`
  - `_generate_skill_failure()`

### api.py

- Add `machine_name: str = ""` kwarg to `ladder_to_pyrung_project()`
- Pass through to `_generate_project()`

### Cheatsheet source-of-truth

Read from `docs/guides/click-cheatsheet.md` at generation time via path relative to
`__file__` (4 parents up to repo root). Fallback stub if file not found (installed
package without docs/). Future: bundle as package data for the installed case.

## Verified pyrung live commands

All confirmed in dap/console.py registry:

**Execution:** step, run, continue, pause, reload, watch, unwatch
**Data:** force, unforce, clear_forces, patch, bounds, monitor, unmonitor, note
**Analysis:** dataview, downstream, upstream, structures, log, cause, effect,
  recovers, why, how, explore, simplified
**Capture:** record, replay
**Review:** candidates, accept, deny, suppress, spec

**NOT available:** prove (Python-only API)
