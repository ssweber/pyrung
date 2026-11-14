"""A pointer is an Int or Dint tag; an index expression is integer-valued.

A Real, Word, Bool or Char pointer, or an index expression with any other
operand or one that may be a float (a float constant, ``/``, ``**`` or a math
function), is refused when the program is built; so is a ``select()`` bound
that breaks the same rule.
"""

from __future__ import annotations

import pytest

from pyrung.core import Block, Bool, Char, Dint, Int, Real, TagType, Word, sqrt
from pyrung.core.memory_block import IndirectBlockRange, IndirectExprRef, IndirectRef

DS = Block("DS", TagType.INT, 1, 10)


@pytest.mark.parametrize("pointer", [Int("PtrI"), Dint("PtrD")], ids=["int", "dint"])
def test_an_int_or_dint_pointer_is_accepted(pointer):
    assert isinstance(DS[pointer], IndirectRef)


@pytest.mark.parametrize(
    ("pointer", "type_name"),
    [
        (Real("PtrR"), "REAL"),
        (Word("PtrW"), "WORD"),
        (Bool("PtrB"), "BOOL"),
        (Char("PtrC"), "CHAR"),
    ],
    ids=["real", "word", "bool", "char"],
)
def test_any_other_pointer_type_is_refused(pointer, type_name):
    with pytest.raises(TypeError) as excinfo:
        DS[pointer]
    message = str(excinfo.value)
    assert f"Pointer {pointer.name!r} into block 'DS' is {type_name}" in message
    assert "Int or Dint" in message
    assert message.isascii()


@pytest.mark.parametrize(
    "make",
    [
        lambda i, d: i + 1,
        lambda i, d: i // 2,
        lambda i, d: i % 3,
        lambda i, d: d + i,
        lambda i, d: (i << 1) & 7,
        lambda i, d: -i + 5,
    ],
    ids=["add", "floordiv", "mod", "dint", "bitwise", "negate"],
)
def test_an_integer_index_expression_is_accepted(make):
    assert isinstance(DS[make(Int("I"), Dint("D"))], IndirectExprRef)


@pytest.mark.parametrize(
    "make",
    [
        lambda i, r: r + 1,
        lambda i, r: i / 2,
        lambda i, r: i * 1.5,
        lambda i, r: i**2,
        lambda i, r: sqrt(i),
        lambda i, r: Word("W") + 1,
        lambda i, r: Bool("B") + 1,
    ],
    ids=["real", "truediv", "float_constant", "pow", "math_func", "word", "bool"],
)
def test_an_index_expression_that_is_not_integer_valued_is_refused(make):
    with pytest.raises(TypeError) as excinfo:
        DS[make(Int("I"), Real("R"))]
    message = str(excinfo.value)
    assert "into block 'DS' is not integer-valued" in message
    assert "Int or Dint" in message
    assert message.isascii()


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (Int("S"), 5),
        (1, Dint("E")),
        (Int("S"), Int("S") + 2),
        (Dint("S2") // 2, 9),
    ],
    ids=["int_start", "dint_end", "expr_end", "floordiv_start"],
)
def test_a_select_bound_that_is_an_int_tag_or_index_expression_is_accepted(start, end):
    assert isinstance(DS.select(start, end), IndirectBlockRange)


@pytest.mark.parametrize(
    ("start", "end", "fragment"),
    [
        (Real("RS"), 5, "select() start bound 'RS' into block 'DS' is REAL"),
        (1, Word("WE"), "select() end bound 'WE' into block 'DS' is WORD"),
        (Int("S"), Int("S") / 2, "select() end bound 'S / 2' into block 'DS' is not integer"),
        (Word("W") + 1, 5, "select() start bound 'W + 1' into block 'DS' is not integer"),
        (2.5, Int("E2"), "select() start bound into block 'DS' is a float"),
    ],
    ids=["real_start", "word_end", "truediv_end", "word_expr_start", "float_literal"],
)
def test_any_other_select_bound_is_refused(start, end, fragment):
    with pytest.raises(TypeError) as excinfo:
        DS.select(start, end)
    message = str(excinfo.value)
    assert fragment in message
    assert message.isascii()
