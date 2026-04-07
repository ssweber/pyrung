# Test Audit: Codegen and Ladder Exporter

Goal: bring tests for `ladder_to_pyrung` (CSV → pyrung) and
`pyrung_to_ladder` (pyrung → CSV) to a uniform style where each
test shows the full input and full output side by side. No partial
matching, no hidden assertions, no tests that lie by omission.

## Why

Today's exporter rewrite collapsed a class of layout bugs the test
suite hadn't been catching. Concrete failure mode: a test asserts
one cell, passes, the real CSV comes out broken, we ask for
expected vs actual, paste both into Click, and *both* are wrong in
different ways. Single-cell assertions (`row[0] == "X"`) can't see
the bugs that live around them. Full-grid equality can.

The audit's "out of scope" line: this is about test clarity and
redundancy, not new coverage or production fixes. If a test exposes
a bug during conversion, file it and keep moving.

## Two test mechanisms

**CSV-with-comment fixtures** for shapes only humans can draw. A
Click CSV file with the expected pyrung source written into the
rung comment. You draw it in Click, type the expected pyrung in the
comment, save. The whole fixture is one artifact you can open and
verify in Click. One parameterized test loads them all:

```python
@pytest.mark.parametrize("fixture", load_fixtures("user_shapes/"))
def test_user_fixture(fixture):
    decoded = analyze(fixture.csv)
    expected = decoded[0].comment
    actual = "\n".join(generate_pyrung(r) for r in decoded)
    assert actual.strip() == expected.strip()
```

Adding a case is "draw it in Click, save, done" — no Python edit.
For multi-rung splits (continued-rung cases), the primary rung's
comment covers the whole group; continued rungs can't carry their
own comments per the DSL rules.

**Triple-quoted tests** for everything else: exporter, codegen, and
regression cases where the input is naturally pyrung source rather
than a hand-drawn grid.

The split is mechanical: if a human had to draw the input in Click,
it's a fixture. Otherwise it's a triple-quoted test.

## Use raw addresses, not semantic names

Test bodies use raw Click addresses (`C1`, `DS1`, `T1`, `CT1`,
`TD1`, `CTD1`), not semantic names like `Start`, `Done`, `Acc`.
Three reasons:

1. **Correct frame.** These tests are about structure: does this
   rung shape produce this grid shape? `C1` is obviously a
   placeholder; `Start` invites speculation about start buttons.
2. **Cleaner round-trips.** Raw addresses round-trip without
   touching the nickname pipeline. A failure localizes to layout
   instead of "could be either layer."
3. **Trivial helper.** Each address declares as the type its
   prefix implies — same convention codegen already uses, no
   inference, no drift.

Semantic names belong only in tests specifically about the
nickname pipeline.

## Helpers

Live next to the test files, not in production code.

1. **`build_program(source)`** — for exporter tests. Takes a
   triple-quoted pyrung body, declares each referenced address as
   the type its prefix implies (`C` → Bool, `DS` → Int, `T` →
   Timer, `TD` → Timer data, `CT` → Counter, `CTD` → Counter
   data), prepends the declarations, execs in a fresh namespace,
   and returns the resulting `Program`. Bare bodies are fine; the
   helper can auto-wrap them in `with Program() as p:`.
2. **`strip_pyrung_boilerplate(generated)`** — for codegen tests.
   Removes imports, leaves the `with Program()` block (including
   tag declarations) for comparison. Fails loudly on shape
   mismatch.
3. **CSV normalizer** — strips the header row, the trailing
   `end()` rung that every export appends, and trailing empty
   cells per row. (`_write_cell` already raises on writes past
   column AF, so trimming costs nothing in coverage.)
4. **Pyrung normalizer** — strips trailing whitespace per line so
   indentation diffs are real.
5. **Fixture loader** — walks a directory of CSVs, returns
   `(csv_text, comment)` pairs for the parameterized test.

## Target style

### Exporter test

```python
def test_counter_with_reset():
    p = build_program("""
        with Program() as p:
            with Rung(C1):
                count_up(C2, accumulator=DS1, preset=10).reset(C3)
    """)

    assert export_csv(p) == """\
R,C1,-,...,-,count_up(C2,DS1,10)
,C3,-,...,-,.reset
"""
```

### Codegen test

```python
def test_counter_with_reset_roundtrip():
    csv = """\
R,C1,-,...,-,count_up(C2,DS1,10)
,C3,-,...,-,.reset
"""
    assert generate_pyrung(csv) == """\
with Program() as p:
    with Rung(C1):
        count_up(C2, accumulator=DS1, preset=10).reset(C3)
"""
```

## Fixtures to author

These exercise shapes the exporter never produces — bridges,
rail-shared wires, terminal-instruction quirks. They're invisible
to a DSL-driven generator because the exporter normalizes them
away on the way out.

1. **Bridge topology.** Non-SP graph that triggers Shannon
   expansion. (Already exists as a CSV fixture; convert first as
   the template for the rest.)
2. **Maniac reset wire.** A rail-fed wire feeding a counter or
   RTON's `.reset()` *and* driving a sibling `out(...)`. Exercises
   `_split_continued`.
3. **Multiple terminal instructions in one rung.** Two counters
   (or counter + RTON) on the same visual rung, each with their
   own reset.
4. **Reset wire shared between two terminal instructions.** One
   rail-fed wire feeding the `.reset()` of two different counters.
5. **Pin row with non-trivial conditions.** A counter whose reset
   pin row has multiple ANDed contacts, an OR via parallel
   branches, or a nested sub-branch. Exercises pin-conditions →
   `.reset(...)` argument tuple translation.
6. **Bridge with a pin row.** Combines (1) and (5).

Each fixture is one rung, ≤ 1400 chars of expected source (the
comment limit), named after its category. If it doesn't fit, it's
testing too much — split it.

## Audit checklist

For each existing test, ask:

**Whole output or piece of it?** Whole-output → convert directly.
Partial-match → either the author was lazy (convert to full-grid)
or they were testing one focused property (keep, rename to make
the property obvious, add a separate full-grid test).

**Redundant with another?** After conversion, identical inputs +
identical outputs = same test, delete one. A common pattern:
"happy path" + "with comment" tests differing by one
`r.comment = "..."` line — merge them.

**Telling the whole truth?** Tells that it isn't:

- Asserts only the AF column → hides every condition-cell bug.
- Asserts row count but not row content → hides cell placement.
- Asserts "renders without raising" → tests nothing but
  exception-freedom. Upgrade or delete.
- Uses `in` instead of `==` → partial match in disguise.
- Uses semantic tag names (outside nickname-pipeline tests) →
  wrong frame, convert to raw addresses.
- Mocks the layout pass → removes the test's value, since the
  layout pass is what's most likely to be wrong.

**Property test in disguise?** If the test would need a `for` loop
or parameterization to express, it's an invariant and belongs in a
Hypothesis suite, not the triple-quoted suite. The two cover
different things: triple-quoted = specific cases, Hypothesis =
invariants.

## Order of operations

1. **Helpers first.** Without them every conversion is unreadable
   and the audit stalls. One afternoon.
2. **Convert the bridge fixture** as the template for fixture-style
   tests. Proves the loop end-to-end before committing to more.
3. **Author the rest of the fixture corpus.** High-value coverage,
   exercises the helpers on hard cases.
4. **Convert whole-output triple-quoted tests file by file.** Easy
   wins, no judgment calls. Replace semantic names as you go.
5. **Triage partial-match tests.** Convert, split, or move to
   Hypothesis. The slow part.
6. **Delete redundancies.** Obvious once everything's in the same
   style.

## Success criteria

Every test fits one of four shapes:

- **CSV-with-comment fixture** — for hand-drawn shapes.
- **Triple-quoted exporter test** — pyrung body in, CSV string
  out, equality.
- **Triple-quoted codegen test** — CSV string in, pyrung source
  out, equality.
- **Hypothesis property test** — invariants only.

No `in` checks, no AF-only assertions, no
"renders-without-raising" tests, no semantic tag names outside
nickname-pipeline tests, no mocks of the layout pass. When
something breaks, the failure is a diff between two human-readable
artifacts.
