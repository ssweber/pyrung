"""Automatically generated module split."""

from __future__ import annotations

import re
from typing import Literal

from pyclickplc.addresses import AddressRecord, format_address_display, parse_address
from pyclickplc.banks import BANKS, DEFAULT_RETENTIVE, DataType
from pyclickplc.blocks import BlockRange as ClickBlockRange
from pyclickplc.blocks import parse_block_tag

from pyrung.core import Block, InputBlock, OutputBlock, TagType

from ._types import _BlockImportSpec

_DATA_TYPE_TO_TAG_TYPE: dict[DataType, TagType] = {
    DataType.BIT: TagType.BOOL,
    DataType.INT: TagType.INT,
    DataType.INT2: TagType.DINT,
    DataType.FLOAT: TagType.REAL,
    DataType.HEX: TagType.WORD,
    DataType.TXT: TagType.CHAR,
}

_HARDWARE_BLOCK_CACHE: dict[str, Block | InputBlock | OutputBlock] = {}

_IDENTIFIER_TOKEN_RE = r"[A-Za-z_][A-Za-z0-9_]*"

_EXPLICIT_NAMED_ARRAY_RE = re.compile(
    rf"^(?P<base>{_IDENTIFIER_TOKEN_RE}):named_array(?:\((?P<args>[^)]*)\))?$"
)

_EXPLICIT_UDT_RE = re.compile(
    rf"^(?P<base>{_IDENTIFIER_TOKEN_RE})\.(?P<field>{_IDENTIFIER_TOKEN_RE}):udt$"
)

_EXPLICIT_BLOCK_RE = re.compile(rf"^(?P<base>{_IDENTIFIER_TOKEN_RE}):block$")

_EXPLICIT_BLOCK_START_RE = re.compile(
    rf"^(?P<base>{_IDENTIFIER_TOKEN_RE}):block\((?P<start>start=(?:0|[1-9][0-9]*)|0|[1-9][0-9]*)\)$"
)


def _tag_type_for_memory_type(memory_type: str) -> TagType:
    config = BANKS[memory_type]
    return _DATA_TYPE_TO_TAG_TYPE[config.data_type]


def _compress_addresses_to_ranges(addresses: list[int]) -> tuple[tuple[int, int], ...] | None:
    if not addresses:
        return None

    ranges: list[tuple[int, int]] = []
    lo = hi = addresses[0]
    for addr in addresses[1:]:
        if addr == hi + 1:
            hi = addr
            continue
        ranges.append((lo, hi))
        lo = hi = addr
    ranges.append((lo, hi))
    return tuple(ranges)


def _valid_ranges_for_bank(memory_type: str) -> tuple[tuple[int, int], ...] | None:
    config = BANKS[memory_type]
    if config.valid_ranges is not None:
        return config.valid_ranges
    if memory_type not in {"XD", "YD"}:
        return None

    # XD/YD expose a sparse MDB address set where XD0u/YD0u maps to address 1.
    valid_addresses: list[int] = []
    for addr in range(config.min_addr, config.max_addr + 1):
        display = format_address_display(memory_type, addr)
        try:
            parsed_bank, parsed_addr = parse_address(display)
        except ValueError:
            continue
        if parsed_bank == memory_type and parsed_addr == addr:
            valid_addresses.append(addr)
    return _compress_addresses_to_ranges(valid_addresses)


def _hardware_block_for(memory_type: str) -> Block | InputBlock | OutputBlock:
    cached = _HARDWARE_BLOCK_CACHE.get(memory_type)
    if cached is not None:
        return cached

    config = BANKS[memory_type]
    name = config.name
    tag_type = _tag_type_for_memory_type(config.name)
    start = config.min_addr
    end = config.max_addr
    valid_ranges = _valid_ranges_for_bank(memory_type)
    formatter = format_address_display

    if memory_type in {"X", "XD"}:
        block = InputBlock(
            name=name,
            type=tag_type,
            start=start,
            end=end,
            valid_ranges=valid_ranges,
            address_formatter=formatter,
        )
    elif memory_type in {"Y", "YD"}:
        block = OutputBlock(
            name=name,
            type=tag_type,
            start=start,
            end=end,
            valid_ranges=valid_ranges,
            address_formatter=formatter,
        )
    else:
        block = Block(
            name=name,
            type=tag_type,
            start=start,
            end=end,
            retentive=DEFAULT_RETENTIVE[memory_type],
            valid_ranges=valid_ranges,
            address_formatter=formatter,
        )
    _HARDWARE_BLOCK_CACHE[memory_type] = block
    return block


def _parse_default(initial_value: str, tag_type: TagType) -> object:
    if initial_value == "":
        if tag_type == TagType.BOOL:
            return False
        if tag_type in (TagType.INT, TagType.DINT, TagType.WORD):
            return 0
        if tag_type == TagType.REAL:
            return 0.0
        if tag_type == TagType.CHAR:
            return ""
        return 0

    try:
        if tag_type == TagType.BOOL:
            return initial_value == "1"
        if tag_type in (TagType.INT, TagType.DINT):
            return int(initial_value)
        if tag_type == TagType.REAL:
            return float(initial_value)
        if tag_type == TagType.WORD:
            return int(initial_value, 16)
        if tag_type == TagType.CHAR:
            return initial_value[:1]
    except ValueError:
        pass

    if tag_type == TagType.REAL:
        return 0.0
    if tag_type == TagType.CHAR:
        return ""
    if tag_type == TagType.BOOL:
        return False
    return 0


def _format_default(value: object, tag_type: TagType) -> str:
    if tag_type == TagType.BOOL:
        return "1" if bool(value) else "0"
    if tag_type in (TagType.INT, TagType.DINT):
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            return str(int(value))
        if isinstance(value, str):
            try:
                return str(int(value))
            except ValueError:
                return "0"
        return "0"
    if tag_type == TagType.REAL:
        if isinstance(value, bool):
            return "1.0" if value else "0.0"
        if isinstance(value, (int, float)):
            return str(float(value))
        if isinstance(value, str):
            try:
                return str(float(value))
            except ValueError:
                return "0.0"
        return "0.0"
    if tag_type == TagType.WORD:
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, int):
            return f"{value:X}"
        if isinstance(value, float):
            return f"{int(value):X}"
        if isinstance(value, str):
            try:
                return f"{int(value, 16):X}"
            except ValueError:
                try:
                    return f"{int(value):X}"
                except ValueError:
                    return "0"
        return "0"
    if tag_type == TagType.CHAR:
        if not isinstance(value, str):
            return ""
        return value[:1]
    return str(value)


def _parse_structured_block_name(
    name: str,
) -> tuple[
    Literal["plain", "udt", "named_array", "group"],
    str | None,
    str | None,
    int | None,
    int | None,
    str,
    int | None,
    bool | None,
]:
    if name.endswith(":group"):
        base = name[: -len(":group")]
        if base == "":
            raise ValueError(
                f"Invalid group block tag {name!r}. Expected Base:group with a non-empty base name."
            )
        return ("group", base, None, None, None, name, None, None)

    named_array_match = _EXPLICIT_NAMED_ARRAY_RE.fullmatch(name)
    if named_array_match is not None:
        count_val: int | None = None
        stride_val: int | None = None
        explicit_always_number = False
        args_str = named_array_match.group("args")
        if args_str is not None:
            tokens = [t.strip() for t in args_str.split(",") if t.strip()]
            numeric_tokens: list[int] = []
            for token in tokens:
                if token == "always_number":
                    explicit_always_number = True
                elif token.isdigit() and int(token) >= 1:
                    numeric_tokens.append(int(token))
                else:
                    raise ValueError(
                        f"Invalid named_array argument {token!r} in {name!r}. "
                        "Expected positive integers and/or 'always_number'."
                    )
            if len(numeric_tokens) == 1:
                count_val = numeric_tokens[0]
            elif len(numeric_tokens) == 2:
                count_val = numeric_tokens[0]
                stride_val = numeric_tokens[1]
            elif len(numeric_tokens) > 2:
                raise ValueError(
                    f"Too many numeric arguments in {name!r}. "
                    "Expected :named_array, :named_array(count), or :named_array(count,stride)."
                )
        return (
            "named_array",
            named_array_match.group("base"),
            None,
            count_val,
            stride_val,
            name,
            None,
            True if explicit_always_number else None,
        )

    if ":named_array" in name:
        raise ValueError(
            f"Invalid named_array block tag {name!r}. Expected Base:named_array "
            "or Base:named_array(count) or Base:named_array(count,stride)."
        )

    udt_match = _EXPLICIT_UDT_RE.fullmatch(name)
    if udt_match is not None:
        return (
            "udt",
            udt_match.group("base"),
            udt_match.group("field"),
            None,
            None,
            name,
            None,
            None,
        )

    if name.endswith(":udt") or ":udt" in name:
        raise ValueError(
            f"Invalid UDT block tag {name!r}. Expected Base.field:udt with identifier tokens."
        )

    block_match = _EXPLICIT_BLOCK_RE.fullmatch(name)
    if block_match is not None:
        return ("plain", None, None, None, None, block_match.group("base"), None, None)

    block_start_match = _EXPLICIT_BLOCK_START_RE.fullmatch(name)
    if block_start_match is not None:
        start_token = block_start_match.group("start")
        if start_token.startswith("start="):
            explicit_start = int(start_token.split("=", maxsplit=1)[1])
        else:
            explicit_start = int(start_token)
        return (
            "plain",
            None,
            None,
            None,
            None,
            block_start_match.group("base"),
            explicit_start,
            None,
        )

    if ":block" in name:
        raise ValueError(
            f"Invalid block start tag {name!r}. Expected Base:block, Base:block(n), or "
            "Base:block(start=n)."
        )

    return ("group", name, None, None, None, name, None, None)


def _build_block_spec(rows: list[AddressRecord], block_range: ClickBlockRange) -> _BlockImportSpec:
    start_row = rows[block_range.start_idx]
    end_row = rows[block_range.end_idx]
    memory_type = start_row.memory_type
    if end_row.memory_type != memory_type:
        raise ValueError(
            f"Block {block_range.name!r} spans multiple memory types: "
            f"{memory_type} and {end_row.memory_type}."
        )

    start_addr = min(start_row.address, end_row.address)
    end_addr = max(start_row.address, end_row.address)
    hardware_block = _hardware_block_for(memory_type)
    hardware_range = hardware_block.select(start_addr, end_addr)
    hardware_addresses = tuple(hardware_range.addresses)
    return _BlockImportSpec(
        name=block_range.name,
        memory_type=memory_type,
        start_idx=block_range.start_idx,
        end_idx=block_range.end_idx,
        hardware_range=hardware_range,
        hardware_addresses=hardware_addresses,
    )


def _default_logical_block_start(hardware_addresses: tuple[int, ...]) -> int:
    if hardware_addresses and hardware_addresses[0] == 0:
        return 0
    return 1


def _extract_address_comment(comment: str) -> str:
    parsed = parse_block_tag(comment)
    if parsed.name is None:
        return comment.strip()
    return parsed.remaining_text.strip()


def _compose_address_comment(comment: str, block_tag: str = "") -> str:
    text = comment.strip()
    if not block_tag:
        return text
    if not text:
        return block_tag
    return f"{block_tag} {text}"


def _is_marker_only_boundary_row(row: AddressRecord, *, block_name: str) -> bool:
    parsed = parse_block_tag(row.comment)
    if parsed.name != block_name:
        return False
    if parsed.remaining_text.strip() != "":
        return False
    if row.nickname != "":
        return False
    if row.retentive != DEFAULT_RETENTIVE[row.memory_type]:
        return False
    logical_type = _tag_type_for_memory_type(row.memory_type)
    return _parse_default(row.initial_value, logical_type) == _parse_default("", logical_type)
