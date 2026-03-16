# Click Ladder CSV Contract (v2)

This document specifies the CSV contract emitted by Click ladder export:

- API entrypoint: `TagMap.to_ladder(program)`
- File writer: `LadderBundle.write(directory)`

It is intended for implementers building a CSV consumer/decoder.

## Scope and guarantees

This is a producer contract for what pyrung emits, not a general parser spec for arbitrary CSVs.

- Deterministic output
- Fully expanded rows (no shorthand expansion required)
- Strict prevalidation before emit
- All-or-nothing export (on failure, no bundle is emitted)

## Output files

`LadderBundle.write(directory)` emits:

1. `main.csv`
2. One file per subroutine: `sub_<slug>.csv`

Rules:

- Subroutines are emitted in lexical order by subroutine name.
- Output directory is auto-created (`parents=True, exist_ok=True`).
- Existing files are overwritten.
- Slug generation:
  - Lowercase
  - Non-alphanumeric sequences become `_`
  - Leading/trailing `_` trimmed
  - Empty slug becomes `subroutine`
  - Collisions are suffixed (`_2`, `_3`, ...)

## CSV shape

- UTF-8 CSV (standard comma-separated, quoted as needed by CSV writer)
- Header is always present and exact:

```csv
marker,A,B,C,D,E,F,G,H,I,J,K,L,M,N,O,P,Q,R,S,T,U,V,W,X,Y,Z,AA,AB,AC,AD,AE,AF
```

- Exactly 33 columns per row:
  - `marker` + `A..AE` (31 condition columns) + `AF` (output token)

## Row semantics

- `marker`:
  - `R` => first row of a rung
  - `""` (blank) => continuation row of current rung
- `AF`:
  - exactly one token or blank
  - blank means no output token on that row

Rung segmentation rule for consumers:

- A rung starts at each row with `marker == "R"` and continues until the next `R` or EOF.

## Comment rows

Comment rows appear directly above the `R` row of the rung they annotate:

- `marker` = `#`
- Column `A` = comment text for that line
- No additional columns

Multi-line comments emit one `#` row per line. Example:

```csv
#,Initialize the light system.
#,Activates when Button is pressed.
R,X001,-,-,...,-,out(Y001)
```

Comment rows are metadata — consumers may ignore them or display them as rung annotations.

## Condition grid cell vocabulary (`A..AE`)

Cells can contain:

- Contact/operand tokens (for example `X001`, `DS10`, `C1`)
- Negated contact: `~X001`
- Edge contacts: `rise(X001)`, `fall(X001)`
- Comparison terms (for example `DS1!=0`, `DS1==5`, `DS1<DS2`)
- Wiring symbols:
  - `-` horizontal-only wire
  - `T` horizontal + vertical-down wire
  - `|` vertical-only wire (reserved; currently not emitted because exporter does not output empty vertical-only rows yet)
- Blank (`""`) empty cell

No shorthand markers (`->`, `...`) are emitted.
No explicit `+` topology token is emitted.

## OR / branch wiring semantics

### `any_of(...)` OR expansion

For OR-expanded condition terms:

- Split/merge marker column uses `T` on non-final stacked rows and `-` on the final stacked row.
- Only the top OR branch row carries trailing downstream condition terms.
- Lower OR continuation rows end at split/merge marker (with wire fill where applicable).

### `branch(...)` rows

Branch rows are continuation rows with normal instruction tokens in `AF`.

- Branch-local conditions are offset to the right of the parent split column.
- Parent split column is wired with `T` on non-final stacked rows and `-` on the final stacked row across parent + branch entry rows.
- Nested branches are not emitted (export error).

## Multi-output rung semantics

If one condition path has multiple output instructions, exporter emits stacked continuation rows:

- First row `marker = R`, then blank marker rows
- Split column uses `T` on non-final stacked rows and `-` on the final stacked row
- Each row has one `AF` token

## Builder pin continuation rows

Builder side conditions are emitted as continuation rows with dot tokens in `AF`:

- `.reset()`
- `.down()`
- `.clock()`
- `.jump(step)`
- `.jog()`

Pin rows are independent left-rail paths (not AND-chained through the parent output row conditions).

## For-loop lowering

`forloop(count, oneshot=...)` lowers to:

1. `for(count)` or `for(count,oneshot=1)` row (`marker=R`)
2. Body instruction rows (`marker=R` per emitted body instruction row)
3. Closing `next()` row (`marker=R`)

## Subroutine tail guarantee

Each subroutine CSV is guaranteed to end with `return()`:

- If last emitted instruction token is already `return()`, unchanged.
- Otherwise exporter appends an `R` row with `return()`.

## AF token format (canonical)

All tokens are compact canonical function-style strings:

- `name(pos1,pos2,key=val,...)`
- Positional args come first, then keyword args as `key=value`
- no extra whitespace
- dot pins as `.name(...)`

String rendering:

- Strings are double-quoted.
- Internal `"` is escaped as `""` (doubled quote). No backslash escaping.

Boolean rendering:

- `1` / `0`

`None` rendering:

- `none`

Collections:

- List/tuple-like values render as bracket lists, for example `[A,B]`, `[[1,0],[0,1]]`.

## Supported instruction tokens (v2)

Positional args stay positional. Keyword-only args use `key=value` syntax.
Conditional kwargs (marked with "if ≠0") are omitted when the value is the default (0).

Producer may emit:

- `out(target)` or `out(target,oneshot=1)`
- `latch(target)`
- `reset(target)`
- `copy(source,target)` or `copy(source,target,oneshot=1)`
- `blockcopy(source,dest)` or `blockcopy(source,dest,oneshot=1)`
- `fill(value,dest)` or `fill(value,dest,oneshot=1)`
- `calc(expression,dest,mode=decimal)` or `calc(...,mode=hex,oneshot=1)`
- `search("cond",value,range,result,found)` or `search(...,continuous=1,oneshot=1)`
- `pack_bits(bit_block,dest)` or `pack_bits(bit_block,dest,oneshot=1)`
- `pack_words(word_block,dest)` or `pack_words(word_block,dest,oneshot=1)`
- `pack_text(source_range,dest)` or `pack_text(...,allow_whitespace=1,oneshot=1)`
- `unpack_to_bits(source,bit_block)` or `unpack_to_bits(source,bit_block,oneshot=1)`
- `unpack_to_words(source,word_block)` or `unpack_to_words(source,word_block,oneshot=1)`
- `on_delay(done,acc,preset=N,unit=Tms)`
- `off_delay(done,acc,preset=N,unit=Tms)`
- `count_up(done,acc,preset=N)`
- `count_down(done,acc,preset=N)`
- `shift(bit_range)`
- `event_drum(outputs=[...],events=[...],pattern=[[...],...],current_step=X,completion_flag=X)`
- `time_drum(outputs=[...],presets=[...],unit=Tms,pattern=[[...],...],current_step=X,accumulator=X,completion_flag=X)`
- `send(target=X,remote_start="addr",source=X,sending=X,success=X,error=X,exception_response=X,count=N)`
- `receive(target=X,remote_start="addr",dest=X,receiving=X,success=X,error=X,exception_response=X,count=N)`
- `call("subroutine_name")`
  - Subroutine names must not contain `"`.
- `return()`
- `for(count)` or `for(count,oneshot=1)`
- `next()`

Pin tokens:

- `.reset()`
- `.down()`
- `.clock()`
- `.jump(step)`
- `.jog()`

Click supports additional instruction placeholders that pyrung does not currently emit:

- Empty instruction placeholder: `,:,...`
- NOP instruction placeholder: `,:,NOP`

## Operand normalization notes

- Tags render as mapped Click addresses (for example `X001`, `DS10`).
- Block ranges render either:
  - contiguous compact form `BANKstart..BANKend` (same bank, +1 sequence), or
  - explicit list form `[A,B,C]`.
- Indirect refs render as `BANK[pointer]` or `BANK[pointer+offset]` / `BANK[pointer-offset]`.
- Copy modifiers are emitted inline as nested operands:
  - `as_value(source)`
  - `as_ascii(source)`
  - `as_binary(source)`
  - `as_text(source,suppress_zero,pad,exponential,termination_code)`

## Immediate handling

Immediate operands are supported only in strict, explicit contexts.

Allowed condition-cell forms:

- `immediate(X001)`
- `~immediate(X001)`

Allowed AF token forms:

- `out(immediate(Y001))`
- `latch(immediate(Y001))`
- `reset(immediate(Y001))`
- `out(immediate(Y001..Y004))` (contiguous mapped range only)

Rules:

- `Tag.immediate` and `immediate(...)` wrapper style are both supported.
- Immediate is allowed only for:
  - direct rung contacts (normal and negated), and
  - `out(...)`, `latch(...)`, `reset(...)` target operands.
- Immediate is not allowed in:
  - edge contacts (`rise(...)`, `fall(...)`),
  - non-coil instruction operands (`copy`, `calc`, `search`, etc.).
- Immediate coil targets must resolve to `Y` bank addresses.
- Immediate-wrapped ranges must resolve to one contiguous address span to emit compact `BANKstart..BANKend` form.
  - Non-contiguous mappings fail strict validation/export with explicit diagnostics.

## Strict validation and failure mode

Before rendering, exporter runs strict Click validation and extra export checks.

On any issue:

- `LadderExportError` is raised
- includes structured `issues` entries (`path`, `message`, `source_file`, `source_line`)
- no CSV bundle is returned/written

## Consumer recommendations

1. Validate exact header and column count (33).
2. Parse in row order and preserve ordering semantics.
3. Treat `AF` as an opaque canonical token string unless your decoder intentionally parses token grammar.
4. Treat unknown future token names as extension points (fail closed if strict).
5. Segment rungs by `marker == "R"`.
