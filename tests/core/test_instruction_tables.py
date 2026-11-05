"""Every lookup keyed on an instruction's type knows about every instruction.

Validators and exporters read tables keyed by class, and most of those lookups
are silent on a miss: ``_TIMER_PRESET_FIELDS.get(name, ())`` returns nothing, so a
new timer that is not in the table simply never gets its preset checked, and an
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

import pytest

from pyrung.circuitpy.validation import _evaluate_timer_resolution
from pyrung.click.ladder.instructions import _InstructionMixin
from pyrung.click.nop import NopInstruction
from pyrung.click.raw import RawInstruction
from pyrung.click.validation import hardware
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
from pyrung.core.validation.render import _VERB
from pyrung.dap.miner import _instruction_owned_tags
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
    PackTextInstruction: "pack_text",  # Has its own check, _evaluate_pack_text.
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
    """The instruction classes a function names, as strings or imports."""
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


TABLES = [
    Table(
        "hardware._ROLE_ASSIGNMENT_FIELDS",
        _hardware_keys(hardware._ROLE_ASSIGNMENT_FIELDS),
        _of_kinds("timer", "out_timer", "counter", "drum"),
    ),
    Table(
        "hardware._COPY_COMPATIBILITY_FIELDS",
        _hardware_keys(hardware._COPY_COMPATIBILITY_FIELDS),
        _of_kinds("copies_data"),
    ),
    Table(
        "hardware._COIL_TARGET_INSTRUCTIONS",
        _hardware_keys(hardware._COIL_TARGET_INSTRUCTIONS),
        _of_kinds("coil"),
    ),
    Table(
        "hardware._TIMER_PRESET_FIELDS",
        _hardware_keys(hardware._TIMER_PRESET_FIELDS),
        _of_kinds("timer", "out_timer"),
    ),
    Table(
        "hardware._TIMER_PAIR_FIELDS",
        _hardware_keys(hardware._TIMER_PAIR_FIELDS),
        _of_kinds("timer", "out_timer", "counter"),
    ),
    Table(
        "hardware._OUT_VARIANT_UNITS",
        _hardware_keys(hardware._OUT_VARIANT_UNITS),
        _of_kinds("out_timer"),
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
    Table(
        "miner _instruction_owned_tags",
        _named_in_function(_instruction_owned_tags),
        _of_kinds("timer", "out_timer", "counter", "drum"),
    ),
]


# --- Declared facts: the expected set comes from the instruction itself. ---

_WRITERS = frozenset(cls.__name__ for cls in KIND if cls._writes)

# Conditions that an exported instruction carries as a pin of its own, not as the
# rung it sits on.  A class declaring one that _pin_specs misses loses it on export.
_PIN_CONDITIONS = frozenset(
    {"reset_condition", "clock_condition", "down_condition", "jump_condition", "jog_condition"}
)

# Render falls back to verb(target), and to the class name without a verb.  These
# writers have no verb yet, so a finding on one shows FunctionCallInstruction(x).
_WRITERS_WITHOUT_A_VERB = frozenset(
    {
        ForLoopInstruction.__name__,
        FunctionCallInstruction.__name__,
        EnabledFunctionCallInstruction.__name__,
    }
)

# Declared writes that the two hardware tables do not check, and why that is right.
_WRITES_CHECKED_ELSEWHERE: dict[tuple[str, str], str] = {
    (CopyInstruction.__name__, "dest"): "the table names it target, a property on dest",
    (EventDrumInstruction.__name__, "outputs"): "_evaluate_drums checks outputs",
    (TimeDrumInstruction.__name__, "outputs"): "_evaluate_drums checks outputs",
    (TimeEventDrumInstruction.__name__, "outputs"): "_evaluate_drums checks outputs",
    (ForLoopInstruction.__name__, "idx_tag"): "no CLICK validation or export reads a loop's index",
    (FunctionCallInstruction.__name__, "_outs"): "run_function has no CLICK form",
    (EnabledFunctionCallInstruction.__name__, "_outs"): "run_function has no CLICK form",
}

_MODBUS_GAP = (
    "CLICK validation never checks where send() and receive() write: a dest in read-only "
    "SD and a status bit on X report nothing, where copy() reports CLK_BANK_NOT_WRITABLE"
)


def _writes_to_check() -> list[object]:
    params: list[object] = []
    for cls in sorted(KIND, key=lambda c: c.__name__):
        for field in cls._writes:
            if (cls.__name__, field) in _WRITES_CHECKED_ELSEWHERE:
                continue
            is_modbus = KIND[cls] == "modbus"
            params.append(
                pytest.param(
                    cls,
                    field,
                    id=f"{cls.__name__}.{field}",
                    marks=(pytest.mark.xfail(strict=True, reason=_MODBUS_GAP),)
                    if is_modbus
                    else (),
                )
            )
    return params


def _hardware_fields() -> list[tuple[str, str]]:
    fields = set(hardware._WRITE_TARGET_FIELDS) | set(hardware._ROLE_ASSIGNMENT_FIELDS)
    for name, field_names in hardware._TIMER_PRESET_FIELDS.items():
        fields.update((name, field) for field in field_names)
    for name, pairs in hardware._TIMER_PAIR_FIELDS.items():
        fields.update((name, field) for pair in pairs for field in pair)
    for name, (_form, source, dest) in hardware._COPY_COMPATIBILITY_FIELDS.items():
        fields.update({(name, source), (name, dest)})
    return sorted(fields)


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
    assert frozenset(_VERB) == _WRITERS - _WRITERS_WITHOUT_A_VERB


def test_every_pin_is_exported() -> None:
    declared = frozenset(cls.__name__ for cls in KIND if _PIN_CONDITIONS & set(cls._conditions))
    assert _named_in_function(_InstructionMixin._pin_specs) == declared


@pytest.mark.parametrize("cls,field", _writes_to_check())
def test_every_declared_write_is_checked_on_click(cls: type, field: str) -> None:
    # Timers, counters and drums reach this through a role, which is the stricter check.
    name = cls.__name__
    assert (name, field) in hardware._WRITE_TARGET_FIELDS | set(hardware._ROLE_ASSIGNMENT_FIELDS), (
        f"nothing checks that CLICK can write {name}.{field}: add it to _WRITE_TARGET_FIELDS or "
        "_ROLE_ASSIGNMENT_FIELDS, or to _WRITES_CHECKED_ELSEWHERE in this file with the reason"
    )


@pytest.mark.parametrize("name,field", _hardware_fields(), ids=lambda v: v)
def test_hardware_tables_name_real_fields(name: str, field: str) -> None:
    # Some of these lookups read getattr(instruction, field, None): a wrong name is a
    # check that never runs.
    cls = next(c for c in KIND if c.__name__ == name)
    assert field in _assigned_fields(cls), f"{name} has no field {field}"
