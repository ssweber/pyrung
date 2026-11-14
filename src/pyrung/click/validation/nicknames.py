"""Click nickname validation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyclickplc.addresses import format_address_display
from pyclickplc.validation import NICKNAME_MAX_LENGTH

from pyrung.click.tag_map._nickname_io import nickname_records
from pyrung.core.analysis import collect_program_tags

from .findings import (
    CLK_NICKNAME_TOO_LONG,
    ClickFinding,
    ValidationMode,
    _route_severity,
)
from .identity import _first_tag_locations

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap
    from pyrung.core.memory_block import Block
    from pyrung.core.program import Program
    from pyrung.core.validation.walker import ProgramFacts


def _program_banks(program: Program) -> list[Block]:
    """Blocks whose slots the program reads or writes, for their configured slot names."""
    banks: dict[int, Block] = {}
    for tag in collect_program_tags(program):
        block = getattr(tag, "_pyrung_block", None)
        if block is not None:
            banks.setdefault(id(block), block)
    return list(banks.values())


def _evaluate_nickname_lengths(
    program: Program,
    facts: ProgramFacts,
    tag_map: TagMap,
    mode: ValidationMode,
) -> list[ClickFinding]:
    """Find nicknames the export would write that CLICK cannot hold."""
    records = nickname_records(tag_map, blocks=_program_banks(program))
    first_locations = _first_tag_locations(facts)
    findings: list[ClickFinding] = []
    for _, record in sorted(records.items()):
        nickname = record.nickname
        if len(nickname) <= NICKNAME_MAX_LENGTH:
            continue
        display_address = format_address_display(record.memory_type, record.address)
        findings.append(
            ClickFinding(
                code=CLK_NICKNAME_TOO_LONG,
                severity=_route_severity(CLK_NICKNAME_TOO_LONG, mode),
                message=(
                    f"Nickname {nickname!r} at {display_address} is {len(nickname)} characters; "
                    f"CLICK allows {NICKNAME_MAX_LENGTH}."
                ),
                location=first_locations.get(nickname, "tag_map"),
                suggestion=f"Rename the tag to {NICKNAME_MAX_LENGTH} characters or fewer.",
            )
        )
    return findings


__all__ = ["_evaluate_nickname_lengths"]
