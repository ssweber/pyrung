"""Tee-drawn twins: the CLICK export draws the pins as tees, pyrung runs the rail rungs.

A case with ``tee=`` runs the same pyrung rungs on every target; only the CLICK
export differs.  These tests pin that the drawing is what it says (pins tee off
the box row, nothing else moved) and that the importer reads it back as the rail
drawing, so what the twin runs in-process is what the drawing imports to.
"""

from __future__ import annotations

import pytest

from pyrung.click.ladder import pyrung_to_ladder
from pyrung.conformance._build import _draw_tees
from pyrung.conformance._tee import TeeSpec, draw_tees
from tests.click.helpers import bundle_logic
from tests.conformance import suite

TWINS = [
    ("counters", "pins_read_before_count_tee"),
    ("drum", "pins_read_before_step_fix_tee"),
]


@pytest.fixture(scope="module")
def part_a():
    build = suite.build("A")
    rail = pyrung_to_ladder(build.program, build.tag_map, validate=False)
    return build, rail, _draw_tees(rail, build.groups)


def _case_rows(bundle, subroutine: str, group: str, case_id: str):
    rows = dict(bundle.subroutine_rows)[subroutine]
    start = next(i for i, r in enumerate(rows) if r[:2] == ("#", f"Case {group}/{case_id}"))
    end = next(
        (i for i in range(start + 1, len(rows)) if rows[i][0] == "#" and rows[i - 1][0] != "#"),
        len(rows),
    )
    return rows[start:end]


@pytest.mark.parametrize(("group", "case_id"), TWINS)
def test_twin_is_drawn_with_tees_and_nothing_else_moves(part_a, group, case_id):
    build, rail, drawn = part_a
    sub = build.group(group).subroutine
    before = _case_rows(rail, sub, group, case_id)
    after = _case_rows(drawn, sub, group, case_id)
    changed = [(b, a) for b, a in zip(before, after, strict=True) if b != a]
    assert changed, "the twin is drawn as the rail drawing"
    # The box row gains a tee; teed pin rows lose their shared contacts.
    assert any(any(c == "T" or c.startswith("T:") for c in a) for _b, a in changed)
    assert any(a[-1].startswith(".") for _b, a in changed)
    # Nothing outside the twin's own rows moved in its subroutine.
    for name, rows in rail.subroutine_rows:
        now = dict(drawn.subroutine_rows)[name]
        moved = [i for i, (x, y) in enumerate(zip(rows, now, strict=True)) if x != y]
        if name == sub:
            span = _case_rows(rail, sub, group, case_id)
            first = next(i for i, r in enumerate(rows) if r == span[0])
            assert all(first <= i < first + len(span) for i in moved)
    assert rail.main_rows == drawn.main_rows


def test_the_importer_reads_the_drawing_as_the_rail_drawing(part_a):
    _build, rail, drawn = part_a
    assert bundle_logic(drawn) == bundle_logic(rail)


def test_twin_sentences_and_expectations_match_their_siblings():
    from tests.conformance import test_counters, test_drum

    for module, sibling, twin in (
        (test_counters, "pins_read_before_count", "pins_read_before_count_tee"),
        (test_drum, "pins_read_before_step_fix", "pins_read_before_step_fix_tee"),
    ):
        cases = {c.id: c for c in module.group.cases}
        assert cases[twin].expect == cases[sibling].expect
        assert cases[twin].probe == cases[sibling].probe
        assert cases[twin].tee is not None and cases[sibling].tee is None


def test_draw_tees_refuses_a_rung_that_is_not_the_expected_rail_drawing(part_a):
    build, rail, _drawn = part_a
    rows = dict(rail.subroutine_rows)[build.group("counters").subroutine]
    marker = "counters/pins_read_before_count_tee"
    with pytest.raises(ValueError, match="shared contacts"):
        draw_tees(rows, marker, TeeSpec("count_up(", (".down()",), after=3))
    with pytest.raises(ValueError, match="not under the box row"):
        draw_tees(rows, marker, TeeSpec("count_up(", (".reset()",), after=2))


def _rows(*cells_per_row):
    return [("#", "Case g/x"), *cells_per_row]


def _row(marker, cells, af):
    row = [marker, *cells, *[""] * (31 - len(cells)), af]
    return tuple(row)


def test_draw_tees_refuses_cells_with_nothing_to_hang_a_tee_on():
    spec = TeeSpec("box(", (".p()",), after=1)
    pin = _row("", ["A", "-"], ".p()")
    for bad in ("", "T", "T:B", "|"):
        rows = _rows(_row("R", ["A", bad], "box()"), pin)
        with pytest.raises(ValueError, match="nothing to hang a tee on"):
            draw_tees(rows, "g/x", spec)
    ok = draw_tees(_rows(_row("R", ["A", "-"], "box()"), pin), "g/x", spec)
    assert ok[1][2] == "T"


def test_a_tee_case_without_its_subroutine_is_refused(part_a):
    from dataclasses import replace

    build, rail, _drawn = part_a
    gone = replace(
        rail,
        subroutine_rows=tuple(
            (n, r) for n, r in rail.subroutine_rows if n != build.group("drum").subroutine
        ),
    )
    with pytest.raises(ValueError, match="no subroutine"):
        _draw_tees(gone, build.groups)
