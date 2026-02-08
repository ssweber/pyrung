# Rich PLC Value Types & Soft PLC Architecture — Handoff

> **Status:** Handoff — decisions captured from cross-repo design session.
> **Depends on:** `pyclickplc` (external), `spec/dialects/click.md`

---

## Goal

Introduce rich value types that subclass Python builtins. `str(val)` returns PLC
display format, math/comparisons work unchanged. Unify across pyclickplc (client,
dataview, server) and pyrung (Click dialect display). Both libraries should feel
like they come from the same author.

End-state includes using pyrung as a **soft PLC** with pyclickplc's Modbus server
fronting pyrung's scan engine as the data source.

---

## Soft PLC Architecture

```
External HMI / SCADA
    | Modbus TCP
pyclickplc.server.ClickServer
    | DataProvider.read("DS1") -> PlcValue
pyrung.click adapter (implements DataProvider)
    | state.tags["DS1"]
pyrung.core.SystemState  <--  PLCRunner executes ladder logic
```

The `DataProvider` protocol is the seam between the two libraries:

```python
# pyclickplc/server.py (existing)
PlcValue = bool | int | float | str

class DataProvider(Protocol):
    def read(self, address: str) -> PlcValue: ...
    def write(self, address: str, value: PlcValue) -> None: ...
```

Rich types (e.g. `PlcWord(255)`) subclass `int`, so they satisfy `PlcValue`
transparently. No protocol changes needed.

---

## Decisions

### 1. Rich types live in pyclickplc

New module: `pyclickplc/values.py`

IEC-standard primary names, Click aliases:

| Type | Base | `str()` | Click alias |
|------|------|---------|-------------|
| `PlcBool` | int | `"1"` / `"0"` | `PlcBit` |
| `PlcInt` | int | `"42"` | — |
| `PlcDint` | int | `"42"` | `PlcInt2` |
| `PlcReal` | float | `"3.14"` (.7G) | `PlcFloat` |
| `PlcWord` | int | `"00FF"` (04X) | `PlcHex` |
| `PlcChar` | str | `"A"` | `PlcTxt` |

Click aliases are just alternate names for the same classes:

```python
PlcHex = PlcWord
PlcFloat = PlcReal
PlcInt2 = PlcDint
PlcTxt = PlcChar
PlcBit = PlcBool
```

### 2. pyrung core stays with raw primitives

`SystemState.tags` stores `bool | int | float | str`. No dependency on pyclickplc.
`_truncate_to_tag_type` continues returning raw primitives. This preserves
performance and pyrsistent PMap compatibility.

### 3. pyrung.click bridges the two

The Click dialect (which already depends on pyclickplc) wraps values at
presentation boundaries and implements the `DataProvider` adapter for soft PLC use.

```python
# pyrung/click/ — future adapter
from pyclickplc.values import PlcWord, PlcInt, ...

class ClickDataProvider:
    """Bridges pyrung SystemState to pyclickplc DataProvider protocol."""

    def read(self, address: str) -> PlcValue:
        tag_name = self._address_to_tag(address)
        return self._state.tags.get(tag_name, default)

    def write(self, address: str, value: PlcValue) -> None:
        self._runner.patch({tag_name: value})
```

### 4. Delete deprecated aliases from pyrung core

Remove from `pyrung.core`:
- `TagType` aliases: `BIT`, `INT2`, `FLOAT`, `HEX`, `TXT`
- `_missing_()` handler for deprecated string values
- Constructor aliases: `Bit()`, `Int2()`, `Float()`, `Txt()`

Re-export from `pyrung.click` only:

```python
# pyrung/click/__init__.py
from pyrung.core import Bool as Bit, Dint as Int2, Real as Float, Char as Txt
```

### 5. Naming convention: IEC primary, Click as aliases

Mirrors what pyrung already does with `TagType`. Both repos use IEC 61131-3 names
as the canonical names; Click-specific names are convenience aliases.

---

## Where values get created vs. consumed

| Path | Values are... | Rich types? |
|------|--------------|-------------|
| pyrung core (SystemState, ScanContext) | raw primitives in PMap | No |
| pyrung -> pyclickplc server (DataProvider.read) | raw primitives | No |
| pyclickplc client reads (ClickClient) | unwrapped from Modbus, wrapped in rich types | **Yes** |
| DataView I/O | CDV strings <-> rich types | **Yes** |
| pyrung state inspection / debug display | raw, wrapped for display | **Yes** |

Rich types are a **presentation layer on top of primitives**. They get created at
presentation boundaries, not in the engine.

---

## Implementation order

1. **pyclickplc `values.py`** — define the six rich types with `__str__`, `__repr__`,
   `__format__`, `.raw()`. See `pyclickplc/spec/HANDOFF.md` for display rules.
2. **pyclickplc client/dataview** — return rich types from reads and conversions.
3. **pyrung core cleanup** — delete deprecated aliases from `TagType` and `tag.py`.
4. **pyrung.click dialect** — re-export Click aliases, implement `ClickDataProvider`.
5. **Soft PLC integration** — wire `ClickDataProvider` + `ClickServer` + `PLCRunner`.

---

## Open questions

- Should `PlcBool` subclass `int` (like Python's `bool` does) or use a custom approach?
  Python's `bool` is final and cannot be subclassed.
- `.raw()` — return the base type (`int(val)`) or just be an alias for clarity?
- `__format__` protocol — support `:plc` format spec? Others?
- Should `ClickDataProvider.read()` return rich types or raw primitives?
  (Server doesn't need rich types, but other consumers of the adapter might.)
