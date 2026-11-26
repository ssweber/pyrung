"""What a CLICK computes for a parsed Math formula, and the formula regrouped one pair at a time.

``tests/click/test_click_formula.py`` checks the parse against the readings of a CLICK with
these, and finds the pairs whose groupings agree. ``test_math_order.py`` checks that each probe
case tells two orders apart.
"""

from __future__ import annotations

import math
import operator
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from typing import Any

from pyrung import PI, LiteralExpr
from pyrung.click.codegen.formula import (
    _CLICK_FUNC_TO_PYTHON,
    Address,
    BinaryOp,
    Call,
    Formula,
    Negate,
    Number,
    Pi,
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


def click_result(
    node: Formula, hex_mode: bool, registers: dict[str, int], dest: str
) -> tuple[float, frozenset[str]]:
    """Return what a CLICK stores in a *dest* for *node*, and the fault flags it raises.

    *dest* is ``Int``, ``Dint``, ``Real`` or ``Word``. Raise :class:`Unread` at each step that
    no reading shows. These steps are:

    - a hex division or MOD by zero inside a longer formula (the probe
      ``math_order/hex_by_zero`` asks about it)
    - a hex AND, OR or XOR of a value above FFFFh or below -FFFFh
    - a hex ``/`` whose quotient has a fraction while an operand is negative, and a hex MOD by
      a negative divisor
    - an error or Inf from ``^``
    - every function but SQRT, and SQRT in hex mode

    A row such as b17 c1 is case c1 of reading round b17. A CLICK 4.00 on a C2-03CPU was read
    running it on 2026-11-26.
    """
    guard = _Guard(hex_mode, dest)
    return guard.store(_value(node, hex_mode, registers, guard))


class _Guard:
    """Collect the flags of one formula, and refuse each step that no reading shows."""

    def __init__(self, hex_mode: bool, dest: str) -> None:
        self.hex_mode = hex_mode
        self.dest = dest
        self.flags: set[str] = set()

    def by_zero(self, op: str, outermost: bool) -> float:
        # A division or MOD by zero is 0, and the rest of the formula runs. The division raises
        # SC40 (b17 c1, ``math_terms/error_terms``, ``hex_math/rotates_and_bits``). The MOD
        # raises SC46 (b17 c2 and c3). A hex division or MOD by zero was read only as the
        # whole formula.
        if self.hex_mode and not outermost:
            raise Unread(f"hex {op} by zero inside a formula")
        self.flags.add("division_error" if op == "/" else "math_operation_error")
        return 0

    def function(self, name: str, args: list[float]) -> float:
        # SQRT(4) is 2 (b15 u2 and u4). SQRT of a negative is 0 with SC46, and the rest of the
        # formula runs (``math_terms/error_terms``). Each other function was read at a few
        # inputs only (``math_more/functions``, ``math_error/math_errors_do_not_stop``).
        if name != "SQRT" or self.hex_mode:
            raise Unread(f"{name} in a formula")
        (x,) = args
        if x < 0:
            self.flags.add("math_operation_error")
            return 0
        return math.sqrt(x)

    def intermediate(self, value: float) -> None:
        # A CLICK keeps each intermediate whole and checks only the value that it stores. No
        # intermediate raises SC43 (b17 b1 to b4, b1x, b2x and a11,
        # ``faults/real_intermediate_range``).
        if not math.isfinite(value):
            raise Unread(f"intermediate {value}")

    def store(self, value: float) -> tuple[float, frozenset[str]]:
        if not math.isfinite(value):
            raise Unread(f"result {value}")
        if self.dest == "Real":
            # A Real result past a float32 is Inf with SC43 (``numeric/real_overflow_stays_finite``).
            if abs(value) > _REAL_MAX:
                self.flags.add("out_of_range")
                value = math.copysign(math.inf, value)
            return value, frozenset(self.flags)
        # The store truncates and wraps, with SC43 (``numeric/real_to_int_truncates``,
        # ``faults/math_store_out_of_range``, ``hex_math/shifts``).
        whole = int(value)
        low, high = _RANGES[self.dest]
        if not low <= whole <= high:
            self.flags.add("out_of_range")
        span = high - low + 1
        return (whole - low) % span + low, frozenset(self.flags)


def _value(
    node: Formula,
    hex_mode: bool,
    registers: dict[str, int],
    guard: _Guard | None,
    outermost: bool = True,
) -> float:
    if isinstance(node, Number):
        return int(node.value) if hex_mode else float(node.value)
    if isinstance(node, Pi):
        return math.pi
    if isinstance(node, Address):
        value = registers[f"{node.bank}{node.index}"]
        return value & 0xFFFF if hex_mode else float(value)
    if isinstance(node, RangeCall):
        if guard is None:
            raise AssertionError(f"no model of {node.name}")
        raise Unread(f"{node.name} in a formula")
    if isinstance(node, Call):
        args = [_value(arg, hex_mode, registers, guard, False) for arg in node.args]
        if guard is not None:
            result = guard.function(node.name, args)
        else:
            assert node.name == "SQRT", f"no model of {node.name}"
            result = math.sqrt(args[0])
    elif isinstance(node, Negate):
        result = -_value(node.operand, hex_mode, registers, guard, False)
    else:
        assert isinstance(node, BinaryOp)
        a = _value(node.left, hex_mode, registers, guard, False)
        b = _value(node.right, hex_mode, registers, guard, False)
        if guard is not None and node.op in ("/", "MOD") and b == 0:
            return guard.by_zero(node.op, outermost)
        result = _step(node.op, a, b, hex_mode, guard)
    if guard is not None and not outermost:
        guard.intermediate(result)
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
            # (b17 a8 to a11).
            "AND": lambda: (x & y) & 0xFFFF,
            "OR": lambda: (x | y) & 0xFFFF,
            "XOR": lambda: (x ^ y) & 0xFFFF,
        }
        return hex_steps[op]()
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
    # The readings show AND, OR and XOR of -2 and -3 (b17 a8 to a11, b14). The model takes any
    # operand from -FFFFh to FFFFh by its low 16 bits, and leaves a wider one unread.
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
