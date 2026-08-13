"""Compile and install PILOT's ordered, guarded pilot-rung overlay.

This module owns the executable overlay records, condition lowering, expansion
and execution receipts, installation, append semantics, and overlay-aware PLC
forks. It does not choose candidates or define world-key identity.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

from pyrsistent import PVector, pvector

from pyrung.core.analysis.pilot.world_key import _rung_identity, _semantic_key

if TYPE_CHECKING:
    from pyrung.core.runner import PLC


@dataclass(frozen=True)
class OperationReceipt:
    """Owner-declared lifetime for one rung-driven operation.

    ``until`` is the observable handoff boundary. ``progress`` is the owner's
    affirmative receipt that this exact operation is already in flight.  The
    overlay compiler uses it to preserve current ownership when another rule
    for the same destination is also waiting to start.
    """

    until: Any
    progress: Any = None


@dataclass(frozen=True)
class PilotRung:
    """One scoped piece of PILOT steering.

    ``guard`` is deliberately required: steering without a reason to release is
    a permanent force wearing ladder syntax.  The proposer owns this condition;
    installation only preserves its meaning and order.

    A ``PilotRung`` is executable form, not correction provenance.  Only pilot
    rungs named by active correction receipts may renegotiate their concrete value
    from a later incident boundary; prerequisites and route holds cannot enter
    the correction lifecycle merely because they compile to this rung type.
    """

    dest: str
    value: Any
    guard: Any
    operation: OperationReceipt | None = None

    def __post_init__(self) -> None:
        if self.guard is None:
            raise ValueError("PilotRung.guard is required")


class PilotRungExecutionState(Enum):
    """One installed rule's status in a frozen rung-entry snapshot."""

    DORMANT = "dormant"
    ELIGIBLE = "eligible"
    SHADOWED = "shadowed"
    CONTINUING = "continuing"
    EFFECTIVE = "effective"


@dataclass(frozen=True)
class PilotRungExecution:
    """Authoritative execution status for one installed :class:`PilotRung`.

    ``continuation`` records whether the rule's owner-declared progress witness
    selected its continuation branch.  An effective continuation still has
    state :attr:`PilotRungExecutionState.EFFECTIVE`; the flag preserves why it
    won without making consumers reconstruct the compiler's expansion.
    """

    rung: PilotRung
    state: PilotRungExecutionState
    continuation: bool = False


@dataclass(frozen=True)
class PilotOverlayExecution:
    """Effective-ownership receipt for one ordered overlay and snapshot."""

    pilot_rungs: tuple[PilotRungExecution, ...]

    @property
    def effective(self) -> tuple[PilotRung, ...]:
        return tuple(
            entry.rung
            for entry in self.pilot_rungs
            if entry.state is PilotRungExecutionState.EFFECTIVE
        )

    def owner(self, dest: str) -> PilotRung | None:
        return next((rung for rung in self.effective if rung.dest == dest), None)


def _until_unresolved_condition(plc: PLC, atom: Any) -> Any:
    """Lower a trace completion ``Atom`` to its still-unresolved condition."""
    return _atom_condition(plc, atom, unresolved=True)


def _constraint_condition(
    plc: PLC,
    constraint: Any,
    *,
    unresolved: bool = False,
) -> Any | None:
    """Lower a crossing ``Constraint`` to an equivalent runtime condition.

    The constraint algebra is the planner's data-only language; coasts and
    folding need the executable Condition language so they can expose exact
    reads and crossing thresholds.  Unsupported constraint shapes return
    ``None`` and leave the caller's predicate authoritative.
    """
    from pyrung.core.condition import (
        AllCondition,
        AnyCondition,
        CompareEq,
        CompareGe,
        CompareGt,
        CompareLe,
        CompareLt,
        CompareNe,
    )
    from pyrung.core.crossing import AffineCmp, Cmp, Eq

    if not isinstance(constraint, (Eq, Cmp, AffineCmp)):
        return None

    tag = plc._known_tags_by_name.get(constraint.tag)
    if tag is None:
        # Static block ranges are intentionally lazy in the runner's tag
        # inventory.  An advance profile still owns concrete Tag objects for
        # its channels, so use that authoritative channel metadata.
        from pyrung.core.analysis.pilot.advance import build_advance_index

        owner = (
            build_advance_index(plc.program, getattr(plc, "_harness", None)).resolve(constraint.tag)
            if plc.program is not None
            else None
        )
        if owner is not None:
            tag = next(
                (channel for channel in owner.profile.channels if channel.name == constraint.tag),
                None,
            )
    if tag is None:
        return None

    if isinstance(constraint, Eq):
        if not constraint.values:
            return None
        compare = CompareNe if unresolved else CompareEq
        terms = [compare(tag, value) for value in constraint.values]
        if len(terms) == 1:
            return terms[0]
        # not(x in {a, b}) == x != a AND x != b
        return AllCondition(*terms) if unresolved else AnyCondition(*terms)

    if isinstance(constraint, AffineCmp):
        operand = plc._known_tags_by_name.get(constraint.bound_tag)
        if operand is None:
            return None
        if constraint.scale != 1:
            operand = operand * constraint.scale
        if constraint.offset != 0:
            operand = operand + constraint.offset
    elif constraint.bound_is_tag:
        operand = plc._known_tags_by_name.get(str(constraint.bound))
        if operand is None:
            return None
    else:
        operand = constraint.bound
    direct = {
        "==": CompareEq,
        "!=": CompareNe,
        "<": CompareLt,
        "<=": CompareLe,
        ">": CompareGt,
        ">=": CompareGe,
        "eq": CompareEq,
        "ne": CompareNe,
        "lt": CompareLt,
        "le": CompareLe,
        "gt": CompareGt,
        "ge": CompareGe,
    }
    inverse = {
        "==": CompareNe,
        "!=": CompareEq,
        "<": CompareGe,
        "<=": CompareGt,
        ">": CompareLe,
        ">=": CompareLt,
        "eq": CompareNe,
        "ne": CompareEq,
        "lt": CompareGe,
        "le": CompareGt,
        "gt": CompareLe,
        "ge": CompareLt,
    }
    comparison = (inverse if unresolved else direct).get(constraint.op)
    return comparison(tag, operand) if comparison is not None else None


def _atom_condition(plc: PLC, atom: Any, *, unresolved: bool = False) -> Any:
    """Lower an atom to its stated or still-unresolved condition."""
    from pyrung.core.condition import (
        CompareEq,
        CompareGe,
        CompareGt,
        CompareLe,
        CompareLt,
        CompareNe,
    )
    from pyrung.core.crossing import AffineCmp, Cmp, Eq
    from pyrung.core.tag import Bool

    if isinstance(atom, (Eq, Cmp, AffineCmp)):
        condition = _constraint_condition(plc, atom, unresolved=unresolved)
        if condition is None:
            raise ValueError(f"constraint {atom!r} cannot lower to a runtime condition")
        return condition

    tag = plc._known_tags_by_name.get(atom.tag)
    if tag is None:
        raise KeyError(f"pilot rung guard tag {atom.tag!r} is not a program tag")

    form = atom.form
    operand = (
        plc._known_tags_by_name.get(atom.operand, atom.operand)
        if atom.operand_is_tag
        else atom.operand
    )
    if atom.operand_is_tag:
        if atom.operand_scale != 1:
            operand = operand * atom.operand_scale
        if atom.operand_offset != 0:
            operand = operand + atom.operand_offset
    if unresolved:
        if form in ("xic", "truthy"):
            return CompareEq(tag, False)
        if form == "xio":
            return CompareEq(tag, True)
        inverse = {
            "eq": CompareNe,
            "ne": CompareEq,
            "lt": CompareGe,
            "le": CompareGt,
            "gt": CompareLe,
            "ge": CompareLt,
        }.get(form)
        if inverse is None:
            raise ValueError(f"trace predicate {form!r} cannot scope a PilotRung")
        return inverse(tag, operand)

    if form in ("xic", "truthy"):
        return tag
    if form == "xio":
        return ~tag
    if form == "eq" and isinstance(tag, Bool) and isinstance(operand, bool):
        return tag if operand else ~tag
    direct = {
        "eq": CompareEq,
        "ne": CompareNe,
        "lt": CompareLt,
        "le": CompareLe,
        "gt": CompareGt,
        "ge": CompareGe,
    }.get(form)
    if direct is None:
        raise ValueError(f"trace predicate {form!r} cannot guard a PilotRung")
    return direct(tag, operand)


def _target_unresolved_condition(
    plc: PLC,
    target_tag: str,
    target_value: Any,
    target_predicate: Any = None,
) -> Any:
    """The honest outer lifetime for a target-directed corrective rung."""
    if target_predicate is not None:
        return _until_unresolved_condition(plc, target_predicate)
    from pyrung.core.condition import CompareNe

    tag = plc._known_tags_by_name.get(target_tag)
    if tag is None:
        raise KeyError(f"pilot target guard tag {target_tag!r} is not a program tag")
    return CompareNe(tag, target_value)


def _pilot_rungs_from_proposals(
    proposals: list[Any],
    scope: Any,
) -> list[PilotRung]:
    """Normalize pair-shaped or ``PilotRung`` proposals to scoped pilot rungs."""
    result: list[PilotRung] = []
    for proposal in proposals:
        if isinstance(proposal, PilotRung):
            result.append(proposal)
            continue
        dest, proposed = proposal
        result.append(PilotRung(dest, proposed, scope))
    return result


@dataclass(frozen=True)
class _ExpandedPilotRule:
    """One compiler branch, linked back to its installed rule owner."""

    rung_index: int
    rung: PilotRung
    guard: Any
    continuation: bool


def _expand_pilot_rules(pilot_rungs: Iterable[PilotRung]) -> tuple[_ExpandedPilotRule, ...]:
    """Lower installed rules to the exact ordered branches the runner scans."""
    from pyrung.core.condition import AllCondition, Condition, _as_condition

    class _DemandActive(Condition):
        def __init__(self, demand: Any):
            self.demand = demand

        def evaluate(self, ctx: Any) -> bool:
            return bool(self.demand.condition.evaluate(ctx)) is bool(self.demand.value)

    class _NoDemandActive(Condition):
        def __init__(self, demands: tuple[Any, ...]):
            self.demands = demands

        def evaluate(self, ctx: Any) -> bool:
            return not any(
                bool(demand.condition.evaluate(ctx)) is bool(demand.value)
                for demand in self.demands
            )

    materialized = list(pilot_rungs)
    progress_by_dest: dict[str, tuple[Any, ...]] = {}
    for rung in materialized:
        progress = rung.operation.progress if rung.operation is not None else None
        if progress is None:
            continue
        current = progress_by_dest.get(rung.dest, ())
        if all(_semantic_key(progress) != _semantic_key(existing) for existing in current):
            progress_by_dest[rung.dest] = (*current, progress)

    rules: list[_ExpandedPilotRule] = []
    continuation_rules: list[_ExpandedPilotRule] = []
    for rung_index, rung in enumerate(materialized):
        rung_guard = _as_condition(rung.guard)
        progress = rung.operation.progress if rung.operation is not None else None
        peers = progress_by_dest.get(rung.dest, ())
        start_guard = (
            AllCondition(rung_guard, _NoDemandActive(peers))
            if progress is not None and peers
            else rung_guard
        )
        rules.append(_ExpandedPilotRule(rung_index, rung, start_guard, False))
        if progress is not None:
            # Continuations come after every start rule. The last active write
            # therefore belongs to the operation whose owner says it is already
            # in flight, rather than to a competing value that merely remains
            # eligible to start. The affirmative progress receipt replaces the
            # start guard: requiring both would release the operation as soon as
            # it left the context that started it.
            continuation_rules.append(
                _ExpandedPilotRule(
                    rung_index,
                    rung,
                    _DemandActive(progress),
                    True,
                )
            )
    rules.extend(continuation_rules)
    return tuple(rules)


def _pilot_rung_execution_receipt(
    pilot_rungs: Iterable[PilotRung], snapshot: Mapping[str, Any]
) -> PilotOverlayExecution:
    """Classify every installed rule using the compiler's exact expansion.

    All conditions read one frozen rung-entry snapshot, just as
    :func:`guarded_copy_rung` executes them.  The last active expanded branch
    for each destination is the effective owner.  Earlier active starts remain
    eligible, earlier active continuation branches remain continuing, and an
    operation prevented from starting by a peer's progress is shadowed.
    """
    from pyrung.core.analysis.sp_values import _SnapshotView
    from pyrung.core.condition import _as_condition

    materialized = tuple(pilot_rungs)
    expanded = _expand_pilot_rules(materialized)
    view = _SnapshotView(dict(snapshot), {})
    active = tuple(bool(rule.guard.evaluate(view)) for rule in expanded)
    effective_by_dest: dict[str, int] = {}
    for rule_index, (rule, is_active) in enumerate(zip(expanded, active, strict=True)):
        if is_active:
            effective_by_dest[rule.rung.dest] = rule_index

    by_rung: list[list[int]] = [[] for _rung in materialized]
    for rule_index, rule in enumerate(expanded):
        if active[rule_index]:
            by_rung[rule.rung_index].append(rule_index)

    entries: list[PilotRungExecution] = []
    for rung_index, rung in enumerate(materialized):
        active_rules = by_rung[rung_index]
        effective_index = effective_by_dest.get(rung.dest)
        is_effective = effective_index in active_rules
        continuing = any(expanded[index].continuation for index in active_rules)
        if is_effective:
            state = PilotRungExecutionState.EFFECTIVE
        elif continuing:
            state = PilotRungExecutionState.CONTINUING
        elif active_rules:
            state = PilotRungExecutionState.ELIGIBLE
        elif not bool(cast(Any, _as_condition(rung.guard)).evaluate(view)):
            state = PilotRungExecutionState.DORMANT
        else:
            state = PilotRungExecutionState.SHADOWED
        entries.append(PilotRungExecution(rung, state, continuing))
    return PilotOverlayExecution(tuple(entries))


def project_pilot_overlay(
    snapshot: Mapping[str, Any],
    pilot_rungs: Iterable[PilotRung],
    resting: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the compiled overlay at one hypothetical rung-entry snapshot.

    This is a read of the overlay compiler's existing ownership receipt.  It
    lets route verification ask which temporary holds would still exist at a
    prospective boundary without executing a speculative PLC scan.
    """

    materialized = tuple(pilot_rungs)
    projected = dict(snapshot)
    receipt = _pilot_rung_execution_receipt(materialized, projected)
    for dest in dict.fromkeys(rung.dest for rung in materialized):
        owner = receipt.owner(dest)
        if owner is not None:
            projected[dest] = owner.value
        elif dest in resting:
            projected[dest] = resting[dest]
    return projected


def _set_pilot_rungs(plc: PLC, pilot_rungs: Iterable[PilotRung]) -> None:
    """Replace the overlay only before this runner has executed a local scan.

    A runner's retained suffix and synthesis overlay are one causal epoch.
    Callers changing an executable world's rungs must first cross
    :func:`fork_with_pilot_rungs`; direct installation is reserved for a fresh
    runner or disposable fork still parked at its initial boundary.
    """
    from pyrung.core.synthesis import guarded_copy_rung

    if plc.state.scan_id != plc._initial_scan_id:
        raise RuntimeError(
            "cannot change PILOT rungs after this runner has executed; "
            "fork_with_pilot_rungs() at the current boundary instead"
        )
    materialized = tuple(pilot_rungs)
    expanded = _expand_pilot_rules(materialized)
    rules: list[tuple[Any, Any, Any]] = []
    for rule in expanded:
        dest = plc._known_tags_by_name.get(rule.rung.dest)
        if dest is None:
            raise KeyError(f"pilot rung destination {rule.rung.dest!r} is not a program tag")
        rules.append((dest, rule.rung.value, rule.guard))
    _set_synth_holds(plc, [guarded_copy_rung(rules)] if rules else [])


def _merged_pilot_rungs(
    proposed: Iterable[PilotRung],
    pilot_rungs: Iterable[PilotRung],
) -> PVector[PilotRung]:
    """Return the ordered semantic union without changing an executable world.

    PILOT merges its world value first, then crosses an overlay fork boundary.
    Keeping the merge pure prevents an old runner's retained scans from being
    reinterpreted under newly installed hold rungs.
    """
    updated_list = list(pilot_rungs)
    seen = {_rung_identity(rung) for rung in updated_list}
    for rung in proposed:
        identity = _rung_identity(rung)
        if identity not in seen:
            updated_list.append(rung)
            seen.add(identity)
    return pvector(updated_list)


def fork_with_pilot_rungs(
    source: PLC,
    pilot_rungs: Iterable[PilotRung],
    *,
    scan_id: int | None = None,
    history_budget: int | float | None = None,
    inherit_log: bool = True,
) -> PLC:
    """Fork *source* and rebuild its scoped steering overlay verbatim.

    Every production PILOT fork that may execute is created here, with the
    owning ``_World.pilot_rungs`` supplied explicitly (the drive bootstrap supplies
    an explicit empty set).  Public ``PLC.fork()`` does not implicitly inherit
    PILOT holds. Historical causal queries delegate scans at and before this
    boundary to ``source``, including synthetic rung resolution. Internal
    replay of the child's later scans may therefore use its current synthesis
    without redefining inherited historical scans.
    """
    fork = source.fork(
        scan_id=scan_id,
        history_budget=history_budget,
        inherit_log=inherit_log,
    )
    _set_pilot_rungs(fork, pilot_rungs)
    return fork


def _union_conditions(terms: Iterable[Any]) -> Any:
    """One condition holding when any distinct term holds.

    A scope such as an incident's source/exposure/landing corridor is assembled
    by *role*, and two roles routinely name the same channel state -- the safe
    landing is often the state an exposure guard already covers.  Disjunction
    over those roles is a set union, so a repeated term is pure redundancy that
    shows up in every rendered guard.  Conditions compare by object identity, so
    ``_semantic_key`` is what makes "same term" decidable here.

    First-occurrence order is preserved, a lone survivor is returned bare rather
    than wrapped in a one-armed ``Or``, and no terms gives ``None``.
    """
    from pyrung.core.condition import AnyCondition

    unique: list[Any] = []
    seen: set[Any] = set()
    for term in terms:
        if term is None:
            continue
        key = _semantic_key(term)
        if key in seen:
            continue
        seen.add(key)
        unique.append(term)
    if not unique:
        return None
    return unique[0] if len(unique) == 1 else AnyCondition(*unique)


def _set_synth_holds(plc: PLC, rungs: list[Any]) -> None:
    """Replace the plc's synthesis holds overlay and invalidate the derived caches."""
    from pyrung.core.synthesis import Synthesis

    if plc._synthesis is None:
        plc._synthesis = Synthesis()
    plc._synthesis.holds = rungs
    plc._fold_context_cache = None
    plc._compiled_replay_kernel = None
    plc._soft_exec_program_cache = None
    plc._causal_lineage.invalidate_current_epoch()
    # Historical causal replay includes the synthesis brackets. A new hold
    # world must not reuse a chain or root classification observed under the
    # previous brackets.
    plc.__dict__.pop("_pilot_cause_memo", None)
    plc.__dict__.pop("_pilot_chase_memo", None)
