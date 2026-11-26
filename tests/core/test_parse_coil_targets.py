"""The parse that each coil runs on its target.

out(), latch() and reset() take one tag or a range with fixed ends. toggle(), debounce(),
flasher() and timed() take one Bool tag. out(), latch(), reset() and toggle() take
immediate(). debounce(), flasher() and timed() do not.
"""

from __future__ import annotations

import pytest

from pyrung.click import ClickBlocks
from pyrung.core import Int, Program, Timer, debounce, flasher, immediate, rung, timed, toggle
from pyrung.core.instruction.operands import (
    COIL,
    RangeArg,
    TagArg,
    held,
    inner,
)
from pyrung.core.program import latch, out, reset

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()
Lo = Int("Lo")
Hi = Int("Hi")
T = Timer.clone("PT", count=5)

_COIL_WORDS = "Expected Tag, BlockRange from .select(), or immediate(...) wrapper, got {}."


def test_a_coil_tag_parses_to_a_tag_arg():
    arg = COIL.parse("out()", "target", c[1])
    assert isinstance(arg, TagArg)
    assert arg.tag is c[1]
    assert arg.immediate is False


def test_an_immediate_coil_tag_keeps_the_authored_wrapper():
    wrapped = immediate(y[1])
    arg = COIL.parse("out()", "target", wrapped)
    assert isinstance(arg, TagArg)
    assert arg.immediate is True
    assert held(arg) is wrapped
    assert inner(arg) is y[1]


def test_a_coil_range_parses_to_a_range_arg():
    span = c.select(1, 3)
    arg = COIL.parse("reset()", "target", span)
    assert isinstance(arg, RangeArg)
    assert arg.span is span
    assert arg.immediate is False


def test_an_immediate_coil_range_keeps_the_authored_wrapper():
    wrapped = immediate(y.select(1, 3))
    arg = COIL.parse("out()", "target", wrapped)
    assert isinstance(arg, RangeArg)
    assert arg.immediate is True
    assert held(arg) is wrapped


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (c.select(Lo, Hi), "IndirectBlockRange"),
        (c.select(1, Hi), "IndirectBlockRange"),
        (c[Lo], "IndirectRef"),
        (5, "int"),
        (None, "NoneType"),
    ],
)
def test_a_coil_refuses_a_pointer_and_a_value(value, shown):
    for build in (out, latch, reset):
        with pytest.raises(TypeError) as excinfo:
            with Program(strict=False):
                with rung(x[1]):
                    build(value)
        assert str(excinfo.value) == _COIL_WORDS.format(shown)


def test_a_coil_writes_any_tag_type():
    with Program(strict=False) as logic:
        with rung(x[1]):
            out(ds[1])
            latch(ds[2])
            reset(ds.select(3, 4))
    kinds = [type(i.target) for i in logic.rungs[0]._instructions]
    assert kinds == [TagArg, TagArg, RangeArg]


@pytest.mark.parametrize(
    ("call", "build"),
    [
        ("debounce()", lambda bit: debounce(bit, on=(T[1], 10), off=(T[2], 10))),
        ("flasher()", lambda bit: flasher(bit, on=(T[1], 10), off=(T[2], 10))),
        ("timed()", lambda bit: timed(bit, T[1], 10)),
    ],
)
def test_debounce_flasher_and_timed_refuse_immediate(call, build):
    with pytest.raises(TypeError) as excinfo:
        with Program(strict=False):
            with rung(x[1]):
                build(immediate(y[1]))
    assert str(excinfo.value) == (
        f"{call} takes no immediate(...) target; only out() and toggle() coils write immediately."
    )


@pytest.mark.parametrize("value", [ds[1], c.select(1, 2), c[Lo], 5])
def test_an_out_variant_refuses_anything_but_one_bool_tag(value):
    with pytest.raises(TypeError) as excinfo:
        with Program(strict=False):
            with rung(x[1]):
                toggle(value)
    assert str(excinfo.value) == f"toggle() writes one Bool tag, got {value!r}."


def test_each_coil_holds_only_its_parsed_target():
    wrapped = immediate(y[2])
    with Program(strict=False) as logic:
        with rung(x[1]):
            out(c[1])
            out(immediate(y.select(3, 4)))
            latch(c[2])
            reset(c.select(3, 5))
            toggle(wrapped)
            debounce(c[6], on=(T[1], 10), off=(T[2], 10))
            flasher(c[7], on=(T[3], 10), off=(T[4], 10))
            timed(c[8], T[5], 10)
    targets = [i.target for i in logic.rungs[0]._instructions]
    assert [type(arg) for arg in targets] == [
        TagArg,
        RangeArg,
        TagArg,
        RangeArg,
        TagArg,
        TagArg,
        TagArg,
        TagArg,
    ]
    assert held(targets[4]) is wrapped
    assert [arg.immediate for arg in targets] == [
        False,
        True,
        False,
        False,
        True,
        False,
        False,
        False,
    ]
