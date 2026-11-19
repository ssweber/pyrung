"""The kernel declines a form that stepping raises on only in a scan where its rung runs.

pyrung builds a program with such a form, and ``generate_circuitpy()`` refuses it.  While
the rung does not run, stepping runs the program.  So a long run must step those scans
without the warning about a bug in pyrung.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from typing import Any

import pytest

from pyrung import PLC, Program, rung
from pyrung.circuitpy import P1AM, generate_circuitpy
from pyrung.click import ClickBlocks
from pyrung.core import Bool, jit_stats, search

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()
ENABLE = Bool("Enable")

FORMS: dict[str, Callable[[], Any]] = {
    "text search with >": lambda: search(txt.select(1, 4) > "A", result=ds[10], found=c[10]),
    "text search for ''": lambda: search(txt.select(1, 4) == "", result=ds[10], found=c[10]),
}


def _program(build: Callable[[], Any]) -> Program:
    with Program(strict=False) as program:
        with rung(ENABLE):
            build()
    return program


@pytest.mark.parametrize("name", list(FORMS))
def test_a_long_run_steps_the_form_with_no_warning(name: str, loop_from_first_scan: None) -> None:
    program = _program(FORMS[name])
    stepped = PLC(program, dt=0.01)
    for _ in range(30):
        stepped.step()
    plc = PLC(program, dt=0.01)
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        with jit_stats.collect() as stats:
            plc.run_for(0.3)
        assert plc._compiled_replay_supported_kernel() is None
    assert stats.scans["interpreted"] == 30
    assert plc.current_state.tags == stepped.current_state.tags
    # The form raises once its rung runs.
    plc.patch({ENABLE: True})
    with pytest.raises(ValueError, match="Text search"):
        plc.step()


@pytest.mark.parametrize("name", list(FORMS))
def test_generate_circuitpy_refuses_the_form(name: str) -> None:
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    with pytest.raises(ValueError, match="Text search"):
        generate_circuitpy(_program(FORMS[name]), hw, target_scan_ms=10.0)
