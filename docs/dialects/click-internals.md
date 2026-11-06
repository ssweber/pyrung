# CLICK Internals

What each element of a CLICK program costs in program steps, and how pyrung estimates a program's total.

## Program steps

A CLICK project holds at most 8,000 program steps. AutomationDirect does not publish what each element costs, so pyrung estimates the total from the exported ladder:

```python
from pyrung.click import estimate_steps, pyrung_to_ladder

estimate = estimate_steps(pyrung_to_ladder(logic, mapping))
print(estimate.total, estimate.main, estimate.subroutines)
```

`estimate.unmeasured` names any instruction no probe has measured yet; each one is costed high (8) on purpose. Validation reports `CLK_STEP_LIMIT` when the estimate passes 95% of the limit (a hint) or the limit itself (a warning).

The estimate reads the exported CSV rows, so it sees the legs, pin rows and always-on rail as CLICK lays them out. The estimate is within about 1% on a large project. Leave a few hundred steps of margin.

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
| `math` | 2 + operands + operators |
| `call`, `break`, `continue` | 1 |
| For and Next rungs (no contacts) | 5 together |
| Main with only END | 3 |
| A subroutine with only its RET | 4 |

A rung with two or more output legs pays extra: 1, plus 1 for each leg without a contact of its own (the first leg counts as one). Two coils on one rung cost 6 with their contact, not 3.

`math` fits its six measured formulas, counting operands and operators but not parentheses: `DS30 + DS31` is 5 and `(DS33 * DS34 + DS35) / DS36` is 9.

A drum is estimated as 8 plus its pattern cells (outputs times steps), plus its contacts. Only a 2-output, 3-step drum has been checked on a CLICK.
