"""calc math-domain errors: 0 in the destination, SC46 on for the scan, PLC in RUN.

CLICK's help: "SC46 _Math_Operation_Error: Data Registers used in the Math
formula have Invalid values.  SC50 is turned to ON, the PLC is put in Stop
Mode."  The hardware does not stop (conformance ``math_error``): SC46 is on
for the rest of the faulting scan, clears at the next scan start, SC50 stays
off and the PLC keeps running.  The ``runner_factory`` cases run on the
interpreter and the compiled kernel (and assert they agree under
``--runner-backend=both``).
"""

from __future__ import annotations

import pytest

from pyrung.core import Bool, Int, Program, Real, Rung, calc, copy, out, system
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.expression import (
    MathDomainError,
    acos,
    asin,
    cos,
    log,
    log10,
    sin,
    sqrt,
    tan,
)
from pyrung.core.instruction.calc import CalcInstruction

SC46 = system.fault.math_operation_error
SC50 = system.sys.cmd_mode_stop
SC40 = system.fault.division_error

DOMAIN_ERRORS = [
    pytest.param(sqrt, -4.0, id="sqrt-negative"),
    pytest.param(log, 0.0, id="ln-zero"),
    pytest.param(log, -1.0, id="ln-negative"),
    pytest.param(log10, 0.0, id="log-zero"),
    pytest.param(log10, -1.0, id="log-negative"),
    pytest.param(asin, 1.5, id="asin-above-1"),
    pytest.param(acos, -1.5, id="acos-below-minus-1"),
]


def _program(func, dest_type=Real):
    x = Real("X")
    dest = dest_type("Dest")
    go = Bool("Go")
    seen = Bool("Seen", retentive=False)
    after = Int("After")
    with Program() as program:
        with Rung(go):
            copy(99, dest)
            calc(func(x), dest)
        with Rung(SC46):
            out(seen)
        with Rung(go):
            copy(7, after)
    return program, x, go


@pytest.mark.parametrize(("func", "operand"), DOMAIN_ERRORS)
def test_domain_error_writes_zero_and_keeps_running(runner_factory, func, operand):
    program, x, go = _program(func)
    plc = runner_factory(program)
    plc.patch({x: operand, go: True})
    plc.step()

    tags = plc.current_state.tags
    assert tags["Dest"] == 0.0
    assert tags[SC46.name] is True
    # The rest of the faulting scan runs: a later rung sees SC46 ...
    assert tags["Seen"] is True
    assert tags["After"] == 7
    # ... and the PLC stays in RUN, SC50 off.
    assert not tags.get(SC50.name, False)
    assert plc.mode == "run"


@pytest.mark.parametrize("dest_type", [Int, Real])
def test_domain_error_into_any_destination_writes_zero(runner_factory, dest_type):
    program, x, go = _program(sqrt, dest_type)
    plc = runner_factory(program)
    plc.patch({x: -1.0, go: True})
    plc.step()
    assert plc.current_state.tags["Dest"] == 0
    assert plc.mode == "run"


def test_the_error_clears_at_the_next_scan(runner_factory):
    program, x, go = _program(sqrt)
    plc = runner_factory(program)
    plc.patch({x: -4.0, go: True})
    plc.step()
    assert plc.current_state.tags[SC46.name] is True

    plc.patch({x: 16.0, go: True})
    plc.step()
    tags = plc.current_state.tags
    assert plc.mode == "run"
    assert tags["Dest"] == 4.0
    assert tags[SC46.name] is False
    assert not tags.get(SC50.name, False)
    assert tags["Seen"] is False


def test_an_error_every_scan_raises_sc46_every_scan(runner_factory):
    program, x, go = _program(sqrt)
    plc = runner_factory(program)
    plc.patch({x: -4.0, go: True})
    for _ in range(5):
        plc.patch({go: True})
        plc.step()
        tags = plc.current_state.tags
        assert plc.mode == "run"
        assert tags[SC46.name] is True
        assert tags["Seen"] is True
        assert tags["Dest"] == 0.0


def test_a_valid_operand_does_not_fault(runner_factory):
    program, x, go = _program(sqrt)
    plc = runner_factory(program)
    plc.patch({x: 2.25, go: True})
    plc.step()
    assert plc.current_state.tags["Dest"] == 1.5
    assert plc.mode == "run"
    assert not plc.current_state.tags.get(SC46.name, False)


def test_non_finite_operand_is_a_division_error_not_a_domain_error(runner_factory):
    # -1 / 0 is a division by zero; sqrt passes the non-finite operand on.
    x = Real("X", default=-1.0)
    zero = Real("Zero")
    dest = Real("Dest")
    with Program() as program:
        with Rung():
            calc(sqrt(x / zero), dest)
    plc = runner_factory(program)
    plc.step()
    tags = plc.current_state.tags
    assert tags["Dest"] == 0.0
    assert tags[SC40.name] is True
    assert not tags.get(SC46.name, False)
    assert plc.mode == "run"


@pytest.mark.parametrize("func", [sin, cos, tan])
def test_unrestricted_functions_of_a_non_finite_operand_do_not_stop(runner_factory, func):
    x = Real("X", default=1.0)
    zero = Real("Zero")
    dest = Real("Dest")
    with Program() as program:
        with Rung():
            calc(func(x / zero), dest)
    plc = runner_factory(program)
    plc.step()
    assert plc.current_state.tags[SC40.name] is True
    assert plc.mode == "run"


def test_math_domain_error_is_a_value_error():
    assert issubclass(MathDomainError, ValueError)


def test_only_domain_limited_calcs_declare_sc46():
    x, dest = Real("X"), Real("Dest")
    assert SC46.name in CalcInstruction(sqrt(x) + 1, dest)._fault_writes
    assert SC46.name in CalcInstruction(acos(x), dest)._fault_writes
    assert SC46.name not in CalcInstruction(sin(x) + x, dest)._fault_writes
    assert SC46.name not in CalcInstruction(x * 2, dest)._fault_writes


def test_pdg_sees_the_calc_write_sc46():
    x, dest, seen = Real("X"), Real("Dest"), Bool("Seen")
    with Program(strict=False) as program:
        with Rung():
            calc(sqrt(x), dest)
        with Rung(SC46):
            out(seen)
    graph = build_program_graph(program)
    assert graph.writers_of[SC46.name] == frozenset({0})
