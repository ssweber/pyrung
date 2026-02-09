# pyrung Cleanup Plan: Step 4 + Step 5A (Alias Relocation)

## Summary
Implement Step 4 now as a breaking cleanup in `pyrung.core`, and include a narrow Step 5A in the same milestone to preserve Click-style naming under `pyrung.click`.
Defer Step 5B (`ClickDataProvider`) to a later milestone because required mapping/thread-safety decisions are still open in spec.

## Scope
In scope:
- Remove deprecated Click aliases from core enum and constructors.
- Remove deprecated core exports (`Bit`, `Int2`, `Float`, `Txt`).
- Migrate tests/docs to IEC names only in core.
- Add `pyrung.click` package with constructor alias re-exports only.

Out of scope:
- `ClickDataProvider` implementation.
- TagMap implementation.
- pyclickplc dependency wiring in `pyrung` package.
- Soft PLC runtime integration.

## Public API Changes
Removed from `pyrung.core`:
- `TagType.BIT`, `TagType.INT2`, `TagType.FLOAT`, `TagType.HEX`, `TagType.TXT`
- `TagType._missing_()` alias parsing (`"bit"`, `"int2"`, etc.)
- Constructors: `Bit()`, `Int2()`, `Float()`, `Txt()`
- Core re-exports of those constructor names from `src/pyrung/core/__init__.py`

Added in `pyrung.click`:
- `Bit`, `Int2`, `Float`, `Hex`, `Txt` as alias constructors mapped to core IEC constructors.

Unchanged:
- Core runtime behavior, state model, instructions, truncation semantics.
- Canonical core API: `Bool`, `Int`, `Dint`, `Real`, `Word`, `Char`, `TagType`.

## Implementation Plan
1. Core type cleanup in `src/pyrung/core/tag.py`.
- Delete deprecated enum alias members.
- Delete `_missing_()` alias resolver.
- Delete deprecated helper constructors (`Bit`, `Int2`, `Float`, `Txt`).
- Update docstrings referencing old names.

2. Core export cleanup in `src/pyrung/core/__init__.py`.
- Remove imports of deprecated constructors.
- Remove deprecated names from `__all__`.

3. Test migration to IEC-only core API.
- Replace constructor usage in `tests/core/*`:
- `Bit(` -> `Bool(`
- `Int2(` -> `Dint(`
- `Float(` -> `Real(`
- `Txt(` -> `Char(`
- Replace enum assertions:
- `TagType.BIT` -> `TagType.BOOL`
- `TagType.FLOAT` -> `TagType.REAL`
- Update import lines accordingly.

4. Add explicit breakage checks in tests (`tests/core/test_tag.py`).
- Assert deprecated enum names are absent.
- Assert deprecated `TagType("bit")`-style parsing now fails (`ValueError`).
- Assert core defaults remain unchanged (`Tag()` defaults to `TagType.BOOL`).

5. Create Step 5A alias home in `src/pyrung/click/__init__.py`.
- Add constructor aliases only:
- `Bit = Bool`
- `Int2 = Dint`
- `Float = Real`
- `Hex = Word`
- `Txt = Char`
- Define `__all__` for these alias names.
- Do not import pyclickplc here yet.

6. Add click-alias tests in `tests/click/test_aliases.py`.
- Verify each alias returns a `Tag` with correct IEC `TagType`.
- Verify aliases are available from `pyrung.click`.
- Verify aliases are not available from `pyrung.core`.

7. Documentation updates.
- Update `README.md` examples to IEC names in core.
- Add migration note: Click aliases moved from `pyrung.core` to `pyrung.click`.
- Update `spec/HANDOFF.md` step status to reflect Step 4 done and Step 5A done.

## Test Cases and Scenarios
- Core API imports:
- `from pyrung.core import Bool, Dint, Real, Char` succeeds.
- `from pyrung.core import Bit` fails.
- Enum behavior:
- `TagType.BOOL` works.
- `TagType.BIT` missing.
- `TagType("bit")` fails.
- Functional regression:
- Existing core behavior tests still pass after constructor renames.
- Click alias namespace:
- `from pyrung.click import Bit, Int2, Float, Hex, Txt` succeeds.
- Returned tags map to `TagType.BOOL`, `TagType.DINT`, `TagType.REAL`, `TagType.WORD`, `TagType.CHAR`.
- Full validation:
- `make test`
- `make lint`
- optionally `make` as final gate

## Acceptance Criteria
- No deprecated Click alias symbols remain in `src/pyrung/core/*`.
- All tests pass using IEC names in core.
- `pyrung.click` provides Click alias constructors.
- Core and click import boundaries match spec intent.

## Assumptions and Defaults
- Breaking changes in core are acceptable in this milestone.
- Step 5 for this cleanup means alias relocation only (Step 5A), not `ClickDataProvider`.
- `ClickDataProvider` is deferred until mapping and concurrency semantics are specified.
- Existing unrelated local edits are preserved.
