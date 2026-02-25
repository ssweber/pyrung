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
- advanced register/search: `ShiftInstruction`, `SearchInstruction`
- packing/unpacking: `PackBitsInstruction`, `PackWordsInstruction`, `PackTextInstruction`, `UnpackToBitsInstruction`, `UnpackToWordsInstruction`
- control flow: `CallInstruction`, `ReturnInstruction`, `FunctionCallInstruction`, `EnabledFunctionCallInstruction`
- loop control: `ForLoopInstruction`
- rung constructs: conditions, nested branches, subroutines

No instruction families are deferred in v1.

Instruction classes outside this list must fail generation with a deterministic `NotImplementedError` naming the instruction type and source location.

### 2.2 Tag representation

- Non-block logical tags compile to flat global Python variables.
- Block-backed tags compile to Python lists.
- Indirect addressing is list-based (`addr -> index`) with explicit bounds checks.
- I/O blocks are also represented as lists so direct and indirect references use one model.

### 2.3 Retentive persistence

- Retentive persistence is file-backed on the built-in microSD card at `/sd/memory.json`.
- Generated code must mount SD at startup using `sdcardio` on explicit SD pins (`board.SD_SCK`, `board.SD_MOSI`, `board.SD_MISO`, `board.SD_CS`), not `board.SPI()`.
- No `boot.py` changes are required; CircuitPython runtime writes `/sd/*`, while `CIRCUITPY` remains host-writable for code updates.
- Mounting must be wrapped in `try/except`:
  - on success, set `_sd_available = True`
  - on failure or missing card, set `_sd_available = False`, print warning, continue without fault
- `load_memory()`:
  - synchronous; called once at startup after tag defaults initialize and before scan execution
  - reads `/sd/memory.json`; on missing/corrupt/unreadable file, warn and continue with defaults
  - applies only values whose key exists in generated retentive symbols and whose stored type matches generated type
  - missing keys, unknown keys, and type mismatches are ignored (defaults retained)
- `save_memory()`:
  - ladder-callable (rung-triggered), never auto-called every scan
  - codegen must not call `save_memory()` from the unconditional scan loop
  - if `_sd_available` is false, it is a no-op
  - serializes only retentive symbols whose current value differs from generated defaults
  - writes `/sd/_memory.tmp`, then renames to `/sd/memory.json` (atomic replace on FAT)
  - persisted JSON includes a deterministic schema hash derived from retentive tag names + types for forward-compat checks
- Optional NVM dirty-flag mode:
  - set `microcontroller.nvm[0] = 1` immediately before save write
  - clear to `0` only after rename succeeds
  - on boot, if flag is `1`, treat last save as interrupted, warn, skip restore, keep defaults

### 2.4 Output format

- Codegen emits one standalone string for `code.py`.
- `code.py` contains runtime helpers inline (no project imports except stdlib + `P1AM`).
- Generated code includes read-input, logic, write-output scan phases.

### 2.5 Determinism

Generation is deterministic for identical `(program, hw, target_scan_ms, watchdog_ms)`:

- stable ordering for slots, tags, blocks, helpers, subroutines, and generated functions
- stable symbol names
- stable indentation and whitespace

### 2.6 Watchdog API compatibility

- Generated code must bind watchdog methods from the runtime `P1AM.Base()` instance by capability, not assumption.
- Required names are `config_watchdog`, `start_watchdog`, and `pet_watchdog`.
- If `WATCHDOG_MS` is enabled and any required watchdog method is missing, generated code raises a deterministic runtime error.

### 2.7 Hardware metadata notes

- Temperature input modules (`P1-04RTD`, `P1-04THM`, `P1-04NTC`) are modeled as `TagType.REAL` and read via `base.readTemperature(slot, channel)`.
- `P1-04TRS` remains modeled as a 4-point relay output module in local metadata.

### Acceptance criteria

- Full v1 instruction scope is explicit and enforced by generation errors.
- Retentive SD persistence behavior is explicitly specified (mount, load, save, failure handling, schema hash, optional dirty flag).
- Watchdog API binding strategy is explicit for snake_case runtime methods.
- Temperature input handling (`TagType.REAL` + `readTemperature`) is explicitly specified.
- `P1-04TRS` local 4-point modeling decision is explicitly documented.
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
- `ValueError`: invalid argument values, unsupported hardware shape (v1 constraints), or embedded callable source not inspectable.
- `NotImplementedError`: unsupported instruction/condition/expression class encountered.
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
- retentive persistence globals (`_sd_available`, schema hash, retentive defaults/type maps)
- optional NVM dirty-flag globals when enabled
- watchdog method bindings + optional watchdog config/petting
- scan pacing diagnostics (`_scan_overrun_count`, optional print toggle)
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
4. watchdog API binding + startup config
5. tag and block declarations
6. runtime memory declarations
7. SD mount + `load_memory()` startup call
8. helper definitions (`save_memory` + only used math/edge helpers)
9. embedded user function sources
10. compiled subroutine functions
11. compiled main-rung function
12. scan-time I/O read/write helpers
13. main scan loop

`SD mount + load_memory()` is intentionally inserted between the previous v1 sections 5 and 6 (after declarations, before helper-heavy logic execution).

### 4.2 Annotated template

```python
import hashlib
import json
import math
import os
import time

import board
import busio
import P1AM
import sdcardio
import storage

try:
    import microcontroller
except ImportError:
    microcontroller = None

TARGET_SCAN_MS = 10.0
WATCHDOG_MS = 1000  # or None
PRINT_SCAN_OVERRUNS = False

# Configured slot manifest in ascending slot order.
_SLOT_MODULES = ["P1-08SIM", "P1-08TRS"]
_RET_DEFAULTS = {"Step": 0}
_RET_TYPES = {"Step": "INT"}
_RET_SCHEMA = hashlib.sha256(
    "\n".join(f"{name}:{_RET_TYPES[name]}" for name in sorted(_RET_TYPES)).encode("utf-8")
).hexdigest()

base = P1AM.Base()
base.rollCall(_SLOT_MODULES)

_wd_config = getattr(base, "config_watchdog", None)
_wd_start = getattr(base, "start_watchdog", None)
_wd_pet = getattr(base, "pet_watchdog", None)
if WATCHDOG_MS is not None:
    if _wd_config is None or _wd_start is None or _wd_pet is None:
        raise RuntimeError("P1AM snake_case watchdog API not found on Base() instance")
    _wd_config(WATCHDOG_MS)
    _wd_start()

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
_scan_overrun_count = 0

_sd_available = False
_MEMORY_PATH = "/sd/memory.json"
_MEMORY_TMP_PATH = "/sd/_memory.tmp"
_sd_spi = None
_sd = None
_sd_vfs = None

def _mount_sd():
    global _sd_available, _sd_spi, _sd, _sd_vfs
    try:
        _sd_spi = busio.SPI(board.SD_SCK, board.SD_MOSI, board.SD_MISO)
        _sd = sdcardio.SDCard(_sd_spi, board.SD_CS)
        _sd_vfs = storage.VfsFat(_sd)
        storage.mount(_sd_vfs, "/sd")
        _sd_available = True
    except Exception as exc:
        _sd_available = False
        print(f"Retentive storage unavailable: {exc}")

def load_memory():
    global Step  # trimmed to referenced retentive symbols only
    if not _sd_available:
        print("Retentive load skipped: SD unavailable")
        return
    if microcontroller is not None and len(microcontroller.nvm) > 0 and microcontroller.nvm[0] == 1:
        print("Retentive load skipped: interrupted previous save detected")
        return
    try:
        with open(_MEMORY_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        print(f"Retentive load skipped: {exc}")
        return

    if payload.get("schema") != _RET_SCHEMA:
        print("Retentive load skipped: schema mismatch")
        return

    values = payload.get("values", {})
    entry = values.get("Step")
    if isinstance(entry, dict) and entry.get("type") == "INT":
        Step = int(entry.get("value", Step))

_mount_sd()
load_memory()

# Optional helpers are emitted only when referenced by generated code.
def save_memory():
    global Step  # trimmed to referenced retentive symbols only
    if not _sd_available:
        return

    values = {}
    if Step != _RET_DEFAULTS["Step"]:
        values["Step"] = {"type": "INT", "value": Step}
    payload = {"schema": _RET_SCHEMA, "values": values}

    dirty_armed = False
    if microcontroller is not None and len(microcontroller.nvm) > 0:
        microcontroller.nvm[0] = 1
        dirty_armed = True
    try:
        with open(_MEMORY_TMP_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        try:
            os.remove(_MEMORY_PATH)
        except OSError:
            pass
        os.rename(_MEMORY_TMP_PATH, _MEMORY_PATH)
    except Exception as exc:
        print(f"Retentive save failed: {exc}")
        return

    if dirty_armed:
        microcontroller.nvm[0] = 0

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
    global Light, Step, _mem  # only symbols referenced in this function
    # Compiled subroutine rung logic.
    # ReturnInstruction compiles to `return`.
    return

def _run_main_rungs():
    global Start, Light, Step, DS, C, _mem, _prev  # trimmed per function
    # Compiled main rungs in source order.
    pass

def _read_inputs():
    global Start, DS  # only symbols referenced in this function
    # Discrete input slot example:
    # mask = int(base.readDiscrete(1))
    # Start = bool((mask >> 0) & 1)
    # Analog count input slot example:
    # DS[0] = int(base.readAnalog(3, 1))
    # Temperature input slot example:
    # Temp[0] = float(base.readTemperature(4, 1))
    pass

def _write_outputs():
    global Light, DS  # only symbols referenced in this function
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
        _wd_pet()

    elapsed_ms = (time.monotonic() - scan_start) * 1000.0
    sleep_ms = TARGET_SCAN_MS - elapsed_ms
    if sleep_ms > 0:
        time.sleep(sleep_ms / 1000.0)
    else:
        _scan_overrun_count += 1
        if PRINT_SCAN_OVERRUNS:
            print(f"Scan overrun #{_scan_overrun_count}: {-sleep_ms:.3f} ms late")
```

### 4.3 SD mount requirements

SD SPI bus: The P1AM-200 has dedicated SPI lines for the SD card slot, separate from the Base Controller SPI bus. Generated code must use these explicit pins, not the default `board.SPI()`:

```python
import board
import busio
import sdcardio
import storage

_sd_spi = busio.SPI(board.SD_SCK, board.SD_MOSI, board.SD_MISO)
_sd = sdcardio.SDCard(_sd_spi, board.SD_CS)
_sd_vfs = storage.VfsFat(_sd)
storage.mount(_sd_vfs, "/sd")
```

### Acceptance criteria

- Template order is mandatory.
- Scan loop includes dt capture, I/O read, logic execution, I/O write, prev update, watchdog pet, pacing, and overrun diagnostics.
- Startup includes SD mount attempt and synchronous `load_memory()` before scan loop.
- `save_memory()` is emitted with helper section and uses temp-write + rename persistence.
- Watchdog calls use snake_case runtime methods (`config_watchdog`, `start_watchdog`, `pet_watchdog`).
- `global` statements are trimmed per function to only referenced mutable symbols.

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
    retentive_tags: dict[str, Tag]           # key=tag.name, tag.retentive == True

    subroutine_names: list[str]              # sorted for deterministic emission
    function_sources: dict[str, str]         # stable generated function name -> source
    function_globals: dict[str, set[str]]    # function name -> referenced mutable globals
    used_helpers: set[str]                   # names from §13 helper list
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
- `collect_retentive_tags()`
- `assign_symbols()`
- `compute_retentive_schema_hash() -> str`
- `mark_helper(helper_name: str)`
- `mark_function_global(fn_name: str, symbol: str)`
- `globals_for_function(fn_name: str) -> list[str]`
- `symbol_for_tag(tag: Tag) -> str`
- `symbol_for_block(block: Block) -> str`

### 5.3 Deterministic ordering rules

- slots: ascending numeric order
- subroutines: lexical sort by name
- tags: lexical sort by tag name
- retentive tags: lexical sort by tag name
- blocks: lexical sort by block symbol
- helper emission: fixed order defined in §13.2
- embedded functions: sort by generated function symbol
- function `global` emission: lexical sort per function

### Acceptance criteria

- Context fields and methods are sufficient to compile all sections.
- Context enforces deterministic generation order.
- Context tracks helper usage, function-source embedding, retentive symbol schema, and per-function global symbols.

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
6. Build `ctx.retentive_tags` as the subset where `tag.retentive` is true.
7. Compute schema input from sorted `"{tag.name}:{tag.type.name}"` lines over `ctx.retentive_tags`.

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

### 6.6 Retentive persistence symbol set

- `ctx.retentive_tags` is the source of truth for generated persistence code.
- Generated `load_memory()` and `save_memory()` bodies must be symbol-specialized to exactly this set.
- Persistence entries are keyed by logical tag name and include both type and value:
  - `{"type": "<TAG_TYPE>", "value": <json-serializable-value>}`
- Symbols not in `ctx.retentive_tags` must never be read from or written to `/sd/memory.json`.

### Acceptance criteria

- All referenced tags are discoverable through recursive walk.
- UDT/named-array values are handled without custom structure logic.
- Retentive tag filtering by `tag.retentive` is explicit and deterministic.
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

- validate inspectability during codegen (not deploy/runtime):
  - reject lambdas (`__name__ == "<lambda>"`)
  - reject closures (`fn.__closure__` contains bound cells)
  - reject callables without stable source (`inspect.getsource()` / `inspect.getsourcefile()` fails)
  - reject dynamic/builtin callables that cannot be embedded as source
  - raise `ValueError` naming instruction type and function name
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

### 9.10 Search compiler

`SearchInstruction` parity requirements:

- supports numeric (`INT`/`DINT`/`REAL`/`WORD`) and text (`CHAR`) paths
- resolve search range each execution (including indirect ranges and reverse order)
- empty range writes miss: `result = -1`, `found = False`
- numeric path:
  - resolve RHS once
  - find first address where operator condition matches
- text path:
  - only `==` and `!=` operators allowed
  - RHS text must be non-empty
  - compare sliding windows of `len(rhs_text)` over CHAR tags
- `continuous=False`: start at first address each execution
- `continuous=True`: resume after previous `result` address; `result=0` restarts; `result=-1` means exhausted
- write hit as `{result: matched_address, found: True}`, else miss

### 9.11 Shift compiler

`ShiftInstruction` parity requirements:

- always executes each scan (independent of rung disabled short-circuit)
- range must resolve to non-empty BOOL tags
- `enabled_expr` is the data bit inserted on shift events
- evaluate clock and reset conditions each scan
- rising edge detection uses `_mem["_shift_prev_clock:<location>"]`
- on rising edge:
  - capture current range values
  - write data bit into first range element
  - shift previous values forward in range order
- reset is level-sensitive and applied after shift logic (reset wins if both active)
- persist current clock state to `_mem` for next scan

### 9.12 Pack/unpack compilers

`PackBitsInstruction`:

- destination type must be `INT`, `WORD`, `DINT`, or `REAL`
- source range must contain BOOL tags only
- source width must be `<= 16` for `INT`/`WORD`, `<= 32` for `DINT`/`REAL`
- bit `0` maps from first source tag; set bits accumulate into integer pattern
- `REAL` destination stores via 32-bit IEEE bit reinterpretation

`PackWordsInstruction`:

- destination type must be `DINT` or `REAL`
- source range must contain exactly two `INT`/`WORD` tags
- source[0] is low word, source[1] is high word
- combine as `(hi << 16) | (lo & 0xFFFF)`, then store by destination type rules

`PackTextInstruction`:

- source range must contain CHAR tags
- destination must be `INT`, `DINT`, `WORD`, or `REAL`
- parse text with `allow_whitespace` behavior parity:
  - when false, leading/trailing whitespace is invalid
  - when true, trim before parse
- parse rules:
  - `INT`/`DINT`: signed decimal text
  - `WORD`: unsigned hex text
  - `REAL`: finite float value
- parse/type/range failure follows core parity (out-of-range fault path where available)

`UnpackToBitsInstruction`:

- source must be `INT`, `WORD`, `DINT`, or `REAL`
- destination range must contain BOOL tags only
- destination width must fit source bit width (`16` or `32`)
- write each destination bit from source pattern (`bit_index` by range order)

`UnpackToWordsInstruction`:

- source must be `DINT` or `REAL`
- destination range must contain exactly two `INT`/`WORD` tags
- split source pattern into low/high 16-bit words and store by destination type rules

### 9.13 For-loop compiler

`ForLoopInstruction` parity requirements:

- captured child instructions compile recursively through `compile_instruction(...)`
- when disabled:
  - execute compiled child instructions with `enabled=False` to preserve reset semantics
  - reset loop one-shot state and child one-shot states
- when enabled:
  - evaluate `iterations = max(0, int(count))`
  - for each iteration `i`, write loop index tag, then execute child list with `enabled=True`

### Acceptance criteria

- Each in-scope instruction family has explicit compile semantics.
- Timer/counter/copy/calc behavior mirrors current core runtime semantics.
- Search/shift/pack/unpack/for-loop are included in v1 dispatch and parity-tested.

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

Module metadata compatibility note:

- `P1-04TRS` is treated as a 4-channel discrete output module by the local catalog and codegen mapping.

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

Analog count inputs (`TagType.INT`):

```python
for ch in 1..count:
    block[ch] = int(base.readAnalog(slot, ch))
```

Temperature inputs (`TagType.REAL`):

```python
for ch in 1..count:
    block[ch] = float(base.readTemperature(slot, ch))
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

- Read/write mapping is explicit for discrete, analog-count, temperature, and combo modules.
- Roll-call behavior and contiguous-slot requirement are explicit.
- Channel-to-bit, channel-to-analog-count, and channel-to-temperature mapping are testable.

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
5. `_int_to_float_bits(n)`
6. `_float_to_int_bits(f)`
7. `_parse_pack_text_value(text, dest_type)`
8. `_store_copy_value_to_type(value, dest_type)`

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
- `_int_to_float_bits`
  - `PackBitsInstruction` when destination is `REAL`
  - `PackWordsInstruction` when destination is `REAL`
- `_float_to_int_bits`
  - `UnpackToBitsInstruction` when source is `REAL`
  - `UnpackToWordsInstruction` when source is `REAL`
- `_parse_pack_text_value`
  - `PackTextInstruction`
- `_store_copy_value_to_type`
  - `CopyInstruction`, `BlockCopyInstruction`, `FillInstruction`
  - `FunctionCallInstruction` and `EnabledFunctionCallInstruction` output assignment
  - pack/unpack destination writes where destination conversion parity is required

### 13.2 Emission algorithm

- During compile pass, call `ctx.mark_helper(name)` whenever a helper is needed.
- At render time, emit helpers in fixed order:
  - `_clamp_int`
  - `_wrap_int`
  - `_rise`
  - `_fall`
  - `_int_to_float_bits`
  - `_float_to_int_bits`
  - `_parse_pack_text_value`
  - `_store_copy_value_to_type`
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
- `TestSearchInstructions`
- `TestShiftInstructions`
- `TestPackUnpackInstructions`
- `TestFunctionCallInstructions`
- `TestCallReturnInstructions`
- `TestForLoopInstructions`
- `TestUnknownInstructionType`

### 15.6 Retentive persistence coverage

- `TestRetentivePersistence`
  - SD mount success/failure path (`_sd_available` true/false)
  - `load_memory()` applies only matching name+type retentive entries
  - missing file/corrupt JSON/schema mismatch paths keep defaults and do not fault
  - `save_memory()` writes only values changed from defaults
  - write-to-temp then rename behavior
  - no SD card path makes `save_memory()` a no-op
  - optional NVM dirty-flag behavior (interrupted save warning + defaults fallback)

### 15.7 Watchdog and scan diagnostics coverage

- `TestWatchdogBinding`
  - snake_case API path (`config_watchdog`, `start_watchdog`, `pet_watchdog`)
  - missing snake_case methods with `WATCHDOG_MS` set raises deterministic runtime error
- `TestScanOverrunDiagnostics`
  - `sleep_ms <= 0` increments `_scan_overrun_count`
  - optional warning print path

### 15.8 I/O mapping coverage

- `TestDiscreteIOMapping`
  - bitmask read/write channel mapping
- `TestAnalogIOMapping`
  - per-channel read/write mapping
- `TestComboModuleIOMapping`
  - split input/output behavior

### 15.9 Indirect addressing coverage

- `TestIndirectAddressing`
  - `IndirectRef` read/write
  - `IndirectExprRef`
  - static and indirect block ranges
  - bounds failures
  - sparse range behavior

### 15.10 End-to-end generated code smoke

- `TestGeneratedCodeSmoke`
  - generate code for minimal DI->DO program
  - compile generated source with `compile()`
  - execute with stubbed `P1AM` runtime
  - run one or more loop iterations via extracted helper function path

### 15.11 Required scenario list from project direction

Must include explicit tests for:

1. minimal discrete input/output generation
2. `on_delay` TON and RTON parity
3. `off_delay` parity with scan accumulation
4. `count_up` and `count_down` with DINT clamp edges
5. `copy` clamp vs `calc` wrap differences
6. `blockcopy` and `fill` for static and indirect ranges
7. `search` numeric/text and `continuous` resume behavior
8. `shift` rising-edge semantics with reset priority
9. `pack_bits` / `pack_words` / `pack_text` parity and conversions
10. `unpack_to_bits` / `unpack_to_words` parity and width/type checks
11. `for_loop` iteration semantics including disabled-path child reset behavior
12. rise/fall edge persistence across scans
13. subroutine call/return and branch ordering
14. function source embedding, inspectability rejection, and output mapping
15. SD retentive load/save success and failure paths
16. scan-overrun counter + optional warning behavior
17. watchdog method binding for snake_case API
18. per-function `global` statement trimming
19. discrete bitmask channel correctness
20. analog and temperature channel mapping correctness for input/output/combo
21. deterministic generation snapshots

### Acceptance criteria

- Every required scenario is mapped to at least one concrete test.
- Unknown instruction types have explicit deterministic failing tests.
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
6. Trigger rung-calls to `save_memory()`, power-cycle, and verify retentive values restore from `/sd/memory.json`.
7. Remove SD card and verify runtime continues with defaults (warning only, no fault).

### Pass criteria

- All tests and lint pass.
- Generated code compiles under parser check.
- Manual on-device smoke passes for DI->DO, watchdog, and SD retentive persistence behaviors.

---

## Completeness Checklist

- [x] Context and pipeline placement defined.
- [x] Design decisions and full v1 instruction scope defined.
- [x] Public API signature and contract defined.
- [x] Generated file structure and template defined.
- [x] `CodegenContext` data contract defined.
- [x] Tag collection/classification rules defined.
- [x] Condition compiler mappings defined.
- [x] Expression compiler mappings defined.
- [x] Instruction compiler patterns defined.
- [x] Retentive SD persistence behavior defined.
- [x] Watchdog API binding and scan overrun diagnostics defined.
- [x] I/O mapping rules defined.
- [x] Branch compilation strategy defined.
- [x] Indirect addressing strategy defined.
- [x] Runtime helper emission policy defined.
- [x] Future implementation file list defined.
- [x] Test plan and scenarios defined.
- [x] Verification commands and pass criteria defined.
