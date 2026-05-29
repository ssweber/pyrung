# Bug: forcing a Real tag crashes step with "Cannot use Tag as boolean"

## Repro

```
pyrung live "clear_forces; force HMI_on true; force systemLevel_opt2011 50; force sv_levelSetPoint 60; force sv_levelBand 5; step 1"
```

Error: `Cannot use Tag 'fill_stepNumber' as boolean. Use it in a Rung condition instead: Rung(tag) or Rung(tag == value)`

Forcing just `HMI_on` (Bool) works fine. Adding the Real forces causes the crash on the next `step`.

## Where

- `Tag.__bool__()` raises at `src/pyrung/core/tag.py:284-289`
- Something in the execution engine is evaluating `fill_stepNumber` (an Int with choices) in a boolean context during rung evaluation after Real forces are applied
- `fill_stepNumber` is never used as a bare boolean in the generated source — all uses are `fill_stepNumber == 1`, `fill_stepNumber > 5`, `calc(fill_stepNumber + 1, ...)`, etc.
- Program: "system fill" project via ClickNick Live, `pyrung_project/main.py`
