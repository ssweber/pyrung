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
    Address,
    BinaryOp,
    Call,
    Formula,
    Negate,
    Number,
    Pi,
    operator_kind,
)

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
    and its MOD keeps the dividend's sign. It stores the low 16 bits of the result.
    """
    value = _value(node, hex_mode, registers, None)
    return int(value) & 0xFFFF if hex_mode else value


def click_result(
    node: Formula, hex_mode: bool, registers: dict[str, int], dest: str
) -> tuple[float, frozenset[str]]:
    """Return what a CLICK stores in a *dest* for *node*, and the fault flags it raises.

    *dest* is ``Int``, ``Dint``, ``Real`` or ``Word``. Raise :class:`Unread` at each step that
    no reading shows. These steps are:

    - an intermediate result outside the destination's range, except in a Real
      (``faults/real_intermediate_range`` read one past it, with no flag)
    - a hex division by zero inside a formula, and a hex MOD by zero
    - an error or Inf from ``^``

    The probes ``math_order/width_int``, ``width_word`` and ``hex_by_zero`` ask about the
    first two.
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
        # A decimal division or MOD by zero is 0, and the rest of the formula runs. The
        # division raises SC40 and the MOD raises none (``math_terms/error_terms``,
        # ``term_zero/quiet_terms``, ``numeric/div_by_zero``, ``math_more/math_errors``).
        # A hex Math was read dividing by zero only as the whole formula
        # (``hex_math/rotates_and_bits``).
        if self.hex_mode and (op == "MOD" or not outermost):
            raise Unread(f"hex {op} by zero")
        if op == "/":
            self.flags.add("division_error")
        return 0

    def intermediate(self, value: float) -> None:
        if not math.isfinite(value):
            raise Unread(f"intermediate {value}")
        if self.dest == "Real":
            return
        # The readings show the value of a wide intermediate, but not whether it raises SC43
        # (``numeric/intermediate_fits_32_bits``, ``numeric/word_math_unsigned``).
        low, high = _RANGES[self.dest]
        if not low <= value <= high:
            raise Unread(f"intermediate {value} outside a {self.dest}")

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
    if isinstance(node, Call):
        assert node.name == "SQRT", f"no model of {node.name}"
        result = math.sqrt(_value(node.args[0], hex_mode, registers, guard, False))
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
        hex_steps: dict[str, Callable[[], int]] = {
            "+": lambda: x + y,
            "-": lambda: x - y,
            "*": lambda: x * y,
            "/": lambda: _truncated(x, y),
            "MOD": lambda: x - y * _truncated(x, y),
            "AND": lambda: x & y,
            "OR": lambda: x | y,
            "XOR": lambda: x ^ y,
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
