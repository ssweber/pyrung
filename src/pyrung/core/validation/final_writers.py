"""Final multiple-writers validation for pyrung programs.

Detects when a tag marked ``final=True`` has more than one write site in the
ladder. ``final`` means exactly one writer — no mutual-exclusivity exemption.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pyrung.core.validation._common import (
    WriteSite,
    _build_tag_map,
    _collect_write_sites,
    _format_site_location,
)
from pyrung.core.validation.readonly_write import _any_write_targets

if TYPE_CHECKING:
    from pyrung.core.program import Program

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CORE_FINAL_MULTIPLE_WRITERS = "CORE_FINAL_MULTIPLE_WRITERS"

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FinalWritersFinding:
    """A final tag with more than one write site."""

    code: str
    target_name: str
    sites: tuple[WriteSite, ...]
    message: str


@dataclass(frozen=True)
class FinalWritersReport:
    findings: tuple[FinalWritersFinding, ...]

    def summary(self) -> str:
        if not self.findings:
            return "No final multiple-writer violations."
        return f"{len(self.findings)} final multiple-writer violation(s)."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_final_writers(program: Program) -> FinalWritersReport:
    """Validate a Program for final tags with multiple writers."""
    tag_map = _build_tag_map(program)
    sites = _collect_write_sites(program, target_extractor=_any_write_targets)

    # Group sites by target
    sites_by_target: dict[str, list[WriteSite]] = {}
    for site in sites:
        sites_by_target.setdefault(site.target_name, []).append(site)

    findings: list[FinalWritersFinding] = []
    for tag_name in sorted(sites_by_target):
        tag = tag_map.get(tag_name)
        if tag is None or not tag.final:
            continue
        target_sites = sites_by_target[tag_name]
        if len(target_sites) <= 1:
            continue
        locs = [_format_site_location(s) for s in target_sites]
        message = (
            f"Tag '{tag_name}' is final but has {len(target_sites)} write site(s):\n"
            + "\n".join(f"  - {loc}" for loc in locs)
        )
        findings.append(
            FinalWritersFinding(
                code=CORE_FINAL_MULTIPLE_WRITERS,
                target_name=tag_name,
                sites=tuple(target_sites),
                message=message,
            )
        )

    return FinalWritersReport(findings=tuple(findings))
