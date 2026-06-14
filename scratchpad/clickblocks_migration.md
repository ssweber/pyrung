# ClickBlocks migration — remove singletons

## Goal

Remove the module-level singleton blocks (`x`, `y`, `c`, `ds`, etc.) from
`pyrung.click` and the `reset_banks()` function.  Every caller unpacks
fresh blocks from `ClickBlocks()` instead.

**Breaking change** — `from pyrung.click import ds` stops working.

## Why it's broken today

`ClickBlocks()` exists but internal code still imports singletons:
- `to_nickname_file()` iterates `_ALL_BANKS` singletons for slot overrides
  — misses everything on the user's fresh blocks
- `_parsers.py` resolves "DS165" → `ds[165]` on the singleton
- `data_provider.py` maps SystemState to Modbus via singleton banks

If someone uses `ClickBlocks()`, these internals look at the wrong blocks.

## The fix: same trick as `_mapped_tags`

TagMap already holds references to the user's blocks via
`entry.target._pyrung_block`.  Internal code should discover blocks
from the TagMap's entries, not import globals.

- `to_nickname_file()` → walk `self._entries` to find blocks with overrides
- `_parsers.py` → receive blocks as parameter or from TagMap context
- `data_provider.py` → receive blocks from TagMap or explicit parameter

No API redesign needed — just stop importing globals, use the references
you already have.

## The one-liner

Every file that imports blocks changes from:

```python
from pyrung.click import x, y, c, ds, dd, dh, df, ...
```

to:

```python
from pyrung.click import ClickBlocks
x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()
```

## What to delete

- `pyrung/click/__init__.py`:
  - The module-level unpack line
  - `_ALL_BANKS` tuple
  - `reset_banks()` function
  - Remove `x`, `y`, `c`, etc. from `__all__`
- `tests/conftest.py`: `_clean_click_banks` autouse fixture
- `tests/click/helpers.py`: `_clean_click_banks_for_exec` fixture
- Every `reset_banks()` call in tests

## Migration order

1. Fix internals: `to_nickname_file`, `_parsers`, `data_provider` —
   thread blocks from TagMap entries instead of importing singletons
2. Delete singletons + `reset_banks()` + `_ALL_BANKS` from `__init__.py`
3. Delete conftest/helpers fixtures
4. Fix tests (~40 files, mechanical — agent-parallelizable)
5. Fix docs/examples
6. Codegen emitter: emit `ClickBlocks()` + unpack instead of
   `from pyrung.click import ds, x, ...`

## Testing strategy

Run `make test` after each batch.  Failures are all ImportError /
AttributeError (missing names) — loud and obvious.
