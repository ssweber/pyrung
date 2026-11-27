"""A Math error belongs to its own operation. That operation is 0 and the formula carries on.

Read on a CLICK (2026-11-06, conformance ``math_terms``): ``7 / 0 + 5`` and
``(7 / 0 + 1) * 5`` are 5 with SC40, ``SQRT(-4.0) + 5`` is 5 with SC46, into a DF
too (5.0).  ``0 ^ 0``, MOD by 0 and ABS of a type's bottom follow the same model
(2026-11-10, ``term_zero``).  ``0 ^ 0`` sets no flag, and MOD by 0 sets SC46
(2026-11-26).  ``0 ^ -1`` is +Inf.  Every engine must agree: the interpreted runtime,
the compiled kernel (legacy and blockless) and the generated CircuitPython code.  The
prover must see the rest of the formula live.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from functools import reduce
from operator import mul
from pathlib import Path
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
    TagType,
    copy,
    latch,
    lro,
    lsh,
    math,
    out,
    rro,
    rsh,
    rung,
    system,
)
from pyrung.core.expression import sqrt
from tests.circuitpy.test_codegen import _context_for_program, _run_single_scan_source
from tests.core.kernel_step import step_compiled_kernel

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
    """Return the product of 34 factors of 2 ** 31 - 1.

    As the product grows, it becomes a double past 2 ** 53 and then Inf past 2 ** 1024.
    """
    return reduce(mul, [DINT_TOP] * 34)


#: Each formula, its destination type, what it stores, and the flags it raises.
FORMULAS: dict[str, tuple[Callable[[], Any], type, Any, set[int]]] = {
    "div_plus": (lambda: SEVEN / ZERO + 5, Dint, 5, {40}),
    "div_inside": (lambda: (SEVEN / ZERO + 1) * 5, Dint, 5, {40}),
    "div_into_real": (lambda: SEVEN / ZERO + 5, Real, 5.0, {40}),
    "floordiv_plus": (lambda: SEVEN // ZERO + 5, Dint, 5, {40}),
    "div_by_formula": (lambda: SEVEN / (ZERO * 2) + 5, Dint, 5, {40}),
    "floordiv_by_formula": (lambda: SEVEN // (ZERO + ZERO) + 5, Dint, 5, {40}),
    "constant_cut_to_zero": (lambda: SEVEN / 4294967296 + 5, Dint, 5, {40}),
    "root_plus": (lambda: sqrt(MINUS) + 5, Int, 5, {46}),
    "root_into_real": (lambda: sqrt(MINUS) + 5, Real, 5.0, {46}),
    "root_over_zero": (lambda: sqrt(MINUS) / ZERO + 3, Int, 3, {40, 46}),
    "fractional_power": (lambda: MINUS_EIGHT**0.5 + 2, Real, 2.0, {46}),
    "mod_zero": (lambda: SEVEN_INT % ZERO_INT + 5, Int, 5, {46}),
    "zero_to_zero": (lambda: ZERO**ZERO + 5, Dint, 5, set()),
    # 0 to a negative power is +Inf, not an error, so the formula is +Inf. An integer
    # register stores 0 and a Real stores +Inf. Both set SC43 (read 2026-11-10).
    "zero_to_minus_one": (lambda: ZERO**-1 + 5, Dint, 0, {43}),
    "zero_to_minus_one_real": (lambda: ZERO**-1 + 5, Real, float("inf"), {43}),
    "abs_int_bottom": (lambda: abs(INT_BOTTOM) + 1, Int, 1, {43}),
    "abs_dint_bottom": (lambda: abs(DINT_BOTTOM) + 1, Dint, 1, {43}),
    "negative_shift": (lambda: lsh(SEVEN_INT, NEGATIVE) + 1, Int, 1, {43}),
    "inf_into_and": (lambda: ((BIG**9) & 1) + 2, Int, 2, {43}),
    # An integer step in a Math becomes a double past 2 ** 53 and Inf past 2 ** 1024
    # (lsh(1, 2000)). A bit operation on Inf is 0 with SC43, as it is on a Real.
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
    # A finite double raised to an integer power past the float range is Inf, as it is
    # for a Real. There is no term error. An integer register stores Inf as 0 with SC43.
    "stepped_double_squared": (lambda: lsh(ONE, THOUSAND) ** 2 + 2, Int, 0, {43}),
    "stepped_double_cubed_real": (lambda: lsh(ONE, THOUSAND) ** 3 + 2, Real, float("inf"), {43}),
    "stepped_double_negative_cubed": (
        lambda: (0 - lsh(ONE, THOUSAND)) ** 3 + 2,
        Real,
        float("-inf"),
        {43},
    ),
    # A left shift of a double near the float range passes it, so the shift is Inf, as
    # an integer step past 2 ** 1024 is. A bit operation on that Inf is 0 with SC43.
    "double_shifted_past_floats": (lambda: ((DINT_TOP**32) << 60) + 1, Real, float("inf"), {43}),
    "double_lsh_past_floats": (lambda: lsh(DINT_TOP**33, 64) - DINT_TOP, Real, float("inf"), {43}),
    "double_shifted_into_and": (lambda: ((DINT_TOP**32) << 60) & 1, Real, 0.0, {43}),
    "real_lsh_past_floats": (lambda: lsh(BIG**8, 64) + 1, Real, float("inf"), {43}),
    "real_lsh_past_floats_into_and": (lambda: (lsh(BIG**8, 64) & 1) + 2, Int, 2, {43}),
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


def _program(name: str, table: dict = FORMULAS) -> tuple[Program, dict[str, Any]]:
    """Return a program and its tags: *name*'s formula into a destination, a coil per flag."""
    build, dest_type, _stored, _flags = table[name]
    dest = dest_type("Dest")
    seen = {code: Bool(f"Saw{code}") for code in FLAGS}
    with Program(strict=False) as prog:
        with rung():
            for tag, value in STAGED.items():
                copy(value, tag)
            math(build(), dest)
        for code, flag in FLAGS.items():
            with rung(flag):
                out(seen[code])
    tags: dict[str, Any] = {"Dest": dest}
    tags.update({tag.name: tag for tag in seen.values()})
    return prog, tags


def _first_scan(prog: Program, engine: str, tags: dict[str, Any], monkeypatch) -> dict[str, Any]:
    """Return the named tags after one scan of *prog* on *engine*."""
    if engine in ("interpreted", "compiled"):
        runner = (PLC if engine == "interpreted" else CompiledPLC)(prog, dt=0.010)
        runner.step()
        state = runner.current_state.tags
        return {name: state[name] for name in tags}
    if engine in ("kernel_legacy", "kernel_blockless"):
        compiled = compile_kernel(prog, blockless=engine == "kernel_blockless")
        kernel = compiled.create_kernel()
        step_compiled_kernel(compiled, kernel, dt=0.010)
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


#: A power with a large integer exponent, into a Real. The Math computes it in doubles,
#: so it overflows to Inf and sets SC43. As a Python int, 5 ** (2 ** 31 - 1) takes hours.
LARGE_POWERS: dict[str, tuple[Callable[[], Any], type, Any, set[int]]] = {
    "five_to_dint_top": (lambda: 5**DINT_TOP, Real, float("inf"), {43}),
    "minus_five_to_dint_top": (lambda: -5 * 5**DINT_TOP, Real, float("-inf"), {43}),
}


def _check_large_powers() -> None:
    """Check each of :data:`LARGE_POWERS` on every engine. A child process runs this."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        for name, (_build, _dest_type, stored, flags) in LARGE_POWERS.items():
            for engine in ENGINES:
                prog, tags = _program(name, LARGE_POWERS)
                actual = _first_scan(prog, engine, tags, monkeypatch)
                assert actual["Dest"] == stored, (name, engine)
                assert {code for code in FLAGS if actual[f"Saw{code}"]} == flags, (name, engine)


def test_a_large_power_into_a_real_finishes_on_every_engine() -> None:
    # A hang would hold the GIL, so the check runs in a child process that a timeout can kill.
    code = "from tests.core.test_math_terms import _check_large_powers; _check_large_powers()"
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr


TABLE = Block("Table", TagType.INT, 1, 10)

#: Pointers whose address is a Math's double past the integer range. The address is
#: Inf (``lsh(1, 2000)``) or NaN (Inf - Inf), each alone and after a division by 0.
#: The last is ``~`` of Inf, which is 0 with SC43, below the block.
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
    # Inf or NaN is not an address. The Math writes nothing and sets the address error.
    # It also sets the flags of the address's failing operations, as for an address
    # below the block.
    build, flags = POINTERS[name]
    dest, address = Int("Dest"), Bool("SawAddress")
    seen = {code: Bool(f"Saw{code}") for code in FLAGS}
    with Program(strict=False) as prog:
        with rung():
            for tag, value in ((ONE, 1), (FAR, 2000), (SEVEN, 7), (ZERO, 0), (dest, 99)):
                copy(value, tag)
            copy(5, TABLE[3])
            math(build(), dest)
        with rung(system.fault.address_error):
            out(address)
        for code, flag in FLAGS.items():
            with rung(flag):
                out(seen[code])
    tags: dict[str, Any] = {"Dest": dest, "SawAddress": address}
    tags.update({tag.name: tag for tag in seen.values()})
    actual = _first_scan(prog, engine, tags, monkeypatch)
    assert actual["Dest"] == 99
    assert actual["SawAddress"] is True
    assert {code for code in FLAGS if actual[f"Saw{code}"]} == flags


@pytest.mark.parametrize("engine", ENGINES)
def test_a_raw_mod_by_zero_in_a_pointer_is_its_term_with_sc40(engine: str, monkeypatch) -> None:
    # A pointer keeps Python's % (calc_form leaves it alone). A % by 0 is a division
    # error and gives 0 for the term, so Table[0 + 3] is read.
    dest = Int("Dest")
    seen = {code: Bool(f"Saw{code}") for code in FLAGS}
    with Program(strict=False) as prog:
        with rung():
            copy(7, SEVEN)
            copy(0, ZERO)
            copy(5, TABLE[3])
            math(TABLE[SEVEN % ZERO + 3], dest)
        for code, flag in FLAGS.items():
            with rung(flag):
                out(seen[code])
    tags: dict[str, Any] = {"Dest": dest}
    tags.update({tag.name: tag for tag in seen.values()})
    actual = _first_scan(prog, engine, tags, monkeypatch)
    assert actual["Dest"] == 5
    assert {code for code in FLAGS if actual[f"Saw{code}"]} == {40}


def _threshold_program(threshold: int) -> tuple[Program, Bool]:
    numerator = Int("PN", external=True, min=0, max=7, default=0)
    divisor = Int("PZ", external=True, min=0, max=1, default=0)
    dest, bad = Int("PD"), Bool("PBad")
    with Program(strict=False) as logic:
        with rung():
            math(numerator / divisor + 5, dest)
        with rung(dest < threshold):
            latch(bad)
    return logic, bad


def _latches_on_some_input(logic: Program) -> bool:
    """Step every PN and PZ that the tags allow. Return whether PBad latched."""
    plc = PLC(logic, dt=0.010)
    for numerator in range(8):
        for divisor in range(2):
            plc.patch({"PN": numerator, "PZ": divisor})
            plc.step()
    return plc.current_state.tags["PBad"]


def test_the_rest_of_a_formula_runs_past_a_division_by_zero() -> None:
    # PN / PZ + 5 is 5 when PZ is 0 (the division alone is 0), so never below 5 ...
    assert _latches_on_some_input(_threshold_program(5)[0]) is False
    # ... and 5 itself is reached.
    assert _latches_on_some_input(_threshold_program(6)[0]) is True
