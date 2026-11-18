"""A source reaches a comparison through what a scan stores on the way.

A store into a target changes the value it was given.  A WORD stores 65535 when an INT
copies -1 to it.  A math into an INT wraps ``I + 1`` to -32768 when ``I`` is 32767.  Each row
is stepped on both engines, which must agree that the coil turns on.
"""

from __future__ import annotations

from typing import Any

import pytest

from pyrung.core import (
    Block,
    Bool,
    Dint,
    Int,
    Program,
    TagType,
    copy,
    math,
    out,
    rung,
    unpack_to_bits,
)
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.runner import PLC

ENGINES = (PLC, CompiledPLC)


def _fires_at(program: Program, source: str, value: Any, hit: Bool) -> bool:
    """Return whether a scan with *source* at *value* turns the coil on.

    Assert that both engines give the same answer.
    """
    answers = []
    for engine in ENGINES:
        plc = engine(program, dt=0.01)
        plc.patch({source: value})
        plc.step()
        answers.append(bool(plc.current_state.tags.get(hit.name)))
    assert answers[0] == answers[1]
    return answers[0]


_I = Int("I", external=True)
_DI = Int("DI")
_DD = Dint("DD")
_HIT = Bool("Hit")


def _rungs(*pairs: tuple[Any, ...]) -> Program:
    """Return a program of ``(body, *conditions)`` rungs."""
    with Program(strict=False) as program:
        for body, *conditions in pairs:
            with rung(*conditions):
                body()
    return program


def _hit() -> None:
    out(_HIT)


def _a_square_squared() -> Program:
    """The math's target is not compared.  Nothing is carried back to it, only to what it feeds."""
    squared = Dint("Squared")
    return _rungs(
        (lambda: copy(_I, _DI),),
        (lambda: math(_I * _I, _DD),),
        (lambda: math(_DD * _DD, squared),),
        (_hit, _DI > 0, squared == 2401),
    )


def _unpacked_too() -> Program:
    bits = Block("Bits", TagType.BOOL, 1, 16)
    return _rungs(
        (lambda: copy(_I, _DI),),
        (lambda: unpack_to_bits(_I, bits.select(1, 16)),),
        (_hit, _DI > 0, bits[4]),
    )


def _a_pointer_too() -> Program:
    table = Block("Arr", TagType.INT, 1, 5)
    return _rungs(
        (lambda: copy(42, table[3]),),
        (lambda: copy(_I, _DI),),
        (lambda: copy(table[_I], _DD),),
        (_hit, _DI > 0, _DD == 42),
    )


# A carried source's other way to the comparison goes through something the carried set
# does not look at: the source reaches more than its own comparisons with a literal.
ANOTHER_WAY_THERE = pytest.mark.parametrize(
    ("build", "value"),
    [
        pytest.param(_a_square_squared, 7, id="a-calc-of-a-calc"),
        pytest.param(_unpacked_too, 8, id="unpack-to-bits"),
        pytest.param(_a_pointer_too, 3, id="a-pointer"),
    ],
)


@ANOTHER_WAY_THERE
def test_the_scans_another_way_there_reaches_do_fire(build, value) -> None:
    assert _fires_at(build(), "I", value, _HIT)
