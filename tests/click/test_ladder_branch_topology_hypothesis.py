"""Property-based tests for branch() topology in ladder CSV export.

Generates rungs with branch() blocks and checks structural invariants on the
exported CSV, plus parse -> re-export stability through ladder_to_pyrung().

Exercises:
- Parent conditions, with and without any_of(...)
- Parent instructions before and after branches
- Branch-local any_of(...)
- Nested branch() blocks
- Nested branch() blocks with inner OR
- continued() rungs whose branch rows must stay visually pushed down
"""

from __future__ import annotations

import csv
from io import StringIO

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.hypothesis

from pyrung.click import TagMap, c, ladder_to_pyrung, pyrung_to_ladder, x, y
from pyrung.core import Bool, Program, Rung, all_of, any_of
from pyrung.core.program import branch, out


# ---------------------------------------------------------------------------
# Strategy helpers
# ---------------------------------------------------------------------------


def _new_tag_builders():
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

    return make_cond, make_out, cond_tags, out_tags


def _draw_or_term(draw, make_cond):
    n = draw(st.integers(min_value=1, max_value=2))
    leaves = [make_cond() for _ in range(n)]
    return leaves[0] if n == 1 else all_of(*leaves)


def _draw_parent_conditions(draw, make_cond, *, use_or_parent: bool) -> list:
    n_prefix = draw(st.integers(min_value=1, max_value=2))
    parent_conditions: list = [make_cond() for _ in range(n_prefix)]

    if use_or_parent:
        n_or_branches = draw(st.integers(min_value=2, max_value=3))
        parent_conditions.append(any_of(*[_draw_or_term(draw, make_cond) for _ in range(n_or_branches)]))

    n_suffix = draw(st.integers(min_value=0, max_value=1))
    parent_conditions.extend(make_cond() for _ in range(n_suffix))
    return parent_conditions


# ---------------------------------------------------------------------------
# Branch strategies
# ---------------------------------------------------------------------------


@st.composite
def _branch_rung(draw, *, use_or_parent: bool = False, use_local_or: bool = False):
    """Generate a rung with branches.

    Returns:
        (parent_conditions, before_outputs, branch_specs, after_outputs, cond_tags, out_tags)

    Each branch spec is (branch_conditions, branch_output), where branch_conditions
    may contain Bool terms and any_of(...) terms.
    """
    make_cond, make_out, cond_tags, out_tags = _new_tag_builders()
    parent_conditions = _draw_parent_conditions(draw, make_cond, use_or_parent=use_or_parent)

    n_before = draw(st.integers(min_value=0, max_value=2))
    before_outputs = [make_out() for _ in range(n_before)]

    branch_specs: list[tuple[list, Bool]] = []
    n_branches = draw(st.integers(min_value=1, max_value=3))
    local_or_index = draw(st.integers(min_value=0, max_value=n_branches - 1)) if use_local_or else -1

    for branch_index in range(n_branches):
        branch_conditions: list = []

        if use_local_or and branch_index == local_or_index:
            if draw(st.booleans()):
                branch_conditions.append(make_cond())

            n_local_or = draw(st.integers(min_value=2, max_value=3))
            branch_conditions.append(any_of(*[_draw_or_term(draw, make_cond) for _ in range(n_local_or)]))

            if draw(st.booleans()):
                branch_conditions.append(make_cond())
        else:
            n_conds = draw(st.integers(min_value=1, max_value=2))
            branch_conditions.extend(make_cond() for _ in range(n_conds))

        branch_specs.append((branch_conditions, make_out()))

    n_after = draw(st.integers(min_value=0, max_value=1))
    after_outputs = [make_out() for _ in range(n_after)]

    return parent_conditions, before_outputs, branch_specs, after_outputs, cond_tags, out_tags


def simple_branch_rung():
    return _branch_rung(use_or_parent=False, use_local_or=False)


def or_branch_rung():
    return _branch_rung(use_or_parent=True, use_local_or=False)


def branch_local_or_rung():
    return _branch_rung(use_or_parent=False, use_local_or=True)


def parent_or_and_branch_local_or_rung():
    return _branch_rung(use_or_parent=True, use_local_or=True)


# ---------------------------------------------------------------------------
# Nested branch strategies
# ---------------------------------------------------------------------------


@st.composite
def _nested_branch_rung(draw, *, use_inner_or: bool = False):
    """Generate a nested branch tree, optionally with inner-branch OR."""
    make_cond, make_out, cond_tags, out_tags = _new_tag_builders()

    parent_conditions = [make_cond()]
    outer_conditions: list = [make_cond()]
    if draw(st.booleans()):
        outer_conditions.append(make_cond())

    outer_output = make_out()

    if use_inner_or:
        inner_conditions: list = []
        if draw(st.booleans()):
            inner_conditions.append(make_cond())
        n_inner_or = draw(st.integers(min_value=2, max_value=3))
        inner_conditions.append(any_of(*[_draw_or_term(draw, make_cond) for _ in range(n_inner_or)]))
        if draw(st.booleans()):
            inner_conditions.append(make_cond())
    else:
        inner_conditions = [make_cond()]

    inner_output = make_out()
    return parent_conditions, outer_conditions, inner_conditions, outer_output, inner_output, cond_tags, out_tags


def nested_branch_rung():
    return _nested_branch_rung(use_inner_or=False)


def nested_branch_local_or_rung():
    return _nested_branch_rung(use_inner_or=True)


# ---------------------------------------------------------------------------
# continued() strategy
# ---------------------------------------------------------------------------


@st.composite
def _continued_branch_rung(draw):
    """Generate a 2-rung program where the second rung is continued()."""
    make_cond, make_out, cond_tags, out_tags = _new_tag_builders()

    first_conditions = [make_cond()]
    first_output = make_out()

    continued_conditions = [make_cond()]
    continued_output = make_out()

    branch_conditions: list = []
    if draw(st.booleans()):
        branch_conditions.append(make_cond())
    n_local_or = draw(st.integers(min_value=2, max_value=3))
    branch_conditions.append(any_of(*[_draw_or_term(draw, make_cond) for _ in range(n_local_or)]))
    if draw(st.booleans()):
        branch_conditions.append(make_cond())

    branch_output = make_out()

    return (
        first_conditions,
        first_output,
        continued_conditions,
        continued_output,
        branch_conditions,
        branch_output,
        cond_tags,
        out_tags,
    )


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------


_CONDITION_COLS = 31


def _make_tag_map(cond_tags, out_tags):
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
    with Program(strict=False) as logic:
        with Rung(*parent_conditions):
            for output in before_outputs:
                out(output)
            for branch_conditions, branch_output in branch_specs:
                with branch(*branch_conditions):
                    out(branch_output)
            for output in after_outputs:
                out(output)

    mapping = TagMap(_make_tag_map(cond_tags, out_tags), include_system=False)
    return pyrung_to_ladder(logic, mapping), mapping


def _export_nested(parent_conditions, outer_conditions, inner_conditions, outer_output, inner_output, cond_tags, out_tags):
    with Program(strict=False) as logic:
        with Rung(*parent_conditions):
            with branch(*outer_conditions):
                out(outer_output)
                with branch(*inner_conditions):
                    out(inner_output)

    mapping = TagMap(_make_tag_map(cond_tags, out_tags), include_system=False)
    return pyrung_to_ladder(logic, mapping), mapping


def _export_continued(
    first_conditions,
    first_output,
    continued_conditions,
    continued_output,
    branch_conditions,
    branch_output,
    cond_tags,
    out_tags,
):
    with Program(strict=False) as logic:
        with Rung(*first_conditions):
            out(first_output)
        with Rung(*continued_conditions).continued():
            out(continued_output)
            with branch(*branch_conditions):
                out(branch_output)

    mapping = TagMap(_make_tag_map(cond_tags, out_tags), include_system=False)
    return pyrung_to_ladder(logic, mapping), mapping


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


def _strip_tee(cell: str) -> str:
    return cell[2:] if cell.startswith("T:") else cell


def _format_rows(rows: tuple[tuple[str, ...], ...]) -> str:
    lines = []
    for row in rows[1:]:
        cells = [cell for cell in row if cell]
        lines.append("  " + ", ".join(cells))
    return "\n".join(lines)


def _format_csv_rows(rows: tuple[tuple[str, ...], ...]) -> str:
    return "\n".join(f"{index:02d}: {','.join(row)}" for index, row in enumerate(rows))


def _serialize_csv_text(rows) -> str:
    buffer = StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerows(rows)
    return buffer.getvalue()


def _describe_first_difference(rows1, rows2) -> str:
    if len(rows1) != len(rows2):
        return f"row count differs: {len(rows1)} != {len(rows2)}"

    for row_index, (row1, row2) in enumerate(zip(rows1, rows2)):
        if row1 == row2:
            continue
        if len(row1) != len(row2):
            return f"row {row_index} width differs: {len(row1)} != {len(row2)}"
        for col_index, (cell1, cell2) in enumerate(zip(row1, row2)):
            if cell1 != cell2:
                return (
                    f"first differing cell at row {row_index}, col {col_index}: "
                    f"{cell1!r} != {cell2!r}"
                )
        return f"row {row_index} differs"

    return "no differing cell found"


def _check_row_width(bundle):
    for i, row in enumerate(bundle.main_rows):
        assert len(row) == 33, f"Row {i} has {len(row)} cells, expected 33"


def _check_first_row_wired(data_rows):
    first = data_rows[0]
    assert first[0] == "R", f"First data row marker is {first[0]!r}, expected 'R'"
    for col in range(1, _CONDITION_COLS + 1):
        assert first[col] != "", f"First data row has empty cell at column {col}"


def _check_all_tags_present(data_rows, tag_addresses):
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
    af_text = {row[-1] for row in data_rows if row[-1]}
    for addr in output_addresses:
        found = any(addr in af for af in af_text)
        assert found, f"Output {addr} not found in any AF column"


def _check_round_trip(bundle):
    code = ladder_to_pyrung(bundle)
    ns: dict = {}
    exec(code, ns)  # noqa: S102

    bundle2 = pyrung_to_ladder(ns["logic"], ns["mapping"])
    assert list(bundle.main_rows) == list(bundle2.main_rows), (
        "Round-trip mismatch: exported CSV differs after parse -> re-export.\n"
        f"Original rows:\n{_format_rows(bundle.main_rows)}\n"
        f"Reproduced rows:\n{_format_rows(bundle2.main_rows)}"
    )


def _check_round_trip_ignoring_tee_markers(bundle):
    """Importer may normalize tee markers while preserving row topology."""
    code = ladder_to_pyrung(bundle)
    ns: dict = {}
    exec(code, ns)  # noqa: S102

    bundle2 = pyrung_to_ladder(ns["logic"], ns["mapping"])

    def normalize(rows):
        return [tuple(_strip_tee(cell) for cell in row) for row in rows]

    normalized_original = normalize(bundle.main_rows)
    normalized_reproduced = normalize(bundle2.main_rows)

    assert normalized_original == normalized_reproduced, (
        "Round-trip mismatch after tee-marker normalization.\n"
        f"{_describe_first_difference(normalized_original, normalized_reproduced)}\n\n"
        "Round-tripped pyrung:\n"
        f"{code}\n\n"
        "Original CSV:\n"
        f"{_format_csv_rows(bundle.main_rows)}\n\n"
        "Re-exported CSV:\n"
        f"{_format_csv_rows(bundle2.main_rows)}\n\n"
        "Original emitted CSV text:\n"
        f"{_serialize_csv_text(bundle.main_rows)}\n"
        "Re-exported emitted CSV text:\n"
        f"{_serialize_csv_text(bundle2.main_rows)}\n"
        "Normalized original CSV:\n"
        f"{_format_csv_rows(tuple(normalized_original))}\n\n"
        "Normalized re-exported CSV:\n"
        f"{_format_csv_rows(tuple(normalized_reproduced))}"
    )


# ---------------------------------------------------------------------------
# Property tests - flat branches
# ---------------------------------------------------------------------------


@given(tree=simple_branch_rung())
@settings(max_examples=500, deadline=None)
def test_branch_structural_invariants(tree):
    parent_conditions, before_outputs, branch_specs, after_outputs, cond_tags, out_tags = tree
    bundle, mapping = _export_branch(
        parent_conditions,
        before_outputs,
        branch_specs,
        after_outputs,
        cond_tags,
        out_tags,
    )
    data_rows = bundle.main_rows[1:-1]

    _check_row_width(bundle)
    _check_first_row_wired(data_rows)
    _check_all_tags_present(data_rows, [mapping.resolve(t) for t in cond_tags + out_tags])
    _check_all_outputs_in_af(data_rows, [mapping.resolve(t) for t in out_tags])


@given(tree=simple_branch_rung())
@settings(max_examples=500, deadline=None)
def test_branch_round_trip(tree):
    parent_conditions, before_outputs, branch_specs, after_outputs, cond_tags, out_tags = tree
    bundle, _ = _export_branch(
        parent_conditions,
        before_outputs,
        branch_specs,
        after_outputs,
        cond_tags,
        out_tags,
    )
    _check_round_trip(bundle)


@given(tree=or_branch_rung())
@settings(max_examples=500, deadline=None)
def test_or_branch_structural_invariants(tree):
    parent_conditions, before_outputs, branch_specs, after_outputs, cond_tags, out_tags = tree
    bundle, mapping = _export_branch(
        parent_conditions,
        before_outputs,
        branch_specs,
        after_outputs,
        cond_tags,
        out_tags,
    )
    data_rows = bundle.main_rows[1:-1]

    _check_row_width(bundle)
    _check_first_row_wired(data_rows)
    _check_all_tags_present(data_rows, [mapping.resolve(t) for t in cond_tags + out_tags])
    _check_all_outputs_in_af(data_rows, [mapping.resolve(t) for t in out_tags])


@given(tree=or_branch_rung())
@settings(max_examples=500, deadline=None)
def test_or_branch_round_trip(tree):
    parent_conditions, before_outputs, branch_specs, after_outputs, cond_tags, out_tags = tree
    bundle, _ = _export_branch(
        parent_conditions,
        before_outputs,
        branch_specs,
        after_outputs,
        cond_tags,
        out_tags,
    )
    _check_round_trip(bundle)


@given(tree=branch_local_or_rung())
@settings(max_examples=300, deadline=None)
def test_branch_local_or_structural_invariants(tree):
    parent_conditions, before_outputs, branch_specs, after_outputs, cond_tags, out_tags = tree
    bundle, mapping = _export_branch(
        parent_conditions,
        before_outputs,
        branch_specs,
        after_outputs,
        cond_tags,
        out_tags,
    )
    data_rows = bundle.main_rows[1:-1]

    _check_row_width(bundle)
    _check_first_row_wired(data_rows)
    _check_all_tags_present(data_rows, [mapping.resolve(t) for t in cond_tags + out_tags])
    _check_all_outputs_in_af(data_rows, [mapping.resolve(t) for t in out_tags])


@given(tree=branch_local_or_rung())
@settings(max_examples=300, deadline=None)
def test_branch_local_or_round_trip(tree):
    parent_conditions, before_outputs, branch_specs, after_outputs, cond_tags, out_tags = tree
    bundle, _ = _export_branch(
        parent_conditions,
        before_outputs,
        branch_specs,
        after_outputs,
        cond_tags,
        out_tags,
    )
    _check_round_trip_ignoring_tee_markers(bundle)


@given(tree=parent_or_and_branch_local_or_rung())
@settings(max_examples=200, deadline=None)
def test_parent_or_and_branch_local_or_round_trip(tree):
    parent_conditions, before_outputs, branch_specs, after_outputs, cond_tags, out_tags = tree
    bundle, _ = _export_branch(
        parent_conditions,
        before_outputs,
        branch_specs,
        after_outputs,
        cond_tags,
        out_tags,
    )
    _check_round_trip_ignoring_tee_markers(bundle)


# ---------------------------------------------------------------------------
# Property tests - nested branches
# ---------------------------------------------------------------------------


@given(tree=nested_branch_rung())
@settings(max_examples=100, deadline=None)
def test_nested_branch_structural_invariants(tree):
    parent_conditions, outer_conditions, inner_conditions, outer_output, inner_output, cond_tags, out_tags = tree
    bundle, mapping = _export_nested(
        parent_conditions,
        outer_conditions,
        inner_conditions,
        outer_output,
        inner_output,
        cond_tags,
        out_tags,
    )
    data_rows = bundle.main_rows[1:-1]

    _check_row_width(bundle)
    _check_first_row_wired(data_rows)
    _check_all_tags_present(data_rows, [mapping.resolve(t) for t in cond_tags + out_tags])
    _check_all_outputs_in_af(data_rows, [mapping.resolve(t) for t in out_tags])


@given(tree=nested_branch_rung())
@settings(max_examples=100, deadline=None)
def test_nested_branch_round_trip(tree):
    parent_conditions, outer_conditions, inner_conditions, outer_output, inner_output, cond_tags, out_tags = tree
    bundle, _ = _export_nested(
        parent_conditions,
        outer_conditions,
        inner_conditions,
        outer_output,
        inner_output,
        cond_tags,
        out_tags,
    )
    _check_round_trip(bundle)


@given(tree=nested_branch_local_or_rung())
@settings(max_examples=100, deadline=None)
def test_nested_branch_local_or_structural_invariants(tree):
    parent_conditions, outer_conditions, inner_conditions, outer_output, inner_output, cond_tags, out_tags = tree
    bundle, mapping = _export_nested(
        parent_conditions,
        outer_conditions,
        inner_conditions,
        outer_output,
        inner_output,
        cond_tags,
        out_tags,
    )
    data_rows = bundle.main_rows[1:-1]

    _check_row_width(bundle)
    _check_first_row_wired(data_rows)
    _check_all_tags_present(data_rows, [mapping.resolve(t) for t in cond_tags + out_tags])
    _check_all_outputs_in_af(data_rows, [mapping.resolve(t) for t in out_tags])


@given(tree=nested_branch_local_or_rung())
@settings(max_examples=100, deadline=None)
def test_nested_branch_local_or_round_trip(tree):
    parent_conditions, outer_conditions, inner_conditions, outer_output, inner_output, cond_tags, out_tags = tree
    bundle, _ = _export_nested(
        parent_conditions,
        outer_conditions,
        inner_conditions,
        outer_output,
        inner_output,
        cond_tags,
        out_tags,
    )
    _check_round_trip(bundle)


# ---------------------------------------------------------------------------
# Property tests - continued() + pushed-down branch rows
# ---------------------------------------------------------------------------


@given(tree=_continued_branch_rung())
@settings(max_examples=100, deadline=None)
def test_continued_branch_rows_stay_blank_marker_and_pushed_down(tree):
    (
        first_conditions,
        first_output,
        continued_conditions,
        continued_output,
        branch_conditions,
        branch_output,
        cond_tags,
        out_tags,
    ) = tree

    bundle, mapping = _export_continued(
        first_conditions,
        first_output,
        continued_conditions,
        continued_output,
        branch_conditions,
        branch_output,
        cond_tags,
        out_tags,
    )
    data_rows = bundle.main_rows[1:-1]
    continued_rows = data_rows[1:]

    _check_row_width(bundle)
    _check_first_row_wired(data_rows)
    _check_all_tags_present(data_rows, [mapping.resolve(t) for t in cond_tags + out_tags])
    _check_all_outputs_in_af(data_rows, [mapping.resolve(t) for t in out_tags])
    assert continued_rows, "Expected at least one continued() row"
    assert all(row[0] == "" for row in continued_rows), "continued() rows must keep blank markers"


@given(tree=_continued_branch_rung())
@settings(max_examples=100, deadline=None)
def test_continued_branch_round_trip(tree):
    (
        first_conditions,
        first_output,
        continued_conditions,
        continued_output,
        branch_conditions,
        branch_output,
        cond_tags,
        out_tags,
    ) = tree

    bundle, _ = _export_continued(
        first_conditions,
        first_output,
        continued_conditions,
        continued_output,
        branch_conditions,
        branch_output,
        cond_tags,
        out_tags,
    )
    _check_round_trip(bundle)
