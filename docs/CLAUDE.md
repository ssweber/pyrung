# pyrung Documentation — Context for Claude

## What is pyrung

pyrung is a Python DSL for writing ladder logic. `with Rung()` maps to a ladder rung — condition on the rail, instructions in the body. It targets AutomationDirect CLICK PLCs and ProductivityOpen P1AM-200 controllers.

Core engine (~19k lines, 1,100+ tests) with three dialects: core, Click PLC, and CircuitPython. Includes a VS Code DAP debugger. Solo project, not yet on PyPI.

## The pitch

Click PLCs have no built-in simulator. pyrung lets you test first — write logic in Python, unit test with pytest, then transpose to Click. Or run as a soft PLC over Modbus to test send/receive instructions (two pyrung programs can talk to each other). Or generate a CircuitPython scan loop for P1AM-200.

## Tone and style decisions

- **Direct, no-nonsense, shows-don't-tells.** No marketing fluff, no "powerful" or "elegant." Say what it does.
- **Code speaks first.** Lead with examples, explain after. If the code is clear, don't restate it in prose.
- **One concept per section.** Short paragraphs, minimal formatting. No bullet points unless they genuinely help.
- **Don't teach two ways to do the same thing.** Pick the best pattern, show that. Put alternatives in reference docs.
- **Don't front-load internals.** Concepts page = vocabulary for writing programs. Architecture page = how the engine works. API reference = exhaustive details. Keep them separate.
- **Real scenarios, not API walkthroughs.** The quickstart is a traffic light, not "Step 1: Define tags." Teach by building something.
- **Respect the reader's time.** If we've explained FIXED_STEP in the quickstart, don't re-explain it in concepts. Link, don't repeat.

## API design decisions (implemented)

### Tag-first APIs
All runner methods accept Tag objects as the primary interface. String keys are supported but secondary — mentioned once as a footnote, not used in examples.

- `runner.add_force(Enable, True)` — Tag object, not `"Enable"`
- `runner.remove_force(Enable)`
- `runner.force({Enable: True, Fault: False})` — Tag-keyed dict
- `runner.patch({Button: True})`
- `runner.monitor(Motor, callback)` — Tag object

**Exception: `runner.diff()` returns string-keyed dicts.** This is a diagnostic tool — string keys are more readable when printing or inspecting. No need for tag objects here.

### Condition expressions instead of lambdas
`run_until`, `when`, and other predicate APIs accept the same condition DSL used inside `Rung()`:

```python
runner.run_until(~Motor, max_cycles=100)
runner.run_until(Motor & ~Fault)
runner.run_until(Temp > 150.0)
runner.run_until(any_of(AlarmA, AlarmB, AlarmC))

runner.when(Fault).snapshot("fault_triggered")
runner.when(Fault).pause()
```

No lambdas, no `s.tags.get("Motor")` — same expressions everywhere.

### `runner.fork()` 
Forks from current state by default (the common case). Can accept a `scan_id` kwarg for forking from history. Primary use case is testing alternate outcomes from a shared setup:

```python
fault_path = runner.fork()
normal_path = runner.fork()
```

### `runner.set_rtc(datetime)` — simulation-aware real-time clock
- Stores a base datetime and the simulation timestamp at time of call
- RTC = base_datetime + (current_sim_time - sim_time_at_set)
- In FIXED_STEP: advances deterministically with simulation time
- In REALTIME: effectively tracks wall clock with an offset
- Default (no set_rtc called): uses runner creation wall time as base
- Existing ladder logic system tags (new_year, new_hour, etc.) write through the same offset mechanism
- RTC stored as snapshot metadata, not in SystemState.tags

## Key technical details

- **Scan evaluation:** conditions (rung + branches) evaluate first against pre-instruction state. Then instructions execute in source order — interleaved naturally, not "all rung then all branch."
- **Branches** AND their condition with the parent rung's condition.
- **Each rung starts fresh** — sees state as left by previous rung's instructions.
- **`out`** de-energizes when rung is false. **`latch`** is sticky until `reset`.
- **`.value` writes are one-shot** (consumed after one scan). **Forces** persist across scans.
- **Blocks** are typically 1-indexed (PLC convention) but any start index is supported.
- **The soft PLC** is for testing Modbus send/receive — it runs a real program behind a Click-compatible Modbus interface. Two pyrung programs can talk to each other via their send/receive instructions.
- **Built to match real Click behavior** — timer accumulation, counter edge cases, integer overflow, floating-point handling. No surprises when you move to hardware.

## What we've done

### README (rewritten)
Tight pitch, motor start/stop quickstart example, "Why?" section naming the real pain point (Click has no simulator), soft PLC and Modbus testing surfaced ("two pyrung programs talking to each other"), full product names (AutomationDirect CLICK, ProductivityOpen P1AM-200) where first mentioned, "built to match real Click behavior — no surprises" in core engine blurb.

### Quickstart (rewritten)
Traffic light state machine (simplified, no UDTs). Write → run → test flow. Explains scan timing with concrete math (300 scans × 10ms = 3 seconds). Links to full traffic light example in /examples for next steps (UDTs, edge detection, blockcopy).

### Core Concepts (rewritten)
DSL vocabulary only: scans, tags, rungs, branches (with numbered ①②③④⑤ evaluation diagram), instructions, timers/counters, programs, UDTs, blocks, reading/writing values. Architecture internals displaced to a future Architecture page. No redundancy with quickstart (FIXED_STEP explained once).

### Testing Guide (rewritten)
Scenario-driven, tag-first throughout. Sections in escalating complexity: basic assert → timers → RTC/time-of-day (shift changeover example) → edge detection → forces as fixtures → run_until with condition expressions → forking for alternate outcomes → monitors → predicate breakpoints/snapshots → diff → pytest fixtures. All examples use `runner.active()` / `.value` pattern. No lambdas, no string-keyed dicts (except diff). RTC/freezegun section removed in favor of `set_rtc`.

### Runner Guide (rewritten)
User-facing execution docs only. Creating a runner, time modes, set_rtc, execution methods (step/run/run_for/run_until/run_until_fn), injecting inputs (patch + active()), mode control (stop/reboot/battery), inspecting state, history, playhead, diff, fork, numeric behavior. Debug internals displaced: scan_steps(), scan_steps_debug(), inspect(), inspect_event(), RungTrace → future architecture/DAP docs. Breakpoints/monitors briefly shown, linked to testing guide for patterns.

## What's left

### Needs new home (displaced from old concepts page, context.md, and old runner guide)
- **Architecture guide** — Redux model, SystemState, PRecord, 9-phase scan cycle, ScanContext, consumer-driven execution, scan_steps(), scan_steps_debug(), inspect()/inspect_event(), RungTrace model

### Ladder Logic Guide (rewritten)
Polish pass. Tightened intro (one-line link to concepts instead of re-explaining Program/Rung). Aligned branch section with concepts page ①②③④⑤ evaluation model + three rules + nested branch example. Added oneshot counting subsection. Removed dead API Reference links. Lowercased section headers, removed admonition syntax, matched tone of other guides. Programs section moved to end as brief reference linking to concepts.

### Forces & Debug (rewritten)
Renamed to just "Forces" — the debug half (history, diff, fork, playhead, inspect, breakpoints, monitors) was fully redundant with runner guide + testing guide and removed. Forces section kept as authoritative reference: force vs patch table, add/remove/clear, context manager with nesting, scan-cycle semantics (pre-logic + post-logic), force+patch interaction, supported types. Tag-first examples throughout.

### Guides to review and rewrite
Priority order:
1. **DAP/VS Code Guide** — Low priority. Setup and reference doc, fine as-is. Update when extension ships.

### Dialect docs (not yet reviewed)
- Click Dialect guide
- CircuitPython Dialect guide
- Click Reference (42 pages — probably leave as-is)

### API Reference
Per-slot block config, named_array stride, default_factory, clone, etc. — all displaced from old concepts page, needs a proper reference section.

## Files

Working drafts:
- README: /mnt/user-data/outputs/README_proposed.md
- Quickstart: /mnt/user-data/outputs/quickstart.md
- Core Concepts: /mnt/user-data/outputs/concepts.md
- Testing Guide: /mnt/user-data/outputs/testing.md

## Repo

- README: https://raw.githubusercontent.com/ssweber/pyrung/refs/heads/dev/README.md
- Docs: https://raw.githubusercontent.com/ssweber/pyrung/refs/heads/dev/docs/getting-started/quickstart.md
- Guides: https://raw.githubusercontent.com/ssweber/pyrung/refs/heads/dev/docs/guides/testing.md (and similar paths)
- Examples: https://raw.githubusercontent.com/ssweber/pyrung/refs/heads/dev/examples/traffic_light.py
