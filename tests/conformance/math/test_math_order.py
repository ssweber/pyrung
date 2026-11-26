"""Check the order in which a CLICK reads a Math formula: the ``math_order`` group (part
``follow5_order``).

pyrung's exporter brackets every operation, so each case passes ``formulas=``, and the CLICK
export types each formula without brackets (``pyrung.conformance._formula``). The PLC then
reads it in its own order. In-process, the case's ``math()`` runs the order that pyrung's
importer reads from the same text.

A CLICK reads a minus sign before an address first. Then it reads ``^``, then ``*``, ``/``,
MOD, AND, OR and XOR at one level, then ``+`` and ``-``. It applies each level from the left.
A CLICK 4.00 on a C2-03CPU was read running the formulas of the confirmed cases on 2026-11-25
and 2026-11-26. Each case expects the values read then. The cases put each constant of a reading in a
register of its own, so pyrung's source keeps the operation. A decimal case writes into the
Results. A hex case writes into a DH and copies the DH into a Result, so FFFFh reads -1.

CLICK's tiers put seven pairs of operators at one level, but no PLC has run a formula that
uses both operators of such a pair. The ``probe`` cases ask about them. Each case runs a
formula and its reverse. Each expects the value
with the two operators at one level, applied from the left. The other value is the one with
the right-hand operator read first.

- ``div_mod``: ``8h / 2h MOD 3h`` gives 1 (or 4), and ``9h MOD 6h / 2h`` gives 1 (or 0).
- ``div_and``: ``8h / 2h AND 3h`` gives 0 (or 4), and ``6h AND 7h / 2h`` gives 3 (or 2).
- ``div_xor``: ``8h / 2h XOR 1h`` gives 5 (or 2), and ``1h XOR 6h / 2h`` gives 3 (or 2).
- ``mod_or``: ``7h MOD 4h OR 1h`` gives 3 (or 2), and ``4h OR 7h MOD 5h`` gives 2 (or 6).
- ``mod_xor``: ``8h MOD 5h XOR 1h`` gives 2 (or 0), and ``1h XOR 7h MOD 5h`` gives 1 (or 3).
- ``mul_or``: ``2h * 3h OR 1h`` gives 7 (or 6), and ``1h OR 2h * 3h`` gives 9 (or 7).
- ``mul_xor``: ``2h * 3h XOR 1h`` gives 7 (or 4), and ``1h XOR 2h * 3h`` gives 9 (or 7).

``test_each_probe_tells_the_two_readings_apart`` computes both values. When a probe case
passes on a CLICK, remove its pair from ``formula.UNCONFIRMED_PAIRS``.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

import operator
import re
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from pyrung.click.codegen.formula import (
    Address,
    BinaryOp,
    Formula,
    Negate,
    parse_with_unconfirmed_pairs,
)
from pyrung.conformance import Case, Group, assert_all_passed, case, run
from pyrung.core import copy, fill, math, rung
from tests.conformance.memory import dh, ds
from tests.conformance.probe_kit import finish, once

#: A constant of a formula. A minus sign written against it is part of it (``-7``).
_CONSTANT = re.compile(r"(?<![\w-])-?\b[0-9][0-9A-Fa-f]*h?\b")

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

_RESULTS = ("Result1", "Result2", "Result3", "Result4")


def _value(token: str) -> int:
    return int(token[:-1], 16) if token.endswith("h") else int(token)


def _registers(formulas: Sequence[str], prefix: str, first: int) -> tuple[list[str], dict]:
    """Return *formulas* with each constant read from a register, and each register's value."""
    addresses: dict[int, int] = {}

    def register(match: re.Match[str]) -> str:
        value = _value(match.group())
        addresses.setdefault(value, first + len(addresses))
        return f"{prefix}{addresses[value]}"

    typed = [_CONSTANT.sub(register, formula) for formula in formulas]
    return typed, {address: value for value, address in addresses.items()}


def _expression(node: Formula, bank: Any) -> Any:
    """Return the pyrung expression of *node*, in the order of the tree."""
    if isinstance(node, Address):
        return bank[node.index]
    if isinstance(node, Negate):
        return -_expression(node.operand, bank)
    assert isinstance(node, BinaryOp)
    return _OPERATORS[node.op](_expression(node.left, bank), _expression(node.right, bank))


def _order_case(
    sentence: str,
    case_id: str,
    readings: Sequence[tuple[str, int]],
    *,
    hex_mode: bool,
    first: int,
    probe: str | None = None,
) -> Case:
    """Return a case that runs each formula of *readings* and expects the value beside it.

    The case owns ten registers from *first*: DS in decimal mode and DH in hex mode. A hex
    case keeps its last registers for the results.
    """
    bank, prefix = (dh, "DH") if hex_mode else (ds, "DS")
    formulas = [formula for formula, _ in readings]
    typed, staged = _registers(formulas, prefix, first)
    dests = range(first + 10 - len(readings), first + 10)
    if len(staged) > (dests.start - first if hex_mode else 10):
        raise ValueError(f"Case {case_id} needs more than ten registers")
    trees = [parse_with_unconfirmed_pairs(text, hex_mode=hex_mode)[0] for text in typed]

    def ladder(slot: Any) -> None:
        with rung(*once(slot)):
            for tree, dest, field in zip(trees, dests, _RESULTS, strict=False):
                if hex_mode:
                    math(_expression(tree, bank), dh[dest])
                    copy(dh[dest], getattr(slot, field))
                else:
                    math(_expression(tree, bank), getattr(slot, field))
        finish(slot)

    def init(slot: Any) -> None:
        for address, value in staged.items():
            copy(value, bank[address])
        if hex_mode:
            fill(0, dh.select(dests.start, dests.stop - 1))

    return case(
        sentence,
        ladder=ladder,
        init=init,
        id=case_id,
        expect={field: value for field, (_, value) in zip(_RESULTS, readings, strict=False)},
        formulas=typed,
        probe=probe,
    )


def _decimal(case_id: str, sentence: str, first: int, *readings: tuple[str, int]) -> Case:
    return _order_case(sentence, case_id, readings, hex_mode=False, first=first)


def _hex(case_id: str, sentence: str, first: int, *readings: tuple[str, int]) -> Case:
    return _order_case(sentence, case_id, readings, hex_mode=True, first=first)


def _probe(case_id: str, pair: str, first: int, *readings: tuple[str, int]) -> Case:
    sentence = f"{pair} sit at one level and apply from the left: " + " and ".join(
        f"{formula} gives {value}" for formula, value in readings
    )
    return _order_case(
        sentence,
        case_id,
        readings,
        hex_mode=True,
        first=first,
        probe=f"whether {pair} sit at one level",
    )


#: Each probe case's formulas, with the value of each reading: the two operators at one level,
#: and the right-hand one first.
PROBES: dict[str, tuple[tuple[str, int, int], ...]] = {
    "div_mod": (("8h / 2h MOD 3h", 1, 4), ("9h MOD 6h / 2h", 1, 0)),
    "div_and": (("8h / 2h AND 3h", 0, 4), ("6h AND 7h / 2h", 3, 2)),
    "div_xor": (("8h / 2h XOR 1h", 5, 2), ("1h XOR 6h / 2h", 3, 2)),
    "mod_or": (("7h MOD 4h OR 1h", 3, 2), ("4h OR 7h MOD 5h", 2, 6)),
    "mod_xor": (("8h MOD 5h XOR 1h", 2, 0), ("1h XOR 7h MOD 5h", 1, 3)),
    "mul_or": (("2h * 3h OR 1h", 7, 6), ("1h OR 2h * 3h", 9, 7)),
    "mul_xor": (("2h * 3h XOR 1h", 7, 4), ("1h XOR 2h * 3h", 9, 7)),
}

_PAIR_WORDS = {
    "div_mod": "/ and MOD",
    "div_and": "/ and AND",
    "div_xor": "/ and XOR",
    "mod_or": "MOD and OR",
    "mod_xor": "MOD and XOR",
    "mul_or": "* and OR",
    "mul_xor": "* and XOR",
}

group = Group(
    "math_order",
    [
        _decimal(
            "dec_tiers",
            "2 + 3 * 4 is 14, 7 MOD 3 * 2 is 2, 2 ^ 3 ^ 2 is 64 and 10 - 4 - 3 is 3",
            4101,
            ("2 + 3 * 4", 14),
            ("7 MOD 3 * 2", 2),
            ("2 ^ 3 ^ 2", 64),
            ("10 - 4 - 3", 3),
        ),
        _decimal(
            "dec_power",
            "100 / 10 / 5 is 2, (-2) ^ 2 is 4, 2 * 3 ^ 2 is 18 and 3 ^ 2 * 2 is 18",
            4111,
            ("100 / 10 / 5", 2),
            ("-2 ^ 2", 4),
            ("2 * 3 ^ 2", 18),
            ("3 ^ 2 * 2", 18),
        ),
        _decimal(
            "dec_minus_power",
            "A minus sign binds tighter than ^: - DS ^ 2 is 4 for 2 and for -2, and - DS ^ 3 is "
            "-8 for 2. 3 * 7 MOD 4 is 1",
            4121,
            ("- 2 ^ 2", 4),
            ("- -2 ^ 2", 4),
            ("- 2 ^ 3", -8),
            ("3 * 7 MOD 4", 1),
        ),
        _decimal(
            "dec_div_level",
            "12 / 2 * 3 is 18, 18 / 3 ^ 2 is 2, 5 - 3 + 1 is 3 and 12 / 4 * 3 is 9",
            4131,
            ("12 / 2 * 3", 18),
            ("18 / 3 ^ 2", 2),
            ("5 - 3 + 1", 3),
            ("12 / 4 * 3", 9),
        ),
        _decimal(
            "dec_add_after",
            "+ binds looser than / and MOD: 1 + 6 / 2, 6 / 2 + 1, 1 + 7 MOD 4 and 7 MOD 4 + 1 "
            "are each 4",
            4141,
            ("1 + 6 / 2", 4),
            ("6 / 2 + 1", 4),
            ("1 + 7 MOD 4", 4),
            ("7 MOD 4 + 1", 4),
        ),
        _decimal(
            "dec_sub_after",
            "- binds looser than / and MOD: 10 - 7 MOD 4 is 7, 7 MOD 4 - 1 is 2, 10 - 6 / 2 is 7 "
            "and 6 / 2 - 1 is 2",
            4151,
            ("10 - 7 MOD 4", 7),
            ("7 MOD 4 - 1", 2),
            ("10 - 6 / 2", 7),
            ("6 / 2 - 1", 2),
        ),
        _decimal(
            "dec_power_mod",
            "^ binds tighter than MOD, + and -: 2 ^ 3 MOD 3 is 2, 7 MOD 3 ^ 2 is 7, 1 + 2 ^ 2 is "
            "5 and 10 - 2 ^ 2 is 6",
            4161,
            ("2 ^ 3 MOD 3", 2),
            ("7 MOD 3 ^ 2", 7),
            ("1 + 2 ^ 2", 5),
            ("10 - 2 ^ 2", 6),
        ),
        _decimal(
            "dec_minus_mul",
            "2 ^ 2 + 1 is 5 and 2 ^ 2 - 1 is 3. With DS = 2, - DS * 3 is -6 and - DS MOD 3 is -2",
            4171,
            ("2 ^ 2 + 1", 5),
            ("2 ^ 2 - 1", 3),
            ("- 2 * 3", -6),
            ("- 2 MOD 3", -2),
        ),
        _decimal(
            "dec_minus_add",
            "MOD keeps the sign of the dividend: -7 MOD 3 is -1. With DS = 2, - DS + 5 is 3 and "
            "- DS - 1 is -3",
            4181,
            ("-7 MOD 3", -1),
            ("- 2 + 5", 3),
            ("- 2 - 1", -3),
        ),
        _hex(
            "hex_tiers",
            "2h + 3h * 4h is Eh, 7h MOD 3h * 2h is 2h, 6h AND 3h OR 8h is Ah and 6h OR 3h AND 8h "
            "is 0",
            201,
            ("2h + 3h * 4h", 0xE),
            ("7h MOD 3h * 2h", 2),
            ("6h AND 3h OR 8h", 0xA),
            ("6h OR 3h AND 8h", 0),
        ),
        _hex(
            "hex_level",
            "5h XOR 3h AND 1h is 0, 10h - 4h - 3h is 9h, 100h / 10h / 5h is 3h and 4h AND 4h XOR "
            "1h is 5h",
            211,
            ("5h XOR 3h AND 1h", 0),
            ("10h - 4h - 3h", 9),
            ("100h / 10h / 5h", 3),
            ("4h AND 4h XOR 1h", 5),
        ),
        _hex(
            "hex_or_xor",
            "1h OR 1h XOR 1h is 0, 1h XOR 1h OR 1h is 1h, 2h AND 3h + 1h is 3h and 1h + 2h AND 2h "
            "is 3h",
            221,
            ("1h OR 1h XOR 1h", 0),
            ("1h XOR 1h OR 1h", 1),
            ("2h AND 3h + 1h", 3),
            ("1h + 2h AND 2h", 3),
        ),
        _hex(
            "hex_and_mul",
            "3h AND 3h * 2h is 6h, 2h * 3h AND 3h is 2h, 1h + 2h OR 1h is 4h and 1h + 3h XOR 1h "
            "is 3h",
            231,
            ("3h AND 3h * 2h", 6),
            ("2h * 3h AND 3h", 2),
            ("1h + 2h OR 1h", 4),
            ("1h + 3h XOR 1h", 3),
        ),
        _hex(
            "hex_minus_and",
            "2h * 3h / 4h is 1h. A minus sign binds tighter than AND: - DH AND 3h is 2h for 2h "
            "and 3h for 1h",
            241,
            ("2h * 3h / 4h", 1),
            ("- 2h AND 3h", 2),
            ("- 1h AND 3h", 3),
        ),
        _hex(
            "hex_minus_or",
            "A minus sign binds tighter than OR and XOR: - DH OR 1h is FFFFh for 2h, and - DH XOR "
            "1h is FFFFh for 2h and FFFCh for 3h. 7h MOD 5h AND 3h is 2h",
            251,
            ("- 2h OR 1h", -1),
            ("- 2h XOR 1h", -1),
            ("- 3h XOR 1h", -4),
            ("7h MOD 5h AND 3h", 2),
        ),
        _hex(
            "hex_mod_and_div_or",
            "MOD and AND sit at one level, and so do / and OR: 3h AND 7h MOD 5h, 6h / 2h OR 1h "
            "and 4h OR 6h / 2h are each 3h",
            261,
            ("3h AND 7h MOD 5h", 3),
            ("6h / 2h OR 1h", 3),
            ("4h OR 6h / 2h", 3),
        ),
        *(
            _probe(
                case_id,
                _PAIR_WORDS[case_id],
                271 + 10 * index,
                *((formula, level) for formula, level, _ in readings),
            )
            for index, (case_id, readings) in enumerate(PROBES.items())
        ),
    ],
)


def _click_hex(node: Formula, registers: dict[int, int]) -> int:
    """Return what a CLICK computes for *node* in hex mode, with each step cut to 16 bits."""
    if isinstance(node, Address):
        return registers[node.index]
    if isinstance(node, Negate):
        return -_click_hex(node.operand, registers) & 0xFFFF
    assert isinstance(node, BinaryOp)
    left, right = _click_hex(node.left, registers), _click_hex(node.right, registers)
    steps = {
        "+": left + right,
        "-": left - right,
        "*": left * right,
        "/": left // right if right else 0,
        "MOD": left % right if right else 0,
        "AND": left & right,
        "OR": left | right,
        "XOR": left ^ right,
    }
    return steps[node.op] & 0xFFFF


@pytest.mark.parametrize("case_id", PROBES)
def test_each_probe_tells_the_two_readings_apart(case_id: str) -> None:
    for formula, level, right_first in PROBES[case_id]:
        typed, staged = _registers([formula], "DH", 1)
        tree = parse_with_unconfirmed_pairs(typed[0], hex_mode=True)[0]
        assert isinstance(tree, BinaryOp)
        assert isinstance(tree.left, BinaryOp)
        regrouped = BinaryOp(
            tree.left.op, tree.left.left, BinaryOp(tree.op, tree.left.right, tree.right)
        )
        assert _click_hex(tree, staged) == level
        assert _click_hex(regrouped, staged) == right_first
        assert level != right_first


def test_math_order():
    assert_all_passed(run(group))
