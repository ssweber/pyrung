# Click Ladder CSV Contract (v1)

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

## Condition grid cell vocabulary (`A..AE`)

Cells can contain:

- Contact/operand tokens (for example `X001`, `DS10`, `C1`)
- Negated contact: `~X001`
- Edge contacts: `rise(X001)`, `fall(X001)`
- Comparison terms (for example `DS1!=0`, `DS1==5`, `DS1<DS2`)
- Wiring symbols:
  - `-` horizontal wire
  - `T` top of vertical stack
  - `+` middle vertical pass-through
- Blank (`""`) empty cell

No shorthand markers (`->`, `...`) are emitted.

## OR / branch wiring semantics

### `any_of(...)` OR expansion

For OR-expanded condition terms:

- Split/merge marker column uses vertical stack markers `T`, `+`, `-`.
- Only the top OR branch row carries trailing downstream condition terms.
- Lower OR continuation rows end at split/merge marker (with wire fill where applicable).

### `branch(...)` rows

Branch rows are continuation rows with normal instruction tokens in `AF`.

- Branch-local conditions are offset to the right of the parent split column.
- Parent split column is wired with `T/+/-` across parent + branch entry rows.
- Nested branches are not emitted (export error).

## Multi-output rung semantics

If one condition path has multiple output instructions, exporter emits stacked continuation rows:

- First row `marker = R`, then blank marker rows
- Split column uses `T/+/-`
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

1. `for(count,oneshot)` row (`marker=R`)
2. Body instruction rows (`marker=R` per emitted body instruction row)
3. Closing `next()` row (`marker=R`)

## Subroutine tail guarantee

Each subroutine CSV is guaranteed to end with `return()`:

- If last emitted instruction token is already `return()`, unchanged.
- Otherwise exporter appends an `R` row with `return()`.

## AF token format (canonical)

All tokens are compact canonical function-style strings:

- `name(arg1,arg2,...)`
- no extra whitespace
- dot pins as `.name(...)`

String rendering:

- Strings are double-quoted.
- Internal `\` and `"` are escaped as `\\` and `\"`.

Boolean rendering:

- `1` / `0`

`None` rendering:

- `none`

Collections:

- List/tuple-like values render as bracket lists, for example `[A,B]`, `[[1,0],[0,1]]`.

## Supported instruction tokens (v1)

Producer may emit:

- `out(target,oneshot)`
- `latch(target)`
- `reset(target)`
- `copy(source,target,oneshot)`
- `blockcopy(source,dest,oneshot)`
- `fill(value,dest,oneshot)`
- `calc(expression,dest,mode,oneshot)`
- `search("cond",value,range,result,found,continuous,oneshot)`
- `pack_bits(bit_block,dest,oneshot)`
- `pack_words(word_block,dest,oneshot)`
- `pack_text(source_range,dest,allow_whitespace,oneshot)`
- `unpack_to_bits(source,bit_block,oneshot)`
- `unpack_to_words(source,word_block,oneshot)`
- `on_delay(done,acc,preset,unit,has_reset)`
- `off_delay(done,acc,preset,unit)`
- `count_up(done,acc,preset)`
- `count_down(done,acc,preset)`
- `shift(bit_range)`
- `event_drum(outputs,events,pattern,current_step,completion_flag)`
- `time_drum(outputs,presets,unit,pattern,current_step,accumulator,completion_flag)`
- `send("host",port,"remote_start",source,sending,success,error,exception_response,device_id,count)`
- `receive("host",port,"remote_start",dest,receiving,success,error,exception_response,device_id,count)`
- `call("subroutine_name")`
- `return()`
- `for(count,oneshot)`
- `next()`

Pin tokens:

- `.reset()`
- `.down()`
- `.clock()`
- `.jump(step)`
- `.jog()`

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

## Immediate (`.immediate`) handling

`ImmediateRef` is not part of this CSV contract and is not emitted as a token.

- In rung conditions, `.immediate` is rejected earlier by the DSL (`Rung(...)` expects `Condition` or `Tag`).
- In instruction operands, `.immediate` reaches export as an unsupported operand and raises `LadderExportError`.

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
