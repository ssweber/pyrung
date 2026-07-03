"""Stuck-bit validation for pyrung programs.

Detects latch/reset imbalances where a Bool tag can be latched but never
reset (stuck HIGH) or reset but never latched (stuck LOW).

Covers ``LatchInstruction``, ``ResetInstruction``, and ``CopyInstruction``
when the destination is a Bool tag (source-aware: literal True/1 → latch,
literal False/0 → reset, tag/expression → both).  All three are
INERT_WHEN_DISABLED=True — they only fire when their rung condition is
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
    _build_tag_map,
    _caller_conditions,
    _CallerMap,
    _collect_write_sites,
    _format_site_location,
    _resolve_tag_names,
    _resolve_tag_objects,
)
from pyrung.core.validation.severity import Severity

if TYPE_CHECKING:
    from pyrung.core.program import Program

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COIL_STUCK_HIGH = "COIL_STUCK_HIGH"
COIL_STUCK_LOW = "COIL_STUCK_LOW"

_COPY_LATCH = "CopyInstruction(latch)"
_COPY_RESET = "CopyInstruction(reset)"

# ---------------------------------------------------------------------------
# Latch/Reset target extractor
# ---------------------------------------------------------------------------


def _classify_copy_source_for_bool(source: Any) -> list[str]:
    """Classify a Copy source for Bool-destination stuck-bit analysis.

    Returns instruction-type labels for partitioning into latch/reset buckets.
    """
    if source is True:
        return [_COPY_LATCH]
    if source is False:
        return [_COPY_RESET]
    if isinstance(source, int) and not isinstance(source, bool):
        return [_COPY_RESET] if source == 0 else [_COPY_LATCH]
    return [_COPY_LATCH, _COPY_RESET]


def _latch_reset_write_targets(instr: Any) -> list[tuple[str, str]]:
    """Return (tag_name, instruction_type) pairs for latch/reset-like writes.

    Covers LatchInstruction, ResetInstruction, and CopyInstruction when the
    destination is a Bool tag (source-aware classification).
    """
    from pyrung.core.instruction.coils import LatchInstruction, ResetInstruction
    from pyrung.core.instruction.data_transfer import CopyInstruction
    from pyrung.core.tag import TagType

    if isinstance(instr, (LatchInstruction, ResetInstruction)):
        itype = type(instr).__name__
        return [(name, itype) for name in _resolve_tag_names(instr.target)]

    if isinstance(instr, CopyInstruction):
        results: list[tuple[str, str]] = []
        for tag in _resolve_tag_objects(instr.dest):
            if tag.type != TagType.BOOL:
                continue
            for itype in _classify_copy_source_for_bool(instr.source):
                results.append((tag.name, itype))
        return results

    return []


# ---------------------------------------------------------------------------
# Reachability analysis
# ---------------------------------------------------------------------------


def _conditions_provably_unreachable(conditions: tuple[Any, ...]) -> bool:
    """Check if an AND-chain of conditions is provably unsatisfiable.

    Uses per-tag domain feasibility to catch both pairwise contradictions
    and transitive unsatisfiability (e.g. ``CompareEq(T, 4) + CompareGt(T, 5)``).
    """
    from pyrung.core.validation._common import _conjunction_satisfiable

    return not _conjunction_satisfiable(conditions)


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
    severity: Severity = "warning"


def _site_signature(site: WriteSite) -> tuple[Any, ...]:
    """Location-only signature for a write site (ignores the target tag).

    Two findings whose reachable sites share this signature were produced by
    the *same instruction* — e.g. a range reset/fill that clears a whole block
    of coils — so they describe one pattern, not independent stuck bits.
    """
    return (
        site.scope,
        site.subroutine,
        site.rung_index,
        site.branch_path,
        site.instruction_index,
        site.instruction_type,
        site.source_file,
        site.source_line,
    )


@dataclass(frozen=True)
class StuckBitGroup:
    """A set of stuck-bit findings sharing kind, missing side, and write sites.

    A range-style instruction (block reset/fill) touching many tags produces
    one finding per tag.  Grouping collapses those into a single entry keyed on
    the shared reachable sites, so a 140-coil block clear reads as one pattern
    instead of 140 near-identical lines.  Member findings are preserved in
    ``findings`` so consumers can still expand to per-tag detail.
    """

    code: str
    kind: Literal["high", "low"]
    missing_side: str
    sites: tuple[WriteSite, ...]
    findings: tuple[StuckBitFinding, ...]

    @property
    def target_names(self) -> tuple[str, ...]:
        return tuple(f.target_name for f in self.findings)

    @property
    def common_prefix(self) -> str:
        """Longest shared name prefix across members ('' if none).

        A display label only — e.g. ``A_Alm`` for a bank of alarm coils.  Not
        used for grouping (that keys on the shared write site, not the name).
        """
        names = self.target_names
        if not names:
            return ""
        prefix = names[0]
        for name in names[1:]:
            while prefix and not name.startswith(prefix):
                prefix = prefix[:-1]
            if not prefix:
                return ""
        return prefix

    @property
    def message(self) -> str:
        if len(self.findings) == 1:
            return self.findings[0].message
        verb = "reset but never latched" if self.kind == "low" else "latched but never reset"
        loc_lines = "\n".join(f"  - {_format_site_location(s)}" for s in self.sites)
        names = ", ".join(self.target_names)
        return (
            f"{len(self.findings)} tags can be {verb} at the same site:\n"
            f"{loc_lines}\n"
            f"  tags: {names}"
        )


@dataclass(frozen=True)
class StuckBitReport:
    findings: tuple[StuckBitFinding, ...]

    def summary(self) -> str:
        if not self.findings:
            return "No stuck bits."
        return f"{len(self.findings)} stuck bit(s)."

    def grouped(self) -> tuple[StuckBitGroup, ...]:
        """Collapse findings sharing kind, missing side, and write-site signature.

        Findings produced by the same instruction (a block reset/fill that
        touches many tags) merge into one ``StuckBitGroup``.  A finding with a
        unique signature becomes a single-member group whose ``message`` is its
        original per-tag message.  Group order follows first appearance.
        """
        buckets: dict[tuple[Any, ...], list[StuckBitFinding]] = {}
        order: list[tuple[Any, ...]] = []
        for finding in self.findings:
            key = (
                finding.code,
                finding.missing_side,
                frozenset(_site_signature(s) for s in finding.reachable_sites),
            )
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(finding)

        groups: list[StuckBitGroup] = []
        for key in order:
            members = buckets[key]
            first = members[0]
            groups.append(
                StuckBitGroup(
                    code=first.code,
                    kind=first.kind,
                    missing_side=first.missing_side,
                    sites=first.reachable_sites,
                    findings=tuple(members),
                )
            )
        return tuple(groups)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_stuck_bits(program: Program) -> StuckBitReport:
    """Validate a Program for stuck latch/reset bits.

    Detects tags that can be latched but never reset (STUCK_HIGH) or reset
    but never latched (STUCK_LOW).

    Skips ``readonly`` tags (frozen constants) and ``external`` tags (the
    missing latch or reset side is handled outside the ladder).

    Returns a StuckBitReport with one finding per stuck tag.
    """
    from pyrung.core.instruction.coils import LatchInstruction, ResetInstruction

    sites = _collect_write_sites(program, target_extractor=_latch_reset_write_targets)
    caller_map = _build_caller_map(program)
    tag_map = _build_tag_map(program)

    # Partition sites by target and instruction type
    latch_types = {LatchInstruction.__name__, _COPY_LATCH}
    reset_types = {ResetInstruction.__name__, _COPY_RESET}

    latch_sites: dict[str, list[WriteSite]] = defaultdict(list)
    reset_sites: dict[str, list[WriteSite]] = defaultdict(list)

    for site in sites:
        if site.instruction_type in latch_types:
            latch_sites[site.target_name].append(site)
        elif site.instruction_type in reset_types:
            reset_sites[site.target_name].append(site)

    # All tags that have at least one latch or reset
    all_tags = sorted(set(latch_sites) | set(reset_sites))

    findings: list[StuckBitFinding] = []

    for tag_name in all_tags:
        # Skip readonly tags — they're frozen constants, not stuck-bit candidates
        tag = tag_map.get(tag_name)
        if tag is not None and tag.readonly:
            continue
        # Skip external tags — missing side is handled outside the ladder
        if tag is not None and tag.external:
            continue

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
                    code=COIL_STUCK_HIGH,
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
                    code=COIL_STUCK_LOW,
                    target_name=tag_name,
                    kind="low",
                    reachable_sites=tuple(reachable_resets),
                    missing_side="latch",
                    message=message,
                )
            )

    return StuckBitReport(findings=tuple(findings))
