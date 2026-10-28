"""A reversed shift range exports as CLICK's descending range (A4).

CLICK stores a shift register's start and end as separate fields; an ending
address below the starting one shifts from start down to end.  pyrung spells
that ``c.select(1011, 1018).reverse()``.
"""

from __future__ import annotations

import pytest

from pyrung.click import (
    ClickBlocks,
    TagMap,
    ladder_to_pyrung,
    ladder_to_pyrung_project,
    pyrung_to_ladder,
)
from pyrung.core import Program, Rung
from pyrung.core.program import shift
from tests.click.helpers import round_trip_logic, strip_pyrung_boilerplate

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()


def _program() -> Program:
    with Program() as logic:
        with Rung(c[1]):
            shift(c.select(1011, 1018).reverse()).clock(c[2]).reset(c[3])
        with Rung(c[1]):
            shift(c.select(11, 18)).clock(c[2]).reset(c[3])
    return logic


def test_reversed_range_exports_descending():
    rows = pyrung_to_ladder(_program(), TagMap()).main_rows
    assert [row[-1] for row in rows if row[-1].startswith("shift(")] == [
        "shift(C1018..C1011)",
        "shift(C11..C18)",
    ]


def test_descending_range_imports_as_reverse():
    body = strip_pyrung_boilerplate(ladder_to_pyrung(pyrung_to_ladder(_program(), TagMap())))
    assert "shift(c.select(1011, 1018).reverse()).clock(C2).reset(C3)" in body
    assert "shift(c.select(11, 18)).clock(C2).reset(C3)" in body


def test_workspace_import_uses_reverse():
    files = ladder_to_pyrung_project(pyrung_to_ladder(_program(), TagMap()))
    assert "shift(c.select(1011, 1018).reverse())" in files["src/plc/main.py"]


def test_reversed_range_round_trips():
    code, before, after = round_trip_logic(_program(), TagMap())
    assert after == before, code


def test_descending_select_still_raises():
    with pytest.raises(ValueError, match="must be <= end"):
        c.select(1018, 1011)
