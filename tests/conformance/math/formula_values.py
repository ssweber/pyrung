"""What a CLICK computes for a parsed Math formula, and the formula regrouped one pair at a time.

``tests/click/test_click_formula.py`` checks the parse against the readings of a CLICK with
these. ``test_math_order.py`` checks that each probe case tells two orders apart.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from dataclasses import replace

from pyrung.click.codegen.formula import (
    Address,
    BinaryOp,
    Call,
    Formula,
    Negate,
    Number,
    operator_kind,
)


def click_value(node: Formula, hex_mode: bool, registers: dict[str, int]) -> float:
    """Return what a CLICK computes for *node*, by the rules the readings show.

    A decimal Math computes in doubles, and its MOD keeps the dividend's sign. A hex Math reads
    each DH as unsigned and computes in whole numbers of any size. Its ``/`` drops the fraction,
    and its MOD keeps the dividend's sign. It stores the low 16 bits of the result.
    """
    value = _value(node, hex_mode, registers)
    return int(value) & 0xFFFF if hex_mode else value


def _value(node: Formula, hex_mode: bool, registers: dict[str, int]) -> float:
    if isinstance(node, Number):
        return int(node.value) if hex_mode else float(node.value)
    if isinstance(node, Address):
        value = registers[f"{node.bank}{node.index}"]
        return value & 0xFFFF if hex_mode else float(value)
    if isinstance(node, Negate):
        return -_value(node.operand, hex_mode, registers)
    if isinstance(node, Call):
        assert node.name == "SQRT", f"no model of {node.name}"
        return math.sqrt(_value(node.args[0], hex_mode, registers))
    assert isinstance(node, BinaryOp)
    a = _value(node.left, hex_mode, registers)
    b = _value(node.right, hex_mode, registers)
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
        return hex_steps[node.op]()
    decimal_steps: dict[str, Callable[[], float]] = {
        "+": lambda: a + b,
        "-": lambda: a - b,
        "*": lambda: a * b,
        "/": lambda: a / b,
        "^": lambda: a**b,
        "MOD": lambda: math.fmod(a, b),
    }
    return decimal_steps[node.op]()


def _truncated(x: int, y: int) -> int:
    quotient = abs(x) // abs(y)
    return quotient if (x < 0) == (y < 0) else -quotient


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
