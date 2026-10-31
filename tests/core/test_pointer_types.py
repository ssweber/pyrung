"""A pointer is an Int or Dint tag; an index expression is integer-valued.

A Real, Word, Bool or Char pointer, or an index expression that may be a float
(a Real operand, a float constant, ``/``, ``**`` or a math function), is
refused when the program is built.
"""

from __future__ import annotations

import pytest

from pyrung.core import Block, Bool, Char, Dint, Int, Real, TagType, Word, sqrt
from pyrung.core.memory_block import IndirectExprRef, IndirectRef

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
        lambda i, w: i + 1,
        lambda i, w: i // 2,
        lambda i, w: i % 3,
        lambda i, w: w + 1,  # a Word operand is integer-valued
        lambda i, w: (i << 1) & 7,
    ],
    ids=["add", "floordiv", "mod", "word", "bitwise"],
)
def test_an_integer_index_expression_is_accepted(make):
    assert isinstance(DS[make(Int("I"), Word("W"))], IndirectExprRef)


@pytest.mark.parametrize(
    "make",
    [
        lambda i, r: r + 1,
        lambda i, r: i / 2,
        lambda i, r: i * 1.5,
        lambda i, r: i**2,
        lambda i, r: sqrt(i),
    ],
    ids=["real", "truediv", "float_constant", "pow", "math_func"],
)
def test_an_index_expression_that_may_be_float_is_refused(make):
    with pytest.raises(TypeError) as excinfo:
        DS[make(Int("I"), Real("R"))]
    message = str(excinfo.value)
    assert "into block 'DS' may be a float" in message
    assert "integer-valued" in message
    assert message.isascii()
