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
#: before an address binds tighter than every tier, so ``- DS1 ^ 2`` is 4 with DS1 = 2. A
#: minus sign before a bracket binds tighter than ``^``, ``+`` and AND. A minus sign before a
#: function binds tighter than ``^`` and ``+``. The parse reads a minus sign tightest against
#: the other operators too. CLICK 4.00 on a C2-03CPU was read for this order.
BINARY_TIERS: tuple[tuple[str, ...], ...] = (
    ("^",),
    ("*", "/", "MOD", "AND", "OR", "XOR"),
    ("+", "-"),
)

#: A minus sign before an address, as an operator in :data:`CONFIRMED_PAIRS`. A minus sign
#: before a constant is part of the constant (CLICK stores ``-2`` as H-2), so it is no operator.
NEGATE_ADDRESS = "a minus sign before an address"

#: A minus sign before a bracket, and before a function, as operators in
#: :data:`CONFIRMED_PAIRS`. They are two operators, because a function can raise an error of
#: its own.
NEGATE_BRACKET = "a minus sign before a bracket"
NEGATE_FUNCTION = "a minus sign before a function"

#: A minus sign before a pointer, before PI and before another minus sign. Each is an operator
#: of its own, because no reading has run one yet.
NEGATE_POINTER = "a minus sign before a pointer"
NEGATE_PI = "a minus sign before PI"
NEGATE_MINUS = "a minus sign before another minus sign"

#: Each pair of operators whose order a reading of a CLICK settled, with the rounds of those
#: readings. Two operators meet when one is an operand of the other, and the operand has no
#: brackets of its own. Only two operators that meet have one other grouping to test, so a
#: reading settles nothing about the rest. A reading rules out an order of two operators that
#: meet when the formula, grouped in that order, gives another value. A pair is settled once
#: its readings rule out every order but CLICK's. ``tests/click/test_click_formula.py``
#: checks this list against its readings.
CONFIRMED_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("^", "^", "b10"),
    ("^", "*", "b11"),
    ("^", "/", "b11"),
    ("^", "MOD", "b12"),
    ("^", "+", "b12"),
    ("^", "-", "b12"),
    ("*", "/", "b11, b12, b15"),
    ("*", "MOD", "b10, b11, b15"),
    ("*", "AND", "b11"),
    ("*", "+", "b10"),
    ("*", "-", "b15"),
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
    ("AND", "-", "b15"),
    ("OR", "XOR", "b11"),
    ("OR", "+", "b11"),
    ("OR", "-", "b15"),
    ("XOR", "+", "b11"),
    ("XOR", "-", "b15"),
    ("-", "-", "b10"),
    (NEGATE_ADDRESS, "^", "b11"),
    (NEGATE_ADDRESS, "AND", "b12"),
    (NEGATE_ADDRESS, "OR", "b14"),
    (NEGATE_ADDRESS, "XOR", "b14"),
    (NEGATE_ADDRESS, "+", "b12"),
    (NEGATE_ADDRESS, "-", "b12"),
    (NEGATE_BRACKET, "^", "b15"),
    (NEGATE_BRACKET, "AND", "b15"),
    (NEGATE_BRACKET, "+", "b15"),
    (NEGATE_FUNCTION, "^", "b15"),
    (NEGATE_FUNCTION, "+", "b15"),
)

#: Formulas that a CLICK was read running, as (text, hex mode), whose pairs of operators the
#: readings have not all settled. A formula with the same tree is confirmed, because a CLICK
#: was seen computing it. ``tests/click/test_click_formula.py`` checks this list against its
#: readings.
READ_FORMULAS: tuple[tuple[str, bool], ...] = (("5 - 3 + 1", False),)

#: Pairs of operators whose two groupings give the same value and the same fault flags on a
#: CLICK, as (first operator, second operator, hex mode) in the order that the formula writes
#: them. Sam ruled on 2026-11-26 that such a pair needs no reading.
#: ``tests/click/test_click_formula.py`` computes both groupings over a grid of values, with a
#: model of what the readings show a CLICK does. It checks that this list holds each pair whose
#: groupings agree there, and whose value pyrung's ``math()`` stores. A pair agrees only if a
#: reading shows each step of both groupings.
SAME_VALUE_PAIRS: tuple[tuple[str, str, bool], ...] = (
    ("+", "+", False),
    ("+", "-", False),
    ("*", "*", False),
    (NEGATE_ADDRESS, "*", False),
    (NEGATE_ADDRESS, "/", False),
    (NEGATE_ADDRESS, "MOD", False),
    (NEGATE_BRACKET, "*", False),
    (NEGATE_BRACKET, "/", False),
    (NEGATE_BRACKET, "MOD", False),
    ("+", "+", True),
    ("+", "-", True),
    ("*", "*", True),
    ("AND", "AND", True),
    ("OR", "OR", True),
    ("XOR", "XOR", True),
    (NEGATE_ADDRESS, "*", True),
    (NEGATE_ADDRESS, "MOD", True),
    (NEGATE_BRACKET, "*", True),
    (NEGATE_BRACKET, "MOD", True),
)

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
_SAME_VALUE = frozenset(SAME_VALUE_PAIRS)


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
    0x255. Raise ValueError for text that is not a formula, such as ``^`` in hex mode.

    Return None unless a CLICK reading has settled the order of every pair of operators in
    each bracket scope. A scope is the whole formula outside brackets, the inside of one
    bracket, or one argument of a function. So ``DS1 - DS2 + DS3 * DS4`` needs the pairs
    ``- +``, ``- *`` and ``+ *``, and ``( DS1 - DS2 ) * 3`` needs none. A minus sign is an
    operator too, of the kind :data:`NEGATE_ADDRESS`, :data:`NEGATE_BRACKET`,
    :data:`NEGATE_FUNCTION`, :data:`NEGATE_POINTER`, :data:`NEGATE_PI` or
    :data:`NEGATE_MINUS`.

    A minus sign applies before every operator to its left in its scope, so it pairs only with
    the operators to its right. A pair in :data:`SAME_VALUE_PAIRS` needs no reading. A formula
    with the tree of one in :data:`READ_FORMULAS` is confirmed.
    """
    tree, pairs = parse_with_unconfirmed_pairs(text, hex_mode=hex_mode)
    return None if pairs else tree


def parse_with_unconfirmed_pairs(
    text: str, *, hex_mode: bool = False
) -> tuple[Formula, tuple[tuple[str, str], ...]]:
    """Return the tree of *text* in :data:`BINARY_TIERS` order, and its unconfirmed pairs.

    Each pair is one that :func:`parse_click_formula` needs. No reading in
    :data:`CONFIRMED_PAIRS` settled it, and :data:`SAME_VALUE_PAIRS` does not list it. Each
    pair lists its operators in the order that the formula writes them. Raise ValueError for
    text that is not a formula.
    """
    tree = _Parser(text, hex_mode=hex_mode).parse()
    if (tree, hex_mode) in _read_trees():
        return tree, ()
    pairs: dict[frozenset[str], tuple[str, str]] = {}
    for operators in _scopes(tree):
        for index, (first, _) in enumerate(operators):
            for second, second_is_minus in operators[index + 1 :]:
                if second_is_minus:
                    continue
                if (first, second, hex_mode) in _SAME_VALUE:
                    continue
                if frozenset((first, second)) not in _CONFIRMED:
                    pairs.setdefault(frozenset((first, second)), (first, second))
    return tree, tuple(pairs.values())


def operator_kind(node: Formula) -> str | None:
    """Return the operator of *node* as :data:`CONFIRMED_PAIRS` names it, or None.

    The name also reads as words in a warning.
    """
    if isinstance(node, BinaryOp):
        return node.op
    if not isinstance(node, Negate):
        return None
    operand = node.operand
    if isinstance(operand, Address) and not operand.bracketed:
        return NEGATE_ADDRESS
    if operand.bracketed:
        return NEGATE_BRACKET
    if isinstance(operand, (Call, RangeCall)):
        return NEGATE_FUNCTION
    if isinstance(operand, Pointer):
        return NEGATE_POINTER
    if isinstance(operand, Pi):
        return NEGATE_PI
    return NEGATE_MINUS


def _scopes(tree: Formula) -> Iterator[list[tuple[str, bool]]]:
    """Yield the operators of each bracket scope of *tree*, in the order the formula writes
    them, each with whether it is a minus sign before its operand.
    """
    operators: list[tuple[str, bool]] = []
    inner: list[Formula] = []

    def walk(node: Formula, outermost: bool = False) -> None:
        if node.bracketed and not outermost:
            inner.append(node)
        elif isinstance(node, BinaryOp):
            walk(node.left)
            operators.append((node.op, False))
            walk(node.right)
        elif isinstance(node, Negate):
            words = operator_kind(node)
            assert words is not None
            operators.append((words, True))
            walk(node.operand)
        elif isinstance(node, Call):
            inner.extend(node.args)

    walk(tree, outermost=True)
    yield operators
    for node in inner:
        yield from _scopes(node)


_READ_TREES: list[tuple[Formula, bool]] = []


def _read_trees() -> list[tuple[Formula, bool]]:
    if READ_FORMULAS and not _READ_TREES:
        _READ_TREES.extend(
            (_Parser(text, hex_mode=hex_mode).parse(), hex_mode) for text, hex_mode in READ_FORMULAS
        )
    return _READ_TREES


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
                if value == "^" and self.hex_mode:
                    # CLICK's dialog refuses it with OBJ-1006 (reading rounds b17 and b18).
                    raise self._error("CLICK has no '^' in hex mode")
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
