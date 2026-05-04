# prove BFS optimization checklist

Reference from design review conversation. Use this to verify implementation correctness.

## 1. Free input elision — state key reduction

**Core insight:** ND inputs without edge semantics can take any value on any scan. Their current value does not constrain future behavior. Two states differing only in free input values have identical successor sets and can be merged.

**Partition logic — classify every ND input:**

- **Edge-bearing (keep in state key):** input appears in any of:
  - `rise()` expression
  - `fall()` expression
  - `oneshot=True` condition
  - `.clock()` argument on a shift register (implicit rising edge)
  - `.jog()` argument on a drum (implicit rising edge)
  - `.jump()` argument on a drum (likely edge-triggered — verify)
  - Drum event conditions (edge-triggered per Click spec — verify)
- **Free / combinational (mask to sentinel in state key):** everything else

**Things to verify:**
- `_once_*` memory keys for oneshot are in `memory_key_names` and stay in the state key. If not, oneshot re-fire prevention breaks and merge becomes unsound.
- The partition scan covers all builder condition arguments on shift registers and drums, not just rise/fall/oneshot.
- Assignment generation (`_iter_input_assignments`) is unchanged — still enumerates all live inputs. Only the state key changes.
- Liveness cache unchanged — liveness determines enumeration, not key membership.

**Soundness argument:**
1. Free ND inputs can take any value next scan — current value is not a constraint.
2. Edge-bearing inputs remain in key — rise/fall semantics preserved.
3. Oneshot memory keys remain in key — re-fire prevention preserved.
4. Merging over-approximates (explores unreachable combos) but never under-approximates. Consistent with existing soundness contract.

**PackML bench impact:** 17 ND inputs → 2 edge-bearing, 15 free. State space collapses from ~52K to ~4-8K.

## 2. Exclusive family canonicalization — assignment reduction

**Orthogonal to elision.** Elision reduces states (BFS nodes). Exclusive families reduce assignments per state (edges per node).

- Detect encoder-style external bool families (e.g., CmdReset..CmdComplete — only one active at a time)
- Enumerate canonical none-or-one-hot assignments instead of raw 2^N combinations
- Static inference from rung structure where inputs are pivots in mutually exclusive branches
- Manual `exclusive_group` annotation for constraints the static pass can't infer (physical wiring, Modbus protocol guarantees)

**Check:** exclusive groups affect enumeration only, not the state key (post-elision, free inputs aren't in the key anyway).

## 3. Blockless kernel — step cost reduction

**Problem:** Block sync copies thousands of tags ↔ block arrays every BFS step. 66% of step cost on PackML bench.

**Preferred approach:** Compiler flag (`blockless=True`) that emits direct tag dict access instead of block array access at codegen time. Avoids fragile regex post-processing.

**What to emit:**
- Static access: `tags["DS43"] = value` / `tags.get("DS43", 0)` instead of `_b_DS[42]`
- Dynamic access: `tags[_b_DS_names[expr]]` with injected name-lookup tuples
- Remove `_b_XX = blocks["_b_XX"]` assignments

**If using regex post-process approach instead, check:**
- Write vs read detection (LHS of assignment = write, everything else = read)
- Process writes first (full-line match), then reads (inline replacement)
- No nested block access like `_b_DS[_b_DS[i]]`
- No multi-line expressions with block access
- `_store_copy_value_to_type` helper — does it receive block var as argument?

## 4. State key minimization — what else to elide

Beyond free inputs, verify these are excluded from the state key:

- **Written-before-read (WBR):** tags unconditionally written before any read in the same scan
- **Terminals:** only written, never read — physical outputs
- **Non-retentive `out()` coils:** `out()` writes unconditionally every scan, previous value never matters
- **Deterministic projections:** tags whose value is a pure function of other tags already in the key (e.g., `State_Clearing` through `State_Completed` are projections of `StateCurrent` via `sm_map_val2_state`)
- **Scan-local intermediates:** tags written and consumed within a single subroutine call chain (e.g., `CmdValidIdx`, `CmdMask`, `StateMaskIdx`, `StateMask`, `CmdValidResult`, `StateMaskResult`). Subroutine-aware WBR pass needed to catch these across `call()` boundaries.
- **Monotonic convergence tags:** e.g., `LoopIndex` — only increments, resets on state change, determined by scan count in current state

**PackML bench result:** only 4 tags needed in key after full elision:
1. `StateCurrent` (17-value state machine)
2. `UnitModeCurrent` (3-value mode)
3. `CmdChgRequest` prev value (for `rise()` detection)
4. `ModeChgRequest` prev value (for oneshot edge detection)

## 5. Send/receive — implicit external inference

`receive()` destination tags are inherently nondeterministic. They should be automatically inferred as `external` without requiring user annotation.

**Check:**
- Tags appearing as `dest` in a `receive()` call are treated as external for partition purposes
- Status tags (`receiving`, `success`, `error`) — model comm cycle as nondeterministic phases
- `success` / `error` are one-scan pulses, likely WBR
- `exception_response` only meaningful on error, otherwise scratch
- If someone does `rise(RecvOK)`, partition catches it and keeps prev value in key

**Sanity check direction:** if a tag is in a `send()` source but marked external, warn — likely user error.

## 6. Combined performance estimate

| Optimization | Effect | PackML bench |
|---|---|---|
| Free input elision | Fewer BFS nodes | ~52K → ~4-8K states |
| Exclusive families | Fewer edges per node | 2^18 → handful of canonical patterns |
| Blockless kernel | Cheaper step execution | ~450s sync eliminated |
| Full key minimization | Minimal state dimensions | 4 tags in key |
| Timer three-phase abstraction | Collapse accumulator domain | 3 values per timer, time warp through dead scans |

**Combined target:** PackML bench from 31+ min (timeout) → under 2 min.

## 7. Scaling expectations

These optimizations target Click PLC programs (max ~144 I/O). Expected performance:

- **Typical Click project** (one state machine, few timers, 20-30 inputs): seconds
- **Complex Click project** (PackML-level, multiple modes, alarm historian): under 2 min
- **Multiple interacting state machines:** potentially problematic — fundamental state explosion, not an implementation issue
- **Programs with drums/shift registers:** rare on Click, partition logic handles them, near-zero false positive rate on edge classification since clock/jog inputs are usually internal C-bits
