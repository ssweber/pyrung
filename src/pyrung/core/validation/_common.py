"""Shared machinery for pyrung program validators.

Provides site-collection walking, caller-map construction, condition
contradiction detection, and formatting helpers used by multiple
validation passes (conflicting outputs, stuck bits, etc.).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
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
# Shared types
# ---------------------------------------------------------------------------

FactScope = Literal["main", "subroutine"]


@dataclass(frozen=True)
class WriteSite:
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


def _resolve_tag_objects(target: Any) -> list[Tag]:
    """Extract Tag objects from an output target."""
    from pyrung.core.memory_block import BlockRange, IndirectBlockRange

    if isinstance(target, ImmediateRef):
        return _resolve_tag_objects(target.value)
    if isinstance(target, Tag):
        return [target]
    if isinstance(target, BlockRange):
        return list(target.tags())
    if isinstance(target, IndirectBlockRange):
        return []
    return []


def _build_tag_map(program: Program) -> dict[str, Tag]:
    """Build a name→Tag map from all tag references in a program."""
    tag_map: dict[str, Tag] = {}

    def _collect_from_rung(rung: Any) -> None:
        for cond in rung._conditions:
            tag_obj = getattr(cond, "tag", None)
            if tag_obj is not None:
                raw = tag_obj
                if isinstance(raw, ImmediateRef):
                    raw = object.__getattribute__(raw, "value")
                if isinstance(raw, Tag) and raw.name not in tag_map:
                    tag_map[raw.name] = raw
        for instr in rung._instructions:
            target = getattr(instr, "target", None)
            if target is not None:
                for tag_obj in _resolve_tag_objects(target):
                    if tag_obj.name not in tag_map:
                        tag_map[tag_obj.name] = tag_obj
            source = getattr(instr, "source", None)
            if source is not None:
                for tag_obj in _resolve_tag_objects(source):
                    if tag_obj.name not in tag_map:
                        tag_map[tag_obj.name] = tag_obj
        for branch in rung._branches:
            _collect_from_rung(branch)

    for rung in program.rungs:
        _collect_from_rung(rung)
    for sub_name in program.subroutines:
        for rung in program.subroutines[sub_name]:
            _collect_from_rung(rung)

    return tag_map


def _instruction_write_targets(instr: Any) -> list[tuple[str, str]]:
    """Return (tag_name, instruction_type) pairs for write targets of an instruction.

    Only covers INERT_WHEN_DISABLED=False instruction types.
    """
    from pyrung.core.instruction.advanced import ShiftInstruction
    from pyrung.core.instruction.coils import OutInstruction
    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.instruction.drums import EventDrumInstruction, TimeDrumInstruction
    from pyrung.core.instruction.send_receive import (
        ModbusReceiveInstruction,
        ModbusSendInstruction,
    )
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
    elif isinstance(instr, ModbusSendInstruction):
        targets = [
            instr.sending.name,
            instr.success.name,
            instr.error.name,
            instr.exception_response.name,
        ]
    elif isinstance(instr, ModbusReceiveInstruction):
        targets = [
            instr.receiving.name,
            instr.success.name,
            instr.error.name,
            instr.exception_response.name,
        ]

    return [(name, itype) for name in targets]


# ---------------------------------------------------------------------------
# Program walking
# ---------------------------------------------------------------------------


def walk_instructions(program: Program):
    """Yield every instruction in a program (flat).

    Traverses main rungs, branches, subroutines, and ForLoop bodies.
    """
    from pyrung.core.instruction.control import ForLoopInstruction

    def _from_rung(rung: Rung):  # type: ignore[name-defined]
        for instr in rung._instructions:
            yield instr
            if isinstance(instr, ForLoopInstruction) and hasattr(instr, "instructions"):
                yield from _from_instructions(instr.instructions)
        for branch in rung._branches:
            yield from _from_rung(branch)

    def _from_instructions(instructions: list):  # type: ignore[type-arg]
        for instr in instructions:
            yield instr
            if isinstance(instr, ForLoopInstruction) and hasattr(instr, "instructions"):
                yield from _from_instructions(instr.instructions)

    for rung in program.rungs:
        yield from _from_rung(rung)
    for sub_rungs in program.subroutines.values():
        for rung in sub_rungs:
            yield from _from_rung(rung)


def _collect_write_sites(
    program: Program,
    *,
    target_extractor: Any = None,
) -> list[WriteSite]:
    """Walk program and collect write-target sites.

    Parameters
    ----------
    program : Program
        The program to walk.
    target_extractor : callable, optional
        A function ``(instruction) -> list[tuple[str, str]]`` returning
        ``(tag_name, instruction_type)`` pairs.  Defaults to
        :func:`_instruction_write_targets` (INERT_WHEN_DISABLED=False types).
    """
    from pyrung.core.instruction.control import ForLoopInstruction

    if target_extractor is None:
        target_extractor = _instruction_write_targets

    sites: list[WriteSite] = []

    def _walk_instructions(
        instructions: list[Any],
        scope: FactScope,
        subroutine: str | None,
        rung_index: int,
        branch_path: tuple[int, ...],
        conditions: tuple[Condition, ...],
    ) -> None:
        for instr_idx, instr in enumerate(instructions):
            for tag_name, itype in target_extractor(instr):
                sites.append(
                    WriteSite(
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


def _caller_conditions(site: WriteSite, caller_map: _CallerMap) -> list[tuple[Condition, ...]]:
    """Return the caller condition chains for a subroutine-scope site.

    Returns an empty list for uncalled subroutines (no callers found).
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


_EPS = 1e-9


def _tag_domain_feasible(conds: list[Condition]) -> bool:
    """Check if conditions constraining a single tag have a feasible domain.

    Bool conditions (BitCondition/NormallyClosedCondition) constrain a set
    {True, False}.  Numeric conditions (Compare*) constrain an interval
    [lo, hi] plus equality/inequality point constraints.
    """
    # -- Bool domain: narrowed by bit conditions ----------------------------
    bool_domain: set[bool] | None = None

    # -- Numeric domain -----------------------------------------------------
    lo = float("-inf")
    hi = float("inf")
    eq_point: int | float | None = None
    ne_points: set[int | float] = set()
    has_numeric = False

    # Determine discrete vs continuous from literal value types (first pass).
    is_continuous = any(
        isinstance(c.value, float)
        for c in conds
        if isinstance(c, (CompareEq, CompareNe, CompareGt, CompareGe, CompareLt, CompareLe))
        and not isinstance(c.value, Tag)
    )

    for cond in conds:
        # -- Bool constraints -----------------------------------------------
        if isinstance(cond, BitCondition):
            if bool_domain is None:
                bool_domain = {True, False}
            bool_domain &= {True}
            continue

        if isinstance(cond, NormallyClosedCondition):
            if bool_domain is None:
                bool_domain = {True, False}
            bool_domain &= {False}
            continue

        # -- Numeric constraints (Compare*) ---------------------------------
        assert isinstance(cond, (CompareEq, CompareNe, CompareGt, CompareGe, CompareLt, CompareLe))

        if isinstance(cond.value, Tag):
            continue  # tag-vs-tag: can't constrain statically
        val = cond.value
        if not isinstance(val, (int, float)):
            continue  # non-numeric literal: skip

        has_numeric = True

        if isinstance(cond, CompareEq):
            if eq_point is not None and eq_point != val:
                return False  # two different equality pins
            eq_point = val
        elif isinstance(cond, CompareNe):
            ne_points.add(val)
        elif isinstance(cond, CompareGt):
            new_lo = val + (_EPS if is_continuous else 1)
            lo = max(lo, new_lo)
        elif isinstance(cond, CompareGe):
            lo = max(lo, val)
        elif isinstance(cond, CompareLt):
            new_hi = val - (_EPS if is_continuous else 1)
            hi = min(hi, new_hi)
        elif isinstance(cond, CompareLe):
            hi = min(hi, val)

    # -- Feasibility checks -------------------------------------------------
    if bool_domain is not None and not bool_domain:
        return False

    if has_numeric:
        if eq_point is not None:
            if eq_point < lo or eq_point > hi:
                return False
            if eq_point in ne_points:
                return False
        else:
            if lo > hi:
                return False
            if lo == hi and lo in ne_points:
                return False

    return True


def _conjunction_satisfiable(conditions: Iterable[Condition]) -> bool:
    """Check whether a conjunction (AND) of conditions is satisfiable.

    Groups leaf conditions by tag and checks per-tag domain feasibility.
    Strictly stronger than pairwise ``_conditions_contradict`` — catches
    transitive cases like ``CompareEq(T, 4) + CompareGt(T, 5)``.

    AnyCondition, edge conditions, and indirect comparisons are treated as
    opaque (always satisfiable) — conservative by design.
    """
    flat = _flatten_and_conditions(tuple(conditions))

    # Group constrainable leaf conditions by tag name.
    groups: dict[str, list[Condition]] = defaultdict(list)
    for cond in flat:
        if isinstance(
            cond,
            (
                BitCondition,
                NormallyClosedCondition,
                CompareEq,
                CompareNe,
                CompareGt,
                CompareGe,
                CompareLt,
                CompareLe,
            ),
        ):
            name = _tag_name(cond.tag)
            if name is not None:
                groups[name].append(cond)

    for conds in groups.values():
        if not _tag_domain_feasible(conds):
            return False

    return True


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

    Two chains are mutually exclusive when their combined conjunction is
    unsatisfiable (i.e. no assignment can make both chains true at once).
    """
    flat_a = _flatten_and_conditions(chain_a)
    flat_b = _flatten_and_conditions(chain_b)

    return not _conjunction_satisfiable(flat_a + flat_b)


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def _format_site_location(site: WriteSite) -> str:
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
