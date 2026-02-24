# CircuitPython Codegen Module

## 1. Context

The CircuitPython dialect already has:

- hardware modeling in [src/pyrung/circuitpy/hardware.py](c:/Users/ssweb/Documents/GitHub/pyrung/src/pyrung/circuitpy/hardware.py)
- module metadata in [src/pyrung/circuitpy/catalog.py](c:/Users/ssweb/Documents/GitHub/pyrung/src/pyrung/circuitpy/catalog.py)
- deployment validation in [src/pyrung/circuitpy/validation.py](c:/Users/ssweb/Documents/GitHub/pyrung/src/pyrung/circuitpy/validation.py)

The next stage is code generation: convert a validated `Program` plus `P1AM` hardware config into a standalone CircuitPython `code.py` file.

This document is a handoff spec for implementing that codegen stage. It is intentionally decision complete: implementers should not need to make design choices that affect behavior.

### Acceptance criteria

- `scratchpad/circuitpython-codegen.md` defines the full implementation plan.
- The plan is implementable without additional architecture decisions.
- The plan is consistent with current core instruction semantics.

---

## 2. Design Decisions

### 2.1 In-scope instruction surface (P1 v1)

Compile these instruction families:

- coils: `OutInstruction`, `LatchInstruction` (set), `ResetInstruction`
- timers: `OnDelayInstruction`, `OffDelayInstruction`
- counters: `CountUpInstruction`, `CountDownInstruction`
- data transfer: `CopyInstruction`, `CalcInstruction`, `BlockCopyInstruction`, `FillInstruction`
- control flow: `CallInstruction`, `ReturnInstruction`, `FunctionCallInstruction`, `EnabledFunctionCallInstruction`
- rung constructs: conditions, nested branches, subroutines

Deferred in this phase:

- `ShiftInstruction`
- `SearchInstruction`
- `PackBitsInstruction`
- `PackWordsInstruction`
- `PackTextInstruction`
- `UnpackToBitsInstruction`
- `UnpackToWordsInstruction`
- `ForLoopInstruction`

Unsupported instruction classes must fail generation with a deterministic `NotImplementedError` naming the instruction type and source location.

### 2.2 Tag representation

- Non-block logical tags compile to flat global Python variables.
- Block-backed tags compile to Python lists.
- Indirect addressing is list-based (`addr -> index`) with explicit bounds checks.
- I/O blocks are also represented as lists so direct and indirect references use one model.

### 2.3 Retentive persistence

- Retentive persistence to on-device storage is explicitly deferred.
- Generated `code.py` initializes all tags from their defaults at startup.
- A future phase can add file-backed persistence without changing v1 codegen structure.

### 2.4 Output format

- Codegen emits one standalone string for `code.py`.
- `code.py` contains runtime helpers inline (no project imports except stdlib + `P1AM`).
- Generated code includes read-input, logic, write-output scan phases.

### 2.5 Determinism

Generation is deterministic for identical `(program, hw, target_scan_ms, watchdog_ms)`:

- stable ordering for slots, tags, blocks, helpers, subroutines, and generated functions
- stable symbol names
- stable indentation and whitespace

### Acceptance criteria

- Scope and deferrals are explicit and enforced by generation errors.
- Representation choice (scalars + lists) is fully specified.
- Output contract is a single standalone `code.py` string.
- Deterministic output constraints are explicit and testable.

---

## 3. Public API

```python
def generate_circuitpy(
    program: Program,
    hw: P1AM,
    *,
    target_scan_ms: float,
    watchdog_ms: int | None = None,
) -> str:
    ...
```

### 3.1 Preconditions

- `program` must be a `Program` instance.
- `hw` must be a `P1AM` instance.
- `target_scan_ms` must be finite and `> 0`.
- `watchdog_ms` must be `None` or `int >= 0`.
- `hw` must contain at least one configured slot.
- Slot list must be contiguous from `1..N` for v1 roll-call generation.

### 3.2 Validation gate

Codegen must call:

```python
report = validate_circuitpy_program(program, hw=hw, mode="strict")
```

If `report.errors` is non-empty, raise `ValueError` with:

- summary (`report.summary()`)
- one line per error code/location

Codegen does not proceed on strict findings.

### 3.3 Return value

- Returns the full contents of generated `code.py` as `str`.
- The output must compile under CPython parser (`compile(..., "code.py", "exec")`) even though runtime target is CircuitPython.

### 3.4 Error model

- `TypeError`: invalid argument types.
- `ValueError`: invalid argument values or unsupported hardware shape (for v1 constraints).
- `NotImplementedError`: deferred instruction type encountered.
- `RuntimeError`: internal generator invariant violation (should indicate generator bug).

### 3.5 Internal interfaces to define in codegen module

```python
@dataclass(frozen=True)
class SlotBinding: ...

@dataclass(frozen=True)
class BlockBinding: ...

@dataclass
class CodegenContext: ...

def compile_condition(cond: Condition, ctx: CodegenContext) -> str: ...
def compile_expression(expr: Expression, ctx: CodegenContext) -> str: ...
def compile_instruction(instr: Any, enabled_expr: str, ctx: CodegenContext, indent: int) -> list[str]: ...
def compile_rung(rung: LogicRung, fn_name: str, ctx: CodegenContext, indent: int = 0) -> list[str]: ...
```

### 3.6 Generated runtime data model (in `code.py`)

Generated file must include:

- global scalar variables for scalar tags
- global list variables for block tags
- `_mem: dict[str, object]` for runtime memory keys
- `_prev: dict[str, object]` for edge-condition previous scan values
- optional watchdog config and petting
- scan loop entrypoint (`while True:`)

### Acceptance criteria

- API signature matches exactly.
- Validation is enforced before code emission.
- All error categories are explicit.
- Internal interfaces are named and scoped.
- Generated runtime model is explicitly defined.

---

## 4. Generated `code.py` Structure

### 4.1 Required top-level order

Sections must appear in this exact order:

1. imports
2. config constants
3. hardware bootstrap + roll-call
4. tag and block declarations
5. runtime memory declarations
6. optional helper definitions (only used helpers)
7. embedded user function sources
8. compiled subroutine functions
9. compiled main-rung function
10. scan-time I/O read/write helpers
11. main scan loop

### 4.2 Annotated template

```python
import math
import time
import P1AM

TARGET_SCAN_MS = 10.0
WATCHDOG_MS = 1000  # or None

# Configured slot manifest in ascending slot order.
_SLOT_MODULES = ["P1-08SIM", "P1-08TRS"]

base = P1AM.Base()
base.rollCall(_SLOT_MODULES)
if WATCHDOG_MS is not None:
    base.config_watchdog(WATCHDOG_MS)
    base.start_watchdog()

# Scalars (non-block tags).
Start = False
Light = False
Step = 0

# Blocks (list-backed; PLC addresses remain 1-based, list indexes are 0-based).
DS = [0] * 100
C = [False] * 16

# Runtime memory.
_mem = {}
_prev = {}
_last_scan_ts = time.monotonic()

# Optional helpers are emitted only when referenced by generated code.
def _clamp_int(value):
    if value < -32768:
        return -32768
    if value > 32767:
        return 32767
    return int(value)

def _wrap_int(value, bits, signed):
    mask = (1 << bits) - 1
    v = int(value) & mask
    if signed and v >= (1 << (bits - 1)):
        v -= (1 << bits)
    return v

def _rise(curr, prev):
    return bool(curr) and not bool(prev)

def _fall(curr, prev):
    return not bool(curr) and bool(prev)

# Embedded function call targets (inspect.getsource output, dedented).
def user_fn(value):
    return {"result": value + 1}

def _sub_MySub():
    global Start, Light, Step, DS, C, _mem, _prev
    # Compiled subroutine rung logic.
    # ReturnInstruction compiles to `return`.
    return

def _run_main_rungs():
    global Start, Light, Step, DS, C, _mem, _prev
    # Compiled main rungs in source order.
    pass

def _read_inputs():
    global Start, DS
    # Discrete input slot example:
    # mask = int(base.readDiscrete(1))
    # Start = bool((mask >> 0) & 1)
    # Analog input slot example:
    # DS[0] = int(base.readAnalog(3, 1))
    pass

def _write_outputs():
    global Light, DS
    # Discrete output slot example:
    # mask = 0
    # if Light: mask |= (1 << 0)
    # base.writeDiscrete(mask, 2)
    # Analog output slot example:
    # base.writeAnalog(int(DS[0]), 4, 1)
    pass

while True:
    scan_start = time.monotonic()
    dt = scan_start - _last_scan_ts
    if dt < 0:
        dt = 0.0
    _last_scan_ts = scan_start
    _mem["_dt"] = dt

    _read_inputs()
    _run_main_rungs()
    _write_outputs()

    # Edge memory update after logic: mirrors runner semantics.
    # One entry per referenced tag symbol.
    # _prev["Start"] = Start
    # _prev["Light"] = Light

    if WATCHDOG_MS is not None:
        base.pet_watchdog()

    elapsed_ms = (time.monotonic() - scan_start) * 1000.0
    sleep_ms = TARGET_SCAN_MS - elapsed_ms
    if sleep_ms > 0:
        time.sleep(sleep_ms / 1000.0)
```

### Acceptance criteria

- Template order is mandatory.
- Scan loop includes dt capture, I/O read, logic execution, I/O write, prev update, watchdog, pacing.
- Global declarations include all mutable symbols referenced inside compiled functions.

---
## 5. `CodegenContext` Class

### 5.1 Required fields

```python
@dataclass
class CodegenContext:
    program: Program
    hw: P1AM
    target_scan_ms: float
    watchdog_ms: int | None

    slot_bindings: list[SlotBinding]
    block_bindings: dict[int, BlockBinding]  # key=id(block)
    scalar_tags: dict[str, Tag]              # key=tag.name
    referenced_tags: dict[str, Tag]          # all tags used anywhere

    subroutine_names: list[str]              # sorted for deterministic emission
    function_sources: dict[str, str]         # stable generated function name -> source
    used_helpers: set[str]                   # {"_clamp_int", "_wrap_int", "_rise", "_fall"}
    symbol_table: dict[str, str]             # logical name -> python symbol
```

`SlotBinding` and `BlockBinding` must include:

- slot number
- module part number
- module direction and channel counts
- discrete/analog kind
- mapping from channel to storage expression
- block `start`, `end`, optional sparse valid-address set

### 5.2 Required methods

- `collect_hw_bindings()`
- `collect_program_references()`
- `assign_symbols()`
- `mark_helper(helper_name: str)`
- `symbol_for_tag(tag: Tag) -> str`
- `symbol_for_block(block: Block) -> str`

### 5.3 Deterministic ordering rules

- slots: ascending numeric order
- subroutines: lexical sort by name
- tags: lexical sort by tag name
- blocks: lexical sort by block symbol
- helper emission: fixed order `["_clamp_int", "_wrap_int", "_rise", "_fall"]`
- embedded functions: sort by generated function symbol

### Acceptance criteria

- Context fields and methods are sufficient to compile all sections.
- Context enforces deterministic generation order.
- Context tracks helper usage and function-source embedding.

---

## 6. Tag Collection and Classification

### 6.1 Categories

Classify discovered references into:

- hardware input blocks (`InputBlock` from `hw.slot(...)`)
- hardware output blocks (`OutputBlock` from `hw.slot(...)`)
- internal blocks (`Block` not from hardware slots)
- scalar tags (all non-block-resident tags)

### 6.2 Discovery algorithm

1. Build hardware block registry from `hw._slots`.
2. Walk all main rungs and subroutine rungs recursively:
   - conditions
   - instructions
   - branch child rungs
3. For each value tree, extract:
   - `Tag`
   - `IndirectRef`
   - `IndirectExprRef`
   - `BlockRange`
   - `IndirectBlockRange`
   - condition children
   - expression nodes
   - function call `ins`/`outs`
4. Record each referenced tag by logical name.
5. Associate tags with known blocks where possible; unassociated tags are scalar.

### 6.3 UDT and named-array flattening

No special UDT AST handling is required.

- `@udt()` and `@named_array()` already materialize as normal tags/blocks.
- Codegen consumes the materialized tags exactly as discovered.
- Count-1 UDT fields are scalars.
- Count-N UDT fields are blocks and compile as lists.

### 6.4 Default initialization

Initialize each symbol using tag type defaults:

- `BOOL -> False`
- `INT -> 0`
- `DINT -> 0`
- `REAL -> 0.0`
- `WORD -> 0`
- `CHAR -> ""`

### 6.5 Symbol mangling

Use a deterministic mangler:

1. Replace non `[A-Za-z0-9_]` with `_`.
2. Prefix with `_t_` for scalars and `_b_` for blocks.
3. If first char is digit, prefix `_`.
4. Resolve collisions with suffix `_2`, `_3`, ... by lexical insertion order.

Store mapping in `ctx.symbol_table`.

### Acceptance criteria

- All referenced tags are discoverable through recursive walk.
- UDT/named-array values are handled without custom structure logic.
- Initialization defaults and symbol names are deterministic.

---

## 7. Condition Compiler

### 7.1 Function contract

```python
def compile_condition(cond: Condition, ctx: CodegenContext) -> str:
    """Return a Python boolean expression string."""
```

### 7.2 Mapping table

| Condition class | Emitted expression |
|---|---|
| `BitCondition(tag)` | `bool(<tag_value_expr>)` |
| `NormallyClosedCondition(tag)` | `(not bool(<tag_value_expr>))` |
| `IntTruthyCondition(tag)` | `(int(<tag_value_expr>) != 0)` |
| `CompareEq(tag, value)` | `(<lhs> == <rhs>)` |
| `CompareNe(tag, value)` | `(<lhs> != <rhs>)` |
| `CompareLt(tag, value)` | `(<lhs> < <rhs>)` |
| `CompareLe(tag, value)` | `(<lhs> <= <rhs>)` |
| `CompareGt(tag, value)` | `(<lhs> > <rhs>)` |
| `CompareGe(tag, value)` | `(<lhs> >= <rhs>)` |
| `AllCondition([...])` | `(<c1> and <c2> and ...)` |
| `AnyCondition([...])` | `(<c1> or <c2> or ...)` |
| `RisingEdgeCondition(tag)` | `_rise(bool(<tag_value_expr>), bool(_prev.get("<tag_name>", False)))` |
| `FallingEdgeCondition(tag)` | `_fall(bool(<tag_value_expr>), bool(_prev.get("<tag_name>", False)))` |
| `IndirectCompareEq(ref, value)` | `(<indirect_value> == <rhs>)` |
| `IndirectCompareNe(ref, value)` | `(<indirect_value> != <rhs>)` |
| `IndirectCompareLt(ref, value)` | `(<indirect_value> < <rhs>)` |
| `IndirectCompareLe(ref, value)` | `(<indirect_value> <= <rhs>)` |
| `IndirectCompareGt(ref, value)` | `(<indirect_value> > <rhs>)` |
| `IndirectCompareGe(ref, value)` | `(<indirect_value> >= <rhs>)` |
| `ExprCompareEq(left, right)` | `(<expr(left)> == <expr(right)>)` |
| `ExprCompareNe(left, right)` | `(<expr(left)> != <expr(right)>)` |
| `ExprCompareLt(left, right)` | `(<expr(left)> < <expr(right)>)` |
| `ExprCompareLe(left, right)` | `(<expr(left)> <= <expr(right)>)` |
| `ExprCompareGt(left, right)` | `(<expr(left)> > <expr(right)>)` |
| `ExprCompareGe(left, right)` | `(<expr(left)> >= <expr(right)>)` |

### 7.3 Helper usage

- Mark `_rise` helper when compiling `RisingEdgeCondition`.
- Mark `_fall` helper when compiling `FallingEdgeCondition`.

### 7.4 Unsupported condition handling

Unknown subclasses raise:

```python
NotImplementedError(f"Unsupported condition type: {type(cond).__name__}")
```

### Acceptance criteria

- Every known condition class has an explicit mapping.
- Rise/fall behavior uses previous scan memory (`_prev`) only.
- Unsupported condition types fail deterministically.

---

## 8. Expression Compiler

### 8.1 Function contract

```python
def compile_expression(expr: Expression, ctx: CodegenContext) -> str:
    """Return a Python expression string with explicit parentheses."""
```

### 8.2 Required node mappings

Leaf nodes:

- `TagExpr(tag) -> <tag_value_expr>`
- `LiteralExpr(value) -> repr(value)`

Binary arithmetic:

- `AddExpr -> (<l> + <r>)`
- `SubExpr -> (<l> - <r>)`
- `MulExpr -> (<l> * <r>)`
- `DivExpr -> (<l> / <r>)`
- `FloorDivExpr -> (<l> // <r>)`
- `ModExpr -> (<l> % <r>)`
- `PowExpr -> (<l> ** <r>)`

Unary arithmetic:

- `NegExpr -> (-(<x>))`
- `PosExpr -> (+(<x>))`
- `AbsExpr -> abs(<x>)`

Bitwise:

- `AndExpr -> (int(<l>) & int(<r>))`
- `OrExpr -> (int(<l>) | int(<r>))`
- `XorExpr -> (int(<l>) ^ int(<r>))`
- `LShiftExpr -> (int(<l>) << int(<r>))`
- `RShiftExpr -> (int(<l>) >> int(<r>))`
- `InvertExpr -> (~int(<x>))`

Function expressions:

- `MathFuncExpr(name in {sqrt,sin,cos,tan,asin,acos,atan,radians,degrees,log10,log}) -> math.<name>(<operand>)`
- `ShiftFuncExpr("lsh") -> (int(<value>) << int(<count>))`
- `ShiftFuncExpr("rsh") -> (int(<value>) >> int(<count>))`
- `ShiftFuncExpr("lro") -> (((int(<value>) & 0xFFFF) << (int(<count>) % 16)) | ((int(<value>) & 0xFFFF) >> (16 - (int(<count>) % 16)))) & 0xFFFF`
- `ShiftFuncExpr("rro") -> (((int(<value>) & 0xFFFF) >> (int(<count>) % 16)) | ((int(<value>) & 0xFFFF) << (16 - (int(<count>) % 16)))) & 0xFFFF`

### 8.3 Parentheses rule

Every non-leaf output must be wrapped in parentheses unless syntactically required form already isolates precedence (`abs(...)`, `math.fn(...)`).

### 8.4 Unsupported node handling

Unknown expression class must raise deterministic `TypeError`.

### Acceptance criteria

- All core expression node families are mapped.
- Parentheses policy is explicit and deterministic.
- Unknown expression nodes fail deterministically.

---
## 9. Instruction Compilers

### 9.1 Dispatch contract

```python
def compile_instruction(
    instr: Any,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    ...
```

`enabled_expr` is the precomputed rung/branch power expression for this flow.

### 9.2 One-shot handling

For any `OneShotMixin` instruction with `oneshot=True`, emit logic:

1. key = `"_oneshot:<location>"`
2. if not enabled: clear key (`False`) and skip body
3. if enabled and key is False: execute body and set key True
4. if enabled and key is True: skip body

For `OutInstruction`, disabled path still forces target to `False` and clears one-shot key.

### 9.3 Coil compilers

`OutInstruction(target)`:

- when enabled: set target(s) `True`
- when disabled: set target(s) `False`

`LatchInstruction(target)`:

- when enabled: set target(s) `True`
- when disabled: no-op

`ResetInstruction(target)`:

- when enabled: set target(s) to type default
- when disabled: no-op

Target can be scalar tag or block range.

### 9.4 Timer compilers

`OnDelayInstruction` parity requirements:

- read/reset fractional key `_frac:<acc_name>` from `_mem`
- if reset condition true: clear frac, set done `False`, acc `0`, return
- if enabled:
  - `dt_units = unit.dt_to_units(dt) + frac`
  - `int_units = int(dt_units)`
  - `new_frac = dt_units - int_units`
  - `acc = min(acc + int_units, 32767)`
  - `done = (acc >= preset)`
  - write frac, done, acc
- else:
  - RTON (`has_reset=True`): hold values
  - TON (`has_reset=False`): clear frac, done, acc

`OffDelayInstruction` parity requirements:

- if enabled: frac=0.0, done=True, acc=0
- if disabled:
  - accumulate by dt units + frac
  - clamp acc at 32767
  - `done = (acc < preset)`

### 9.5 Counter compilers

`CountUpInstruction`:

- reset condition check first: done=False, acc=0, return
- `delta = 0`
- if enabled: `delta += 1`
- if down condition true: `delta -= 1`
- `acc = clamp to DINT`
- `done = acc >= preset`

`CountDownInstruction`:

- reset condition check first: done=False, acc=0, return
- if enabled: `acc -= 1`
- clamp DINT
- `done = acc <= -preset`

### 9.6 Copy/calc compilers

`CopyInstruction`:

- resolve source value (literal/tag/expression/indirect)
- resolve destination (tag/indirect)
- store semantics:
  - INT: saturating clamp (`_clamp_int`)
  - DINT: saturating clamp inline to `[-2147483648, 2147483647]`
  - WORD: wrap `& 0xFFFF`
  - BOOL: `bool(value)`
  - REAL: `float(value)`
  - CHAR: store as-is

`CalcInstruction`:

- evaluate expression
- on divide-by-zero/non-finite result: set value to `0`
- mode `"hex"`:
  - 16-bit unsigned wrap
- mode `"decimal"`:
  - INT/DINT/WORD wrapping semantics (not clamp)
  - use `_wrap_int` helper for signed/unsigned wrap

### 9.7 Block operation compilers

`BlockCopyInstruction`:

- resolve source and destination address windows
- lengths must match, else `ValueError`
- per-element copy with `CopyInstruction` destination conversion semantics

`FillInstruction`:

- resolve destination window
- resolve fill value once per execution
- write converted value to each destination element

### 9.8 Function call compilers

`FunctionCallInstruction`:

- embed callable source once via `inspect.getsource()` and `textwrap.dedent()`
- call with compiled `ins` kwargs
- if `outs` declared:
  - result must not be `None`
  - required keys must exist
  - assign each output using copy conversion semantics

`EnabledFunctionCallInstruction`:

- same as above, but first argument is `enabled` state (`True`/`False`)
- always executes each scan

### 9.9 Subroutine call/return compilers

`CallInstruction("name")`:

- emit `_sub_<name>()`

`ReturnInstruction`:

- emit `return` inside generated subroutine function

### 9.10 Deferred instructions behavior

On encounter, raise `NotImplementedError`:

- `ShiftInstruction`
- `SearchInstruction`
- `PackBitsInstruction`
- `PackWordsInstruction`
- `PackTextInstruction`
- `UnpackToBitsInstruction`
- `UnpackToWordsInstruction`
- `ForLoopInstruction`

### Acceptance criteria

- Each in-scope instruction family has explicit compile semantics.
- Timer/counter/copy/calc behavior mirrors current core runtime semantics.
- Deferred instructions fail clearly and deterministically.

---

## 10. I/O Mapping

### 10.1 Slot extraction

Use `hw._slots` sorted by slot number.

Each slot binding captures:

- `slot_number`
- `module_spec.part_number`
- input group metadata (if present)
- output group metadata (if present)
- linked block binding(s)

### 10.2 Roll-call emission

Emit roll-call list from contiguous slot sequence `1..max_slot`.

If configured slots are not contiguous from 1, generator raises `ValueError` in v1.

### 10.3 Read phase rules

Discrete inputs (`TagType.BOOL`):

```python
mask = int(base.readDiscrete(slot))
for ch in 1..count:
    block[ch] = bool((mask >> (ch - 1)) & 1)
```

Analog inputs (`TagType.INT`):

```python
for ch in 1..count:
    block[ch] = int(base.readAnalog(slot, ch))
```

Combo modules:

- read only input group channels
- output group is untouched in read phase

### 10.4 Write phase rules

Discrete outputs (`TagType.BOOL`):

```python
mask = 0
for ch in 1..count:
    if bool(block[ch]):
        mask |= (1 << (ch - 1))
base.writeDiscrete(mask, slot)
```

Analog outputs (`TagType.INT`):

```python
for ch in 1..count:
    base.writeAnalog(int(block[ch]), slot, ch)
```

Combo modules:

- write only output group channels

### 10.5 Address/index note

In formulas above, `block[ch]` is conceptual 1-based PLC channel naming.
Actual generated Python list access uses `block[ch - start_addr]`.

### Acceptance criteria

- Read/write mapping is explicit for discrete, analog, and combo modules.
- Roll-call behavior and contiguous-slot requirement are explicit.
- Channel-to-bit and channel-to-analog mapping are testable.

---

## 11. Branch Compilation

Branch execution must match `Rung.execute()` semantics:

1. Compute parent rung power once.
2. Precompute direct child branch enable states before executing any items.
3. Execute `_execution_items` in source order.
4. For branch items, execute branch body with precomputed power.

### 11.1 Compile pattern

For each rung function:

```python
enabled = (<compiled rung condition>)

# Precompute direct branch enables first.
branch_enabled_0 = enabled and (<branch0 local conditions>)
branch_enabled_1 = enabled and (<branch1 local conditions>)

# Execute source-order mixed items.
# instruction item:
if enabled:
    ...
# branch item:
_exec_branch_0(branch_enabled_0)
```

Nested branches recurse with the same pattern.

Branch local conditions are the slice:

```python
branch_rung._conditions[branch_rung._branch_condition_start:]
```

### Acceptance criteria

- Branch enable precomputation happens before any rung item execution.
- Source-order interleaving of instructions and branches is preserved.
- Nested branches follow the same algorithm recursively.

---
## 12. Indirect Addressing Approach

### 12.1 Address conversion

Convert PLC address to list index using block metadata:

```python
index = addr - block_start
```

General form supports blocks not starting at 1.

### 12.2 Bounds and sparse checks

For each indirect access:

1. resolve address to `int`
2. check `start <= addr <= end`
3. if sparse block: check membership in `valid_addresses`
4. convert to index and access list

Out-of-range raises `IndexError` with deterministic message:

```python
f"Address {addr} out of range for {block_name} ({start}-{end})"
```

### 12.3 Indirect read/write helpers (generated inline where used)

```python
def _resolve_index_<block>(addr):
    ...
    return idx
```

Use helper for:

- `IndirectRef` value resolution
- `IndirectExprRef` resolution
- indirect destination writes
- `IndirectBlockRange` start/end resolution

### 12.4 Indirect block ranges

- resolve start and end each execution
- inclusive addressing
- if `start > end`, raise `ValueError`
- for sparse blocks, use only valid addresses in window
- respect `reverse_order` by reversing resolved address list

### Acceptance criteria

- 1-based to 0-based conversion rule is explicit.
- Bounds/sparse behavior is explicit.
- Indirect single-element and range access behavior is fully specified.

---

## 13. Runtime Helper Emission Policy

Emit these helpers only if referenced by generated instruction/condition code:

1. `_clamp_int(value)`
2. `_wrap_int(value, bits, signed)`
3. `_rise(curr, prev)`
4. `_fall(curr, prev)`

### 13.1 Helper call sites

- `_clamp_int`
  - `CopyInstruction` writes to INT tags
  - optional timer acc clamp implementation (if chosen)
- `_wrap_int`
  - `CalcInstruction` decimal wrap to INT/DINT/WORD
  - `CalcInstruction` hex mode wrap
- `_rise`
  - `RisingEdgeCondition`
- `_fall`
  - `FallingEdgeCondition`

### 13.2 Emission algorithm

- During compile pass, call `ctx.mark_helper(name)` whenever a helper is needed.
- At render time, emit helpers in fixed order:
  - `_clamp_int`
  - `_wrap_int`
  - `_rise`
  - `_fall`
- Skip any helper not present in `ctx.used_helpers`.

### Acceptance criteria

- Helper emission is usage-driven and deterministic.
- No unused helper is emitted.
- Required helpers are always emitted when referenced.

---

## 14. Files to Create / Modify

### 14.1 `src/pyrung/circuitpy/codegen.py` - NEW

Estimated size: 500-800 lines.

Required contents:

```python
def generate_circuitpy(program: Program, hw: P1AM, *, target_scan_ms: float, watchdog_ms: int | None = None) -> str: ...

@dataclass(frozen=True)
class SlotBinding: ...

@dataclass(frozen=True)
class BlockBinding: ...

@dataclass
class CodegenContext: ...

def compile_condition(cond: Condition, ctx: CodegenContext) -> str: ...
def compile_expression(expr: Expression, ctx: CodegenContext) -> str: ...
def compile_instruction(instr: Any, enabled_expr: str, ctx: CodegenContext, indent: int) -> list[str]: ...
def compile_rung(rung: LogicRung, fn_name: str, ctx: CodegenContext, indent: int = 0) -> list[str]: ...
```

### 14.2 `src/pyrung/circuitpy/__init__.py` - MODIFY

- Import and export `generate_circuitpy`.
- Add symbol to `__all__`.

### 14.3 `tests/circuitpy/test_codegen.py` - NEW

Estimated size: 350-600 lines.

- unit tests for API, compilers, deterministic output, unsupported paths
- small generated-code execution smoke with stubbed `P1AM` object

### 14.4 Optional fixtures directory

`tests/circuitpy/fixtures/` for golden snapshots if needed.

### Acceptance criteria

- File list is explicit and sufficient.
- Public API exposure path is explicit.
- Test module path and scope are explicit.

---

## 15. Test Plan

Test style should follow existing `tests/circuitpy/` class-based layout.

### 15.1 Core API and validation gate

- `TestGenerateCircuitPyAPI`
  - rejects bad argument types/values
  - rejects sparse slot layouts
  - invokes strict validator and fails on strict findings
  - returns `str` for valid program

### 15.2 Deterministic emission

- `TestDeterministicOutput`
  - same inputs generate byte-identical output
  - helper order stable
  - subroutine order stable
  - function embedding order stable

### 15.3 Condition compiler coverage

- `TestConditionCompiler`
  - bit/nc/truthy
  - all comparison operators
  - all/any
  - rise/fall
  - indirect comparisons
  - expression comparison conditions

### 15.4 Expression compiler coverage

- `TestExpressionCompiler`
  - literals and tags
  - arithmetic and unary nodes
  - bitwise nodes
  - math function nodes
  - shift/rotate function nodes

### 15.5 Instruction compiler coverage

- `TestCoilInstructions`
- `TestTimerInstructions`
- `TestCounterInstructions`
- `TestCopyCalcInstructions`
- `TestBlockOpsInstructions`
- `TestFunctionCallInstructions`
- `TestCallReturnInstructions`
- `TestUnsupportedInstructions`

### 15.6 I/O mapping coverage

- `TestDiscreteIOMapping`
  - bitmask read/write channel mapping
- `TestAnalogIOMapping`
  - per-channel read/write mapping
- `TestComboModuleIOMapping`
  - split input/output behavior

### 15.7 Indirect addressing coverage

- `TestIndirectAddressing`
  - `IndirectRef` read/write
  - `IndirectExprRef`
  - static and indirect block ranges
  - bounds failures
  - sparse range behavior

### 15.8 End-to-end generated code smoke

- `TestGeneratedCodeSmoke`
  - generate code for minimal DI->DO program
  - compile generated source with `compile()`
  - execute with stubbed `P1AM` runtime
  - run one or more loop iterations via extracted helper function path

### 15.9 Required scenario list from project direction

Must include explicit tests for:

1. minimal discrete input/output generation
2. `on_delay` TON and RTON parity
3. `off_delay` parity with scan accumulation
4. `count_up` and `count_down` with DINT clamp edges
5. `copy` clamp vs `calc` wrap differences
6. `blockcopy` and `fill` for static and indirect ranges
7. rise/fall edge persistence across scans
8. subroutine call/return and branch ordering
9. function source embedding and output mapping
10. discrete bitmask channel correctness
11. analog channel mapping correctness for input/output/combo
12. deterministic generation snapshots

### Acceptance criteria

- Every required scenario is mapped to at least one concrete test.
- Deferred instructions have explicit failing tests.
- Determinism is enforced by snapshot/string equality tests.

---

## 16. Verification

Run these commands after implementation:

```bash
make test
make lint
uv run pytest tests/circuitpy/test_codegen.py -v
```

Generated-source parser check:

```bash
uv run python - <<'PY'
from pyrung.circuitpy.codegen import generate_circuitpy
from pyrung.circuitpy import P1AM
from pyrung.core import Program, Rung, out
from pyrung.core.tag import Bool

hw = P1AM()
di = hw.slot(1, "P1-08SIM")
do = hw.slot(2, "P1-08TRS")

Button = di[1]
Light = do[1]

prog = Program(strict=False)
with prog:
    with Rung(Button):
        out(Light)

source = generate_circuitpy(prog, hw, target_scan_ms=10.0, watchdog_ms=None)
compile(source, "code.py", "exec")
print("OK")
PY
```

On-device smoke procedure (manual):

1. Generate `code.py` from a known simple program.
2. Copy to CIRCUITPY volume.
3. Connect module stack matching configured slots.
4. Toggle discrete input and verify mapped output.
5. Observe watchdog behavior when enabled.

### Pass criteria

- All tests and lint pass.
- Generated code compiles under parser check.
- Manual on-device smoke passes for basic DI->DO case.

---

## Completeness Checklist

- [x] Context and pipeline placement defined.
- [x] Design decisions and scope/deferred list defined.
- [x] Public API signature and contract defined.
- [x] Generated file structure and template defined.
- [x] `CodegenContext` data contract defined.
- [x] Tag collection/classification rules defined.
- [x] Condition compiler mappings defined.
- [x] Expression compiler mappings defined.
- [x] Instruction compiler patterns defined.
- [x] I/O mapping rules defined.
- [x] Branch compilation strategy defined.
- [x] Indirect addressing strategy defined.
- [x] Runtime helper emission policy defined.
- [x] Future implementation file list defined.
- [x] Test plan and scenarios defined.
- [x] Verification commands and pass criteria defined.
