"""calc math-domain errors: 0 in the destination, SC46 on until the next Math, PLC in RUN.

CLICK's help: "SC46 _Math_Operation_Error: Data Registers used in the Math
formula have Invalid values.  SC50 is turned to ON, the PLC is put in Stop
Mode."  The hardware does not stop (conformance ``math_error``).  SC46 stays on
until the next Math the scan reaches clears it, in this scan or a later one
(conformance ``flag_carry``, ``flag_reads/math_after_sqrt``).  SC50 stays off
and the PLC keeps running.  The ``runner_factory`` cases run on the
interpreter and the compiled kernel (and assert they agree under
``--runner-backend=both``).
"""

from __future__ import annotations

import pytest

from pyrung.core import Bool, Int, Program, Real, Rung, copy, math, out, system
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
            math(func(x), dest)
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


def test_the_next_math_clears_the_error(runner_factory):
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
    # -1 / 0 is a division by zero: it is 0 with SC40, and sqrt(0) is 0, no SC46.
    x = Real("X", default=-1.0)
    zero = Real("Zero")
    dest = Real("Dest")
    with Program() as program:
        with Rung():
            math(sqrt(x / zero), dest)
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
            math(func(x / zero), dest)
    plc = runner_factory(program)
    plc.step()
    assert plc.current_state.tags[SC40.name] is True
    assert plc.mode == "run"


def test_math_domain_error_is_a_value_error():
    assert issubclass(MathDomainError, ValueError)


def test_every_calc_declares_sc46():
    """A Math with no function in it still clears SC46, so it writes the flag."""
    x, dest = Real("X"), Real("Dest")
    assert SC46.name in CalcInstruction(sqrt(x) + 1, dest)._fault_writes
    assert SC46.name in CalcInstruction(acos(x), dest)._fault_writes
    assert SC46.name in CalcInstruction(sin(x) + x, dest)._fault_writes
    assert SC46.name in CalcInstruction(x * 2, dest)._fault_writes


def _error_then(later) -> tuple[Program, Bool]:
    x = Real("X", default=-4.0)
    dest, other = Real("Dest"), Real("Other")
    seen = Bool("Seen")
    with Program(strict=False) as program:
        with Rung():
            math(sqrt(x), dest)
        with Rung(SC46):
            out(seen)
        later(other)
    return program, seen


def _plain_math(other):
    with Rung():
        math(other + 1, other)


def _math_on_a_false_rung(other):
    with Rung(Bool("Never")):
        math(other + 1, other)


def _a_copy(other):
    with Rung():
        copy(1, other)


@pytest.mark.parametrize(
    ("later", "left"),
    [
        pytest.param(_plain_math, False, id="a_later_math_clears_it"),
        pytest.param(_math_on_a_false_rung, False, id="a_math_on_a_false_rung_clears_it"),
        pytest.param(_a_copy, True, id="a_copy_leaves_it"),
    ],
)
def test_what_clears_sc46(runner_factory, later, left):
    """CLICK: ``flag_reads/math_after_sqrt``, ``flag_steps``."""
    program, seen = _error_then(later)
    plc = runner_factory(program)
    plc.step()
    tags = plc.current_state.tags
    assert tags["Seen"] is True
    assert tags[SC46.name] is left


def test_sc46_carries_into_the_next_scan(runner_factory):
    """CLICK: ``flag_carry``.  The first rung of the next scan reads it on."""
    x = Real("X", default=-4.0)
    dest = Real("Dest")
    seen_at_top = Bool("SeenAtTop")
    with Program(strict=False) as program:
        with Rung(SC46):
            out(seen_at_top)
        with Rung():
            math(sqrt(x), dest)
    plc = runner_factory(program)
    plc.step()
    assert plc.current_state.tags["SeenAtTop"] is False
    plc.step()
    assert plc.current_state.tags["SeenAtTop"] is True


def test_pdg_sees_the_calc_write_sc46():
    x, dest, seen = Real("X"), Real("Dest"), Bool("Seen")
    with Program(strict=False) as program:
        with Rung():
            math(sqrt(x), dest)
        with Rung(SC46):
            out(seen)
    graph = build_program_graph(program)
    assert graph.writers_of[SC46.name] == frozenset({0})
