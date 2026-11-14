"""Crossings Phase 2 — coverage map (the forcing function).

Every concrete instruction class is either covered by a crossing
(``EXPECTED_WITH_CROSSINGS``) or explicitly exempt (``EXEMPT``).  A new
instruction class in neither set fails ``test_every_instruction_is_covered_or_exempt``,
forcing a covered-or-fallthrough decision.

Two holes stay closed here:

* ``crossing_for()`` walks the MRO, so a subclass of a registered class inherits
  its parent's crossing.  ``test_exempt_classes_resolve_no_crossing`` keeps an
  ``EXEMPT`` entry from silently resolving to a parent's crossing at runtime.
* Discovery walks ``Instruction.__subclasses__()``, which only sees imported
  modules.  ``_import_all_pyrung_modules`` imports every ``pyrung`` module first,
  so the enumeration does not depend on what the session happened to import.
"""

from __future__ import annotations

import functools
import importlib
import pkgutil
import subprocess
import sys
import textwrap

import pytest

import pyrung
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
    BreakInstruction,
    CallInstruction,
    ContinueInstruction,
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
from pyrung.core.instruction.drums import (
    EventDrumInstruction,
    TimeDrumInstruction,
    TimeEventDrumInstruction,
)
from pyrung.core.instruction.out_variants import (
    DebounceInstruction,
    FlasherInstruction,
    TimedInstruction,
    ToggleInstruction,
)
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
        ShiftInstruction,
        EventDrumInstruction,
        TimeDrumInstruction,
        TimeEventDrumInstruction,
        SearchInstruction,
        ModbusReceiveInstruction,
        ToggleInstruction,
        DebounceInstruction,
        FlasherInstruction,
        TimedInstruction,
    }
)

# Instruction classes deliberately left without a crossing: control-flow carries
# no data write to invert; modbus_send writes no local tag; Nop/Raw are no-op /
# opaque.
EXEMPT = frozenset(
    {
        BreakInstruction,
        CallInstruction,
        ContinueInstruction,
        EnabledFunctionCallInstruction,
        ForLoopInstruction,
        FunctionCallInstruction,
        ReturnInstruction,
        ModbusSendInstruction,
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
    ToggleInstruction,
    DebounceInstruction,
    FlasherInstruction,
    TimedInstruction,
)


# Modules skipped by the import walk: ``pyrung.zen`` prints on import and
# ``__main__`` modules run an entry point.  Neither may define an instruction.
_IMPORT_WALK_SKIP = frozenset({"pyrung.zen"})


def _walk_skipped(name: str) -> bool:
    return name in _IMPORT_WALK_SKIP or name.rsplit(".", 1)[-1] == "__main__"


@functools.cache
def _import_all_pyrung_modules() -> None:
    """Import every module under ``pyrung`` so subclass discovery is complete."""
    for info in pkgutil.walk_packages(pyrung.__path__, prefix="pyrung."):
        if not _walk_skipped(info.name):
            importlib.import_module(info.name)


def _registered_crossing_by_mro(cls: type) -> object | None:
    """``crossing_for``'s lookup, at class level (it takes an instance)."""
    for base in cls.__mro__:
        crossing = crossings._REGISTRY.get(base)
        if crossing is not None:
            return crossing
    return None


def _concrete_instruction_classes() -> frozenset[type]:
    _import_all_pyrung_modules()
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


@pytest.mark.parametrize("cls", sorted(EXEMPT, key=lambda c: c.__name__), ids=lambda c: c.__name__)
def test_exempt_classes_resolve_no_crossing(cls: type) -> None:
    # An exempt subclass of a registered class would inherit the parent's
    # crossing through the MRO walk; exempt must mean no crossing at runtime.
    assert _registered_crossing_by_mro(cls) is None


def test_registry_is_complete_from_package_import_alone() -> None:
    # Production code only imports ``pyrung.core.analysis.crossings``; a
    # crossing registered in a module that package does not import would be
    # missing at runtime yet present in a test session that imported it.
    # Compare a fresh interpreter's registry before and after importing all of
    # ``pyrung`` so the check does not depend on this session's imports.
    script = textwrap.dedent(
        f"""
        import importlib, pkgutil
        import pyrung
        from pyrung.core.analysis.crossings import registered_classes

        def names():
            return sorted(f"{{c.__module__}}.{{c.__qualname__}}" for c in registered_classes())

        before = names()
        skip = {sorted(_IMPORT_WALK_SKIP)!r}
        for info in pkgutil.walk_packages(pyrung.__path__, prefix="pyrung."):
            if info.name in skip or info.name.rsplit(".", 1)[-1] == "__main__":
                continue
            importlib.import_module(info.name)
        after = names()
        print(sorted(set(after) - set(before)))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert proc.stdout.strip().splitlines()[-1] == "[]", proc.stdout


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
