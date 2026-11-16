"""A coil read above the rung that writes it is state, not combinational.

A read that comes before a tag's first write in scan order sees the value the
tag ended the previous scan with.  So a chain of coils written below their
readers (``Rung(Y2): out(Y3)`` above ``Rung(Y1): out(Y2)``) keeps each coil as
state in the facts.
"""

from __future__ import annotations

from typing import Any

import pytest

from pyrung.core import (
    And,
    Bool,
    Int,
    Or,
    Program,
    Rung,
    branch,
    call,
    copy,
    fall,
    forloop,
    math,
    out,
    return_early,
    rise,
    subroutine,
)
from pyrung.core.analysis.prove import Intractable
from pyrung.core.analysis.prove.classify import _classify_dimensions

Btn = Bool("Btn", external=True)
G = Bool("G", external=True)
Y1 = Bool("Y1")
Y2 = Bool("Y2")
Y3 = Bool("Y3")
D = Int("D")
E = Int("E")


def _y3(tags: Any) -> bool:
    return tags["Y3"]


def _y3_without_btn(tags: Any) -> bool:
    return tags["Y3"] and not tags["Btn"]


def _y3_without_g(tags: Any) -> bool:
    return tags["Y3"] and not tags["G"]


def _y3_with_g(tags: Any) -> bool:
    return tags["Y3"] and tags["G"]


def _main_chain():
    with Program(strict=False) as logic:
        with Rung(Y2):
            out(Y3)
        with Rung(Y1):
            out(Y2)
        with Rung(rise(Btn)):
            out(Y1)
    return logic, ["Btn"], Y3, _y3


def _subroutine_chain():
    with Program(strict=False) as logic:
        with Rung():
            call("chain")
        with subroutine("chain"):
            with Rung(Y2):
                out(Y3)
            with Rung(Y1):
                out(Y2)
            with Rung(rise(Btn)):
                out(Y1)
    return logic, ["Btn"], Y3, _y3


def _main_reads_above_call():
    with Program(strict=False) as logic:
        with Rung(Y2):
            out(Y3)
        with Rung():
            call("chain")
        with subroutine("chain"):
            with Rung(Y1):
                out(Y2)
            with Rung(rise(Btn)):
                out(Y1)
    return logic, ["Btn"], Y3, _y3


def _for_body_chain():
    with Program(strict=False) as logic:
        with Rung(Y2):
            with forloop(1):
                out(Y3)
        with Rung(Y1):
            with forloop(1):
                out(Y2)
        with Rung(rise(Btn)):
            with forloop(1):
                out(Y1)
    return logic, ["Btn"], Y3, _y3


def _for_body_skipped():
    with Program(strict=False) as logic:
        with Rung(Btn):
            with forloop(1):
                out(Y2)
        with Rung(Y2):
            out(Y3)
    return logic, ["Btn"], And(Y3, ~Btn), _y3_without_btn


def _for_zero_count():
    with Program(strict=False) as logic:
        with Rung(G):
            copy(0, D)
        with Rung(~G):
            copy(1, D)
        with Rung(Btn):
            with forloop(D):
                out(Y2)
        with Rung(Y2):
            out(Y3)
    return logic, ["Btn", "G"], And(Y3, ~Btn), _y3_without_btn


def _through_or():
    with Program(strict=False) as logic:
        with Rung(Or(Y2, G)):
            out(Y3)
        with Rung(Y1):
            out(Y2)
        with Rung(rise(Btn)):
            out(Y1)
    return logic, ["Btn", "G"], And(Y3, ~G), _y3_without_g


def _through_branch():
    with Program(strict=False) as logic:
        with Rung(G):
            with branch(Y2):
                out(Y3)
        with Rung(Y1):
            out(Y2)
        with Rung(rise(Btn)):
            out(Y1)
    return logic, ["Btn", "G"], And(Y3, G), _y3_with_g


def _rise_reader_pulse():
    # The pulse is in the entry value and the edge memory together: never seen.
    with Program(strict=False) as logic:
        with Rung(rise(Y2)):
            out(Y3)
        with Rung(Y1):
            out(Y2)
        with Rung(rise(Btn)):
            out(Y1)
    return logic, ["Btn"], Y3, _y3


def _fall_reader_pulse():
    with Program(strict=False) as logic:
        with Rung(fall(Y2)):
            out(Y3)
        with Rung(Y1):
            out(Y2)
        with Rung(rise(Btn)):
            out(Y1)
    return logic, ["Btn"], Y3, _y3


def _rise_reader_level():
    # rise() compares against the previous scan's end value, so a rise(Y2) above
    # Y2's only writer never fires, so Y3 is never on.
    with Program(strict=False) as logic:
        with Rung(rise(Y2)):
            out(Y3)
        with Rung(Btn):
            out(Y2)
    return logic, ["Btn"], And(Y3, ~Btn), _y3_without_btn


def _copy_dest_above_writer():
    with Program(strict=False) as logic:
        with Rung(D == 1):
            out(Y3)
        with Rung(Y1):
            copy(1, D)
        with Rung(~Y1):
            copy(0, D)
        with Rung(rise(Btn)):
            out(Y1)
    return logic, ["Btn"], Y3, _y3


def _calc_dest_above_writer():
    with Program(strict=False) as logic:
        with Rung(D == 1):
            out(Y3)
        with Rung():
            math(E * 1, D)
        with Rung(Y1):
            copy(1, E)
        with Rung(~Y1):
            copy(0, E)
        with Rung(rise(Btn)):
            out(Y1)
    return logic, ["Btn"], Y3, _y3


def _two_writers_read_between():
    with Program(strict=False) as logic:
        with Rung(Y1):
            out(Y2)
        with Rung(Y2):
            out(Y3)
        with Rung(rise(Btn)):
            out(Y1)
        with Rung(G):
            out(Y2)
    return logic, ["Btn", "G"], And(Y3, ~G), _y3_without_g


def _return_early_skips_writer():
    with Program(strict=False) as logic:
        with Rung():
            call("work")
        with Rung(Y2):
            out(Y3)
        with subroutine("work"):
            with Rung(G):
                return_early()
            with Rung(Btn):
                out(Y2)
    return logic, ["Btn", "G"], And(Y3, ~Btn), _y3_without_btn


_SHAPES = {
    "main_chain": _main_chain,
    "subroutine_chain": _subroutine_chain,
    "main_reads_above_call": _main_reads_above_call,
    "for_body_chain": _for_body_chain,
    "for_body_skipped": _for_body_skipped,
    "for_zero_count": _for_zero_count,
    "through_or": _through_or,
    "through_branch": _through_branch,
    "rise_reader_pulse": _rise_reader_pulse,
    "fall_reader_pulse": _fall_reader_pulse,
    "rise_reader_level": _rise_reader_level,
    "copy_dest_above_writer": _copy_dest_above_writer,
    "calc_dest_above_writer": _calc_dest_above_writer,
    "two_writers_read_between": _two_writers_read_between,
    "return_early_skips_writer": _return_early_skips_writer,
}


@pytest.mark.parametrize("shape_name", ["main_chain", "subroutine_chain", "through_or"])
def test_coil_read_above_its_writer_is_stateful(shape_name: str) -> None:
    logic, *_ = _SHAPES[shape_name]()

    result = _classify_dimensions(logic)

    assert not isinstance(result, Intractable)
    stateful, _nd, combinational, _done_acc, _done_presets, _done_kinds = result
    assert {"Y1", "Y2"} <= set(stateful)
    assert "Y3" in combinational  # read by nothing: still left out of the key


def test_coil_read_only_below_its_writer_stays_combinational() -> None:
    with Program(strict=False) as logic:
        with Rung(rise(Btn)):
            out(Y1)
        with Rung(Y1):
            out(Y2)
        with Rung(Y2):
            out(Y3)

    result = _classify_dimensions(logic)

    assert not isinstance(result, Intractable)
    stateful, _nd, combinational, _done_acc, _done_presets, _done_kinds = result
    assert {"Y1", "Y2", "Y3"} <= combinational
    assert not {"Y1", "Y2"} & set(stateful)
