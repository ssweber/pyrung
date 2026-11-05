"""Tee-drawn twins: the CLICK export draws the pins as tees, pyrung runs the rail rungs.

A case with ``tee=`` runs the same pyrung rungs on every target; only the CLICK
export differs.  These tests pin that the drawing is what it says (pins tee off
the box row, nothing else moved) and that the importer reads it back as the rail
drawing, so what the twin runs in-process is what the drawing imports to.
"""

from __future__ import annotations

import pytest

from pyrung.click.ladder import pyrung_to_ladder
from pyrung.conformance import build_program, load_manifest
from pyrung.conformance._build import PASTE_PROBE_FINDINGS, _draw_tees
from pyrung.conformance._tee import TeeSpec, draw_tees
from tests.click.helpers import bundle_logic
from tests.conformance import suite

TWINS = [
    ("counters", "pins_read_before_count_tee"),
    ("drum", "pins_read_before_step_fix_tee"),
]


@pytest.fixture(scope="module")
def twins_build():
    names = {group for group, _case_id in TWINS}
    groups = [g for g in suite.ALL_GROUPS if g.name in names]
    build = build_program(groups, load_manifest(suite.MANIFEST_PATH))
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
def test_twin_is_drawn_with_tees_and_nothing_else_moves(twins_build, group, case_id):
    build, rail, drawn = twins_build
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


def test_the_importer_reads_the_drawing_as_the_rail_drawing(twins_build):
    _build, rail, drawn = twins_build
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


def test_draw_tees_refuses_a_rung_that_is_not_the_expected_rail_drawing(twins_build):
    build, rail, _drawn = twins_build
    rows = dict(rail.subroutine_rows)[build.group("counters").subroutine]
    marker = "counters/pins_read_before_count_tee"
    with pytest.raises(ValueError, match="shared contacts"):
        draw_tees(rows, marker, TeeSpec("count_up(", (".down()",), after=2))
    with pytest.raises(ValueError, match="not under the box row"):
        draw_tees(rows, marker, TeeSpec("count_up(", (".reset()",), after=1))


def test_draw_tees_refuses_a_pin_left_on_the_rail_c000a(twins_build):
    build, rail, _drawn = twins_build
    rows = dict(rail.subroutine_rows)[build.group("counters").subroutine]
    marker = "counters/pins_read_before_count_tee"
    with pytest.raises(ValueError, match="C000A"):
        draw_tees(rows, marker, TeeSpec("count_up(", (".down()",), after=1))


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


def test_a_tee_case_without_its_subroutine_is_refused(twins_build):
    from dataclasses import replace

    build, rail, _drawn = twins_build
    gone = replace(
        rail,
        subroutine_rows=tuple(
            (n, r) for n, r in rail.subroutine_rows if n != build.group("drum").subroutine
        ),
    )
    with pytest.raises(ValueError, match="no subroutine"):
        _draw_tees(gone, build.groups)


def test_a_tee_in_column_a_is_refused():
    with pytest.raises(ValueError, match="column A"):
        TeeSpec("box(", (".p()",), after=0)


@pytest.mark.parametrize("part", sorted(suite.PARTS))
def test_no_exported_row_has_a_vertical_token_in_column_a(part):
    """CLICK refuses T, | and T: in column A (the rail); true of the drawn export too."""
    from pyrung.click.ladder.layout import assert_no_column_a_vertical

    build = suite.build(part)
    drawn = _draw_tees(
        pyrung_to_ladder(build.program, build.tag_map, allow=PASTE_PROBE_FINDINGS), build.groups
    )
    assert_no_column_a_vertical(drawn.main_rows[1:])
    for _name, rows in drawn.subroutine_rows:
        assert_no_column_a_vertical(rows[1:])


@pytest.fixture(scope="module")
def control_build():
    build = suite.build("control")
    rail = pyrung_to_ladder(build.program, build.tag_map, validate=False)
    return build, rail, _draw_tees(rail, build.groups)


def test_the_bare_twin_hangs_its_tee_off_a_wire_in_column_a(control_build):
    """S5 #3: ``R,-,T`` over ``,,-``: no trunk contact, a wire in column A, the tee in B."""
    build, rail, drawn = control_build
    sub = build.group("cont_box").subroutine
    before = _case_rows(rail, sub, "cont_box", "box_tee_without_trunk")
    after = _case_rows(drawn, sub, "cont_box", "box_tee_without_trunk")
    box = next(i for i, r in enumerate(after) if r[-1].startswith("count_up("))
    assert after[box][:3] == ("R", "-", "T:C481")
    assert after[box + 1][:3] == ("", "", "T:C482")
    assert after[box + 2][:3] == ("", "", "DS3641==0")
    # The rail drawing has the same contacts one cell to the left; nothing else moved.
    assert before[box][:2] == ("R", "C481")
    assert before[box + 1][:2] == ("", "C482")
    moved = [i for i, (b, a) in enumerate(zip(before, after, strict=True)) if b != a]
    assert moved == [box, box + 1, box + 2]
    assert rail.main_rows == drawn.main_rows


def test_the_importer_reads_the_bare_twin_as_the_rail_drawing(control_build):
    _build, rail, drawn = control_build
    assert bundle_logic(drawn) == bundle_logic(rail)


def test_a_bare_tee_shares_no_contact():
    with pytest.raises(ValueError, match="shares none"):
        TeeSpec("box(", (".p()",), after=1, bare=True)
    TeeSpec("box(", (".p()",), after=0, bare=True)
