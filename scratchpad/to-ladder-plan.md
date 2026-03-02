# Click Ladder Export v1 (`TagMap.to_ladder`) With CSV Contract Examples

## Summary
Implement Click ladder export as a `TagMap` concern so all operands are rendered as mapped Click addresses.  
`TagMap.to_ladder(program)` returns a structured bundle containing `main.csv` rows plus one CSV per subroutine, with strict prevalidation and deterministic formatting.

## Public API / Interface Changes
1. Add `TagMap.to_ladder(program: Program) -> LadderBundle`.
2. Add `LadderBundle` (row-matrix primary payload) with `write(directory)` helper.
3. Add `LadderExportError` with structured `issues` (program path + source location when available).
4. Re-export `LadderBundle` and `LadderExportError` from `pyrung.click`.
5. Do not add `export_ladder` alias.

## Output Artifact Contract
1. Emit separate files: `main.csv` and one `sub_<slug>.csv` per subroutine.
2. Subroutine file order is lexical by subroutine name.
3. Writer auto-creates directories and overwrites existing files.

## CSV Contract
1. Header row is always present.
2. Header is exactly `marker,A,B,...,AE,AF`.
3. Fixed width is 33 columns: `marker` + 31 condition columns (`A..AE`) + output (`AF`).
4. Data rows are always fully expanded; no `->` / `...` in output.
5. `marker` is `R` for new rung row; blank for continuation rows.
6. `AF` contains exactly one instruction token or blank.
7. Token style is compact canonical (no extra spaces), explicit defaults, explicit `oneshot` where supported.

## CSV Contract Examples
All examples below are shown in compact form for readability. Real output is fully expanded to `A..AE`.

1. `AND` conditions
Source:
```python
with Rung(x[1], x[2]):
    out(y[1])
```
Rows:
```csv
marker,A,B,C,...,AE,AF
R,X001,X002,-,...,-,out(Y001,0)
```

2. `OR` expansion (`any_of`) with trailing `AND`
Source:
```python
with Rung(any_of(x[1], x[2]), Ready):
    out(y[1])
```
Rows:
```csv
marker,A,B,C,D,...,AE,AF
R,X001,T,Ready,-,...,-,out(Y001,0)
,X002,-,-,...,-,
```
Rule: split column uses vertical stack `T` (top), `+` (middle), `-` (last).
Rule: in OR expansion, only the top branch row carries downstream trailing terms; continuation branch rows end at the split/merge wire marker.

3. Multiple instructions in one rung (shared powered path)
Source:
```python
with Rung(x[1], x[2]):
    out(y[1])
    latch(y[2])
    reset(y[3])
```
Rows:
```csv
marker,A,B,C,...,AE,AF
R,X001,X002,T,...,-,out(Y001,0)
,,,+,...,-,latch(Y002)
,,,-,...,-,reset(Y003)
```
Only first row is `R`; subsequent instruction rows are continuations.

## Instruction Lowering Scope
1. Support all current Click-portable instruction families in v1:
`out/latch/reset/copy/blockcopy/fill/calc/search/pack/unpack/on_delay/off_delay/count_up/count_down/shift/event_drum/time_drum/send/receive/call/for/next/return`.
   - `pack` token forms: `pack_bits`, `pack_words`, `pack_text`.
   - `unpack` token forms: `unpack_to_bits`, `unpack_to_words`.
2. Builder side conditions use continuation dot pins: `.reset()`, `.down()`, `.clock()`, `.jump(step)`, `.jog()`.
3. `call` token is quoted: `call("name")`.
4. `forloop` lowers to `for(count,oneshot)` row, `R` body rows, and closing `R` `next()` row.
5. Ensure subroutines end with `return()`: append only if last emitted instruction is not already return.

## Validation and Failure Behavior
1. Always run strict Click precheck before rendering.
2. Enforce TagMap-only address resolution (no direct raw-name fallback).
3. Enforce no nested subroutine calls.
4. On any issue, raise `LadderExportError` and emit no CSV.

## Test Cases and Scenarios
1. Header/width invariants (33 columns, always expanded).
2. Golden tests for `AND`, `OR`, and multi-instruction examples above.
3. Vertical wire stack correctness (`T/+/-`) for 2+ branch rows.
4. Builder pin continuation row semantics.
5. For-loop lowering (`for`, `R` body rows, `next`).
6. Subroutine split files, lexical ordering, slug names, return-tail behavior.
7. Full instruction token coverage with explicit defaults/oneshot.
8. Strict precheck + `LadderExportError` issue payload checks.

## Assumptions and Defaults
1. CSV is a stable contract and should be deterministic over human formatting preferences.
2. Compact canonical tokens are preferred for parser stability.
3. Export is strict and all-or-nothing.
4. Click address mapping is owned by `TagMap`; export does not infer unmapped hardware addresses.
