"""Hardware profile checks for Click validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from laddercodec import Code
from pyclickplc.banks import BANKS

from pyrung.click.capabilities import (
    HEX_CONSTANT_BANKS,
    single_register_fill_dest,
)
from pyrung.core.instruction.conversions import (
    LITERAL,
    _render_text_from_numeric,
    copy_source_kind,
)
from pyrung.core.memory_block import _element_name, _store_block, block_room
from pyrung.core.tag import ImmediateRef, Tag, TagType
from pyrung.core.validation.walker import ProgramLocation

from .findings import (
    CLK_COPY_WIDE_CONSTANT,
    CLK_TEXT_PAST_BANK,
    CLK_TEXT_PAST_BLOCK,
    CLK_TIMER_PAIR_MISMATCH,
    ClickFinding,
    ValidationMode,
    _route_severity,
)
from .resolve import (
    _bank_label,
    _format_location,
    _instruction_location,
    _ResolutionCache,
    _unresolved_finding,
    _wrong_role_text,
)

if TYPE_CHECKING:
    from pyrung.click.profile import HardwareProfile


def _load_default_profile() -> HardwareProfile | None:
    from pyrung.click.profile import load_default_profile

    return load_default_profile()


def _role_banks(profile: HardwareProfile, role: str) -> str:
    """Return the banks that *profile* allows for *role*, as ``"Y, C"``."""
    return ", ".join(bank for bank in BANKS if profile.valid_for_role(bank, role))


def _wide_constant_into_hex(
    instruction_type: str,
    value: Any,
    dest_memory_types: tuple[str, ...],
    dest_location: ProgramLocation,
    mode: ValidationMode,
    *,
    range_fill: bool = False,
) -> list[ClickFinding]:
    """Report CLK_COPY_WIDE_CONSTANT, a constant that the export refuses for a hex register.

    A CLICK hex constant is 0h..FFFFh. The export refuses a constant into DH, XD or YD
    outside -32768..65535, with a fraction or without. It writes -32768..-1 as its 16 bits.
    A Copy writes a fraction as a Real constant, but a Fill of several registers
    (*range_fill*) has only the hex constant, so the export refuses any fraction there.
    """
    if isinstance(value, ImmediateRef):
        value = value.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return []
    if isinstance(value, float) and not math.isfinite(value):
        return []
    fraction = range_fill and isinstance(value, float) and not value.is_integer()
    if -32768 <= value <= 65535 and not fraction:
        return []
    hex_banks = [
        memory_type for memory_type in dest_memory_types if memory_type in HEX_CONSTANT_BANKS
    ]
    if not hex_banks:
        return []
    verb = "fill" if instruction_type == "FillInstruction" else "copy"
    location_text = _format_location(dest_location)
    banks = ", ".join(hex_banks)
    if fraction:
        message = (
            f"fill({value!r}, ...) into {banks} at {location_text} has a constant that is not "
            "a whole number. A CLICK Fill into a hex register takes 0h..FFFFh, so the export "
            "refuses it."
        )
        suggestion = "Fill a whole 16-bit value, 0..65535 (0h..FFFFh)."
    else:
        message = (
            f"{verb}({value!r}, ...) into {banks} at {location_text} has a constant that does "
            "not fit in 16 bits. A CLICK hex constant is 0h..FFFFh, so the export refuses it."
        )
        suggestion = "Write the constant as its 16-bit value, 0..65535 (0h..FFFFh)."
    return [
        ClickFinding(
            code=CLK_COPY_WIDE_CONSTANT,
            severity=_route_severity(CLK_COPY_WIDE_CONSTANT, mode),
            message=message,
            location=location_text,
            suggestion=suggestion,
        )
    ]


def _evaluate_wide_copy_constant(
    instruction: Any,
    base_location: ProgramLocation,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    """Report a copy() or fill() constant that the export has no hex constant for."""
    instruction_type = type(instruction).__name__
    if instruction_type == "CopyInstruction":
        value, dest, range_fill = instruction.source, instruction.dest, False
    elif instruction_type == "FillInstruction":
        value, dest = instruction.value, instruction.dest
        # A Fill of one register exports as a Copy, which takes a fraction as a Real.
        range_fill = single_register_fill_dest(dest) is None
    else:
        return []
    if copy_source_kind(value) != LITERAL:
        return []
    resolution = resolver.resolve_operand_slots(dest)
    if resolution.unresolved:
        return []
    dest_memory_types = tuple(dict.fromkeys(slot.memory_type for slot in resolution.slots))
    return _wide_constant_into_hex(
        instruction_type,
        value,
        dest_memory_types,
        _instruction_location(base_location, "instruction.dest"),
        mode,
        range_fill=range_fill,
    )


# The fewest characters that ``to_text()`` writes for a tag of each type, and whether
# that is the length of every value. With zero suppressed the count is one or more.
# Otherwise an Int is five digits (six with a sign), a Dint ten (eleven) and a Word four
# hex digits. A finite Real has seven decimals. Its shortest form is "0.0000000", or
# "0.0000000E+00" in exponential form. NaN and infinity are "nan", "inf" and "-inf".
_FIXED_TEXT_WIDTH = {TagType.INT: (5, False), TagType.DINT: (10, False), TagType.WORD: (4, True)}
_REAL_TEXT_WIDTH = 9
_REAL_EXPONENTIAL_TEXT_WIDTH = 13


@dataclass(frozen=True)
class _Written:
    """How many elements a Copy writes from its destination on.

    *count* is the length of every value when *exact*, and the fewest otherwise. It
    applies to a finite value only when *finite_only*.
    """

    count: int
    exact: bool
    finite_only: bool = False

    def describe(self, what: str) -> str:
        if self.exact:
            return f"{self.count} {what}"
        finite = " for a finite value" if self.finite_only else ""
        return f"{self.count} {what} or more{finite}"

    def dropped(self, room: int) -> str:
        """Describe what a store with *room* elements left drops."""
        if self.exact:
            lost = self.count - room
            return "the last one" if lost == 1 else f"the last {lost}"
        return "all but the first" if room == 1 else f"all but the first {room}"

    def dropped_verb(self, room: int) -> str:
        return "is" if self.exact and self.count - room == 1 else "are"

    def kept(self) -> str:
        return "every one" if self.exact else f"the first {self.count}"


def _fewest_elements_written(instruction: Any) -> _Written | None:
    """Return how many elements a Copy writes from its destination on.

    Return ``None`` when it writes one.
    """
    source = instruction.source
    converter = instruction.convert
    if converter is None:
        target = instruction.target
        is_text = isinstance(target, Tag) and target.type is TagType.CHAR
        return _Written(len(source), True) if isinstance(source, str) and is_text else None
    if converter.mode in {"value", "ascii"}:
        return _Written(len(source), True) if isinstance(source, str) else None
    if converter.mode != "text":
        return None
    tail = 0 if converter.termination_code is None else 1
    kind = copy_source_kind(source)
    if kind == LITERAL:
        try:
            text = _render_text_from_numeric(
                source,
                source_tag=None,
                suppress_zero=converter.suppress_zero,
                exponential=converter.exponential,
            )
        except (TypeError, ValueError, OverflowError):
            return None
        return _Written(len(text) + tail, True)
    if kind is TagType.REAL:
        width = _REAL_EXPONENTIAL_TEXT_WIDTH if converter.exponential else _REAL_TEXT_WIDTH
        return _Written(width + tail, False, finite_only=True)
    if converter.suppress_zero:
        return _Written(1 + tail, False)
    fixed = _FIXED_TEXT_WIDTH.get(kind)
    return None if fixed is None else _Written(fixed[0] + tail, fixed[1])


def _saved_constant_length(value: Any) -> int | None:
    """Return how many characters CLICK saves for a Copy's constant, without outer quotes.

    CLICK sets a text Copy's end address from this length, not from the text it writes
    (1.5 into TXT1 ends at TXT3). Text and a whole number count in the export's spelling,
    which is CLICK's saved form. CLICK saves a Copy's text as typed and counts each quote
    once, so "A"B" ends at TXT3 (clicknick ``buildbot/jobs/lc-diff-click/RESULTS9-10.md``).

    A Real keeps at most 7 digits, so 3.4028235E+38 saves as 3.402823E+38. From 1e7 up,
    CLICK saves a Real in E form: 123456789.0 as 1.234568E+08 and 1e10 as 1E+10
    (RESULTS9-10). The export still spells those positionally, so this function counts
    CLICK's E form. No CLICK result covers the E form of a smaller Real, so a smaller
    Real counts in the export's spelling.
    """
    from pyrung.click.ladder.translator import _float32, click_real_literal

    if isinstance(value, ImmediateRef):
        value = value.value
    if isinstance(value, str):
        return len(value)
    if isinstance(value, bool):
        return 1
    if isinstance(value, int):
        return len(str(value))
    if isinstance(value, float):
        text = click_real_literal(value)
        if text is None:
            return None
        f32 = _float32(value)
        if f32 is not None and abs(f32) >= 1e7:
            text = f"{f32:.7G}"
        return len(text)
    return None


def constant_text_past_txt(instruction: Any, address: int) -> int | None:
    """Return how many characters CLICK saves for a text Copy's constant into TXT *address*.

    Return ``None`` unless those characters run past TXT1000. CLICK's Copy dialog refuses
    that store (OBJ-003). CLICK's Copy accepts a register source, which saves no end
    address, so this function does not judge a register source.
    """
    if copy_source_kind(instruction.source) != LITERAL:
        return None
    if _fewest_elements_written(instruction) is None:
        return None
    length = _saved_constant_length(instruction.source)
    if length is None or address + length - 1 <= BANKS["TXT"].max_addr:
        return None
    return length


def constant_text_past_bank_advice(instruction: Any, resolver: _ResolutionCache) -> str | None:
    """Return the advice for a text Copy's constant that runs past TXT1000.

    The advice names the highest TXT that keeps every character that CLICK saves and
    every character that the Copy writes.
    """
    if type(instruction).__name__ != "CopyInstruction":
        return None
    target = instruction.target
    if isinstance(target, ImmediateRef):
        target = target.value
    written = _fewest_elements_written(instruction)
    resolution = resolver.resolve_operand_slots(target)
    if written is None or resolution.unresolved or len(resolution.slots) != 1:
        return None
    address = resolution.slots[0].address
    saved = None if address is None else constant_text_past_txt(instruction, address)
    if saved is None:
        return None
    bank = BANKS["TXT"]
    lowest = bank.max_addr - max(saved, written.count) + 1
    if lowest < bank.min_addr:
        return None
    return f"Start it at TXT{lowest} or lower to keep {written.kept()}."


def _evaluate_text_past_bank(
    instruction: Any,
    base_location: ProgramLocation,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    """Report CLK_TEXT_PAST_BANK and CLK_TEXT_PAST_BLOCK.

    Each is a Copy by address whose text runs past the end of its bank, or past the end
    of its own block while the bank goes on.

    pyrung writes the elements that its block holds and drops the rest. A store past the
    bank warns. laddercodec refuses a constant into TXT that runs past the bank as CLICK
    saves it (OBJ-003), so this function skips that store. CLICK's Copy accepts a register
    source, which saves no end address. A store past the end of a block that is mapped
    inside a bank warns. CLICK writes the rest of the text into the bank past the block,
    and pyrung stops at the end of the block. Only a store whose shortest text runs past
    the end is reported.
    """
    if type(instruction).__name__ != "CopyInstruction":
        return []
    target = instruction.target
    if isinstance(target, ImmediateRef):
        target = target.value
    if not isinstance(target, Tag):
        return []
    written = _fewest_elements_written(instruction)
    if written is None:
        return []
    resolution = resolver.resolve_operand_slots(target)
    if resolution.unresolved or len(resolution.slots) != 1:
        return []
    slot = resolution.slots[0]
    bank = BANKS.get(slot.memory_type)
    if bank is None or slot.address is None:
        return []
    count = written.count
    bank_room = bank.max_addr - slot.address + 1
    location_text = _format_location(_instruction_location(base_location, "instruction.target"))
    memory_type = slot.memory_type
    start = f"{memory_type}{slot.address}"
    severity = "error" if mode == "strict" else "warning"

    if memory_type == "TXT" and constant_text_past_txt(instruction, slot.address) is not None:
        # laddercodec refuses this constant as text-past-bank.
        return []
    if count < 2:
        return []
    block_room_ = block_room(target)
    what = "characters" if memory_type == "TXT" else "elements"

    if block_room_ is not None and block_room_ < min(count, bank_room):
        home = _store_block(target)
        assert home is not None
        block = home[0]
        named = target.name if target.name == start else f"{target.name} ({start})"
        lowest = block.end - count + 1
        return [
            ClickFinding(
                code=CLK_TEXT_PAST_BLOCK,
                severity=severity,
                message=(
                    f"copy() into {named} at {location_text} writes "
                    f"{written.describe(what)}, past {_element_name(block, block.end)} at the "
                    f"end of its block {block.name}: pyrung drops "
                    f"{written.dropped(block_room_)}, where a "
                    f"CLICK writes on into {memory_type}{slot.address + block_room_}."
                ),
                location=location_text,
                suggestion=(
                    f"Start it at {_element_name(block, lowest)} or lower to keep {written.kept()}."
                    if lowest >= block.start
                    else f"Map {block.name} onto more of {memory_type} to keep {written.kept()}."
                ),
            )
        ]
    if count <= bank_room:
        return []
    lowest = bank.max_addr - count + 1
    return [
        ClickFinding(
            code=CLK_TEXT_PAST_BANK,
            severity=severity,
            message=(
                f"copy() into {start} at {location_text} writes {written.describe(what)}, "
                f"past {memory_type}{bank.max_addr} at the end of the bank: "
                f"{written.dropped(bank_room)} {written.dropped_verb(bank_room)} dropped."
            ),
            location=location_text,
            suggestion=(
                f"Start it at {memory_type}{lowest} or lower to keep {written.kept()}."
                if lowest >= bank.min_addr
                else None
            ),
        )
    ]


# A CLICK timer or counter is one number. Its instruction names T<n> (or CT<n>), and the
# accumulator TD<n> (CTD<n>) comes with it. The Out coils name only TD<n>.
_PAIRED_BANKS = {"T": "TD", "CT": "CTD"}


def _evaluate_done_bits(
    instruction: Any,
    pairs: tuple[tuple[str, str], ...],
    base_location: ProgramLocation,
    profile: HardwareProfile,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    """Report a Done bit that has no address or sits in a bank that CLICK refuses.

    laddercodec checks the accumulator of an Out variant timer and never sees its Done bit,
    so the Done bit is checked here as a timer done bit.
    """
    code = str(Code.BANK_NOT_ALLOWED)
    findings: list[ClickFinding] = []
    for done_field, _acc_field in pairs:
        location = _instruction_location(base_location, f"instruction.{done_field}")
        resolution = resolver.resolve_operand_slots(getattr(instruction, done_field))
        if resolution.unresolved:
            findings.append(_unresolved_finding(location, mode, "operand"))
            continue
        for slot in resolution.slots:
            if profile.valid_for_role(slot.memory_type, "timer_done_bit"):
                continue
            location_text = _format_location(location)
            findings.append(
                ClickFinding(
                    code=code,
                    severity=_route_severity(code, mode),
                    message=_wrong_role_text(
                        slot,
                        "timer done bit",
                        location_text,
                        _role_banks(profile, "timer_done_bit"),
                    ),
                    location=location_text,
                )
            )
    return findings


def _evaluate_timer_pairs(
    instruction: Any,
    base_location: ProgramLocation,
    profile: HardwareProfile,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    from .families import family_of

    family = family_of(instruction)
    if family is None:
        return []
    pairs = family.timer_pairs.get(type(instruction).__name__, ())
    findings = _evaluate_done_bits(instruction, pairs, base_location, profile, mode, resolver)
    for done_field, acc_field in pairs:
        done, acc = getattr(instruction, done_field), getattr(instruction, acc_field)
        done_slots = resolver.resolve_operand_slots(done).slots
        acc_slots = resolver.resolve_operand_slots(acc).slots
        if len(done_slots) != 1 or len(acc_slots) != 1:
            continue
        done_slot, acc_slot = done_slots[0], acc_slots[0]
        if _PAIRED_BANKS.get(done_slot.memory_type) != acc_slot.memory_type:
            continue  # a wrong bank is a role finding
        if done_slot.address == acc_slot.address:
            continue
        location_text = _format_location(
            _instruction_location(base_location, f"instruction.{acc_field}")
        )
        findings.append(
            ClickFinding(
                code=CLK_TIMER_PAIR_MISMATCH,
                severity=_route_severity(CLK_TIMER_PAIR_MISMATCH, mode),
                message=(
                    f"{_bank_label(done_slot)} and {_bank_label(acc_slot)} are different "
                    f"CLICK timers or counters at {location_text}; Done and Acc must share "
                    "one number."
                ),
                location=location_text,
                suggestion="Map Done and Acc to the same number, e.g. T5 with TD5.",
            )
        )
    return findings
