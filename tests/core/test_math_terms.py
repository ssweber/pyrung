"""A Math error is its operation's alone: that operation is 0, the formula carries on.

Read on a CLICK (2026-11-06, conformance ``math_terms``): ``7 / 0 + 5`` and
``(7 / 0 + 1) * 5`` are 5 with SC40, ``SQRT(-4.0) + 5`` is 5 with SC46, into a DF
too (5.0).  The quiet errors (``0 ^ 0``, ``0 ^ -1``, MOD by 0) and ABS of a type's
bottom follow the same model, read alone only.  Every engine must agree: the
interpreted runtime, the compiled kernel (legacy and blockless) and the generated
CircuitPython code, and the prover must see the rest of the formula live.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import reduce
from operator import mul
from typing import Any

import pytest

from pyrung.circuitpy import P1AM, generate_circuitpy
from pyrung.circuitpy.codegen import compile_kernel
from pyrung.core import (
    PLC,
    Block,
    Bool,
    CompiledPLC,
    Dint,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    copy,
    latch,
    lro,
    lsh,
    math,
    out,
    rro,
    rsh,
    system,
)
from pyrung.core.analysis.prove import Counterexample, Proven, never
from pyrung.core.analysis.prove.kernel import _step_compiled_kernel
from pyrung.core.expression import sqrt
from tests.circuitpy.test_codegen import _context_for_program, _run_single_scan_source

ENGINES = [
    "interpreted",
    "compiled",
    "kernel_legacy",
    "kernel_blockless",
    "circuitpy",
    "circuitpy_rt",
]

FLAGS = {
    40: system.fault.division_error,
    43: system.fault.out_of_range,
    46: system.fault.math_operation_error,
}

SEVEN, ZERO, MINUS = Dint("Seven"), Dint("Zero"), Real("Minus")
SEVEN_INT, ZERO_INT, NEGATIVE = Int("SevenInt"), Int("ZeroInt"), Int("Negative")
INT_BOTTOM, DINT_BOTTOM = Int("IntBottom"), Dint("DintBottom")
BIG, MINUS_EIGHT = Real("Big"), Real("MinusEight")
ONE, FAR, THOUSAND = Int("One"), Int("Far"), Int("Thousand")
DINT_TOP = Dint("DintTop")


def _past_doubles() -> Any:
    """34 factors of 2 ** 31 - 1: a double past 2 ** 53, then Inf past 2 ** 1024."""
    return reduce(mul, [DINT_TOP] * 34)


#: Each formula, its destination type, what it stores, and the flags it raises.
FORMULAS: dict[str, tuple[Callable[[], Any], type, Any, set[int]]] = {
    "div_plus": (lambda: SEVEN / ZERO + 5, Dint, 5, {40}),
    "div_inside": (lambda: (SEVEN / ZERO + 1) * 5, Dint, 5, {40}),
    "div_into_real": (lambda: SEVEN / ZERO + 5, Real, 5.0, {40}),
    "floordiv_plus": (lambda: SEVEN // ZERO + 5, Dint, 5, {40}),
    "div_by_formula": (lambda: SEVEN / (ZERO * 2) + 5, Dint, 5, {40}),
    "floordiv_by_formula": (lambda: SEVEN // (ZERO + ZERO) + 5, Dint, 5, {40}),
    "constant_cut_to_zero": (lambda: SEVEN / 65536 + 5, Dint, 5, {40}),
    "root_plus": (lambda: sqrt(MINUS) + 5, Int, 5, {46}),
    "root_into_real": (lambda: sqrt(MINUS) + 5, Real, 5.0, {46}),
    "root_over_zero": (lambda: sqrt(MINUS) / ZERO + 3, Int, 3, {40, 46}),
    "fractional_power": (lambda: MINUS_EIGHT**0.5 + 2, Real, 2.0, {46}),
    "mod_zero": (lambda: SEVEN_INT % ZERO_INT + 5, Int, 5, set()),
    "zero_to_zero": (lambda: ZERO**ZERO + 5, Dint, 5, set()),
    "zero_to_minus_one": (lambda: ZERO**-1 + 5, Dint, 5, set()),
    "abs_int_bottom": (lambda: abs(INT_BOTTOM) + 1, Int, 1, {43}),
    "abs_dint_bottom": (lambda: abs(DINT_BOTTOM) + 1, Dint, 1, {43}),
    "negative_shift": (lambda: lsh(SEVEN_INT, NEGATIVE) + 1, Int, 1, {43}),
    "inf_into_and": (lambda: ((BIG**9) & 1) + 2, Int, 2, {43}),
    # A calc's integer step past 2 ** 53 is a double, Inf past 2 ** 1024
    # (lsh(1, 2000)): a bit operation on Inf is 0 with SC43, as on a Real's.
    "stepped_inf_into_and": (lambda: (lsh(ONE, FAR) & 1) + 2, Int, 2, {43}),
    "stepped_inf_into_and_real": (lambda: (lsh(ONE, FAR) & 1) + 2, Real, 2.0, {43}),
    "stepped_inf_into_or": (lambda: ((ONE << FAR) | 0) + 2, Int, 2, {43}),
    "stepped_inf_into_xor": (lambda: (lsh(ONE, FAR) ^ 1) + 2, Int, 2, {43}),
    "stepped_inf_inverted": (lambda: ~lsh(ONE, FAR) + 2, Int, 2, {43}),
    "stepped_inf_shifted_left": (lambda: lsh(lsh(ONE, FAR), 1) + 2, Int, 2, {43}),
    "stepped_inf_shifted_right": (lambda: rsh(lsh(ONE, FAR), 1) + 2, Int, 2, {43}),
    "stepped_inf_as_shift_count": (lambda: (ONE << lsh(ONE, FAR)) + 2, Int, 2, {43}),
    "stepped_inf_rotated_left": (lambda: lro(lsh(ONE, FAR), 1) + 2, Int, 2, {43}),
    "stepped_inf_rotated_right": (lambda: rro(lsh(ONE, FAR), 1) + 2, Int, 2, {43}),
    "product_past_doubles_into_and": (lambda: (_past_doubles() & 1) + 2, Int, 2, {43}),
    # A finite double to an integer power past the float range is Inf, as a
    # Real's is: no term error, Inf into an integer register is 0 with SC43.
    "stepped_double_squared": (lambda: lsh(ONE, THOUSAND) ** 2 + 2, Int, 0, {43}),
    "stepped_double_cubed_real": (lambda: lsh(ONE, THOUSAND) ** 3 + 2, Real, float("inf"), {43}),
    "stepped_double_negative_cubed": (
        lambda: (0 - lsh(ONE, THOUSAND)) ** 3 + 2,
        Real,
        float("-inf"),
        {43},
    ),
    "no_error": (lambda: SEVEN / 2 + 5, Real, 8.5, set()),
}

STAGED = {
    SEVEN: 7,
    ZERO: 0,
    MINUS: -4.0,
    SEVEN_INT: 7,
    ZERO_INT: 0,
    NEGATIVE: -1,
    INT_BOTTOM: -32768,
    DINT_BOTTOM: -2147483648,
    BIG: 1.0e38,
    MINUS_EIGHT: -8.0,
    ONE: 1,
    FAR: 2000,
    THOUSAND: 1000,
    DINT_TOP: 2147483647,
}


class _StubBase:
    def rollCall(self, modules):
        return None

    def readDiscrete(self, slot):
        return 0

    def writeDiscrete(self, value, slot):
        return None

    def readAnalog(self, slot, ch):
        return 0

    def writeAnalog(self, value, slot, ch):
        return None

    def readTemperature(self, slot, ch):
        return 0.0


def _program(name: str) -> tuple[Program, dict[str, Any]]:
    """*name*'s formula into its destination, then a coil per flag it may raise."""
    build, dest_type, _stored, _flags = FORMULAS[name]
    dest = dest_type("Dest")
    seen = {code: Bool(f"Saw{code}") for code in FLAGS}
    with Program(strict=False) as prog:
        with Rung():
            for tag, value in STAGED.items():
                copy(value, tag)
            math(build(), dest)
        for code, flag in FLAGS.items():
            with Rung(flag):
                out(seen[code])
    tags: dict[str, Any] = {"Dest": dest}
    tags.update({tag.name: tag for tag in seen.values()})
    return prog, tags


def _first_scan(prog: Program, engine: str, tags: dict[str, Any], monkeypatch) -> dict[str, Any]:
    """The named tags after one scan of *prog* on *engine*."""
    if engine in ("interpreted", "compiled"):
        runner = (PLC if engine == "interpreted" else CompiledPLC)(prog, dt=0.010)
        runner.step()
        state = runner.current_state.tags
        return {name: state[name] for name in tags}
    if engine in ("kernel_legacy", "kernel_blockless"):
        compiled = compile_kernel(prog, blockless=engine == "kernel_blockless")
        kernel = compiled.create_kernel()
        _step_compiled_kernel(compiled, kernel, dt=0.010)
        return {name: kernel.tags[name] for name in tags}
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    split = engine == "circuitpy_rt"
    result = generate_circuitpy(prog, hw, target_scan_ms=10.0, runstop=None, force_runtime=split)
    namespace = _run_single_scan_source(
        result.code, monkeypatch, _StubBase(), runtime_source=result.runtime
    )
    ctx = _context_for_program(prog, hw)
    return {name: namespace[ctx.symbol_for_tag(tag)] for name, tag in tags.items()}


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("name", list(FORMULAS))
def test_a_failing_operation_is_zero_and_the_formula_carries_on(
    name: str, engine: str, monkeypatch
) -> None:
    prog, tags = _program(name)
    actual = _first_scan(prog, engine, tags, monkeypatch)
    _build, _dest_type, stored, flags = FORMULAS[name]
    assert actual["Dest"] == stored
    assert type(actual["Dest"]) is type(stored)
    assert {code for code in FLAGS if actual[f"Saw{code}"]} == flags


TABLE = Block("Table", TagType.INT, 1, 10)

#: A pointer whose address is a calc's double past the integer range: Inf
#: (``lsh(1, 2000)``) or NaN (Inf - Inf), each alone and after a division by 0;
#: and ``~`` of Inf, 0 with SC43: below the block.
POINTERS: dict[str, tuple[Callable[[], Any], set[int]]] = {
    "shift_to_inf": (lambda: TABLE[lsh(ONE, FAR)], set()),
    "inf_minus_inf": (lambda: TABLE[lsh(ONE, FAR) - lsh(ONE, FAR) + 3], set()),
    "nan_after_div0": (lambda: TABLE[SEVEN // ZERO + lsh(ONE, FAR) - lsh(ONE, FAR)], {40}),
    "inverted_inf": (lambda: TABLE[~lsh(ONE, FAR)], {43}),
}


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("name", list(POINTERS))
def test_a_pointer_with_no_integer_address_is_an_address_error(
    name: str, engine: str, monkeypatch
) -> None:
    # Inf or NaN is no address: nothing written, the address error, and the
    # flags of the address's failing operations, as for an address below the
    # block.
    build, flags = POINTERS[name]
    dest, address = Int("Dest"), Bool("SawAddress")
    seen = {code: Bool(f"Saw{code}") for code in FLAGS}
    with Program(strict=False) as prog:
        with Rung():
            for tag, value in ((ONE, 1), (FAR, 2000), (SEVEN, 7), (ZERO, 0), (dest, 99)):
                copy(value, tag)
            copy(5, TABLE[3])
            math(build(), dest)
        with Rung(system.fault.address_error):
            out(address)
        for code, flag in FLAGS.items():
            with Rung(flag):
                out(seen[code])
    tags: dict[str, Any] = {"Dest": dest, "SawAddress": address}
    tags.update({tag.name: tag for tag in seen.values()})
    actual = _first_scan(prog, engine, tags, monkeypatch)
    assert actual["Dest"] == 99
    assert actual["SawAddress"] is True
    assert {code for code in FLAGS if actual[f"Saw{code}"]} == flags


@pytest.mark.parametrize("engine", ENGINES)
def test_a_raw_mod_by_zero_in_a_pointer_is_its_term_with_sc40(engine: str, monkeypatch) -> None:
    # A pointer keeps Python's % (calc_form leaves it): by 0 a division error,
    # 0 for the term, so Table[0 + 3] is read.
    dest = Int("Dest")
    seen = {code: Bool(f"Saw{code}") for code in FLAGS}
    with Program(strict=False) as prog:
        with Rung():
            copy(7, SEVEN)
            copy(0, ZERO)
            copy(5, TABLE[3])
            math(TABLE[SEVEN % ZERO + 3], dest)
        for code, flag in FLAGS.items():
            with Rung(flag):
                out(seen[code])
    tags: dict[str, Any] = {"Dest": dest}
    tags.update({tag.name: tag for tag in seen.values()})
    actual = _first_scan(prog, engine, tags, monkeypatch)
    assert actual["Dest"] == 5
    assert {code for code in FLAGS if actual[f"Saw{code}"]} == {40}


def _prove_program(threshold: int) -> tuple[Program, Bool]:
    numerator = Int("PN", external=True, min=0, max=7, default=0)
    divisor = Int("PZ", external=True, min=0, max=1, default=0)
    dest, bad = Int("PD"), Bool("PBad")
    with Program(strict=False) as logic:
        with Rung():
            math(numerator / divisor + 5, dest)
        with Rung(dest < threshold):
            latch(bad)
    return logic, bad


def test_the_prover_sees_the_rest_of_a_formula_live() -> None:
    # PN / PZ + 5 is 5 when PZ is 0 (the division alone is 0), so never below 5 ...
    logic, bad = _prove_program(5)
    assert isinstance(never(logic, bad), Proven)
    # ... and 5 itself is reached.
    logic, bad = _prove_program(6)
    assert isinstance(never(logic, bad), Counterexample)
