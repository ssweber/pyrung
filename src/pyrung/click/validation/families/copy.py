"""The Copy family: copy(), blockcopy(), fill(), pack_bits(), pack_words(), pack_text(),
unpack_to_bits() and unpack_to_words().

laddercodec's Copy, BlockCopy, Fill, Pack and Unpack check them.
"""

from __future__ import annotations

from typing import Any

from laddercodec import BlockCopy, Copy, Fill, Pack, Unpack
from pyclickplc.addresses import format_address_display, parse_address

from pyrung.click.capabilities import single_register_fill_dest
from pyrung.core.tag import TagType

from ..fields import Family, Unbuildable, memory_types, refusals_of, render_operand, split_range
from ..hardware import constant_text_past_bank_advice
from ..resolve import _ResolutionCache

# The attribute that holds each operand of the call, with its plain word in a
# CLK_BANK_UNRESOLVED finding.
_SOURCE_DEST = {"source": "source", "dest": "destination"}
_CLASSES: dict[str, dict[str, str]] = {
    "CopyInstruction": _SOURCE_DEST,
    "BlockCopyInstruction": _SOURCE_DEST,
    "FillInstruction": {"value": "source", "dest": "destination"},
    "PackBitsInstruction": {"bit_block": "source", "dest": "destination"},
    "PackWordsInstruction": {"word_block": "source", "dest": "destination"},
    "PackTextInstruction": {"source_range": "source", "dest": "destination"},
    "UnpackToBitsInstruction": {"source": "source", "bit_block": "destination"},
    "UnpackToWordsInstruction": {"source": "source", "word_block": "destination"},
}

# The argument of the pyrung call that holds each field of laddercodec's Copy family. The
# export writes a fill() of one register as a Copy, so Copy.source is then the value of
# fill().
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

# The operands of each instruction that hold a range of addresses.
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


def _advice(code: str, field: str, instruction: Any, resolver: _ResolutionCache) -> str | None:
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
        and memory_types(instruction.dest, resolver) & _BIT_BANKS
    ):
        if memory_types(instruction.value, resolver):
            return "Copy the bit into each destination bit with copy()."
        return "Set or clear the bits with latch() or reset() on the range."
    if (
        code == "bank-not-allowed"
        and kind == "BlockCopyInstruction"
        and "YD" in memory_types(instruction.dest, resolver)
    ):
        return "Write each YD with copy() or math()."
    return _ADVICE.get(code)


def _dest_range(text: str, source_start: str, source_end: str) -> tuple[str, str]:
    """Return the start and end of a Block copy's destination *text*.

    The export writes a destination tag as one address. CLICK ends the destination as many
    addresses past its start as the source runs past its own start. laddercodec counts the
    same way.
    """
    if ".." in text:
        return split_range(text)
    try:
        memory_type, address = parse_address(text)
        _bank, first = parse_address(source_start)
        _bank, last = parse_address(source_end)
    except ValueError as exc:
        raise Unbuildable from exc
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


def _single_register_fill_refusals(instruction: Any, tag: Any, exporter: Any) -> list[Any]:
    """Return laddercodec's refusals of a Fill of one register.

    CLICK's Fill needs two registers, so the export writes a Fill of one as a Copy. CLICK
    sees the Copy, so the Copy's refusals come first. A Fill converts no value, so the banks
    must also pass as a Fill.
    """
    from pyrung.click.ladder.types import _RenderError

    oneshot = bool(getattr(instruction, "oneshot", False))
    hex_dest = exporter._is_hex_bank_operand
    dest = render_operand(exporter, tag, "dest", instruction).partition("..")[0]
    copy_refusals: list[Any] | None
    try:
        # The export refuses some of these Fills as a Copy, and says why.
        exporter._single_register_fill_token(instruction, tag, path="instruction", oneshot_kw={})
        with exporter._hex_constants("copy" if hex_dest(dest) else None):
            value = render_operand(exporter, instruction.value, "value", instruction)
        copy_refusals = refusals_of(lambda: Copy(source=value, dest=dest, oneshot=oneshot))
    except (_RenderError, Unbuildable):
        copy_refusals = None
    if copy_refusals:
        return copy_refusals
    try:
        with exporter._hex_constants("word" if hex_dest(dest) else None):
            value = render_operand(exporter, instruction.value, "value", instruction)
    except Unbuildable:
        if copy_refusals is None:
            raise
        return []
    refusals = refusals_of(
        lambda: Fill(value=value, dest_start=dest, dest_end=dest, oneshot=oneshot)
    )
    # laddercodec refuses a Fill range of one address. The export writes a Copy in its
    # place, so CLICK never sees that range.
    return [refusal for refusal in refusals if str(refusal.code) != "range-not-ascending"]


def _build(instruction: Any, exporter: Any) -> list[Any]:
    """Return laddercodec's refusals of the instruction that the export writes."""
    kind = type(instruction).__name__
    oneshot = bool(getattr(instruction, "oneshot", False))
    hex_dest = exporter._is_hex_bank_operand

    def render(value: Any, attribute: str) -> str:
        return render_operand(exporter, value, attribute, instruction)

    if kind == "FillInstruction":
        tag = single_register_fill_dest(instruction.dest)
        if tag is not None:
            return _single_register_fill_refusals(instruction, tag, exporter)
        dest_start, dest_end = split_range(render(instruction.dest, "dest"))
        with exporter._hex_constants("word" if hex_dest(dest_start) else None):
            value = render(instruction.value, "value")
        return refusals_of(
            lambda: Fill(value=value, dest_start=dest_start, dest_end=dest_end, oneshot=oneshot)
        )
    if kind == "CopyInstruction":
        # The export writes a text Copy's destination as the range that the text fills.
        # laddercodec's Copy takes the first address of it.
        dest = render(instruction.dest, "dest").partition("..")[0]
        with exporter._hex_constants("copy" if hex_dest(dest) else None):
            source = render(instruction.source, "source")
        kwargs = _copy_kwargs(instruction, instruction.source)
        return refusals_of(lambda: Copy(source=source, dest=dest, oneshot=oneshot, **kwargs))
    if kind == "BlockCopyInstruction":
        source_start, source_end = split_range(render(instruction.source, "source"))
        dest_start, dest_end = _dest_range(
            render(instruction.dest, "dest"), source_start, source_end
        )
        convert = None if instruction.convert is None else f"to_{instruction.convert.mode}"
        return refusals_of(
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
        # A pack call takes its source first.
        source_attribute = next(iter(_CLASSES[kind]))
        start, end = split_range(render(getattr(instruction, source_attribute), source_attribute))
        dest = render(instruction.dest, "dest")
        return refusals_of(
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
        # An unpack call takes its destination second.
        _source, dest_attribute = _CLASSES[kind]
        start, end = split_range(render(getattr(instruction, dest_attribute), dest_attribute))
        source = render(instruction.source, "source")
        return refusals_of(
            lambda: Unpack(
                source=source,
                dest_start=start,
                dest_end=end,
                kind=_UNPACK_KINDS[kind],  # ty: ignore[invalid-argument-type]
                oneshot=oneshot,
            )
        )
    raise Unbuildable


def _export_ignores_allow(code: str, instruction: Any) -> bool:
    """Return True if the export refuses the fault of *code* even when the code is allowed.

    The export refuses a Fill of text with a quote among other characters, and a Fill of
    one register with text of several characters. It has no form of either to write.
    """
    if type(instruction).__name__ != "FillInstruction" or not isinstance(instruction.value, str):
        return False
    value = instruction.value
    if code == "invalid-text-constant":
        return '"' in value and value != '"'
    one_register = single_register_fill_dest(instruction.dest)
    return (
        code == "constant-out-of-range"
        and one_register is not None
        and one_register.type == TagType.CHAR
        and len(value) > 1
    )


FAMILY = Family(
    name="copy",
    classes=_CLASSES,
    build=_build,
    arguments=_FIELD_ARGUMENTS,
    advice=_advice,
    ranges=_RANGE_FIELDS,
    export_ignores_allow=_export_ignores_allow,
    replaced_codes={
        "CLK_COPY_BANK_INCOMPATIBLE": "copy-bank-incompatible or bank-not-allowed",
        "CLK_COPY_CONVERTER_INCOMPATIBLE": (
            "copy-converter-bank, copy-txt-needs-convert or option-not-offered"
        ),
        "CLK_COPY_CONSTANT_INTO_BIT": "constant-into-bit or bank-not-allowed",
        "CLK_PACK_TEXT_BANK_INCOMPATIBLE": "pack-kind-mismatch or copy-bank-incompatible",
    },
)
