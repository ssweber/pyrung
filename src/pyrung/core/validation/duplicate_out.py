"""Conflicting output target validation for pyrung programs.

Detects when multiple INERT_WHEN_DISABLED=False instructions target the same
tag from non-mutually-exclusive execution paths.  Such conflicts cause
last-writer-wins stomping at runtime.

Covered instruction types:
  OutInstruction, OnDelayInstruction, OffDelayInstruction,
  CountUpInstruction, CountDownInstruction, ShiftInstruction,
  EventDrumInstruction, TimeDrumInstruction.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.condition import (
    AllCondition,
    BitCondition,
    CompareEq,
    CompareGe,
    CompareGt,
    CompareLe,
    CompareLt,
    CompareNe,
    Condition,
    NormallyClosedCondition,
)
from pyrung.core.tag import ImmediateRef, Tag

if TYPE_CHECKING:
    from pyrung.core.program import Program
    from pyrung.core.rung import Rung

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

CORE_CONFLICTING_OUTPUT = "CORE_CONFLICTING_OUTPUT"

FactScope = Literal["main", "subroutine"]


@dataclass(frozen=True)
class OutputSite:
    """A single write-target site with its full condition context."""

    target_name: str
    scope: FactScope
    subroutine: str | None
    rung_index: int
    branch_path: tuple[int, ...]
    instruction_index: int
    instruction_type: str
    conditions: tuple[Condition, ...]
    source_file: str | None
    source_line: int | None


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
# Caller map type
# ---------------------------------------------------------------------------

# subroutine_name -> list of (scope, subroutine, rung_index, branch_path, conditions)
_CallerEntry = tuple[FactScope, str | None, int, tuple[int, ...], tuple[Condition, ...]]
_CallerMap = dict[str, list[_CallerEntry]]

# ---------------------------------------------------------------------------
# Target extraction helpers
# ---------------------------------------------------------------------------


def _resolve_tag_names(target: Any) -> list[str]:
    """Extract statically-known tag names from an output target."""
    from pyrung.core.memory_block import BlockRange, IndirectBlockRange

    if isinstance(target, ImmediateRef):
        return _resolve_tag_names(target.value)
    if isinstance(target, Tag):
        return [target.name]
    if isinstance(target, BlockRange):
        return [t.name for t in target.tags()]
    if isinstance(target, IndirectBlockRange):
        return []  # runtime-only, skip
    return []


def _instruction_write_targets(instr: Any) -> list[tuple[str, str]]:
    """Return (tag_name, instruction_type) pairs for write targets of an instruction.

    Only covers INERT_WHEN_DISABLED=False instruction types.
    """
    from pyrung.core.instruction.advanced import ShiftInstruction
    from pyrung.core.instruction.coils import OutInstruction
    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.instruction.drums import EventDrumInstruction, TimeDrumInstruction
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction

    itype = type(instr).__name__
    targets: list[str] = []

    if isinstance(instr, OutInstruction):
        targets = _resolve_tag_names(instr.target)
    elif isinstance(instr, (OnDelayInstruction, OffDelayInstruction)):
        targets = [instr.done_bit.name, instr.accumulator.name]
    elif isinstance(instr, (CountUpInstruction, CountDownInstruction)):
        targets = [instr.done_bit.name, instr.accumulator.name]
    elif isinstance(instr, ShiftInstruction):
        targets = _resolve_tag_names(instr.bit_range)
    elif isinstance(instr, TimeDrumInstruction):
        # TimeDrum before EventDrum (TimeDrum is subclass)
        targets = [t.name for t in instr.outputs]
        targets.append(instr.current_step.name)
        targets.append(instr.completion_flag.name)
        targets.append(instr.accumulator.name)
    elif isinstance(instr, EventDrumInstruction):
        targets = [t.name for t in instr.outputs]
        targets.append(instr.current_step.name)
        targets.append(instr.completion_flag.name)

    return [(name, itype) for name in targets]


# ---------------------------------------------------------------------------
# Program walking
# ---------------------------------------------------------------------------


def _collect_output_sites(program: Program) -> list[OutputSite]:
    """Walk program and collect all write-target sites from covered instructions."""
    from pyrung.core.instruction.control import ForLoopInstruction

    sites: list[OutputSite] = []

    def _walk_instructions(
        instructions: list[Any],
        scope: FactScope,
        subroutine: str | None,
        rung_index: int,
        branch_path: tuple[int, ...],
        conditions: tuple[Condition, ...],
    ) -> None:
        for instr_idx, instr in enumerate(instructions):
            for tag_name, itype in _instruction_write_targets(instr):
                sites.append(
                    OutputSite(
                        target_name=tag_name,
                        scope=scope,
                        subroutine=subroutine,
                        rung_index=rung_index,
                        branch_path=branch_path,
                        instruction_index=instr_idx,
                        instruction_type=itype,
                        conditions=conditions,
                        source_file=getattr(instr, "source_file", None),
                        source_line=getattr(instr, "source_line", None),
                    )
                )
            # Recurse into ForLoopInstruction children
            if isinstance(instr, ForLoopInstruction) and hasattr(instr, "instructions"):
                _walk_instructions(
                    instr.instructions,
                    scope,
                    subroutine,
                    rung_index,
                    branch_path,
                    conditions,
                )

    def _walk_rung(
        rung: Rung,
        scope: FactScope,
        subroutine: str | None,
        rung_index: int,
        branch_path: tuple[int, ...],
    ) -> None:
        conditions = tuple(rung._conditions)
        _walk_instructions(
            rung._instructions, scope, subroutine, rung_index, branch_path, conditions
        )
        for branch_idx, branch_rung in enumerate(rung._branches):
            _walk_rung(
                branch_rung,
                scope,
                subroutine,
                rung_index,
                branch_path + (branch_idx,),
            )

    # Main rungs
    for rung_index, rung in enumerate(program.rungs):
        _walk_rung(rung, "main", None, rung_index, ())

    # Subroutines
    for sub_name in sorted(program.subroutines):
        for rung_index, rung in enumerate(program.subroutines[sub_name]):
            _walk_rung(rung, "subroutine", sub_name, rung_index, ())

    return sites


def _build_caller_map(program: Program) -> _CallerMap:
    """Map subroutine name -> list of call sites with their conditions."""
    from pyrung.core.instruction.control import CallInstruction

    caller_map: _CallerMap = defaultdict(list)

    def _scan_rung(
        rung: Rung,
        scope: FactScope,
        subroutine: str | None,
        rung_index: int,
        branch_path: tuple[int, ...],
    ) -> None:
        conditions = tuple(rung._conditions)
        for instr in rung._instructions:
            if isinstance(instr, CallInstruction):
                caller_map[instr.subroutine_name].append(
                    (scope, subroutine, rung_index, branch_path, conditions)
                )
        for branch_idx, branch_rung in enumerate(rung._branches):
            _scan_rung(
                branch_rung,
                scope,
                subroutine,
                rung_index,
                branch_path + (branch_idx,),
            )

    for rung_index, rung in enumerate(program.rungs):
        _scan_rung(rung, "main", None, rung_index, ())

    for sub_name in sorted(program.subroutines):
        for rung_index, rung in enumerate(program.subroutines[sub_name]):
            _scan_rung(rung, "subroutine", sub_name, rung_index, ())

    return dict(caller_map)


# ---------------------------------------------------------------------------
# Caller conditions
# ---------------------------------------------------------------------------


def _caller_conditions(site: OutputSite, caller_map: _CallerMap) -> list[tuple[Condition, ...]]:
    """Return the caller condition chains for a subroutine-scope site.

    Only caller conditions matter for mutual exclusivity — the subroutine's
    internal rung conditions don't prevent stomping because out() actively
    writes when disabled (INERT_WHEN_DISABLED=False).

    Returns an empty list for uncalled subroutines (no conflict possible).
    """
    assert site.scope == "subroutine" and site.subroutine is not None
    callers = caller_map.get(site.subroutine, [])
    if not callers:
        return []  # uncalled subroutine
    return [caller_conds for _scope, _sub, _ri, _bp, caller_conds in callers]


# ---------------------------------------------------------------------------
# Mutual exclusivity detection
# ---------------------------------------------------------------------------


def _tag_name(tag_or_ref: Any) -> str | None:
    """Extract tag name, unwrapping ImmediateRef if needed."""
    if isinstance(tag_or_ref, ImmediateRef):
        if isinstance(tag_or_ref.value, Tag):
            return tag_or_ref.value.name
        return None
    if isinstance(tag_or_ref, Tag):
        return tag_or_ref.name
    return None


def _conditions_contradict(a: Condition, b: Condition) -> bool:
    """Check if two leaf conditions are provably contradictory."""
    # Pattern 1: CompareEq(same_tag, different_literal)
    if isinstance(a, CompareEq) and isinstance(b, CompareEq):
        if (
            _tag_name(a.tag) is not None
            and _tag_name(a.tag) == _tag_name(b.tag)
            and not isinstance(a.value, Tag)
            and not isinstance(b.value, Tag)
        ):
            return a.value != b.value

    # Pattern 2: BitCondition vs NormallyClosedCondition
    if isinstance(a, BitCondition) and isinstance(b, NormallyClosedCondition):
        na, nb = _tag_name(a.tag), _tag_name(b.tag)
        if na is not None and na == nb:
            return True
    if isinstance(a, NormallyClosedCondition) and isinstance(b, BitCondition):
        na, nb = _tag_name(a.tag), _tag_name(b.tag)
        if na is not None and na == nb:
            return True

    # Pattern 3: CompareEq vs CompareNe (same tag, same literal value)
    if isinstance(a, CompareEq) and isinstance(b, CompareNe):
        if (
            _tag_name(a.tag) is not None
            and _tag_name(a.tag) == _tag_name(b.tag)
            and not isinstance(a.value, Tag)
            and not isinstance(b.value, Tag)
        ):
            return a.value == b.value
    if isinstance(a, CompareNe) and isinstance(b, CompareEq):
        return _conditions_contradict(b, a)

    # Pattern 4: Range complements
    # CompareLt(T, N) vs CompareGe(T, N)
    if isinstance(a, CompareLt) and isinstance(b, CompareGe):
        if (
            _tag_name(a.tag) is not None
            and _tag_name(a.tag) == _tag_name(b.tag)
            and not isinstance(a.value, Tag)
            and not isinstance(b.value, Tag)
        ):
            return a.value == b.value
    if isinstance(a, CompareGe) and isinstance(b, CompareLt):
        return _conditions_contradict(b, a)

    # CompareLe(T, N) vs CompareGt(T, N)
    if isinstance(a, CompareLe) and isinstance(b, CompareGt):
        if (
            _tag_name(a.tag) is not None
            and _tag_name(a.tag) == _tag_name(b.tag)
            and not isinstance(a.value, Tag)
            and not isinstance(b.value, Tag)
        ):
            return a.value == b.value
    if isinstance(a, CompareGt) and isinstance(b, CompareLe):
        return _conditions_contradict(b, a)

    return False


def _flatten_and_conditions(conditions: tuple[Condition, ...]) -> list[Condition]:
    """Flatten AllCondition wrappers into leaf conditions.

    AnyCondition is treated as an opaque leaf (conservative).
    """
    result: list[Condition] = []
    for cond in conditions:
        if isinstance(cond, AllCondition):
            result.extend(_flatten_and_conditions(tuple(cond.conditions)))
        else:
            result.append(cond)
    return result


def _chain_pair_mutually_exclusive(
    chain_a: tuple[Condition, ...], chain_b: tuple[Condition, ...]
) -> bool:
    """Check if two AND-condition chains are provably mutually exclusive.

    Two chains are mutually exclusive if ANY pair of leaf conditions from them
    contradicts (since each chain is an AND of its conditions).
    """
    flat_a = _flatten_and_conditions(chain_a)
    flat_b = _flatten_and_conditions(chain_b)

    for cond_a in flat_a:
        for cond_b in flat_b:
            if _conditions_contradict(cond_a, cond_b):
                return True
    return False


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
# Message formatting
# ---------------------------------------------------------------------------


def _format_site_location(site: OutputSite) -> str:
    """Format a human-readable location for a site."""
    parts: list[str] = []
    if site.scope == "subroutine":
        parts.append(f"subroutine '{site.subroutine}'")
    parts.append(f"rung {site.rung_index}")
    if site.branch_path:
        parts.append(f"branch {'.'.join(str(b) for b in site.branch_path)}")
    parts.append(f"[{site.instruction_type}]")
    if site.source_file and site.source_line:
        parts.append(f"({site.source_file}:{site.source_line})")
    elif site.source_line:
        parts.append(f"(line {site.source_line})")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_conflicting_outputs(program: Program) -> ConflictingOutputReport:
    """Validate a Program for conflicting output targets.

    Detects when multiple INERT_WHEN_DISABLED=False instructions write to the
    same tag from non-mutually-exclusive execution paths.

    Returns a ConflictingOutputReport with one finding per conflicting target.
    """
    sites = _collect_output_sites(program)
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
