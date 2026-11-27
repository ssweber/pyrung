"""Check the sign of the 0 that a decimal ``math()`` stores into a Real, on every backend.

A CLICK computes a decimal Math in doubles. A DS of 0 is the double +0.0, a minus sign flips
the sign bit, and the 0 of a MOD has the dividend's sign. A CLICK was read storing these zeros
in reading round b16 (``math_range/minus_sign`` and ``math_range/zero_sum``). ``==`` cannot
tell -0.0 from +0.0, so each check compares the sign bit.
"""

from __future__ import annotations

import math as pymath

import pytest

from pyrung.core import PLC, Int, Program, Real, copy, rung
from pyrung.core import math as math_instruction
from pyrung.core.compiled_plc import CompiledPLC
from tests.circuitpy.test_codegen import _on_every_backend

ZERO, ONE, MINUS_FIVE, FIVE = Int("Zero"), Int("One"), Int("MinusFive"), Int("Five")
ZERO_R, MINUS_ZERO_R, ONE_R, FIVE_R = Real("ZeroR"), Real("MinusZeroR"), Real("OneR"), Real("FiveR")

#: (formula, sign of the 0 that it stores). True is -0.0.
FORMULAS = [
    ("- DS 0", lambda: -ZERO, True),
    ("DS 0 * DS -5", lambda: ZERO * MINUS_FIVE, True),
    ("- DS 0 * DS -5", lambda: -ZERO * MINUS_FIVE, False),
    ("- DS 0 / DS 1", lambda: -ZERO / ONE, True),
    ("- ( DS 0 / DS 1 )", lambda: -(ZERO / ONE), True),
    ("- DS 0 MOD DS 1", lambda: -ZERO % ONE, True),
    ("DS -5 MOD DS 5", lambda: MINUS_FIVE % FIVE, True),
    ("DS -5 MOD DS 0", lambda: MINUS_FIVE % ZERO, False),
    ("DS 5 + DS -5", lambda: FIVE + MINUS_FIVE, False),
    ("DF -0.0 MOD DF 5.0", lambda: MINUS_ZERO_R % FIVE_R, True),
    ("DF 0.0 MOD DF 5.0", lambda: ZERO_R % FIVE_R, False),
    ("- DF 0.0 MOD DF 1.0", lambda: -ZERO_R % ONE_R, True),
    ("DF -0.0 MOD DF 0.0", lambda: MINUS_ZERO_R % ZERO_R, False),
]

#: Known difference on CircuitPython: the board has no doubles, so code.py keeps integer
#: steps exact. An integer 0 has no sign, so code.py stores +0.0 for these formulas.
INTEGER_ZERO_ON_CODE_PY = frozenset(
    {"- DS 0", "DS 0 * DS -5", "- DS 0 / DS 1", "- DS 0 MOD DS 1", "DS -5 MOD DS 5"}
)


def _program() -> tuple[Program, list[Real]]:
    dests = [Real(f"Dest{n}") for n in range(len(FORMULAS))]
    with Program(strict=False) as program:
        with rung():
            copy(0, ZERO)
            copy(1, ONE)
            copy(-5, MINUS_FIVE)
            copy(5, FIVE)
            copy(0.0, ZERO_R)
            copy(1.0, ONE_R)
            copy(5.0, FIVE_R)
            math_instruction(ZERO_R * -1.0, MINUS_ZERO_R)
        with rung():
            for (_, formula, _), dest in zip(FORMULAS, dests, strict=True):
                math_instruction(formula(), dest)
    return program, dests


def _signs(values) -> list[bool]:
    assert all(value == 0 for value in values)
    return [pymath.copysign(1.0, value) < 0 for value in values]


def test_each_backend_stores_the_zero_sign_that_a_click_stores(monkeypatch) -> None:
    program, dests = _program()
    results = _on_every_backend(program, monkeypatch, dests, scans=2)
    for backend, values in results.items():
        want = [
            minus and not (backend == "circuitpy" and text in INTEGER_ZERO_ON_CODE_PY)
            for text, _, minus in FORMULAS
        ]
        got = _signs(values)
        wrong = [text for (text, _, _), g, w in zip(FORMULAS, got, want, strict=True) if g != w]
        assert not wrong, backend


@pytest.mark.parametrize("runner", [PLC, CompiledPLC], ids=["interpreted", "compiled"])
def test_a_long_run_stores_the_zero_sign_that_a_click_stores(runner) -> None:
    program, dests = _program()
    plc = runner(program)
    plc.run_for(1.0)
    got = _signs([plc.current_state.tags[dest.name] for dest in dests])
    assert got == [minus for _, _, minus in FORMULAS]
