"""Property-based tests for branch() topology in ladder CSV export.

Generates random rungs with branch() blocks — parallel output paths with
their own conditions — and asserts structural invariants on the exported
CSV, plus a full round-trip through the codegen parser.

Exercises:
- Parent conditions (series, with/without any_of)
- Parent instructions interleaved before/after branches
- 1-3 branches per rung, each with 1-2 series conditions
- The combination of OR parent conditions with branch splits
- Nested branch() blocks (expected to raise LadderExportError)
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.hypothesis

from pyrung.click import (
    LadderExportError,
    TagMap,
    c,
    ladder_to_pyrung,
    pyrung_to_ladder,
    x,
    y,
)
from pyrung.core import Bool, Program, Rung, all_of, any_of
from pyrung.core.program import branch, out

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


@st.composite
def _branch_rung(draw, *, use_or_parent=False):
    """Generate a rung with branch() blocks.

    Returns ``(parent_conditions, before_outputs, branch_specs,
    after_outputs, cond_tags, out_tags)``.

    Each ``branch_spec`` is ``(branch_conditions, branch_output)``.
    """
    cond_tags: list[Bool] = []
    out_tags: list[Bool] = []
    counter = [0]

    def make_cond() -> Bool:
        counter[0] += 1
        tag = Bool(f"C{counter[0]}")
        cond_tags.append(tag)
        return tag

    def make_out() -> Bool:
        counter[0] += 1
        tag = Bool(f"O{counter[0]}")
        out_tags.append(tag)
        return tag

    # Parent conditions: 1-2 series + optional any_of + 0-1 suffix
    n_prefix = draw(st.integers(min_value=1, max_value=2))
    parent_conditions: list = [make_cond() for _ in range(n_prefix)]

    if use_or_parent:
        n_or_branches = draw(st.integers(min_value=2, max_value=3))
        or_leaves = []
        for _ in range(n_or_branches):
            n = draw(st.integers(min_value=1, max_value=2))
            leaves = [make_cond() for _ in range(n)]
            or_leaves.append(leaves[0] if n == 1 else all_of(*leaves))
        parent_conditions.append(any_of(*or_leaves))

    n_suffix = draw(st.integers(min_value=0, max_value=1))
    parent_conditions.extend(make_cond() for _ in range(n_suffix))

    # Parent outputs before branches (0-2)
    n_before = draw(st.integers(min_value=0, max_value=2))
    before_outputs = [make_out() for _ in range(n_before)]

    # Branches (1-3), each with 1-2 series conditions and 1 output
    n_branches = draw(st.integers(min_value=1, max_value=3))
    branch_specs = []
    for _ in range(n_branches):
        n_conds = draw(st.integers(min_value=1, max_value=2))
        b_conds = [make_cond() for _ in range(n_conds)]
        b_out = make_out()
        branch_specs.append((b_conds, b_out))

    # Parent outputs after branches (0-1)
    n_after = draw(st.integers(min_value=0, max_value=1))
    after_outputs = [make_out() for _ in range(n_after)]

    return parent_conditions, before_outputs, branch_specs, after_outputs, cond_tags, out_tags


def simple_branch_rung():
    """Rung with branches, series-only parent conditions."""
    return _branch_rung(use_or_parent=False)


def or_branch_rung():
    """Rung with branches AND any_of in parent conditions."""
    return _branch_rung(use_or_parent=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONDITION_COLS = 31


def _make_tag_map(cond_tags, out_tags):
    """Map condition tags to X/C addresses, output tags to Y addresses."""
    mapping: dict = {}
    x_idx, c_idx = 1, 1
    for tag in cond_tags:
        if x_idx <= 16:
            mapping[tag] = x[x_idx]
            x_idx += 1
        else:
            mapping[tag] = c[c_idx]
            c_idx += 1
    y_idx = 1
    for tag in out_tags:
        mapping[tag] = y[y_idx]
        y_idx += 1
    return mapping


def _export_branch(parent_conditions, before_outputs, branch_specs, after_outputs, cond_tags, out_tags):
    """Build a program with branches, map tags, export to ladder CSV."""
    with Program(strict=False) as logic:
        with Rung(*parent_conditions):
            for o in before_outputs:
                out(o)
            for b_conds, b_out in branch_specs:
                with branch(*b_conds):
                    out(b_out)
            for o in after_outputs:
                out(o)

    mapping = TagMap(_make_tag_map(cond_tags, out_tags), include_system=False)
    return pyrung_to_ladder(logic, mapping), mapping


def _strip_tee(cell: str) -> str:
    return cell[2:] if cell.startswith("T:") else cell


def _format_rows(rows: tuple[tuple[str, ...], ...]) -> str:
    lines = []
    for row in rows[1:]:
        cells = [c for c in row if c]
        lines.append("  " + ", ".join(cells))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Invariant checks
# ---------------------------------------------------------------------------


def _check_row_width(bundle):
    """Every row must be exactly 33 cells (marker + 31 conditions + AF)."""
    for i, row in enumerate(bundle.main_rows):
        assert len(row) == 33, f"Row {i} has {len(row)} cells, expected 33"


def _check_first_row_wired(data_rows):
    """The first data row must have no empty condition cells."""
    first = data_rows[0]
    assert first[0] == "R", f"First data row marker is {first[0]!r}, expected 'R'"
    for col in range(1, _CONDITION_COLS + 1):
        assert first[col] != "", f"First data row has empty cell at column {col}"


def _check_all_tags_present(data_rows, tag_addresses):
    """Every tag's Click address must appear somewhere in the output."""
    cell_text = set()
    for row in data_rows:
        for cell in row:
            cell_text.add(cell)
            cell_text.add(_strip_tee(cell))
            if cell.startswith("~"):
                cell_text.add(cell[1:])

    for addr in tag_addresses:
        found = any(addr in text for text in cell_text if text)
        assert found, f"Tag address {addr} not found in any CSV cell"


def _check_all_outputs_in_af(data_rows, output_addresses):
    """Every output tag's address must appear in an AF-column entry."""
    af_text = {row[-1] for row in data_rows if row[-1]}
    for addr in output_addresses:
        found = any(addr in af for af in af_text)
        assert found, f"Output {addr} not found in any AF column"


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


# ---------------------------------------------------------------------------
# Property tests — simple branches (series parent conditions)
# ---------------------------------------------------------------------------


@given(tree=simple_branch_rung())
@settings(max_examples=500, deadline=None)
def test_branch_structural_invariants(tree):
    """Structural invariants hold for rungs with branch() blocks.

    Covers 1-3 branches with 1-2 series conditions each,
    0-2 parent instructions before branches, 0-1 after.
    """
    parent_conditions, before_outputs, branch_specs, after_outputs, cond_tags, out_tags = tree
    bundle, mapping = _export_branch(
        parent_conditions, before_outputs, branch_specs, after_outputs, cond_tags, out_tags,
    )
    data_rows = bundle.main_rows[1:-1]

    _check_row_width(bundle)
    _check_first_row_wired(data_rows)
    _check_all_tags_present(data_rows, [mapping.resolve(t) for t in cond_tags + out_tags])
    _check_all_outputs_in_af(data_rows, [mapping.resolve(t) for t in out_tags])


@given(tree=simple_branch_rung())
@settings(max_examples=500, deadline=None)
def test_branch_round_trip(tree):
    """Exported CSV survives parse → re-export for branched rungs."""
    parent_conditions, before_outputs, branch_specs, after_outputs, cond_tags, out_tags = tree
    bundle, _ = _export_branch(
        parent_conditions, before_outputs, branch_specs, after_outputs, cond_tags, out_tags,
    )
    _check_round_trip(bundle)


# ---------------------------------------------------------------------------
# Property tests — branches with OR parent conditions
# ---------------------------------------------------------------------------


@given(tree=or_branch_rung())
@settings(max_examples=500, deadline=None)
def test_or_branch_structural_invariants(tree):
    """Structural invariants hold when branches combine with OR parent conditions.

    Exercises the interaction between any_of condition expansion
    (multi-row parent) and the branch slot layout.
    """
    parent_conditions, before_outputs, branch_specs, after_outputs, cond_tags, out_tags = tree
    bundle, mapping = _export_branch(
        parent_conditions, before_outputs, branch_specs, after_outputs, cond_tags, out_tags,
    )
    data_rows = bundle.main_rows[1:-1]

    _check_row_width(bundle)
    _check_first_row_wired(data_rows)
    _check_all_tags_present(data_rows, [mapping.resolve(t) for t in cond_tags + out_tags])
    _check_all_outputs_in_af(data_rows, [mapping.resolve(t) for t in out_tags])


@given(tree=or_branch_rung())
@settings(max_examples=500, deadline=None)
def test_or_branch_round_trip(tree):
    """Exported CSV survives parse → re-export for OR + branch combos."""
    parent_conditions, before_outputs, branch_specs, after_outputs, cond_tags, out_tags = tree
    bundle, _ = _export_branch(
        parent_conditions, before_outputs, branch_specs, after_outputs, cond_tags, out_tags,
    )
    _check_round_trip(bundle)


# ---------------------------------------------------------------------------
# Nested branches — must raise LadderExportError
# ---------------------------------------------------------------------------


@st.composite
def _nested_branch_rung(draw):
    """Generate a rung with a nested branch (branch inside branch).

    Returns ``(parent_cond, outer_cond, inner_cond, out1, out2,
    cond_tags, out_tags)`` — minimal structure to trigger the error.
    """
    cond_tags: list[Bool] = []
    out_tags: list[Bool] = []
    counter = [0]

    def make_cond() -> Bool:
        counter[0] += 1
        tag = Bool(f"C{counter[0]}")
        cond_tags.append(tag)
        return tag

    def make_out() -> Bool:
        counter[0] += 1
        tag = Bool(f"O{counter[0]}")
        out_tags.append(tag)
        return tag

    parent_cond = make_cond()
    outer_cond = make_cond()
    inner_cond = make_cond()

    n_outer_extra = draw(st.integers(min_value=0, max_value=1))
    outer_extra = [make_cond() for _ in range(n_outer_extra)]

    out1 = make_out()
    out2 = make_out()

    return parent_cond, outer_cond, outer_extra, inner_cond, out1, out2, cond_tags, out_tags


@given(tree=_nested_branch_rung())
@settings(max_examples=100, deadline=None)
def test_nested_branch_raises_export_error(tree):
    """Nested branch(...) blocks must raise LadderExportError in Click v1.

    This ensures the exporter rejects structures it cannot represent
    rather than silently producing invalid CSV.
    """
    parent_cond, outer_cond, outer_extra, inner_cond, out1, out2, cond_tags, out_tags = tree

    with Program() as logic:
        with Rung(parent_cond):
            out(out1)
            with branch(outer_cond, *outer_extra):
                out(out2)
                with branch(inner_cond):
                    out(out2)

    mapping = TagMap(_make_tag_map(cond_tags, out_tags), include_system=False)

    with pytest.raises(LadderExportError, match="Nested branch"):
        pyrung_to_ladder(logic, mapping)
