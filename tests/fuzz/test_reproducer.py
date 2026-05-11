from __future__ import annotations

from pyrung.core import Block, Bool, Int, TagType

from .reproducer import _instr_code
from .strategies import InstrSpec


def test_instruction_code_preserves_oneshot_arguments():
    target = Bool("B0")
    source = Int("I0")
    dest = Int("I1")
    block = Block("DS", TagType.INT, 1, 3)

    assert (
        _instr_code(InstrSpec(kind="out", args={"target": target, "oneshot": True}))
        == "out(B0, oneshot=True)"
    )
    assert (
        _instr_code(
            InstrSpec(
                kind="copy",
                args={"source": source, "dest": dest, "oneshot": True},
            )
        )
        == "copy(I0, I1, oneshot=True)"
    )
    assert (
        _instr_code(
            InstrSpec(
                kind="fill",
                args={"block": block, "value": 7, "start": 1, "end": 2, "oneshot": True},
            )
        )
        == "fill(7, DS.select(1, 2), oneshot=True)"
    )


def test_instruction_code_preserves_calc_tag_tag():
    left = Int("Left")
    right = Int("Right")
    dest = Int("Dest")

    assert (
        _instr_code(
            InstrSpec(
                kind="calc_tag_tag",
                args={"source1": left, "source2": right, "op": "bitxor", "dest": dest},
            )
        )
        == "calc(Left ^ Right, Dest)"
    )
