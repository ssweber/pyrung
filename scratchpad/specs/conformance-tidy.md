# Conformance suite: recommendation (rewritten 2026-11-10)

The first version of this note proposed a model of each instruction and a case generator. That is dropped.
`findings.md` says where the suite stands today.

## What the user ruled (2026-11-10)

- These are bloat as a grid, one cell per instruction: an instruction that is not scanned, one-shot, reset, and
  a copy between two memory names of the same data type.
- For what an instruction accepts, show the CLICK capability table and call it done.
- The suite is not thrown out.

## Why the grid is dropped

- The suite grew from questions about CLICK. Questions are what found the differences.
- The test for a new case is "would a CLICK's answer here surprise anyone?" If not, the capability table covers it.
- "An instruction that is not scanned does nothing" is one rule. It needs one case per family that holds state
  (a timer, a counter, a drum). It does not need one case per instruction. One-shot and reset are the same.

## This release: the report

- Every case, by group, with its state: confirmed on a CLICK, in-process only, or an open question.
- The CLICK capability table as it is: the memory types each operand takes and the options each instruction has.
- One sentence between them: the table shows what is accepted, the cases show what was run on a CLICK.
- A few lines in words on the least confirmed instructions (Flasher 3 of 14, Debounce 5 of 10, OffDelay 4 of 8).
- No coverage grid and no percentage.

## After the release: tidy the suite, do not rebuild it

1. **The checklist becomes data.** It is 173 hand-ticked lines in `tests/conformance/AGENTS.md` (135 ticked,
   38 open, five sections). Each line becomes a rule with a sentence, a state and the cases that prove it.
   A test checks every named case exists. The report reads the file. No case changes, so nothing reruns.
2. **Each case names its subject.** A case's hash covers its exported rows, starting at the comment row
   (id, sentence, expectation). A new field that stays out of those rows leaves every confirmation current.
3. **Write the family rules into the checklist.** Not scanned, one-shot and reset, one line each per family.
   Existing cases already prove most of them.
4. **A handful of new cases for rules nobody has tested.** A time unit other than ms. A bit through a copy
   instruction. Reset in the middle of a run on one timer and one drum. They go in one new group, so no
   existing confirmation goes stale. That is one project to load on the CLICK.
5. **Close what is already open first.** 33 cases have a reading the case does not yet enforce. The Flasher,
   Debounce and off-delay cases are the least confirmed.

## Dropped

- The model per instruction, the generator and the pin grid.
- The recorder as a shipped tool. `measure.py` stays here as a one-off measurement.
- The per-scan record, the fixed address file and the instrument-check project.
