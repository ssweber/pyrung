# Core Types — Handoff Brief

> **Status:** Handoff — decisions captured, needs full spec writeup.
> **Depends on:** `overview.md` (architecture, layer boundaries)
> **Referenced by:** Every other spec file.

---

## Scope

This file specifies the foundational type system: tags, blocks, and the IEC type system. Everything else in pyrung builds on these.

---

## Decisions Made

### Tag Hierarchy

```
Tag                  ← standalone (Bool, Int, etc.) or Block[n]
├── InputTag         ← InputBlock[n] only
└── OutputTag        ← OutputBlock[n] only
```

- **Tag** is a single named, typed value. Always internal memory. No `.immediate`.
- **InputTag** adds `.immediate` property. Only created by indexing an `InputBlock`.
- **OutputTag** adds `.immediate` property. Only created by indexing an `OutputBlock`.
- Standalone constructors (`Bool("X")`, `Int("X")`) always produce plain `Tag`.
- Tags carry type metadata but **no state**. Values live only in `SystemState.tags`.

### Block Hierarchy

```
Block                ← internal memory
├── InputBlock       ← physical inputs
└── OutputBlock      ← physical outputs
```

- **Block** is a named, typed, 1-indexed array of tags.
- `Block[0]` is always `IndexError`. PLC addressing starts at 1.
- `Block[n]` returns `Tag`. `InputBlock[n]` returns `InputTag`. `OutputBlock[n]` returns `OutputTag`.
- `Block[tag]` returns an `IndirectRef` resolved at runtime (pointer addressing).
- `Block[expr]` returns an `IndirectExprRef` for computed addresses (e.g., `Block[idx + 1]`).
- `Block.select(start, end)` returns a `MemoryBlock` for block operations (blockcopy, fill, pack). Both bounds inclusive.
- `Block.name` + index generates the tag key for `SystemState.tags` by default (e.g., `"Alarms3"`), but dialects may provide an address formatter override.

### Constructor

```python
Block(
    name: str,
    type: TagType,
    start: int,
    end: int,
    retentive: bool = False,
    valid_ranges: tuple[tuple[int, int], ...] | None = None,
    address_formatter: Callable[[str, int], str] | None = None,
)
InputBlock(...)
OutputBlock(...)
```

- `start` and `end` are both **inclusive**. `Block("DS", TagType.INT, 1, 100)` means indices 1–100.
- `start` must be 1 or greater. PLC addressing starts at 1.
- `retentive` only applies to `Block`. InputBlock/OutputBlock are never retentive.
- `valid_ranges` is optional. If unset, all addresses in `[start, end]` are valid. If set, only addresses within the listed segments are valid.
- `address_formatter` is optional. If set, it controls generated tag names (used by dialects such as Click for canonical display names like `X001`).
- When type is omitted, it can be inferred at map time (Click dialect feature — see `click.md`).

### .select(start, end)

Selects a range window of tags for block operations.

```python
Block.select(start: int | Tag | Expression, end: int | Tag | Expression) -> MemoryBlock | IndirectMemoryBlock
```

- Both bounds are **inclusive**: `DS.select(1, 100)` selects tags 1–100 (100 tags for contiguous blocks).
- `start > end` is invalid and raises `ValueError`.
- Symmetrical with the constructor: `Block("DS", INT, 1, 100)` defines 1–100, `DS.select(1, 100)` selects 1–100.
- For sparse blocks (`valid_ranges` set), `.select(start, end)` returns all valid addresses inside the inclusive window and skips invalid gaps.
- When both arguments are `int`, returns a `MemoryBlock` (resolved at definition time).
- When either argument is a `Tag` or `Expression`, returns an `IndirectMemoryBlock` (resolved at scan time).

```python
# Static range
DS.select(1, 100)              # → MemoryBlock, tags 1–100

# Sparse window (e.g., Click X/Y style ranges)
X.select(1, 21)                # → MemoryBlock, valid tags in window (1..16, 21)

# Dynamic range via pointer
DS.select(idx, idx + 10)       # → IndirectMemoryBlock, resolved each scan

# Mixed static/dynamic
DS.select(1, count)            # → IndirectMemoryBlock
```

Python's `Block[n:m]` slice syntax is **not supported**. Slice semantics are half-open (`[start, stop)`) which conflicts with PLC's inclusive convention and creates off-by-one traps. `.select()` is explicit and unambiguous.

### IndirectMemoryBlock

Runtime-resolved range window, returned by `.select()` when either bound is dynamic.

```python
IndirectMemoryBlock.resolve_ctx(ctx: ScanContext) -> MemoryBlock
```

- Evaluates `start` and `end` expressions against current state.
- Returns a concrete `MemoryBlock` for the resolved range.
- Applies the same validation rules as static `.select()`.
- Raises `ValueError` if resolved bounds are invalid (`start > end`) and `IndexError` if bounds are out of block range.

### IEC 61131-3 Type Constructors

These are convenience functions that return `Tag`:

```python
Bool(name, retentive=False) → Tag(name, TagType.BOOL, retentive)
Int(name, retentive=False)  → Tag(name, TagType.INT, retentive)
Dint(name, retentive=False) → Tag(name, TagType.DINT, retentive)
Real(name, retentive=False) → Tag(name, TagType.REAL, retentive)
Word(name, retentive=False) → Tag(name, TagType.WORD, retentive)
Char(name, retentive=False) → Tag(name, TagType.CHAR, retentive)
```

Click aliases (`Bit`, `Float`, `Hex`, `Txt`, `Int2`) live in `pyrung.click`, not core.

### TagType Enum

```python
class TagType(Enum):
    BOOL = "bool"      # 1 bit
    INT  = "int"       # 16-bit signed
    DINT = "dint"      # 32-bit signed
    REAL = "real"       # 32-bit IEEE 754
    WORD = "word"       # 16-bit unsigned
    CHAR = "char"       # 8-bit ASCII
```

### .immediate

- `.immediate` is a property on `InputTag` and `OutputTag` only.
- Returns an `ImmediateRef` wrapper that instructions unwrap.
- `Tag` (from `Block`) does **not** have `.immediate` — it's an `AttributeError`.
- In simulation: validation-time check, no runtime behavior.
- In Click: transcription hint.
- In CircuitPython: different codegen.

---

## Needs Specification

- **Tag identity:** How are tags identified in `SystemState.tags`? By name string? By object identity? Collision rules for duplicate names.
- **Tag name validation:** Core should validate basic rules (non-empty, no whitespace?). Dialect-specific rules (Click's 24-char max, forbidden chars) belong in the dialect validator.
- **IndirectRef:** Full specification of pointer/indirect addressing. What types are valid as indices? How does resolution work at runtime? Error behavior for out-of-range.
- ~~**Slice:**~~ Resolved — replaced by `.select(start, end)` with inclusive bounds. No slice syntax on `__getitem__`.
- **Retentive semantics:** What does `retentive=True` mean in simulation? (Probably: survives a simulated power cycle / `runner.reset()`.)
- **Operator overloading:** Tags support `==`, `!=`, `<`, `<=`, `>`, `>=` for conditions, and `+`, `-`, `*`, `/`, `%`, `&`, `|`, `^` for math expressions. Specify what these return (Condition objects, Expression objects).
- **Named attribute access:** `Setpoints.Max_Temp` as alias for `Setpoints["Max Temp"]`. Specify the name-mangling rules (spaces → underscores, etc.).
- **Click metadata bridge:** This is Click-dialect-specific. Documented in `click.md`, not here. Core `Block` class has no knowledge of Click bank metadata types.
