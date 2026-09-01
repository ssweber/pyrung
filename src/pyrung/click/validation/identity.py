"""Click address identity validation."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from pyclickplc.addresses import format_address_display, parse_address

from pyrung.core.analysis import collect_program_tags

from .findings import (
    CLK_ADDRESS_IDENTITY_CONFLICT,
    ClickFinding,
    ValidationMode,
    _route_severity,
)
from .resolve import _format_location

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap
    from pyrung.core.program import Program
    from pyrung.core.tag import Tag
    from pyrung.core.validation.walker import ProgramFacts


def _canonical_click_address(tag: Tag, tag_map: TagMap) -> tuple[str, int] | None:
    try:
        return parse_address(tag_map.resolve(tag))
    except (KeyError, TypeError, ValueError):
        pass

    block = getattr(tag, "_pyrung_block", None)
    if block is not None:
        address = getattr(tag, "_pyrung_block_addr", None)
        if isinstance(address, int):
            try:
                return parse_address(block._format_tag_name(address))
            except (AttributeError, ValueError):
                return None

    try:
        return parse_address(tag.name)
    except ValueError:
        return None


def _first_tag_locations(facts: ProgramFacts) -> dict[str, str]:
    locations: dict[str, str] = {}
    for fact in facts.operands:
        if fact.value_kind != "tag":
            continue
        name = fact.metadata.get("tag_name")
        if isinstance(name, str):
            locations.setdefault(name, _format_location(fact.location))
    return locations


def _evaluate_address_identity_conflicts(
    program: Program,
    facts: ProgramFacts,
    tag_map: TagMap,
    mode: ValidationMode,
) -> list[ClickFinding]:
    """Find distinct pyrung state identities that target one Click address."""
    tags_by_address: dict[tuple[str, int], dict[str, Tag]] = defaultdict(dict)
    for tag in collect_program_tags(program):
        resolved = _canonical_click_address(tag, tag_map)
        if resolved is not None:
            tags_by_address[resolved][tag.name] = tag

    first_locations = _first_tag_locations(facts)
    findings: list[ClickFinding] = []
    for (memory_type, address), tags_by_name in sorted(tags_by_address.items()):
        if len(tags_by_name) < 2:
            continue
        names = sorted(tags_by_name)
        display_address = format_address_display(memory_type, address)
        quoted_names = ", ".join(repr(name) for name in names)
        location = first_locations.get(names[0], "program")
        findings.append(
            ClickFinding(
                code=CLK_ADDRESS_IDENTITY_CONFLICT,
                severity=_route_severity(CLK_ADDRESS_IDENTITY_CONFLICT, mode),
                message=(
                    f"Click address {display_address} has multiple pyrung identities: "
                    f"{quoted_names}. These are separate values in Python but one value on Click."
                ),
                location=location,
                suggestion=(
                    "Use one tag identity consistently. Prefer the configured Click block slot "
                    "or map one semantic tag through TagMap; do not also construct the raw address."
                ),
            )
        )
    return findings


__all__ = ["_evaluate_address_identity_conflicts"]
