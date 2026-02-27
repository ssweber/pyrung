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