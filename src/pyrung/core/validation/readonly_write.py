"""Readonly write validation for pyrung programs.

Detects write sites that target a tag marked ``readonly=True``.  Readonly
tags are initialized from their declared default at power-on and must never
be written again — not by the ladder, not by any external source.

This validator covers ALL instruction types that produce writes, including
one-shot instructions (copy, calc, etc.) that are not covered by the
conflicting-output or stuck-bit validators.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.validation._common import (
    WriteSite,
    _build_tag_map,
    _collect_write_sites,
    _format_site_location,
    _resolve_tag_names,
)

if TYPE_CHECKING:
    from pyrung.core.program import Program

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CORE_READONLY_WRITE = "CORE_READONLY_WRITE"

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadonlyWriteFinding:
    """A write site targeting a readonly tag."""

    code: str
    target_name: str
    sites: tuple[WriteSite, ...]
    message: str


@dataclass(frozen=True)
class ReadonlyWriteReport:
    findings: tuple[ReadonlyWriteFinding, ...]

    def summary(self) -> str:
        if not self.findings:
            return "No readonly write violations."
        return f"{len(self.findings)} readonly write violation(s)."


# ---------------------------------------------------------------------------
# Write target extraction (all instruction types)
# ---------------------------------------------------------------------------


def _any_write_targets(instr: Any) -> list[tuple[str, str]]:
    """Return (tag_name, instruction_type) for any instruction with a write target."""
    itype = type(instr).__name__
    target = getattr(instr, "target", None)
    if target is not None:
        names = _resolve_tag_names(target)
        if names:
            return [(name, itype) for name in names]

    # Timer/counter done_bit + accumulator
    done_bit = getattr(instr, "done_bit", None)
    accumulator = getattr(instr, "accumulator", None)
    if done_bit is not None and accumulator is not None:
        names = _resolve_tag_names(done_bit) + _resolve_tag_names(accumulator)
        return [(name, itype) for name in names]

    # Drum outputs + current_step + completion_flag
    outputs = getattr(instr, "outputs", None)
    current_step = getattr(instr, "current_step", None)
    if outputs is not None and current_step is not None:
        names = []
        for t in outputs:
            names.extend(_resolve_tag_names(t))
        names.extend(_resolve_tag_names(current_step))
        completion_flag = getattr(instr, "completion_flag", None)
        if completion_flag is not None:
            names.extend(_resolve_tag_names(completion_flag))
        if accumulator is not None:
            names.extend(_resolve_tag_names(accumulator))
        return [(name, itype) for name in names]

    # Shift instruction bit_range
    bit_range = getattr(instr, "bit_range", None)
    if bit_range is not None:
        names = _resolve_tag_names(bit_range)
        if names:
            return [(name, itype) for name in names]

    # Pack/unpack destination
    destination = getattr(instr, "destination", None)
    if destination is not None:
        names = _resolve_tag_names(destination)
        if names:
            return [(name, itype) for name in names]

    return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_readonly_writes(program: Program) -> ReadonlyWriteReport:
    """Validate a Program for writes to readonly tags."""
    tag_map = _build_tag_map(program)
    sites = _collect_write_sites(program, target_extractor=_any_write_targets)

    # Group sites by target
    sites_by_target: dict[str, list[WriteSite]] = {}
    for site in sites:
        sites_by_target.setdefault(site.target_name, []).append(site)

    findings: list[ReadonlyWriteFinding] = []
    for tag_name in sorted(sites_by_target):
        tag = tag_map.get(tag_name)
        if tag is None or not tag.readonly:
            continue
        target_sites = sites_by_target[tag_name]
        locs = [_format_site_location(s) for s in target_sites]
        message = f"Tag '{tag_name}' is readonly but has write site(s):\n" + "\n".join(
            f"  - {loc}" for loc in locs
        )
        findings.append(
            ReadonlyWriteFinding(
                code=CORE_READONLY_WRITE,
                target_name=tag_name,
                sites=tuple(target_sites),
                message=message,
            )
        )

    return ReadonlyWriteReport(findings=tuple(findings))
