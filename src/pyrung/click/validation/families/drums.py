"""The Drum family: event_drum(), time_drum() and time_event_drum().

laddercodec's Drum checks all three. The call holds lists of tags and conditions, and its
``.jump(cond, step=...)`` holds a step inside a chained call. So the family overrides
``operands`` and ``call_text``, which the declarations of a class cannot express.
"""

from __future__ import annotations

from typing import Any, assert_never

from laddercodec import Drum

from pyrung.core.condition import AllCondition, Condition
from pyrung.core.instruction.operands import ConstArg, Preset, TagArg
from pyrung.core.validation.render import Call, call_of, operand_name, render_condition

from ..fields import (
    Family,
    Operand,
    Unbuildable,
    refusals_of,
    render_operand,
    render_preset,
)
from ..resolve import _ResolutionCache

# laddercodec's Drum kind for each pyrung class. The kind is also the name of the call.
_KINDS: dict[str, str] = {
    "EventDrumInstruction": "event_drum",
    "TimeDrumInstruction": "time_drum",
    "TimeEventDrumInstruction": "time_event_drum",
}

# The argument of the pyrung call that holds each field of laddercodec's Drum. The reset,
# jump and jog are chained calls, and the jump holds its step as a keyword of its own.
# Drum.pin_rows is refused for the whole pin layout, so it marks `.reset()`, the first pin.
_FIELD_ARGUMENTS: dict[str, int | str] = {
    "Drum.outputs": "outputs",
    "Drum.presets": "presets",
    "Drum.events": "events",
    "Drum.pattern": "pattern",
    "Drum.current_step": "current_step",
    "Drum.completion_flag": "completion_flag",
    "Drum.accumulator": "accumulator",
    "Drum.unit": "unit",
    "Drum.jog_enabled": "jog",
    "Drum.jump_enabled": "jump",
    "Drum.jump_step": "jump_step",
    "Drum.pin_rows": "reset",
}

# The plain word for each attribute of the call that holds a tag or a condition.
_WORDS: dict[str, str] = {
    "outputs": "drum output",
    "events": "drum event",
    "presets": "drum preset",
    "current_step": "current step",
    "accumulator": "accumulator",
    "completion_flag": "completion flag",
    "jump_step": "jump step",
    "reset_condition": "reset",
    "jump_condition": "jump",
    "jog_condition": "jog",
}

# The chained calls, with the attribute that holds the condition of each.
_PINS: tuple[tuple[str, str], ...] = (
    ("reset", "reset_condition"),
    ("jump", "jump_condition"),
    ("jog", "jog_condition"),
)

# pyrung's advice for a refusal of a field, where the advice names pyrung's own calls or
# arguments.
_ADVICE: dict[tuple[str, str], str] = {
    ("not-an-address", "Drum.jump_step"): "Copy the step into a DS tag and jump to that tag.",
    ("not-an-address", "Drum.events"): (
        "Copy the condition into a C bit and use that bit as the event."
    ),
}


def _advice(code: str, field: str, instruction: Any, resolver: _ResolutionCache) -> str | None:
    # A list item's field carries its index, such as Drum.events[1].
    return _ADVICE.get((code, field.partition("[")[0]))


def _events(instruction: Any) -> tuple[Any, ...]:
    return tuple(getattr(instruction, "events", ()))


def _presets(instruction: Any) -> tuple[Preset | None, ...]:
    return tuple(getattr(instruction, "presets", ()))


def _preset_tags(preset: Preset | None) -> list[Any]:
    """Return the tag that a step's preset or the jump's step names, if it names one."""
    match preset:
        case TagArg(tag=tag):
            return [tag]
        case ConstArg() | None:
            return []
        case _:
            assert_never(preset)


def _operands(instruction: Any) -> list[Operand]:
    """Return each argument of the drum's call that holds tags, with each tag as a value.

    A list argument gives each of its tags a value of its own, so that laddercodec names
    each address as the call spells it.
    """
    found: list[Operand] = []

    def add(argument: str, attribute: str, values: list[Any]) -> None:
        if values:
            word = _WORDS[attribute]
            found.append(Operand(argument, word, tuple((attribute, value) for value in values)))

    add("outputs", "outputs", list(instruction.output_tags))
    add("events", "events", [event for event in _events(instruction) if event is not None])
    add("presets", "presets", [tag for p in _presets(instruction) for tag in _preset_tags(p)])
    add("current_step", "current_step", [instruction.current_step.tag])
    accumulator = getattr(instruction, "accumulator", None)
    add("accumulator", "accumulator", [] if accumulator is None else [accumulator.tag])
    add("completion_flag", "completion_flag", [instruction.completion_flag.tag])
    for name, attribute in _PINS:
        condition = getattr(instruction, attribute)
        if condition is not None:
            add(name, attribute, [condition])
    add("jump_step", "jump_step", _preset_tags(instruction.jump_step))
    return found


def _item_text(value: Any) -> str:
    """Return a step's event or preset as the author wrote it."""
    if value is None:
        return "None"
    if isinstance(value, Condition):
        return render_condition(value)
    return operand_name(value)


def _pin_text(condition: Condition) -> str:
    """Return a chained call's conditions as the author passed them, one argument each."""
    if isinstance(condition, AllCondition):
        return ", ".join(render_condition(part) for part in condition.conditions)
    return render_condition(condition)


def _call(instruction: Any) -> Call:
    """Return the drum as the author wrote it, with the place of each argument and item."""
    kind = _KINDS[type(instruction).__name__]
    timed = kind != "event_drum"
    pattern = "[" + ", ".join(
        f"[{', '.join(str(int(c)) for c in row)}]" for row in instruction.pattern
    )
    arguments: list[tuple[str, Any, str]] = [
        ("outputs", [operand_name(tag) for tag in instruction.output_tags], "keyword")
    ]
    if timed:
        arguments.append(("presets", [_item_text(p) for p in instruction.presets], "keyword"))
    if kind != "time_drum":
        arguments.append(("events", [_item_text(e) for e in instruction.events], "keyword"))
    if timed:
        arguments.append(("unit", operand_name(instruction.unit.value), "keyword"))
    arguments.append(("pattern", pattern + "]", "keyword"))
    arguments.append(("current_step", operand_name(instruction.current_step.tag), "keyword"))
    if timed:
        arguments.append(("accumulator", operand_name(instruction.accumulator.tag), "keyword"))
    arguments.append(("completion_flag", operand_name(instruction.completion_flag.tag), "keyword"))
    head = call_of(kind, arguments)

    # call_of writes no step inside a chained call, so the chained calls are added here.
    text = head.text
    spans = list(head.arguments)
    for name, attribute in _PINS:
        condition = getattr(instruction, attribute)
        if condition is None:
            continue
        text += f".{name}("
        condition_text = _pin_text(condition)
        spans.append((name, len(text), len(condition_text), "chained"))
        text += condition_text
        if name == "jump":
            text += ", step="
            step_text = operand_name(instruction.jump_step)
            spans.append(("jump_step", len(text), len(step_text), "chained"))
            text += step_text
        text += ")"
    items = (*head.items, *(() for _ in spans[len(head.arguments) :]))
    return Call(text, tuple(spans), items)


def _build(instruction: Any, exporter: Any) -> list[Any]:
    """Return laddercodec's refusals of the Drum that the export writes."""
    from pyrung.click.ladder.types import _RenderError

    kind = _KINDS[type(instruction).__name__]

    def render(value: Any, attribute: str) -> str:
        return render_operand(exporter, value, attribute, instruction)

    # A step's blank preset is the empty text.
    presets = tuple(
        "" if preset is None else render_preset(exporter, preset, f"presets[{index}]", instruction)
        for index, preset in enumerate(_presets(instruction))
    )

    events: list[str] = []
    for index, event in enumerate(_events(instruction)):
        if event is None:
            events.append("")
            continue
        try:
            events.append(
                exporter._render_condition_inline(
                    event, path=f"instruction.events[{index}]", source=instruction
                )
            )
        except _RenderError as exc:
            raise Unbuildable from exc
    jumps = instruction.jump_condition is not None
    accumulator = getattr(instruction, "accumulator", None)
    unit = getattr(instruction, "unit", None)
    outputs = tuple(
        render(tag, f"outputs[{index}]") for index, tag in enumerate(instruction.output_tags)
    )
    current_step = render(instruction.current_step.tag, "current_step")
    completion_flag = render(instruction.completion_flag.tag, "completion_flag")
    accumulator_text = "" if accumulator is None else render(accumulator.tag, "accumulator")
    unit_text = "" if unit is None else render(unit, "unit")
    jump_step = (
        render_preset(exporter, instruction.jump_step, "jump_step", instruction) if jumps else ""
    )
    pattern = tuple(tuple(int(cell) for cell in row) for row in instruction.pattern)
    fields: dict[str, Any] = {
        "kind": kind,
        "outputs": outputs,
        "presets": presets,
        "events": tuple(events),
        "pattern": pattern,
        "current_step": current_step,
        "completion_flag": completion_flag,
        "accumulator": accumulator_text,
        "unit": unit_text,
        "jog_enabled": instruction.jog_condition is not None,
        "jump_enabled": jumps,
        "jump_step": jump_step,
    }
    return refusals_of(lambda: Drum(**fields))


def _words(*attributes: str) -> dict[str, str]:
    return {attribute: _WORDS[attribute] for attribute in attributes}


_COMMON = _words(
    "outputs",
    "current_step",
    "completion_flag",
    "jump_step",
    "reset_condition",
    "jump_condition",
    "jog_condition",
)

FAMILY = Family(
    name="drums",
    classes={
        "EventDrumInstruction": _COMMON | _words("events"),
        "TimeDrumInstruction": _COMMON | _words("presets", "accumulator"),
        "TimeEventDrumInstruction": _COMMON | _words("events", "presets", "accumulator"),
    },
    build=_build,
    arguments=_FIELD_ARGUMENTS,
    advice=_advice,
    operands=_operands,
    call_text=_call,
    replaced_codes={
        "CLK_DRUM_JUMP_STEP_TAG_REQUIRED": "not-an-address",
        "CLK_TIMER_PRESET_TOO_LOW": "constant-out-of-range",
    },
)
