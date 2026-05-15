# Hypothesis Grammar-Based Fuzzer — Implementation Plan

## Context

The BFS verifier (`prove()`) has had 12+ soundness fixes in v0.8, many found by hand-written regression tests. A grammar-based fuzzer generates random valid programs and mechanically checks that optimized and unoptimized verify paths agree, and that interpreted and compiled execution backends produce identical results. This catches optimization bugs (absorption, elision, threshold, backward propagation) and compiled kernel disagreements that hand-written tests miss.

The full checklist is at `scratchpad/fuzzer-checklist.md`. This plan covers Phase 1a (infrastructure + basic instructions + both test modes), with Phase 1b/1c outlined at the end.

## Architecture: Spec-Then-Emit

The pyrung DSL uses context managers (`with Rung():`) and thread-local state. Hypothesis strategies must be pure during generation. Solution: strategies generate lightweight dataclass "specs", then a separate `build_program()` function materializes them inside a `Program()` context. This also gives good shrinking and printable failing examples.

```
tag_pools()        → TagPool          (bag of tags, timers, counters, blocks)
condition_specs()  → CondSpec         (what condition to build)
instruction_specs()→ InstrSpec        (what instruction to emit)
rung_specs()       → RungSpec         (condition + instructions)
program_specs()    → ProgramSpec      (pool + rungs)
property_specs()   → PropertySpec     (what to prove)
                          ↓
              build_program(spec) → Program
              build_property(spec) → condition
```

## Files to Create

### `tests/fuzz/__init__.py` — empty

### `tests/fuzz/conftest.py` — constants and markers

```python
import pytest

pytestmark = [pytest.mark.hypothesis, pytest.mark.fuzz]

MAX_STATES = 10_000
DEPTH_BUDGET = 20
DT = 0.010
PARITY_SCANS = 50
```

### `tests/fuzz/pool.py` — TagPool + strategy

**`TagPool` dataclass** with fields:
- `bool_inputs: list[Tag]` — 1–4 external Bool (ND dimension)
- `bool_internal: list[Tag]` — 0–3 internal Bool
- `int_tags: list[Tag]` — 0–3 Int (some with min/max or choices)
- `dint_tags: list[Tag]` — 0–2 Dint
- `real_tags: list[Tag]` — 0–1 Real
- `word_tags: list[Tag]` — 0–1 Word
- `timers: list` — 0–2 Timer.clone instances
- `counters: list` — 0–2 Counter.clone instances
- `int_block: Block | None` — optional Block("DS", INT, 1, N)

Helper methods:
- `all_bool()` → inputs + internal
- `writable_bool()` → internal only (can't write external)
- `all_numeric()` → int + dint + real + word
- `writable_numeric()` → same as all_numeric (none are external yet)
- `all_conditions()` → all_bool + [t.Done for t in timers] + [c.Done for c in counters]
- `input_names()` → [t.name for t in bool_inputs]

**`tag_pools()` strategy** (`@st.composite`):
- Draw counts for each category
- Create tags with indexed names: `In0`, `B0`, `N0`, `D0`, `R0`, `W0`, `T0`, `C0`
- Int tags: ~30% chance of `min=/max=` metadata (draw from small ranges), ~20% chance of `choices=`
- Block size 3–8 when present

### `tests/fuzz/strategies.py` — conditions, instructions, rungs, programs, properties

**Value strategies:**
- `int_values()` — `st.one_of(st.sampled_from([0, 1, -1, 10, 100]), st.integers(-100, 100))`
- `timer_presets()` — `st.one_of(st.sampled_from([0, 1, 10, 50, 100]), st.integers(0, 100))`
- `counter_presets()` — `st.one_of(st.sampled_from([0, 1, 5, 10]), st.integers(0, 10))`

**CondSpec dataclass:**
```python
@dataclass
class CondSpec:
    kind: str       # "bit", "negated", "compare", "truthy"
    tag: Tag | None = None
    op: str | None = None
    operand: Any = None
```

**`condition_specs(pool)` strategy** — Phase 1a:
- bit (40%): Bool from pool.all_bool()
- negated (15%): Bool from pool.all_bool()
- compare (35%): numeric from pool.all_numeric(), op from [==,!=,<,<=,>,>=], value from int_values()
- truthy (10%): Int/Dint from pool.int_tags + pool.dint_tags (if any exist; fallback to bit)

**`build_condition(spec)` function:**
- bit → return tag
- negated → return ~tag
- compare → return tag <op> operand (using operator module)
- truthy → return tag

**InstrSpec dataclass:**
```python
@dataclass
class InstrSpec:
    kind: str
    args: dict[str, Any]
```

**Phase 1a instruction strategies** (5 types):

| Kind | Strategy | Emission |
|------|----------|----------|
| `"out"` | Bool from pool.writable_bool() | `out(target)` |
| `"latch"` | Bool from pool.writable_bool() | `latch(target)` |
| `"reset"` | Bool from pool.writable_bool() or numeric | `reset(target)` |
| `"copy"` | source: literal or numeric tag; dest: writable numeric | `copy(source, dest)` |
| `"calc"` | simple binary expr (tag op literal); dest: writable numeric | `calc(expr, dest)` |

Weights: out 25%, latch 10%, reset 10%, copy 35%, calc 20%.

**`instruction_specs(pool)` strategy** — Needs at least one writable Bool or numeric tag. If pool has no writable targets, fall back to the `assume()` filter.

**Calc expression building:** Phase 1a expressions limited to:
- `tag + literal`, `tag - literal`, `tag * literal`
- Store as tuple: `("add", tag_ref, literal)`, build via `tag + literal` during emission.

**RungSpec dataclass:**
```python
@dataclass
class RungSpec:
    conditions: list[CondSpec]    # 1–2 conditions
    instructions: list[InstrSpec] # 1–3 instructions
```

**`rung_specs(pool)` strategy:**
- Draw 1–2 conditions, 1–3 instructions
- Filter: instructions must have valid targets (use `assume()` if pool too small)

**ProgramSpec dataclass:**
```python
@dataclass
class ProgramSpec:
    pool: TagPool
    rungs: list[RungSpec]
```

**`program_specs()` strategy:**
- Draw pool from `tag_pools()`
- Draw 2–8 rung specs
- Return ProgramSpec

**`build_program(spec)` function:**
```python
def build_program(spec: ProgramSpec) -> Program:
    with Program(strict=False) as logic:
        for rs in spec.rungs:
            conds = [build_condition(c) for c in rs.conditions]
            with rung(*conds):
                for instr in rs.instructions:
                    emit_instruction(instr)
    return logic
```

**PropertySpec dataclass:**
```python
@dataclass
class PropertySpec:
    kind: str
    tags: list[Tag]
    bound: int | None = None
```

**`property_specs(pool)` strategy:**
- always_false (40%): pick writable Bool → `tag == False`
- always_true (20%): pick writable Bool → `tag == True`
- bounded (25%): pick numeric tag → `tag < N`
- mutual_exclusion (15%): pick 2 writable Bool → `~And(A, B)`

**`build_property(spec)` function:** converts spec to live condition.

### `tests/fuzz/test_soundness.py` — Mode 1

```python
@given(data=st.data())
@settings(max_examples=200, deadline=None)
def test_optimization_soundness(data):
    spec = data.draw(program_specs())
    program = build_program(spec)
    prop_spec = data.draw(property_specs(spec.pool))
    prop = build_property(prop_spec)

    optimized = prove(program, prop, max_states=MAX_STATES, depth_budget=DEPTH_BUDGET)
    if isinstance(optimized, (Intractable, Counterexample)):
        return  # only check when optimized claims Proven

    unoptimized = prove(program, prop, max_states=MAX_STATES, depth_budget=DEPTH_BUDGET,
                        _skip_optimizations=True)
    if isinstance(unoptimized, Intractable):
        return

    assert not isinstance(unoptimized, Counterexample), (
        f"Unsound optimization: optimized=Proven, unoptimized=Counterexample\n"
        f"Trace: {unoptimized.trace}"
    )
```

Uses `st.data()` for dependent draws (property depends on pool).

### `tests/fuzz/test_parity.py` — Mode 2

```python
@given(data=st.data())
@settings(max_examples=200, deadline=None)
def test_engine_parity(data):
    spec = data.draw(program_specs())
    program = build_program(spec)

    interpreted = PLC(program, dt=DT)
    compiled = CompiledPLC(program, dt=DT)

    for scan in range(PARITY_SCANS):
        inputs = {name: data.draw(st.booleans()) for name in spec.pool.input_names()}
        interpreted.patch(inputs)
        compiled.patch(inputs)
        interpreted.step()
        compiled.step()

        i_state = interpreted.current_state
        c_state = compiled.current_state
        assert i_state.scan_id == c_state.scan_id
        assert i_state.timestamp == pytest.approx(c_state.timestamp)
        assert dict(i_state.tags) == dict(c_state.tags), (
            f"Tag mismatch at scan {scan}: "
            f"{_diff_dicts(dict(i_state.tags), dict(c_state.tags))}"
        )
        assert dict(i_state.memory) == dict(c_state.memory), (
            f"Memory mismatch at scan {scan}"
        )
```

Follows the `_assert_states_match` pattern from `tests/conftest.py:36`.
Draws inputs per-scan via `st.data()` so Hypothesis can shrink the input sequence.

### `tests/fuzz/patterns.py` — placeholder for Phase 1b

Empty module with a docstring. Will contain Tier 1 pattern templates.

## Files to Modify

### `pyproject.toml` — add markers

Add to the `markers` list (after existing entries):
```
"fuzz: grammar-based fuzzer tests",
"parity: engine parity tests (interpreted vs compiled)",
```

### `Makefile` — add targets and .PHONY entries

Add to `.PHONY` line: `test-fuzz test-parity`

Add after `test-soundness:`:
```makefile
test-fuzz:
	uv run pytest -m fuzz

test-parity:
	uv run pytest -m parity
```

## Implementation Order

1. Create `tests/fuzz/__init__.py`
2. Create `tests/fuzz/conftest.py` (constants)
3. Create `tests/fuzz/pool.py` (TagPool + tag_pools strategy)
4. Create `tests/fuzz/strategies.py` (all strategies + build functions)
5. Create `tests/fuzz/test_soundness.py` (Mode 1)
6. Create `tests/fuzz/test_parity.py` (Mode 2)
7. Create `tests/fuzz/patterns.py` (placeholder)
8. Modify `pyproject.toml` (markers)
9. Modify `Makefile` (targets)
10. Run `make lint` + `make test-fuzz` to verify

## Phase 1b Outline (after Phase 1a works)

**New instruction strategies:**
- OnDelaySpec / OffDelaySpec — timer from pool, preset from timer_presets(), optional .reset()
- CountUpSpec / CountDownSpec — counter from pool, preset from counter_presets(), required .reset(), optional .down() for bidirectional

**New condition forms:**
- rise(tag), fall(tag) — edge detection on Bool tags
- And(c1, c2), Or(c1, c2) — composites with depth limit 2

**Pattern injection (`patterns.py`):**
- Implement all 11 Tier 1 templates from checklist Section 5
- Modify `program_specs()` to draw 1–3 pattern snippets alongside random rungs (30–50% pattern mix)

**Enhanced properties:**
- `~timer.Done` (timer never fires — should be Counterexample)
- `counter.Acc < N` (counter stays bounded)

## Phase 1c Outline (after Phase 1b works)

**Remaining Phase 1 instructions:**
- FillSpec, BlockcopySpec — require Block in pool, generate valid ranges via `block.select(start, end)`
- SearchSpec — range comparison + result/found tags
- ShiftSpec — Bool block range + clock/reset conditions (builder chain)
- PackBitsSpec / UnpackToBitsSpec — Bool range ↔ Int/Word/Dint register
- PackWordsSpec / UnpackToWordsSpec — 2× Int/Word ↔ Dint

**Boundary value biasing:** Refine value strategies with heavier weights on boundary values from checklist Section 6.

**Indirect addressing:** CopySpec and CalcSpec variants using `DS[ptr]` and `DS[ptr + N]` when pool has int_block.

## Verification

1. `make lint` passes
2. `make test` passes (no regressions — fuzz tests excluded by default)
3. `make test-fuzz` runs both test files, generates programs, calls prove() and PLC/CompiledPLC
4. Local run with `max_examples=200`: programs generate without construction errors, prove() returns mix of Proven/Counterexample/Intractable (not all Intractable), parity checks pass
5. Failing examples shrink to minimal cases (small pool, few rungs, short input sequences)
