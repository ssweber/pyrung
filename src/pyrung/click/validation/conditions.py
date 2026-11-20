"""Find each contact that the CLICK export writes, with the line that holds it.

A contact is one leaf of a condition, such as ``X1``, ``~C2``, ``rise(C3)`` or
``DS1 > 5``. The export writes each leaf as one cell. Leaves sit in two kinds of place.

- The conditions of a rung, of a branch and of a loop body rung.
- The conditions of an instruction's pins, such as a counter's ``.reset()``.

The walk opens ``And`` and ``Or``. pyrung stores a text compare such as ``Txt[1] == "AB"``
as an ``And`` of one compare per character. The walk reads it back as one leaf, because the
export writes it as one cell. Each leaf's location has the ``arg_path`` that
:mod:`pyrung.core.validation.walker` gives the same leaf. Validation uses that path to find
the leaf that holds each of the walker's operand facts.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.condition import (
    AllCondition,
    AnyCondition,
    BitCondition,
    CompareEq,
    CompareGe,
    CompareGt,
    CompareLe,
    CompareLt,
    CompareNe,
    Condition,
    FallingEdgeCondition,
    IndirectCompareEq,
    IndirectCompareGe,
    IndirectCompareGt,
    IndirectCompareLe,
    IndirectCompareLt,
    IndirectCompareNe,
    NormallyClosedCondition,
    RisingEdgeCondition,
    text_window_of,
)
from pyrung.core.instruction import ForLoopInstruction
from pyrung.core.tag import ImmediateRef
from pyrung.core.validation.render import Call, operand_name, render_condition
from pyrung.core.validation.walker import ProgramLocation

if TYPE_CHECKING:
    from pyrung.core.program import Program
    from pyrung.core.rung import Rung

COMPARES = (CompareEq, CompareNe, CompareLt, CompareLe, CompareGt, CompareGe)
INDIRECT_COMPARES = (
    IndirectCompareEq,
    IndirectCompareNe,
    IndirectCompareLt,
    IndirectCompareLe,
    IndirectCompareGt,
    IndirectCompareGe,
)
CONTACTS = (BitCondition, NormallyClosedCondition, RisingEdgeCondition, FallingEdgeCondition)

_SYMBOLS: dict[type[Condition], str] = {
    CompareEq: "==",
    CompareNe: "!=",
    CompareLt: "<",
    CompareLe: "<=",
    CompareGt: ">",
    CompareGe: ">=",
    IndirectCompareEq: "==",
    IndirectCompareNe: "!=",
    IndirectCompareLt: "<",
    IndirectCompareLe: "<=",
    IndirectCompareGt: ">",
    IndirectCompareGe: ">=",
}


@dataclass(frozen=True)
class ConditionSite:
    """One leaf of a condition that the export writes as a cell."""

    leaf: Condition
    # The leaf's location. Its ``arg_path`` is the walker's path to the leaf, such as
    # ``condition[1].conditions[0]`` or ``instruction.reset_condition``.
    location: ProgramLocation
    # The line that a finding prints, such as ``with rung(X1, C1):``. Its arguments mark
    # the parts of this leaf. ``contact`` is the whole leaf, ``operand`` is the tag of a
    # contact, and ``left`` and ``right`` are the sides of a compare.
    line: Call
    # True for a leaf in a pin of an instruction, such as a counter's ``.reset()``.
    pin: bool = False


@dataclass(frozen=True)
class ConditionSites:
    """Each leaf that the export writes, and the other locations of some of them."""

    sites: tuple[ConditionSite, ...]
    # A branch holds its rung's conditions before its own. The walker may report a fact
    # about one of those conditions at the branch. Each key is such a location in a
    # branch. Its value is the location of the same leaf in the rung that holds the
    # condition as its own.
    aliases: dict[ProgramLocation, ProgramLocation]


def shown(condition: Condition) -> str:
    """Return *condition* as its author wrote it.

    It is :func:`~pyrung.core.validation.render.render_condition`, except in two places.
    A compare of a bit with True or False keeps its compare, such as ``C1 == True``, and
    an immediate contact shows ``immediate()``. The export writes both as written.
    """
    condition = text_window_of(condition)
    if isinstance(condition, (AllCondition, AnyCondition)):
        verb = "And" if isinstance(condition, AllCondition) else "Or"
        return f"{verb}({', '.join(shown(child) for child in condition.conditions)})"
    if isinstance(condition, COMPARES) and isinstance(condition.value, bool):
        symbol = _SYMBOLS[type(condition)]
        return f"{operand_name(condition.tag)} {symbol} {condition.value}"
    if isinstance(condition, (BitCondition, NormallyClosedCondition)) and isinstance(
        condition.tag, ImmediateRef
    ):
        not_ = "~" if isinstance(condition, NormallyClosedCondition) else ""
        return f"{not_}immediate({operand_name(condition.tag)})"
    return render_condition(condition)


# A renderer of a condition: shown or render_condition.
_Render = Callable[[Condition], str]


def leaf_call(
    leaf: Condition, text: str | None = None, start: int = 0, render: _Render = shown
) -> Call:
    """Return the call that marks the parts of *leaf*, which starts at *start* in *text*.

    *render* gives the text of *leaf* in *text*. With no *text*, the call is the leaf alone,
    such as ``DS1 > 5``.
    """
    own = render(leaf)
    line = own if text is None else text
    return Call(
        line,
        tuple((name, start + at, size, "positional") for name, at, size in _parts(leaf, own)),
    )


def _parts(leaf: Condition, own: str) -> list[tuple[str, int, int]]:
    """Return the ``(name, start, length)`` of each part of *leaf* in its text *own*."""
    parts = [("contact", 0, len(own))]
    if isinstance(leaf, (*COMPARES, *INDIRECT_COMPARES)):
        left = operand_name(leaf.tag if isinstance(leaf, COMPARES) else leaf.indirect_ref)
        right = shown_value(leaf.value)
        middle = f" {_SYMBOLS[type(leaf)]} "
        if own == f"{left}{middle}{right}":
            parts.append(("left", 0, len(left)))
            parts.append(("right", len(own) - len(right), len(right)))
        elif own.endswith(left):
            # render_condition writes ``C1 == True`` as the contact ``C1``.
            parts.append(("left", len(own) - len(left), len(left)))
        return parts
    if isinstance(leaf, CONTACTS):
        name = operand_name(leaf.tag)
        at = own.rfind(name)
        if at >= 0:
            parts.append(("operand", at, len(name)))
    return parts


def shown_value(value: Any) -> str:
    """Return the right side of a compare as :func:`shown` writes it."""
    return str(value) if isinstance(value, bool) else operand_name(value)


def _leaves(
    condition: Condition, path: str, start: int, render: _Render = shown
) -> Iterator[tuple[Condition, str, int]]:
    """Yield each leaf of *condition* with its path and its start in the line.

    *start* is the column where *condition* starts in the line. *render* gives the text of
    *condition* in the line. pyrung stores a text compare as an ``And`` of one compare per
    character. This walk reads it back as one leaf, because the export writes it as one
    cell. Its path is the path of that ``And``.
    """
    written = text_window_of(condition)
    if isinstance(written, (AllCondition, AnyCondition)):
        at = start + len("And(" if isinstance(written, AllCondition) else "Or(")
        for index, child in enumerate(written.conditions):
            yield from _leaves(child, f"{path}.conditions[{index}]", at, render)
            at += len(render(child)) + len(", ")
        return
    yield written, path, start


def _series(
    conditions: list[tuple[Condition, str]], prefix: str, suffix: str, render: _Render = shown
) -> Iterator[tuple[Condition, str, Call]]:
    """Yield each leaf of *conditions*, joined by commas between *prefix* and *suffix*.

    Each condition comes with its path. Each leaf comes with its path and its line.
    """
    text = prefix + ", ".join(render(c) for c, _path in conditions) + suffix
    at = len(prefix)
    for condition, path in conditions:
        for leaf, leaf_path, start in _leaves(condition, path, at, render):
            yield leaf, leaf_path, leaf_call(leaf, text, start, render)
        at += len(render(condition)) + len(", ")


def _pin_leaves(
    condition: Condition, path: str, call: Call, name: str
) -> Iterator[tuple[Condition, str, Call]]:
    """Yield each leaf of a pin's *condition*, inside the chained call *name* of *call*.

    The call shows the pin's condition as one argument, or one argument for each condition
    of an ``And``. The line shows each leaf as written. ``render_condition`` prints
    ``c[1] == True`` as the contact ``C1``. When neither form matches the call's text, each leaf marks the
    whole chained call.
    """
    span = call.span(name)
    if span is not None:
        text = call.text[span[0] : span[0] + span[1]]
        before, after = call.text[: span[0]], call.text[span[0] + span[1] :]
        written = text_window_of(condition)
        parts = (
            [(c, f"{path}.conditions[{i}]") for i, c in enumerate(written.conditions)]
            if isinstance(written, AllCondition)
            else []
        )
        for render in (shown, render_condition):
            if text == render(condition):
                yield from _series([(condition, path)], before, after)
                return
            if parts and text == ", ".join(render(c) for c, _p in parts):
                yield from _series(parts, before, after)
                return
    whole = () if span is None else (("contact", span[0], span[1], "positional"),)
    for leaf, leaf_path, _start in _leaves(condition, path, 0):
        yield leaf, leaf_path, Call(call.text, whole)


def condition_sites(program: Program, exporter: Any) -> ConditionSites:
    """Return each leaf of a condition that the export writes, in the order of the program.

    The pins are those that ``exporter._pin_specs`` gives, so validation reads the pins
    that the export writes.
    """
    from pyrung.click.ladder.types import _RenderError

    from .families import family_of

    sites: list[ConditionSite] = []
    aliases: dict[ProgramLocation, ProgramLocation] = {}
    # The walker numbers the loop body rungs of one rung, or of one branch, from 0.
    body_counts: dict[tuple[Any, ...], int] = {}

    def add(leaf: Condition, location: ProgramLocation, line: Call, pin: bool = False) -> None:
        sites.append(ConditionSite(leaf, location, line, pin))

    def pins(instruction: Any, location: ProgramLocation) -> None:
        try:
            specs = exporter._pin_specs(instruction, path="instruction")
        except _RenderError:
            # The export refuses the drum's jump step. The drum family reports it.
            return
        family = family_of(instruction)
        try:
            call = None if family is None else family.call_of(instruction)
        except TypeError:
            # The renderer cannot write the call, such as a counter whose Done bit and Acc
            # are two loose tags. Each contact of a pin then prints alone.
            call = None
        for name, condition, _token in specs:
            path = f"instruction.{name}_condition"
            if call is None:
                found = _series([(condition, path)], "", "")
            else:
                found = _pin_leaves(condition, path, call, name)
            for leaf, leaf_path, line in found:
                add(leaf, replace(location, arg_path=leaf_path), line, pin=True)

    def instructions(items: Any, base: ProgramLocation, index: int, site: tuple[Any, ...]) -> None:
        for item in items:
            pins(
                item,
                replace(
                    base,
                    instruction_index=index,
                    instruction_type=type(item).__name__,
                    arg_path="",
                ),
            )
            if isinstance(item, ForLoopInstruction):
                loop(item, base, index, site)

    def loop(
        instruction: ForLoopInstruction, base: ProgramLocation, index: int, site: tuple[Any, ...]
    ) -> None:
        for body_rung in instruction.body_rungs:
            number = body_counts.get(site, 0)
            body_counts[site] = number + 1
            local = body_rung._conditions[body_rung._branch_condition_start :]
            paths = [(c, f"body_rung[{number}].condition[{i}]") for i, c in enumerate(local)]
            for leaf, path, line in _series(paths, "with rung(", "):"):
                add(leaf, replace(base, arg_path=path), line)
        instructions(instruction.instructions, base, index, site)

    def condition_path(item: Rung, index: int) -> str:
        return "condition" if len(item._conditions) == 1 else f"condition[{index}]"

    def rung(
        item: Rung,
        base: ProgramLocation,
        verb: Literal["rung", "branch"],
        parents: tuple[tuple[Rung, ProgramLocation], ...] = (),
    ) -> None:
        conditions = item._conditions
        start = item._branch_condition_start
        paths = [(conditions[i], condition_path(item, i)) for i in range(start, len(conditions))]
        for leaf, path, line in _series(paths, f"with {verb}(", "):"):
            add(leaf, replace(base, arg_path=path), line)
        for index in range(start):
            # The owner is the nearest parent that holds this condition as its own.
            owner, owner_base = next(
                (parent, parent_base)
                for parent, parent_base in reversed(parents)
                if parent._branch_condition_start <= index
            )
            here = _leaves(conditions[index], condition_path(item, index), 0)
            there = _leaves(conditions[index], condition_path(owner, index), 0)
            for (_leaf, path, _start), (_same, owner_path, _at) in zip(here, there, strict=True):
                aliases[replace(base, arg_path=path)] = replace(owner_base, arg_path=owner_path)
        site = (base.scope, base.subroutine, base.rung_index, base.branch_path)
        for index, instruction in enumerate(item._instructions):
            instructions((instruction,), base, index, site)
        for index, branch in enumerate(item._branches):
            rung(
                branch,
                replace(base, branch_path=(*base.branch_path, index)),
                "branch",
                (*parents, (item, base)),
            )

    def top(
        scope: Literal["main", "subroutine"], subroutine: str | None, index: int
    ) -> ProgramLocation:
        return ProgramLocation(scope, subroutine, index, (), None, None, "")

    for index, item in enumerate(program.rungs):
        rung(item, top("main", None, index), "rung")
    for name in sorted(program.subroutines):
        for index, item in enumerate(program.subroutines[name]):
            rung(item, top("subroutine", name, index), "rung")
    return ConditionSites(tuple(sites), aliases)


__all__ = [
    "COMPARES",
    "CONTACTS",
    "INDIRECT_COMPARES",
    "ConditionSite",
    "ConditionSites",
    "condition_sites",
    "leaf_call",
    "shown",
]
