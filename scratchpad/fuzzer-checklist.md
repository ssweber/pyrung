# Fuzzer Checklist: Hypothesis Grammar-Based Generator

Inventory of every instruction, tag type, operand form, condition form,
wiring pattern, and degenerate value the fuzzer must cover. Reviewed before
implementation begins.

---

## 1. Instruction Types

### Phase 1 — Core (generate from day one)

| # | Instruction | Category | Signature highlights | Fuzzer notes |
|---|-------------|----------|---------------------|--------------|
| 1 | `out(tag, *, oneshot=)` | Coil | Bool target or coil range; resets when rung False | Immediate I/O target and oneshot variant are separate strategy weights |
| 2 | `latch(tag)` | Coil | Bool target or coil range; holds True until `reset()` | Always pair with a reset somewhere |
| 3 | `reset(tag)` | Coil | Any target or coil range; clears to default | Target can be Bool or numeric |
| 4 | `copy(src, dst, *, convert=, oneshot=)` | Data | Src: literal/tag/indirect. Dst: tag/indirect. Clamps overflow | Key wiring instruction; source of copy chains |
| 5 | `calc(expr, dst, *, oneshot=)` | Math | Expression tree → tag. Wraps on overflow, div-by-zero → 0 | Operators include `+ - * / // % ** & \| ^ << >> ~`, unary `+/-`, `abs()`, `PI`, `sqrt/sin/cos/tan/asin/acos/atan/log/log10/radians/degrees`, and Click shift/rotate funcs `lsh/rsh/lro/rro` |
| 6 | `fill(value, dest_range, *, oneshot=)` | Data | Single value → block range | Range from `.select()` |
| 7 | `blockcopy(src_range, dst_range, *, convert=, oneshot=)` | Data | Range → range, equal length | Indirect ranges stress address resolution |
| 8 | `on_delay(timer, preset, unit=)` | Timer | Acc counts up while enabled. Without `.reset()` → TON; with → RTON | Dynamic preset (tag) is high-value pattern |
| 9 | `off_delay(timer, preset, unit=)` | Timer | Done stays True until delay expires after rung False | Re-enable semantics differ from on_delay |
| 10 | `count_up(counter, preset)` | Counter | Acc increments every scan while enabled. Requires `.reset()`. Optional `.down()` for bidirectional | NOT edge-triggered; fuzzer must sometimes wrap in `rise()` |
| 11 | `count_down(counter, preset)` | Counter | Acc decrements; Done when acc ≤ -preset. Requires `.reset()` | Negative comparison semantics |
| 12 | `search(comparison, *, result, found, continuous=)` | Search | RangeComparison from `.select() op value`. Result: Int/Dint, Found: Bool | Continuous mode resumes from last position; CHAR/TXT search only supports `==` / `!=` |
| 13 | `shift(bit_range)` | Shift | `.clock(cond).reset(cond)`. Data bit from rung condition. Rising-edge clock | Direction set by range order vs `.reverse()` |
| 14 | `pack_bits(bit_range, dest)` | Packing | Bool range → Int/Word/Dint/Real register | Max 16 bits (Int/Word), 32 (Dint/Real) |
| 15 | `unpack_to_bits(source, bit_range)` | Packing | Int/Word/Dint/Real register → Bool range | Inverse of pack_bits |
| 16 | `pack_words(word_range, dest)` | Packing | 2× Int/Word → Dint/Real. Lo/Hi packing | Exactly 2 source elements |
| 17 | `unpack_to_words(source, word_range)` | Packing | Dint/Real → 2× Int/Word. Inverse of pack_words | Exactly 2 dest elements |

### Phase 2 — Extended (add after core is stable)

| # | Instruction | Category | Reason to defer |
|---|-------------|----------|-----------------|
| 18 | `forloop(count, *, oneshot=)` | Control | Captures nested instructions; `loop.idx` is an auto-generated DINT tag. Variable-count tags (including 0) are a known BFS stress point |
| 19 | `call(subroutine)` / `return_early()` | Control | `call()` accepts a string or `@subroutine` function; OTE-inside-conditional-subroutine is a known bug vector |
| 20 | `event_drum(...).reset(...).jump(...).jog(...)` | Sequencer | Complex terminal builder with pattern matrix, step tracking, and edge-bearing jog/event inputs |
| 21 | `time_drum(...).reset(...).jump(...).jog(...)` | Sequencer | Even more complex: per-step presets, accumulator tag, and jump/jog chaining |
| 22 | `pack_text(char_range, dest, *, allow_whitespace=)` | Packing | Requires CHAR/TXT blocks plus decimal/hex/float text parsing semantics |
| 23 | `receive(target=..., remote_start=..., dest=..., receiving=..., success=..., error=..., exception_response=...)` | Communication | Verifier-relevant despite simulation being inert for string targets: `prove()` auto-treats `dest` as nondeterministic |
| 24 | `run_function(fn, *, ins=, outs=, oneshot=)` | Callback | Opaque Python writer; needs a fixed callback corpus plus `choices=` / `min=/max=` annotations to stay tractable |
| 25 | `run_enabled_function(fn, *, ins=, outs=)` | Callback | Same as `run_function`, but executes every scan with the rung-enabled state passed in |

### Out of Scope

| Instruction | Reason |
|-------------|--------|
| `send()` | Network I/O; string-target form is simulation-inert, so it adds little value to BFS soundness beyond status-tag plumbing |
| `nop()` / `raw()` | Click passthrough; no-ops in runner |

---

## 2. Tag Types

| # | Type | Constructor | Range | Default | Retentive | Fuzzer role |
|---|------|-------------|-------|---------|-----------|-------------|
| 1 | BOOL | `Bool(name)` | True/False | False | No | Conditions, coils, edge detection |
| 2 | INT | `Int(name)` | -32768..32767 | 0 | Yes | Presets, accumulators, comparison targets |
| 3 | DINT | `Dint(name)` | -2^31..2^31-1 | 0 | Yes | Counter accumulators, wide arithmetic |
| 4 | REAL | `Real(name)` | IEEE 754 float | 0.0 | Yes | Analog values, rounding edge cases |
| 5 | WORD | `Word(name)` | 0..65535 | 0 | Yes | Bitwise ops, pack/unpack |
| 6 | CHAR | `Char(name)` | Single ASCII char | "" | Yes | Phase 2 (text instructions) |

### Compound Types

| # | Type | Construction | Fields | Fuzzer role |
|---|------|-------------|--------|-------------|
| 7 | Timer | `Timer.clone(name)` or `Timer.clone(name, count=N)` | `.Done` (Bool), `.Acc` (Int) | on_delay, off_delay operand |
| 8 | Counter | `Counter.clone(name)` or `Counter.clone(name, count=N)` | `.Done` (Bool), `.Acc` (Dint) | count_up, count_down operand |
| 9 | UDT runtime | `@udt()` / `.clone(...)` | Arbitrary named fields | Custom Done/Acc-compatible structs; structure-backed `choices=` domains |
| 10 | Named array runtime | `@named_array(...)` / `.clone(...)` | Interleaved per-instance fields | Whole-instance blockcopy / fill via `.instance()` / `.instance_select()` |

### Block Types

| # | Type | Construction | Indexing | Fuzzer role |
|---|------|-------------|----------|-------------|
| 11 | Block | `Block(name, type, start, end)` | Inclusive address space (often 1-based; generic blocks may start at 0) | General memory pool for tags; sparse `valid_ranges` is a phase-2 address stressor |
| 12 | InputBlock | `InputBlock(name, type, start, end)` | Inclusive address space, `LiveInputTag` | External inputs (ND dimension); `.immediate` contacts |
| 13 | OutputBlock | `OutputBlock(name, type, start, end)` | Inclusive address space, `LiveOutputTag` | Terminal outputs; `.immediate` coils |

### Tag Metadata (relevant to BFS domain inference)

| Metadata | Effect on verifier | Fuzzer bias |
|----------|-------------------|-------------|
| `choices={1: "A", 2: "B"}` | Finite domain | Generate for some Int/Dint tags; tests enum closure |
| `min=` / `max=` | Domain range cap (≤1000 values) | Generate for analog-like tags |
| `external=True` | Marks tag as ND input | Essential for BFS input enumeration |
| `readonly=True` | Zero writers enforced | Useful as static config values |
| `public=True` | Prevents some absorptions unless not projected | Generate for threshold/config tags that may or may not be projected |
| `final=True` | Marks single-writer terminal/config tags; influences some absorption paths | Generate for init-written thresholds/presets |
| `band={...}` | Predicate-based value grouping | Phase 2 |

---

## 3. Operand Forms

### Value Sources (can appear as instruction sources / condition operands)

| # | Form | Syntax | Resolution | Example |
|---|------|--------|------------|---------|
| 1 | Literal int | `42` | Immediate | `copy(0, tag)` |
| 2 | Literal float | `3.14` | Immediate | `copy(1.5, real_tag)` |
| 3 | Literal bool | `True` / `False` | Immediate | Condition constant |
| 4 | Literal string / char | `"HELLO"` / `"A"` | Immediate | `copy("00026", Txt[1])`, `search(Txt.select(1, 10) == "AB", ...)` |
| 5 | Tag reference | `DS[1]` or `Bool("X")` | SystemState lookup | `copy(DS[1], DS[2])` |
| 6 | Immediate tag ref | `immediate(X[1])` / `Y[1].immediate` | Bypass image table semantics | `Rung(X[1].immediate): out(Y[1].immediate)` |
| 7 | Indirect ref | `DS[ptr]` where `ptr` is a tag | Resolve ptr value → address at scan time | `copy(DS[idx], dest)` |
| 8 | Indirect expr ref | `DS[ptr + 1]` | Evaluate expression → address at scan time | `copy(DS[idx + offset], dest)` |
| 9 | Sub-field access | `timer.Done`, `timer.Acc`, `Recipe.Step` | Structure field dereference | `Rung(T1.Done): out(Alarm)` |
| 10 | Block range | `DS.select(1, 10)` / `.reverse()` | Static contiguous range | `fill(0, DS.select(1, 10).reverse())` |
| 11 | Indirect block range | `DS.select(start_tag, end_tag)` | Dynamic range bounds | `blockcopy(DS.select(a, b), ...)` |
| 12 | Named-array instance range | `Recipe.instance(2)` / `.instance_select(1, 3)` | Explicit ordered tag list | `blockcopy(Recipe.instance(2), WorkingRecipe.instance(1))` |
| 13 | Expression | `DS[1] + DS[2]`, `sqrt(X)`, `lsh(Word, 1)` | Arithmetic / function tree | `calc(DS[1] * 2 + 1, DS[3])` |
| 14 | Range sum | `DS.select(1, 5).sum()` | Sum of range elements | `calc(DS.select(1,5).sum(), total)` |

### Value Destinations (can appear as instruction targets)

| # | Form | Types | Notes |
|---|------|-------|-------|
| 1 | Tag reference | Any tag | `copy(src, DS[5])`; `copy("HELLO", Txt[1])` fans out sequentially across CHAR/TXT slots |
| 2 | Immediate target | `immediate(Y[1])` / `Y[1].immediate` | Coil family only (`out` / `latch` / `reset`) |
| 3 | Indirect ref | `DS[ptr]` | Address-error fault on OOB |
| 4 | Indirect expr ref | `DS[ptr + N]` | Evaluated each scan |
| 5 | Block range | `.select(start, end)` / named-array `.instance*()` | For `fill`, `blockcopy`, and coil range targets |
| 6 | Indirect block range | `.select(tag, tag)` | For `fill`, `blockcopy`, `shift` |

---

## 4. Condition Forms

| # | Form | Syntax | Semantics | Edge behavior |
|---|------|--------|-----------|---------------|
| 1 | Bit condition | `Rung(bool_tag)` | True when tag True | Level |
| 2 | Normally closed | `Rung(~bool_tag)` | True when tag False | Level |
| 3 | Immediate bit condition | `Rung(X[1].immediate)` | True from physical/immediate image | Level |
| 4 | Rising edge | `Rung(rise(tag))` / `Rung(rise(X[1].immediate))` | True on False→True transition only | Cross-scan; needs `_prev` |
| 5 | Falling edge | `Rung(fall(tag))` / `Rung(fall(X[1].immediate))` | True on True→False transition only | Cross-scan; needs `_prev` |
| 6 | Compare EQ | `Rung(tag == value)` / `Rung(tag == other_tag)` | Equality | Level |
| 7 | Compare NE | `Rung(tag != value)` | Inequality | Level |
| 8 | Compare LT | `Rung(tag < value)` | Less than | Level |
| 9 | Compare LE | `Rung(tag <= value)` | Less or equal | Level |
| 10 | Compare GT | `Rung(tag > value)` | Greater than | Level |
| 11 | Compare GE | `Rung(tag >= value)` | Greater or equal | Level |
| 12 | AND | `Rung(A, B)` or `And(A, B)` | All true | Composite |
| 13 | OR | `Or(A, B)` | Any true | Composite |
| 14 | Int-truthy | `Rung(int_tag)` | True when != 0 | Level |
| 15 | Indirect compare | `Rung(DS[ptr] == 5)` | Resolve ptr, then compare | Level + address resolution |
| 16 | Expression compare | `Rung((A + B) > 100)` | Evaluate expr, then compare | Level |

Direct tag contacts only support `BOOL` and `INT`. `DINT` / `REAL` / `WORD` /
`CHAR` require explicit comparisons.

---

## 5. Wiring Patterns That Have Historically Caused Bugs

Ordered by bug frequency / severity from changelog and test suite analysis.

### Tier 1 — Known soundness failures (must generate frequently)

| # | Pattern | Bug history | How to generate |
|---|---------|-------------|-----------------|
| 1 | **Timer Acc in downstream comparison** | Threshold absorption misclassified; 3+ fixes in v0.8 | `on_delay(T, preset)` in rung N, `Rung(T.Acc >= K)` in rung M |
| 2 | **Copy chain into comparison** | Backward propagation didn't follow multi-hop chains | `copy(T.Acc, DS[1])` then `Rung(DS[1] >= K)` |
| 3 | **Conditional write + edge read** | Elision removed tag that `rise()` needed cross-scan | `Rung(A): copy(1, DS[1])` + `Rung(rise(DS[1] > 0))` in another rung |
| 4 | **Dynamic preset (tag as preset)** | External preset default crosses threshold at init | `copy(src, preset_tag)` + `on_delay(T, preset_tag)` |
| 5 | **`receive()` dest consumed downstream** | Receive destinations were previously absorbed instead of treated as ND inputs | `receive(..., dest=Dest, ...)` + `Rung(Dest == 2)` |
| 6 | **OTE inside conditional subroutine** | Misclassified as combinational; WBR elision unsound | Phase 2 (requires subroutine generation) |
| 7 | **Exclusive inputs across scans** | Input group composition failure; groups not in cross-product | Two+ external Bool inputs with `rise()` in separate rungs |
| 8 | **Count-down with constant preset** | Threshold vector incorrect for negative comparison | `count_down(C, 5).reset(R)` + `Rung(C.Acc <= -3)` |
| 9 | **Bidirectional counter** | Same threshold vector bug | `count_up(C, 10).down(down_cond).reset(R)` |
| 10 | **Self-referencing accumulator** | Calc wrapping interacts with domain inference | `calc(DS[1] + 1, DS[1])` |
| 11 | **Truthy accumulator contact** | `Rung(T.Acc)` / downstream nonzero tests blocked absorption and dropped reachable Done states | `on_delay(T, 100)` + `Rung(T.Acc)` or `Rung(C.Acc > 0)` |
| 11a | **Init-guarded exhaustive single-writer block** | `~InitDone` gate writes a swath of tags exactly once; elision needs to recognise the guard as exhaustive and treat the writes as final. Drives the elision-checklist "write-coverage under exhaustive guards" item | `Rung(~InitDone): copy(0, A); copy(0, B); …; copy(1, InitDone)` followed by rungs that read A/B/etc. |
| 11b | **Char/state-string transitions** | Char tags driving a state machine via `copy("g", State)` and `Rung(State == "g")`; `==` against string literal is a distinct domain-inference path from int compare | Pool needs Char tags; emit `Rung(State == "g"): on_delay(T, P)` + `Rung(T.Done): copy("y", State)` rotation |
| 11c | **Timer-chain state advancement** | T2 enabled by T1.Done with explicit copy-into-state in between; nested hidden-event scheduling and absorption interaction | `Rung(T1.Done): copy(NEXT, State)` + `Rung(State == NEXT): on_delay(T2, P)` |

### Tier 2 — Known engine parity bugs

| # | Pattern | Bug history | How to generate |
|---|---------|-------------|-----------------|
| 12 | **Indirect copy source miss** | Compiled kernel didn't preserve address-fault classification | `copy(DS[ptr], dest)` where ptr can go OOB |
| 13 | **Copy converter modes** | Compiled converter disagreed on fault handling | `copy(src, dest, convert=to_value)` / `to_binary` with indirect source |
| 14 | **Block-element commit semantics** | Only written elements committed; compiled path got this wrong | `fill(0, DS.select(1, 5))` conditional on rung |
| 15 | **Oneshot output semantics** | `out(tag, oneshot=True)` wrote False vs entry-value after firing | `Rung(trigger): out(light, oneshot=True)` |
| 15a | **Oneshot copy semantics** | `copy(src, dest, oneshot=True)` is the SFC bread-and-butter pattern (`copy(1, ds.Trans, oneshot=True)`); compiled vs interpreted edge handling has historically diverged | `Rung(rise(Cmd)): copy(1, sub.Trans, oneshot=True)` |
| 15b | **Multi-hop copy chain (3+ hops)** | Existing pattern is single hop `copy(T.Acc, X)`. Real programs route through 2–4 intermediate registers (Click idiom: indirect indexing → mask → result). Backward propagation must follow each link without giving up | `copy(A, B)` + `copy(B, C)` + `copy(C, D)` + `Rung(D >= K)` |
| 15c | **Branch under parent rung** | Nested `with branch(cond): ...` — branch cond ANDs with parent rail. Coil emission inside a branch follows different scope rules than top-level rungs | `with rung(EstopOK): with branch(Running): out(Motor); with branch(Running): out(Light)` |

### Tier 3 — Structural patterns the BFS stresses

| # | Pattern | Why it matters | How to generate |
|---|---------|---------------|-----------------|
| 16 | **Chained timers** | T2 enabled by T1.Done; nested hidden-event scheduling | `Rung(T1.Done): on_delay(T2, preset)` |
| 17 | **Latch + conditional reset** | Counter/timer self-reset breaks absorption monotonicity | `count_up(C, 10).reset(C.Done)` or `Rung(C.Done): reset(C.Acc)` |
| 18 | **Copy chain (multi-hop)** | Each hop is a backward-propagation step | `copy(A, B)` + `copy(B, C)` + `Rung(C >= K)` |
| 19 | **Non-invertible calc** | `%`, `&`, `\|`, `**`, shifts/rotates can block backward propagation; metadata fallback must stay sound | `calc(DS[1] % 3, DS[2])` + `Rung(DS[2] == 0)` |
| 20 | **Calc with overflow** | Int wraps at 32767; Dint at 2^31-1 | `calc(DS[1] + 30000, DS[2])` with DS[1] near INT_MAX |
| 21 | **Pointer/indirect in dest** | Address computed at runtime; OOB = fault | `copy(value, DS[ptr])` |
| 22 | **ForLoop with count=0** | Zero iterations; children never execute | Phase 2 |
| 23 | **Tags with `choices=`** | BFS uses enum-closure domain inference | `Int("Mode", choices={1: "A", 2: "B", 3: "C"})` |
| 24 | **Tags with `min=/max=`** | BFS uses range domain; boundary ±1 values matter | `Int("Level", min=0, max=100)` |
| 25 | **Fill into later comparison** | Backward propagation now crosses `fill()`; each written element is a potential threshold sink | `fill(Level, DS.select(1, 3))` + `Rung(DS[2] == 75)` |
| 26 | **Blockcopy into later comparison** | Backward propagation now crosses range copies | `blockcopy(Src.select(1, 3), Dst.select(1, 3))` + `Rung(Dst[2] == 75)` |
| 27 | **Opaque callback output with metadata** | Unsupported writers should widen to `choices=` / `min=/max=` rather than go unsound | `run_function(fn, outs={"result": Mode})` + downstream compare |
| 28 | **Drum jog/event edges** | `event_drum` jog/events are edge-bearing ND inputs, not free inputs | `event_drum(...).reset(Rst).jog(Jog)` |
| 29 | **Range-sum aggregation into compare** | `calc(block.select(a, b).sum(), Total)` + `Rung(Total != 0)` is the AlarmExtent idiom from `examples/fault_coverage.py` and `packml_bench.py`. Stresses the operand form #14 (range sum) plus backward propagation through aggregation | `calc(AlarmInts.select(1, 4).sum(), AlarmExtent)` + `Rung(AlarmExtent != 0)` |
| 30 | **`band=` collapse interaction** | A tag with `band={"ZERO": 0, "POSITIVE": ">0"}` collapses values post-BFS but is read by downstream rungs as raw value; lock projection vs live read must agree | `Int("AlarmExtent", band={"ZERO": 0, "POSITIVE": ">0"})` driven by range sum, then both `Rung(AlarmExtent != 0)` and lock projection assertions |
| 31 | **Indirect ptr OOB on source AND dest** | Existing #21 covers dest OOB; symmetrically `copy(DS[ptr], Z)` with ptr ≤ 0 or ptr > end faults on the source side. Compiled kernel's address-fault classification has diverged here | `Rung(Cond): copy(DS[Ptr], Z)` where Ptr's domain spans both valid and OOB |
| 32 | **Identity / self-cancelling calc** | `calc(X + 0, Y)`, `calc(X * 1, Y)`, `calc(X * 0, Y)`, `calc(X - X, Y)`. Constant-folding and absorption must collapse these without dropping the underlying read; backward propagation should still see X is observed | `Rung(Cond): calc(X + 0, Y)` + `Rung(Y == K)` |

---

## 6. Degenerate / Boundary Values to Bias Toward

### Timer Presets

| Value | Why | Expected behavior |
|-------|-----|-------------------|
| 0 | Done immediately on first enabled scan | Acc clamps at 0; Done = True |
| 1 | Done on second scan (1ms dt) | Minimal accumulation before crossing |
| 100 | Normal small preset | Standard behavior baseline |
| 32767 | INT_MAX | Acc clamps here; tests clamping logic |

### Timer Units / Aliases

| Value | Why |
|-------|-----|
| `"ms"` / `"Tms"` | Default unit + Click-style alias |
| `"sec"` / `"Ts"` | Coarser tick conversion |
| `"min"` / `"Tm"` | Minute-scale accumulation |
| `"hour"` / `"Th"` | Large-step unit conversion |
| `"day"` / `"Td"` | Widest valid built-in unit |

### Counter Presets

| Value | Why | Expected behavior |
|-------|-----|-------------------|
| 0 | Done immediately | Acc ≥ 0 from start |
| 1 | Done on first counting scan | Minimal counting |
| 10 | Normal small preset | Standard baseline |

### Comparison Boundaries

| Value | Why |
|-------|-----|
| 0 | Default value for most numeric tags; tests "tag never written" path |
| 1 | Off-by-one vs default |
| -1 | Sign boundary for Int/Dint |
| 32767 | INT_MAX; wrapping boundary for Int |
| 32768 | INT_MAX + 1; wraps to -32768 for Int |
| -32768 | INT_MIN for Int |
| 65535 | WORD_MAX |

### Calc Operands

| Pattern | Why |
|---------|-----|
| `tag + 0` | Identity; should be optimizable |
| `tag * 0` | Always zero; tests constant folding |
| `tag * 1` | Identity |
| `tag / 0` | Div-by-zero → result=0, fault flag set |
| `tag % 1` | Always 0 |
| `tag - tag` | Self-cancellation |
| `32767 + 1` | Int overflow → wraps to -32768 |
| `tag ** 2` | Nonlinear reverse propagation fallback |
| `lsh(tag, 1)` / `rro(word, 1)` | Click-specific shift/rotate expression paths |

> ~~**Fuzzer coverage gap:**~~ Resolved — `calc_tag_tag` emits `tag <op> tag` forms
> (add, sub, mul, mod, bitand, bitor, bitxor) and `calc_shift` emits `lsh/rsh/lro/rro`.

### Calc Tag-Tag Binary Forms

| Pattern | Why |
|---------|-----|
| `tag1 + tag2` | Two-tag add, both reads must propagate |
| `tag1 - tag2` | Asymmetric reads; sign matters |
| `tag1 * tag2` | Two-tag mul; either operand zero collapses |
| `tag % tag2` | Non-invertible; div-by-zero on second operand |
| `tag1 & tag2` | Word/bitwise interaction across two reads |

### Pointer/Address Values

| Value | Why |
|-------|-----|
| Block start | Lower bound (not always 1 on generic `Block`) |
| Block end | Upper bound |
| 0 | Below valid range; address fault |
| Block end + 1 | Above valid range; address fault |

### Search Resume Seeds

| Value | Why |
|-------|-----|
| `0` in `result` | `continuous=True` restart sentinel |
| `-1` in `result` | `continuous=True` exhausted sentinel |
| First matching address | Resume should skip current hit and continue |
| Last address in range | Resume should terminate cleanly |

### Copy Source/Dest Type Combinations

| Source Type | Dest Type | Behavior |
|-------------|-----------|----------|
| Int → Int | Same width | Direct copy |
| Int → Dint | Widening | Sign extension |
| Dint → Int | Narrowing | Clamps to ±32767 |
| Real → Int | Float→int | Truncates + clamps |
| Int → Real | Int→float | Exact (within float precision) |
| Word → Int | Unsigned→signed | Reinterpret; values > 32767 wrap |
| Bool → Int | Bool→numeric | 0 or 1 |

### Copy Converter / Text Inputs

| Pattern | Why |
|---------|-----|
| `convert=to_value` with `"7"` | Baseline text→numeric conversion |
| `convert=to_ascii` with `"A"` | Face value vs ASCII code divergence |
| `convert=to_binary` with indirect numeric source | Compiled parity + low-byte char conversion |
| `convert=to_text(suppress_zero=False)` | Fixed-width leading-zero formatting |
| `convert=to_text(exponential=True)` | REAL exponential rendering |
| `convert=to_text(termination_code=0)` / `"$0D"` | NUL / hex termination code handling |
| `"1A3"` into `to_value` | Out-of-range fault; no partial numeric write |
| `"00026"` copied to CHAR/TXT | String literal fan-out across sequential tags |

### `pack_text()` Inputs

| Pattern | Why |
|---------|-----|
| `"ABCD"` into `WORD` | Hex parse path |
| `"1e-2"` into `REAL` | Float/exponential parse path |
| `" 12"` with `allow_whitespace=True` | Trimmed numeric parse |
| `" 12"` with `allow_whitespace=False` | Out-of-range fault path |

### ForLoop Counts

| Value | Why |
|-------|-----|
| 0 | Zero-iteration path |
| 1 | Minimal non-empty loop |
| Small INT tag domain | Exercises dynamic count and `loop.idx` |

---

## 7. Execution Backends for Agreement Checks

### Mode 1: Optimization Soundness

```
optimized  = prove(program, condition)                       # default
unoptimized = prove(program, condition, _skip_optimizations=True)
```

`_skip_optimizations=True` disables:
- Accumulator absorption (redundant + threshold)
- Scan-local state elision (abstract + concrete)
- Domain absorption fallback in classify

Does NOT disable BFS-time optimizations (live input pruning, edge compression, etc.).

Agreement check: if optimized returns `Proven`, unoptimized must not return `Counterexample`.

### Mode 2: Engine Parity at BFS States

Two execution backends:
- **Interpreted**: `PLC` class → `step()` → `_scan_steps()` → rung-by-rung evaluation via `ScanContext`
- **Compiled**: `CompiledPLC` class → `step()` → compiled `step_fn` mutating plain dicts

The BFS internally uses `_step_compiled_kernel()` from `prove/kernel.py`. For engine parity, we need to:
1. Call `reachable_states(program)` to enumerate all states
2. At each state, snapshot the kernel, run one scan through both backends, diff tag values

Existing pattern in `tests/core/test_compiled_replay.py`: `_assert_compiled_kernels_match()`.

### Mode 3: Full 3-Way Oracle

All three must agree:
- Interpreted PLC scan produces same tag values as compiled kernel at each state
- prove() optimized and unoptimized agree on verdict
- Reachable state sets match

Implementation note: `prove()` currently exposes `_skip_optimizations=True`,
but `reachable_states()` does not. A true optimized-vs-unoptimized state-set
comparison needs an internal harness around `_build_explore_context()` /
`_bfs_explore()` (or an added helper), not just the public `reachable_states()`
API.

---

## 8. Existing Infrastructure to Wire Into

### Markers (pyproject.toml)

Already registered:
- `hypothesis` — property-based tests
- `soundness` — expensive agreement tests

Need to add:
- `fuzz` — grammar fuzzer (superset marker)
- `parity` — engine parity tests
- `oracle` — full 3-way agreement tests

### Make Targets

Existing: `test-hypothesis`, `test-soundness`

To add:
- `test-fuzz` — all fuzzer tests (`-m fuzz`)
- `test-parity` — engine parity only (`-m parity`)
- `test-oracle` — 3-way oracle only (`-m oracle`)

### Prove Agreement Oracle

`tests/core/analysis/conftest.py` — `--prove-agreement` flag auto-runs unoptimized on every `Proven` result. The fuzzer's Mode 1 essentially does this per-generated-program.

### Hypothesis Settings

Existing hypothesis tests use `@settings(max_examples=200)`. Fuzzer should define profiles:
- CI: `max_examples=50`, `deadline=None`
- Local: `max_examples=1000` or time-based

---

## 9. Property Strategies for `prove()`

The fuzzer needs to generate properties to verify. Strategies:

| # | Property shape | Example | Notes |
|---|---------------|---------|-------|
| 1 | Output always False | `prove(program, OutputTag == False)` | Can it ever turn on? |
| 2 | Output always True | `prove(program, OutputTag == True)` | Once on, stays on? |
| 3 | Mutual exclusion | `prove(program, ~And(A, B))` | Two outputs never both True |
| 4 | Reachability | Expect `Counterexample` for `prove(program, OutputTag == False)` when output should be reachable | Confirms BFS finds the path |
| 5 | Comparison bound | `prove(program, DS[1] < 100)` | Value stays in range? |
| 6 | Counter done | `prove(program, ~Counter.Done)` | Counter never finishes? (should be Counterexample) |
| 7 | Receive-driven alarm | `prove(program, ~Alarm)` with `receive(..., dest=Dest)` and `Rung(Dest == K)` | Confirms ND receive domains flow through |
| 8 | Search hit/miss invariant | `prove(program, Or(~Found, Result >= 1))` | Exercises `search()` result/found coupling |
| 9 | Range-sum compare | `prove(program, AlarmExtent != 0)` paired with #29 emission | Confirms backward propagation crosses `block.select(...).sum()` |
| 10 | Init-done invariant | `prove(program, Or(~InitDone, AnyInitTag == InitValue))` | Confirms once-only init writes are visible to absorption |
| 11 | State-string reachability | `prove(program, State == "g")` with the Char state-machine pattern | Confirms `==` against string literal participates in domain inference |

For the agreement oracle, the property result doesn't matter — only that optimized and unoptimized agree. So we can use simple properties like `prove(program, some_output_tag)`.

---

## 10. Open Questions

1. **Real-valued state keys**: Known to cause BFS non-termination (T-1, T-5 in soundness matrix). Should the fuzzer avoid Real tags in stateful positions, or intentionally generate them with `max_states` safety net?

2. **Word/bitwise + Dint overflow parity**: Known gaps (T-2, T-6). Generate to find more, or defer?

3. **Conditional reset monotonicity** (Test 5 in matrix): Known unfixed. Should the fuzzer mark these as `xfail`, or generate them to track progress?

4. **Input group composition** (adversarial-bfs edge cases): The `joint_inputs` / `exclusive_inputs` parameters interact with free inputs in known-broken ways. Should the fuzzer exercise these parameters, or stick to default?

5. **Callback corpus**: For `run_function()` / `run_enabled_function()`, do we want a tiny built-in library of pure callbacks (identity, bounded enum, bounded range), or leave them as explicit non-goals for v1?

6. **`receive()` in parity mode**: The verifier treats receive destinations as nondeterministic, but the runtime path is inert without live I/O. Should the first generator use `receive()` only for soundness/oracle modes, or build a replay harness for parity too?

---

## 11. Pure-Function Invariant Tests (companion suite)

The grammar fuzzer above generates whole programs and exercises BFS soundness +
engine parity. A complementary suite tests the underlying kernel functions
directly. These are cheaper to run, shrink to single-value examples, and catch
bugs *before* BFS gets involved. Lifted from
`scratchpad/hypothesis-testing-opportunities.md`.

### Tier 1 — Pure functions (use `@given` directly)

| # | Target | Source | Invariant |
|---|--------|--------|-----------|
| U1 | `_store_copy_value_to_tag_type` | `core/instruction/conversions.py:169` | Result always in tag-type range; clamping idempotent |
| U2 | `_truncate_to_tag_type` | `core/instruction/conversions.py:192` | `truncate(x, INT) == ((x + 32768) % 65536) - 32768`; non-finite → 0 |
| U3 | `_math_out_of_range_for_dest` | `core/instruction/conversions.py:255` | True iff truncate would change value (oracle agreement) |
| U4 | `_rotate_left_16` / `_rotate_right_16` | `core/expression.py:444` | `rro(lro(v, n), n) == v & 0xFFFF`; `lro(v, 16) == v & 0xFFFF`; associative |
| U5 | `_int_to_float_bits` / `_float_to_int_bits` | `core/instruction/conversions.py:34` | Round-trip identity in both directions for finite floats / 32-bit uints |

### Tier 2 — Stateful machines (use `RuleBasedStateMachine`)

| # | Target | Source | Invariant |
|---|--------|--------|-----------|
| U6 | Counter Acc clamping | `core/instruction/counters.py` | Acc ∈ DINT range after any rule sequence; `done == (acc >= preset)` for CTU; `done == (acc <= -preset)` for CTD; reset is absolute |
| U7 | Timer fractional accumulation | `core/instruction/timers.py:67` | Acc monotone while enabled; clamp at 32767; total accumulated == sum of `dt_to_units(dt)`; `done == (acc >= preset)` |
| U8 | Drum step machine | `core/instruction/drums.py:228` | Step always in `[1, step_count]`; outputs equal `pattern[step-1]`; reset is authoritative; held event advances at most once |

### Why both suites

The grammar fuzzer can find an unsound elision that produces a wrong verdict
but cannot tell you which kernel primitive is broken. The unit suite localises
to the function. Run unit tests in CI on every PR; run the fuzzer on a slower
cadence with higher `max_examples`.

---

## Review Checklist

Before implementation:

- [x] Every instruction from Section 1 Phase 1 has a Hypothesis strategy
- [x] Every oneshot-capable instruction has explicit strategy coverage (not just `out()`)
- [x] `out(tag, oneshot=True)` AND `copy(src, dest, oneshot=True)` are emitted (Tier 2 #15, #15a)
- [x] Every tag type from Section 2 appears in the tag pool — including Char (Tier 1 #11b)
- [x] Every operand form from Section 3 is reachable (with appropriate weights) — including range-sum `.select(a, b).sum()` (Tier 3 #29)
- [x] Calc strategy emits `tag <op> tag`, identity forms, and `lsh/rsh/lro/rro` (Section 6 fuzzer-coverage-gap note)
- [x] Tier 1 #11a init-guarded single-writer block pattern emitted
- [x] Tier 1 #11b Char state-machine pattern emitted (needs Char in pool)
- [x] Tier 1 #11c timer-chain advancement pattern emitted
- [x] Tier 2 #15b multi-hop copy chain (3+ hops) pattern emitted
- [x] Tier 2 #15c branch-under-rung pattern emitted
- [x] Tier 3 #29 range-sum into compare pattern emitted
- [x] Tier 3 #30 `band=` collapse pattern emitted
- [x] Tier 3 #31 indirect OOB on source pattern emitted
- [x] Tier 3 #32 identity / self-cancelling calc pattern emitted
- [x] Boundary values from Section 6 are in the shrink-friendly value sets
- [ ] All three agreement modes from Section 7 have test functions (Mode 3 still requires the `_build_explore_context()` harness)
- [x] Markers and make targets from Section 8 are wired up
- [ ] `receive()` / callback-backed instructions are either covered by explicit strategies or documented as deferred
- [x] Copy-converter and `pack_text()` modes are represented somewhere in the generator corpus
- [x] Section 11 unit-invariant suite exists alongside the grammar fuzzer (Tier 1 U1–U5)
