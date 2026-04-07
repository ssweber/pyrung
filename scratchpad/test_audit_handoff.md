# Test Audit Handoff

Read `scratchpad/test_audit.md` first — it's the spec. This file is
the "where we left off" companion.

## Completed

**Step 1: Helpers** (`d38b700` on `dev`)

Created `tests/click/helpers.py` with 5 helpers and
`tests/click/test_helpers.py` with 15 smoke tests.

| Helper | Purpose |
|--------|---------|
| `build_program(source)` | Triple-quoted pyrung body → `(Program, TagMap)`. Auto-declares tags by Click address prefix (C→Bool, DS→Int, T→Bool, TD→Int, CT→Bool, CTD→Dint, etc.). Injects `strict=False`. |
| `normalize_csv(rows)` | Strips header row, end() rung, trailing empty cells from `bundle.main_rows`. |
| `normalize_pyrung(code)` | Strips trailing whitespace per line, leading/trailing blank lines. |
| `strip_pyrung_boilerplate(code)` | Extracts rung body only — strips imports, tag decls, `with Program` wrapper, TagMap, and leading `comment()` calls from CSV rung comments. Dedents one level. |
| `load_fixtures(directory)` | Walks CSV dir, extracts expected pyrung from rung comments, returns `Fixture` namedtuples for parametrize. |

Key production APIs reused:
- `_OPERAND_RE` and `_parse_operand_prefix` from
  `src/pyrung/click/codegen/constants.py` / `utils.py`
- `_parse_csv` from `src/pyrung/click/codegen/parser.py` (in
  `load_fixtures`)

**Step 2: Fixture loop** (`16cdaa1` on `dev`)

Parameterized test in `tests/click/test_codegen_fixtures.py` loads all
CSVs from `tests/fixtures/user_shapes/`. Comment rows contain expected
rung body only (no tag decls, no `with Program`, no TagMap — all
derivable boilerplate). Wheatstone bridge fixture proved the loop.

**Step 3: Fixture corpus** (`a08be2b` on `dev`)

All 7 fixtures in `tests/fixtures/user_shapes/`, all passing:

1. `wheatstone_bridge.csv` — bridge topology, Shannon expansion
2. `reset_wire_and_output.csv` — rail-fed wire feeding `.reset()` +
   sibling `out()`. Exposed codegen bug: continued-rung split wasn't
   recognizing T-junction feeding both pin row and sibling output.
   Fixed in `7441f7f`.
3. `wheatstone_bridge_with_reset.csv` — bridge + pin row combined.
   Exposed codegen bug: pin rows excluded from vertical down-connection
   claims in `_grid_to_graph`, breaking bridge topology spanning into
   pin rows. Pin conditions were flat tokens instead of SP trees.
   Fixed in `0c2c9cd`.
4. `multiple_pins_separate.csv` — two terminal instructions (counter +
   RTON), each with independent reset on separate rail contacts.
5. `multiple_pins_tee_feed.csv` — T-junction feeds reset AND second
   terminal instruction below.
6. `multiple_pins_shared_reset.csv` — T-junction feeds both reset
   pins, second reset also has its own contact.
7. `non_trivial_reset_pin.csv` — reset pin row with OR parallel
   branches (any_of) and multiple ANDed contacts.

Also added `devtools/split_fixture.py` for splitting multi-rung CSVs
into one-per-file (re-adds header row).

**Step 4: Whole-output triple-quoted tests** (working tree)

Completed the deterministic whole-output conversions in
`test_ladder_export.py` and `test_codegen.py`.

- `test_ladder_export.py`: direct exporter goldens now compare
  `normalize_csv(bundle.main_rows)` against parsed literal CSV snippets
  via `_assert_export_main_rows(...)`. The helper-built
  `_normalized_rows(_row(...))` style is gone from the direct golden
  cases converted in Step 4.
- `test_codegen.py`: deterministic whole-output round-trip cases now use
  explicit goldens instead of `orig == repro`. Converted areas include
  comment-preservation, calc operator families, the full send/receive
  family, block-range forms, subroutine regressions, nickname-pipeline
  hold-outs, raw-range codegen, and NOP normalization.
- `tests/click/helpers.py`: `build_program(...)` now auto-wraps bare
  test bodies in `with Program() as p:` so codegen tests can write only
  the interesting body.
- `tests/click/test_helpers.py`: added a smoke test covering the new
  bare-body wrapping behavior.

Validation:

- `python -m pytest tests/click/test_helpers.py tests/click/test_ladder_export.py tests/click/test_codegen.py -k "not wheatstone_bridge_variants_round_trip" -q`
  → `217 passed, 1 deselected`
- `python -m pytest tests/click/test_helpers.py tests/click/test_ladder_export.py tests/click/test_codegen.py -q`
  → `218 passed`

## Next: Step 5 — Triage partial-match tests

## Steps 5–6 (future)

5. **Triage partial-match tests** — Tests using `in`, AF-only
   assertions, "renders-without-raising". Convert to full-grid, split,
   or move to Hypothesis.

6. **Delete redundancies** — After everything's in the same style,
   identical-input identical-output tests collapse.

## File sizes for context

| File | Lines | Notes |
|------|-------|-------|
| `test_ladder_export.py` | 2,248 | Exporter: pyrung → CSV. ~25 full-grid golden tests, ~12 partial-match. |
| `test_codegen.py` | 4,156 | Codegen: CSV → pyrung. Classes: CsvParsing, TopologyAnalysis, GraphWalkEdgeCases, ShannonBridgeCoverage, OperandInference, AfArgParsing, RoundTrip, InMemoryRoundTrip, NicknameMerge, CodeGeneration, ContinuedRoundTrip, StructuredCodegen, Nop. |
| `test_ladder_realistic.py` | 565 | Big realistic program. Heavy semantic names. "Renders-without-raising" + partial AF checks. Prime candidate for step 5 triage. |
| `test_codegen_smoke.py` | 37 | Likely redundant with test_codegen round-trip tests. |
| `test_codegen_project.py` | 782 | Multi-file project output. Lower priority — different concern. |

Don't split these files into folders yet. The audit steps will
naturally shrink them; split after if still needed.

## Existing CSV fixtures

- `tests/fixtures/wheatstone_bridge.csv` — 5-edge bridge shape
- `tests/fixtures/click_or_topology.csv` — 8 OR topology patterns
  (already used by native pattern golden tests in test_ladder_export)
