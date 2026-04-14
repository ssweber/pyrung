# Plan: Tag Metadata Hints for pyrung

## Context

Some Int/Dint tags represent enums or internal state (sequencer step, mode selector) that users shouldn't casually force/patch from the Data View panel. Currently there's no way to distinguish `Int('SensorReading')` from `Int('MachineState')`. This feature adds optional `choices` and `readonly` kwargs to tag constructors, flows them through DAP trace to the VS Code Data View (dropdowns for choices, visual indicators for readonly), and adds a `[...]` TagMeta grammar for CSV round-tripping in Click nickname files. Hints are informational only — the engine stays permissive.

## API

```python
class Mode(IntEnum):
    IDLE = 0
    RUNNING = 1
    FAULT = 2

MachineState = Int('MachineState', choices=Mode)           # IntEnum
Priority = Int('Priority', choices={1: 'Low', 2: 'High'})  # dict
InternalCounter = Dint('InternalCounter', readonly=True)
Light = Bool('Light')                                       # unchanged
```

Timer/Counter `.Done`/`.Acc` are not automatically readonly — users opt in on their own clones.

## TagMeta CSV Grammar

Bracket-delimited metadata in the Click nickname comment field. Position-independent — can appear anywhere in the comment alongside block tags and human text:

```
Motor speed [readonly] <Volume />
Step number [choices=IDLE:0|RUN:1|FAULT:2] <Machine.State>
[readonly, choices=OFF:0|ON:1]
```

- `readonly` — bare flag, no value needed
- `choices=LABEL:value|LABEL:value` — pipe-separated, `LABEL:value` pairs
- Comma separates multiple attrs: `[readonly, choices=...]`
- Parsed by new `parse_tag_meta()` / `format_tag_meta()` in pyrung (not pyclickplc)

## Scope

| Concern | In scope? |
|---|---|
| Core tag creation (`choices=`, `readonly=`) | Yes |
| UDT `Field()` support (`choices=`, `readonly=`) | Yes |
| DAP trace serialization (`tagHints`) | Yes |
| VS Code Data View rendering | Yes |
| Click nickname CSV: TagMeta `[...]` parse/emit | Yes |
| Click nickname CSV: preserve `bg_color` on round-trip | Yes |
| Click ladder codegen | No — PLCs don't have enums |
| pyclickplc changes | No |
| `Block.slot()` hint overrides | Yes |

## File Changes

### 1. `src/pyrung/core/tag.py` — Add `choices` and `readonly` to `Tag`

**Add fields to `Tag`** (after `comment`, line 59):
```python
choices: dict[int | float | bool | str, str] | None = field(default=None)
readonly: bool = False
```

**Add IntEnum normalization helper** (module-level):
```python
def _normalize_choices(raw) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, type) and issubclass(raw, enum.IntEnum):
        return {member.value: member.name for member in raw}
    return dict(raw)
```

**Thread through `_TagTypeBase.__init__` and `__new__`** (lines 543–568):
Add `choices` and `readonly` kwargs, normalize choices, pass to `LiveTag(...)`.

### 2. `src/pyrung/core/structure.py` — Hints on UDT fields

**Add to `_FieldSpec`** (line 94–99):
```python
choices: dict[int | float | bool | str, str] | None = None
readonly: bool = False
```

**Add to `Field`** and its `__new__`:
```python
choices: type[IntEnum] | dict | None = None
readonly: bool = False
```

**In `_build_field_spec`** (line 532–556), extract from `raw_default` when it's a `Field`.

**In `_StructRuntime.__init__`** (around line 252), stamp on each block:
```python
block._pyrung_field_choices = field_spec.choices
block._pyrung_field_readonly = field_spec.readonly
```

**In `.fields` property**, thread through to `Field(...)`.

### 3. `src/pyrung/core/memory_block.py` — Slot overrides + structure propagation

**Add override dicts** (alongside existing `_slot_name_overrides`, etc.):
```python
self._slot_choices_overrides: dict[int, dict] = {}
self._slot_readonly_overrides: dict[int, bool] = {}
```

**Add to `Block.slot()`** (line 348) — new kwargs `choices` and `readonly`:
```python
def slot(self, addr, ..., choices=UNSET, readonly=None):
    if choices is not UNSET:
        self._slot_choices_overrides[addr] = _normalize_choices(choices)
    if readonly is not None:
        self._slot_readonly_overrides[addr] = bool(readonly)
```

**Add to `SlotView`** — `choices`, `readonly`, `choices_overridden`, `readonly_overridden` properties.

**Add to `SlotView.reset()`** — clear choices/readonly overrides.

**In `_get_tag`** (line 253), read slot overrides and pass to `_new_tag_for_slot`:
```python
choices = self._slot_choices_overrides.get(addr)
readonly = self._slot_readonly_overrides.get(addr, False)
```

**In `_new_tag_for_slot`** (line 266), pass `choices`/`readonly` to `LiveTag(...)`.

**In `_annotate_tag`** (lines 278–288), propagate structure-level hints (from `_FieldSpec`) as fallback when no slot override exists:
```python
field_choices = getattr(self, "_pyrung_field_choices", None)
if field_choices is not None and tag.choices is None:
    object.__setattr__(tag, "choices", field_choices)
field_readonly = getattr(self, "_pyrung_field_readonly", False)
if field_readonly and not tag.readonly:
    object.__setattr__(tag, "readonly", True)
```
Priority: explicit slot override > structure field hint > default (None/False).

### 4. `src/pyrung/core/__init__.py` — Exports

No new classes. `choices` and `readonly` are kwargs on existing constructors and `Field`.

### 5. `src/pyrung/dap/adapter.py` — Serialize hints in trace events

**Add `_tag_hints_locked` static method** (after `_tag_groups_locked`):
```python
@staticmethod
def _tag_hints_locked(runner: Any) -> dict[str, dict[str, Any]]:
    hints: dict[str, dict[str, Any]] = {}
    for name, tag in runner._known_tags_by_name.items():
        entry: dict[str, Any] = {}
        choices = getattr(tag, "choices", None)
        if choices is not None:
            entry["choices"] = {str(k): v for k, v in choices.items()}
        if getattr(tag, "readonly", False):
            entry["readonly"] = True
        if entry:
            hints[name] = entry
    return hints
```

**Add `tagHints` to both trace body builders** (`_current_trace_body_locked` and `_live_trace_body_locked`).

### 6. `editors/vscode/pyrung-debug/extension.js` — Pass hints to Data View

Add `body.tagHints || {}` as 5th arg to `dataView.updateTrace(...)`.

### 7. `editors/vscode/pyrung-debug/dataViewProvider.js` — Render hints

- Accept `tagHints` param in `updateTrace`, filter and forward to webview.
- **Choice tags**: `<select>` dropdown showing `label (value)` options.
- **Value display**: Show `IDLE (0)` instead of raw `0` when label matches.
- **Readonly tags**: Dim Force button, visual indicator, tooltip. Force/patch still works.

### 8. `src/pyrung/click/tag_map/_parsers.py` — TagMeta grammar

**New `TagMeta` dataclass**:
```python
@dataclass(frozen=True)
class TagMeta:
    readonly: bool = False
    choices: dict[int | float | str, str] | None = None
```

**New `parse_tag_meta(comment: str) -> tuple[TagMeta | None, str]`**:
- Finds `[...]` anywhere in comment string (position-independent).
- Parses `readonly` as bare flag, `choices=LABEL:value|LABEL:value` with pipe separator.
- Returns `(TagMeta, remaining_text)` with brackets stripped.
- Returns `(None, original)` if no brackets found.

**New `format_tag_meta(meta: TagMeta) -> str`**:
- Produces `[readonly, choices=LABEL:value|LABEL:value]` or subset.
- Returns empty string for default/empty meta.

### 9. `src/pyrung/click/tag_map/_parsers.py` — Fix `_extract_address_comment`

Update to strip both block tags AND tag meta, returning only human text:
```python
def _extract_address_comment(comment: str) -> tuple[str, TagMeta | None, str | None]:
    parsed = parse_block_tag(comment)
    remaining = parsed.remaining_text if parsed.name else comment
    bg_color = parsed.bg_color if parsed.name else None
    meta, human_text = parse_tag_meta(remaining)
    return human_text.strip(), meta, bg_color
```

Update `_compose_address_comment` to reassemble all three parts.

### 10. `src/pyrung/click/tag_map/_nickname_io.py` — Round-trip TagMeta + bg_color

**Import side** (`apply_block_rows`, around line 151):
- Call updated `_extract_address_comment` to get `(human_comment, tag_meta, bg_color)`.
- Store `tag_meta.readonly` and `tag_meta.choices` on the slot via `Block.slot(addr, readonly=..., choices=...)`.
- Store `bg_color` on the block entry for re-emission (side dict keyed by block name).

**Export side** (around line 608):
- Pass stored `bg_color` to `format_block_tag(name, tag_type, bg_color=...)`.
- Generate `format_tag_meta(...)` for slots with hints.
- Use updated `_compose_address_comment` to reassemble.

## Verification

1. **`make test`** — all existing tests pass.
2. **New unit tests:**
   - `Int('X', choices=Mode)` normalizes IntEnum to dict.
   - `Int('X', choices={0: 'A'})` stores dict.
   - `Int('X', readonly=True)` stores flag.
   - UDT `Field(choices=..., readonly=...)` threads through to tags.
   - `_tag_hints_locked` returns sparse dict with stringified keys.
   - Trace events include `tagHints`.
   - `parse_tag_meta` / `format_tag_meta` round-trips correctly.
   - `_extract_address_comment` handles all combos: block tag + meta + human, meta only, block tag only, human only.
   - `bg_color` preserved through nickname CSV round-trip.
3. **Manual VS Code test:** Debug with choice/readonly tags, verify Data View.

## Future (not this PR, note in commit)

- Upstream TagMeta grammar to pyclickplc if it stabilizes
