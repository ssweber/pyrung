"""CLICK's field rules for an instruction family, as laddercodec checks them.

For each instruction of a family in :mod:`pyrung.click.validation.families`, pyrung builds
the laddercodec instruction that the export writes. laddercodec refuses the values that
CLICK's dialog refuses. Each refusal becomes a finding whose code is laddercodec's code. The
finding prints the author's call with a caret under the refused argument. laddercodec's
short text labels the caret, and its explanation leads the hint. The family's advice follows
it, where the advice names a pyrung call or argument.

A family declares what differs from one family to the next in a :class:`Family`. This
module holds the rest, so that a family branch adds a module and edits nothing here. Where a
family's call does not fit the declarations on its classes, the family overrides the
``operands``, ``call_text`` or ``place`` hook of its :class:`Family` in its own module.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from laddercodec import Code, FieldError, LaddercodecError
from pyclickplc.addresses import format_address_display, parse_address

from pyrung.core.expression import Expression
from pyrung.core.memory_block import BlockRange, IndirectRef
from pyrung.core.tag import ImmediateRef, Tag
from pyrung.core.validation._common import instruction_frame
from pyrung.core.validation.display import FindingDisplay, Frame
from pyrung.core.validation.render import Call, declared_call, operand_name

from .findings import (
    CLK_COPY_RANGE_NOT_CONSECUTIVE,
    ClickFinding,
    FindingSeverity,
    RangeProblem,
    ValidationMode,
    _route_severity,
    range_not_consecutive_advice,
    range_not_consecutive_text,
)
from .resolve import (
    _format_location,
    _instruction_location,
    _ResolutionCache,
    _unresolved_finding,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pyrung.click.tag_map import TagMap
    from pyrung.core.validation.walker import ProgramLocation


@dataclass(frozen=True)
class Family:
    """One instruction family that laddercodec checks.

    Each module in :mod:`pyrung.click.validation.families` defines one, named ``FAMILY``.
    """

    name: str
    # Each pyrung instruction class of the family, by name. Each maps every instruction
    # attribute that holds an operand or a condition of the call to the plain word for it
    # in a CLK_BANK_UNRESOLVED finding, such as "destination".
    classes: Mapping[str, Mapping[str, str]]
    # Return laddercodec's refusals of the instruction that the export writes, from
    # ``build(instruction, exporter)``. Raise :class:`Unbuildable` when the export cannot
    # write it.
    build: Callable[[Any, Any], list[Any]]
    # The pyrung argument that holds each laddercodec field, such as ``"Copy.source"``. An
    # int is the index of a positional argument, and a str is the name of a keyword
    # argument or a chained call. A test checks that every field of the family's laddercodec
    # classes is here.
    arguments: Mapping[str, int | str]
    # Return pyrung's advice for a refusal, from ``advice(code, field, instruction,
    # resolver)``. The field is laddercodec's, such as ``"Search.result"``, so that one
    # code can take different advice on each field. The advice names a pyrung call or
    # argument. Return None to give none.
    advice: Callable[[str, str, Any, _ResolutionCache], str | None]
    # The operands of each class that hold a range of addresses. pyrung refuses a range
    # that CLICK cannot hold as a range with CLK_COPY_RANGE_NOT_CONSECUTIVE before it asks
    # laddercodec.
    ranges: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # The (Done, accumulator) attributes of each timer that a class names. A timer is one
    # number on CLICK, so CLK_TIMER_PAIR_MISMATCH reports a Done and an accumulator that
    # the tag map puts on different numbers.
    timer_pairs: Mapping[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    # Each CLK_ code that the family's move removed, with the codes that replace it.
    # ``allow=`` names them when a caller passes the old code.
    replaced_codes: Mapping[str, str] = field(default_factory=dict)
    # Return each argument of an instruction's call that holds tags, from
    # ``operands(instruction)``. The CLK_BANK_UNRESOLVED check, laddercodec's names and the
    # caret's label read it. None reads the declarations on the class, through
    # :func:`declared_operands`.
    operands: Callable[[Any], Sequence[Operand]] | None = None
    # Return the call that the family's findings print, from ``call_text(instruction)``.
    # None reads the declarations on the class, through
    # :func:`~pyrung.core.validation.render.declared_call`.
    call_text: Callable[[Any], Call] | None = None
    # Return the argument that a laddercodec refusal marks, from ``place(refusal)``. None
    # reads the refusal's field through :func:`default_place`.
    place: Callable[[Any], Place | None] | None = None
    # Return True if the export refuses a laddercodec refusal even when its code is allowed,
    # from ``export_ignores_allow(code, instruction)``. Warn mode then reports the refusal
    # as a warning. None means the export lets every refusal of the family through.
    export_ignores_allow: Callable[[str, Any], bool] | None = None

    def operands_of(self, instruction: Any) -> tuple[Operand, ...]:
        """Return each argument of the call of *instruction* that holds tags."""
        if self.operands is not None:
            return tuple(self.operands(instruction))
        return declared_operands(instruction, self.classes[type(instruction).__name__])

    def call_of(self, instruction: Any) -> Call:
        """Return the call of *instruction* that the family's findings print."""
        if self.call_text is not None:
            return self.call_text(instruction)
        return declared_call(instruction)

    def place_of(self, refusal: Any) -> Place | None:
        """Return the argument that laddercodec's *refusal* marks, or None if none holds it."""
        if self.place is not None:
            return self.place(refusal)
        return default_place(refusal.field, self.arguments)


@dataclass(frozen=True)
class Place:
    """The argument of a call that a refusal marks, and the item of a list argument."""

    # The argument as ``Family.arguments`` names it.
    argument: int | str
    # The index of the item in a list argument, such as 1 for ``Drum.outputs[1]``. None
    # marks the whole argument.
    item: int | None = None


# A field of one item of a list, such as ``Drum.outputs[1]``.
_ITEM_FIELD = re.compile(r"(.+)\[(\d+)\]")


def default_place(field: str | None, arguments: Mapping[str, int | str]) -> Place | None:
    """Return the argument in *arguments* that holds laddercodec's *field*, or None.

    laddercodec names one item of a list field with its index, such as ``Drum.outputs[1]``.
    Such a field marks item 1 of the argument that holds ``Drum.outputs``. A refusal of a
    pair, such as two outputs that are the same bit, names the first field of the pair.
    """
    if field is None:
        return None
    if field in arguments:
        return Place(arguments[field])
    match = _ITEM_FIELD.fullmatch(field)
    if match is None or match.group(1) not in arguments:
        return None
    return Place(arguments[match.group(1)], int(match.group(2)))


@dataclass(frozen=True)
class Operand:
    """One argument of an instruction's call that holds tags, ranges or a condition."""

    # The argument as ``Family.arguments`` names it: the index of a positional argument,
    # or the name of a keyword argument or a chained call.
    argument: int | str
    # The plain word for the argument in a CLK_BANK_UNRESOLVED finding, such as "source".
    word: str
    # Each instruction attribute that the argument holds, with its value. A finding's
    # location names the attribute.
    values: tuple[tuple[str, Any], ...]
    # Each tag of the argument with the name that the call spells for it, where the value
    # is a range that the call wrote as one tag. With None, ``tags()`` reads the tags from
    # the values.
    spelled: tuple[tuple[Tag, str], ...] | None = None

    @property
    def attributes(self) -> tuple[str, ...]:
        """The instruction attributes that the argument holds."""
        return tuple(attribute for attribute, _value in self.values)

    def tags(self) -> list[tuple[Tag, str]]:
        """Return each tag that the argument names, with the name that the call spells."""
        if self.spelled is not None:
            return list(self.spelled)
        return [found for _attribute, value in self.values for found in _value_tags(value)]


def declared_operands(instruction: Any, words: Mapping[str, str]) -> tuple[Operand, ...]:
    """Return the operands that the class of *instruction* declares for its call.

    These are the positional arguments, the keyword arguments that hold an operand, and
    the conditions of the chained calls. *words* maps an attribute to its plain word, and
    an attribute with no word takes the argument's name. An argument that holds nothing,
    such as a chained call that the author left off, is not there.
    """
    cls = type(instruction)
    found: list[Operand] = []

    def add(argument: int | str, name: str, attributes: tuple[str, ...]) -> None:
        values = tuple(
            (attribute, getattr(instruction, attribute, None)) for attribute in attributes
        )
        if any(value is not None for _attribute, value in values):
            spelled = _authored_range_tags(instruction, *values[0]) if len(values) == 1 else None
            found.append(Operand(argument, words.get(attributes[0], name), values, spelled))

    for index, (name, attribute) in enumerate(cls._call_operands or ()):
        add(index, name, _attributes(attribute))
    for name, attribute in (*cls._call_keyword_operands, *cls._call_chained):
        add(name, name, (attribute,))
    return tuple(found)


def _authored_range_tags(
    instruction: Any, attribute: str, value: Any
) -> tuple[tuple[Tag, str], ...] | None:
    """Return the tags of a range that the call wrote as one tag, or None.

    A blockcopy() into one tag writes the range that starts at that tag, and keeps the tag
    in ``authored_dest``. The range's first address takes that tag's name. Each later
    address takes the name of its own tag, such as ``X003``. Where the first name spells
    its own address, as ``x1`` spells X001, each later name takes the same form, such as
    ``x3``. So the two ends of the range read alike.
    """
    authored = getattr(instruction, f"authored_{attribute}", None)
    if not isinstance(authored, Tag) or not isinstance(value, BlockRange):
        return None
    first = operand_name(authored)
    start = f"{value.block.name}{value.addresses[0]}"
    form = _ADDRESS_FORM.fullmatch(first)
    tags = [(value.block[address], address) for address in value.addresses[1:]]
    if form is None or not _spells(first, start):
        return ((authored, first), *((tag, operand_name(tag)) for tag, _address in tags))
    letters, digits = form.groups()
    width = len(digits) if digits.startswith("0") else 0
    return ((authored, first), *((tag, f"{letters}{address:0{width}d}") for tag, address in tags))


# A name in the form of a CLICK address, such as ``x1`` or ``X001``.
_ADDRESS_FORM = re.compile(r"([A-Za-z]+)(\d+)")


class Unbuildable(Exception):
    """Raised when the export cannot write the instruction, so laddercodec has no input."""


class Unreadable(Exception):
    """Raised when laddercodec cannot read the token that the export writes for the call.

    Carries laddercodec's error. Such an error is about the shape of the token, so it has no
    field to mark.
    """

    def __init__(self, error: LaddercodecError) -> None:
        super().__init__(str(error))
        self.error = error


def render_operand(exporter: Any, value: Any, attribute: str, instruction: Any) -> str:
    """Return the operand in *attribute* of *instruction* as the export writes it.

    Raise :class:`Unbuildable` when the export refuses it.
    """
    from pyrung.click.ladder.types import _RenderError

    if isinstance(value, Expression):
        # CLK_EXPR_ONLY_IN_MATH reports an expression outside math().
        raise Unbuildable
    try:
        return exporter._render_operand(value, path=f"instruction.{attribute}", source=instruction)
    except _RenderError as exc:
        if isinstance(value, str) and '"' in value:
            # Only a Fill refuses a quote in text. It hands laddercodec the quote doubled,
            # which laddercodec refuses in its own words.
            return '"{}"'.format(value.replace('"', '""'))
        raise Unbuildable from exc


def split_range(text: str) -> tuple[str, str]:
    """Return the start and end of a range that the export wrote, such as ``DS1..DS5``."""
    start, sep, end = text.partition("..")
    if not sep:
        # A range that is not one run of CLICK addresses, such as ``[DS1,DS5]``, has a
        # finding of its own.
        raise Unbuildable
    return start, end


def refusals_of(build: Callable[[], Any]) -> list[Any]:
    """Return laddercodec's refusals of the instruction that *build* constructs."""
    try:
        build()
    except FieldError as exc:
        return list(exc.errors)
    except LaddercodecError as exc:
        raise Unreadable(exc) from exc
    return []


def memory_types(operand: Any, resolver: _ResolutionCache) -> set[str]:
    """Return the CLICK banks that *operand* maps to."""
    return {slot.memory_type for slot in resolver.resolve_operand_slots(operand).slots}


def _range_text(exporter: Any, value: Any, path: str, instruction: Any) -> tuple[str, Any]:
    """Return a range as the export writes it, and its problem, as ``_range_text`` does."""
    from pyrung.click.ladder.types import _RenderError

    try:
        return exporter._range_text(value, path=path, instruction=instruction)
    except _RenderError as exc:
        raise Unbuildable from exc


def _broken_ranges(instruction: Any, exporter: Any, family: Family) -> list[RangeProblem]:
    """Return each range operand that CLICK cannot hold as a range.

    The export would write such a range as a list, such as ``[DS1,DS5]`` or ``[]``. CLICK
    has no list form, so no CLICK project can hold it.
    """
    problems: list[RangeProblem] = []
    for attribute in family.ranges.get(type(instruction).__name__, ()):
        value = getattr(instruction, attribute)
        if isinstance(value, ImmediateRef):
            # An immediate() range has the same list form, and the export refuses it too.
            value = value.value
        try:
            _text, problem = _range_text(exporter, value, f"instruction.{attribute}", instruction)
        except Unbuildable:
            continue
        if problem is not None:
            problems.append(problem)
    return problems


def _operand_tags(operand: Any) -> list[tuple[Tag, str]]:
    """Return each tag that *operand* names, with the name that the call spells for it.

    A pointer such as ``DS[Ptr]`` names ``Ptr``. A range such as ``X1..X16`` spells each
    tag by the block's name and the tag's address.
    """
    if isinstance(operand, ImmediateRef):
        operand = operand.value
    if isinstance(operand, IndirectRef):
        return [(operand.pointer, operand_name(operand.pointer))]
    if isinstance(operand, Tag):
        return [(operand, operand_name(operand))]
    if isinstance(operand, BlockRange):
        name = operand.block.name
        return [(operand.block[addr], f"{name}{addr}") for addr in operand.addresses]
    return []


def _leaves(value: Any) -> list[Any]:
    """Return the operands that *value* reads: itself, or each operand of a condition."""
    from pyrung.core.condition import Condition
    from pyrung.core.validation.walker import _condition_children

    if not isinstance(value, Condition):
        return [value]
    return [leaf for _name, child in _condition_children(value) for leaf in _leaves(child)]


def _value_tags(value: Any) -> list[tuple[Tag, str]]:
    """Return each tag that an operand or a condition names, with its name in the call."""
    return [found for leaf in _leaves(value) for found in _operand_tags(leaf)]


def _unresolved(value: Any, resolver: _ResolutionCache) -> bool:
    """Return True if an operand or a condition names a tag that has no CLICK address."""
    return any(resolver.resolve_operand_slots(leaf).unresolved for leaf in _leaves(value))


def _attributes(attribute: str | tuple[str, ...]) -> tuple[str, ...]:
    """Return the attributes of an argument that holds one attribute or several."""
    return (attribute,) if isinstance(attribute, str) else attribute


def _operand_of(operands: Sequence[Operand], argument: int | str) -> Operand | None:
    """Return the operand of *argument*, or None for a flag, an option or a constant."""
    return next((operand for operand in operands if operand.argument == argument), None)


def _address_text(address: str) -> str:
    """Return a CLICK address in CLICK's form, such as ``X001`` for ``X1``."""
    return format_address_display(*parse_address(address))


def _tag_address(tag: Tag, resolver: _ResolutionCache) -> str | None:
    """Return the CLICK address of *tag* in CLICK's form, or None if it has none."""
    slot = resolver.resolve_direct_tag(tag)
    if slot is None or slot.address is None:
        return None
    return format_address_display(slot.memory_type, slot.address)


def tag_names(tag_map: TagMap) -> dict[str, str]:
    """Return the name of each mapped CLICK address, for laddercodec's text.

    A tag of the program is named as a call spells it, such as ``Motor.speed`` for a field
    of a structure.
    """
    names = {slot.hardware_address: slot.logical_name for slot in tag_map.mapped_slots()}
    for entry in tag_map.tags():
        names[_address_text(tag_map.resolve(entry.logical))] = operand_name(entry.logical)
    for block in tag_map.blocks():
        for index in block.logical_addresses:
            address = _address_text(tag_map.resolve(block.logical, index))
            names[address] = operand_name(block.logical[index])
    return names


def _call_names(
    operands: Sequence[Operand], names: Mapping[str, str], resolver: _ResolutionCache
) -> dict[str, str]:
    """Return *names*, with each address of *operands* named as the call spells it.

    laddercodec quotes every address when it gets no names, so each address of the call
    has an entry. laddercodec shows a name that spells its own address, such as ``X1``
    for ``X001``, with no address in brackets after it.
    """
    call = dict(names)
    for tag, name in (found for operand in operands for found in operand.tags()):
        address = _tag_address(tag, resolver)
        if address is not None:
            call[address] = name
    return call


def _spells(name: str, address: str) -> bool:
    """Return True if *name* spells the CLICK address *address*, as ``X1`` spells ``X001``."""
    try:
        return parse_address(name) == parse_address(address)
    except ValueError:
        return False


def _sentence(text: str) -> str:
    """Return a phrase of laddercodec's as a sentence, with a capital and a full stop.

    A phrase that starts with a name, such as ``to_value``, keeps its case.
    """
    first_word = text.split(" ", 1)[0]
    if first_word.isalpha() and first_word.islower():
        text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else f"{text}."


@dataclass(frozen=True)
class _Refusal:
    """One laddercodec refusal, ready to become a finding once all advice is known."""

    code: str
    severity: FindingSeverity
    message: str
    location: str
    frame: Frame
    problem: str
    detail: str
    click_id: str | None
    advice: str | None
    # The instruction attributes that hold the refused argument, or the name of a refused
    # flag, option or constant. It is empty for a field that no argument holds.
    attributes: tuple[str, ...]

    def finding(self, extra_advice: Sequence[str] = ()) -> ClickFinding:
        """Return the finding. Advice in *extra_advice* replaces the family's advice."""
        advice = " ".join(extra_advice) if extra_advice else self.advice
        hint = [_sentence(self.detail)] if self.detail else []
        if advice:
            hint.append(advice)
        if self.click_id and self.click_id not in f"{self.problem} {self.detail}":
            hint.append(f"CLICK's error is {self.click_id}.")
        return ClickFinding(
            code=self.code,
            severity=self.severity,
            message=self.message,
            location=self.location,
            suggestion=advice,
            display=FindingDisplay(
                code=self.code,
                severity=self.severity,
                frames=(self.frame,),
                problem=self.problem,
                hint=" ".join(hint),
            ),
        )


@dataclass(frozen=True)
class FamilyResult:
    """What validation found in one instruction of a family.

    ``checks`` holds the findings of pyrung's own checks, and ``refusals`` holds
    laddercodec's refusals.
    """

    checks: list[ClickFinding]
    refusals: tuple[_Refusal, ...] = ()

    @property
    def codes(self) -> frozenset[str]:
        """The codes of laddercodec's refusals."""
        return frozenset(refusal.code for refusal in self.refusals)

    def findings(
        self, pointer_advice: Mapping[tuple[str, str], Sequence[str]] | None = None
    ) -> list[ClickFinding]:
        """Return every finding, with one finding for each laddercodec refusal.

        *pointer_advice* maps a code and an instruction attribute to the advice of the
        pyrung findings that a refusal of that code replaced. The refusal of that operand
        takes the advice. If no refusal of the code is about that operand, the first
        refusal of the code takes it.
        """
        extra: dict[int, list[str]] = {}
        for (code, attribute), advice in (pointer_advice or {}).items():
            same_code = [i for i, r in enumerate(self.refusals) if r.code == code]
            on_operand = [i for i in same_code if attribute in self.refusals[i].attributes]
            for i in on_operand or same_code[:1]:
                taken = extra.setdefault(i, [])
                taken.extend(text for text in advice if text not in taken)
        return [
            *self.checks,
            *(refusal.finding(extra.get(i, ())) for i, refusal in enumerate(self.refusals)),
        ]


def _refusal(
    refusal: Any,
    family: Family,
    instruction: Any,
    base_location: ProgramLocation,
    location_text: str,
    mode: ValidationMode,
    resolver: _ResolutionCache,
    names: Mapping[str, str],
) -> _Refusal:
    """Return laddercodec's *refusal* with its frame, its caret and its texts."""
    code = str(refusal.code)
    place = family.place_of(refusal)
    argument = None if place is None else place.argument
    operands = family.operands_of(instruction)
    operand = None if argument is None else _operand_of(operands, argument)
    if operand is not None:
        attributes = operand.attributes
    else:
        attributes = (argument,) if isinstance(argument, str) else ()
    call = family.call_of(instruction)
    item = None if place is None else place.item
    frame = instruction_frame(base_location, instruction, argument=argument, item=item, call=call)
    call_names = _call_names(operands, names, resolver)
    text = refusal.message(call_names)
    # laddercodec gives no detail for an instruction it has no rules for yet. The short
    # text is then the whole message, so it stays as the problem line.
    detail = refusal.detail(call_names) if frame.caret is not None else ""
    if detail:
        # The caret marks the operand, so the label gives the addresses of its tags and not
        # their names. A name that spells its own address, such as X1 in X1..X3 or a tag
        # named ds1 on DS1, stays as the call spells it.
        own = {
            address: name if _spells(name, address) else address
            for tag, name in (operand.tags() if operand is not None else ())
            if (address := _tag_address(tag, resolver)) is not None
        }
        frame = replace(frame, caret_label=refusal.short({**call_names, **own}))
        problem = ""
    else:
        problem = text
    message = f"CLICK refuses {call.text} at {location_text}. {text}"
    if refusal.click_id and refusal.click_id not in text:
        message += f" CLICK's error is {refusal.click_id}."
    severity = _route_severity(code, mode)
    if (
        severity == "hint"
        and family.export_ignores_allow is not None
        and family.export_ignores_allow(code, instruction)
    ):
        severity = "warning"
    return _Refusal(
        code=code,
        severity=severity,
        message=message,
        location=location_text,
        frame=frame,
        problem=problem,
        detail=detail,
        click_id=refusal.click_id,
        advice=family.advice(code, str(refusal.field), instruction, resolver),
        attributes=attributes,
    )


def evaluate_family(
    instruction: Any,
    base_location: ProgramLocation,
    exporter: Any,
    mode: ValidationMode,
    resolver: _ResolutionCache,
    names: Mapping[str, str],
) -> FamilyResult:
    """Report each value of *instruction* that laddercodec refuses.

    An instruction that no family covers has no findings here. *names* maps CLICK
    addresses to tag names for laddercodec's text. :func:`tag_names` builds it.
    """
    from .families import family_of

    family = family_of(instruction)
    if family is None:
        return FamilyResult([])

    # One finding for each argument, at the first of its attributes that has no address.
    unresolved: list[ClickFinding] = []
    for operand in family.operands_of(instruction):
        missing = [a for a, value in operand.values if _unresolved(value, resolver)]
        if missing:
            location = _instruction_location(base_location, f"instruction.{missing[0]}")
            unresolved.append(_unresolved_finding(location, mode, operand.word))
    if unresolved:
        return FamilyResult(unresolved)

    location_text = _format_location(_instruction_location(base_location, "instruction"))
    broken = _broken_ranges(instruction, exporter, family)
    if broken:
        finding = ClickFinding(
            code=CLK_COPY_RANGE_NOT_CONSECUTIVE,
            severity=_route_severity(CLK_COPY_RANGE_NOT_CONSECUTIVE, mode),
            message=" ".join(
                [
                    f"CLICK refuses {family.call_of(instruction).text} at {location_text}.",
                    range_not_consecutive_text(broken),
                ]
            ),
            location=location_text,
            suggestion=range_not_consecutive_advice(broken),
        )
        return FamilyResult([finding])

    try:
        refusals = family.build(instruction, exporter)
    except Unbuildable:
        # laddercodec has nothing to read. An expression operand, or an operand that the
        # export refuses, has a finding of its own.
        return FamilyResult([])
    except Unreadable as exc:
        code = str(getattr(exc.error, "code", None) or Code.INVALID_TOKEN)
        return FamilyResult(
            [
                ClickFinding(
                    code=code,
                    severity=_route_severity(code, mode),
                    message=f"CLICK cannot read {family.call_of(instruction).text} at "
                    f"{location_text}. {exc.error.message()}",
                    location=location_text,
                    suggestion=None,
                )
            ]
        )

    return FamilyResult(
        [],
        tuple(
            _refusal(r, family, instruction, base_location, location_text, mode, resolver, names)
            for r in refusals
        ),
    )


__all__ = [
    "Family",
    "FamilyResult",
    "Operand",
    "Place",
    "Unbuildable",
    "Unreadable",
    "declared_operands",
    "default_place",
    "evaluate_family",
    "memory_types",
    "refusals_of",
    "render_operand",
    "split_range",
    "tag_names",
]
