"""What a CLICK computes for a parsed Math formula, and the formula regrouped one pair at a time.

``tests/click/test_click_formula.py`` checks the parse against the readings of a CLICK with
these, and finds the pairs whose groupings agree. ``test_math_order.py`` checks that each probe
case tells two orders apart.
"""

from __future__ import annotations

import math
import operator
import struct
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from typing import Any

from pyrung import PI, LiteralExpr
from pyrung.click.codegen.formula import (
    _CLICK_FUNC_TO_PYTHON,
    NEGATE_ADDRESS,
    NEGATE_BRACKET,
    Address,
    BinaryOp,
    Call,
    Formula,
    Negate,
    Number,
    Pi,
    Pointer,
    RangeCall,
    operator_kind,
)
from pyrung.core import expression

#: pyrung's operator for each of CLICK's.
_OPERATORS: dict[str, Callable[[Any, Any], Any]] = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": operator.truediv,
    "^": operator.pow,
    "MOD": operator.mod,
    "AND": operator.and_,
    "OR": operator.or_,
    "XOR": operator.xor,
}

#: The range of each integer destination.
_RANGES = {"Int": (-32768, 32767), "Dint": (-(2**31), 2**31 - 1), "Word": (0, 0xFFFF)}

#: The largest finite float32, the range of a Real.
_REAL_MAX = 3.4028234663852886e38


def float32(value: float) -> float:
    """Return *value* rounded to a float32, as a DF holds it."""
    return struct.unpack("<f", struct.pack("<f", value))[0]


#: The lowest and the highest value that a reading shows a CLICK storing in each destination.
#: The store of a value past them is unread.
_STORES_READ: dict[str, tuple[float, float]] = {
    # -1.0E10 stores 7168 (``math_more/real_past_int``). 2 ^ 40 stores 0
    # (``math_more/power_edges``).
    "Int": (-1.0e10, 2.0**40),
    # 1.0E10 stores 1410065408 (``math_more/real_past_int``). No reading stores a value below
    # a Dint's range.
    "Dint": (-(2.0**31), 1.0e10),
    # -3 stores FFFDh (b15 m1b). 10000h stores 0h (b18 d5x).
    "Word": (-3, 0x10000),
    # 3.0E+38 * 10 and * -10 store +Inf and -Inf (``numeric/real_overflow_stays_finite``).
    "Real": (-10 * float32(3.0e38), 10 * float32(3.0e38)),
}


#: The largest hex value that a reading shows a CLICK holding whole inside a formula.
#: FF38h * FE70h MOD 7h is 0005h, so the product FDA93880h stays whole before the MOD (b15 m2b).
_HEX_READ = 0xFDA93880


class Unread(Exception):
    """No reading shows what a CLICK does at this step of a formula."""


def click_value(node: Formula, hex_mode: bool, registers: dict[str, int]) -> float:
    """Return what a CLICK computes for *node*, by the rules the readings show.

    A decimal Math computes in doubles, and its MOD keeps the dividend's sign. A hex Math reads
    each DH as unsigned and computes in whole numbers of any size. Its ``/`` drops the fraction,
    and its MOD keeps the dividend's sign. Its AND, OR and XOR return 16 unsigned bits. It
    stores the low 16 bits of the result.
    """
    value = _value(node, hex_mode, registers, None)
    return int(value) & 0xFFFF if hex_mode else value


def click_steps(
    node: Formula, hex_mode: bool, registers: dict[str, int]
) -> tuple[float, frozenset[str]]:
    """Return what a CLICK computes for *node* before the store, and the fault flags it raises.

    Raise :class:`Unread` at each step that no reading shows. These steps are:

    - a hex step whose value is above FDA93880h or below -FDA93880h, the largest read (b15 m2b)
    - a hex AND, OR or XOR of a value above FFFFh or below -FFFFh
    - a hex ``/`` whose quotient has a fraction while an operand is negative, and a hex MOD by
      a negative divisor
    - a decimal step on Inf or NaN, which a DF can hold
    - a decimal minus sign before 0
    - a decimal division or MOD by zero
    - the sign of a decimal 0 that no reading shows, from a ``*`` or ``/`` other than +0.0 by
      -1.0, a sum, a MOD, a ``^``, or SQRT of 0 or of a negative value
    - a decimal step whose value passes the largest double or falls below the smallest
      normal double
    - an error from ``^``
    - every function but SQRT, and SQRT in hex mode
    - a pointer

    A decimal Math computes in doubles (``math_more/abs_and_precision``). Its ``/`` keeps the
    fraction until the store (``math_more/int_div_mid_formula``), and its MOD of two Reals
    keeps the dividend's sign (``math_more/mod_signs``).

    A row such as b17 c1 is case c1 of reading round b17. Each round ran on a C2-03CPU with
    firmware 4.00, on 2026-11-25 or 2026-11-26.
    """
    guard = _Guard(hex_mode)
    return _value(node, hex_mode, registers, guard), frozenset(guard.flags)


def click_store(value: float, flags: frozenset[str], dest: str) -> tuple[float, frozenset[str]]:
    """Return what a CLICK stores in a *dest* for the formula *value*, and the fault flags.

    *flags* are the flags of the formula's steps. *dest* is ``Int``, ``Dint``, ``Real`` or
    ``Word``. Raise :class:`Unread` for a value past the values that a reading shows a CLICK
    storing in a *dest*.
    """
    low, high = _STORES_READ[dest]
    if not low <= value <= high:
        raise Unread(f"a store of {value} into a {dest}")
    if dest == "Real":
        # A Real result past a float32 is Inf with SC43 (``numeric/real_overflow_stays_finite``).
        if abs(value) > _REAL_MAX:
            return math.copysign(math.inf, value), flags | {"out_of_range"}
        return value, flags
    # The store truncates and wraps, with SC43 (``numeric/real_to_int_truncates``,
    # ``faults/math_store_out_of_range``, ``math_more/real_past_int``, ``math_more/power_edges``,
    # ``hex_math/shifts``). A hex result of 10000h stores 0h with SC43 (b18 d5x).
    whole = int(value)
    low, high = _RANGES[dest]
    if not low <= whole <= high:
        flags |= {"out_of_range"}
    span = high - low + 1
    return (whole - low) % span + low, flags


class _Guard:
    """Collect the flags of one formula, and refuse each step that no reading shows."""

    def __init__(self, hex_mode: bool) -> None:
        self.hex_mode = hex_mode
        self.flags: set[str] = set()

    def by_zero(self, op: str, dividend: float, divisor: float) -> float:
        # A division or MOD by zero is 0, and the rest of the formula runs. The division raises
        # SC40 (b17 c1, b18 d1 and d3, ``math_terms/error_terms``, ``hex_math/rotates_and_bits``).
        # The MOD raises SC46 (b17 c2 and c3, b18 d2). b18 d1 and d2 read each in hex mode
        # inside a longer formula. No reading shows the sign of a decimal 0 of this kind, so
        # the model leaves each decimal division or MOD by zero unread. ``by_zero`` in
        # ``test_math_range.py`` asks for it.
        zero = 0
        if not self.hex_mode:
            self.unread_zero(op, dividend, divisor)
            # The rejoin rules suppose that this 0 has the dividend's sign.
            zero = math.copysign(0.0, dividend)
        self.flags.add("division_error" if op == "/" else "math_operation_error")
        return zero

    def function(self, name: str, args: list[float]) -> float:
        # SQRT(4) is 2 (b15 u2 and u4). SQRT of a negative is 0 with SC46, and the rest of the
        # formula runs (``math_terms/error_terms``). No reading shows the sign of that 0, or of
        # SQRT of 0. Each other function was read at a few inputs only (``math_more/functions``,
        # ``math_error/math_errors_do_not_stop``).
        if name != "SQRT" or self.hex_mode:
            raise Unread(f"{name} in a formula")
        (x,) = args
        if x <= 0:
            self.unread_zero("SQRT", x)
            # The rejoin rules suppose that SQRT of a negative value is +0.0, as pyrung's is.
            if x < 0:
                self.flags.add("math_operation_error")
                return 0.0
        return math.sqrt(x)

    def operands(self, *values: float) -> None:
        # A DF can hold Inf and NaN. 3.0E+38 * 10 stores Inf
        # (``numeric/real_overflow_stays_finite``), and Inf - Inf stores NaN
        # (``faults/inf_and_nan_arithmetic``). That reading ran only Inf * 2 and Inf - Inf, each
        # as the last step. No reading shows a minus sign before Inf, or any step on NaN, so the
        # model leaves each step on Inf or NaN unread.
        if not self.hex_mode and not all(math.isfinite(value) for value in values):
            raise Unread(f"a step on {values}")

    def negate(self, value: float) -> None:
        # A DF holds -0.0 (``real_bits/minus_zero_math``), but no reading shows the sign of the
        # 0 that a minus sign gives. Flipping the sign bit gives -0.0, and 0 - x gives +0.0.
        # This also leaves unread a minus sign before the 0 of a division or MOD by zero.
        if not self.hex_mode and value == 0:
            self.unread_zero("-", value)

    def result(self, op: str, a: float, b: float, value: float) -> None:
        # A CLICK does not cut an intermediate value to the destination's size. It raises SC43
        # only for the value that it stores (b17 b1 to b4, b1x, b2x and a11,
        # ``faults/real_intermediate_range``). Every value read is well inside a double. No
        # reading shows a step that overflows a double, or that falls below its smallest normal
        # value, where a CPU may flush the value to 0.
        if self.hex_mode:
            return
        if not math.isfinite(value):
            raise Unread(f"{a} {op} {b} overflows a double")
        underflow = value == 0 and op in ("*", "/", "^") and a != 0 and (op == "/" or b != 0)
        if underflow or 0 < abs(value) < sys.float_info.min:
            raise Unread(f"{a} {op} {b} falls below the smallest normal double")
        if value == 0 and not _zero_sign_is_read(op, a, b):
            self.unread_zero(op, a, b)

    def unread_zero(self, op: str, *operands: float) -> None:
        """Raise :class:`Unread` for the 0 of *op* on *operands*, whose sign no reading shows."""
        raise Unread(f"the sign of the 0 of {zero_sign_row(op, *operands)}")


def _sign_bit(value: float) -> bool:
    """Return whether *value* has its sign bit set, so that -0.0 does and 0.0 does not."""
    return math.copysign(1.0, value) < 0


def _zero_sign_is_read(op: str, a: float, b: float) -> bool:
    """Return whether a reading shows the sign of the 0 that *a* *op* *b* gives.

    The readings show a ``*`` and a ``/`` of +0.0 by -1.0, and -0.0 - -0.0. No reading shows
    the 0 of any other ``*`` or ``/``, of another sum, of a MOD or of a ``^``. ``zero_sum`` in
    ``test_math_range.py`` asks for the 0 of a sum, and of -0.0 times or divided by -5.0.
    """
    plus_zero = [x == 0 and not _sign_bit(x) for x in (a, b)]
    if op == "*":
        # 0.0 * -1.0 gives -0.0 (``real_bits/minus_zero_math``), and so does -1.0 * 0.0
        # (``real_bits/minus_zero_shapes``).
        return (plus_zero[0] and b == -1.0) or (plus_zero[1] and a == -1.0)
    if op == "/":
        # 0.0 / -1.0 gives -0.0 (``real_bits/minus_zero_shapes``).
        return plus_zero[0] and b == -1.0
    # -0.0 - -0.0 gives +0.0 (``real_bits/minus_zero_shapes``).
    return op == "-" and a == 0 and b == 0 and _sign_bit(a) and _sign_bit(b)


def zero_sign_row(op: str, *operands: float) -> str:
    """Return the row of :data:`PENDING_ZERO_SIGNS` that the 0 of *op* on *operands* falls in.

    *op* is a binary operator, ``-`` for a minus sign, or ``SQRT``.
    """
    classes = [
        ("-0.0" if _sign_bit(value) else "+0.0") if value == 0 else ("+x" if value > 0 else "-x")
        for value in operands
    ]
    if len(classes) == 2:
        return f"{classes[0]} {op} {classes[1]}"
    return f"{op} ( {classes[0]} )"


class _Recorder(_Guard):
    """A decimal guard that records each zero sign that no reading shows, instead of raising."""

    def __init__(self) -> None:
        super().__init__(hex_mode=False)
        self.rows: set[str] = set()

    def unread_zero(self, op: str, *operands: float) -> None:
        self.rows.add(zero_sign_row(op, *operands))


def unread_zero_signs(node: Formula, registers: dict[str, float]) -> set[str]:
    """Return the row of each zero sign in the decimal formula *node* that no reading shows.

    After each such 0, the model goes on as the rejoin rules of ``test_math_range.py`` suppose:

    - each step computes as a double does;
    - a minus sign flips the sign bit;
    - the 0 of a division or MOD by zero has the dividend's sign;
    - SQRT of a negative value gives +0.0.

    Any other step that no reading shows, such as a step on Inf, ends the formula there.
    """
    recorder = _Recorder()
    try:
        _value(node, False, registers, recorder)
    except Unread:
        pass
    return recorder.rows


#: The probe of a row of :data:`PENDING_ZERO_SIGNS` that no case of ``test_math_range.py`` reads.
NO_PROBE = "no probe yet"

_MINUS_SIGNS = {
    "- ( +0.0 )": "minus_sign Result2 bit 1 and ErrorCode bit 1",
    "- ( -0.0 )": NO_PROBE,
}
_SUMS = {
    "+0.0 + +0.0": "zero_sum Result1 and Result4 bit 1",
    "+x + -x": "zero_sum Result2 bit 1 (5 + -5 only)",
    "-x + +x": "zero_sum Result3 bit 2 (-5.0 + 5.0 only)",
}
_DIFFERENCES = {
    "+0.0 - +0.0": NO_PROBE,
    "+x - +x": "zero_sum Result2 bit 2 and Result3 bit 1 (5 - 5 only)",
    "-x - -x": NO_PROBE,
}
_PRODUCTS = {
    "+0.0 * +0.0": NO_PROBE,
    "+0.0 * +x": NO_PROBE,
    "+0.0 * -x": "minus_sign ErrorCode bit 4 (0 * -5 only)",
    "+x * +0.0": NO_PROBE,
    "-0.0 * +0.0": NO_PROBE,
    "-0.0 * +x": NO_PROBE,
    "-0.0 * -x": "zero_sum ErrorCode bit 1 (-0.0 * -5.0 only)",
    "-x * +0.0": NO_PROBE,
}
_PRODUCTS_BY_MINUS_ZERO = {
    "+0.0 * -0.0": NO_PROBE,
    "+x * -0.0": NO_PROBE,
    "-0.0 * -0.0": NO_PROBE,
    "-x * -0.0": NO_PROBE,
}
_QUOTIENTS = {
    "+0.0 / +x": NO_PROBE,
    "+0.0 / -x": NO_PROBE,
    "-0.0 / +x": NO_PROBE,
    "-0.0 / -x": "zero_sum ErrorCode bit 2 (-0.0 / -5.0 only)",
    "+0.0 / +0.0": "by_zero Result2 bit 1",
    "-0.0 / +0.0": "by_zero Result2 bit 2",
    "+x / +0.0": "by_zero Result1 bit 1 (5.0 / 0.0 only)",
    "-x / +0.0": "by_zero Result1 bit 2 (-5.0 / 0.0 only)",
}
_DIVISIONS_BY_MINUS_ZERO = {
    "+0.0 / -0.0": NO_PROBE,
    "+x / -0.0": NO_PROBE,
    "-0.0 / -0.0": NO_PROBE,
    "-x / -0.0": NO_PROBE,
}
_REMAINDERS = {
    "+0.0 MOD +x": "zero_sum Scratch bit 2 (0.0 MOD 5.0 only)",
    "+0.0 MOD -x": NO_PROBE,
    "+x MOD +x": "minus_sign Result3 bit 4 (5.0 MOD 5.0 only)",
    "+x MOD -x": "minus_sign Result4 bit 1 (5.0 MOD -5.0 only)",
    "-0.0 MOD +x": "zero_sum Scratch bit 1 (-0.0 MOD 5.0 only)",
    "-0.0 MOD -x": NO_PROBE,
    "-x MOD +x": "minus_sign Result3 bit 2 (-5.0 MOD 5.0 only)",
    "-x MOD -x": "minus_sign Result4 bit 2 (-5.0 MOD -5.0 only)",
    "+0.0 MOD +0.0": "by_zero Result4 bit 1",
    "-0.0 MOD +0.0": "by_zero Result4 bit 2",
    "+x MOD +0.0": "by_zero Result3 bit 1 (5.0 MOD 0.0 only)",
    "-x MOD +0.0": "by_zero Result3 bit 2 (-5.0 MOD 0.0 only)",
}
_REMAINDERS_BY_MINUS_ZERO = {
    "+0.0 MOD -0.0": NO_PROBE,
    "+x MOD -0.0": NO_PROBE,
    "-0.0 MOD -0.0": NO_PROBE,
    "-x MOD -0.0": NO_PROBE,
}
#: The stand-in SQRT ( DS4 ) at -1 meets this row.
_SQRT_OF_A_NEGATIVE = {"SQRT ( -x )": NO_PROBE}

#: The zero signs that a decimal pair meets and that no reading shows, by the list that the
#: pair would join again. A pair joins that list only when a reading shows every row of its
#: entry. ``tests/click/test_click_formula.py`` generates the rows of each pair over the grid
#: of that list, and checks that this table holds exactly those rows.
#:
#: A pair has no entry for a list that another difference keeps it out of. Reals that cancel
#: give the two orders of ``+ +`` and ``+ -`` different values, so neither pair has an entry
#: for ``SAME_VALUE_PAIRS``.
#:
#: A row is a step that gives 0, with a class for each operand:
#:
#: - ``+0.0`` and ``-0.0`` are the two zeros;
#: - ``+x`` is a value above 0, and ``-x`` is a value below 0;
#: - ``- ( +0.0 )`` is a minus sign before +0.0;
#: - ``+x / +0.0`` is a division by zero.
#:
#: The readings show +0.0 * -1.0, -1.0 * +0.0 and +0.0 / -1.0 (``real_bits/minus_zero_math``,
#: ``real_bits/minus_zero_shapes``). So in ``+0.0 * -x``, ``-x * +0.0`` and ``+0.0 / -x``, -x is
#: a value below 0 other than -1.0.
#:
#: Each row names the probe in ``test_math_range.py`` that reads it, or :data:`NO_PROBE`. A
#: probe that reads one value of ``+x`` or ``-x`` says which, such as ``(5.0 / 0.0 only)``.
#: That row is read only when a reading or a ruling shows that every value of the class gives
#: the same sign.
#:
#: The rejoin rules in ``test_math_range.py`` name what else some pairs need.
PENDING_ZERO_SIGNS: dict[str, dict[tuple[str, str], dict[str, str]]] = {
    "SAME_VALUE_ON_PLAIN_OPERANDS": {
        ("+", "+"): _SUMS,
        ("+", "-"): {**_SUMS, **_DIFFERENCES},
        (NEGATE_ADDRESS, "*"): {**_MINUS_SIGNS, **_PRODUCTS},
        (NEGATE_ADDRESS, "/"): {**_MINUS_SIGNS, **_QUOTIENTS},
        (NEGATE_ADDRESS, "MOD"): {**_MINUS_SIGNS, **_REMAINDERS},
        (NEGATE_BRACKET, "*"): {**_MINUS_SIGNS, **_PRODUCTS},
        (NEGATE_BRACKET, "/"): {**_MINUS_SIGNS, **_QUOTIENTS},
        (NEGATE_BRACKET, "MOD"): {**_MINUS_SIGNS, **_REMAINDERS},
    },
    "SAME_VALUE_PAIRS": {
        **{
            (minus_sign, op): {**_MINUS_SIGNS, **rows, **by_minus_zero, **_SQRT_OF_A_NEGATIVE}
            for minus_sign in (NEGATE_ADDRESS, NEGATE_BRACKET)
            for op, rows, by_minus_zero in (
                ("*", _PRODUCTS, _PRODUCTS_BY_MINUS_ZERO),
                ("/", _QUOTIENTS, _DIVISIONS_BY_MINUS_ZERO),
                ("MOD", _REMAINDERS, _REMAINDERS_BY_MINUS_ZERO),
            )
        },
    },
}


def _value(
    node: Formula,
    hex_mode: bool,
    registers: dict[str, int],
    guard: _Guard | None,
) -> float:
    if isinstance(node, Number):
        return int(node.value) if hex_mode else float(node.value)
    if isinstance(node, Pi):
        return math.pi
    if isinstance(node, Address):
        value = registers[f"{node.bank}{node.index}"]
        return value & 0xFFFF if hex_mode else float(value)
    if isinstance(node, Pointer):
        # A Copy through a pointer past its bank raises SC44 (``faults/pointer_past_bank``). No
        # reading shows a pointer in a Math.
        if guard is None:
            raise AssertionError("no model of a pointer")
        raise Unread("a pointer in a formula")
    if isinstance(node, RangeCall):
        if guard is None:
            raise AssertionError(f"no model of {node.name}")
        raise Unread(f"{node.name} in a formula")
    if isinstance(node, Call):
        args = [_value(arg, hex_mode, registers, guard) for arg in node.args]
        if guard is None:
            assert node.name == "SQRT", f"no model of {node.name}"
            return math.sqrt(args[0])
        guard.operands(*args)
        return guard.function(node.name, args)
    if isinstance(node, Negate):
        operand = _value(node.operand, hex_mode, registers, guard)
        if guard is not None:
            guard.operands(operand)
            guard.negate(operand)
        return -operand
    assert isinstance(node, BinaryOp)
    a = _value(node.left, hex_mode, registers, guard)
    b = _value(node.right, hex_mode, registers, guard)
    if guard is None:
        return _step(node.op, a, b, hex_mode, guard)
    guard.operands(a, b)
    if node.op in ("/", "MOD") and b == 0:
        return guard.by_zero(node.op, a, b)
    result = _step(node.op, a, b, hex_mode, guard)
    guard.result(node.op, a, b, result)
    return result


def _step(op: str, a: float, b: float, hex_mode: bool, guard: _Guard | None) -> float:
    if hex_mode:
        x, y = int(a), int(b)
        if guard is not None:
            _check_hex_step(op, x, y)
        hex_steps: dict[str, Callable[[], int]] = {
            "+": lambda: x + y,
            "-": lambda: x - y,
            "*": lambda: x * y,
            "/": lambda: _truncated(x, y),
            "MOD": lambda: x - y * _truncated(x, y),
            # AND, OR and XOR return 16 unsigned bits, so a negative operand gives no SC43
            # (b17 a8 to a11). The next step reads those bits as unsigned, so ( 0h - 1h ) OR 0h
            # is FFFFh there (b18 d5, d5x and d6).
            "AND": lambda: (x & y) & 0xFFFF,
            "OR": lambda: (x | y) & 0xFFFF,
            "XOR": lambda: (x ^ y) & 0xFFFF,
        }
        result = hex_steps[op]()
        if guard is not None and abs(result) > _HEX_READ:
            raise Unread(f"hex {x} {op} {y}")
        return result
    if op == "^" and guard is not None and not _power_is_read(a, b):
        raise Unread(f"{a} ^ {b}")
    decimal_steps: dict[str, Callable[[], float]] = {
        "+": lambda: a + b,
        "-": lambda: a - b,
        "*": lambda: a * b,
        "/": lambda: a / b,
        "^": lambda: a**b,
        "MOD": lambda: math.fmod(a, b),
    }
    return decimal_steps[op]()


def _check_hex_step(op: str, x: int, y: int) -> None:
    """Raise :class:`Unread` for a hex step whose operands no reading shows."""
    # The readings show AND, OR and XOR of -1, -2 and -3 (b17 a8 to a11, b14, b18 d5, d5x and
    # d6). The model takes any operand from -FFFFh to FFFFh by its low 16 bits, and leaves a
    # wider one unread.
    if op in ("AND", "OR", "XOR") and not (-0xFFFF <= x <= 0xFFFF and -0xFFFF <= y <= 0xFFFF):
        raise Unread(f"hex {x} {op} {y}")
    # A negative dividend was read in MOD (b17 a7, b15 m1b) and in an exact ``/`` (b17 a6).
    if op == "/" and (x < 0 or y < 0) and y != 0 and x % y != 0:
        raise Unread(f"hex {x} / {y}")
    if op == "MOD" and y < 0:
        raise Unread(f"hex {x} MOD {y}")


def _power_is_read(a: float, b: float) -> bool:
    """Return whether *a* ``^`` *b* is a finite double, which needs no reading of an error."""
    if (a == 0 and b <= 0) or (a < 0 and b != int(b)):
        return False
    try:
        return math.isfinite(a**b)
    except OverflowError:
        return False


def _truncated(x: int, y: int) -> int:
    quotient = abs(x) // abs(y)
    return quotient if (x < 0) == (y < 0) else -quotient


def pyrung_expression(node: Formula, tags: Mapping[int, Any]) -> Any:
    """Return the pyrung expression of *node*, in the order of the tree, with *tags* by number."""
    if isinstance(node, Address):
        return tags[node.index]
    if isinstance(node, Number):
        return LiteralExpr(node.value)
    if isinstance(node, Pi):
        return PI
    if isinstance(node, Negate) and isinstance(node.operand, Negate):
        # pyrung's export refuses a minus sign right after another one (CLICK's OBJ-1001). So
        # the inner one becomes 0 - x, which computes the same.
        inner = pyrung_expression(node.operand.operand, tags)
        return -(LiteralExpr(0) - inner)
    if isinstance(node, Negate):
        return -pyrung_expression(node.operand, tags)
    if isinstance(node, Call):
        name = _CLICK_FUNC_TO_PYTHON[node.name]
        function = abs if name == "abs" else getattr(expression, name)
        return function(*(pyrung_expression(arg, tags) for arg in node.args))
    assert isinstance(node, BinaryOp)
    left, right = pyrung_expression(node.left, tags), pyrung_expression(node.right, tags)
    return _OPERATORS[node.op](left, right)


def regroupings(node: Formula) -> Iterator[tuple[str, str, bool, Formula]]:
    """Yield each parent and child operator that meet in *node*, whether the child is the
    left operand, and *node* regrouped so that the parent applies first.

    A minus sign on the right of an operator has no other order, so it yields nothing.
    """
    if isinstance(node, Negate):
        for parent, child, child_is_left, operand in regroupings(node.operand):
            yield parent, child, child_is_left, replace(node, operand=operand)
        return
    if not isinstance(node, BinaryOp):
        return
    for parent, child, child_is_left, left in regroupings(node.left):
        yield parent, child, child_is_left, replace(node, left=left)
    for parent, child, child_is_left, right in regroupings(node.right):
        yield parent, child, child_is_left, replace(node, right=right)
    op, left, right = node.op, node.left, node.right
    if isinstance(left, BinaryOp) and not left.bracketed:
        yield op, left.op, True, BinaryOp(left.op, left.left, BinaryOp(op, left.right, right))
    if isinstance(left, Negate) and not left.bracketed:
        kind = operator_kind(left)
        assert kind is not None
        yield op, kind, True, Negate(BinaryOp(op, left.operand, right))
    if isinstance(right, BinaryOp) and not right.bracketed:
        yield op, right.op, False, BinaryOp(right.op, BinaryOp(op, left, right.left), right.right)
