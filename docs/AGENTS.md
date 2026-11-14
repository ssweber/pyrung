# pyrung Documentation — Agent Context

## What is pyrung

pyrung is a Python DSL for writing ladder logic. `with rung()` maps to a ladder rung — condition on the rail, instructions in the body. It targets AutomationDirect CLICK PLCs and ProductivityOpen P1AM-200 controllers.

## The pitch

CLICK Programming Software has no simulator. pyrung is the engine under ClickNick's CLICK Simulator, and the conformance suite checks it against a real CLICK, case by case. Write logic in Python, unit test it with pytest, then transpose to CLICK. Or run it as a soft PLC over Modbus, or generate a CircuitPython scan loop for P1AM-200.

Every claim that pyrung was checked against a CLICK gets a visible disclaimer in the same place, not only at the bottom of the page. It says three things. The results come from one CPU (a C2-03CPU with CLICK V4.00), so yours may differ, and the known differences are listed. pyrung doesn't replace testing on the real machine, with proper safeguards, before anything interacts with people. pyrung and ClickNick are not affiliated with or endorsed by AutomationDirect. Write "checked against a real CLICK, case by case", never "matches CLICK".

## Tone and style decisions

- **Direct, no-nonsense, shows-don't-tells.** No marketing fluff, no "powerful" or "elegant." Say what it does.
- **Code speaks first.** Lead with examples, explain after.
- **One concept per section.** Short paragraphs, minimal formatting. No bullet points unless they genuinely help.
- **Don't teach two ways to do the same thing.** Pick the best pattern, show that. Put alternatives in reference docs.
- **Don't front-load internals.** Concepts page = vocabulary for writing programs. Architecture page = how the engine works. API reference = exhaustive details. Keep them separate.
- **Real scenarios, not API walkthroughs.** The quickstart is a traffic light, not "Step 1: Define tags." Teach by building something.
- **Respect the reader's time.** If we've explained `dt=` determinism in the quickstart, don't re-explain it in concepts. Link, don't repeat.
- **State the rule, not its history.** No release narration, dated readings or "measured on hardware". `CHANGELOG.md` holds what changed.
- **Don't document every edge case.** The conformance suite is the source of truth for CLICK behaviour. Docs cover what a program author needs, plus a reference area for gotchas. A corner that only a conformance case reaches stays there.
- **Keep a line only if it changes what an author writes.** Keep it if they would write different code after reading it and couldn't find it out by running it against pyrung. A fine edge of the engine is answered by testing it. Where pyrung differs from CLICK, prefer a validation finding to a paragraph. A change touches docs only when an author's code would change, and a review asks "what can go?" as well as "is it right?".

## Writing

Follow the Writing section of the root `AGENTS.md`. It covers the docs, the changelog, comments and commit messages.

## API design decisions (implemented)

### Tag-first APIs
All PLC methods accept Tag objects as the primary interface. String keys work too. The docs mention them once, in a footnote, and never use them in examples.

- `plc.force(Enable, True)` — Tag object, not `"Enable"`
- `plc.unforce(Enable)`
- `plc.force({Enable: True, Fault: False})` — Tag-keyed dict
- `plc.patch({Button: True})`
- `plc.monitor(Motor, callback)` — Tag object

**Exception: `plc.diff()` returns string-keyed dicts.** It is a diagnostic tool, and string keys read better when printed.

## Key technical details

- **Scan evaluation:** don't describe a rung as evaluated row by row, or as left to right and top to bottom.
  - Each instruction evaluates only the path from the rail to its own inputs.
  - A box evaluates its enable and all its pins together, before it writes.
  - A path segment already evaluated (the trunk, or a branch's own contacts) is reused by later instructions that share it.
  - A contact no instruction has read yet is read live when an instruction first needs it.
  - Instructions execute in source order, so rung and branch instructions interleave.
  - `.continued()` is an import form, kept for programs that hold a CLICK rung with more than one wire. A continued rung evaluates like a rung of its own.
- **Branches** AND their condition with the parent rung's condition.
- **Each rung starts fresh** — sees state as left by previous rung's instructions.
- **`out`** de-energizes when rung is false. **`latch`** is sticky until `reset`.
- **`.value` writes are one-shot** (consumed after one scan). **Forces** persist across scans.
- **Blocks** are typically 1-indexed (PLC convention) but any start index is supported.
- **pyrung matches CLICK.** Where pyrung knowingly differs, the docs say so.
