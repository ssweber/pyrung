"""CLICK's field rules for the Copy family, as laddercodec checks them.

For each copy(), blockcopy(), fill(), pack and unpack instruction, pyrung builds the
laddercodec instruction that the export writes. laddercodec refuses the values that CLICK's
dialog refuses. Each refusal becomes a finding whose code is laddercodec's code. The
finding prints the author's call with a caret under the refused argument. laddercodec's
short text labels the caret, and its explanation leads the hint. pyrung's advice follows it,
where the advice names a pyrung call or argument.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from laddercodec import BlockCopy, Copy, FieldError, Fill, Pack, Unpack
from pyclickplc.addresses import format_address_display, parse_address

from pyrung.click.capabilities import single_register_fill_dest
from pyrung.core.expression import Expression
from pyrung.core.memory_block import BlockRange, IndirectRef
from pyrung.core.tag import ImmediateRef, Tag, TagType
from pyrung.core.validation._common import instruction_frame
from pyrung.core.validation.display import FindingDisplay, Frame
from pyrung.core.validation.render import operand_name, render_instruction

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
from .hardware import constant_text_past_bank_advice
from .resolve import (
    _format_location,
    _instruction_location,
    _ResolutionCache,
    _unresolved_finding,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pyrung.click.tag_map import TagMap
    from pyrung.core.validation.walker import ProgramLocation

# Each pyrung instruction of the Copy family, with its source and destination attributes.
_COPY_FAMILY_FIELDS: dict[str, tuple[str, str]] = {
    "CopyInstruction": ("source", "dest"),
    "BlockCopyInstruction": ("source", "dest"),
    "FillInstruction": ("value", "dest"),
    "PackBitsInstruction": ("bit_block", "dest"),
    "PackWordsInstruction": ("word_block", "dest"),
    "PackTextInstruction": ("source_range", "dest"),
    "UnpackToBitsInstruction": ("source", "bit_block"),
    "UnpackToWordsInstruction": ("source", "word_block"),
}

# The argument of the pyrung call that holds each field of laddercodec's Copy family. An
# int is the index of a positional argument, and a str is the name of a keyword argument.
# The export writes a fill() of one register as a Copy, so Copy.source is then the value
# of fill().
_FIELD_ARGUMENTS: dict[str, int | str] = {
    "Copy.source": 0,
    "Copy.dest": 1,
    "Copy.convert": "convert",
    "Copy.suppress_zero": "convert",
    "Copy.exponential": "convert",
    "Copy.termination_code": "convert",
    "Copy.oneshot": "oneshot",
    "BlockCopy.source_start": 0,
    "BlockCopy.source_end": 0,
    "BlockCopy.dest_start": 1,
    "BlockCopy.dest_end": 1,
    "BlockCopy.convert": "convert",
    "BlockCopy.termination_code": "convert",
    "BlockCopy.oneshot": "oneshot",
    "Fill.value": 0,
    "Fill.dest_start": 1,
    "Fill.dest_end": 1,
    "Fill.oneshot": "oneshot",
    "Pack.source_start": 0,
    "Pack.source_end": 0,
    "Pack.dest": 1,
    "Pack.allow_whitespace": "allow_whitespace",
    "Pack.oneshot": "oneshot",
    "Unpack.source": 0,
    "Unpack.dest_start": 1,
    "Unpack.dest_end": 1,
    "Unpack.oneshot": "oneshot",
}

# The fields of each Copy family instruction that hold a range of addresses.
_RANGE_FIELDS: dict[str, tuple[str, ...]] = {
    "BlockCopyInstruction": ("source", "dest"),
    "FillInstruction": ("dest",),
    "PackBitsInstruction": ("bit_block",),
    "PackWordsInstruction": ("word_block",),
    "PackTextInstruction": ("source_range",),
    "UnpackToBitsInstruction": ("bit_block",),
    "UnpackToWordsInstruction": ("word_block",),
}

_PACK_KINDS = {
    "PackBitsInstruction": "pack_bits",
    "PackWordsInstruction": "pack_words",
    "PackTextInstruction": "pack_text",
}
_UNPACK_KINDS = {
    "UnpackToBitsInstruction": "unpack_to_bits",
    "UnpackToWordsInstruction": "unpack_to_words",
}

# pyrung's advice for a code, where the advice names pyrung's own calls or arguments.
_ADVICE: dict[str, str] = {
    "constant-into-bit": "Set or clear a bit with out(), latch() or reset(), or copy a bit into it.",
    "copy-converter-bank": (
        "to_text() and to_binary read a number register and write TXT. to_value and "
        "to_ascii read TXT and write a number register."
    ),
    "pointer-outside-copy": "Copy the value through the pointer with copy() in a rung above.",
}

_BIT_BANKS = frozenset({"X", "Y", "C", "T", "CT", "SC"})


def _memory_types(operand: Any, resolver: _ResolutionCache) -> set[str]:
    return {slot.memory_type for slot in resolver.resolve_operand_slots(operand).slots}


def _advice(code: str, instruction: Any, resolver: _ResolutionCache) -> str | None:
    kind = type(instruction).__name__
    if code == "text-past-bank":
        return constant_text_past_bank_advice(instruction, resolver)
    if code == "option-not-offered":
        converter = getattr(instruction, "convert", None)
        if converter is not None and converter.exponential:
            return "Remove exponential=True from to_text(). Only a Real source takes it."
        return None
    if code == "range-not-ascending" and kind == "BlockCopyInstruction":
        return "Use copy() for one register."
    if (
        code == "bank-not-allowed"
        and kind == "FillInstruction"
        and _memory_types(instruction.dest, resolver) & _BIT_BANKS
    ):
        if _memory_types(instruction.value, resolver):
            return "Copy the bit into each destination bit with copy()."
        return "Set or clear the bits with latch() or reset() on the range."
    if (
        code == "bank-not-allowed"
        and kind == "BlockCopyInstruction"
        and "YD" in _memory_types(instruction.dest, resolver)
    ):
        return "Write each YD with copy() or math()."
    return _ADVICE.get(code)


class _Unbuildable(Exception):
    """Raised when the export cannot write the instruction, so laddercodec has no input."""


def _render(exporter: Any, value: Any, path: str, instruction: Any) -> str:
    from pyrung.click.ladder.types import _RenderError

    if isinstance(value, Expression):
        # CLK_EXPR_ONLY_IN_MATH reports an expression outside math().
        raise _Unbuildable
    try:
        return exporter._render_operand(value, path=path, source=instruction)
    except _RenderError as exc:
        raise _Unbuildable from exc


def _range_text(exporter: Any, value: Any, path: str, instruction: Any) -> tuple[str, Any]:
    """Return a range as the export writes it, and its problem, as ``_range_text`` does."""
    from pyrung.click.ladder.types import _RenderError

    try:
        return exporter._range_text(value, path=path, instruction=instruction)
    except _RenderError as exc:
        raise _Unbuildable from exc


def _split_range(text: str) -> tuple[str, str]:
    start, sep, end = text.partition("..")
    if not sep:
        # A range that is not one run of CLICK addresses, such as ``[DS1,DS5]``, has a
        # finding of its own.
        raise _Unbuildable
    return start, end


def _dest_range(text: str, source_start: str, source_end: str) -> tuple[str, str]:
    """Return the start and end of a Block copy's destination *text*.

    The export writes a destination tag as one address. CLICK ends the destination as many
    addresses past its start as the source runs past its own start. laddercodec counts the
    same way.
    """
    if ".." in text:
        return _split_range(text)
    try:
        memory_type, address = parse_address(text)
        _bank, first = parse_address(source_start)
        _bank, last = parse_address(source_end)
    except ValueError as exc:
        raise _Unbuildable from exc
    return text, format_address_display(memory_type, address + last - first)


def _copy_kwargs(instruction: Any, source: Any) -> dict[str, Any]:
    """Return the converter fields of the laddercodec Copy that the export writes."""
    from pyrung.click.ladder.instructions import _exported_converter
    from pyrung.click.ladder.translator import _operand_type

    converter = _exported_converter(instruction)
    if converter is None:
        return {}
    kwargs: dict[str, Any] = {"convert": f"to_{converter.mode}"}
    if converter.mode == "text":
        # The export writes suppress zero for every source but a Real. CLICK offers
        # exponential numbering only for a Real.
        if _operand_type(source) is TagType.REAL:
            kwargs["suppress_zero"] = None
        else:
            kwargs["suppress_zero"] = bool(converter.suppress_zero)
        kwargs["exponential"] = bool(converter.exponential)
        if converter.termination_code is not None:
            kwargs["termination_code"] = f"${converter.termination_code:02X}"
    return kwargs


def _refusals(build: Any) -> list[Any]:
    """Return laddercodec's refusals of the instruction that *build* constructs."""
    try:
        build()
    except FieldError as exc:
        return list(exc.errors)
    return []


def _single_register_fill_refusals(instruction: Any, tag: Any, exporter: Any) -> list[Any]:
    """Return laddercodec's refusals of a Fill of one register.

    CLICK's Fill needs two registers, so the export writes a Fill of one as a Copy. CLICK
    sees the Copy, so the Copy's refusals come first. A Fill converts no value, so the banks
    must also pass as a Fill.
    """
    from pyrung.click.ladder.types import _RenderError

    oneshot = bool(getattr(instruction, "oneshot", False))
    hex_dest = exporter._is_hex_bank_operand
    dest = _render(exporter, tag, "instruction.dest", instruction).partition("..")[0]
    copy_refusals: list[Any] | None
    try:
        # The export refuses some of these Fills as a Copy, and says why.
        exporter._single_register_fill_token(instruction, tag, path="instruction", oneshot_kw={})
        with exporter._hex_constants("copy" if hex_dest(dest) else None):
            value = _render(exporter, instruction.value, "instruction.value", instruction)
        copy_refusals = _refusals(lambda: Copy(source=value, dest=dest, oneshot=oneshot))
    except (_RenderError, _Unbuildable):
        copy_refusals = None
    if copy_refusals:
        return copy_refusals
    try:
        with exporter._hex_constants("word" if hex_dest(dest) else None):
            value = _render(exporter, instruction.value, "instruction.value", instruction)
    except _Unbuildable:
        if copy_refusals is None:
            raise
        return []
    refusals = _refusals(lambda: Fill(value=value, dest_start=dest, dest_end=dest, oneshot=oneshot))
    # laddercodec refuses a Fill range of one address. The export writes a Copy in its
    # place, so CLICK never sees that range.
    return [refusal for refusal in refusals if str(refusal.code) != "range-not-ascending"]


def _build(instruction: Any, exporter: Any) -> list[Any]:
    """Return laddercodec's refusals of the instruction that the export writes.

    Raise ``_Unbuildable`` when the export cannot write the instruction at all.
    """
    kind = type(instruction).__name__
    oneshot = bool(getattr(instruction, "oneshot", False))
    hex_dest = exporter._is_hex_bank_operand

    def render(value: Any, field: str) -> str:
        return _render(exporter, value, f"instruction.{field}", instruction)

    if kind == "FillInstruction":
        tag = single_register_fill_dest(instruction.dest)
        if tag is not None:
            return _single_register_fill_refusals(instruction, tag, exporter)
        dest_start, dest_end = _split_range(render(instruction.dest, "dest"))
        with exporter._hex_constants("word" if hex_dest(dest_start) else None):
            value = render(instruction.value, "value")
        return _refusals(
            lambda: Fill(value=value, dest_start=dest_start, dest_end=dest_end, oneshot=oneshot)
        )
    if kind == "CopyInstruction":
        # The export writes a text Copy's destination as the range that the text fills.
        # laddercodec's Copy takes the first address of it.
        dest = render(instruction.dest, "dest").partition("..")[0]
        with exporter._hex_constants("copy" if hex_dest(dest) else None):
            source = render(instruction.source, "source")
        kwargs = _copy_kwargs(instruction, instruction.source)
        return _refusals(lambda: Copy(source=source, dest=dest, oneshot=oneshot, **kwargs))
    if kind == "BlockCopyInstruction":
        source_start, source_end = _split_range(render(instruction.source, "source"))
        dest_start, dest_end = _dest_range(
            render(instruction.dest, "dest"), source_start, source_end
        )
        convert = None if instruction.convert is None else f"to_{instruction.convert.mode}"
        return _refusals(
            lambda: BlockCopy(
                source_start=source_start,
                source_end=source_end,
                dest_start=dest_start,
                dest_end=dest_end,
                convert=convert,  # ty: ignore[invalid-argument-type]
                oneshot=oneshot,
            )
        )
    if kind in _PACK_KINDS:
        source_field, _ = _COPY_FAMILY_FIELDS[kind]
        start, end = _split_range(render(getattr(instruction, source_field), source_field))
        dest = render(instruction.dest, "dest")
        return _refusals(
            lambda: Pack(
                source_start=start,
                source_end=end,
                dest=dest,
                kind=_PACK_KINDS[kind],  # ty: ignore[invalid-argument-type]
                allow_whitespace=bool(getattr(instruction, "allow_whitespace", False)),
                oneshot=oneshot,
            )
        )
    if kind in _UNPACK_KINDS:
        _, dest_field = _COPY_FAMILY_FIELDS[kind]
        start, end = _split_range(render(getattr(instruction, dest_field), dest_field))
        source = render(instruction.source, "source")
        return _refusals(
            lambda: Unpack(
                source=source,
                dest_start=start,
                dest_end=end,
                kind=_UNPACK_KINDS[kind],  # ty: ignore[invalid-argument-type]
                oneshot=oneshot,
            )
        )
    raise _Unbuildable


def _broken_ranges(instruction: Any, exporter: Any) -> list[RangeProblem]:
    """Return each range operand that CLICK cannot hold as a range.

    The export would write such a range as a list, such as ``[DS1,DS5]`` or ``[]``. CLICK
    has no list form, so no CLICK project can hold it.
    """
    problems: list[RangeProblem] = []
    for field in _RANGE_FIELDS.get(type(instruction).__name__, ()):
        value = getattr(instruction, field)
        try:
            _text, problem = _range_text(exporter, value, f"instruction.{field}", instruction)
        except _Unbuildable:
            continue
        if problem is not None:
            problems.append(problem)
    return problems


def _call_text(instruction: Any) -> str:
    """Return the instruction as the call that the author wrote."""
    return render_instruction(instruction, "")[0]


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
    instruction: Any, names: Mapping[str, str], resolver: _ResolutionCache
) -> dict[str, str]:
    """Return *names*, with each address in the call named as the call spells it.

    laddercodec quotes every address when it gets no names, so each address of the call
    has an entry. An address that the call spells as an address, such as ``X1`` for
    ``X001``, is named by itself. laddercodec would show any other name with the address
    after it in brackets.
    """
    call = dict(names)
    for attribute in _COPY_FAMILY_FIELDS[type(instruction).__name__]:
        for tag, name in _operand_tags(getattr(instruction, attribute)):
            address = _tag_address(tag, resolver)
            if address is not None:
                call[address] = address if _spells(name, address) else name
    return call


def _spells(name: str, address: str) -> bool:
    """Return True if *name* is a spelling of the CLICK address *address*."""
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
    # The instruction attribute that holds the refused operand, or the name of the refused
    # keyword argument. It is None for a field that no argument holds.
    attribute: str | None

    def finding(self, extra_advice: Sequence[str] = ()) -> ClickFinding:
        """Return the finding. Advice in *extra_advice* replaces pyrung's advice for the code."""
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
class CopyFamilyResult:
    """What validation found in one Copy family instruction.

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
            on_operand = [i for i in same_code if self.refusals[i].attribute == attribute]
            for i in on_operand or same_code[:1]:
                taken = extra.setdefault(i, [])
                taken.extend(text for text in advice if text not in taken)
        return [
            *self.checks,
            *(refusal.finding(extra.get(i, ())) for i, refusal in enumerate(self.refusals)),
        ]


def _refusal(
    refusal: Any,
    instruction: Any,
    base_location: ProgramLocation,
    location_text: str,
    mode: ValidationMode,
    resolver: _ResolutionCache,
    names: Mapping[str, str],
) -> _Refusal:
    """Return laddercodec's *refusal* with its frame, its caret and its texts."""
    code = str(refusal.code)
    argument = _FIELD_ARGUMENTS.get(refusal.field)
    operand: Any = None
    if isinstance(argument, int):
        attribute: str | None = _COPY_FAMILY_FIELDS[type(instruction).__name__][argument]
        operand = getattr(instruction, attribute)
    else:
        # A keyword argument, such as the converter, holds no address.
        attribute = argument
    frame = instruction_frame(base_location, instruction, argument=argument)
    call_names = _call_names(instruction, names, resolver)
    text = refusal.message(call_names)
    # laddercodec gives no detail for an instruction it has no rules for yet. The short
    # text is then the whole message, so it stays as the problem line.
    detail = refusal.detail(call_names) if frame.caret is not None else ""
    if detail:
        # The caret marks the operand, so the label gives the operand's addresses and not
        # its names.
        own = {
            address: address
            for tag, _name in _operand_tags(operand)
            if (address := _tag_address(tag, resolver)) is not None
        }
        frame = replace(frame, caret_label=refusal.short({**call_names, **own}))
        problem = ""
    else:
        problem = text
    message = f"CLICK refuses {_call_text(instruction)} at {location_text}. {text}"
    if refusal.click_id and refusal.click_id not in text:
        message += f" CLICK's error is {refusal.click_id}."
    return _Refusal(
        code=code,
        severity=_route_severity(code, mode),
        message=message,
        location=location_text,
        frame=frame,
        problem=problem,
        detail=detail,
        click_id=refusal.click_id,
        advice=_advice(code, instruction, resolver),
        attribute=attribute,
    )


def evaluate_copy_family(
    instruction: Any,
    base_location: ProgramLocation,
    exporter: Any,
    mode: ValidationMode,
    resolver: _ResolutionCache,
    names: Mapping[str, str],
) -> CopyFamilyResult:
    """Report each value of a Copy family instruction that laddercodec refuses.

    *names* maps CLICK addresses to tag names for laddercodec's text. :func:`tag_names`
    builds it.
    """
    kind = type(instruction).__name__
    fields = _COPY_FAMILY_FIELDS.get(kind)
    if fields is None:
        return CopyFamilyResult([])

    unresolved: list[ClickFinding] = []
    for field, what in zip(fields, ("source", "destination"), strict=True):
        location = _instruction_location(base_location, f"instruction.{field}")
        if resolver.resolve_operand_slots(getattr(instruction, field)).unresolved:
            unresolved.append(_unresolved_finding(location, mode, what))
    if unresolved:
        return CopyFamilyResult(unresolved)

    location_text = _format_location(_instruction_location(base_location, "instruction"))
    broken = _broken_ranges(instruction, exporter)
    if broken:
        finding = ClickFinding(
            code=CLK_COPY_RANGE_NOT_CONSECUTIVE,
            severity=_route_severity(CLK_COPY_RANGE_NOT_CONSECUTIVE, mode),
            message=" ".join(
                [
                    f"CLICK refuses {_call_text(instruction)} at {location_text}.",
                    range_not_consecutive_text(broken),
                ]
            ),
            location=location_text,
            suggestion=range_not_consecutive_advice(broken),
        )
        return CopyFamilyResult([finding])

    try:
        refusals = _build(instruction, exporter)
    except _Unbuildable:
        # laddercodec has nothing to read. An expression operand, or an operand that the
        # export refuses, has a finding of its own.
        return CopyFamilyResult([])

    return CopyFamilyResult(
        [],
        tuple(
            _refusal(r, instruction, base_location, location_text, mode, resolver, names)
            for r in refusals
        ),
    )


__all__ = ["CopyFamilyResult", "_COPY_FAMILY_FIELDS", "evaluate_copy_family", "tag_names"]
