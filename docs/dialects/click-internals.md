# CLICK Internals

What a real CLICK does that its manuals don't say, measured on hardware and in CLICK Programming Software. pyrung's estimates and validators rest on these numbers.

## Program steps

A CLICK project holds at most 8,000 program steps. AutomationDirect does not publish what each element costs, so pyrung measured it and estimates from the exported ladder:

```python
from pyrung.click import estimate_steps, pyrung_to_ladder

estimate = estimate_steps(pyrung_to_ladder(logic, mapping))
print(estimate.total, estimate.main, estimate.subroutines)
```

`estimate.unmeasured` names any instruction no probe has measured yet; each one is costed high (8) on purpose. Validation reports `CLK_STEP_LIMIT` when the estimate passes 95% of the limit (a hint) or the limit itself (a warning).

The estimate reads the exported CSV rows, so it sees what CLICK is given: legs, pin rows and the always-on rail as CLICK lays them out. Against a whole project (the conformance suite's part A, 592 rungs) it came to 6,067 where CLICK reported 6,088, before `break` and `continue` were measured. Leave a few hundred steps of margin.

### Measured costs

Each cost below excludes the rung's contacts unless noted.

| Element | Steps |
|---|---|
| NO, NC, rise, fall or immediate contact | 1 |
| Compare contact (`==`, `>`, ...; INT, DINT or REAL; constant or register) | 3 |
| No contact on the rung (always-on rail) | 1 |
| OR'd contacts | 1 each, no OR overhead |
| Rung comment, any length | 0 |
| `out`, `latch`, `reset`, `out(immediate(...))`, `toggle` | 1 |
| `reset` of a range | 2 |
| `flasher` | 4 |
| `on_delay`, `off_delay`, `count_up`, `count_down` | 2, plus 1 per pin contact (reset, down) |
| `copy` (constant, register, REAL, oneshot) | 4 |
| `fill`, `unpack_to_bits`, `unpack_to_words` | 4 |
| `blockcopy`, `pack_bits`, `pack_words` | 5 |
| `search` | 6 |
| `shift` | 2, plus 1 per clock and reset contact |
| `calc` | 2 + operands + operators |
| `call`, `break`, `continue` | 1 |
| For and Next rungs (no contacts) | 5 together |
| Main with only END | 3 |
| A subroutine with only its RET | 4 |

A rung with two or more output legs pays extra: 1, plus 1 for each leg without a contact of its own (the first leg counts as one). Two coils on one rung cost 6 with their contact, not 3.

`calc` fits its six measured formulas, counting operands and operators but not parentheses: `DS30 + DS31` is 5 and `(DS33 * DS34 + DS35) / DS36` is 9. A formula with REAL constants came out 1 lower than the rule (`DF1 * 1.8 + 32.0` is 6), so the rule rounds up there.

One drum was measured: an event or time drum with 2 outputs and 3 steps is 14, plus its contacts. Other sizes are estimated as 8 + pattern cells, a guess.

### Method

`devtools/click_step_probe.py` builds a project with one element per rung. Load it with Guided Paste, read the step count (the PC side of **Write Project into PLC** shows it without a download), then delete rungs from the bottom up and read the count after each. Each difference is one rung's cost. The script writes the sheet to fill in, and `--check` compares the estimator with the readings it holds (2026-10-29, CLICK Programming Software). A new round adds its rungs and readings there.
