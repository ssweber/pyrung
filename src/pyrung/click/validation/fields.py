"""CLICK's field rules for the Copy family, as laddercodec checks them.

For each copy(), blockcopy(), fill(), pack and unpack instruction, pyrung builds the
laddercodec instruction that the export writes. laddercodec refuses the values that CLICK's
dialog refuses. Each refusal becomes a finding whose code is laddercodec's code. The
finding's message names the author's call and gives laddercodec's message. Its advice is
pyrung's, where the advice names a pyrung call or argument.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from laddercodec import BlockCopy, Copy, FieldError, Fill, Pack, Unpack
from pyclickplc.addresses import format_address_display, parse_address

from pyrung.click.capabilities import single_register_fill_dest
from pyrung.core.expression import Expression
from pyrung.core.tag import TagType
from pyrung.core.validation.render import operand_name, render_instruction

from .findings import (
    CLK_COPY_RANGE_NOT_CONSECUTIVE,
    ClickFinding,
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


@dataclass(frozen=True)
class CopyFamilyResult:
    """The findings for one Copy family instruction, and the codes that laddercodec raised."""

    findings: list[ClickFinding]
    codes: frozenset[str]


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
    kind = type(instruction).__name__
    verb = _PACK_KINDS.get(kind) or _UNPACK_KINDS.get(kind)
    if verb is None:
        return render_instruction(instruction, "")[0]
    source, dest = (getattr(instruction, field) for field in _COPY_FAMILY_FIELDS[kind])
    return f"{verb}({operand_name(source)}, {operand_name(dest)})"


def _finding_text(call: str, location_text: str, refusals: list[Any]) -> str:
    """Return the finding's message: pyrung's frame, then laddercodec's message."""
    text = f"CLICK refuses {call} at {location_text}."
    messages = list(dict.fromkeys(map(str, refusals)))
    click_ids = dict.fromkeys(r.click_id for r in refusals if r.click_id)
    for click_id in click_ids:
        if not any(click_id in message for message in messages):
            messages.append(f"CLICK's error is {click_id}.")
    return " ".join([text, *messages])


def evaluate_copy_family(
    instruction: Any,
    base_location: ProgramLocation,
    exporter: Any,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> CopyFamilyResult:
    """Report each value of a Copy family instruction that laddercodec refuses."""
    kind = type(instruction).__name__
    fields = _COPY_FAMILY_FIELDS.get(kind)
    if fields is None:
        return CopyFamilyResult([], frozenset())

    unresolved: list[ClickFinding] = []
    for field, what in zip(fields, ("source", "destination"), strict=True):
        location = _instruction_location(base_location, f"instruction.{field}")
        if resolver.resolve_operand_slots(getattr(instruction, field)).unresolved:
            unresolved.append(_unresolved_finding(location, mode, what))
    if unresolved:
        return CopyFamilyResult(unresolved, frozenset())

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
        return CopyFamilyResult([finding], frozenset())

    try:
        refusals = _build(instruction, exporter)
    except _Unbuildable:
        # laddercodec has nothing to read. An expression operand, or an operand that the
        # export refuses, has a finding of its own.
        return CopyFamilyResult([], frozenset())

    # pyrung reports one finding for each code.
    by_code: dict[str, list[Any]] = {}
    for refusal in refusals:
        by_code.setdefault(str(refusal.code), []).append(refusal)
    call = _call_text(instruction)
    findings = [
        ClickFinding(
            code=code,
            severity=_route_severity(code, mode),
            message=_finding_text(call, location_text, grouped),
            location=location_text,
            suggestion=_advice(code, instruction, resolver),
        )
        for code, grouped in by_code.items()
    ]
    return CopyFamilyResult(findings, frozenset(by_code))


__all__ = ["CopyFamilyResult", "_COPY_FAMILY_FIELDS", "evaluate_copy_family"]
