"""The parse that copy(), fill() and blockcopy() run on each operand.

Each parse function returns one result type, or raises the refusal that the operand's
declaration gives. held() returns the object that the author passed, and inner() returns
it without its immediate().
"""

from __future__ import annotations

import pytest

from pyrung import Int, Real, immediate
from pyrung.click import ClickBlocks
from pyrung.core.instruction import BlockCopyInstruction, CopyInstruction, FillInstruction
from pyrung.core.instruction.operands import (
    RANGE,
    TAG,
    VALUE,
    ConstArg,
    ExprArg,
    PointerArg,
    PointerRangeArg,
    RangeArg,
    TagArg,
    as_authored,
    held,
    inner,
    parse_value,
)
from pyrung.core.memory_block import IndirectExprRef, IndirectRef
from pyrung.core.tag import ImmediateRef

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()
Count = Int("Count")
Ptr = Int("Ptr")
Lo = Int("Lo")
Hi = Int("Hi")


def test_a_tag_parses_to_a_tag_arg():
    arg = parse_value(Count)
    assert isinstance(arg, TagArg)
    assert arg.tag is Count
    assert arg.immediate is False
    assert arg.wrapper is None
    assert held(arg) is Count
    assert inner(arg) is Count


def test_an_immediate_tag_keeps_the_authored_wrapper():
    wrapped = immediate(x[1])
    arg = parse_value(wrapped)
    assert isinstance(arg, TagArg)
    assert arg.tag is x[1]
    assert arg.immediate is True
    assert held(arg) is wrapped
    assert held(arg) is held(arg)
    assert inner(arg) is x[1]


def test_a_fixed_range_parses_to_a_range_arg():
    span = ds.select(1, 5)
    arg = parse_value(span)
    assert isinstance(arg, RangeArg)
    assert arg.span is span
    assert arg.immediate is False
    assert held(arg) is span
    assert inner(arg) is span


def test_an_immediate_range_keeps_the_authored_wrapper():
    span = x.select(1, 4)
    wrapped = immediate(span)
    arg = parse_value(wrapped)
    assert isinstance(arg, RangeArg)
    assert arg.span is span
    assert arg.immediate is True
    assert held(arg) is wrapped
    assert inner(arg) is span


@pytest.mark.parametrize(
    ("ref", "ref_type"),
    [(ds[Ptr], IndirectRef), (ds[Ptr + 1], IndirectExprRef)],
    ids=["pointer", "pointer expression"],
)
def test_a_pointer_parses_to_a_pointer_arg(ref, ref_type):
    assert isinstance(ref, ref_type)
    arg = parse_value(ref)
    assert isinstance(arg, PointerArg)
    assert arg.ref is ref
    assert arg.block is ds
    assert held(arg) is ref
    assert inner(arg) is ref


def test_a_range_with_pointer_ends_parses_to_a_pointer_range_arg():
    span = ds.select(Lo, Hi)
    arg = parse_value(span)
    assert isinstance(arg, PointerRangeArg)
    assert arg.span is span
    assert held(arg) is span
    assert inner(arg) is span


def test_an_expression_parses_to_an_expr_arg():
    expr = Count + 1
    arg = parse_value(expr)
    assert isinstance(arg, ExprArg)
    assert arg.expr is expr
    assert held(arg) is expr


@pytest.mark.parametrize("value", [5, 1.5, True, "AB"])
def test_a_constant_parses_to_a_const_arg(value):
    arg = parse_value(value)
    assert isinstance(arg, ConstArg)
    assert arg.value is value
    assert held(arg) is value
    assert inner(arg) is value


def test_an_immediate_arg_made_by_hand_gets_one_wrapper():
    arg = TagArg(x[1], True)
    assert isinstance(arg.wrapper, ImmediateRef)
    assert arg.wrapper.value is x[1]
    assert held(arg) is held(arg)


def test_a_wrapper_needs_immediate():
    with pytest.raises(ValueError, match="A wrapper needs immediate=True."):
        TagArg(x[1], False, immediate(x[1]))


def test_a_wrapper_must_wrap_the_operand():
    with pytest.raises(ValueError, match="The wrapper holds .*, but the operand is"):
        TagArg(x[1], True, immediate(x[2]))
    with pytest.raises(ValueError, match="The wrapper holds .*, but the operand is"):
        RangeArg(ds.select(1, 2), True, immediate(x.select(1, 2)))


def test_as_authored_returns_any_other_value_unchanged():
    expr = Count + 1
    for value in (Count, expr, 5, None, ds.select(1, 2)):
        assert as_authored(value) is value
    assert as_authored(parse_value(expr)) is expr


def test_tag_parse_returns_a_tag_or_a_pointer():
    assert TAG.parse("copy()", "target", Count).tag is Count
    ref = ds[Ptr]
    assert TAG.parse("copy()", "target", ref).ref is ref


def test_range_parse_returns_a_fixed_or_a_pointer_range():
    fixed = ds.select(1, 3)
    pointed = ds.select(Lo, Hi)
    assert isinstance(RANGE.parse("fill()", "dest", fixed), RangeArg)
    assert isinstance(RANGE.parse("fill()", "dest", pointed), PointerRangeArg)


def test_value_parse_returns_what_parse_value_returns():
    assert isinstance(VALUE.parse("copy()", "source", 5), ConstArg)
    assert isinstance(VALUE.parse("copy()", "source", Count), TagArg)


@pytest.mark.parametrize(
    ("operand", "call", "arg", "value"),
    [
        (TAG, "copy()", "target", [Count, Ptr]),
        (TAG, "copy()", "target", 5),
        (TAG, "copy()", "target", ds.select(1, 3)),
        (RANGE, "fill()", "dest", Count),
        (RANGE, "fill()", "dest", 5),
        (VALUE, "copy()", "source", Count > 1),
        (VALUE, "copy()", "source", None),
    ],
    ids=[
        "list as tag",
        "constant as tag",
        "range as tag",
        "tag as range",
        "constant as range",
        "condition as value",
        "None as value",
    ],
)
def test_each_parse_raises_the_refusal_of_its_declaration(operand, call, arg, value):
    refusal = operand.refusal(call, arg, value)
    assert refusal is not None
    with pytest.raises(type(refusal)) as raised:
        operand.parse(call, arg, value)
    assert str(raised.value) == str(refusal)


def test_copy_into_a_list_raises_the_tag_refusal():
    with pytest.raises(TypeError, match=r"copy\(\) got a list as target\. Pass one tag"):
        CopyInstruction(5, [Count, Ptr])


def test_the_instructions_hold_only_parsed_operands():
    wrapped = immediate(x[1])
    copy = CopyInstruction(wrapped, Count)
    assert isinstance(copy.source, TagArg)
    assert held(copy.source) is wrapped
    assert isinstance(copy.dest, TagArg)

    fill = FillInstruction(Count * 2, df.select(Lo, Hi))
    assert isinstance(fill.value, ExprArg)
    assert isinstance(fill.dest, PointerRangeArg)

    block = BlockCopyInstruction(ds.select(1, 3), dd.select(1, 3))
    assert isinstance(block.source, RangeArg)
    assert isinstance(block.dest, RangeArg)


def test_copy_into_a_real_keeps_a_real_constant():
    arg = CopyInstruction(1.5, Real("R")).source
    assert isinstance(arg, ConstArg)
    assert arg.value == 1.5
