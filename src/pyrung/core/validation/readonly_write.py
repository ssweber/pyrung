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

from pyrung.core.analysis.write_sites import instruction_written_tags
from pyrung.core.validation._common import (
    WriteSite,
    _collect_write_sites,
    site_frame,
)
from pyrung.core.validation.display import FindingDisplay, _FindingTextMixin
from pyrung.core.validation.severity import Severity

if TYPE_CHECKING:
    from pyrung.core.program import Program
    from pyrung.core.tag import Tag

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TAG_READONLY_WRITE = "TAG_READONLY_WRITE"

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadonlyWriteFinding(_FindingTextMixin):
    """A write site targeting a readonly tag."""

    code: str
    target_name: str
    sites: tuple[WriteSite, ...]
    display: FindingDisplay
    severity: Severity = "error"

    @property
    def message(self) -> str:
        return self.display.as_text()


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
    """Return (tag_name, instruction_type) for each tag *instr* may write by name."""
    itype = type(instr).__name__
    return [(tag.name, itype) for tag in instruction_written_tags(instr)]


def _written_tags_by_name(sites: list[WriteSite]) -> dict[str, list[Tag]]:
    """Return the tag objects that the sites write, by name, to read their flags from."""
    by_name: dict[str, list[Tag]] = {}
    for instr in {id(site.instruction): site.instruction for site in sites}.values():
        for tag in instruction_written_tags(instr):
            by_name.setdefault(tag.name, []).append(tag)
    return by_name


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_readonly_writes(program: Program) -> ReadonlyWriteReport:
    """Validate a Program for writes to readonly tags."""
    sites = _collect_write_sites(program, target_extractor=_any_write_targets)
    written = _written_tags_by_name(sites)

    # Group sites by target
    sites_by_target: dict[str, list[WriteSite]] = {}
    for site in sites:
        sites_by_target.setdefault(site.target_name, []).append(site)

    findings: list[ReadonlyWriteFinding] = []
    for tag_name in sorted(sites_by_target):
        if not any(tag.readonly for tag in written[tag_name]):
            continue
        target_sites = sites_by_target[tag_name]
        display = FindingDisplay(
            code=TAG_READONLY_WRITE,
            severity="error",
            frames=tuple(site_frame(s, caret_label="readonly") for s in target_sites),
            problem=f"{tag_name} is readonly.",
            hint=f"remove this write, or remove readonly=True from {tag_name}",
        )
        findings.append(
            ReadonlyWriteFinding(
                code=TAG_READONLY_WRITE,
                target_name=tag_name,
                sites=tuple(target_sites),
                display=display,
            )
        )

    return ReadonlyWriteReport(findings=tuple(findings))
