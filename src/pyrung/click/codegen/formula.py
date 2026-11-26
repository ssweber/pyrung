"""Parse a CLICK Math formula in the order that a CLICK computes it.

:func:`parse_click_formula` reads the formula text of a Math box, such as
``DS1 + DS2 * 3`` or ``6h OR DH1 AND 8h``, and returns a tree. Equality ignores brackets
and spelling, so ``( DS1 * 2 ) + 3`` equals ``DS1 * 2 + 3`` and ``X001`` equals ``X1``.

A CLICK does not read a formula in Python's order. :data:`BINARY_TIERS` gives its order,
and the importer brackets the Python source that it writes so that pyrung computes the
same value.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from typing import TypeAlias

from pyrung.click.codegen.constants import _OPERAND_PREFIXES

#: CLICK's binary operators by tier, tightest first. A CLICK applies the operators of one
#: tier from the left, so ``2 ^ 3 ^ 2`` is 64 and ``6 OR 3 AND 8`` is 0. A minus sign
#: before an address binds tighter than every tier, so ``- DS1 ^ 2`` is 4 with DS1 = 2.
#: The parse reads a minus sign before a bracket or a function the same way, but no CLICK
#: has been read running one. CLICK 4.00 on a C2-03CPU was read for the rest.
BINARY_TIERS: tuple[tuple[str, ...], ...] = (
    ("^",),
    ("*", "/", "MOD", "AND", "OR", "XOR"),
    ("+", "-"),
)

#: A minus sign before an address, as an operator in :data:`CONFIRMED_PAIRS`. A minus sign
#: before a constant is part of the constant (CLICK stores ``-2`` as H-2), so it is no operator.
NEGATE_ADDRESS = "a minus sign before an address"

#: A minus sign before a bracket, a function, a pointer, PI or another minus sign. No CLICK
#: reading has shown where it sits in CLICK's order.
NEGATE_GROUP = "a minus sign before a bracket or a function"

#: The pairs of operators whose order readings of a CLICK settled, with the rounds of those
#: readings. Take two operators that meet in a formula (see :func:`parse_click_formula`). A
#: reading of that formula rules out an order of the two when the formula, grouped in that
#: order, gives another value. A pair is settled once its readings rule out every order but
#: CLICK's. ``tests/click/test_click_formula.py`` checks this list against its readings.
CONFIRMED_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("^", "^", "b10"),
    ("^", "*", "b11"),
    ("^", "/", "b11"),
    ("^", "MOD", "b12"),
    ("^", "+", "b12"),
    ("^", "-", "b12"),
    ("*", "/", "b11, b12"),
    ("*", "MOD", "b10, b11"),
    ("*", "AND", "b11"),
    ("*", "+", "b10"),
    ("/", "/", "b10"),
    ("/", "OR", "b14"),
    ("/", "+", "b12"),
    ("/", "-", "b12"),
    ("MOD", "AND", "b14"),
    ("MOD", "+", "b12"),
    ("MOD", "-", "b12"),
    ("AND", "OR", "b10"),
    ("AND", "XOR", "b10, b11"),
    ("AND", "+", "b11"),
    ("OR", "XOR", "b11"),
    ("OR", "+", "b11"),
    ("XOR", "+", "b11"),
    ("+", "-", "b11"),
    ("-", "-", "b10"),
    (NEGATE_ADDRESS, "^", "b11"),
    (NEGATE_ADDRESS, "AND", "b12"),
    (NEGATE_ADDRESS, "OR", "b14"),
    (NEGATE_ADDRESS, "XOR", "b14"),
    (NEGATE_ADDRESS, "+", "b12"),
    (NEGATE_ADDRESS, "-", "b12"),
)

#: The order of these pairs never changes the value, so no reading needs to settle them.
#: They are the same one of ``+ * AND OR XOR`` twice, two minus signs, and a minus sign
#: with ``*``.
_ORDER_FREE_SAME = frozenset({"+", "*", "AND", "OR", "XOR"})
_NEGATIONS = frozenset({NEGATE_ADDRESS, NEGATE_GROUP})

# CLICK's Math functions, and the Python names that the importer writes for them.
_CLICK_FUNC_TO_PYTHON: dict[str, str] = {
    "SQRT": "sqrt",
    "SIN": "sin",
    "COS": "cos",
    "TAN": "tan",
    "ASIN": "asin",
    "ACOS": "acos",
    "ATAN": "atan",
    "RAD": "radians",
    "DEG": "degrees",
    "LOG": "log10",
    "LN": "log",
    "LSH": "lsh",
    "RSH": "rsh",
    "LRO": "lro",
    "RRO": "rro",
    "RAND": "randint",
    # ABS maps to the Python builtin, which is never imported (see _EXPR_FUNC_IMPORT_NAMES).
    "ABS": "abs",
}

# CLICK's range functions, ``SUM ( DS1 : DS10 )``, and the BlockRange method for each.
_RANGE_AGG_TO_METHOD: dict[str, str] = {
    "SUM": "sum",
    "MAX": "max",
    "MIN": "min",
    "MDN": "median",
    "AVG": "mean",
}

_BANKS = tuple(prefix for prefix, _, _ in _OPERAND_PREFIXES)
_ADDRESS_RE = re.compile(rf"({'|'.join(_BANKS)})(\d+)")

# A hex constant ends in a lowercase h, so it never reads as an address: C1h is 0xC1.
_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<hex>[0-9A-Fa-f]+h)(?![0-9A-Za-z_])
      | (?P<number>(?:\d+\.?\d*|\.\d+)(?:[Ee][+-]?\d+)?)(?![0-9A-Za-z_.])
      | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<punct>[-+*/^(),:\[\]])
    )""",
    re.VERBOSE,
)

_TIER_OF = {op: index for index, ops in enumerate(BINARY_TIERS) for op in ops}
_WORD_OPS = frozenset(op for ops in BINARY_TIERS for op in ops if op.isalpha())
_CONFIRMED = frozenset(frozenset((first, second)) for first, second, _ in CONFIRMED_PAIRS)


@dataclass(frozen=True)
class _Node:
    #: Whether the formula wrapped this node in brackets. Equality and hash ignore it.
    bracketed: bool = field(default=False, compare=False, kw_only=True)


@dataclass(frozen=True)
class Number(_Node):
    """A constant. ``text`` is its spelling in the formula, with its sign."""

    value: int | float
    text: str = field(default="", compare=False)
    hex: bool = field(default=False, compare=False)


@dataclass(frozen=True)
class Address(_Node):
    """An address such as ``DS1``. ``text`` is its spelling, so ``X001`` equals ``X1``."""

    bank: str
    index: int
    text: str = field(default="", compare=False)


@dataclass(frozen=True)
class Pointer(_Node):
    """A pointer such as ``DH[DS1]``: the *bank* address whose number *index* holds."""

    bank: str
    index: Address


@dataclass(frozen=True)
class Pi(_Node):
    """The constant ``PI``."""


@dataclass(frozen=True)
class Call(_Node):
    """A function such as ``SQRT ( DS1 )`` or ``LSH ( DH1 , 2 )``."""

    name: str
    args: tuple[Formula, ...]


@dataclass(frozen=True)
class RangeCall(_Node):
    """A range function such as ``SUM ( DS1 : DS10 )``."""

    name: str
    first: Address
    last: Address


@dataclass(frozen=True)
class Negate(_Node):
    """A minus sign before an address, a function or a bracket."""

    operand: Formula


@dataclass(frozen=True)
class BinaryOp(_Node):
    """A binary operation. *op* is CLICK's spelling: ``+ - * / ^ MOD AND OR XOR``."""

    op: str
    left: Formula
    right: Formula


Formula: TypeAlias = Number | Address | Pointer | Pi | Call | RangeCall | Negate | BinaryOp


def parse_click_formula(text: str, *, hex_mode: bool = False) -> Formula | None:
    """Return the tree of the Math formula *text*, in the order that a CLICK computes it.

    In hex mode (*hex_mode*) a bare number is hex, as CLICK's dialog reads it: ``255`` is
    0x255. Raise ValueError for text that is not a formula.

    Return None unless a CLICK reading has settled the order of every pair of operators
    that meet in the tree. Two operators meet when one is an operand of the other and no
    bracket of its own holds it. In ``DS1 - DS2 * 3``, ``-`` and ``*`` meet. In
    ``( DS1 - DS2 ) * 3`` no two operators meet. An operator inside a function's brackets
    meets none outside them. A minus sign is an operator too, of the kind
    :data:`NEGATE_ADDRESS` or :data:`NEGATE_GROUP`. A pair needs no reading when its order
    never changes the value. The rule assumes that CLICK ranks its operators, so two
    operators that meet only through a third follow from the pairs that do meet.
    """
    tree, pairs = parse_with_unconfirmed_pairs(text, hex_mode=hex_mode)
    return None if pairs else tree


def parse_with_unconfirmed_pairs(
    text: str, *, hex_mode: bool = False
) -> tuple[Formula, tuple[tuple[str, str], ...]]:
    """Return the tree of *text* in :data:`BINARY_TIERS` order, and its unconfirmed pairs.

    Each pair meets in the tree, as :func:`parse_click_formula` says, and no reading in
    :data:`CONFIRMED_PAIRS` settled it. Each pair lists its operators in the order that the
    formula writes them. Raise ValueError for text that is not a formula.
    """
    tree = _Parser(text, hex_mode=hex_mode).parse()
    pairs: dict[frozenset[str], tuple[str, str]] = {}
    for parent, child, child_is_left in _meetings(tree):
        first, second = (child, parent) if child_is_left else (parent, child)
        key = frozenset((first, second))
        if key not in _CONFIRMED and not _order_free(first, second):
            pairs.setdefault(key, (first, second))
    return tree, tuple(pairs.values())


def operator_kind(node: Formula) -> str | None:
    """Return the operator of *node* as :data:`CONFIRMED_PAIRS` names it, or None."""
    if isinstance(node, BinaryOp):
        return node.op
    if isinstance(node, Negate):
        operand = node.operand
        if isinstance(operand, Address) and not operand.bracketed:
            return NEGATE_ADDRESS
        return NEGATE_GROUP
    return None


def _meetings(node: Formula) -> Iterator[tuple[str, str, bool]]:
    """Yield each pair of operators that meet in *node*, in the order the formula writes them.

    Each item is the outer operator, the inner one, and whether the inner one is the outer
    one's left operand.
    """
    if isinstance(node, Call):
        for arg in node.args:
            yield from _meetings(arg)
        return
    parent = operator_kind(node)
    if isinstance(node, BinaryOp):
        operands = ((node.left, True), (node.right, False))
    elif isinstance(node, Negate):
        operands = ((node.operand, False),)
    else:
        return
    for operand, is_left in operands:
        child = operator_kind(operand)
        if is_left:
            yield from _meetings(operand)
        if parent is not None and child is not None and not operand.bracketed:
            yield parent, child, is_left
        if not is_left:
            yield from _meetings(operand)


def _order_free(first: str, second: str) -> bool:
    if first == second and first in _ORDER_FREE_SAME:
        return True
    pair = {first, second}
    return pair <= _NEGATIONS or (bool(pair & _NEGATIONS) and "*" in pair)


class _Parser:
    def __init__(self, text: str, *, hex_mode: bool) -> None:
        self.text = text
        self.hex_mode = hex_mode
        self.tokens = self._tokenize(text)
        self.pos = 0

    def _error(self, reason: str) -> ValueError:
        return ValueError(f"Cannot read the Math formula {self.text!r}: {reason}.")

    def _tokenize(self, text: str) -> list[tuple[str, str]]:
        tokens: list[tuple[str, str]] = []
        index = 0
        while index < len(text):
            if text[index:].strip() == "":
                break
            match = _TOKEN_RE.match(text, index)
            if match is None or match.lastgroup is None:
                rest = text[index:].lstrip()
                raise self._error(f"unexpected {rest[:1]!r}")
            tokens.append((match.lastgroup, match.group(match.lastgroup)))
            index = match.end()
        return tokens

    def _peek(self, offset: int = 0) -> tuple[str, str] | None:
        index = self.pos + offset
        return self.tokens[index] if index < len(self.tokens) else None

    def _next(self) -> tuple[str, str]:
        token = self._peek()
        if token is None:
            raise self._error("it ends too soon")
        self.pos += 1
        return token

    def _expect(self, punct: str) -> None:
        token = self._next()
        if token != ("punct", punct):
            raise self._error(f"expected {punct!r}, found {token[1]!r}")

    def _binary_op(self, tier: int) -> str | None:
        token = self._peek()
        if token is None:
            return None
        kind, value = token
        if kind == "punct" or (kind == "word" and value in _WORD_OPS):
            if _TIER_OF.get(value) == tier:
                return value
        return None

    def parse(self) -> Formula:
        tree = self._tier(len(BINARY_TIERS) - 1)
        token = self._peek()
        if token is not None:
            raise self._error(f"unexpected {token[1]!r}")
        return tree

    def _tier(self, tier: int) -> Formula:
        operand = self._tier if tier > 0 else lambda _tier: self._unary()
        node = operand(tier - 1)
        while (op := self._binary_op(tier)) is not None:
            self.pos += 1
            node = BinaryOp(op, node, operand(tier - 1))
        return node

    def _unary(self) -> Formula:
        token = self._peek()
        if token == ("punct", "+"):
            self.pos += 1
            return self._unary()
        if token == ("punct", "-"):
            self.pos += 1
            after = self._peek()
            if after is not None and after[0] in ("number", "hex"):
                # CLICK stores a minus sign before a constant in the constant (-2 is H-2).
                self.pos += 1
                return self._number(after, negative=True)
            return Negate(self._unary())
        return self._primary()

    def _number(self, token: tuple[str, str], *, negative: bool = False) -> Number:
        kind, text = token
        sign = "-" if negative else ""
        if kind == "hex":
            value: int | float = int(text[:-1], 16)
            is_hex = True
        elif self.hex_mode and text.isdigit():
            # CLICK's hex dialog reads typed digits as hex and adds the h.
            value, is_hex, text = int(text, 16), True, f"{text}h"
        else:
            value = int(text) if text.isdigit() else float(text)
            is_hex = False
        return Number(-value if negative else value, f"{sign}{text}", is_hex)

    def _address(self, word: str) -> Address:
        match = _ADDRESS_RE.fullmatch(word)
        if match is None:
            raise self._error(f"{word!r} is not an address")
        return Address(match.group(1), int(match.group(2)), word)

    def _primary(self) -> Formula:
        token = self._next()
        kind, value = token
        if kind in ("number", "hex"):
            return self._number(token)
        if token == ("punct", "("):
            inner = self._tier(len(BINARY_TIERS) - 1)
            self._expect(")")
            return replace(inner, bracketed=True)
        if kind != "word":
            raise self._error(f"unexpected {value!r}")
        if value == "PI":
            return Pi()
        following = self._peek()
        if value in _RANGE_AGG_TO_METHOD and following == ("punct", "("):
            self.pos += 1
            first = self._address(self._next()[1])
            self._expect(":")
            last = self._address(self._next()[1])
            self._expect(")")
            return RangeCall(value, first, last)
        if value in _CLICK_FUNC_TO_PYTHON and following == ("punct", "("):
            self.pos += 1
            args = [self._tier(len(BINARY_TIERS) - 1)]
            while self._peek() == ("punct", ","):
                self.pos += 1
                args.append(self._tier(len(BINARY_TIERS) - 1))
            self._expect(")")
            return Call(value, tuple(args))
        if value in _BANKS and following == ("punct", "["):
            self.pos += 1
            index = self._address(self._next()[1])
            self._expect("]")
            return Pointer(value, index)
        return self._address(value)
