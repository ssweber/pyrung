# S5: hardware answers (CLICK round, 2026-11-01)

The cases hold the detail; this is the index. Numbers match the references in the tests.

## Questions

1. **Dead-path contacts: solved.** An edge contact after a false contact still tracks its bit,
   on a box's path and in a branch. `row_solve/edge_on_box_path_after_false_contact`,
   `row_solve/edge_after_false_contact_in_branch`.
2. **A box first on a continued wire: accepted,** and it reads like a box on its own rung.
   `cont_box/box_pin_on_continued_wire`, `box_enable_on_continued_wire`, `box_pin_on_own_rung`.
3. **A box rung with no trunk contact: accepted.** `cont_box/box_tee_without_trunk`.
4. **A For body split into body rungs: reads the same.** `paste_risk/loop_body_split`.
5. Dropped: CLICK has no coil on CT/T. pyrung refuses out/latch/reset on a timer's or
   counter's bits and points at the `.reset()` pin.
6. **Drum correction scan: answered, not as hypothesised.** See the drum rule below.
   `row_solve/correction_scan_with_free_enable`, group `drum_trace`.

The solver wording is done (docs, comments and the lint message).

## Drum rule, for the DRUM fix

A drum writes its outputs when its step moves or its rung comes back, not on every scan. A
write to its step register is not a move; the next event acts on the register's step.

- A jog on a bad step leaves that scan alone; the next scan corrects with no output write.
- With no jog, a bad step corrects at once, with no output write.
- A jog on the last step completes: flag on, outputs off, stays on the step. A further jog
  does nothing.

## Fault flags, for the SC43, MATH, SYS and TEXT fixes

Group `faults` (part E), plus `paste_risk/rand_real_bounds`.

- Every Math that wraps raises SC43.
- A Copy that clamps raises SC43, including +/-Inf into an Int. NaN writes 0 without it.
- A Math of NaN, into a Real or an Int, raises no SC43. Of +Inf into an integer: 0 with SC43.
- `RAND` with equal constants writes 0 with SC46; from DFs, equal or not, 0 with no flag.
  CLICK's editor refuses equal bounds and DF bounds typed in, so these are what a paste does.
- `RAND` with constants that differ draws between them with no flag, the bounds cut to 16
  bits: `RAND(70000,70002) / 2` read 2233, half of 4466.
- A pointer below its bank: SC44, destination untouched. Past it: no flag, and it reads what
  lies past the bank, no wrap and no clamp: 1 at DD1001, 0 at 1531 and 1532. pyrung has no
  memory there, so the SYS fix has to choose a value, and `pointer_past_bank`'s 1 may need
  loosening with it.
- +Inf copied to text writes lowercase text with no flag. SIN(+Inf) leaves no 0 and raises no
  flag; SQRT(-Inf) writes 0 with SC46.

## FOR with a Real count

CLICK's editor takes a FOR count from a constant, DS, DD, TD, CTD or SD. A pasted DF stops the
PLC: 3.0 ran 545130 iterations until a 10 s watchdog raised error 0107, which fits its 32 bits
read whole. `for_count` is in-process only, and part D is gone.

## Still to do

- Every wire case is confirmed on a CLICK (`tests/conformance/confirmed.toml`).
- Have CLICK validation refuse what the editor refuses: a Real FOR count, RAND with DF bounds.
- Land the fixes `pending=` names (`PENDING_STEPS` in `src/pyrung/conformance/_case.py`).
- Docs, CHANGELOG, then the prover sweep. The ledger holds the list:
  `pyrung-wt/buildbot/jobs/conformance-round2/LEDGER.md`.
