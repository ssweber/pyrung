"""Shared PLC operations and action-admission helpers for PILOT.

The module projects search/world keys, compiles guarded input rungs, reports
their authoritative snapshot-relative execution ownership, forks PLC state,
applies pulses, settles delayed effects, and adapts common coasts to
``CoastReceipt`` results. It also contains the shared avoid and hold/route
admission checks used at execution boundaries.

It does not choose candidates, judge trial outcomes, or manage checkpoints and
reverts.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

from pyrsistent import pvector

from pyrung.core.analysis.pilot.coast import CoastReceipt

if TYPE_CHECKING:
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)


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

    rungs: tuple[PilotRungExecution, ...]

    @property
    def effective(self) -> tuple[PilotRung, ...]:
        return tuple(
            entry.rung for entry in self.rungs if entry.state is PilotRungExecutionState.EFFECTIVE
        )

    def owner(self, dest: str) -> PilotRung | None:
        return next((rung for rung in self.effective if rung.dest == dest), None)


def _rung_identity(rung: PilotRung) -> tuple[Any, ...]:
    """Exact executable identity used for overlay ownership and deduplication."""
    return (
        rung.dest,
        _semantic_key(rung.value),
        _semantic_key(rung.guard),
        _semantic_key(rung.operation),
    )


def coast_departure_tags(state: Any, ctx: Any) -> tuple[str, ...]:
    """Channels whose departure terminates a coast holding the current world.

    Pipeline analysis owns recognized request/state channels.  Gauge owns
    monotone progress coordinates.  An exact stateful target with no Gauge
    owner is itself a discrete channel, even when the program has no inferred
    operator-request pipeline.  Keeping that arbitration here gives coast,
    VERIFY replay, and investigation the same channel set.
    """
    channels = list(dict.fromkeys(role.channel_tag for role in ctx.pipeline_roles))
    config = state.key_config
    target = ctx.target.tag
    gauge_tags = {
        component.tag for component in getattr(getattr(state, "gauge", None), "components", ())
    }
    if (
        ctx.target.predicate is None
        and config is not None
        and target in config.stateful_names
        and target not in gauge_tags
        and target not in channels
    ):
        channels.append(target)
    return tuple(channels)


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
    from pyrung.core.crossing import Cmp, Eq

    if not isinstance(constraint, (Eq, Cmp)):
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

    if constraint.bound_is_tag:
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
    from pyrung.core.crossing import Cmp, Eq
    from pyrung.core.tag import Bool

    if isinstance(atom, (Eq, Cmp)):
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


def _rungs_from_proposals(
    plc: PLC,
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


def _expand_pilot_rules(rungs: Iterable[PilotRung]) -> tuple[_ExpandedPilotRule, ...]:
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

    materialized = list(rungs)
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


def _rung_execution_receipt(
    rungs: Iterable[PilotRung], snapshot: Mapping[str, Any]
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

    materialized = tuple(rungs)
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


def _set_rungs(plc: PLC, rungs: Iterable[PilotRung]) -> None:
    """Replace PILOT's overlay from its ordered, guarded rung records."""
    from pyrung.core.synthesis import guarded_copy_rung

    materialized = tuple(rungs)
    expanded = _expand_pilot_rules(materialized)
    rules: list[tuple[Any, Any, Any]] = []
    for rule in expanded:
        dest = plc._known_tags_by_name.get(rule.rung.dest)
        if dest is None:
            raise KeyError(f"pilot rung destination {rule.rung.dest!r} is not a program tag")
        rules.append((dest, rule.rung.value, rule.guard))
    _set_synth_holds(plc, [guarded_copy_rung(rules)] if rules else [])


def _append_rungs(
    plc: PLC,
    proposed: list[PilotRung],
    rungs: Iterable[PilotRung],
) -> Any:
    """Append new evidence and install the resulting ordered overlay.

    The returned persistent vector is the new world value.  Mutating a plain
    list remains supported for the low-level public seam and older callers, but
    PILOT itself always assigns the returned value into ``_World.rungs``.
    """
    updated_list = list(rungs)
    seen = {_rung_identity(rung) for rung in updated_list}
    for rung in proposed:
        identity = _rung_identity(rung)
        if identity not in seen:
            updated_list.append(rung)
            seen.add(identity)
    if isinstance(rungs, list):
        list_rungs = cast(list[PilotRung], rungs)
        list_rungs[:] = updated_list
        updated = list_rungs
    else:
        updated = pvector(updated_list)
    _set_rungs(plc, list(updated))
    return pvector(updated)


def fork_with_rungs(source: PLC, rungs: Iterable[PilotRung]) -> PLC:
    """Fork *source* and rebuild its scoped steering overlay verbatim."""
    fork = source.fork()
    _set_rungs(fork, rungs)
    return fork


# A zoom/coast gets a generous budget of its own — timer dwell is waiting, not
# searching, so it does not consume the pilot's iteration budget.
_ZOOM_BUDGET = 10_000


def _coast_to_value(
    plc: PLC,
    channel_tag: str | None,
    target_value: Any,
    *,
    budget: int = _ZOOM_BUDGET,
    session: Any = None,
) -> CoastReceipt:
    """Coast *plc* forward (folding) until ``channel_tag == target_value``.

    Arms two bumps — the target and a departure (the channel leaving its
    start value for anything but the target) — so the coast lands on the
    exact scan either fires and the receipt says which.  This is the single
    mechanism for "hold heading and let scans pass": the live zoom
    (``steer``) and the investigation replay (``investigate``) both coast
    through timer dwell identically, so a replay reproduces the live zoom.

    *conditional* holds animate during the coast exactly as in
    :func:`_coast_holding_state` — a confirmed oscillation corrective (a
    watchdog pet) that only the terminal let-run animated would silently drop
    out of every coast, re-tripping the watchdog it exists to feed.

    ``receipt.reached`` is the legacy bool ("target reached, no ejection").
    """
    from pyrung.core.analysis.pilot.coast import (
        TARGET,
        CoastSession,
        departure_bump,
        value_bump,
    )

    if channel_tag is None:
        return CoastReceipt(
            kind=session.kind if session is not None else "zoom",
            start_scan=plc.state.scan_id,
            end_scan=plc.state.scan_id,
            stop_reason="skipped",
            fired=(),
            events=(),
            budget=0,
        )

    start = plc.state.tags.get(channel_tag)
    bumps = [
        value_bump(plc, "target", TARGET, channel_tag, target_value),
        departure_bump(
            plc,
            "ejected",
            {channel_tag: start},
            excluding={channel_tag: target_value},
        ),
    ]
    if session is None:
        session = CoastSession(plc, kind="zoom")
    assert session.plc is plc
    return session.seek(bumps, budget=budget)


def _coast_holding_state(
    plc: PLC,
    target_tag: str,
    target_value: Any,
    role_tags: tuple[str, ...],
    *,
    budget: int = _ZOOM_BUDGET,
    reached_fn: Callable[[Any], bool] | None = None,
    reached_condition: Any = None,
    session: Any = None,
) -> CoastReceipt:
    """Generalized terminal let-run: coast toward the *global* target while
    holding the current macro-state.

    *reached_fn* overrides the stop condition — supply it for a **relational**
    target (``Temp >= 5.0``), where the goal is the predicate holding, not the
    register hitting an exact ``target_value``.  Defaults to exact-value match.

    Heading is the global target itself — no intermediate bearing or channel
    register is assumed.  The ejection guard is "the macro-state I am parked in
    changed on its own": any recognized state-machine role register
    (``role_tags``) leaving the value it held at coast start pauses the coast at
    that scan, so an ejection (Execute -> Aborting) hands a tight incident to
    investigation instead of burning the whole budget.

    With no roles (a program without a recognized state machine) the departure
    bump never fires and the coast simply runs to the target or the budget —
    still safe.

    ``receipt.reached`` is the legacy bool ("target reached, no ejection").
    """
    from pyrung.core.analysis.pilot.coast import (
        TARGET,
        CoastSession,
        departure_bump,
        predicate_bump,
        value_bump,
    )

    # Conditional holds become guarded / oscillating rungs in the coast fork's
    # holds overlay (the rung form of the old reactive breakpoints); steady holds
    # are already rungs from ``fork_with_rungs``.  Both run every scan under the
    # session's fold dispatch — the single mechanism for "hold heading and let
    # scans pass", identical for the live zoom and the investigation replay coast.
    if reached_fn is not None:
        # Relational target: the callable remains authoritative while the
        # equivalent Condition supplies exact fold reads and crossings.
        target = predicate_bump(
            "target",
            TARGET,
            reached_fn,
            condition=reached_condition,
            watched=(target_tag,),
        )
    else:
        target = value_bump(plc, "target", TARGET, target_tag, target_value)

    bumps = [target]
    if role_tags:
        start = {t: plc.state.tags.get(t) for t in role_tags}
        bumps.append(departure_bump(plc, "ejected", start))
    if session is None:
        session = CoastSession(plc, kind="letrun")
    assert session.plc is plc
    return session.seek(bumps, budget=budget)


_THRESHOLD_DOWN_KINDS = frozenset({"count_down", "int_down", "real_down"})
_THRESHOLD_FORM_GT = "gt"


@dataclass(frozen=True)
class _StateKeyConfig:
    """Projection dimensions for the pilot state key.

    When built from the prover's ``_ExploreContext``, ``stateful_names``
    contains every cross-scan tag, ``done_specs`` carries the Done-bit
    three-valued abstraction, ``threshold_vector_specs`` carries
    accumulator crossing vectors, and ``acc_indices`` marks raw
    accumulator positions to mask.

    When the prover pipeline is unavailable, the fallback uses
    ``pivot_tags`` from the trace tree with empty absorption specs.
    """

    stateful_names: tuple[str, ...]
    done_specs: tuple[Any, ...]
    threshold_vector_specs: tuple[Any, ...]
    acc_indices: frozenset[int]


def _threshold_crossed_snap(
    snap: dict[str, Any],
    kind: str,
    acc_name: str,
    threshold: int | float | str,
    form: str,
) -> bool:
    """Threshold-vector bit from a PLC snapshot (mirrors kernel._threshold_crossed)."""
    acc_value = snap.get(acc_name)
    threshold_value = snap.get(threshold) if isinstance(threshold, str) else threshold
    if (
        type(acc_value) is bool
        or type(threshold_value) is bool
        or not isinstance(acc_value, (int, float))
        or not isinstance(threshold_value, (int, float))
    ):
        return False
    if kind in _THRESHOLD_DOWN_KINDS:
        acc_value = -acc_value
        threshold_value = -threshold_value
    if form == _THRESHOLD_FORM_GT:
        return acc_value > threshold_value
    return acc_value >= threshold_value


def _pilot_state_key(snap: dict[str, Any], cfg: _StateKeyConfig) -> tuple[Any, ...]:
    """Project a PLC snapshot onto the state key dimensions."""
    parts: list[Any] = list(map(snap.get, cfg.stateful_names))
    if cfg.done_specs:
        from pyrung.core.analysis.prove.absorb import _done_acc_state

        for spec in cfg.done_specs:
            parts[spec.index] = _done_acc_state(
                spec.kind, parts[spec.index], snap.get(spec.acc_name)
            )
    for idx in cfg.acc_indices:
        parts[idx] = None
    for spec in cfg.threshold_vector_specs:
        parts.append(
            tuple(
                _threshold_crossed_snap(snap, spec.kind, spec.acc_name, atom.threshold, atom.form)
                for atom in spec.atoms
            )
        )
    return tuple(parts)


def wait_edge_nogood(channel_tag: str, from_value: Any, to_value: Any) -> tuple[str, Any]:
    """The world-keyed nogood for a completion (WAIT) edge that proved sterile.

    A completion edge carries no action, so the ordinary ``(tag, value)``
    action nogood can never name it.  This synthetic pair — keyed by the
    channel and the exact ``from -> to`` claim — lets a rejected wait be
    remembered at its world key and filtered out of the next iteration's route
    query, exactly like a failed press.
    """
    return (f"wait::{channel_tag}", (from_value, to_value))


def _semantic_key(value: Any) -> Any:
    """A stable, hashable identity for a rung operand or guard.

    Conditions deliberately compare by identity, which is right while executing
    a ladder but wrong for search identity: rebuilding ``State != Execute`` must
    describe the same world.  Keep only semantic public fields and normalize tag
    references by name; source locations and derived caches are intentionally
    absent.
    """
    from enum import Enum

    from pyrung.core.tag import ImmediateRef, Tag

    if value is None or isinstance(value, bool | int | float | str | bytes):
        return value
    if isinstance(value, Tag):
        return ("tag", value.name)
    if isinstance(value, ImmediateRef):
        return ("immediate", _semantic_key(value.value))
    if isinstance(value, Enum):
        return (type(value).__module__, type(value).__qualname__, value.name)
    if isinstance(value, tuple | list):
        return tuple(_semantic_key(item) for item in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((_semantic_key(item) for item in value), key=repr))
    if isinstance(value, dict):
        return tuple(
            sorted(
                ((_semantic_key(k), _semantic_key(v)) for k, v in value.items()),
                key=repr,
            )
        )
    attrs = getattr(value, "__dict__", None)
    if attrs is not None:
        semantic = tuple(
            (name, _semantic_key(member))
            for name, member in sorted(attrs.items())
            if not name.startswith("_") and name not in {"source_file", "source_line"}
        )
        return (type(value).__module__, type(value).__qualname__, semantic)
    return (type(value).__module__, type(value).__qualname__, str(value))


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


def _pilot_world_key(
    snap: dict[str, Any],
    cfg: _StateKeyConfig,
    rungs: Any,
) -> tuple[Any, ...]:
    """Identity of an executable PILOT world: PLC projection plus PilotRungs."""
    rung_key = tuple(_rung_identity(rung) for rung in rungs)
    return (_pilot_state_key(snap, cfg), rung_key)


def _set_synth_holds(plc: PLC, rungs: list[Any]) -> None:
    """Replace the plc's synthesis holds overlay and invalidate the derived caches."""
    from pyrung.core.synthesis import Synthesis

    if plc._synthesis is None:
        plc._synthesis = Synthesis()
    plc._synthesis.holds = rungs
    plc._fold_context_cache = None
    plc._compiled_replay_kernel = None
    plc._soft_exec_program_cache = None
    # Historical causal replay includes the synthesis brackets. A new hold
    # world must not reuse a chain or root classification observed under the
    # previous brackets.
    plc.__dict__.pop("_pilot_cause_memo", None)
    plc.__dict__.pop("_pilot_chase_memo", None)


def _apply_pulse(
    plc: PLC,
    actions: list[tuple[str, Any]],
    resting: dict[str, Any],
    edge_tags: set[str],
    session: Any = None,
) -> int:
    """Apply *actions* with rising-edge semantics where needed.

    Returns the number of scans consumed.  *session*, when given, records the
    pulse onto that session's timeline (pens ticked after every raw scan, the
    settle dwell run on the session) so a Done that fires inside the pulse
    window is a recorded pen mark, not history-only.
    """
    from pyrung.core.analysis.pilot.coast import LIMITS, CoastSession

    if session is None:
        session = CoastSession(plc, kind="pulse")
    assert session.plc is plc

    patch = {t: v for t, v in actions}
    needs_edge = any(t in edge_tags for t in patch)

    if needs_edge:
        release = {t: resting.get(t, False) for t in patch if t in edge_tags}
        if release:
            plc.patch(release)
            plc.step()
            session.note_pens()

    plc.patch(patch)
    plc.step()
    session.note_pens()

    # Fixed settle window — the one waiting shape with no predicate (an
    # explicit dwell, never disguised as a bump).
    session.dwell(LIMITS.pulse_settle_scans)

    return 6 if needs_edge else 5


def _settle_delayed_effects(
    fork: PLC,
    before_snap: dict[str, Any],
    cfg: _StateKeyConfig | None,
    *,
    scan_budget: int = 2000,
    session: Any = None,
) -> list[CoastReceipt]:
    """Settle environment-owned latency after an intervention.

    If the harness has scheduled patches
    (Physical on_delay/off_delay), seek harness quiescence
    (``pending_count == 0``), then dwell one scan — the plant commits
    feedback the scan it settles; the program that reads it reacts the
    *next* scan (the scan boundary is the plant latency).

    Program instruction progress is deliberately not settled here. A newly
    armed timer/counter/drum is a distinct operation owned by its
    :class:`AdvanceProfile`; trace/program-step must re-read that owner and
    prescribe the observable boundary as an ordinary coast. Fast-forwarding
    timing bits here used to execute that operation a second time, invisibly,
    before option ordering or correction lifecycle could observe it.
    """
    from pyrung.core.analysis.pilot.coast import QUIESCENT, CoastSession, predicate_bump

    del before_snap, cfg
    budget = scan_budget
    receipts: list[CoastReceipt] = []
    if session is None:
        session = CoastSession(fork, kind="delayed-effects")
    assert session.plc is fork

    harness = getattr(fork, "_harness", None)
    if harness is not None and harness.pending_count > 0:
        scan_before = fork.state.scan_id
        receipt = session.seek(
            [
                predicate_bump(
                    "harness_quiescent",
                    QUIESCENT,
                    lambda s: harness.pending_count == 0,
                )
            ],
            budget=budget,
        )
        receipts.append(receipt)
        if harness.pending_count == 0 and fork.state.scan_id - scan_before < budget:
            session.dwell(1)
    return receipts


def _has_pending_effects(fork: PLC) -> bool:
    """True if the fork has unsettled harness feedback.

    Bool dwell reports via ``pending_count``; an analog coupling is "pending"
    while its enable is active — its plant rung is still driving the feedback
    register this scan.
    """
    harness = getattr(fork, "_harness", None)
    if harness is None:
        return False
    if harness.pending_count > 0:
        return True
    snap = fork.current_state.tags
    for c in getattr(harness, "_profile_couplings", ()):
        en_raw = snap.get(c.en_name, False)
        enabled = en_raw == c.trigger_value if c.trigger_value is not None else bool(en_raw)
        if enabled:
            return True
    return False


# ---------------------------------------------------------------------------
# Hold policy — whether a proposed (tag, value) hold is allowed for this ctx.
# Pure duck-typed reads off the pilot context (no imports); shared by
# investigation's precise-cause walk and the enabler-correction arms so neither
# has to depend on the other.
# ---------------------------------------------------------------------------


def _avoid_snap_names(avoid: Any, snap: dict[str, Any]) -> tuple[str, ...]:
    """Names of the avoid conditions *snap* trips (``()`` when avoid is None).

    A ``_AvoidPredicate`` reports its violated member names; a bare callable
    (someone passed ``avoid_pred=`` a raw predicate) reports a generic name.
    """
    if avoid is None:
        return ()
    violated = getattr(avoid, "violated", None)
    if violated is not None:
        try:
            return tuple(violated(snap))
        except Exception:
            return ()
    try:
        return ("avoided condition",) if bool(avoid(snap)) else ()
    except Exception:
        return ()


def _avoid_violations(
    ctx: Any,
    pairs: list[tuple[str, Any]] | tuple[tuple[str, Any], ...],
    snapshot: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    """Names of the avoid conditions that *pairs* would force.

    Static: overlays each ``(tag, value)`` onto *snapshot* (or the resting
    baseline when no snapshot is given — the neutral world a hold asserts its
    tag against) and evaluates the avoid predicate.  This is the action gate's
    primitive (a candidate/hold whose overlay trips avoid depends on it).
    """
    avoid = getattr(ctx, "avoid_pred", None)
    if avoid is None:
        return ()
    base = dict(snapshot) if snapshot is not None else dict(getattr(ctx, "resting", {}) or {})
    for tag, value in pairs:
        base[tag] = value
    return _avoid_snap_names(avoid, base)


def _avoid_forces(
    ctx: Any,
    pairs: list[tuple[str, Any]] | tuple[tuple[str, Any], ...],
    snapshot: dict[str, Any] | None = None,
) -> bool:
    return bool(_avoid_violations(ctx, pairs, snapshot))


def _hold_allowed(ctx: Any, pair: tuple[str, Any]) -> bool:
    tag, _value = pair
    compass = getattr(ctx, "compass", None)
    action_tags = getattr(compass, "action_tags", frozenset())
    blocked_actions = getattr(ctx, "blocked_actions", frozenset())
    if tag in action_tags or pair in blocked_actions:
        return False
    # A hold that drives an avoided tag is a path that depends on it — inadmissible.
    return not _avoid_forces(ctx, [pair])
