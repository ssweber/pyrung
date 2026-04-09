"""Property-based tests for OR/AND condition tree layout in ladder CSV export.

Generates random condition trees with Or/And nesting and asserts
structural invariants on the exported CSV, plus a full round-trip through
the codegen parser to verify semantic correctness.

The bug that motivated this test: a multi-contact And branch inside
Or (e.g. ``Or(And(A, B, C), And(D, E))``) failed to get
the ``T:`` prefix on its first contact when preceded by a series contact,
producing invalid CSV that the Click editor would misinterpret.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.hypothesis

from pyrung.click import TagMap, c, ladder_to_pyrung, pyrung_to_ladder, x, y
from pyrung.core import And, Bool, Int, Or, Program, Rung
from pyrung.core.program import out
from pyrung.core.tag import TagType

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


@st.composite
def _condition_tree(draw, *, n_ors_range=(1, 2), n_prefix_range=(0, 2), n_branches_range=(2, 4)):
    """Generate a random Bool-only condition tree with Or/And nesting.

    Each Or has 2-4 branches; each branch is a single contact or
    And(1-3 contacts).  Prefix is 0-2 series contacts, suffix 0-1.

    Returns ``(conditions, tags)``.
    """
    tags: list[Bool] = []
    counter = [0]

    def make_tag() -> Bool:
        counter[0] += 1
        tag = Bool(f"T{counter[0]}")
        tags.append(tag)
        return tag

    def gen_branch():
        n = draw(st.integers(min_value=1, max_value=3))
        leaves = [make_tag() for _ in range(n)]
        return leaves[0] if n == 1 else And(*leaves)

    def gen_any():
        n_branches = draw(st.integers(min_value=n_branches_range[0], max_value=n_branches_range[1]))
        return Or(*(gen_branch() for _ in range(n_branches)))

    n_prefix = draw(st.integers(min_value=n_prefix_range[0], max_value=n_prefix_range[1]))
    n_ors = draw(st.integers(min_value=n_ors_range[0], max_value=n_ors_range[1]))
    n_suffix = draw(st.integers(min_value=0, max_value=1))

    conditions = (
        [make_tag() for _ in range(n_prefix)]
        + [gen_any() for _ in range(n_ors)]
        + [make_tag() for _ in range(n_suffix)]
    )
    return conditions, tags


def single_or_tree():
    """Condition tree with exactly one Or block including col-0 ORs."""
    return _condition_tree(n_ors_range=(1, 1))


def multi_or_tree():
    """Condition tree with 1-3 Or blocks (tests wider structural space)."""
    return _condition_tree(n_ors_range=(1, 3))


@st.composite
def single_or_tree_with_compare(draw):
    """Single Or block mixing Bool and Int-compare conditions.

    Exercises the ``T:DS1>100`` token form — the same code path as the
    original ``T:TXT1=="s"`` bug.
    """
    tags: list[Bool | Int] = []
    counter = [0]

    def make_bool() -> Bool:
        counter[0] += 1
        tag = Bool(f"B{counter[0]}")
        tags.append(tag)
        return tag

    def make_compare():
        counter[0] += 1
        tag = Int(f"N{counter[0]}")
        tags.append(tag)
        op = draw(st.sampled_from(["eq", "gt", "lt"]))
        val = draw(st.integers(min_value=0, max_value=999))
        if op == "eq":
            return tag == val
        if op == "gt":
            return tag > val
        return tag < val

    def gen_leaf():
        use_compare = draw(st.booleans())
        return make_compare() if use_compare else make_bool()

    def gen_branch():
        n = draw(st.integers(min_value=1, max_value=3))
        leaves = [gen_leaf() for _ in range(n)]
        return leaves[0] if n == 1 else And(*leaves)

    # Require ≥1 prefix so T: is exercised (start_cursor > 0).
    n_prefix = draw(st.integers(min_value=1, max_value=2))
    n_suffix = draw(st.integers(min_value=0, max_value=1))
    n_branches = draw(st.integers(min_value=2, max_value=3))

    conditions = (
        [gen_leaf() for _ in range(n_prefix)]
        + [Or(*(gen_branch() for _ in range(n_branches)))]
        + [gen_leaf() for _ in range(n_suffix)]
    )
    return conditions, tags


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONDITION_COLS = 31  # Columns A through AE


def _make_tag_map(tags):
    """Map a list of Bool/Int tags to Click addresses."""
    from pyrung.click import ds

    mapping: dict = {}
    x_idx, c_idx, ds_idx = 1, 1, 1
    for tag in tags:
        if tag.type == TagType.BOOL:
            if x_idx <= 16:
                mapping[tag] = x[x_idx]
                x_idx += 1
            else:
                mapping[tag] = c[c_idx]
                c_idx += 1
        else:
            mapping[tag] = ds[ds_idx]
            ds_idx += 1
    return mapping


def _strip_tee(cell: str) -> str:
    """Strip the ``T:`` prefix from a cell value."""
    return cell[2:] if cell.startswith("T:") else cell


def _export(conditions, tags):
    """Build program, map tags, export to ladder CSV."""
    Y_OUT = Bool("Y_OUT")

    with Program() as logic:
        with Rung(*conditions):
            out(Y_OUT)

    tag_map_dict = _make_tag_map(tags)
    tag_map_dict[Y_OUT] = y[1]
    mapping = TagMap(tag_map_dict, include_system=False)

    return pyrung_to_ladder(logic, mapping), mapping


# ---------------------------------------------------------------------------
# Invariant checks
# ---------------------------------------------------------------------------


def _check_row_width(bundle):
    """Every row must be exactly 33 cells (marker + 31 conditions + AF)."""
    for i, row in enumerate(bundle.main_rows):
        assert len(row) == 33, f"Row {i} has {len(row)} cells, expected 33"


def _check_first_row_wired(data_rows):
    """The first data row (active path) must have no empty condition cells.

    The exporter fills dashes from the last contact to AE, so every
    condition column should contain a contact, marker, or wire.
    """
    first = data_rows[0]
    assert first[0] == "R", f"First data row marker is {first[0]!r}, expected 'R'"
    for col in range(1, _CONDITION_COLS + 1):
        assert first[col] != "", (
            f"First data row has empty cell at column {col} (should be contact, T, |, or -)"
        )


def _check_all_tags_present(data_rows, tag_addresses):
    """Every leaf tag's Click address must appear somewhere in the output.

    Addresses may be wrapped in ``T:`` or ``~`` prefixes, or embedded in
    compare tokens like ``DS1>100``.
    """
    cell_text = set()
    for row in data_rows:
        for cell in row[1:-1]:
            cell_text.add(cell)
            cell_text.add(_strip_tee(cell))
            if cell.startswith("~"):
                cell_text.add(cell[1:])

    for addr in tag_addresses:
        found = any(addr in text for text in cell_text if text)
        assert found, f"Tag address {addr} not found in any CSV cell"


def _check_last_row_no_tee_prefix(data_rows):
    """The last row should never carry a ``T:`` prefix.

    After OR expansion (and optional triplet compaction), the bottom
    continuation row has no branch below it — a ``T:`` there would
    imply a phantom parallel path.
    """
    last = data_rows[-1]
    for col in range(1, _CONDITION_COLS + 1):
        cell = last[col]
        assert not cell.startswith("T:"), f"Last data row has T: prefix at column {col}: {cell!r}"


def _check_round_trip(bundle):
    """Parse CSV back to Python, re-export, and compare row-for-row."""
    code = ladder_to_pyrung(bundle)
    ns: dict = {}
    exec(code, ns)  # noqa: S102

    bundle2 = pyrung_to_ladder(ns["logic"], ns["mapping"])
    assert list(bundle.main_rows) == list(bundle2.main_rows), (
        "Round-trip mismatch: exported CSV differs after parse → re-export.\n"
        f"Original rows:\n{_format_rows(bundle.main_rows)}\n"
        f"Reproduced rows:\n{_format_rows(bundle2.main_rows)}"
    )


def _format_rows(rows: tuple[tuple[str, ...], ...]) -> str:
    """Format CSV rows for assertion messages (non-empty cells only)."""
    lines = []
    for row in rows[1:]:  # skip header
        cells = [c for c in row if c]
        lines.append("  " + ", ".join(cells))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


@given(tree=multi_or_tree())
@settings(max_examples=500, deadline=None)
def test_or_topology_structural_invariants(tree):
    """Structural invariants hold for any generated OR/AND condition tree.

    Covers single and series Or blocks with 1-3 OR groups,
    0-2 prefix contacts, and 2-4 branches per OR.
    """
    conditions, tags = tree
    bundle, mapping = _export(conditions, tags)
    data_rows = bundle.main_rows[1:-1]  # skip header + end()

    _check_row_width(bundle)
    _check_first_row_wired(data_rows)
    _check_all_tags_present(data_rows, [mapping.resolve(t) for t in tags])
    _check_last_row_no_tee_prefix(data_rows)


@given(tree=single_or_tree())
@settings(max_examples=500, deadline=None)
def test_or_topology_round_trip(tree):
    """Exported CSV survives parse → re-export for single-OR trees.

    Covers ORs at column 0 (no prefix) and mid-rung (with prefix).
    """
    conditions, tags = tree
    bundle, _mapping = _export(conditions, tags)

    _check_round_trip(bundle)


@given(tree=multi_or_tree())
@settings(max_examples=500, deadline=None)
def test_or_topology_round_trip_multi(tree):
    """Exported CSV survives parse → re-export for series OR trees.

    Exercises 1-3 sequential Or blocks with asymmetric branch
    lengths — the case that triggered the frozen-row merge wire bug.
    """
    conditions, tags = tree
    bundle, _mapping = _export(conditions, tags)

    _check_round_trip(bundle)


@given(tree=single_or_tree_with_compare())
@settings(max_examples=300, deadline=None)
def test_or_topology_round_trip_with_compares(tree):
    """Round-trip holds when branches contain Int compare conditions.

    Exercises the ``T:DS1>100`` token form — the same code path as the
    original ``T:TXT1=="s"`` bug.
    """
    conditions, tags = tree
    bundle, _mapping = _export(conditions, tags)
    data_rows = bundle.main_rows[1:-1]

    _check_row_width(bundle)
    _check_first_row_wired(data_rows)
    _check_last_row_no_tee_prefix(data_rows)
    _check_round_trip(bundle)
