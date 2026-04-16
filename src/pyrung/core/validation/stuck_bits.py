"""Stuck-bit validation for pyrung programs.

Detects latch/reset imbalances where a Bool tag can be latched but never
reset (stuck HIGH) or reset but never latched (stuck LOW).

Scope is limited to ``LatchInstruction`` and ``ResetInstruction``.  These
are INERT_WHEN_DISABLED=True — they only fire when their rung condition is
true — so *rung conditions matter* for reachability analysis (unlike the
INERT_WHEN_DISABLED=False conflicting-output validator).

Explicitly out of scope for this module:
  - Counter/timer accumulator stuck values
  - Branch-level set-point analysis
  - BDD/SMT-based condition satisfiability upgrades

Conservative stance: when pattern matching cannot prove a contradiction,
the site is treated as reachable.  This eliminates false positives on
legitimate subroutine-gated pause patterns at the cost of potential false
negatives.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.validation._common import (
    WriteSite,
    _build_caller_map,
    _caller_conditions,
    _CallerMap,
    _collect_write_sites,
    _flatten_and_conditions,
    _format_site_location,
    _resolve_tag_names,
)

if TYPE_CHECKING:
    from pyrung.core.program import Program

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CORE_STUCK_HIGH = "CORE_STUCK_HIGH"
CORE_STUCK_LOW = "CORE_STUCK_LOW"

# ---------------------------------------------------------------------------
# Latch/Reset target extractor
# ---------------------------------------------------------------------------


def _latch_reset_write_targets(instr: Any) -> list[tuple[str, str]]:
    """Return (tag_name, instruction_type) pairs for LatchInstruction/ResetInstruction."""
    from pyrung.core.instruction.coils import LatchInstruction, ResetInstruction

    if isinstance(instr, (LatchInstruction, ResetInstruction)):
        itype = type(instr).__name__
        return [(name, itype) for name in _resolve_tag_names(instr.target)]
    return []


# ---------------------------------------------------------------------------
# Reachability analysis
# ---------------------------------------------------------------------------


def _conditions_provably_unreachable(conditions: tuple[Any, ...]) -> bool:
    """Check if an AND-chain of conditions contains a contradicting pair.

    If so, the rung can never be true — the site is provably unreachable.
    """
    from pyrung.core.validation._common import _conditions_contradict

    flat = _flatten_and_conditions(conditions)
    for i in range(len(flat)):
        for j in range(i + 1, len(flat)):
            if _conditions_contradict(flat[i], flat[j]):
                return True
    return False


def _site_provably_unreachable(site: WriteSite, caller_map: _CallerMap) -> bool:
    """Determine whether a latch/reset site is provably unreachable.

    A site is provably unreachable if:
      (a) its own rung conditions contain a contradicting pair, OR
      (b) it lives in a subroutine AND every caller chain from the caller
          map is itself provably unreachable by rule (a).

    For main-scope sites, only (a) applies.
    """
    # Check the site's own conditions
    own_unreachable = _conditions_provably_unreachable(site.conditions)

    if site.scope == "main":
        return own_unreachable

    # Subroutine scope: even if the site's own conditions are satisfiable,
    # it's unreachable if every caller is unreachable.
    assert site.subroutine is not None
    caller_chains = _caller_conditions(site, caller_map)

    # No callers → uncalled subroutine → unreachable
    if not caller_chains:
        return True

    # If the site's own conditions are contradictory it's unreachable
    # regardless of callers.
    if own_unreachable:
        return True

    # Every caller chain must be provably unreachable for the site to be
    # unreachable.  If ANY caller is reachable, the site is reachable.
    for chain in caller_chains:
        if not _conditions_provably_unreachable(chain):
            return False
    return True


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StuckBitFinding:
    """A stuck-bit finding for a single tag."""

    code: str
    target_name: str
    kind: Literal["high", "low"]
    reachable_sites: tuple[WriteSite, ...]
    missing_side: str
    message: str


@dataclass(frozen=True)
class StuckBitReport:
    findings: tuple[StuckBitFinding, ...]

    def summary(self) -> str:
        if not self.findings:
            return "No stuck bits."
        return f"{len(self.findings)} stuck bit(s)."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_stuck_bits(program: Program) -> StuckBitReport:
    """Validate a Program for stuck latch/reset bits.

    Detects tags that can be latched but never reset (STUCK_HIGH) or reset
    but never latched (STUCK_LOW).

    Returns a StuckBitReport with one finding per stuck tag.
    """
    from pyrung.core.instruction.coils import LatchInstruction, ResetInstruction

    sites = _collect_write_sites(program, target_extractor=_latch_reset_write_targets)
    caller_map = _build_caller_map(program)

    # Partition sites by target and instruction type
    latch_sites: dict[str, list[WriteSite]] = defaultdict(list)
    reset_sites: dict[str, list[WriteSite]] = defaultdict(list)

    for site in sites:
        if site.instruction_type == LatchInstruction.__name__:
            latch_sites[site.target_name].append(site)
        elif site.instruction_type == ResetInstruction.__name__:
            reset_sites[site.target_name].append(site)

    # All tags that have at least one latch or reset
    all_tags = sorted(set(latch_sites) | set(reset_sites))

    findings: list[StuckBitFinding] = []

    for tag_name in all_tags:
        latches = latch_sites.get(tag_name, [])
        resets = reset_sites.get(tag_name, [])

        # Filter to reachable sites
        reachable_latches = [s for s in latches if not _site_provably_unreachable(s, caller_map)]
        reachable_resets = [s for s in resets if not _site_provably_unreachable(s, caller_map)]

        if reachable_latches and not reachable_resets:
            locs = [_format_site_location(s) for s in reachable_latches]
            message = f"Tag '{tag_name}' can be latched but never reset:\n" + "\n".join(
                f"  - {loc}" for loc in locs
            )
            findings.append(
                StuckBitFinding(
                    code=CORE_STUCK_HIGH,
                    target_name=tag_name,
                    kind="high",
                    reachable_sites=tuple(reachable_latches),
                    missing_side="reset",
                    message=message,
                )
            )
        elif reachable_resets and not reachable_latches:
            locs = [_format_site_location(s) for s in reachable_resets]
            message = f"Tag '{tag_name}' can be reset but never latched:\n" + "\n".join(
                f"  - {loc}" for loc in locs
            )
            findings.append(
                StuckBitFinding(
                    code=CORE_STUCK_LOW,
                    target_name=tag_name,
                    kind="low",
                    reachable_sites=tuple(reachable_resets),
                    missing_side="latch",
                    message=message,
                )
            )

    return StuckBitReport(findings=tuple(findings))
