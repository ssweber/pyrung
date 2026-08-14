"""Classify how close a writer is to firing in the current state.

The module reduces writer guards against the live snapshot and exact fire-time
pins, then returns a four-level ``_WriterAvailability`` verdict. Trace attaches
that verdict to actions and candidates use it for ordering only; availability
never rejects or removes a writer that can produce the requested value.

The guard-reduction helpers are intentionally below ``trace.py`` and do not
execute the program.
"""

from __future__ import annotations

from enum import IntEnum
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.partial_eval import partial_eval
from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.pilot.static_expressions import (
    caller_guard_context,
)
from pyrung.core.analysis.pilot.static_expressions import (
    simplified_expr_tags as _simplified_expr_tags,
)
from pyrung.core.analysis.prove.expr import _eval_expr_from_state
from pyrung.core.analysis.simplified import And, Atom, Const, Or, _sp_to_expr
from pyrung.core.analysis.sp_values import (
    _extract_condition_values,
    _required_from_atom,
    _values_match,
    projected_writer_overlay,
)
from pyrung.core.crossing import UNKNOWN, Affine, evaluate_forward

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph


class _WriterAvailability(IntEnum):
    """How reachable a writer's fire condition is from the current live state.

    A total order (worst = highest): an ``And`` of guard terms is only as
    available as its least-available term, so worst-wins along a trace path
    matches ``_expr_availability``'s And-rule.
    """

    AVAILABLE_NOW = 0
    AFTER_PREREQ = 1
    UNKNOWN = 2
    UNAVAILABLE_FROM_HERE = 3


_GUARD_CONTRADICTION = object()


def _guard_eval_atom(atom: Atom, known: dict[str, Any]) -> bool | None:
    """Decide a guard atom against fire-time pins via ``_eval_expr_from_state``.

    Gated so the shared operand-substitution in ``_eval_expr_from_state`` only
    runs when every referenced tag is pinned (a str operand left unsubstituted
    would compare a value against a tag-name string).  PILOT's atom arm for the
    shared partial-eval walk (``core/analysis/partial_eval.py``); the prover's
    twin is ``_known_eval_atom`` in ``prove/expr.py``.
    """
    tags = {atom.tag}
    if atom.operand_is_tag:
        tags.add(atom.operand)
    if tags <= known.keys():
        return _eval_expr_from_state(atom, known)
    return None


def _partial_eval_guard(expr: Any, known: dict[str, Any]) -> Any:
    """Partial-evaluate a simplified guard using exact fire-time pins only."""
    if not known:
        return expr
    return partial_eval(expr, known, _guard_eval_atom)


def _reduce_guard_by_fire_pins(
    guard_expr: Any,
    ro: Any,
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
) -> Any:
    """Drop guard arms decided by a writer's own exact fire-time pins.

    For a self-referential affine writer, producing the target value fixes the
    source value for the firing scan and any one-hop derived tags.  Reduce only
    against those exact pins, not the live snapshot, so an OR arm contradicted
    by the source pin does not hide a sibling frontier.
    """
    built = projected_writer_overlay(ro, tag, value, snapshot, pdg, program, {})
    if built is None:
        return guard_expr
    overlay, _local_pinned = built
    reduced = _partial_eval_guard(guard_expr, overlay)
    if isinstance(reduced, Const):
        return None if reduced.value else _GUARD_CONTRADICTION
    return reduced


def _reduce_guard_by_pin(
    guard_expr: Any, src_tag: str, src_val: Any, snapshot: dict[str, Any]
) -> Any:
    """Reduce a copy writer's guard by its own source pin.

    A copy ``copy(src, dst)`` forces ``src == src_val`` to produce the target, so a
    guard conjunct that constrains *only* ``src`` is fully decided by that pin:

    - the pin **violates** it (``UnitModeCmd != 0`` beside source ``== 0``) → the
      writer can never emit this value; return :data:`_GUARD_CONTRADICTION` so the
      caller drops the writer (producibility);
    - the pin **satisfies** it (``UnitModeCmd != 0`` beside source ``== 2``) → it is
      redundant; drop it so it does not surface as a second, conflicting frontier
      on ``src`` (which would fight the source pin).

    Conjuncts on other tags are left untouched for normal tracing.  Returns the
    reduced expression, ``None`` when nothing remains, or the contradiction
    sentinel.  Only source-*only* conjuncts are decided; a multi-tag conjunct
    (whose other operands may yet be steered) is never dropped or rejected.
    """
    overlay = {**snapshot, src_tag: src_val}
    terms = list(guard_expr.terms) if isinstance(guard_expr, And) else [guard_expr]
    kept: list[Any] = []
    for term in terms:
        if _simplified_expr_tags(term) == {src_tag}:
            decided = _eval_expr_from_state(term, overlay)
            if decided is False:
                return _GUARD_CONTRADICTION
            if decided is True:
                continue  # satisfied by the pin — redundant, drop
        kept.append(term)
    if not kept:
        return None
    if len(kept) == 1:
        return kept[0]
    return And(terms=tuple(kept))


def _equality_gated_coil(
    tag: str, value: Any, pdg: ProgramGraph, program: Any
) -> tuple[str, frozenset[Any]] | None:
    """The channel-register value SET a Bool mode-flag stands for, else ``None``.

    ``out(S_ManualMode)`` under ``rung(S_UnitModeCurrent == 3)`` means
    ``S_ManualMode=True`` is *equivalent to* ``S_UnitModeCurrent=3`` — return
    ``("S_UnitModeCurrent", {3})``.  Generalized past the single-equality case by
    inverting each writer's guard into the *set* of channel values it implies,
    via the And-narrows/Or-widens value lattice (:func:`_channel_constraint`):

    - a flag gated ``Or(Reg==3, Reg==5)`` aliases to ``("Reg", {3, 5})``;
    - a flag with several plain ``out`` writers that all gate the *same*
      channel register aliases to the union of their value sets (the flag is
      ``True`` only if some writer fired, and each writer pins the register).

    Fires only for a Bool driven ``True`` by plain ``out`` coils (``ote_writes``)
    whose guards each constrain exactly one channel register (never ``tag``
    itself) to a finite value set.  A writer that constrains a *different*
    register, more than one register, or nothing invertible (an inequality- or
    live-word-only gate — :func:`_channel_constraint` returns ``None``) makes
    the whole flag un-aliasable: return ``None`` and never fabricate a channel
    constraint.  Lets :func:`_route_conflicts` catch a caller-gate mode that
    contradicts the mode the body requires, even across differently named tags.
    """
    from pyrung.core.analysis.pilot.static_expressions import _channel_constraint

    if value is not True:
        return None
    writers = pdg.writers_of.get(tag, frozenset())
    if not writers:
        return None

    channel: str | None = None
    value_union: set[Any] = set()
    for wi in writers:
        node = pdg.rung_nodes[wi]
        if tag not in node.ote_writes:
            return None
        ro = resolve_rung(program, node)
        if ro is None:
            return None
        sp = ro.sp_tree()
        if sp is None:
            return None
        expr = _sp_to_expr(sp)
        others = [t for t in _extract_condition_values(expr) if t != tag]
        if len(others) != 1:
            return None  # not a clean single-register discriminator
        other = others[0]
        if channel is None:
            channel = other
        elif channel != other:
            return None  # writers disagree on the channel register
        constraint = _channel_constraint(expr, other, {})
        if not constraint:
            return None  # inequality / live-word gate — no finite value set
        value_union |= set(constraint)

    if channel is None or not value_union:
        return None
    return (channel, frozenset(value_union))


def _expr_availability(
    expr: Any,
    snapshot: dict[str, Any],
    steerable: frozenset[str],
    current_tags: frozenset[str],
    pdg: ProgramGraph,
    program: Any,
) -> _WriterAvailability:
    """Current-frame availability of a guard expression.

    False steerable leaves are still available tools; false current-state leaves
    are unavailable here; other false leaves are prerequisites trace may pursue.

    Notion **#3** of three "what's still needed" — per-writer, evaluated in the LIVE
    snapshot, answering *"how far from firing is this guard?"* as a 4-valued tier.
    Its ``AFTER_PREREQ`` leaves are the same prerequisites #1 ``frontier_pairs``
    surfaces and #2 ``_projected_guard_frontier`` returns as ``frontier`` tags; #3
    takes #2's ``counterfactual`` as an input. The agreement among all three is
    pinned by ``tests/core/analysis/test_pilot_needed_vocabulary.py``.
    """
    if isinstance(expr, Const):
        return (
            _WriterAvailability.AVAILABLE_NOW
            if expr.value
            else _WriterAvailability.UNAVAILABLE_FROM_HERE
        )
    if isinstance(expr, Atom):
        result = _eval_expr_from_state(expr, snapshot)
        if result is True:
            return _WriterAvailability.AVAILABLE_NOW
        if result is None:
            return _WriterAvailability.UNKNOWN
        pairs = _required_from_atom(expr)
        if pairs:
            alias_states: list[_WriterAvailability] = []
            for req_tag, req_value in pairs:
                alias = _equality_gated_coil(req_tag, req_value, pdg, program)
                if alias is None:
                    continue
                channel, values = alias
                if channel not in snapshot:
                    alias_states.append(_WriterAvailability.UNKNOWN)
                elif any(_values_match(snapshot.get(channel), v) for v in values):
                    alias_states.append(_WriterAvailability.AVAILABLE_NOW)
                else:
                    alias_states.append(_WriterAvailability.UNAVAILABLE_FROM_HERE)
            if alias_states:
                return min(alias_states)
        if expr.tag in current_tags:
            return _WriterAvailability.UNAVAILABLE_FROM_HERE
        if expr.tag in steerable:
            return _WriterAvailability.AVAILABLE_NOW
        return _WriterAvailability.AFTER_PREREQ
    if isinstance(expr, And):
        return max(
            (
                _expr_availability(term, snapshot, steerable, current_tags, pdg, program)
                for term in expr.terms
            ),
            default=_WriterAvailability.AVAILABLE_NOW,
        )
    if isinstance(expr, Or):
        return min(
            (
                _expr_availability(term, snapshot, steerable, current_tags, pdg, program)
                for term in expr.terms
            ),
            default=_WriterAvailability.UNAVAILABLE_FROM_HERE,
        )
    return _WriterAvailability.UNKNOWN


def _caller_availability(
    rung_node: Any,
    snapshot: dict[str, Any],
    steerable: frozenset[str],
    current_tags: frozenset[str],
    pdg: ProgramGraph,
    program: Any,
) -> _WriterAvailability:
    """Availability of the subroutine call path for ``rung_node``.

    A body rung with an unconditionally-true local guard is not available when
    its subroutine is only called from a contradictory state.  Reads the one
    shared caller-guard recursion (``caller_guard_context`` →
    ``simplified._build_guard_ctx``): the full symbolic call guard for the
    writer's subroutine — OR over call sites, each ANDed with the caller's own
    recursive call guard — classified through the same ``_expr_availability``
    the transparent walk uses.  A subroutine with no call sites classifies
    UNKNOWN (punt, never fabricate); a non-subroutine writer is available now.
    """
    subroutine = getattr(rung_node, "subroutine", None)
    if not subroutine:
        return _WriterAvailability.AVAILABLE_NOW

    ctx = caller_guard_context(program)
    if not ctx.caller_map.get(subroutine):
        return _WriterAvailability.UNKNOWN
    guard_expr = ctx.caller_guards.get(subroutine)
    if guard_expr is None:
        return _WriterAvailability.UNKNOWN
    return _expr_availability(guard_expr, snapshot, steerable, current_tags, pdg, program)


def _writer_availability(
    ro: Any,
    rung_node: Any,
    wv: Any,
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    is_counterfactual: bool,
    ancestry_tags: frozenset[str] = frozenset(),
) -> _WriterAvailability:
    """State-indexed availability for a candidate writer.

    ``ancestry_tags`` are the non-steerable registers the walk is already
    deriving a need through (the trace ancestry).  Their live value is
    authoritative for state-consistency exactly like an ``opaque_loop`` pin:
    a writer demanding a *different* value of such a register is asking to
    drive the very state the plan routes through somewhere else — circular,
    so it classifies UNAVAILABLE_FROM_HERE rather than a mere prerequisite.
    This is what arms state-consistent writer selection for *transparent*
    (plain-copy) state machines, where ``opaque_loop`` is empty.
    """
    if is_counterfactual:
        return _WriterAvailability.UNAVAILABLE_FROM_HERE

    current_tags = frozenset((tag,)) | opaque_loop | ancestry_tags
    availability = _caller_availability(rung_node, snapshot, steerable, current_tags, pdg, program)
    if isinstance(wv, Affine) and wv.source == tag:
        produced = evaluate_forward(wv, snapshot)
        if produced is UNKNOWN or not _values_match(produced, value):
            availability = _WriterAvailability.AFTER_PREREQ
    elif isinstance(wv, Affine):
        produced = evaluate_forward(wv, snapshot)
        if (
            produced is not UNKNOWN
            and not _values_match(produced, value)
            and wv.source not in steerable
            and not pdg.writers_of.get(wv.source)
        ):
            # A frozen reference source cannot be steered to make this writer
            # produce a different value. Keep the writer as an honest fallback,
            # but rank it behind a writer whose source already matches.
            availability = _WriterAvailability.UNAVAILABLE_FROM_HERE

    sp = ro.sp_tree()
    if sp is None:
        return availability
    guard_expr = _sp_to_expr(sp)
    guard_availability = _expr_availability(
        guard_expr, snapshot, steerable, current_tags, pdg, program
    )
    return max(availability, guard_availability)
