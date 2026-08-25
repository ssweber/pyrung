"""Crossings — the fidelity map (the forcing function for handler quality).

Every registered crossing declares a *fidelity level* — the best it can do for a
representative target.  The test reverses a representative instruction per class
and asserts the result classifies to that level, so a handler silently regressing
(an exact crossing dropping to a candidate, or any crossing dropping to
fallthrough) fails here, and a newly registered class with no declared level
fails the completeness check.  This is the per-class companion to
``test_crossings_coverage`` (which guards the covered-or-exempt map).

Levels:
- ``exact``    — necessary-and-sufficient ``Eq``/``Mask`` (copy/pack/affine).
- ``cond``     — condition-level ``CondAttr`` (coils).
- ``prior``    — prior-scan ``Prior`` provenance (shift).
- ``partial``  — a sound superset ``Cmp``/``Quant`` (counter done, search).
- ``external`` — an input ``External`` stop (modbus receive).
- ``fallthrough`` — no sound inversion yet (drums, lossy PackText, off_delay).
"""

from __future__ import annotations

import pytest

from pyrung import Bool, Char, Dint, Int
from pyrung.core.analysis.crossings import registered_classes, reverse
from pyrung.core.analysis.crossings.external import ModbusReceiveCrossing
from pyrung.core.copy_converters import to_text, to_value
from pyrung.core.crossing import (
    AffineCmp,
    Cmp,
    CondAttr,
    CrossingContext,
    External,
    Mask,
    Prior,
    Quant,
    eq_target,
)
from pyrung.core.instruction.advanced import SearchInstruction, ShiftInstruction
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.instruction.coils import LatchInstruction, OutInstruction, ResetInstruction
from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
from pyrung.core.instruction.data_transfer import (
    BlockCopyInstruction,
    CopyInstruction,
    FillInstruction,
)
from pyrung.core.instruction.drums import EventDrumInstruction, TimeDrumInstruction
from pyrung.core.instruction.packing import (
    PackBitsInstruction,
    PackTextInstruction,
    PackWordsInstruction,
    UnpackToBitsInstruction,
    UnpackToWordsInstruction,
)
from pyrung.core.instruction.send_receive._core import ModbusReceiveInstruction
from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction
from pyrung.core.memory_block import Block
from pyrung.core.tag import TagType


def _classify(result) -> str:
    """The fidelity level a result exhibits (see module docstring)."""
    if result.fallthrough:
        return "fallthrough"
    flat = [c for branch in result.branches for c in branch]
    if any(isinstance(c, External) for c in flat):
        return "external"
    if any(isinstance(c, CondAttr) for c in flat):
        return "cond"
    if any(isinstance(c, Prior) for c in flat):
        return "prior"
    if any(isinstance(c, (Cmp, AffineCmp, Quant)) for c in flat):
        return "partial"
    return "exact" if result.exact else "partial"


def _bits():
    return Block("C", TagType.BOOL, 1, 16)


def _words():
    return Block("DS", TagType.INT, 1, 4)


def _event_drum():
    return EventDrumInstruction(
        [Bool("Y1"), Bool("Y2")],
        [Bool("E1"), Bool("E2")],
        [[1, 0], [0, 1]],
        Int("Step"),
        Bool("Cmpl"),
        Bool("Auto"),
        Bool("Rst"),
    )


def _time_drum():
    return TimeDrumInstruction(
        [Bool("Y1"), Bool("Y2")],
        [10, 20],
        "ms",
        [[1, 0], [0, 1]],
        Int("Step"),
        Int("Acc"),
        Bool("Cmpl"),
        Bool("Auto"),
        Bool("Rst"),
    )


# (instruction, target_tag, target_value, expected_level, ctx_tags) per registered
# class.  ctx_tags supplies the type table a crossing needs (calc wrap-correction
# reads source/dest types); most crossings read types off the instruction itself.
def _search():
    blk = Block("DS", TagType.INT, 1, 5)
    return SearchInstruction(blk.select(1, 3) >= 100, result=Int("R"), found=Bool("F"))


def _receive():
    return ModbusReceiveInstruction(
        target_name="peer",
        bank="DS",
        start=1,
        addresses=(1,),
        dest=Int("RemoteValue"),
        receiving=Bool("RequestActive"),
        success=Bool("RequestSucceeded"),
        error=Bool("RequestFailed"),
        exception_response=Int("RequestCode"),
    )


_CASES = [
    (CopyInstruction(Int("S"), Int("D")), "D", 7, "exact", ()),
    (FillInstruction(Int("S"), _words().select(1, 3)), "DS2", 4, "exact", ()),
    (
        BlockCopyInstruction(_words().select(1, 3), Block("DD", TagType.INT, 1, 4).select(1, 3)),
        "DD2",
        9,
        "exact",
        (),
    ),
    (CalcInstruction(Int("S") + 5, Int("D")), "D", 42, "exact", (Int("S"), Int("D"))),
    (PackBitsInstruction(_bits().select(1, 3), Int("P")), "P", 5, "exact", ()),
    (PackWordsInstruction(_words().select(1, 2), Dint("P")), "P", 0x8000, "exact", ()),
    (UnpackToBitsInstruction(Int("S"), _bits().select(1, 16)), "C6", True, "exact", ()),
    (UnpackToWordsInstruction(Dint("S"), _words().select(1, 2)), "DS1", 7, "exact", ()),
    (
        PackTextInstruction(Block("T", TagType.CHAR, 1, 4).select(1, 3), Int("P")),
        "P",
        1,
        "fallthrough",
        (),
    ),
    (OutInstruction(Bool("Y")), "Y", True, "cond", ()),
    (LatchInstruction(Bool("M")), "M", True, "cond", ()),
    (ResetInstruction(Bool("M")), "M", False, "cond", ()),
    (
        CountUpInstruction(Bool("Done"), Dint("Acc"), 10, Bool("En"), Bool("Rst")),
        "Done",
        True,
        "partial",
        (),
    ),
    (
        CountDownInstruction(Bool("Done"), Dint("Acc"), 5, Bool("Dn"), Bool("Rst")),
        "Done",
        True,
        "partial",
        (),
    ),
    (
        OnDelayInstruction(Bool("Done"), Int("Acc"), 100, Bool("En")),
        "Done",
        True,
        "fallthrough",
        (),
    ),
    (
        OffDelayInstruction(Bool("Done"), Int("Acc"), 100, Bool("En")),
        "Done",
        True,
        "fallthrough",
        (),
    ),
    (
        ShiftInstruction(_bits().select(1, 8), Bool("D"), Bool("Clk"), Bool("Rst")),
        "C3",
        True,
        "prior",
        (),
    ),
    (_event_drum(), "Y2", True, "fallthrough", ()),
    (_time_drum(), "Y2", True, "fallthrough", ()),
    (_search(), "F", True, "partial", ()),
]

#: The declared fidelity of every registered crossing.
FIDELITY = {type(instr): level for instr, _tag, _val, level, _tags in _CASES}
FIDELITY[ModbusReceiveInstruction] = "external"


def test_fidelity_map_covers_every_registered_class() -> None:
    # A newly registered class with no declared fidelity fails here.
    assert set(FIDELITY) == registered_classes()


@pytest.mark.parametrize(
    ("instr", "tag", "value", "level", "ctx_tags"),
    _CASES,
    ids=[type(instr).__name__ for instr, *_ in _CASES],
)
def test_representative_target_meets_declared_fidelity(instr, tag, value, level, ctx_tags) -> None:
    ctx = CrossingContext(tags_by_name={t.name: t for t in ctx_tags})
    result = reverse(instr, None, eq_target(tag, value), ctx)
    assert _classify(result) == level


def test_converting_block_copy_and_to_text_are_fallthrough() -> None:
    # A converting block copy and a to_text copy are lossy -> sound fallthrough.
    bc = BlockCopyInstruction(
        Block("DS", TagType.CHAR, 1, 4).select(1, 3),
        Block("DD", TagType.INT, 1, 4).select(1, 3),
        convert=to_value,
    )
    assert reverse(bc, None, eq_target("DD1", 1), CrossingContext()).fallthrough
    txt = CopyInstruction(Int("N"), Char("T"), convert=to_text())
    assert reverse(txt, None, eq_target("T", "5"), CrossingContext()).fallthrough


def test_modbus_receive_is_external() -> None:
    result = ModbusReceiveCrossing().reverse(
        _receive(), None, eq_target("RemoteValue", 1), CrossingContext()
    )
    assert _classify(result) == "external"
    assert FIDELITY[ModbusReceiveInstruction] == "external"


def test_mask_level_is_exact() -> None:
    # Guard the Mask branch of _classify (unpack partial bit is still exact).
    instr = UnpackToBitsInstruction(Int("S"), _bits().select(1, 16))
    result = reverse(instr, None, eq_target("C6", True), CrossingContext())
    assert any(isinstance(c, Mask) for b in result.branches for c in b)
    assert _classify(result) == "exact"
