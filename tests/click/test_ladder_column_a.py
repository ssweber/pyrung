"""Column A is CLICK's left power rail: no exported row carries a vertical token there.

CLICK refuses ``T``, ``|`` and ``T:`` in column A ("Vertical-down tokens are not
allowed in Column A").  A rung with no contacts, or a branch with none, starts
every row from the rail, so its rows need no split marker.
"""

from __future__ import annotations

import pytest

from pyrung.click import ClickBlocks, LadderExportError, TagMap, pyrung_to_ladder
from pyrung.click.ladder.layout import assert_no_column_a_vertical
from pyrung.core import Counter, Program, Rung, Timer
from pyrung.core.program import (
    branch,
    calc,
    copy,
    count_up,
    forloop,
    on_delay,
    out,
    subroutine,
)
from tests.click.helpers import round_trip_logic

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
            calc(ds[1] + 1, ds[1])
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
                calc(ds[1] + 1, ds[1])
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


ONE_RUNG = [
    _calc_copy,
    _branch_after_out,
    _nested_branch,
    _branch_with_two_outputs,
    _two_branches,
    _branches_only_2,
    _branches_only_3,
    _for_body,
    _subroutine,
]


@pytest.mark.parametrize("shape", ONE_RUNG, ids=lambda f: f.__name__)
def test_importer_reads_the_unconditional_shape_as_one_rung(shape):
    from pyrung.click import ladder_to_pyrung

    code = ladder_to_pyrung(pyrung_to_ladder(shape(), _tag_map()))
    assert code.count("with rung(") == 1, code
    assert ".continued()" not in code, code


def test_rail_rows_still_read_as_continued():
    from pyrung.click import ladder_to_pyrung

    with Program() as p:
        with Rung(c[1]):
            out(c[2])
        with Rung(c[3]).continued():
            out(c[4])
    code = ladder_to_pyrung(pyrung_to_ladder(p, _tag_map()))
    assert ".continued()" in code, code


def test_unconditional_outputs_hang_a_wire_trunk_and_split_in_column_b():
    rows = pyrung_to_ladder(_calc_copy(), _tag_map()).main_rows[1:3]
    assert [r[0] for r in rows] == ["R", ""]
    assert [r[1:3] for r in rows] == [("-", "T"), ("", "-")]
    rows = pyrung_to_ladder(_multi(), _tag_map()).main_rows[1:4]
    assert [r[1:3] for r in rows] == [("-", "T"), ("", "T"), ("", "-")]


def test_unconditional_branch_splits_in_column_b():
    rows = pyrung_to_ladder(_branch_after_out(), _tag_map()).main_rows[1:3]
    assert rows[0][1:3] == ("-", "T")
    assert rows[1][1] == "" and rows[1][2].upper() == "C2"


def test_importer_reads_wire_trunk_back_to_one_rung_in_order():
    from pyrung.click import ladder_to_pyrung

    code = ladder_to_pyrung(pyrung_to_ladder(_multi(), _tag_map()))
    assert code.count("with rung()") == 1, code
    assert code.index("out(C1)") < code.index("out(C2)") < code.index("out(C3)"), code


def test_guard_refuses_a_vertical_token_in_column_a():
    for bad in ("T", "|", "T:C1"):
        with pytest.raises(ValueError, match="column A"):
            assert_no_column_a_vertical([("R", bad, "-", "out(C1)")])
    assert_no_column_a_vertical([("#", "T"), ("R", "C1", "T", "out(C1)")])


def test_exporter_surfaces_the_guard_as_an_export_error(monkeypatch):
    from pyrung.click.ladder import layout

    monkeypatch.setattr(
        layout._LayoutMixin,
        "_render_slots_on_condition_rows",
        lambda self, **kw: [("R", "T", "-", "out(C1)")],
    )
    with pytest.raises(LadderExportError, match="column A"):
        pyrung_to_ladder(_calc_copy(), _tag_map())
