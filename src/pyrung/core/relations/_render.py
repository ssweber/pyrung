"""A stand-in copy of an instruction, rendered through the kernel codegen with a recorder.

The stand-in keeps every field that changes the generated code, such as the unit, the
kind of preset, or whether a counter has a down input.  It puts a tag of its own in place
of each tag and each condition, named ``pb_<field>``, so a record names the field it
reads.  Its rung is its power field (the timer's enable, the counter's count input), so
the rung's test reads that field too.

An integer literal, such as a preset of 100, changes only the number in the text.  So its
value is not part of the shape, and the records name its field (``Setting``).  A test
renders presets of every sign and size to check that.
"""

from __future__ import annotations

import copy
import functools
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pyrung.core.instruction.operands import as_authored
from pyrung.core.relations.records import Record, Recorder
from pyrung.core.tag import LiveTag, Tag, TagType

PREFIX = "pb_"

# What the site binder and the debugger stamp on an instruction.  None of it changes the
# generated code, and the stand-in's one-rung program stamps its own.
_SITE_ATTRS = frozenset(
    {
        "source_file",
        "source_line",
        "end_line",
        "debug_substeps",
        "_state_key",
        "_site_program_owned",
        "_site_owner",
        "_edge_frame",
    }
)


class RelationsError(Exception):
    """The instruction's code could not be recorded, so it has no relations."""


@functools.cache
def _supported() -> tuple[type, ...]:
    # The classes whose emitters record everything they write.  Each new emitter that
    # records joins this list.
    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction

    return (OnDelayInstruction, OffDelayInstruction, CountUpInstruction, CountDownInstruction)


def _plain(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str, Enum))


@functools.cache
def _fields(cls: Any) -> tuple[tuple[str, ...], frozenset[str], frozenset[str], frozenset[str]]:
    """The fields of *cls* that a shape key reads, and the attributes it skips."""
    fields = cls.walker_fields()
    return (
        fields,
        frozenset(cls._conditions),
        frozenset(cls._structural_fields),
        frozenset(fields) | _SITE_ATTRS,
    )


def shape_key(instr: Any) -> tuple[Any, ...] | None:
    """What *instr*'s stand-in renders from, or None when no stand-in can stand for it."""
    cls = type(instr)
    if cls not in _supported():
        return None
    fields, conditions, structural, skip = _fields(cls)
    key: list[Any] = [cls]
    for field in fields:
        value = as_authored(getattr(instr, field, None))
        if value is None:
            key.append((field, None))
        elif field in conditions:
            key.append((field, "condition"))
        elif isinstance(value, Tag):
            key.append((field, "tag", value.type))
        elif type(value) is int:
            key.append((field, "int"))
        elif isinstance(value, (bool, float)) or (field in structural and _plain(value)):
            key.append((field, type(value).__name__, value))
        else:
            return None
    # Attributes keep the order the class sets them in.  Another order costs a render.
    for attr, value in vars(instr).items():
        if attr in skip:
            continue
        if not _plain(value):
            return None
        key.append((attr, value))
    return tuple(key)


def power_field(instr: Any) -> str | None:
    """The condition field that carries the rung's own power, such as a timer's enable."""
    from pyrung.core.site_memory import _is_power_field

    return next((f for f in type(instr)._conditions if _is_power_field(instr, f)), None)


@dataclass(frozen=True)
class Rendered:
    source: str
    records: tuple[Record, ...]
    # Stand-in tag name -> the field it stands for.
    fields: dict[str, str]
    power: str
    # Tag name -> its symbol in the source, such as ``_t_pb_done_bit``.
    symbols: dict[str, str]


def _standin(instr: Any) -> tuple[Any, dict[str, str]]:
    from pyrung.core.instruction.utils import to_condition

    cls = type(instr)
    new = copy.copy(instr)
    for attr in _SITE_ATTRS & set(vars(new)):
        delattr(new, attr)
    fields: dict[str, str] = {}
    for field in cls.walker_fields():
        value = as_authored(getattr(instr, field, None))
        if value is None:
            continue
        name = PREFIX + field
        if field in cls._conditions:
            fields[name] = field
            setattr(new, field, to_condition(LiveTag(name, TagType.BOOL)))
        elif isinstance(value, Tag):
            fields[name] = field
            setattr(new, field, LiveTag(name, value.type))
    return new, fields


def render(instr: Any, *, record: bool = True) -> Rendered:
    """The kernel source of a one-rung program holding *instr*'s stand-in.  Nothing runs it."""
    from pyrung.circuitpy.codegen.context import CodegenContext
    from pyrung.circuitpy.codegen.render_kernel import _render_kernel_source
    from pyrung.core.program import Program
    from pyrung.core.rung import Rung

    power = power_field(instr)
    if power is None:
        raise RelationsError(f"{type(instr).__name__} has no power field")
    new, fields = _standin(instr)
    program = Program(strict=False)
    # A rung with no condition leaves the power field None, and the stand-in's rung too.
    power_condition = getattr(new, power)
    rung = Rung() if power_condition is None else Rung(power_condition)
    rung.add_instruction(new)
    program.rungs.append(rung)
    program._assign_state_keys()
    ctx = CodegenContext.for_kernel(program, blockless=True)
    recorder = Recorder() if record else None
    ctx.recorder = recorder
    source = _render_kernel_source(ctx)
    records = tuple(recorder.records) if recorder is not None else ()
    return Rendered(source, records, fields, power, dict(ctx.symbol_table))
