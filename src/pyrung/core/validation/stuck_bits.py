"""Stuck-bit validation for pyrung programs.

Detects latch/reset imbalances where a Bool tag can be latched but never
reset (stuck HIGH) or reset but never latched (stuck LOW).

Covers ``LatchInstruction``, ``ResetInstruction``, and ``CopyInstruction``
when the destination is a Bool tag (source-aware: literal True/1 → latch,
literal False/0 → reset, tag/expression → both).  All three are
INERT_WHEN_DISABLED=True — they only fire when their rung condition is
true — so *rung conditions matter* for reachability analysis (unlike the
INERT_WHEN_DISABLED=False conflicting-output validator).

``OutInstruction`` is covered too, because an ``out`` coil only self-clears on
the scans where it actually runs.  The question is asked of the *coil*, not of
one site: **is there a scan on which none of this coil's ``out`` instructions
execute?**  If so the coil holds its last value on that scan, the ``out``
supplies only the latch side, and something else must ``reset`` it — exactly as
if it had been written ``latch(...)``.

An ``out`` in the main program answers no by itself.  So does a set of ``out``
sites whose subroutines *cover* the state space between them — the state-machine
idiom where every state's subroutine drives the coil, so one of them writes it
every scan.  Coverage is proved via :func:`_chains_cover_domain`, and needs the
state tag to declare a closed domain (``choices=`` or ``min``/``max``); without
one, nothing rules out an unhandled state and the coil is reported.

Only *scope entry* matters here, never the rung's own condition: a rung that
runs and evaluates false has still run, and its ``out`` still drove the coil low.
What breaks the guarantee is the scan never reaching the instruction at all — a
conditional ``call``, or a ``return_early()`` above it.

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
    DomainMap,
    WriteSite,
    _build_caller_map,
    _build_tag_map,
    _caller_conditions,
    _CallerMap,
    _chains_cover_domain,
    _collect_write_sites,
    _domain_map,
    _flatten_and_conditions,
    _resolve_tag_names,
    _resolve_tag_objects,
    _scope_reach_chains,
    _tag_name,
    site_frame,
)
from pyrung.core.validation.display import FindingDisplay, _FindingTextMixin
from pyrung.core.validation.severity import Severity

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pyrung.core.condition import Condition
    from pyrung.core.program import Program
    from pyrung.core.tag import Tag

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COIL_STUCK_HIGH = "COIL_STUCK_HIGH"
COIL_STUCK_LOW = "COIL_STUCK_LOW"

_COPY_LATCH = "CopyInstruction(latch)"
_COPY_RESET = "CopyInstruction(reset)"

_OUT_LATCH = "OutInstruction(latch)"
_OUT_RESET = "OutInstruction(reset)"

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

    Covers LatchInstruction, ResetInstruction, CopyInstruction when the
    destination is a Bool tag (source-aware classification), and OutInstruction
    (both sides — whether the reset side is *guaranteed* is a property of the coil,
    which :func:`_self_clearing_coils` decides later).
    """
    from pyrung.core.instruction.coils import LatchInstruction, OutInstruction, ResetInstruction
    from pyrung.core.instruction.data_transfer import CopyInstruction
    from pyrung.core.tag import TagType

    if isinstance(instr, (LatchInstruction, ResetInstruction)):
        itype = type(instr).__name__
        return [(name, itype) for name in _resolve_tag_names(instr.target)]

    if isinstance(instr, OutInstruction):
        return [
            (tag.name, itype)
            for tag in _resolve_tag_objects(instr.target)
            if tag.type == TagType.BOOL
            for itype in (_OUT_LATCH, _OUT_RESET)
        ]

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
# Retentive out: a coil whose out() the scan can skip
# ---------------------------------------------------------------------------


def _return_rungs(program: Program) -> dict[str, tuple[int, ...]]:
    """Rung indices holding a ``return_early()``, per subroutine.

    An early return skips every rung below it, so an ``out`` under one cannot be
    proved to execute — even when the subroutine itself is called every scan.
    """
    from pyrung.core.instruction.control import ReturnInstruction

    returns: dict[str, list[int]] = defaultdict(list)

    def _scan(rung: Any, sub_name: str, rung_index: int) -> None:
        if any(isinstance(instr, ReturnInstruction) for instr in rung._instructions):
            returns[sub_name].append(rung_index)
        for branch in rung._branches:
            _scan(branch, sub_name, rung_index)

    for sub_name, sub_rungs in program.subroutines.items():
        for rung_index, rung in enumerate(sub_rungs):
            _scan(rung, sub_name, rung_index)

    return {name: tuple(indices) for name, indices in returns.items()}


def _site_run_chains(
    site: WriteSite,
    reach_chains: dict[str, tuple[tuple[Condition, ...], ...]],
    return_rungs: dict[str, tuple[int, ...]],
) -> tuple[tuple[Condition, ...], ...]:
    """The condition chains under which the scan executes this site's instruction.

    An OR of AND-chains — empty tuple when nothing can be proved (an uncalled
    subroutine, or a ``return_early()`` above the site that may skip it), and
    ``((),)`` — the always-true chain — for a site in the main program.
    """
    if site.scope == "main":
        return ((),)
    assert site.subroutine is not None
    if any(index < site.rung_index for index in return_rungs.get(site.subroutine, ())):
        return ()
    return reach_chains.get(site.subroutine, ())


def _self_clearing_coils(
    out_sites: dict[str, list[WriteSite]],
    reach_chains: dict[str, tuple[tuple[Condition, ...], ...]],
    return_rungs: dict[str, tuple[int, ...]],
    domains: DomainMap,
) -> set[str]:
    """Coils whose ``out`` instructions are proved to execute on *every* scan.

    Such a coil is a pure function of the scan — it de-energizes itself and can
    never freeze.  The union of every ``out`` site's run-chains must cover the
    state space: one unconditional site does it, and so does a state machine whose
    per-state subroutines all drive the coil, provided the state tag declares a
    closed domain.
    """
    self_clearing: set[str] = set()
    for tag_name, sites in out_sites.items():
        chains: list[tuple[Condition, ...]] = []
        for site in sites:
            chains.extend(_site_run_chains(site, reach_chains, return_rungs))
        if _chains_cover_domain(chains, domains):
            self_clearing.add(tag_name)
    return self_clearing


def _undeclared_domain_tags(
    sites: Iterable[WriteSite],
    reach_chains: dict[str, tuple[tuple[Condition, ...], ...]],
    return_rungs: dict[str, tuple[int, ...]],
    tag_map: dict[str, Tag],
    domains: DomainMap,
) -> list[str]:
    """Tags whose *missing* domain declaration is the only thing blocking coverage.

    An undeclared tag in the guards is not on its own worth reporting.  Most of these
    coils have a real gap that no declaration would close — a mode the ladder simply
    never drives them in — and pointing at whichever tag happens to lack a ``choices=``
    would send the engineer off to fix the wrong thing.  So the suggestion is *tested*
    first: give every undeclared tag the domain of the values its guards actually
    compare against, and see whether coverage then follows.  If it does, the missing
    declaration is the whole story and we say so.  If it does not, the gap is a
    genuinely undriven state and we stay quiet about domains.
    """
    from pyrung.core.tag import TagType

    chains: list[tuple[Condition, ...]] = []
    for site in sites:
        chains.extend(_site_run_chains(site, reach_chains, return_rungs))

    # The literal values each undeclared tag is compared against across the guards.
    mentioned: dict[str, set[int | float]] = defaultdict(set)
    for chain in chains:
        for cond in _flatten_and_conditions(chain):
            name = _tag_name(getattr(cond, "tag", None))
            tag = tag_map.get(name) if name is not None else None
            if tag is None or tag.type == TagType.BOOL or name in domains:
                continue
            value = getattr(cond, "value", None)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                mentioned[tag.name].add(value)

    if not mentioned:
        return []

    assumed: DomainMap = {**domains, **mentioned}
    if not _chains_cover_domain(chains, assumed):
        return []  # a declaration would not close this gap — do not send them after it
    return sorted(mentioned)


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
class StuckBitFinding(_FindingTextMixin):
    """A stuck-bit finding for a single tag."""

    code: str
    target_name: str
    kind: Literal["high", "low"]
    reachable_sites: tuple[WriteSite, ...]
    missing_side: str
    display: FindingDisplay
    severity: Severity = "warning"

    @property
    def message(self) -> str:
        return self.display.as_text()


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
    def display(self) -> FindingDisplay:
        """Presentation view: the lone member's own display, or a merged one."""
        if len(self.findings) == 1:
            return self.findings[0].display
        # low = reset here but latched nowhere; high = latched here but reset nowhere.
        here, nowhere, verb = (
            ("reset", "latched", "latch") if self.kind == "low" else ("latched", "reset", "reset")
        )
        names = ", ".join(self.target_names)
        return FindingDisplay(
            code=self.code,
            severity=self.findings[0].severity,
            frames=tuple(site_frame(s) for s in self.sites),
            problem=f"{len(self.findings)} coils are {here} here, {nowhere} nowhere: {names}.",
            hint=f"did you forget to {verb} them?",
        )

    @property
    def message(self) -> str:
        return self.display.as_text()


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
    domains = _domain_map(tag_map)
    reach_chains = _scope_reach_chains(program)
    return_rungs = _return_rungs(program)

    # Partition sites by target and instruction type
    latch_types = {LatchInstruction.__name__, _COPY_LATCH, _OUT_LATCH}
    reset_types = {ResetInstruction.__name__, _COPY_RESET, _OUT_RESET}

    out_sites: dict[str, list[WriteSite]] = defaultdict(list)
    for site in sites:
        if site.instruction_type == _OUT_LATCH:
            out_sites[site.target_name].append(site)

    # Coils whose out() instructions run on every scan de-energize themselves; the
    # rest hold their last value on the scans nothing writes them, and their out()
    # is a latch with no matching reset.
    self_clearing = _self_clearing_coils(out_sites, reach_chains, return_rungs, domains)

    latch_sites: dict[str, list[WriteSite]] = defaultdict(list)
    reset_sites: dict[str, list[WriteSite]] = defaultdict(list)

    for site in sites:
        if site.instruction_type in latch_types:
            latch_sites[site.target_name].append(site)
        elif site.instruction_type in reset_types:
            if site.instruction_type == _OUT_RESET and site.target_name not in self_clearing:
                continue
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
            held = [
                s
                for s in reachable_latches
                if s.instruction_type == _OUT_LATCH and s.target_name not in self_clearing
            ]
            hint = f"did you forget a reset({tag_name})?"
            if held:
                subs = ", ".join(sorted({s.subroutine or "Main" for s in held}))
                hint = (
                    f"an out() in {subs} holds its last value on the scans it does not "
                    f"run; reset({tag_name}) when it stops running?"
                )
                undeclared = _undeclared_domain_tags(
                    held, reach_chains, return_rungs, tag_map, domains
                )
                if undeclared:
                    names = ", ".join(undeclared)
                    hint += (
                        f" the out() rungs do cover every state; declare the domain of "
                        f"{names} (choices= or min=/max=) and this finding goes away."
                    )
            findings.append(
                StuckBitFinding(
                    code=COIL_STUCK_HIGH,
                    target_name=tag_name,
                    kind="high",
                    reachable_sites=tuple(reachable_latches),
                    missing_side="reset",
                    display=FindingDisplay(
                        code=COIL_STUCK_HIGH,
                        severity="warning",
                        frames=tuple(
                            site_frame(
                                s,
                                caret_label=(
                                    "held high when skipped" if s in held else "never reset"
                                ),
                            )
                            for s in reachable_latches
                        ),
                        hint=hint,
                    ),
                )
            )
        elif reachable_resets and not reachable_latches:
            findings.append(
                StuckBitFinding(
                    code=COIL_STUCK_LOW,
                    target_name=tag_name,
                    kind="low",
                    reachable_sites=tuple(reachable_resets),
                    missing_side="latch",
                    display=FindingDisplay(
                        code=COIL_STUCK_LOW,
                        severity="warning",
                        frames=tuple(
                            site_frame(s, caret_label="never latched") for s in reachable_resets
                        ),
                        hint=f"did you forget a latch({tag_name})?",
                    ),
                )
            )

    return StuckBitReport(findings=tuple(findings))
