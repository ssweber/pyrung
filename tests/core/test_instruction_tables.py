"""Every lookup keyed on an instruction's type knows about every instruction.

Validators and exporters read tables keyed by class, and most of those lookups
are silent on a miss: ``table.get(name, ())`` returns nothing, so a new instruction that
is not in the table simply never gets checked, and an
exporter's ``_pin_specs`` drops a pin.  Nothing fails, here or anywhere else.

So a new instruction fails ``test_every_instruction_has_a_kind`` until it is given
a kind in ``KIND`` below.  Each table then says which kinds it must cover, and fails
until the instruction is in it.  Where an instruction already declares the fact
(``_writes``, ``_conditions``) the expected set comes from that declaration, not
from a list kept here.

This covers the lookups that are silent.  ``_instruction_token`` and the CircuitPython
compiler raise on an instruction they do not know, so a miss there already fails.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import pytest

import pyrung.core
from pyrung.circuitpy.validation import _evaluate_timer_resolution
from pyrung.click.ladder.instructions import _InstructionMixin
from pyrung.click.nop import NopInstruction
from pyrung.click.raw import RawInstruction
from pyrung.click.validation import legacy
from pyrung.click.validation.families import FAMILIES
from pyrung.core import Int
from pyrung.core.analysis.causal.why import _INSTRUCTION_LABELS
from pyrung.core.instruction.advanced import SearchInstruction, ShiftInstruction
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.instruction.coils import LatchInstruction, OutInstruction, ResetInstruction
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
from pyrung.core.validation.dead_write import _AMBIGUOUS_INSTRUCTIONS
from pyrung.core.validation.owned_bit_coil import _COILS, _OWNERS
from pyrung.core.validation.render import _VERB, render_instruction
from tests.core.analysis.test_crossings_coverage import _concrete_instruction_classes

# One entry per concrete instruction.  Adding an instruction here is the decision
# this file exists to force: pick the kind that says which tables must know it.
KIND: dict[type, str] = {
    OnDelayInstruction: "timer",
    OffDelayInstruction: "timer",
    DebounceInstruction: "out_timer",  # Owns timers but never writes their Done.
    FlasherInstruction: "out_timer",
    TimedInstruction: "out_timer",
    ToggleInstruction: "toggle",
    OutInstruction: "coil",
    LatchInstruction: "coil",
    ResetInstruction: "coil",
    CountUpInstruction: "counter",
    CountDownInstruction: "counter",
    EventDrumInstruction: "drum",
    TimeDrumInstruction: "drum",
    TimeEventDrumInstruction: "drum",
    CopyInstruction: "copies_data",
    BlockCopyInstruction: "copies_data",
    FillInstruction: "copies_data",
    PackBitsInstruction: "copies_data",
    PackWordsInstruction: "copies_data",
    UnpackToBitsInstruction: "copies_data",
    UnpackToWordsInstruction: "copies_data",
    PackTextInstruction: "copies_data",
    CalcInstruction: "other_writer",
    SearchInstruction: "other_writer",
    ShiftInstruction: "other_writer",
    ModbusSendInstruction: "modbus",
    ModbusReceiveInstruction: "modbus",
    ForLoopInstruction: "runs_other_code",
    CallInstruction: "runs_other_code",
    FunctionCallInstruction: "runs_other_code",
    EnabledFunctionCallInstruction: "runs_other_code",
    BreakInstruction: "control",
    ContinueInstruction: "control",
    ReturnInstruction: "control",
    NopInstruction: "click_only",
    RawInstruction: "click_only",
}


def _named_in_function(fn: Callable) -> frozenset[str]:
    """Return the instruction classes that a function names, as strings or imports."""
    code = fn.__code__
    found: set[str] = set(code.co_names)
    for const in code.co_consts:
        found.update(const if isinstance(const, (frozenset, tuple)) else (const,))
    return frozenset(n for n in found if isinstance(n, str) and n.endswith("Instruction"))


def _of_kinds(*kinds: str) -> frozenset[str]:
    return frozenset(cls.__name__ for cls, kind in KIND.items() if kind in kinds)


@dataclass(frozen=True)
class Table:
    name: str
    names: frozenset[str]
    must_cover: frozenset[str]

    def __str__(self) -> str:
        return self.name


def _hardware_keys(table: Iterable[str | tuple[str, str]]) -> frozenset[str]:
    return frozenset(key[0] if isinstance(key, tuple) else key for key in table)


# laddercodec checks each instruction of a family in families/. pyrung's own tables leave
# those instructions out, so that a family branch deletes its rows and edits nothing here.
_IN_FAMILIES = frozenset(name for family in FAMILIES for name in family.classes)


def _legacy_table(name: str, *kinds: str) -> Table:
    return Table(
        f"legacy {name}", _hardware_keys(legacy.table(name)), _of_kinds(*kinds) - _IN_FAMILIES
    )


TABLES = [
    _legacy_table("ROLE_ASSIGNMENT_FIELDS", "timer", "out_timer", "counter", "drum"),
    Table(
        "the copy family's operands",
        frozenset(next(f for f in FAMILIES if f.name == "copy").classes),
        _of_kinds("copies_data"),
    ),
    Table(
        "circuitpy _evaluate_timer_resolution",
        _named_in_function(_evaluate_timer_resolution),
        _of_kinds("timer", "out_timer"),
    ),
    # An Out variant never writes its timers' Done, so a coil there has no owner.
    Table("owned_bit_coil._OWNERS", frozenset(_OWNERS), _of_kinds("timer", "counter")),
    Table("owned_bit_coil._COILS", frozenset(_COILS), _of_kinds("coil", "toggle", "out_timer")),
    Table(
        "dead_write._AMBIGUOUS_INSTRUCTIONS",
        frozenset(cls.__name__ for cls in _AMBIGUOUS_INSTRUCTIONS),
        _of_kinds("runs_other_code"),
    ),
]


# --- Declared facts: the expected set comes from the instruction itself. ---

_WRITERS = frozenset(cls.__name__ for cls in KIND if cls._writes)

# These conditions are exported as a pin on the instruction itself, not as part of the
# rung it sits on.  If a class declares one and _pin_specs misses it, the export drops it.
_PIN_CONDITIONS = frozenset(
    {"reset_condition", "clock_condition", "down_condition", "jump_condition", "jog_condition"}
)

# Declared writes that the write and role tables do not check, and why that is right.
_WRITES_CHECKED_ELSEWHERE: dict[tuple[str, str], str] = {
    (EventDrumInstruction.__name__, "outputs"): "the drum family's CLICK check reads outputs",
    (TimeDrumInstruction.__name__, "outputs"): "the drum family's CLICK check reads outputs",
    (TimeEventDrumInstruction.__name__, "outputs"): "the drum family's CLICK check reads outputs",
    (ForLoopInstruction.__name__, "idx_tag"): "no CLICK validation or export reads a loop's index",
    (FunctionCallInstruction.__name__, "_outs"): "run_function has no CLICK form",
    (EnabledFunctionCallInstruction.__name__, "_outs"): "run_function has no CLICK form",
}


def _writes_to_check() -> list[object]:
    return [
        pytest.param(cls, field, id=f"{cls.__name__}.{field}")
        for cls in sorted(KIND, key=lambda c: c.__name__)
        for field in cls._writes
        if (cls.__name__, field) not in _WRITES_CHECKED_ELSEWHERE
    ]


def _family_operands() -> set[tuple[str, str]]:
    """Return each operand of each instruction of a family that laddercodec checks."""
    found: set[tuple[str, str]] = set()
    for family in FAMILIES:
        for name, words in family.classes.items():
            found.update((name, attribute) for attribute in words)
    return found


# How to read the fields out of a row of each table in legacy/.
_FIELDS_OF_ROW: dict[str, Callable[[Any], Iterable[str]]] = {
    "WRITE_TARGET_FIELDS": lambda fields: fields,
    "ROLE_ASSIGNMENT_FIELDS": lambda roles: [field for field, _role in roles],
}


def _legacy_fields(*names: str) -> set[tuple[str, str]]:
    """Return each (class, field) that the tables *names* in legacy/ read."""
    found: set[tuple[str, str]] = set()
    for name in names:
        for key, row in legacy.table(name).items():
            if isinstance(key, tuple):
                found.add(key)
            else:
                found.update((key, field) for field in _FIELDS_OF_ROW[name](row))
    return found


def _checked_writes() -> set[tuple[str, str]]:
    # Timers, counters and drums reach the write check through a role. Send and Receive
    # reach it through the banks that CLICK's help pages list for them. laddercodec checks
    # every operand of an instruction in one of its families.
    return _legacy_fields("WRITE_TARGET_FIELDS", "ROLE_ASSIGNMENT_FIELDS") | _family_operands()


def _hardware_fields() -> list[tuple[str, str]]:
    return sorted(_legacy_fields(*_FIELDS_OF_ROW) | _family_operands())


def _assigned_fields(cls: type) -> set[str]:
    names: set[str] = {attr for attr in dir(cls) if not attr.startswith("__")}
    for base in cls.__mro__:
        init = base.__dict__.get("__init__")
        code = getattr(init, "__code__", None)
        if code is not None:
            names.update(code.co_names)
    return names


# --- The tests. ---


def test_every_instruction_has_a_kind() -> None:
    known = frozenset(KIND)
    actual = _concrete_instruction_classes()
    assert not actual - known, (
        f"{sorted(c.__name__ for c in actual - known)} have no kind: add each to KIND in this "
        "file, which says which validator and exporter tables must know about it"
    )
    assert not known - actual, f"{sorted(c.__name__ for c in known - actual)} no longer exist"


@pytest.mark.parametrize("table", TABLES, ids=str)
def test_table_covers_exactly_its_kinds(table: Table) -> None:
    missing = table.must_cover - table.names
    extra = table.names - table.must_cover
    assert not missing, (
        f"{table.name} does not know {sorted(missing)}, and a miss there is silent: add them, "
        "or change the kinds this table covers in this file"
    )
    assert not extra, (
        f"{table.name} names {sorted(extra)}, which its kinds do not include: remove a stale "
        "name, or change the kinds this table covers in this file"
    )


def test_every_writer_is_labelled() -> None:
    # cause() labels an instruction only by what it writes.
    assert frozenset(_INSTRUCTION_LABELS) == _WRITERS


def test_every_writer_has_a_verb() -> None:
    # Render falls back to verb(target), or to the class name if there is no verb.
    assert frozenset(_VERB) == _WRITERS


@pytest.mark.parametrize("cls_name,verb", sorted(_VERB.items()), ids=lambda v: v)
def test_findings_show_real_dsl_names(cls_name: str, verb: str) -> None:
    # A finding prints this as source, so it has to be something the user can write.
    assert hasattr(pyrung.core, verb), f"{cls_name} renders as {verb}(), not in pyrung.core"
    assert _INSTRUCTION_LABELS[cls_name] == verb


def test_a_calc_finding_shows_calc_as_it_is_written() -> None:
    a, dest = Int("A"), Int("Dest")
    assert render_instruction(CalcInstruction(a + 1, dest), "Dest")[0] == "math((A + 1), Dest)"


def test_every_pin_is_exported() -> None:
    declared = frozenset(cls.__name__ for cls in KIND if _PIN_CONDITIONS & set(cls._conditions))
    assert _named_in_function(_InstructionMixin._pin_specs) == declared


@pytest.mark.parametrize("cls,field", _writes_to_check())
def test_every_declared_write_is_checked_on_click(cls: type, field: str) -> None:
    assert (cls.__name__, field) in _checked_writes(), (
        f"nothing checks that CLICK can write {cls.__name__}.{field}: add it to a table in "
        "legacy/, or to _WRITES_CHECKED_ELSEWHERE in this file with the reason"
    )


@pytest.mark.parametrize("name,field", _hardware_fields(), ids=lambda v: v)
def test_hardware_tables_name_real_fields(name: str, field: str) -> None:
    # Some of these lookups read getattr(instruction, field, None), so a wrong name gives
    # a check that never runs.
    cls = next(c for c in KIND if c.__name__ == name)
    assert field in _assigned_fields(cls), f"{name} has no field {field}"
