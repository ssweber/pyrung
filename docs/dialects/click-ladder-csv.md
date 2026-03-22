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
- Wire-down prefixed contacts: `T:X001`, `T:~X002`, `T:rise(C1)` — the contact token
  carries a `T:` prefix indicating the contact also has a vertical-down wire exit
  (used on non-final OR branches at mid-rung positions)
- Wiring symbols:
  - `-` horizontal-only wire
  - `T` horizontal + vertical-down wire (T-junction)
  - `|` vertical-only pass-through wire (OR output bus, middle rows)
- Blank (`""`) empty cell

No shorthand markers (`->`, `...`) are emitted.
No explicit `+` topology token is emitted.

## Vertical topology

OR conditions (`any_of`), multi-output stacking, and `branch()` conditions all use
continuation rows within a rung. These mechanisms share the same vertical space — OR
branch rows double as multi-output and branch rows.

### Bus patterns

Two column patterns control vertical connectivity:

**Convergent bus (OR merge)** — gathers parallel branches into a single downstream path:

- `T` on the first row (right + down)
- `|` on middle rows (vertical pass-through — up + down only, no right exit)
- blank on the last row (bus terminates)

Middle and last rows reach downstream columns by routing up through `|` to the `T`, then
right. This ensures only the first row's horizontal path continues directly.

**Divergent bus (output split)** — distributes one condition path to multiple output rows:

- `T` on non-final rows (right + down)
- `-` on the final row (right only)

Every row has a right exit to its own AF output token.

**Contact input bus (T: prefix)** — an OR variant where the contacts themselves carry the
vertical bus instead of a separate wire column:

- `T:` prefix on non-final branch contacts (right + down)
- Bare contact on the final branch (right only)
- Contacts at column 0 (power rail) are always bare — the rail connects all rows.

The `T:` prefix applies to any contact token: `T:X002`, `T:~C1`, `T:rise(X001)`, `T:DS1==5`.

### T-junction physical model

A `T` cell's vertical wire extends from the left edge of its cell downward. This wire
connects to either:

- A cell directly below in the same column, or
- The right edge of a contact in the column to the left (when the cell below is blank)

This diagonal adjacency is how contacts on continuation rows connect upward to merge/split
columns even when their own column has no marker below.

### Continuation row rules

- Only the first row (`marker = R`) carries the full condition path from power rail to output.
- Continuation rows carry only their OR-branch-local contacts and branch-local conditions.
  Shared AND-prefix contacts from earlier columns are not repeated.
- Each continuation row has at most one AF token or is blank in AF.

## `any_of(...)` OR expansion

### Simple OR at power rail

`any_of(X001, X002, X003)`:

```
     A      B     …    AF
R    X001   T     -…-  out(Y001)
     X002   |
     X003
```

Contacts at col 0 are bare (power rail). Convergent bus at col B: T / | / blank.

### Mid-rung OR

`X001, any_of(X002, C1)`:

```
     A      B       C     …    AF
R    X001   T:X002  T     -…-  out(Y001)
            C1
```

T: prefix on X002 (non-final, mid-rung). Convergent bus at col C: T / blank.
Shared AND-prefix (X001) appears only on the first row.

### Series ORs

`any_of(X001, X002), any_of(C1, C2)`:

```
     A      B     C      D     …    AF
R    X001   T     T:C1   T     -…-  out(Y001)
     X002   -     C2
```

First OR at power rail (bare contacts, convergent bus at col B). Second OR mid-rung
(T: prefix on C1, convergent bus at col D). Continuation rows merge — X002 and C2
share the same row, connected by `-` wire at col B.

## Multi-output stacking

Multiple output instructions from the same condition path stack vertically using a
divergent bus:

`X001, X002` → `out(Y001)`, `latch(Y002)`, `reset(Y003)`:

```
     A      B      C     …    AF
R    X001   X002   T     -…-  out(Y001)
                   T     -…-  latch(Y002)
                   -     -…-  reset(Y003)
```

Divergent bus at col C: T / T / -. Each row continues right to its own output.

## `branch(...)` conditions

Branch-local conditions are placed on continuation rows to the right of the output
split column. The branch contact carries a `T:` prefix when it is not on the final row,
maintaining the vertical bus for rows below it.

`X001, X002` → `out(Y001)`, `branch(C1): out(Y002)`, `out(Y003)`:

```
     A      B      C     D      …    AF
R    X001   X002   T     T      -…-  out(Y001)
                   T     T:C1   -…-  out(Y002)
                   -     -      -…-  out(Y003)
```

Divergent bus at col C: T / T / -. Col D: T on row 0, T:C1 on row 1 (branch condition
with down-wire), `-` on row 2. The T: prefix on C1 ensures the down-wire continues to
row 2 (dropping C1 from conditions) so row 2 receives the parent condition without C1.

Nested branches are not emitted (export error).

## Combined OR + multi-output + branch

When a rung has both OR conditions and multiple outputs/branches, they share the same
set of continuation rows. The OR branches provide the rows needed for the outputs.

`X001, any_of(X002, X003, X004)` → `out(Y001)`, `branch(C1): out(Y002)`, `out(Y003)`:

```
     A      B        C     D      …    AF
R    X001   T:X002   T     T      -…-  out(Y001)
            T:X003   |     T:C1   -…-  out(Y002)
            X004           -      -…-  out(Y003)
```

Col B: 3-way OR input bus (T: / T: / bare). Col C: convergent bus (T / | / blank).
Col D: divergent bus with branch condition (T / T:C1 / -).

The 3 OR branches provide the 3 rows needed for the 3 outputs. C1 slots in as a
branch-local condition on the middle row. Middle OR rows reach the output split by
routing up through `|` to `T`, then right and back down through the divergent bus.

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
- `search(range cond value,result,found)` or `search(...,continuous=1,oneshot=1)`
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
- `raw(ClassName,hex)` — opaque instruction passthrough for binary round-trip fidelity.
  `ClassName` is the Click binary class name (unquoted) and `hex` is the raw blob as a
  hex string. Runtime no-op; preserved so CSV → DSL → CSV round-trips losslessly for
  unrecognized instruction types.

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
- Copy converters are emitted as a `convert=` kwarg on the instruction:
  - `convert=to_value`
  - `convert=to_ascii`
  - `convert=to_binary`
  - `convert=to_text(suppress_zero=<0|1>,exponential=<0|1>,termination_code=<none|N>)`

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
