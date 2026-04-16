"""Conflicting output target validation for pyrung programs.

Detects when multiple INERT_WHEN_DISABLED=False instructions target the same
tag from non-mutually-exclusive execution paths.  Such conflicts cause
last-writer-wins stomping at runtime.

Covered instruction types:
  OutInstruction, OnDelayInstruction, OffDelayInstruction,
  CountUpInstruction, CountDownInstruction, ShiftInstruction,
  EventDrumInstruction, TimeDrumInstruction,
  ModbusSendInstruction, ModbusReceiveInstruction.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pyrung.core.validation._common import (
    WriteSite,
    _build_caller_map,
    _caller_conditions,
    _CallerMap,
    _chain_pair_mutually_exclusive,
    _collect_write_sites,
    _format_site_location,
    _instruction_write_targets,
)

if TYPE_CHECKING:
    from pyrung.core.program import Program

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

CORE_CONFLICTING_OUTPUT = "CORE_CONFLICTING_OUTPUT"

# Re-export WriteSite under the original public name for backwards compat.
OutputSite = WriteSite


@dataclass(frozen=True)
class ConflictingOutputFinding:
    """A set of instruction sites that target the same tag without mutual exclusivity."""

    code: str
    target_name: str
    sites: tuple[OutputSite, ...]
    message: str


@dataclass(frozen=True)
class ConflictingOutputReport:
    findings: tuple[ConflictingOutputFinding, ...]

    def summary(self) -> str:
        if not self.findings:
            return "No conflicting outputs."
        return f"{len(self.findings)} conflicting output target(s)."


# ---------------------------------------------------------------------------
# Mutual exclusivity (duplicate-out specific)
# ---------------------------------------------------------------------------


def _sites_mutually_exclusive(
    site_a: OutputSite, site_b: OutputSite, caller_map: _CallerMap
) -> bool:
    """Check if two output sites are provably mutually exclusive.

    INERT_WHEN_DISABLED=False instructions always execute (set or reset) every
    scan.  The only way two such instructions can safely share a target is if
    they are in **different subroutines** whose callers are mutually exclusive —
    because a disabled call() skips the entire subroutine so its instructions
    never run.

    Within the same execution scope (main rungs, same subroutine, branches)
    mutually-exclusive rung conditions do NOT help: both instructions still
    execute every scan, and the disabled one resets the target.
    """
    # An uncalled subroutine's instructions never execute — no conflict
    if (
        site_a.scope == "subroutine"
        and site_a.subroutine is not None
        and not caller_map.get(site_a.subroutine, [])
    ):
        return True
    if (
        site_b.scope == "subroutine"
        and site_b.subroutine is not None
        and not caller_map.get(site_b.subroutine, [])
    ):
        return True

    # Both must be in subroutine scope to have any chance of exclusivity
    if site_a.scope != "subroutine" or site_b.scope != "subroutine":
        return False

    # Same subroutine: both execute when the subroutine is called
    if site_a.subroutine == site_b.subroutine:
        return False

    # Different subroutines: check if callers are mutually exclusive
    chains_a = _caller_conditions(site_a, caller_map)
    chains_b = _caller_conditions(site_b, caller_map)

    # Uncalled subroutine -> no conflict possible
    if not chains_a or not chains_b:
        return True

    # Every pair of caller chains must be mutually exclusive
    for ca in chains_a:
        for cb in chains_b:
            if not _chain_pair_mutually_exclusive(ca, cb):
                return False
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_conflicting_outputs(program: Program) -> ConflictingOutputReport:
    """Validate a Program for conflicting output targets.

    Detects when multiple INERT_WHEN_DISABLED=False instructions write to the
    same tag from non-mutually-exclusive execution paths.

    Returns a ConflictingOutputReport with one finding per conflicting target.
    """
    sites = _collect_write_sites(program, target_extractor=_instruction_write_targets)
    caller_map = _build_caller_map(program)

    # Group sites by target tag name
    groups: dict[str, list[OutputSite]] = defaultdict(list)
    for site in sites:
        groups[site.target_name].append(site)

    findings: list[ConflictingOutputFinding] = []

    for target_name in sorted(groups):
        group = groups[target_name]
        if len(group) < 2:
            continue

        # Find all sites that participate in at least one non-exclusive pair
        conflicting: set[int] = set()
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if not _sites_mutually_exclusive(group[i], group[j], caller_map):
                    conflicting.add(i)
                    conflicting.add(j)

        if not conflicting:
            continue

        conflict_sites = tuple(group[i] for i in sorted(conflicting))
        locations = [_format_site_location(s) for s in conflict_sites]
        message = (
            f"Tag '{target_name}' is written by {len(conflict_sites)} "
            f"non-exclusive instructions:\n" + "\n".join(f"  - {loc}" for loc in locations)
        )

        findings.append(
            ConflictingOutputFinding(
                code=CORE_CONFLICTING_OUTPUT,
                target_name=target_name,
                sites=conflict_sites,
                message=message,
            )
        )

    return ConflictingOutputReport(findings=tuple(findings))
