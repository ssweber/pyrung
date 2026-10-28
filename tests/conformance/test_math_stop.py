"""Math errors that stop the PLC: one group per case (subroutines ``Conf_Stop*``).

CLICK: "SC46 _Math_Operation_Error: Data Registers used in the Math formula
have Invalid values.  SC50 is turned to ON, the PLC is put in Stop Mode."
pyrung's model: the Math stores 0 in its destination, sets SC46, finishes the
scan, and the PLC ends the scan in STOP with SC50 on.

Each group holds one case and is kept out of ``suite.ALL_GROUPS``: a case that
stops the PLC ends the run, so it cannot share one with anything, and a normal
wire run (no ``GROUP=``) never touches these registers.  Their subroutines are
still in the exported project (``suite.STOP_GROUPS``), so the project loads
once.  Run one at a time::

    make test-conformance PLC=192.168.0.10 GROUP=stop_sqrt

then put the CLICK back in RUN by hand (CLICK Programming Software, or the
mode switch) before the next one: in STOP it serves its registers but runs no
ladder, so no case could reset.  The harness refuses to start against a PLC in
STOP (SC11 off).  On pyrung's local sim each run gets a fresh PLC, and
``make test-conformance`` runs every group here that way.

How a case reads: its firing rung presets the destination (99), raises
``Fired`` and only then runs the faulting Math, so ``Fired`` is up even on a
PLC that stops at the instruction.  The destination is a slot register,
retentive and still served in STOP, so the harness reads it directly; so are
SC46, SC50 and SC11 (``expect_system``), which the ladder cannot record once
the PLC has stopped.  The rung after the Math records whether the rest of the
scan saw SC46 (Result2).  What tells the models apart:

- Result1 = 99: the Math skipped the write; something else: it stored a value.
- Result2 = 0: the PLC stopped mid-scan (or set SC46 only after the scan).
- SC46 = 0: no math error.  SC11 = 1: the PLC kept running.  SC50 = 0: the
  firmware stops without raising SC50, or clears it on the way to STOP.

In-process, ``run()`` stops stepping once the PLC is in STOP and puts it back
in RUN before the next reset (``PLC.start()``), as the operator does.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from pyrung.conformance import Group, assert_all_passed, case, run, run_modbus
from pyrung.core import Rung, calc, copy, system
from pyrung.core.expression import acos, asin, log, log10, sqrt
from tests.conformance.memory import df

#: What every case leaves behind: the error flag, the STOP command bit, STOP.
STOPPED = {"SC46": 1, "SC50": 1, "SC11": 0}


def _stop_case(
    func: Callable[[Any], Any], operand: float, staged: int, case_id: str, sentence: str
):
    """One case: ``func`` of ``operand``, staged in DF<staged>, into Result1."""

    def ladder(slot):
        with Rung(slot.Cmd != 0, slot.Fired == 0):
            copy(operand, df[staged])
            copy(99, slot.Result1)
            copy(1, slot.Fired)
            calc(func(df[staged]), slot.Result1)
        with Rung(slot.Cmd != 0, system.fault.math_operation_error):
            copy(1, slot.Result2)

    def init(slot):
        copy(0, df[staged])

    return case(
        sentence,
        ladder=ladder,
        # A zero operand leaves the register as a fresh PLC has it.
        init=init if operand != 0 else None,
        id=case_id,
        expect={"Result1": 0, "Result2": 1},
        expect_system=STOPPED,
    )


def _sentence(what: str) -> str:
    return (
        f"A Math error stops the PLC: {what} writes 0 over the destination's 99 "
        "(Result1), the rest of the scan sees SC46 (Result2 = 1), and the PLC is left "
        "in STOP (SC11 = 0) with SC46 and SC50 on"
    )


groups = [
    Group(
        "stop_sqrt",
        [_stop_case(sqrt, -4.0, 401, "sqrt_negative", _sentence("SQRT(-4.0)"))],
    ),
    Group("stop_ln0", [_stop_case(log, 0.0, 411, "ln_zero", _sentence("LN(0.0)"))]),
    Group(
        "stop_ln_neg",
        [_stop_case(log, -1.0, 421, "ln_negative", _sentence("LN(-1.0)"))],
    ),
    Group("stop_log0", [_stop_case(log10, 0.0, 431, "log_zero", _sentence("LOG(0.0)"))]),
    Group(
        "stop_log_neg",
        [_stop_case(log10, -1.0, 441, "log_negative", _sentence("LOG(-1.0)"))],
    ),
    Group(
        "stop_asin",
        [_stop_case(asin, 1.5, 451, "asin_above_1", _sentence("ASIN(1.5)"))],
    ),
    Group(
        "stop_acos",
        [_stop_case(acos, 1.5, 461, "acos_above_1", _sentence("ACOS(1.5)"))],
    ),
]


@pytest.mark.parametrize("group", groups, ids=[g.name for g in groups])
def test_math_error_stops_the_plc(group):
    assert_all_passed(run(group))


@pytest.mark.conformance
@pytest.mark.parametrize("name", [g.name for g in groups])
def test_math_error_stops_the_local_sim(name, conformance_endpoint, conformance_groups):
    """Each group on a fresh local sim.  A real CLICK runs them only by ``GROUP=``."""
    if conformance_endpoint is not None or conformance_groups is not None:
        pytest.skip("STOP groups run on a real PLC one at a time, with GROUP=")
    from tests.conformance import suite  # suite imports this module

    results = run_modbus(suite.build(), groups=[name])
    assert len(results) == 1
    assert_all_passed(results)
