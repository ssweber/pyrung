# Stage 3: Validation Facade, Hardware Profile, and Extended Rules

## Goal

Three additions that build on Stages 1–2 without modifying them:

1. **`Program.validate()` facade** — generic dialect dispatch so users can validate without knowing the Click-specific entry point.
2. **`ClickProfile`** — a data model encoding Click hardware bank capabilities (writable, valid instruction roles, copy compatibility).
3. **Extended rules (R6+)** — bank-level constraint checks consuming the profile alongside TagMap and walker facts.

---

## Relationship to Stages 1–2

- Stage 1 walker and Stage 2 click policy are unchanged.
- Stage 3 adds orchestration (facade) and richer constraint data (profile).
- `TagMap.validate()` remains the primary Click-specific entry point.
- `Program.validate()` is a convenience facade for multi-dialect codebases.

---

## Part A: `Program.validate()` facade

### Design

Add a class-level registry and thin dispatch on `Program` so core never imports click:

```python
class Program:
    _dialect_validators: ClassVar[dict[str, Callable]] = {}

    @classmethod
    def register_dialect(cls, name: str, validator_fn: Callable) -> None:
        cls._dialect_validators[name] = validator_fn

    def validate(self, dialect: str, *, mode: str = "warn", **kwargs) -> Any:
        return self._dialect_validators[dialect](self, mode=mode, **kwargs)
```

Click self-registers on import:

```python
# In pyrung/click/__init__.py
Program.register_dialect(
    "click",
    lambda prog, mode, **kw: validate_click_program(prog, kw["tag_map"], mode),
)
```

Usage:

```python
report = program.validate(dialect="click", mode="strict", tag_map=tm)
```

### Files to modify

1. `src/pyrung/core/program.py` — add `_dialect_validators`, `register_dialect()`, `validate()`
2. `src/pyrung/click/__init__.py` — add `register_dialect` call

### Scope

- Purely additive.
- No changes to Stage 1 walker or Stage 2 policy rules.
- If no dialect is registered, `validate()` raises `KeyError` with a clear message.

---

## Part B: Hardware bank capability data — sourced from `pyclickplc`

### Why this exists

Stage 2 rules (R1–R5) check operand *placement* (where pointers/expressions/indirect ranges appear). They don't check *bank-level* constraints:

- Is the target bank writable?
- Is this bank valid for this instruction role (timer done_bit, counter accumulator, etc.)?
- Are source and dest banks compatible for copy/block_copy?

This knowledge is static Click hardware truth. **It belongs in `pyclickplc`**, not in pyrung. `pyclickplc` already defines `BANKS` (which pyrung uses to build pre-built blocks). Bank capabilities are the same kind of hardware-level data.

### Where the data lives

`pyclickplc` should expose a bank capability model. The exact shape is up to pyclickplc, but pyrung needs at minimum:

- **writable** — can this bank be a write target?
- **valid_roles** — what instruction roles is this bank valid for? (e.g., timer done_bit, counter accumulator, pointer source)
- **copy_compatibility** — which bank pairs are valid for copy/block_copy source→dest?

Strawman API that pyclickplc could expose:

```python
# In pyclickplc (upstream)
from pyclickplc import BANKS

BANKS["X"].writable       # False
BANKS["DS"].valid_roles   # frozenset({"pointer"})
BANKS["T"].valid_roles    # frozenset({"timer_done"})

# Or a standalone lookup
from pyclickplc import bank_capabilities
bank_capabilities.is_writable("X")                    # False
bank_capabilities.valid_for_role("T", "timer_done")   # True
bank_capabilities.copy_compatible("DS", "DD")          # True
```

The exact design is a pyclickplc concern. pyrung's validation just consumes whatever pyclickplc provides.

### pyrung's consumption layer

pyrung wraps the pyclickplc data behind a thin protocol so validation rules don't couple to pyclickplc's exact API shape:

```python
# In pyrung/click/validation.py (or a small adapter module)
from typing import Protocol

class HardwareProfile(Protocol):
    def is_writable(self, memory_type: str) -> bool: ...
    def valid_for_role(self, memory_type: str, role: str) -> bool: ...
    def copy_compatible(self, source_type: str, dest_type: str) -> bool: ...
```

The validation entry point accepts this protocol:

```python
def validate_click_program(
    program: Program,
    tag_map: TagMap,
    mode: ValidationMode = "warn",
    profile: HardwareProfile | None = None,
) -> ClickValidationReport:
    ...
```

When `profile` is provided, R6–R8 rules run. When `None`, only R1–R5 run (Stage 2 behavior preserved).

### Implementation options

1. **pyclickplc already has the data** — write a thin adapter in `pyrung/click/` that wraps it into the `HardwareProfile` protocol. Preferred.
2. **pyclickplc doesn't have it yet** — contribute the capability model to pyclickplc first, then consume it. This may gate Stage 3 R6–R8 on a pyclickplc release.
3. **Interim fallback** — if pyclickplc isn't ready, pyrung can ship a provisional `_ClickProfileFallback` that hardcodes the data, clearly marked as temporary and to be replaced.

### Files to add (pyrung side)

1. `src/pyrung/click/profile.py` — adapter wrapping pyclickplc bank data into `HardwareProfile`
2. `tests/click/test_profile.py` — tests against the adapter

### Files to modify

1. `src/pyrung/click/validation.py` — accept optional `profile` parameter, use it in R6+ rules
2. `src/pyrung/click/__init__.py` — export adapter

---

## Part C: Extended rules (R6+)

These rules consume walker facts + TagMap + `HardwareProfile` (from pyclickplc via adapter) together. They only run when a profile is provided to `validate_click_program()`.

### Rule R6: Writable target

If an instruction writes to a tag (e.g., `OutInstruction.target`, `CopyInstruction.target`, `MathInstruction.dest`):

- resolve the tag's memory type via `mapped_slots()`,
- check `profile.is_writable(memory_type)`.

Else emit `CLK_BANK_NOT_WRITABLE`.

### Rule R7: Instruction role compatibility

Certain instruction fields require specific bank roles:

- `OnDelayInstruction.done_bit` / `OffDelayInstruction.done_bit` → must be `timer_done` role (T bank)
- `OnDelayInstruction.timer_tag` / `OffDelayInstruction.timer_tag` → must be `timer_accum` role (TD bank)
- `CountUpInstruction.done_bit` / `CountDownInstruction.done_bit` → must be `counter_done` role (CT bank)
- `CountUpInstruction.accumulator` / `CountDownInstruction.accumulator` → must be `counter_accum` role (CTD bank)

Check via `profile.valid_for_role(memory_type, required_role)`.

Else emit `CLK_BANK_WRONG_ROLE`.

### Rule R8: Copy bank compatibility

For `CopyInstruction` and `BlockCopyInstruction`:

- resolve source and dest memory types,
- check `profile.copy_compatible(source_type, dest_type)`.

Else emit `CLK_COPY_BANK_INCOMPATIBLE`.

### Finding codes (Stage 3)

- `CLK_BANK_NOT_WRITABLE`
- `CLK_BANK_WRONG_ROLE`
- `CLK_COPY_BANK_INCOMPATIBLE`

### Test plan (Stage 3 rules)

1. Write to X bank input → `CLK_BANK_NOT_WRITABLE`.
2. Timer done_bit in non-T bank → `CLK_BANK_WRONG_ROLE`.
3. Counter accumulator in non-CTD bank → `CLK_BANK_WRONG_ROLE`.
4. Copy from incompatible bank pair → `CLK_COPY_BANK_INCOMPATIBLE`.
5. All valid cases produce zero findings for these rules.

---

## Relationship to DSL strict mode

`Program(strict=True)` (the DSL control flow guard) and `validate(mode="strict")` are **separate concerns**:

| | DSL guard (`strict=True`) | Dialect validation (`validate(mode=...)`) |
|---|---|---|
| **When** | During program construction (as rungs are added) | After program is fully built |
| **What** | Structural errors (bad nesting, missing subroutines, control flow) | Hardware portability (pointer rules, expression placement, memory banks) |
| **Scope** | Core — dialect-agnostic | Dialect-specific (click, future others) |
| **Output** | Raises immediately on violation | Returns a report (non-raising by default) |

These must not share an entry point. A program that is structurally valid (passes DSL guard) may still be non-portable to Click hardware. Conversely, a Click-portable program with a bad subroutine call is structurally broken regardless of dialect.

Typical workflow:

```python
with Program(strict=True) as prog:     # catches structural mistakes at build time
    with Rung(Button):
        out(Light)

report = tag_map.validate(prog, mode="strict")  # checks Click portability after build
```

---

## Non-goals

- No changes to Stage 1 walker or Stage 2 rules.
- No automatic code rewriting or remediation.
- No runtime execution changes.
- pyrung does not define Click hardware truth — that belongs in `pyclickplc`. pyrung only consumes it.
- R6–R8 are gated on pyclickplc exposing bank capability data. If it doesn't yet, these rules wait or use a clearly-marked provisional fallback.
