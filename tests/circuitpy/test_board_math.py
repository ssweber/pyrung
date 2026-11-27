"""Test that code.py keeps a Math's integer steps exact, because the P1AM-200 has no doubles.

A CircuitPython float on the board has 30 bits (``_circuitpython_float``). The host
runs code.py with doubles, so each test sends every ``float()`` call of code.py
through the board's rounding. It also rounds the stored value the same way.
"""

from __future__ import annotations

from pyrung.circuitpy import generate_circuitpy
from pyrung.core import Dint, Program, Real, copy, math, rung
from tests.circuitpy.test_codegen import _context_for_program, _run_single_scan_source
from tests.circuitpy.test_scan_time_and_stop import _circuitpython_float, _hw
from tests.circuitpy.test_system_points import _StubBase

#: Defines ``_board_float``, which code.py calls in place of ``float()``. A value past
#: the range of the board's float, or NaN, stays as it is.
_BOARD_FLOAT = """\
from tests.circuitpy.test_scan_time_and_stop import _circuitpython_float as _board_round


def _board_float(value):
    value = float(value)
    if value != value or abs(value) > 3.4e38:
        return value
    return _board_round(value)


"""


def test_a_difference_of_large_dints_into_a_real_stores_the_exact_difference(
    monkeypatch,
) -> None:
    first, second, total = Dint("First"), Dint("Second"), Real("Total")
    with Program(strict=False) as program:
        with rung():
            copy(100000001, first)
            copy(100000000, second)
            math(first - second, total)
    hw = _hw()
    result = generate_circuitpy(program, hw, target_scan_ms=10.0, runstop=None)
    source = _BOARD_FLOAT + result.code.replace("float(", "_board_float(")
    namespace = _run_single_scan_source(source, monkeypatch, _StubBase())
    stored = namespace[_context_for_program(program, hw).symbol_for_tag(total)]
    # A 30-bit float of 100000001 is 100000000, so a step in floats would store 0.0.
    assert _circuitpython_float(stored) == 1.0
