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
- `Block[n:m]` returns a `Slice` object for block operations (blockcopy, fill, pack).
- `Block[tag]` returns an `IndirectRef` resolved at runtime (pointer addressing).
- `Block.name` + index generates the tag key for `SystemState.tags` (e.g., `"Alarms_3"`).

### Constructor

```python
Block(name: str, type: TagType, indices: range, retentive: bool = False)
InputBlock(name: str, type: TagType, indices: range)
OutputBlock(name: str, type: TagType, indices: range)
```

- `indices` must start at 1 or greater. `range(1, 101)` means indices 1–100.
- `retentive` only applies to `Block`. InputBlock/OutputBlock are never retentive.
- When type is omitted, it can be inferred at map time (Click dialect feature — see `click.md`).

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
- In Cricket: different codegen.

---

## Needs Specification

- **Tag identity:** How are tags identified in `SystemState.tags`? By name string? By object identity? Collision rules for duplicate names.
- **Tag name validation:** Core should validate basic rules (non-empty, no whitespace?). Dialect-specific rules (Click's 24-char max, forbidden chars) belong in the dialect validator.
- **IndirectRef:** Full specification of pointer/indirect addressing. What types are valid as indices? How does resolution work at runtime? Error behavior for out-of-range.
- **Slice:** Specification of block slicing. What does `Block[3:7]` return? How do blockcopy/fill consume it?
- **Retentive semantics:** What does `retentive=True` mean in simulation? (Probably: survives a simulated power cycle / `runner.reset()`.)
- **Operator overloading:** Tags support `==`, `!=`, `<`, `<=`, `>`, `>=` for conditions, and `+`, `-`, `*`, `/`, `%`, `&`, `|`, `^` for math expressions. Specify what these return (Condition objects, Expression objects).
- **Named attribute access:** `Setpoints.Max_Temp` as alias for `Setpoints["Max Temp"]`. Specify the name-mangling rules (spaces → underscores, etc.).
- **`from_meta()` bridge:** This is Click-dialect-specific. Documented in `click.md`, not here. Core `Block` class has no knowledge of `MemoryBankMeta`.
