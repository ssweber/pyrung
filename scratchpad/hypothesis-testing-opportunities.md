# Hypothesis Testing Opportunities

Found by analyzing areas similar to the named_array stride test in `tests/click/test_codegen_stride_hypothesis.py` (commit ba990c7).

## Tier 1 — High ROI (boundary-heavy numeric code)

### 1. Copy clamping vs. Calc wrapping

**Source:** `src/pyrung/core/instruction/conversions.py`

`_store_copy_value_to_tag_type()` (line 169) **clamps** while `_truncate_to_tag_type()` (line 192) **wraps** — two different overflow strategies on the same type ranges.

Properties:
- **Clamping idempotence**: `clamp(clamp(x, INT), INT) == clamp(x, INT)` for all `x`
- **Wrapping is modular**: `truncate(x, INT) == ((x + 32768) % 65536) - 32768` for any integer `x`
- **Result always in range**: For any input `v` and tag type `T`, both functions produce a value within `T`'s valid range
- **Hex mode wraps unsigned 16-bit**: `truncate(x, ANY_INT_TYPE, mode="hex") == x & 0xFFFF`
- **Non-finite sentinel handling**: `inf`, `-inf`, `nan` all map to `0` in both functions
- **Cross-type pairs**: Random values, copy INT→DINT (always fits), DINT→INT (clamps), WORD→INT (sign-extends), etc.

Strategy: `st.integers()` with wide range (±10^18), combined with `st.sampled_from([TagType.INT, TagType.DINT, TagType.WORD, TagType.REAL])`.

### 2. 16-bit Rotate functions

**Source:** `src/pyrung/core/expression.py:444-455`

`_rotate_left_16` and `_rotate_right_16` have clean mathematical invariants:

- **Round-trip**: `rro(lro(v, n), n) == v & 0xFFFF` for all `v`, `n`
- **Full rotation is identity**: `lro(v, 16) == v & 0xFFFF`
- **Associativity**: `lro(v, a+b) == lro(lro(v, a), b)`
- **Always 16-bit**: result is always in `0..65535`

Strategy: `st.integers(0, 0xFFFF)` for value, `st.integers(0, 31)` for count.

### 3. Float bit-reinterpretation round-trip

**Source:** `src/pyrung/core/instruction/conversions.py:34-41`

`_int_to_float_bits` and `_float_to_int_bits` should be inverses:

- **Round-trip**: `_float_to_int_bits(_int_to_float_bits(n)) == n & 0xFFFFFFFF` for any 32-bit unsigned int
- **Reverse round-trip**: `_int_to_float_bits(_float_to_int_bits(f)) == f` for any finite 32-bit float

Strategy: `st.integers(0, 0xFFFFFFFF)` and `st.floats(width=32, allow_nan=False, allow_infinity=False)`.

## Tier 2 — Good coverage (stateful instruction logic)

### 4. Counter clamping at DINT boundaries

**Source:** `src/pyrung/core/instruction/counters.py`

CountUp (line 91) and CountDown (line 162) both use `_clamp_dint()`.

Properties:
- **Accumulator stays in DINT range**: After any sequence of N scans, acc is always in `[-2147483648, 2147483647]`
- **Net delta**: Starting at acc=0, after `up_scans` enabled + `down_scans` via down_condition, acc == `clamp(up_scans - down_scans)`
- **Done bit consistency**: `done == (acc >= preset)` after every scan for CTU; `done == (acc <= -preset)` for CTD
- **Reset is absolute**: After reset, acc=0 and done=False regardless of prior state

Strategy: `hypothesis.stateful.RuleBasedStateMachine` modeling a counter receiving random enable/disable/reset signals over N scans. Compare against a simple reference model.

### 5. Timer fractional accumulation

**Source:** `src/pyrung/core/instruction/timers.py:67-101`

The fractional remainder tracking (`frac_key`) is subtle:

- **Accumulator monotonicity**: While continuously enabled, accumulator never decreases
- **Clamped at 32767**: Accumulator never exceeds INT16_MAX regardless of duration
- **Fractional conservation**: Total accumulated (int_units + frac) across N scans equals sum of `dt_to_units(dt)` values
- **Done bit correctness**: `done == (acc >= preset)` at every scan
- **Unit conversion linearity**: `dt_to_units(a) + dt_to_units(b) == dt_to_units(a+b)`

Strategy: Generate random sequences of `(enabled: bool, dt: float)` pairs where `dt` is `st.floats(min_value=0, max_value=1.0)`. Run N scans, verify invariants.

### 6. Drum step machine

**Source:** `src/pyrung/core/instruction/drums.py:228-300`

EventDrum has complex edge detection logic:

- **Step always valid**: After every scan, current step is in `[1, step_count]`
- **Outputs match pattern**: Outputs always equal `pattern[step-1]` after execution
- **Reset is authoritative**: After reset, step == 1 and completion_flag == False
- **Edge-only transitions**: Holding event high across multiple scans advances step at most once

Strategy: Generate random `(steps, outputs, pattern)` dimensions and signal sequences. `@st.composite` to build valid drum configs (pattern matrix must be `steps x outputs`).

## Tier 3 — Targeted (mapping/addressing)

### 7. Named array map_to address layout

**Source:** `src/pyrung/core/structure.py:336-360`

Beyond the existing hypothesis test, test `map_to` directly:

- **Correct field→address mapping**: Field `f` of instance `i` → hw address `base + (i-1)*stride + offset(f)`
- **Instance selection span**: `instance_select(a, b)` produces `(b-a+1) * field_count` tags
- **Wrong-sized ranges rejected**: `map_to(wrong_size_range)` raises ValueError

### 8. Out-of-range detection consistency

**Source:** `src/pyrung/core/instruction/conversions.py:255-276`

`_math_out_of_range_for_dest` is an oracle for whether truncation changes the value:

- **Consistency**: `_math_out_of_range_for_dest(v, tag, mode) == True` iff `_truncate_to_tag_type(v, tag, mode) != v` (for integer types)

This is a direct oracle test — detection should agree with whether truncation actually changed anything.

## Template Pattern

The existing test (`tests/click/test_codegen_stride_hypothesis.py`) demonstrates:

1. `@st.composite` to generate valid parameter triples with constraints (stride >= field_count)
2. Build real runtime objects from generated params
3. Full round-trip through the system (not just unit functions)
4. Multiple assertions checking different invariants per example

Tier 1 items are pure functions — use `@given` directly with basic strategies.
Tier 2 items benefit from multi-scan simulation loops or `hypothesis.stateful.RuleBasedStateMachine`.
