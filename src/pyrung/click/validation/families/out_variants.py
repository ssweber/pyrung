"""The Out variant family: toggle(), debounce(), flasher() and timed().

laddercodec's OutVariant checks them.
"""

from __future__ import annotations

from typing import Any, assert_never

from laddercodec import OutVariant, OutVariantKind

from pyrung.core.instruction.operands import ConstArg, Preset, TagArg
from pyrung.core.time_mode import TimeUnit
from pyrung.core.validation.render import Call, operand_name

from ..fields import (
    Family,
    Operand,
    declared_operands,
    refusals_of,
    render_operand,
    render_preset,
)
from ..resolve import _ResolutionCache
from .coils import immediate_call

_KINDS: dict[str, OutVariantKind] = {
    "ToggleInstruction": "toggle",
    "DebounceInstruction": "debounce",
    "FlasherInstruction": "flasher",
    "TimedInstruction": "timed",
}

# The attributes that hold each side of a variant, as (side, accumulator, preset). A
# debounce and a flasher have an on and an off side. A timed coil has an on side only, and
# its accumulator is ``timer_acc``.
_SIDES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "DebounceInstruction": (("on", "on_acc", "on_preset"), ("off", "off_acc", "off_preset")),
    "FlasherInstruction": (("on", "on_acc", "on_preset"), ("off", "off_acc", "off_preset")),
    "TimedInstruction": (("on", "timer_acc", "on_preset"),),
}

# CLICK v4's Out dialog offers fewer time units than its timers do.
_UNIT_CHOICES = {
    "debounce": "ms/sec",
    "flasher": "ms/sec",
    "timed": "ms/sec/min",
}

# The argument of the pyrung call that holds each field of laddercodec's OutVariant. The
# call names the accumulator and the preset of each side ``on_acc``, ``on_preset``,
# ``off_acc`` and ``off_preset``. A timed coil writes its timer and its preset as
# positional arguments with the names of its on side.
_FIELD_ARGUMENTS: dict[str, int | str] = {
    "OutVariant.operand": 0,
    "OutVariant.immediate": 0,
    "OutVariant.on_acc": "on_acc",
    "OutVariant.on_preset": "on_preset",
    "OutVariant.off_acc": "off_acc",
    "OutVariant.off_preset": "off_preset",
    "OutVariant.unit": "unit",
    "OutVariant.maintained": "maintained",
}


# The attribute that holds the preset that each laddercodec field names.
_PRESET_FIELDS = {"OutVariant.on_preset": "on_preset", "OutVariant.off_preset": "off_preset"}


def _advice(code: str, field: str, instruction: Any, resolver: _ResolutionCache) -> str | None:
    if code == "coil-time-unit":
        choices = _UNIT_CHOICES[_KINDS[type(instruction).__name__]]
        return f"Use unit= one of {choices} and scale the preset."
    if code == "constant-out-of-range" and field in _PRESET_FIELDS:
        preset: Preset = getattr(instruction, _PRESET_FIELDS[field])
        match preset:
            case ConstArg(value=int() as value) if value <= 0:
                return "Pass a DS tag as the preset. A register preset takes any value."
    return None


def _preset_value(preset: Preset) -> Any:
    """Return the tag or the constant that *preset* holds, as the author passed it."""
    match preset:
        case TagArg(tag=tag):
            return tag
        case ConstArg(value=value):
            return value
        case _:
            assert_never(preset)


def _operands(instruction: Any) -> list[Operand]:
    """Return each argument of the call that holds the target, a timer or a preset."""
    name = type(instruction).__name__
    if name not in _SIDES:
        return list(declared_operands(instruction, {"target": "target"}))
    found = [Operand(0, "target", (("target", instruction.target.tag),))]
    for side, acc, preset in _SIDES[name]:
        found.append(
            Operand(f"{side}_acc", f"{side} timer", ((acc, getattr(instruction, acc).tag),))
        )
        found.append(
            Operand(
                f"{side}_preset",
                f"{side} preset",
                ((preset, _preset_value(getattr(instruction, preset))),),
            )
        )
    return found


def _owner(acc: Any) -> str:
    """Return the name of the timer that holds *acc*, such as ``Timer1`` for ``Timer1.Acc``."""
    text = operand_name(acc)
    return text.rpartition(".")[0] or text


def _call(instruction: Any) -> Call:
    """Return the call of the variant as its author wrote it.

    A debounce and a flasher write ``on=(Timer1, 500), off=(Timer2, 200)``. A timed coil
    writes ``Timer1, 500``. The unit shows only when it is not milliseconds.
    """
    name = type(instruction).__name__
    if name not in _SIDES:
        return immediate_call(instruction)
    text = f"{_KINDS[name]}("
    arguments: list[tuple[str, int, int, str]] = []

    def add(argument: str, shown: str, kind: str) -> None:
        nonlocal text
        arguments.append((argument, len(text), len(shown), kind))
        text += shown

    shown = operand_name(instruction.target.tag)
    if instruction.target.immediate:
        shown = f"immediate({shown})"
    add("target", shown, "positional")
    for side, acc, preset in _SIDES[name]:
        if name == "TimedInstruction":
            text += ", "
            add("on_acc", _owner(instruction.timer_acc.tag), "positional")
            text += ", "
            add("on_preset", operand_name(_preset_value(instruction.on_preset)), "positional")
            continue
        text += f", {side}=("
        add(f"{side}_acc", _owner(getattr(instruction, acc).tag), "keyword")
        text += ", "
        add(f"{side}_preset", operand_name(_preset_value(getattr(instruction, preset))), "keyword")
        text += ")"
    if instruction.unit is not TimeUnit.Tms:
        text += ", unit="
        add("unit", repr(instruction.unit.value), "keyword")
    if getattr(instruction, "maintained", False):
        text += ", maintained="
        add("maintained", "True", "keyword")
    return Call(text + ")", tuple(arguments))


def _build(instruction: Any, exporter: Any) -> list[Any]:
    """Return laddercodec's refusals of the Out variant that the export writes."""
    name = type(instruction).__name__
    # The export refuses an immediate target outside Y itself. laddercodec gets the
    # address and the flag, and refuses it in its own words.
    target: TagArg = instruction.target
    immediate = target.immediate
    operand = render_operand(exporter, target.tag, "target", instruction)
    kind = _KINDS[name]
    if name not in _SIDES:
        return refusals_of(lambda: OutVariant(kind, operand, immediate=immediate))
    values: dict[str, Any] = {}
    for side, acc, preset in _SIDES[name]:
        values[f"{side}_acc"] = render_operand(
            exporter, getattr(instruction, acc).tag, acc, instruction
        )
        values[f"{side}_preset"] = render_preset(
            exporter, getattr(instruction, preset), preset, instruction
        )
    unit = instruction.unit.name
    maintained = bool(getattr(instruction, "maintained", False))
    return refusals_of(
        lambda: OutVariant(kind, operand, unit=unit, maintained=maintained, **values)
    )


_ON_OFF_WORDS = {
    "target": "target",
    "on_acc": "on timer",
    "on_preset": "on preset",
    "off_acc": "off timer",
    "off_preset": "off preset",
}

FAMILY = Family(
    name="out_variants",
    classes={
        "ToggleInstruction": {"target": "target"},
        "DebounceInstruction": _ON_OFF_WORDS,
        "FlasherInstruction": _ON_OFF_WORDS,
        "TimedInstruction": {
            "target": "target",
            "timer_acc": "on timer",
            "on_preset": "on preset",
        },
    },
    build=_build,
    arguments=_FIELD_ARGUMENTS,
    advice=_advice,
    timer_pairs={
        "DebounceInstruction": (("on_done", "on_acc"), ("off_done", "off_acc")),
        "FlasherInstruction": (("on_done", "on_acc"), ("off_done", "off_acc")),
        "TimedInstruction": (("timer_done", "timer_acc"),),
    },
    replaced_codes={"CLK_OUT_UNIT_UNSUPPORTED": "coil-time-unit"},
    operands=_operands,
    call_text=_call,
)
