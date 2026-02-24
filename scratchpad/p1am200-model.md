# Plan: P1AM-200 Module Catalog + Hardware Model

## Context

The CircuitPython dialect targets the ProductivityOpen P1AM-200 industrial automation CPU. The spec (`docs/internal/circuitpy-spec.md`) has architecture decisions made but no implementation. This is the foundation piece: the module catalog (all P1000-series I/O modules with metadata) and the `P1AM` hardware model class with `slot()` API.

The Click dialect provides the reference pattern: `pyclickplc.banks.BANKS` dict drives block construction via `_block_from_bank_config()`. CircuitPython has no external package equivalent, so the catalog lives in-tree.

## Design Decisions

- **`slot()` returns bare blocks**: `InputBlock` for input modules, `OutputBlock` for output modules, `tuple[InputBlock, OutputBlock]` for combo modules. Matches the existing spec.
- **Tag naming**: `Slot{N}.{ch}` (e.g. `Slot1.3`). Combo: `Slot{N}_In.{ch}`, `Slot{N}_Out.{ch}`. User can override with `name=` parameter.
- **1-indexed channels**: Matches P1AM hardware convention and pyrung core convention.
- **Analog uses `TagType.INT`**: Raw ADC counts; user scales via `calc()`.
- **PWM/HSC deferred**: Specialty modules excluded from P1 scope.
- **Eager validation**: `slot()` validates slot range (1-15), module string in catalog, no duplicate slots immediately.

## Files to Create

### 1. `src/pyrung/circuitpy/__init__.py`
Re-exports: `P1AM`, `MODULE_CATALOG`, `ModuleSpec`, `ChannelGroup`, `ModuleDirection`, `MAX_SLOTS`.

### 2. `src/pyrung/circuitpy/catalog.py`
Pure data module, depends only on `pyrung.core.tag.TagType`.

**Types:**
```python
class ModuleDirection(Enum):
    INPUT = "input"
    OUTPUT = "output"
    COMBO = "combo"

@dataclass(frozen=True)
class ChannelGroup:
    direction: ModuleDirection   # INPUT or OUTPUT (never COMBO)
    count: int                   # number of channels
    tag_type: TagType            # BOOL for discrete, INT for analog

@dataclass(frozen=True)
class ModuleSpec:
    part_number: str             # e.g. "P1-08SIM"
    description: str             # e.g. "8-ch discrete input simulator"
    groups: tuple[ChannelGroup, ...]  # 1 group for simple, 2 for combo
    # Properties: .direction, .is_combo, .input_group, .output_group
```

**`MODULE_CATALOG: dict[str, ModuleSpec]`** — 38 entries covering:
- 7 discrete input modules (8/16 ch, Bool)
- 9 discrete output modules (4/8/15/16 ch, Bool)
- 3 combo discrete modules (8 DI + 7-8 DO, Bool)
- 10 analog input modules (4/8 ch, Int)
- 4 analog output modules (4/8 ch, Int)
- 2 combo analog modules (4 AI + 2 AO, Int)
- (PWM and HSC excluded from P1)

Helper factories `_di()`, `_do()`, `_ai()`, `_ao()`, `_combo_discrete()`, `_combo_analog()` keep entries concise.

### 3. `src/pyrung/circuitpy/hardware.py`
Depends on `catalog.py` and `pyrung.core.memory_block`.

**`P1AM` class:**
- `__init__()` — empty slot dict
- `slot(number, module, *, name=None)` — validates, looks up `ModuleSpec`, builds block(s), caches, returns
  - Returns `InputBlock` for input modules
  - Returns `OutputBlock` for output modules
  - Returns `tuple[InputBlock, OutputBlock]` for combo modules
- `configured_slots` property — `dict[int, ModuleSpec]`
- `get_slot(number)` — retrieve block(s) for already-configured slot
- `__repr__()` — e.g. `P1AM(1=P1-08SIM, 2=P1-08TRS)`

**Block construction:**
- Block name defaults to `"Slot{N}"`, overridable via `name=` kwarg
- Combo blocks use `"{name}_In"` / `"{name}_Out"` suffixes
- `address_formatter` produces `"Slot1.3"` style tag names
- Channels are 1-indexed, contiguous (no `valid_ranges` needed)
- All I/O blocks are non-retentive (physical I/O)

**Constants:** `MAX_SLOTS = 15`

### 4. `tests/circuitpy/test_catalog.py`
- Every entry has valid part number, positive channel counts
- Direction properties compute correctly
- Combo modules have exactly 2 groups with opposing directions
- All 35+ module part numbers are present
- No duplicate entries

### 5. `tests/circuitpy/test_hardware.py`
- Discrete input: `slot()` returns `InputBlock`, correct type/channels/name
- Discrete output: `slot()` returns `OutputBlock`
- Analog input/output: correct `TagType.INT`
- Combo discrete: returns `tuple[InputBlock, OutputBlock]`, both correct
- Combo analog: same tuple pattern
- Tag naming: `Slot1.1`, `Slot1.2`, etc.
- Custom name: `hw.slot(1, "P1-08SIM", name="Inputs")` → `Inputs.1`
- Combo naming: `Slot3_In.1`, `Slot3_Out.1`
- Validation errors: slot < 1, slot > 15, unknown module, duplicate slot
- `configured_slots` property
- `repr` output
- Integration: build a Program with P1AM blocks, run with PLCRunner, verify tag values propagate

## Module Catalog Data Sources

The module catalog entries (part numbers, channel counts, I/O direction, data types) are derived from:

- **Arduino `Module_List.h`**: https://github.com/facts-engineering/P1AM/blob/master/src/Module_List.h — master list of all P1000-series modules with DI/DO/AI/AO byte counts and data sizes
- **CircuitPython P1AM library**: https://github.com/facts-engineering/CircuitPython_P1AM — Python API (`Base`, `IO_Module`, `IO_Channel`) showing channel access patterns
- **P1AM Python API reference**: https://facts-engineering.github.io/api_reference.html — `readDiscrete`, `writeDiscrete`, `readAnalog`, `writeAnalog`, `readTemperature` signatures and channel semantics
- **P1AM-200 hardware docs**: https://facts-engineering.github.io/modules/P1AM-200/P1AM-200.html — slot limits, power budget, base controller details
- **P1AM-200 datasheet**: https://cdn.automationdirect.com/static/specs/p1am200specs.pdf
- **CircuitPython helpers**: https://github.com/facts-engineering/CircuitPython_p1am_200_helpers — LED, Ethernet, RTC initialization helpers
- **AutomationDirect product catalog**: https://www.automationdirect.com/adc/shopping/catalog/programmable_controllers/productivity1000_plcs_(stackable_micro) — full module listing with specs

### Module count cross-reference

`Module_List.h` defines 38 modules (excluding "Empty" and "BAD SLOT" sentinels). Our catalog covers 35 of those (excluding P1-04PWM, P1-02HSC specialty modules deferred to P2). Channel counts were verified against the API reference (`readDiscrete` returns bitmapped values matching DI byte counts; `readAnalog`/`writeAnalog` match AI/AO channel counts).

### Combo module channel splits

Combo modules are not explicitly documented with per-direction channel counts in `Module_List.h` (it lists DI bytes + DO bytes). The splits were derived from:
- P1-16CDR: 1 DI byte (8 ch) + 1 DO byte (8 ch) = 8 DI + 8 DO
- P1-15CDD1/CDD2: 1 DI byte (8 ch) + 1 DO byte (7 ch) = 8 DI + 7 DO
- P1-4ADL2DAL-1/2: AI bytes matching 4-ch ADL + AO bytes matching 2-ch DAL = 4 AI + 2 AO

## Critical Reference Files

- `src/pyrung/core/memory_block.py` — `InputBlock`, `OutputBlock`, `Block` constructors, `address_formatter` callback
- `src/pyrung/click/__init__.py` — `_block_from_bank_config()` pattern, dialect package structure
- `src/pyrung/core/tag.py` — `TagType` enum
- `tests/click/test_aliases.py` — reference for dialect block/tag tests

## Verification

```bash
make test    # all existing tests still pass
make lint    # no ruff/codespell/ty issues
```

Then confirm:
```python
from pyrung.circuitpy import P1AM
hw = P1AM()
inputs = hw.slot(1, "P1-08SIM")
outputs = hw.slot(2, "P1-08TRS")
Button = inputs[1]
Light = outputs[1]
# Button is a LiveInputTag, Light is a LiveOutputTag
# Can be used in Program/Rung/PLCRunner as normal
```
