"""Crossings Phase 2 — coverage map (the forcing function).

Every concrete instruction class is either covered by a crossing
(``EXPECTED_WITH_CROSSINGS``) or explicitly exempt (``EXEMPT``).  A new
instruction class in neither set fails ``test_every_instruction_is_covered_or_exempt``,
forcing a covered-or-fallthrough decision.
"""

from __future__ import annotations

import pytest

# Import every module that defines an Instruction subclass so the enumeration
# below is deterministic regardless of what the rest of the session imported.
import pyrung.click.nop  # noqa: F401
import pyrung.click.raw  # noqa: F401
import pyrung.core.instruction  # noqa: F401
import pyrung.core.instruction.send_receive._core  # noqa: F401
from pyrung import Char, Int
from pyrung.click.nop import NopInstruction
from pyrung.click.raw import RawInstruction
from pyrung.core.analysis import crossings
from pyrung.core.analysis.crossings import registered_classes, reverse
from pyrung.core.copy_converters import to_ascii, to_value
from pyrung.core.crossing import CrossingContext, Eq, eq_target
from pyrung.core.instruction.advanced import SearchInstruction, ShiftInstruction
from pyrung.core.instruction.base import Instruction
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.instruction.coils import (
    LatchInstruction,
    OutInstruction,
    ResetInstruction,
)
from pyrung.core.instruction.control import (
    CallInstruction,
    EnabledFunctionCallInstruction,
    ForLoopInstruction,
    FunctionCallInstruction,
    ReturnInstruction,
)
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
from pyrung.core.instruction.send_receive._core import (
    ModbusReceiveInstruction,
    ModbusSendInstruction,
)
from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction

# Classes with a projected reverse crossing (copy/fill/blockcopy/calc data-flow,
# the bool coil placeholder, and the five conservative pack/unpack fallthroughs).
EXPECTED_WITH_CROSSINGS = frozenset(
    {
        CopyInstruction,
        FillInstruction,
        BlockCopyInstruction,
        CalcInstruction,
        OutInstruction,
        PackBitsInstruction,
        PackWordsInstruction,
        PackTextInstruction,
        UnpackToBitsInstruction,
        UnpackToWordsInstruction,
        CountUpInstruction,
        CountDownInstruction,
        OnDelayInstruction,
        OffDelayInstruction,
        LatchInstruction,
        ResetInstruction,
    }
)

# Instruction classes deliberately left without a crossing: drums/control-flow/
# search/shift/send/receive carry no copy/calc data-flow to invert projectively
# (yet).
EXEMPT = frozenset(
    {
        SearchInstruction,
        ShiftInstruction,
        CallInstruction,
        EnabledFunctionCallInstruction,
        ForLoopInstruction,
        FunctionCallInstruction,
        ReturnInstruction,
        EventDrumInstruction,
        TimeDrumInstruction,
        ModbusSendInstruction,
        ModbusReceiveInstruction,
        NopInstruction,
        RawInstruction,
    }
)

# Registered crossings that fall through for every input (placeholder / lossy).
# BlockCopy + PackBits/PackWords/UnpackToBits/UnpackToWords now have real
# handlers; only the Boolean-coil placeholder and the lossy PackText parse remain
# unconditional fallthroughs.
_ALWAYS_FALLTHROUGH = (
    PackTextInstruction,
    OffDelayInstruction,
)


def _concrete_instruction_classes() -> frozenset[type]:
    seen: set[type] = set()
    stack: list[type] = [Instruction]
    while stack:
        cls = stack.pop()
        for sub in cls.__subclasses__():
            if sub not in seen:
                seen.add(sub)
                stack.append(sub)
    return frozenset(
        c
        for c in seen
        if not getattr(c, "__abstractmethods__", None)
        and c.__module__.startswith("pyrung.")  # ignore test-defined subclasses
    )


def _ctx() -> CrossingContext:
    return CrossingContext()


def test_registered_classes_match_expected() -> None:
    assert registered_classes() == EXPECTED_WITH_CROSSINGS


def test_expected_and_exempt_are_disjoint() -> None:
    assert not (EXPECTED_WITH_CROSSINGS & EXEMPT)


def test_every_instruction_is_covered_or_exempt() -> None:
    # The forcing function: a new concrete instruction class lands in neither set
    # and fails here until someone gives it a crossing or marks it exempt.
    assert _concrete_instruction_classes() == EXPECTED_WITH_CROSSINGS | EXEMPT


def test_unregistered_type_reverse_is_fallthrough() -> None:
    class _NotAnInstruction:
        pass

    assert reverse(_NotAnInstruction(), None, eq_target("X", 1), _ctx()).fallthrough is True


@pytest.mark.parametrize("cls", _ALWAYS_FALLTHROUGH, ids=lambda c: c.__name__)
def test_registered_fallthrough_cells(cls: type) -> None:
    # BaseCrossing.reverse ignores instr, so a dummy operand is enough to assert
    # the cell is a registered fallthrough.
    crossing = crossings._REGISTRY[cls]
    assert crossing.reverse(None, None, eq_target("X", 1), _ctx()).fallthrough is True


def test_convert_to_value_falls_through_to_ascii_does_not() -> None:
    chars, code = Char("C"), Int("Code")
    assert (
        reverse(
            CopyInstruction(chars, code, convert=to_value), None, eq_target("Code", 5), _ctx()
        ).fallthrough
        is True
    )
    ascii_result = reverse(
        CopyInstruction(chars, code, convert=to_ascii), None, eq_target("Code", 53), _ctx()
    )
    assert ascii_result.fallthrough is False
    assert ascii_result.branches == ((Eq("C", frozenset({"5"})),),)
