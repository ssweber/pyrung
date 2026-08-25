"""Shared machinery for pyrung program validators.

Provides site-collection walking, caller-map construction, condition
contradiction detection, and formatting helpers used by multiple
validation passes (conflicting outputs, stuck bits, etc.).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.condition import (
    AllCondition,
    AnyCondition,
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
    from pyrung.core.validation.display import Frame

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
    # The live instruction that produced this write, carried so a display can render
    # it as DSL code at build time.  ``compare=False`` keeps eq/hash (and the
    # signature-based dedup in stuck_bits) independent of the instruction identity.
    instruction: Any = field(default=None, compare=False)


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


@dataclass(frozen=True)
class RungLoc:
    """Structured location of a rung/branch, with a compact traceback form.

    ``str(loc)`` and ``loc.compact`` both give ``Main:R5`` / ``SFCExample:R2`` — a
    stable id usable as a finding's ``target_name`` and as a ``Frame.location``.
    """

    scope: FactScope
    subroutine: str | None
    rung_index: int
    branch_path: tuple[int, ...] = ()

    @property
    def compact(self) -> str:
        return compact_location(self.scope, self.subroutine, self.rung_index, self.branch_path)

    def __str__(self) -> str:
        return self.compact


def iter_rungs(program: Program):
    """Yield ``(RungLoc, rung)`` for every rung: main, subroutines, branches.

    Each branch is its own rung with its own ``_conditions`` — a branch whose
    own conditions matter is walked in its own right.  Shared by the rung- and
    comparison-condition validators so both describe locations identically.
    """

    def _walk(rung, scope, subroutine, rung_index, branch_path):  # type: ignore[no-untyped-def]
        yield RungLoc(scope, subroutine, rung_index, branch_path), rung
        for branch_idx, branch in enumerate(rung._branches):
            yield from _walk(branch, scope, subroutine, rung_index, branch_path + (branch_idx,))

    for rung_index, rung in enumerate(program.rungs):
        yield from _walk(rung, "main", None, rung_index, ())
    for sub_name in sorted(program.subroutines):
        for rung_index, rung in enumerate(program.subroutines[sub_name]):
            yield from _walk(rung, "subroutine", sub_name, rung_index, ())


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
                        instruction=instr,
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

# Cap on enumerating a min/max range into a value set.
_MAX_ENUMERATED_DOMAIN = 4096

# tag name -> the finite set of values it can hold (from ``choices=`` or ``min``/``max``).
# A tag absent from the map has an open domain: nothing rules out a value the program
# never mentions, so no coverage over it can be proved.
DomainMap = dict[str, set[int | float]]


def _tag_domain_values(tag: Tag | None) -> set[int | float] | None:
    """The finite value set a tag is declared to live in, or ``None`` if open.

    A `choices=` map pins the domain outright; an integer `min`/`max` pair encloses
    it.  Without one of those an ``Int`` is open — nothing rules out a value the
    program never mentions — and coverage over it cannot be proved.
    """
    from pyrung.core.tag import TagType

    if tag is None:
        return None
    if tag.choices:
        return {v for v in tag.choices if isinstance(v, (int, float))}
    if tag.type in (TagType.INT, TagType.DINT, TagType.WORD):
        lo, hi = tag.min, tag.max
        if isinstance(lo, int) and isinstance(hi, int) and 0 <= hi - lo < _MAX_ENUMERATED_DOMAIN:
            return set(range(lo, hi + 1))
    return None


_COMPARE_HOLDS: dict[type, Any] = {
    CompareEq: lambda v, lit: v == lit,
    CompareNe: lambda v, lit: v != lit,
    CompareGt: lambda v, lit: v > lit,
    CompareGe: lambda v, lit: v >= lit,
    CompareLt: lambda v, lit: v < lit,
    CompareLe: lambda v, lit: v <= lit,
}


def _domain_map(tag_map: dict[str, Tag]) -> DomainMap:
    """The declared finite domain of every tag that has one, keyed by name."""
    domains: DomainMap = {}
    for name, tag in tag_map.items():
        values = _tag_domain_values(tag)
        if values is not None:
            domains[name] = values
    return domains


def _value_satisfies(value: int | float, conds: Sequence[Condition]) -> bool:
    """Whether a concrete value satisfies every (literal-valued) condition on its tag."""
    for cond in conds:
        if not isinstance(cond, (CompareEq, CompareNe, CompareGt, CompareGe, CompareLt, CompareLe)):
            continue
        literal = cond.value
        if isinstance(literal, ImmediateRef):
            literal = literal.value
        if not isinstance(literal, (int, float)) or isinstance(literal, bool):
            continue  # tag-valued or non-numeric: no constraint we can check here
        if not _COMPARE_HOLDS[type(cond)](value, literal):
            return False
    return True


def _tag_domain_feasible(conds: list[Condition], domain: set[int | float] | None = None) -> bool:
    """Check if conditions constraining a single tag have a feasible domain.

    Bool conditions (BitCondition/NormallyClosedCondition) constrain a set
    {True, False}.  Numeric conditions (Compare*) constrain an interval
    [lo, hi] plus equality/inequality point constraints.

    When the tag has a declared finite *domain* (``choices=`` or an integer
    ``min``/``max`` pair), its values are enumerated against the conditions as well —
    that is what makes an exhaustive set of ``!=`` constraints infeasible, and so what
    lets a caller prove a state machine's guards *cover* their state variable.
    """
    if domain is not None:
        compares = [
            c
            for c in conds
            if isinstance(c, (CompareEq, CompareNe, CompareGt, CompareGe, CompareLt, CompareLe))
        ]
        if compares and not any(_value_satisfies(v, compares) for v in domain):
            return False

    # -- Bool domain: narrowed by bit conditions ----------------------------
    bool_domain: set[bool] | None = None

    # -- Numeric domain -----------------------------------------------------
    lo = float("-inf")
    hi = float("inf")
    eq_point: int | float | None = None
    ne_points: set[int | float] = set()
    has_numeric = False

    # -- Tag-value comparisons (same value Tag, complementary operators) ----
    tag_value_ops: dict[str, set[type]] = defaultdict(set)

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

        val = cond.value
        if isinstance(val, ImmediateRef):
            val = val.value
        if isinstance(val, Tag):
            tag_value_ops[val.name].add(type(cond))
            continue

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

    # Tag-value: complementary operators on the same value Tag are infeasible
    # (e.g. CompareEq(X, Y) + CompareNe(X, Y) can never both be true)
    for ops in tag_value_ops.values():
        if CompareEq in ops and CompareNe in ops:
            return False
        if CompareLt in ops and CompareGe in ops:
            return False
        if CompareLe in ops and CompareGt in ops:
            return False

    return True


def _conjunction_satisfiable(
    conditions: Iterable[Condition], domains: DomainMap | None = None
) -> bool:
    """Check whether a conjunction (AND) of conditions is satisfiable.

    Groups leaf conditions by tag and checks per-tag domain feasibility.
    Strictly stronger than pairwise ``_conditions_contradict`` — catches
    transitive cases like ``CompareEq(T, 4) + CompareGt(T, 5)``.

    AnyCondition, edge conditions, and indirect comparisons are treated as
    opaque (always satisfiable) — conservative by design.

    Pass *domains* (see :func:`_domain_map`) to bring declared tag domains
    (``choices=`` / ``min``/``max``) into the check; without them a tag's domain is
    open and a conjunction of ``!=`` constraints is always satisfiable.
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

    for name, conds in groups.items():
        if not _tag_domain_feasible(conds, domains.get(name) if domains else None):
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


# ---------------------------------------------------------------------------
# OTE-alias resolution (one-hot mode-decode exclusivity)
# ---------------------------------------------------------------------------

# Cap on distributed-normal-form clause count; past it, bail to the opaque path.
_MAX_DNF_CLAUSES = 512
# Cap on alias-substitution recursion depth (a bit whose driver is itself a bit).
_MAX_ALIAS_DEPTH = 8

# tag name -> tuple of writer-rung condition chains (the tag is true iff the OR of
# these chains holds — an over-approximation, exact for a single writer).
_AliasMap = dict[str, tuple[tuple[Condition, ...], ...]]


def _build_ote_alias_map(program: Program) -> _AliasMap:
    """Map each purely-combinational bit tag to its OTE writers' condition chains.

    A tag qualifies only when *every* writer is a non-oneshot ``out`` coil (an OTE
    bit).  Then ``tag == OR(writer conditions)`` holds combinationally each scan, so
    a positive ``BitCondition(tag)`` can be substituted by that disjunction when
    checking mutual exclusivity — e.g. ``S_ProductionMode`` decoded by
    ``with rung(S_UnitModeCurrent == 1): out(S_ProductionMode)`` resolves to
    ``S_UnitModeCurrent == 1``, exposing its exclusivity with ``S_ManualMode``
    (``== 3``).  Stateful writers (latch/reset/copy/timer/counter) disqualify a tag:
    its value is not a static function of one scan's rung condition.

    Built on the shared :func:`~pyrung.core.analysis.sp_values.writer_value_facts`
    primitive (the same source-alias recognition PILOT reads), so the
    "which bit means which register value" logic lives in one place.
    """
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.sp_values import writer_value_facts

    graph = build_program_graph(program)
    facts_by_tag = writer_value_facts(program, graph)

    alias_map: _AliasMap = {}
    for tag_name, writers in graph.writers_of.items():
        facts = facts_by_tag.get(tag_name, ())
        ote_facts = [f for f in facts if f.is_ote_bit]
        # Every writer must be an OTE bit for the disjunction to be exact/sound.
        if not ote_facts or len(ote_facts) != len(writers):
            continue
        alias_map[tag_name] = tuple(f.conditions for f in ote_facts)
    return alias_map


def _cond_to_dnf(cond: Condition, alias_map: _AliasMap, depth: int) -> list[list[Condition]] | None:
    """Expand one condition into DNF clauses (each a flat leaf list).

    ``And``/``Any`` structure is distributed; a positive aliased ``BitCondition`` is
    substituted by its writers' (OR of) condition chains.  Negated aliases are left
    opaque — negating the disjunction would *under*-approximate and could fabricate
    exclusivity.  Returns ``None`` on clause-count blow-up (caller falls back).
    """
    if isinstance(cond, AllCondition):
        return _expand_to_dnf(tuple(cond.conditions), alias_map, depth)

    if isinstance(cond, AnyCondition):
        frags: list[list[Condition]] = []
        for child in cond.conditions:
            sub = _cond_to_dnf(child, alias_map, depth)
            if sub is None:
                return None
            frags.extend(sub)
        return frags

    if isinstance(cond, BitCondition) and depth < _MAX_ALIAS_DEPTH:
        name = _tag_name(cond.tag)
        chains = alias_map.get(name) if name is not None else None
        if chains is not None:
            frags = []
            for chain in chains:
                sub = _expand_to_dnf(chain, alias_map, depth + 1)
                if sub is None:
                    return None
                frags.extend(sub)
            return frags if frags else [[cond]]

    return [[cond]]


def _expand_to_dnf(
    conditions: tuple[Condition, ...], alias_map: _AliasMap, depth: int = 0
) -> list[list[Condition]] | None:
    """DNF clauses whose OR equals ``AND(conditions)`` after alias substitution.

    Returns ``None`` if expansion would exceed :data:`_MAX_DNF_CLAUSES`.
    """
    clauses: list[list[Condition]] = [[]]
    for cond in conditions:
        sub = _cond_to_dnf(cond, alias_map, depth)
        if sub is None:
            return None
        new_clauses: list[list[Condition]] = []
        for clause in clauses:
            for frag in sub:
                new_clauses.append(clause + frag)
                if len(new_clauses) > _MAX_DNF_CLAUSES:
                    return None
        clauses = new_clauses
    return clauses


def _chain_pair_mutually_exclusive(
    chain_a: tuple[Condition, ...],
    chain_b: tuple[Condition, ...],
    alias_map: _AliasMap | None = None,
) -> bool:
    """Check if two AND-condition chains are provably mutually exclusive.

    Two chains are mutually exclusive when their combined conjunction is
    unsatisfiable (i.e. no assignment can make both chains true at once).

    When *alias_map* is supplied, positive combinational-bit conditions are first
    resolved to their driving register comparisons and the ``And``/``Or`` structure
    distributed to DNF, so one-hot mode decodes (``S_ProductionMode`` vs
    ``S_ManualMode``, both from ``S_UnitModeCurrent``) are recognised as exclusive.
    The check is sound: substitution only weakens each chain (an over-approximation),
    so a proven-unsatisfiable result is never a false claim of exclusivity.
    """
    if alias_map:
        clauses = _expand_to_dnf(tuple(chain_a) + tuple(chain_b), alias_map)
        if clauses is not None:
            return all(not _conjunction_satisfiable(clause) for clause in clauses)
        # Blow-up: fall through to the opaque (no-alias) check.

    flat_a = _flatten_and_conditions(chain_a)
    flat_b = _flatten_and_conditions(chain_b)

    return not _conjunction_satisfiable(flat_a + flat_b)


# ---------------------------------------------------------------------------
# Coverage detection (the dual of mutual exclusivity)
# ---------------------------------------------------------------------------


def _negate_condition(cond: Condition) -> list[list[Condition]] | None:
    """DNF of ``¬cond`` — a list of clauses, each a conjunction of leaf conditions.

    Returns ``None`` when the condition cannot be negated exactly (an edge, an
    indirect comparison, a truthiness test): a caller proving coverage must then
    give up rather than guess, since a wrong negation would fabricate coverage.
    """
    if isinstance(cond, BitCondition):
        return [[NormallyClosedCondition(cond.tag)]]
    if isinstance(cond, NormallyClosedCondition):
        return [[BitCondition(cond.tag)]]

    if isinstance(cond, (CompareEq, CompareNe, CompareGt, CompareGe, CompareLt, CompareLe)):
        flips: dict[type, Any] = {
            CompareEq: CompareNe,
            CompareNe: CompareEq,
            CompareGt: CompareLe,
            CompareLe: CompareGt,
            CompareGe: CompareLt,
            CompareLt: CompareGe,
        }
        return [[flips[type(cond)](cond.tag, cond.value)]]

    # ¬(a ∧ b) = ¬a ∨ ¬b — one clause per disjunct.
    if isinstance(cond, AllCondition):
        clauses: list[list[Condition]] = []
        for child in cond.conditions:
            sub = _negate_condition(child)
            if sub is None:
                return None
            clauses.extend(sub)
        return clauses

    # ¬(a ∨ b) = ¬a ∧ ¬b — the cross product of the disjuncts' negations.
    if isinstance(cond, AnyCondition):
        clauses = [[]]
        for child in cond.conditions:
            sub = _negate_condition(child)
            if sub is None:
                return None
            merged: list[list[Condition]] = []
            for clause in clauses:
                for frag in sub:
                    merged.append(clause + frag)
                    if len(merged) > _MAX_DNF_CLAUSES:
                        return None
            clauses = merged
        return clauses

    return None


def _chains_cover_domain(
    chains: Sequence[tuple[Condition, ...]], domains: DomainMap | None = None
) -> bool:
    """Whether an OR of AND-chains is a tautology — some chain holds on every scan.

    The dual of :func:`_chain_pair_mutually_exclusive`: that one proves guards never
    overlap, this one proves they leave no gap.  A state machine whose subroutines are
    called on ``State == 1 / 2 / 3`` covers its domain — but only if ``State`` is
    *declared* to live in ``{1, 2, 3}`` (via ``choices=`` or ``min``/``max``), which is
    why *domains* is what makes the proof possible.  Without a closed domain nothing
    rules out ``State == 7``, and the honest answer is False.

    Sound, not complete: an un-negatable guard (an edge, an indirect comparison) or a
    DNF blow-up returns False.  Coverage is only ever *proved*, never assumed.
    """
    if not chains:
        return False

    # ¬OR(chains) = AND over chains of ¬chain.  Negate each chain to DNF, then
    # distribute: a clause takes one disjunct from every chain.  Coverage holds iff
    # every such clause is unsatisfiable — no assignment escapes all the chains.
    per_chain: list[list[list[Condition]]] = []
    for chain in chains:
        flat = _flatten_and_conditions(chain)
        if not flat:
            return True  # an unconditional chain covers everything on its own
        negated: list[list[Condition]] = []
        for cond in flat:
            sub = _negate_condition(cond)
            if sub is None:
                return False
            negated.extend(sub)
        per_chain.append(negated)

    clauses: list[list[Condition]] = [[]]
    for negated in per_chain:
        merged: list[list[Condition]] = []
        for clause in clauses:
            for frag in negated:
                merged.append(clause + frag)
                if len(merged) > _MAX_DNF_CLAUSES:
                    return False
        clauses = merged

    return all(not _conjunction_satisfiable(clause, domains) for clause in clauses)


# ---------------------------------------------------------------------------
# Subroutine reachability chains
# ---------------------------------------------------------------------------

# Cap on the reach chains kept for one subroutine (a deep call web).
_MAX_REACH_CHAINS = 64


def _scope_reach_chains(program: Program) -> dict[str, tuple[tuple[Condition, ...], ...]]:
    """Per subroutine, the condition chains under which the scan *enters* it.

    An OR of AND-chains: each is one path from the main program down to a ``call``,
    carrying every rung and enclosing-branch condition along the way.  A subroutine
    reached by an unconditional call from the main program gets the empty chain — it
    runs on every scan.  Uncalled subroutines get no chains at all.

    This is scope entry only.  It says nothing about the rungs *inside* the
    subroutine — a rung that runs and evaluates false has still run.
    """
    from pyrung.core.instruction.control import CallInstruction

    # sub_name -> list of (enclosing subroutine or None for main, chain within that scope)
    calls: dict[str, list[tuple[str | None, tuple[Condition, ...]]]] = defaultdict(list)

    def _scan(rung: Rung, subroutine: str | None, chain: tuple[Condition, ...]) -> None:
        # A branch is gated by its own conditions on top of its parent's.
        chain = chain + tuple(rung._conditions)
        for instr in rung._instructions:
            if isinstance(instr, CallInstruction):
                calls[instr.subroutine_name].append((subroutine, chain))
        for branch in rung._branches:
            _scan(branch, subroutine, chain)

    for rung in program.rungs:
        _scan(rung, None, ())
    for sub_name, sub_rungs in program.subroutines.items():
        for rung in sub_rungs:
            _scan(rung, sub_name, ())

    memo: dict[str, tuple[tuple[Condition, ...], ...]] = {}

    def _resolve(sub_name: str, active: frozenset[str]) -> tuple[tuple[Condition, ...], ...]:
        if sub_name in memo:
            return memo[sub_name]
        chains: list[tuple[Condition, ...]] = []
        for caller, chain in calls.get(sub_name, []):
            if caller is None:
                chains.append(chain)
            elif caller not in active:  # a call cycle contributes no path from main
                for prefix in _resolve(caller, active | {sub_name}):
                    chains.append(prefix + chain)
            if len(chains) > _MAX_REACH_CHAINS:
                break
        result = tuple(chains)
        if not active:  # only memoize the cycle-free (top-level) resolution
            memo[sub_name] = result
        return result

    return {name: _resolve(name, frozenset()) for name in program.subroutines}


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def compact_location(
    scope: FactScope,
    subroutine: str | None,
    rung_index: int,
    branch_path: tuple[int, ...] = (),
) -> str:
    """Compact traceback location: ``Main:R5`` / ``SFCExample:R2`` / ``Main:R5.0``.

    Main-program rungs are prefixed ``Main`` and subroutine rungs by the subroutine
    name; the rung number is 1-indexed.  A branch path appends dotted indices.
    """
    head = subroutine if scope == "subroutine" and subroutine else "Main"
    loc = f"{head}:R{rung_index + 1}"
    if branch_path:
        loc += "." + ".".join(str(b) for b in branch_path)
    return loc


def _site_source(site: WriteSite) -> str:
    """Codegen provenance for a write site (``file:line`` / ``line 12`` / '')."""
    if site.source_file and site.source_line:
        return f"{site.source_file}:{site.source_line}"
    if site.source_line:
        return f"line {site.source_line}"
    return ""


def compact_site_location(site: WriteSite) -> str:
    """Compact traceback location for a write site."""
    return compact_location(site.scope, site.subroutine, site.rung_index, site.branch_path)


def site_frame(site: WriteSite, *, caret_token: str | None = None, caret_label: str = "") -> Frame:
    """Build a diagnostic :class:`Frame` for a write site.

    The instruction is rendered as DSL source and, when the rung has conditions,
    shown inside its ``with rung(...):`` header — exactly as written::

         --> Main:R5
          |
          |  with rung(Start, ~Stop):
          |      latch(C2000)
          |            ^^^^^ never reset

    The caret underlines *caret_token* (defaulting to the written tag) and carries
    *caret_label*.  Falls back to a bare ``type(target)`` form when the live
    instruction was not captured.
    """
    from pyrung.core.validation.display import Frame
    from pyrung.core.validation.render import render_instruction, with_rung_line

    if site.instruction is not None:
        code, code_caret = render_instruction(
            site.instruction, site.target_name, caret_token=caret_token
        )
    else:
        code, code_caret = f"{site.instruction_type}({site.target_name})", None

    if site.conditions:
        lines = (with_rung_line(site.conditions), f"    {code}")
        caret = (1, 4 + code_caret[0], code_caret[1]) if code_caret else None
    else:
        lines = (code,)
        caret = (0, code_caret[0], code_caret[1]) if code_caret else None

    return Frame(
        location=compact_site_location(site),
        lines=lines,
        caret=caret,
        caret_label=caret_label if caret else "",
        source=_site_source(site),
    )
