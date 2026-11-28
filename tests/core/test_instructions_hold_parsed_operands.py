"""Every instruction holds its operands parsed, never as the raw object that the author passed.

The parse (:mod:`pyrung.core.instruction.operands`) turns each operand into a result such
as ``TagArg`` or ``RangeArg`` when the program is built.  The readers then match on those
results.  A field that holds a raw ``Tag`` or ``BlockRange`` instead slips past them.

A field that holds no operand, such as a condition or a tag that pyrung makes itself, is
listed in ``STRUCTURAL``.  Each instruction class needs an entry there, so a new class
fails here until someone lists its fields.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import pyrung
from pyrung.core.condition import Condition
from pyrung.core.expression import Expression
from pyrung.core.instruction.base import Instruction
from pyrung.core.instruction.operands import (
    ConstArg,
    ExprArg,
    PointerArg,
    PointerRangeArg,
    RangeArg,
    TagArg,
)
from pyrung.core.memory_block import BlockRange, IndirectBlockRange, IndirectExprRef, IndirectRef
from pyrung.core.tag import ImmediateRef, Tag
from pyrung.core.validation._common import walk_instructions

PARSED = (TagArg, PointerArg, ConstArg, ExprArg, RangeArg, PointerRangeArg)
RAW = (Tag, BlockRange, IndirectRef, IndirectExprRef, IndirectBlockRange, ImmediateRef)

_DRUM_CONDITIONS = ("auto_condition", "reset_condition", "jump_condition", "jog_condition")

# The fields of each instruction class that hold something other than an operand.
STRUCTURAL: dict[str, tuple[str, ...]] = {
    "BlockCopyInstruction": (),
    "BreakInstruction": (),
    # math() keeps the formula that calc_form() rewrites for its mode.  The parse result is
    # source_expression.
    "CalcInstruction": ("expression",),
    "CallInstruction": (),
    "ContinueInstruction": (),
    "CopyInstruction": (),
    "CountDownInstruction": ("down_condition", "reset_condition"),
    "CountUpInstruction": ("up_condition", "down_condition", "reset_condition"),
    "DebounceInstruction": ("enable_condition",),
    "EnabledFunctionCallInstruction": ("_enable_condition",),
    "EventDrumInstruction": ("events", *_DRUM_CONDITIONS),
    "FillInstruction": (),
    "FlasherInstruction": ("enable_condition",),
    # forloop() makes the tag that holds the loop's index, and the author never passes it.
    # send() and receive() differ.  The author may pass each status tag, so the tag is parsed
    # even when the builder makes a hidden one.
    "ForLoopInstruction": ("idx_tag",),
    "FunctionCallInstruction": (),
    # A coil keeps the tags of its target, read from the parsed target, to write them fast.
    "LatchInstruction": ("_static_targets",),
    "ModbusReceiveInstruction": (),
    "ModbusSendInstruction": (),
    "NopInstruction": (),
    "OffDelayInstruction": ("enable_condition",),
    "OnDelayInstruction": ("enable_condition", "reset_condition"),
    "OutInstruction": ("_static_targets",),
    "PackBitsInstruction": (),
    "PackTextInstruction": (),
    "PackWordsInstruction": (),
    "RawInstruction": (),
    "ResetInstruction": ("_static_targets",),
    "ReturnInstruction": (),
    "SearchInstruction": (),
    "ShiftInstruction": ("data_condition", "clock_condition", "reset_condition"),
    "TimeDrumInstruction": _DRUM_CONDITIONS,
    "TimeEventDrumInstruction": ("events", *_DRUM_CONDITIONS),
    "TimedInstruction": ("enable_condition",),
    "ToggleInstruction": ("enable_condition",),
    "UndecodedInstruction": (),
    "UnpackToBitsInstruction": (),
    "UnpackToWordsInstruction": (),
}


def _instruction_classes() -> set[str]:
    """Return the name of each instruction class in pyrung that a program can hold."""
    # Import every module, so that a class counts even when no package imports its module.
    for module in pkgutil.walk_packages(pyrung.__path__, "pyrung."):
        importlib.import_module(module.name)
    found: set[str] = set()
    pending = [Instruction]
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if cls.__module__.startswith("pyrung.") and not inspect.isabstract(cls):
            found.add(cls.__name__)
    found.discard("Instruction")
    return found


def _raw_parts(value: Any, path: str, seen: set[int]) -> Iterator[tuple[str, str]]:
    """Yield the path and type of each raw operand in *value*, outside a parse result.

    The walk goes into tuples, lists, sets and dicts, and into expressions and conditions.
    It stops at a parse result and at an instruction, which the test walks on its own.
    """
    if id(value) in seen:
        return
    seen.add(id(value))
    if isinstance(value, (*PARSED, Instruction)):
        return
    if isinstance(value, RAW):
        yield path, type(value).__name__
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _raw_parts(key, f"{path} key {key!r}", seen)
            yield from _raw_parts(item, f"{path}[{key!r}]", seen)
    elif isinstance(value, list | tuple | set | frozenset):
        for index, item in enumerate(value):
            yield from _raw_parts(item, f"{path}[{index}]", seen)
    elif isinstance(value, Expression | Condition):
        for name, part in vars(value).items():
            yield from _raw_parts(part, f"{path}.{name}", seen)


def _corpus(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Any]]:
    """Yield each program of the render corpus, by its directory in the render."""
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "devtools"))
    import render_corpus

    for rel, build in render_corpus._corpus(None):
        # The apart build holds the suite's groups again.
        if rel == "conformance/apart":
            continue
        built = build()
        yield rel, getattr(built, "program", built)


def test_every_instruction_class_lists_its_structural_fields() -> None:
    assert _instruction_classes() == set(STRUCTURAL)


def test_every_operand_in_the_render_corpus_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    raw: list[str] = []
    fields: dict[str, set[str]] = {}
    for rel, program in _corpus(monkeypatch):
        for instruction in walk_instructions(program):
            name = type(instruction).__name__
            structural = STRUCTURAL[name]
            fields.setdefault(name, set()).update(vars(instruction))
            for field, value in vars(instruction).items():
                if field in structural:
                    continue
                for path, kind in _raw_parts(value, f"{name}.{field}", set()):
                    raw.append(f"{rel}: {path} holds a raw {kind}")
    assert not raw, "\n".join(raw[:20])
    # Most classes appear in the corpus.  A listed field that none of them has is stale.
    assert len(fields) > 30
    stale = [
        f"{name}.{field}"
        for name, seen in fields.items()
        for field in STRUCTURAL[name]
        if field not in seen
    ]
    assert not stale
