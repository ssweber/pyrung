"""Enumerate and rank alternative routes through a backward Trace read.

This module owns route-option construction and selection policy. It reads the
backward-recursion engine in trace.py but the engine does not depend on route
enumeration.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import pyrung.core.analysis.pilot.route_judgment as _route_judgment
import pyrung.core.analysis.pilot.trace as _trace
import pyrung.core.analysis.pilot.trace_read as _trace_read
from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.pilot.trace_tree import TraceNode
from pyrung.core.analysis.pilot.writer_selection import _can_produce, _rank_writers
from pyrung.core.analysis.simplified import And, Atom, Or, _negate, _sp_to_expr
from pyrung.core.analysis.sp_values import _values_match, _written_value_for_tag

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph


@dataclass(frozen=True)
class _RouteDraft:
    """Accumulated OR-arm selections for one enumerated route.

    Root-only: a route records which arm of each OR in the output writer's
    condition it took.  The writer choice itself is tracked separately and
    applied at ``TraceChoice`` construction.
    """

    route: tuple[str, ...] = ()
    or_locks: tuple[tuple[str, str, int], ...] = ()
    # Concrete ``(tag, value)`` that distinguishes this route; the outermost OR
    # arm's representative leaf (first set wins).
    route_condition: tuple[str, Any] | None = None

    def extend(
        self,
        *,
        route: str | None = None,
        or_lock: tuple[str, str, int] | None = None,
        route_condition: tuple[str, Any] | None = None,
    ) -> _RouteDraft:
        return _RouteDraft(
            route=self.route + ((route,) if route else ()),
            or_locks=self.or_locks + ((or_lock,) if or_lock else ()),
            route_condition=(
                self.route_condition if self.route_condition is not None else route_condition
            ),
        )


def _arm_fully_steerable(e: Any, self_tag: str, steerable: frozenset[str]) -> bool:
    """True when *e* is reachable by directly-steerable inputs alone.

    An OR arm qualifies when PILOT can assert it with inputs only, recursively:

    * ``And`` — *every* term must be steerable (``And(Manual, DiverterBtn)``).
    * ``Or`` — *any* term suffices, since asserting one satisfies it
      (``And(Manual, Or(BtnA, BtnB))``).
    * ``Atom`` — a steerable input the trace can drive: a bit/equality whose
      ``_trace._atom_target`` tag is steerable, **or** an inequality (``Size > 100``)
      whose LHS tag is steerable (the trace's ``_inequality_levers`` drives it).

    Disqualified — and so kept as a surfaced choice — are a non-input /
    coil-backed tag (``ProdMode``), an inequality on a non-steerable computed
    tag, and the self-referencing seal-in atom (taking it commits the machine to
    an internal configuration, a real engineer decision).
    """
    if isinstance(e, And):
        return bool(e.terms) and all(
            _arm_fully_steerable(term, self_tag, steerable) for term in e.terms
        )
    if isinstance(e, Or):
        return any(_arm_fully_steerable(term, self_tag, steerable) for term in e.terms)
    if isinstance(e, Atom):
        if e.tag == self_tag:
            return False  # self-referencing seal-in arm — not a real route
        target = _trace._atom_target(e)
        if target is not None:
            return target[0] in steerable
        # No single target value (an inequality): a steerable LHS is still a
        # lever the trace can drive to satisfy the threshold.
        if e.form in {"lt", "le", "gt", "ge", "ne"}:
            return e.tag in steerable
        return False
    return False  # unknown node — not directly steerable


def _or_ambiguity_over_inputs(
    ri: int,
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
) -> bool:
    """True when one writer's unsatisfied OR(s) each offer a directly-steerable arm.

    The route-choice surface exists so the engineer commits the machine to a
    materially different configuration.  When an OR has *any* arm reachable by
    directly-steerable inputs alone — ``Or(Auto, Manual)``, or the manual-jog
    branch ``And(Manual, DiverterBtn)`` beside an internal auto-sort branch —
    PILOT can take that arm without an internal commitment, so it collapses
    rather than reporting ambiguous (the selected arm can still be excluded
    with ``avoid=``). Returns False when there is no choice-bearing OR
    (nothing to collapse) or any choosing OR offers *no* steerable arm
    (``Or(ProdMode, MaintMode)`` — both coil-backed), which must stay surfaced.
    """
    ro = resolve_rung(program, pdg.rung_nodes[ri])
    if ro is None:
        return False
    sp = ro.sp_tree()
    if sp is None:
        return False
    expr = _sp_to_expr(sp)
    if _values_match(value, False) and tag in pdg.rung_nodes[ri].ote_writes:
        expr = _negate(expr)

    found_choice = False

    def walk(e: Any) -> bool:
        nonlocal found_choice
        if isinstance(e, And):
            return all(walk(term) for term in e.terms)
        if isinstance(e, Or):
            if _trace._expr_satisfied(e, snapshot):
                return True  # already satisfied — contributes no choice
            found_choice = True
            # Collapse when at least one arm is fully steerable: PILOT takes it,
            # no engineer choice needed.  The trace's own Or-scorer then lands on
            # the cheapest (fewest non-steerable) arm, which is that steerable one.
            return any(_arm_fully_steerable(term, tag, steerable) for term in e.terms)
        return True  # atom / leaf — no choice here

    return walk(expr) and found_choice


def enumerate_trace_choices(
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    *,
    steerable: frozenset[str] = frozenset(),
    clear_only: frozenset[str] = frozenset(),
    max_choices: int = 16,
) -> tuple[_trace_read.TraceChoice, ...]:
    """Enumerate route choices for an ambiguous ``tag == value`` trace.

    General over the target value — ``Bool == True``, ``Bool == False`` (the
    writer guard is negated for an ``out`` coil, or only reset writers are
    viable for a retentive coil), or a word ``tag == value`` (only writers whose
    ``_can_produce`` admits *value* are viable).  The route/OR-arm derivation,
    Or-scorer collapse, and rank/tie-break rules are all target-agnostic.

    A "route" is a top-level decision in how *tag* reaches *value*: which
    writer rung drives it, and which arm of each OR in that writer's
    condition is taken.  Choices are **root-only** — each locks just this
    decision; ``_trace.trace_back`` re-traces everything below it from current
    state.  Deeper ambiguity (an OR in a downstream tag's writer) is not
    enumerated, by design: the engineer picks the output route, PILOT plans
    the rest.  This reuses ``_trace.trace_back``'s lock mechanism rather than
    re-walking the trace.

    A single writer whose *only* ambiguity is an OR among directly-steerable
    inputs (``Or(Auto, Manual)``) is **not** surfaced: those arms are inputs
    PILOT can assert directly, so it satisfies the cheapest and plans the rest.
    Multi-writer ambiguity, or an OR over internal coils (``Or(ProdMode,
    MaintMode)`` — materially different machine states), stays a real choice.
    """
    viable: list[int] = []
    for ri in _rank_writers(
        pdg.writers_of.get(tag, frozenset()),
        pdg,
        program,
        tag,
        value,
        snapshot,
        clear_only=clear_only,
        # Route enumeration ranks with the *same* information as the transparent
        # walk (`_trace_back`): the steerable set lets `_writer_availability`
        # distinguish an AVAILABLE_NOW steerable false-leaf from an AFTER_PREREQ
        # one, so the default route is picked by state-consistent availability,
        # not by a strictly-less-informed ranking.  `ancestry` stays empty by
        # design — enumeration is root-only, there is no ancestor path here, so
        # there is no revisited-step-value to demote (unlike the recursive walk).
        steerable=steerable,
    ):
        ro = resolve_rung(program, pdg.rung_nodes[ri])
        if ro is not None and _can_produce(_written_value_for_tag(ro, tag), value):
            viable.append(ri)

    multi_writer = len(viable) > 1
    if (
        not multi_writer
        and viable
        and _or_ambiguity_over_inputs(viable[0], tag, value, snapshot, pdg, program, steerable)
    ):
        return ()
    options: list[tuple[int | None, _RouteDraft]] = []
    for ri in viable:
        for draft in _writer_route_drafts(
            ri, tag, value, snapshot, pdg, program, max_choices=max_choices
        ):
            options.append((ri if multi_writer else None, draft))
            if len(options) >= max_choices:
                break
        if len(options) >= max_choices:
            break

    if len(options) <= 1:
        return ()

    choices: list[_trace_read.TraceChoice] = []
    for i, (writer_ri, draft) in enumerate(options[:max_choices], 1):
        route = draft.route
        writer_locks: tuple[tuple[str, Any, int], ...] = ()
        route_condition = draft.route_condition
        if writer_ri is not None:
            route = (_writer_label(tag, value, writer_ri, pdg.rung_nodes[writer_ri]), *route)
            writer_locks = ((tag, value, writer_ri),)
            # Multi-writer: the discriminator is the writer's own guard; the
            # OR-arm condition (if any) only refines it.
            route_condition = (
                _writer_route_condition(writer_ri, tag, value, pdg, program) or route_condition
            )
        choices.append(
            _trace_read.TraceChoice(
                id=str(i),
                label=_choice_label(route, tag, value),
                route=route,
                writer_locks=writer_locks,
                or_locks=draft.or_locks,
                route_condition=route_condition,
            )
        )
    return tuple(choices)


def writer_route_eligible(
    ri: int, tag: str, pdg: ProgramGraph, program: Any, steerable: frozenset[str]
) -> bool:
    """Is multi-writer route *ri* a sensible *default* (vs a material pivot)?

    Two gates, mirroring the OR-arm collapse:

    1. **Retentive** (``tag not in ote_writes``) — a latch/SET or copy/calc into a
       held register, so establishing via this writer is not clobbered by a
       later last-wins ``out``.  A non-retentive multi-out is a ``duplicate_out``
       conflict, never a safe default.
    2. **Input-gated** (``_arm_fully_steerable``) — the writer's guard is
       reachable by directly-steerable inputs alone (``Manual``), not an internal
       coil (``ProdMode``).  This is what keeps the Burner from auto-defaulting to
       a configuration the engineer should pick deliberately.

    Routes that pass both are preferred as the default; when none do (Burner) the
    default falls to the cheapest by trace score, rung order breaking ties.
    """
    if tag in pdg.rung_nodes[ri].ote_writes:
        return False
    ro = resolve_rung(program, pdg.rung_nodes[ri])
    if ro is None:
        return False
    sp = ro.sp_tree()
    if sp is None:
        return False
    return _arm_fully_steerable(_sp_to_expr(sp), tag, steerable)


def route_rung_order(choice: _trace_read.TraceChoice) -> tuple[int, ...]:
    """Deterministic rung-order tiebreak key for a route (lowest wins).

    The committed writer's rung index for a multi-writer route, else the chosen
    OR-arm indices — so ``Or(ProdMode, MaintMode)`` defaults to the first arm."""
    if choice.writer_locks:
        return (choice.writer_locks[0][2],)
    if choice.or_locks:
        return tuple(index for _, _, index in choice.or_locks)
    return ()


def rank_trace_choices(
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    *,
    clear_only: frozenset[str] = frozenset(),
    opaque_loop: frozenset[str] = frozenset(),
    pipeline_internal_tags: frozenset[str] = frozenset(),
    prior: _trace_read.DomainPrior | None = None,
    avoid_pred: Any = None,
    rejected_actions: frozenset[tuple[str, Any]] = frozenset(),
    harness: Any = None,
    constraints: _trace_read.TraceReadConstraints | None = None,
) -> tuple[
    tuple[_trace_read.TraceChoice, ...],
    tuple[tuple[_trace_read.TraceChoice, TraceNode], ...],
]:
    """Enumerate and rank current-world root choices once.

    The complete enumerated set is returned for route reporting; the ranked set
    contains only choices admitted by the user's avoidance predicate. Both drive
    preparation and Orientation consume this reader so route order is not
    independently re-derived at the two ownership boundaries.
    """

    read = constraints or _trace_read.TraceReadConstraints(
        clear_only=clear_only,
        opaque_loop=opaque_loop,
        pipeline_internal_tags=pipeline_internal_tags,
        prior=prior,
        avoid_pred=avoid_pred,
        rejected_actions=rejected_actions,
        harness=harness,
    )
    choices = enumerate_trace_choices(
        tag,
        value,
        snapshot,
        pdg,
        program,
        steerable=steerable,
        clear_only=read.clear_only,
    )
    traced: list[tuple[_trace_read.TraceChoice, TraceNode]] = []
    for choice in choices:
        tree = _trace.trace_back(
            tag,
            value,
            snapshot,
            pdg,
            program,
            steerable,
            constraints=replace(read, route=choice, avoid_pred=None),
        )
        if read.avoid_pred is not None and _route_judgment.route_forces(
            [tree], snapshot, read.avoid_pred
        ):
            continue
        traced.append((choice, tree))
    if not traced:
        return choices, ()

    # Cross-route contradiction baseline: an identical conflict witness (tag,
    # incompatible value sets, and trace sources) shared by *every* route is
    # inherent to the goal — an SFC sequencing S_StateCurrent 3→6 shows up on all
    # of them. A witness unique to a route is that route's own contradiction (a
    # manual-mode caller gate over a body that needs production mode), and it can
    # never be satisfied — yet an already-held gate makes such a route look cheap
    # to the trace scorer. Witnesses must not collapse to tag names: common
    # ``Mode 0 ↔ 1`` sequencing must not hide Manual's distinct ``Mode 3 ↔ 1``.
    route_conflicts = [
        frozenset(_route_judgment.route_conflicts(tree, pdg, program))
        for _choice, tree in traced
    ]
    shared_conflicts = frozenset.intersection(*route_conflicts) if route_conflicts else frozenset()

    def rank(index: int) -> tuple[Any, ...]:
        choice, tree = traced[index]
        unique_conflicts = len(route_conflicts[index] - shared_conflicts)
        eligible = bool(choice.writer_locks) and writer_route_eligible(
            choice.writer_locks[0][2], tag, pdg, program, steerable
        )
        return (
            unique_conflicts,
            0 if eligible else 1,
            _route_judgment.trace_score([tree], pdg),
            route_rung_order(choice),
        )

    order = sorted(range(len(traced)), key=rank)
    return choices, tuple(traced[index] for index in order)


def _writer_route_drafts(
    ri: int,
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    *,
    max_choices: int,
) -> list[_RouteDraft]:
    """OR-arm route drafts for one writer rung's condition(s)."""
    rn = pdg.rung_nodes[ri]
    ro = resolve_rung(program, rn)
    if ro is None:
        return [_RouteDraft()]
    exprs: list[Any] = []
    sp = ro.sp_tree()
    if sp is not None:
        expr = _sp_to_expr(sp)
        if _values_match(value, False) and tag in rn.ote_writes:
            expr = _negate(expr)
        exprs.append(expr)
    if rn.subroutine:
        for cn in pdg.rung_nodes:
            if rn.subroutine in cn.calls:
                call_ro = resolve_rung(program, cn)
                call_sp = call_ro.sp_tree() if call_ro is not None else None
                if call_sp is not None:
                    exprs.append(_sp_to_expr(call_sp))
    if not exprs:
        return [_RouteDraft()]
    groups = [_enumerate_expr_routes(e, tag, snapshot, max_choices=max_choices) for e in exprs]
    return _combine_route_options(groups, max_choices=max_choices)


def _choice_label(route: tuple[str, ...], tag: str, value: Any) -> str:
    if len(route) >= 2:
        return route[-2]
    if route:
        return route[-1]
    return f"{tag}={value!r}"


def _combine_route_options(
    groups: list[list[_RouteDraft]],
    *,
    max_choices: int,
) -> list[_RouteDraft]:
    drafts = [_RouteDraft()]
    for group in groups:
        if not group:
            continue
        combined: list[_RouteDraft] = []
        for left in drafts:
            for right in group:
                combined.append(
                    _RouteDraft(
                        route=left.route + right.route,
                        or_locks=left.or_locks + right.or_locks,
                        route_condition=(
                            left.route_condition
                            if left.route_condition is not None
                            else right.route_condition
                        ),
                    )
                )
                if len(combined) >= max_choices:
                    break
            if len(combined) >= max_choices:
                break
        drafts = combined
    return drafts[:max_choices]


def _enumerate_expr_routes(
    expr: Any,
    self_tag: str,
    snapshot: dict[str, Any],
    *,
    max_choices: int,
) -> list[_RouteDraft]:
    """Enumerate OR-arm selections within one writer's condition.

    Walks only the boolean structure (And/Or/Atom) of the condition — never
    into downstream writers — so the only decisions recorded are the OR arms
    of *this* condition.  That is the root-only contract: choices distinguish
    output routes, not the full downstream plan.
    """
    if isinstance(expr, And):
        groups = [
            _enumerate_expr_routes(term, self_tag, snapshot, max_choices=max_choices)
            for term in expr.terms
        ]
        return _combine_route_options(groups, max_choices=max_choices)

    if isinstance(expr, Or):
        if _trace._expr_satisfied(expr, snapshot):
            return [_RouteDraft()]
        key = _trace._expr_route_key(expr)
        result: list[_RouteDraft] = []
        for index, term in enumerate(expr.terms):
            if isinstance(term, Atom) and term.tag == self_tag:
                continue  # self-referencing seal-in arm
            label = _route_label_for_expr(term)
            route_condition = _expr_route_condition(term)
            for route in _enumerate_expr_routes(term, self_tag, snapshot, max_choices=max_choices):
                result.append(
                    route.extend(
                        route=label,
                        or_lock=(self_tag, key, index),
                        route_condition=route_condition,
                    )
                )
                if len(result) >= max_choices:
                    return result
        return result or [_RouteDraft()]

    return [_RouteDraft()]


def _route_label_for_expr(expr: Any) -> str:
    if isinstance(expr, Atom):
        target = _trace._atom_target(expr)
        if target is not None:
            tag, value = target
            return f"{tag}={value!r}"
    return str(expr)


def _expr_route_condition(expr: Any) -> tuple[str, Any] | None:
    """A concrete ``(tag, value)`` that distinguishes *expr*'s route.

    The first equality/bit atom found walking the And/Or/Atom structure — the
    OR arm's representative leaf (``ProdMode`` / ``Manual``). Inequalities and
    other non-targetable atoms yield ``None``; the renderer falls back to the
    route label.
    """
    if isinstance(expr, Atom):
        return _trace._atom_target(expr)
    if isinstance(expr, (And, Or)):
        for term in expr.terms:
            route_condition = _expr_route_condition(term)
            if route_condition is not None:
                return route_condition
    return None


def _writer_route_condition(
    ri: int, tag: str, value: Any, pdg: ProgramGraph, program: Any
) -> tuple[str, Any] | None:
    """The gating-condition discriminator for multi-writer route *ri*.

    A multi-writer Bool surfaces because two rungs drive it under different
    guards. Returns the writer condition's representative atom so ``avoid=``
    can name and exclude the chosen route.
    """
    rn = pdg.rung_nodes[ri]
    ro = resolve_rung(program, rn)
    if ro is None:
        return None
    sp = ro.sp_tree()
    if sp is None:
        return None
    expr = _sp_to_expr(sp)
    if _values_match(value, False) and tag in rn.ote_writes:
        expr = _negate(expr)
    return _expr_route_condition(expr)


def _writer_label(tag: str, value: Any, rung_index: int, rung_node: Any) -> str:
    scope = rung_node.subroutine or rung_node.scope
    return f"{tag}={value!r} via {scope} rung {rung_index}"
