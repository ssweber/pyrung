"""Column A is CLICK's left power rail: no exported row carries a vertical token there.

CLICK refuses ``T``, ``|`` and ``T:`` in column A ("Vertical-down tokens are not
allowed in Column A").  Items that hang straight from the rail are separate wires.
CLICK's syntax check makes each of them a rung of its own, so the export draws them
that way.  The importer still reads a wire in column A that splits in column B as
one rung.
"""

from __future__ import annotations

import dataclasses

import pytest

from pyrung.click import ClickBlocks, LadderExportError, TagMap, ladder_to_pyrung, pyrung_to_ladder
from pyrung.click.ladder.layout import assert_no_column_a_vertical
from pyrung.core import Counter, Program, Rung, Timer
from pyrung.core.program import (
    branch,
    copy,
    count_up,
    forloop,
    math,
    on_delay,
    out,
    subroutine,
)
from tests.click.helpers import join_continued, round_trip_logic, strip_pyrung_boilerplate

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()


def _tag_map() -> TagMap:
    return TagMap(
        {
            Timer[1].Done: t[1],
            Timer[1].Acc: td[1],
            Counter[1].Done: ct[1],
            Counter[1].Acc: ctd[1],
        },
        include_system=False,
    )


def _multi():
    with Program() as p:
        with Rung():
            out(c[1])
            out(c[2])
            out(c[3])
    return p


def _calc_copy():
    with Program() as p:
        with Rung():
            math(ds[1] + 1, ds[1])
            copy(1, ds[2])
    return p


def _branch_after_out():
    with Program() as p:
        with Rung():
            out(c[1])
            with branch(c[2]):
                out(c[3])
    return p


def _two_branches():
    with Program() as p:
        with Rung():
            with branch(c[2]):
                out(c[3])
            with branch(c[4]):
                out(c[5])
            out(c[1])
    return p


def _branch_with_two_outputs():
    with Program() as p:
        with Rung():
            with branch(c[2]):
                out(c[3])
                out(c[4])
            out(c[1])
    return p


def _nested_branch():
    with Program() as p:
        with Rung():
            with branch():
                with branch(c[2]):
                    out(c[3])
                out(c[4])
            out(c[1])
    return p


def _branches_only_2():
    with Program() as p:
        with Rung():
            with branch(c[2]):
                out(c[1])
            with branch(c[3]):
                out(c[4])
    return p


def _branches_only_3():
    with Program() as p:
        with Rung():
            with branch(c[2]):
                out(c[1])
            with branch(c[3]):
                out(c[4])
            with branch(c[5]):
                out(c[6])
    return p


def _box_with_pins():
    with Program() as p:
        with Rung():
            count_up(Counter[1], 5).down(c[4]).reset(c[2])
    return p


def _timer_pin():
    with Program() as p:
        with Rung():
            on_delay(Timer[1], 100).reset(c[2])
    return p


def _for_body():
    with Program() as p:
        with Rung(c[1]):
            with forloop(3):
                with Rung():
                    copy(1, ds[1])
                    copy(2, ds[2])
    return p


def _subroutine():
    with Program() as p:
        with subroutine("Gate"):
            with Rung():
                math(ds[1] + 1, ds[1])
                copy(1, ds[2])
    return p


def _continued():
    with Program() as p:
        with Rung():
            out(c[1])
        with Rung(c[2]):
            out(c[3])
            out(c[4])
    return p


SHAPES = [
    _multi,
    _calc_copy,
    _branch_after_out,
    _two_branches,
    _branch_with_two_outputs,
    _nested_branch,
    _branches_only_2,
    _branches_only_3,
    _box_with_pins,
    _timer_pin,
    _for_body,
    _subroutine,
]


def _all_rows(bundle):
    yield from bundle.main_rows
    for _name, rows in bundle.subroutine_rows:
        yield from rows


@pytest.mark.parametrize("shape", SHAPES, ids=lambda f: f.__name__)
def test_no_vertical_token_in_column_a_and_round_trip(shape):
    program = shape()
    bundle = pyrung_to_ladder(program, _tag_map())
    for row in _all_rows(bundle):
        if row[0].startswith("#"):
            continue
        assert not (row[1] in ("T", "|") or row[1].startswith("T:")), row
    _code, before, after = round_trip_logic(program, _tag_map())
    assert before == after


# For each CLICK rung a shape exports to, the first cells and AF of its first row.
RAIL_ITEMS = [
    (_multi, [("-", "out(C1)"), ("-", "out(C2)"), ("-", "out(C3)")]),
    (_calc_copy, [("-", "math(DS1 + 1,DS1,mode=decimal)"), ("-", "copy(1,DS2)")]),
    (_branch_after_out, [("-", "out(C1)"), ("C2", "out(C3)")]),
    (_two_branches, [("C2", "out(C3)"), ("C4", "out(C5)"), ("-", "out(C1)")]),
    (_branch_with_two_outputs, [("C2", "out(C3)"), ("-", "out(C1)")]),
    (_nested_branch, [("C2", "out(C3)"), ("-", "out(C4)"), ("-", "out(C1)")]),
    (_branches_only_2, [("C2", "out(C1)"), ("C3", "out(C4)")]),
    (_branches_only_3, [("C2", "out(C1)"), ("C3", "out(C4)"), ("C5", "out(C6)")]),
]


@pytest.mark.parametrize(("shape", "rungs"), RAIL_ITEMS, ids=lambda v: getattr(v, "__name__", ""))
def test_each_item_on_the_rail_is_a_click_rung_of_its_own(shape, rungs):
    rows = pyrung_to_ladder(shape(), _tag_map()).main_rows[1:-1]
    assert [(row[1], row[-1]) for row in rows if row[0] == "R"] == rungs
    code = ladder_to_pyrung(pyrung_to_ladder(shape(), _tag_map()))
    assert ".continued()" not in code, code


def test_each_item_on_the_rail_is_a_rung_of_its_own_in_a_loop_or_subroutine():
    rows = pyrung_to_ladder(_for_body(), _tag_map()).main_rows[1:-1]
    assert [(row[0], row[1], row[-1]) for row in rows] == [
        ("R", "C1", "for(3)"),
        ("R", "-", "copy(1,DS1)"),
        ("R", "-", "copy(2,DS2)"),
        ("R", "-", "next()"),
    ]
    rows = dict(pyrung_to_ladder(_subroutine(), _tag_map()).subroutine_rows)["Gate"][1:]
    assert [(row[0], row[1], row[-1]) for row in rows] == [
        ("R", "-", "math(DS1 + 1,DS1,mode=decimal)"),
        ("R", "-", "copy(1,DS2)"),
        ("R", "-", "return()"),
    ]


def test_importer_reads_the_wires_of_one_click_rung_as_continued_rungs():
    with Program() as p:
        with Rung(c[1]):
            out(c[2])
        with Rung(c[3]).continued():
            out(c[4])
    bundle = join_continued(pyrung_to_ladder(p, _tag_map()), p)
    assert [row[0] for row in bundle.main_rows[1:]] == ["R", "", "R"]
    code = strip_pyrung_boilerplate(ladder_to_pyrung(bundle))
    assert code == "with rung(C1):\n    out(C2)\nwith rung(C3).continued():\n    out(C4)"


def _row(marker: str, cells: list[str], af: str) -> tuple[str, ...]:
    return (marker, *cells, *["-"] * (31 - len(cells)), af)


def test_importer_reads_a_wire_split_in_column_b_as_one_rung_in_order():
    """An earlier export, or a program saved before a syntax check, can hold this drawing."""
    rows = (
        pyrung_to_ladder(_multi(), _tag_map()).main_rows[0],
        _row("R", ["-", "T"], "out(C1)"),
        _row("", ["", "T"], "out(C2)"),
        _row("", ["", "-"], "out(C3)"),
        _row("R", [], "end()"),
    )
    bundle = dataclasses.replace(pyrung_to_ladder(_multi(), _tag_map()), main_rows=rows)
    code = strip_pyrung_boilerplate(ladder_to_pyrung(bundle))
    assert code == "with rung():\n    out(C1)\n    out(C2)\n    out(C3)", code


def test_guard_refuses_a_vertical_token_in_column_a():
    for bad in ("T", "|", "T:C1"):
        with pytest.raises(ValueError, match="column A"):
            assert_no_column_a_vertical([("R", bad, "-", "out(C1)")])
    assert_no_column_a_vertical([("#", "T"), ("R", "C1", "T", "out(C1)")])


def test_exporter_surfaces_the_guard_as_an_export_error(monkeypatch):
    from pyrung.click.ladder import layout

    monkeypatch.setattr(
        layout._LayoutMixin,
        "_render_network",
        lambda self, *args, **kw: [("R", "T", "-", "out(C1)")],
    )
    with pytest.raises(LadderExportError, match="column A"):
        pyrung_to_ladder(_calc_copy(), _tag_map())


def test_the_layout_refuses_two_items_on_the_rail_in_one_click_rung():
    from pyrung.click.ladder._exporter import _LadderExporter
    from pyrung.click.ladder.types import _RenderError

    exporter = _LadderExporter(tag_map=_tag_map(), program=_multi())
    with pytest.raises(_RenderError, match="two items on the rail"):
        exporter._render_network(
            [], ["out(C1)", "out(C2)"], path="main.rung[0]", source=None, first_marker="R"
        )
