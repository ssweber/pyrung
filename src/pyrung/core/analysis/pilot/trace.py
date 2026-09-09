"""Backward static requirement analysis for PILOT.

``trace_back`` resolves a target through writers, guards, copies, calculations,
and accumulating instructions until it reaches steerable actions or an
unreadable requirement. The module also enumerates trace routes, determines
steerable and clear-only inputs, and ranks writers without suppressing a writer
that could produce the requested value.

Tracing reads program structure and a snapshot. It does not execute trials,
record transition knowledge, or choose the iteration's final candidate order.

Trace consumes execution evidence without taking ownership of it: orientation
may project an exact current-world singleton Pulse rejection back to its
identical trace leaf, but never a rejected joint act onto one member.  Exact
leaf rejections only order unlocked local OR, table-value, and nested-writer
alternatives.  A multi-leaf branch is a distinct, still-untested joint
artifact; a branch with a dead end cannot replace a rejected branch, and the
best rejected branch is retained when no untried alternative without a dead
end survives, so the frontier stays visible.  Only a retained writer attempt's
children and visited state are adopted into the caller's tree; trace-wide
caller locks and guard memoization remain shared evidence rather than
writer-local build state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from itertools import product
from typing import TYPE_CHECKING, Any, Generic, TypeVar

import pyrung.core.analysis.pilot.availability as _availability
import pyrung.core.analysis.pilot.route_judgment as _route_judgment
import pyrung.core.analysis.pilot.trace_constraints as _trace_constraints
import pyrung.core.analysis.pilot.trace_read as _trace_read
from pyrung.core.analysis.pdg import TagRole, resolve_rung
from pyrung.core.analysis.pilot.advance import demand_holds
from pyrung.core.analysis.pilot.navigation_contracts import CrossingFidelity
from pyrung.core.analysis.pilot.static_expressions import (
    _atom_text,
)
from pyrung.core.analysis.pilot.trace_tree import (
    _FORM_TO_OP,
    TraceAction,
    TraceCrossingBranch,
    TraceNode,
    _visit_key,
)
from pyrung.core.analysis.pilot.writer_selection import (
    _UNRESOLVED,
    _can_produce,
    _concrete_written_value,
    _producer_constraints,
    _producer_pins,
    _rank_writers,
    _reverse_writer,
    _sole_write_instr,
    _WriterRank,
)
from pyrung.core.analysis.prove.expr import _eval_expr_from_state
from pyrung.core.analysis.return_guards import return_early_guard_exprs
from pyrung.core.analysis.reverse_semantics import normalize_reverse_result
from pyrung.core.analysis.simplified import (
    And,
    Atom,
    Or,
    _condition_to_expr,
    _negate,
    _sp_to_expr,
)
from pyrung.core.analysis.sp_values import (
    _FLIP_FORM,
    _invert_affine,
    _required_from_atom,
    _values_match,
    _written_value_for_tag,
)
from pyrung.core.crossing import (
    Affine,
    AffineCmp,
    Aggregate,
    Cmp,
    Constraint,
    CrossingContext,
    Eq,
    ReverseResult,
    eq_target,
)
from pyrung.core.instruction.advance import constraint_holds

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph

_TraceChoicePayload = TypeVar("_TraceChoicePayload")


@dataclass(frozen=True)
class _WriterRankReceipt:
    """Reusable state-dependent writer classification for one frozen trace."""

    ranked: tuple[int, ...]
    availability: tuple[tuple[int, _availability._WriterAvailability], ...]
    ranking: tuple[_WriterRank, ...]
    reverses: tuple[tuple[int, ReverseResult], ...]


_WriterRankMemo = dict[tuple[Any, ...], _WriterRankReceipt]


@dataclass(frozen=True)
class _TraceEnv:
    """Invariant context threaded through one backward trace.

    Everything here is constant for the whole trace — only ``tag``/``value`` (or
    ``expr``), ``provenance``, ``_visited``, ``_ancestry`` and ``_depth`` change
    between recursive calls. ``avoid_pred`` excludes alternatives that force
    the avoided condition (``None`` for an unconstrained trace).

    The world-describing subset — ``snapshot`` / ``pdg`` / ``program`` /
    ``steerable`` / ``opaque_loop`` / ``prior`` — is the **read-side seam**: this
    env structurally satisfies :class:`~pyrung.core.analysis.pilot.trace_read.WalkContext`,
    so a read-side capability consuming a ``WalkContext`` takes this ``env``
    directly without depending on the recursion controls.
    """

    snapshot: dict[str, Any]
    pdg: ProgramGraph
    program: Any
    steerable: frozenset[str]
    # Clear-only (ack-cleared momentary) command tags — off-path maintenance levers
    # that ``_rank_writers`` must not treat as a *preferred* init/reset writer gate.
    clear_only: frozenset[str] = frozenset()
    opaque_loop: frozenset[str] = frozenset()
    pipeline_internal_tags: frozenset[str] = frozenset()
    writer_locks: dict[tuple[str, Any], int] | None = None
    or_locks: dict[tuple[str, str], int] | None = None
    prior: _trace_read.DomainPrior | None = None
    avoid_pred: Any = None
    # Exact singleton actions rejected in this executable world. Joint acts are
    # intentionally absent: disproving ``A + B`` does not disprove either member
    # under another co-action context.
    rejected_actions: frozenset[tuple[str, Any]] = frozenset()
    max_depth: int = 15
    # Installed Harness, when tracing on a fork that has one.  Lets the coast
    # disposition attach a harness-linked sensor's *driver* (the input that makes
    # it ramp) as a steerable sibling of the coast leaf.  ``None`` off-fork.
    harness: Any = None
    # Exact executor memory at this read boundary. Trace consumes this receipt
    # only to rule out a currently-spent one-shot writer; it never mutates or
    # reconstructs instruction state.
    execution_memory: Mapping[str, Any] | None = None
    advance_index: Any = None
    # Per-trace memo for the writer-guard rejection arm (:func:`_writer_guard_verdict`).
    # Pure over the trace-invariant env (frozen snapshot + constant domains), so a
    # verdict is deterministic in ``(rung id, fire-pins, guard route key)`` and can
    # be cached for the whole recursion.  Fresh dict per :func:`_env_for` call.
    guard_memo: dict[Any, str] = field(default_factory=dict)
    # Writer ranking is likewise pure within this frozen read. Alternative
    # branches often revisit the same demand and ancestry after cloning the
    # visited set; retain the classified receipt instead of re-projecting every
    # writer against the same snapshot.
    writer_rank_memo: _WriterRankMemo = field(default_factory=dict)
    # One coherent call-site route per subroutine for this trace.  A subroutine
    # writer can be reached repeatedly through different ancestry/visited
    # contexts; choosing its caller independently at every occurrence unions
    # mutually alternative triggers into one action set (for example normal
    # ModeChangeRequest plus SimulateFirstScan).  The first ranked caller is the
    # route; subsequent occurrences reuse it, matching root writer/OR locks.
    caller_locks: dict[str, int] = field(default_factory=dict)
    # Joint terminal supports rank alternatives only; they never authorize an
    # assignment or reject the temporary detour needed by a retentive target.
    preferred_actions: tuple[tuple[str, Any], ...] = ()


def _env_for(
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    *,
    clear_only: frozenset[str] = frozenset(),
    opaque_loop: frozenset[str] = frozenset(),
    pipeline_internal_tags: frozenset[str] = frozenset(),
    route: _trace_read.TraceChoice | None = None,
    writer_locks: dict[tuple[str, Any], int] | None = None,
    or_locks: dict[tuple[str, str], int] | None = None,
    prior: _trace_read.DomainPrior | None = None,
    avoid_pred: Any = None,
    rejected_actions: frozenset[tuple[str, Any]] = frozenset(),
    max_depth: int = 15,
    harness: Any = None,
    execution_memory: Mapping[str, Any] | None = None,
    writer_rank_memo: _WriterRankMemo | None = None,
) -> _TraceEnv:
    """Build a trace env, resolving a ``TraceChoice`` route to its lock maps once."""
    from pyrung.core.analysis.pilot.advance import build_advance_index

    if route is not None:
        writer_locks = route.writer_lock_map()
        or_locks = route.or_lock_map()
    return _TraceEnv(
        snapshot=snapshot,
        pdg=pdg,
        program=program,
        steerable=steerable,
        clear_only=clear_only,
        opaque_loop=opaque_loop,
        pipeline_internal_tags=pipeline_internal_tags,
        writer_locks=writer_locks,
        or_locks=or_locks,
        prior=prior,
        avoid_pred=avoid_pred,
        rejected_actions=rejected_actions,
        max_depth=max_depth,
        harness=harness,
        execution_memory=execution_memory,
        advance_index=build_advance_index(program, harness),
        writer_rank_memo=writer_rank_memo if writer_rank_memo is not None else {},
    )


def _env_from_constraints(
    read: _trace_read.TraceReadConstraints,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    *,
    writer_locks: dict[tuple[str, Any], int] | None = None,
    or_locks: dict[tuple[str, str], int] | None = None,
    max_depth: int = 15,
    writer_rank_memo: _WriterRankMemo | None = None,
) -> _TraceEnv:
    """Lower a caller-owned trace request to the recursion engine's environment."""

    return _env_for(
        snapshot,
        pdg,
        program,
        steerable,
        clear_only=read.clear_only,
        opaque_loop=read.opaque_loop,
        pipeline_internal_tags=read.pipeline_internal_tags,
        route=read.route,
        writer_locks=writer_locks,
        or_locks=or_locks,
        prior=read.prior,
        avoid_pred=read.avoid_pred,
        rejected_actions=read.rejected_actions,
        max_depth=max_depth,
        harness=read.harness,
        execution_memory=read.execution_memory,
        writer_rank_memo=writer_rank_memo,
    )


def _expr_route_key(expr: Any) -> str:
    return repr(expr)


# ---------------------------------------------------------------------------
# TraceNode — the backward-trace tree
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# trace_back — recursive backward trace
# ---------------------------------------------------------------------------

# Feedback-loop guard: how many *distinct* values of one opaque-loop register
# may be expanded along a single ancestor path before further occurrences are
# treated as a data-flow cycle.  Only tags in ``opaque_loop`` (jump-table
# state registers — see ``detect_opaque_loop``) are guarded; simple direct-copy
# state machines are never cut.  Budget 1 keeps the direct prerequisite chain
# (StateCurrent=6 <- StateRequested=6 <- command) but cuts the cross-state
# wandering (StateCurrent=6 -> ... -> StateCurrent=7 -> ...), emitting a leaf
# so Compass can learn the transition from action observations.
_SAME_TAG_VALUE_BUDGET = 1


def _expr_satisfied(expr: Any, snapshot: dict[str, Any]) -> bool:
    """Whether *expr* is definitely satisfied in *snapshot*.

    Delegates to the prover's ``_eval_expr_from_state`` which returns
    ``None`` for undecidable terms (rise/fall, missing tags).  Treat
    ``None`` as not-satisfied — conservative for backward tracing.
    """
    return _eval_expr_from_state(expr, snapshot) is True


def target_reached(
    snapshot: Mapping[str, Any],
    target_tag: str,
    target_value: Any,
    target_predicate: Any = None,
) -> bool:
    """Whether the (possibly relational) target holds in *snapshot*.

    A relational target (``A op B``) is judged by evaluating its live predicate
    — the goal is the relation, not a frozen value.  An equality / Bool target
    falls back to ``_values_match`` on ``(target_tag, target_value)``.
    """
    if target_predicate is not None:
        return _eval_expr_from_state(target_predicate, snapshot) is True
    return _values_match(snapshot.get(target_tag), target_value)


def _trace_crossing_branches(
    env: _TraceEnv,
    atom: Atom,
    provenance: tuple[str, ...],
    *,
    visited: set[tuple[str, Any]],
    ancestry: tuple[tuple[str, Any], ...],
    relational_goal: Any,
    depth: int,
) -> tuple[TraceCrossingBranch, ...]:
    """Resolve reverse/proposal branches without dissolving constraint DNF.

    Each crossing branch is a conjunction. A constraint may itself expose
    multiple reactive levers; their Cartesian choices split that conjunction
    into concrete atomic overlays, while sibling crossing branches remain
    independent alternatives. Unsupported or dead conjuncts invalidate only
    their own branch.
    """

    if atom.tag in env.steerable:
        return ()
    if atom.operand_is_tag and (atom.operand_scale != 1 or atom.operand_offset != 0):
        return ()
    op = _FORM_TO_OP.get(atom.form)
    if op is None:
        return ()
    instr = _sole_write_instr(atom.tag, env.pdg, env.program)
    if instr is None:
        return ()

    from pyrung.core.analysis import crossings

    target = Cmp(atom.tag, op, atom.operand, bound_is_tag=atom.operand_is_tag)
    context = CrossingContext(snapshot=env.snapshot)
    reverse = normalize_reverse_result(crossings.reverse(instr, None, target, context))
    proposed = reverse.fallthrough
    if proposed:
        proposal = crossings.propose(instr, None, target, context)
        if proposal.empty:
            return ()
        crossing_branches = proposal.branches
        reason = proposal.reason
        verify_required = proposal.verify_required
        exact: bool | None = None
    else:
        if reverse.contradiction or reverse.trivial:
            return ()
        crossing_branches = reverse.branches
        # Preserve legacy scalar reverse navigation; grouping is required when
        # reverse presents a real DNF or any conjunctive predecessor branch.
        if len(crossing_branches) == 1 and len(crossing_branches[0]) == 1:
            return ()
        reason = "sound reverse crossing"
        exact = reverse.exact
        verify_required = not reverse.exact

    receipts: list[TraceCrossingBranch] = []
    seen_overlays: set[tuple[tuple[str, Any], ...]] = set()
    marker = "; ".join(
        part
        for part in (
            (
                f"crossing proposal: {reason}"
                if proposed and reason
                else "crossing proposal"
                if proposed
                else "inexact reverse crossing"
                if not exact
                else ""
            ),
            "verification required" if verify_required else "",
        )
        if part
    )

    for crossing_branch in crossing_branches:
        choices_by_constraint: list[tuple[tuple[TraceNode, ...], ...]] = []
        supported = True
        for constraint in crossing_branch:
            if constraint_holds(constraint, env.snapshot) is True:
                continue
            req = _trace_constraints._constraint_atom(constraint)
            if req is None:
                supported = False
                break
            concrete = _trace_constraints._atom_target(req, env.snapshot)
            if concrete is not None:
                tag, value = concrete
                child = _trace_back(
                    env,
                    tag,
                    value,
                    _visited=set(visited),
                    _ancestry=ancestry,
                    _depth=depth + 1,
                )
                if not child.is_steerable or child.children or child.pulse:
                    supported = False
                    break
                if not child.provenance:
                    child.provenance = provenance
                choices_by_constraint.append(((child,),))
                continue
            if req.form not in ("lt", "le", "gt", "ge", "ne"):
                supported = False
                break
            levers = _trace_constraints._inequality_levers(
                req,
                env.snapshot,
                env.steerable,
                env.pdg,
                env.prior,
                env.program,
            )
            alternatives: list[tuple[TraceNode, ...]] = []
            for lever in levers:
                child = _trace_back(
                    env,
                    lever.tag,
                    lever.value,
                    _visited=set(visited),
                    _ancestry=ancestry,
                    _preserve_predicate=relational_goal,
                    _depth=depth + 1,
                )
                if not child.is_steerable or child.children or child.pulse:
                    continue
                if not child.provenance:
                    child.provenance = provenance
                child.lever = lever.label
                child.heuristic = lever.heuristic or verify_required
                child.note = "; ".join(part for part in (lever.note, marker) if part)
                alternatives.append((child,))
            if not alternatives:
                supported = False
                break
            choices_by_constraint.append(tuple(alternatives))
        if not supported:
            continue

        selections = product(*choices_by_constraint) if choices_by_constraint else ((),)
        for selection in selections:
            nodes = tuple(node for choice in selection for node in choice)
            if not nodes or not _route_judgment.route_has_no_dead_end(list(nodes)):
                continue
            details: list[TraceAction] = []
            by_tag: dict[str, Any] = {}
            conflict = False
            for node in nodes:
                for detail in node.ordered_action_details():
                    prior_value = by_tag.get(detail.tag, _UNRESOLVED)
                    if prior_value is not _UNRESOLVED and not _values_match(
                        prior_value, detail.value
                    ):
                        conflict = True
                        break
                    by_tag[detail.tag] = detail.value
                    if detail.pair not in {existing.pair for existing in details}:
                        details.append(
                            replace(
                                detail,
                                heuristic=detail.heuristic or verify_required,
                                note="; ".join(part for part in (detail.note, marker) if part),
                            )
                        )
                if conflict:
                    break
            details.sort(key=lambda detail: (detail.tag, repr(detail.value)))
            pairs = tuple(detail.pair for detail in details)
            if conflict or not pairs or pairs in seen_overlays:
                continue
            seen_overlays.add(pairs)
            receipts.append(
                TraceCrossingBranch(
                    actions=tuple(details),
                    fidelity=CrossingFidelity(
                        constraints=tuple(crossing_branch),
                        reason=reason,
                        verify_required=verify_required,
                        exact=exact,
                        proposed=proposed,
                    ),
                )
            )
    return tuple(receipts)


def _trace_frozen_crossing_branches(
    env: _TraceEnv,
    atom: Atom,
    provenance: tuple[str, ...],
    *,
    visited: set[tuple[str, Any]],
    ancestry: tuple[tuple[str, Any], ...],
    relational_goal: Any,
    depth: int,
) -> tuple[TraceCrossingBranch, ...]:
    """Try grouped crossings after freezing each side of a tag-bound compare."""

    if not atom.operand_is_tag or atom.form not in _FLIP_FORM or atom.operand_scale == 0:
        return ()
    variants: list[Atom] = []
    raw_right = env.snapshot.get(atom.operand)
    try:
        right_now = (
            atom.operand_scale * raw_right + atom.operand_offset if raw_right is not None else None
        )
    except TypeError:
        right_now = None
    if isinstance(right_now, (int, float)) and not isinstance(right_now, bool):
        variants.append(Atom(atom.tag, atom.form, right_now))
    left_now = env.snapshot.get(atom.tag)
    if isinstance(left_now, (int, float)) and not isinstance(left_now, bool):
        right_form = _FLIP_FORM[atom.form] if atom.operand_scale > 0 else atom.form
        variants.append(
            Atom(
                atom.operand,
                right_form,
                (left_now - atom.operand_offset) / atom.operand_scale,
            )
        )

    receipts: list[TraceCrossingBranch] = []
    original = _atom_text(atom)
    for variant in variants:
        for branch in _trace_crossing_branches(
            env,
            variant,
            provenance,
            visited=visited,
            ancestry=ancestry,
            relational_goal=relational_goal,
            depth=depth,
        ):
            actions = tuple(
                replace(
                    action,
                    note="; ".join(
                        part
                        for part in (
                            action.note,
                            f"reactive frozen-side candidate to satisfy {original}",
                        )
                        if part
                    ),
                )
                for action in branch.actions
            )
            receipt = replace(branch, actions=actions)
            if receipt.pairs not in {existing.pairs for existing in receipts}:
                receipts.append(receipt)
    return tuple(receipts)


def _mark_pulse(nodes: list[TraceNode]) -> None:
    for node in nodes:
        if node.is_steerable:
            node.pulse = True
        _mark_pulse(node.children)


def _needs_prior_program_stage(node: TraceNode, env: _TraceEnv) -> bool:
    """Whether this demand first needs persistent program state to move."""

    if node.writer_rung is not None:
        rung = env.pdg.rung_nodes[node.writer_rung]
        if (
            node.tag in rung.all_writes
            and node.tag not in rung.ote_writes
            and (rung.condition_reads or node.tag in rung.data_reads)
        ):
            return True
    return any(_needs_prior_program_stage(child, env) for child in node.children)


def _trace_demand(
    env: _TraceEnv,
    demand: Any,
    *,
    pulse: bool,
    provenance: tuple[str, ...],
    depth: int,
    retain_satisfied: bool,
) -> list[TraceNode]:
    """Resolve one profile condition through the ordinary trace machinery."""

    if demand is None or demand.condition is None:
        return []
    expression = _condition_to_expr(demand.condition)
    if not demand.value:
        expression = _negate(expression)
    nodes = _trace_expression(
        env,
        expression,
        "",
        provenance=provenance,
        _visited=set(),
        _depth=depth,
    )

    def _retain_satisfied_steerables(node: TraceNode) -> None:
        # A profile hold is a lifetime requirement, not merely a value check.
        # Keep an already-true input in the action details so PILOT can renew
        # its scoped rung after the previous boundary expires.
        if node.satisfied and node.tag in env.steerable:
            node.is_steerable = True
        for child in node.children:
            _retain_satisfied_steerables(child)

    if retain_satisfied:
        for node in nodes:
            _retain_satisfied_steerables(node)
    for node in nodes:
        if (
            not node.satisfied
            and not node.is_steerable
            and (
                node.tag in env.opaque_loop
                or node.tag in env.pipeline_internal_tags
                or _needs_prior_program_stage(node, env)
            )
        ):
            # First move an owned state/pipeline register into the condition
            # the instruction needs.  The command that establishes that state
            # is not itself a level hold for the later timer/counter coast.
            node.data_flow = "enable"
    if pulse:
        _mark_pulse(nodes)
    return nodes


@dataclass(frozen=True)
class _CallGateTrace:
    """One main-program call site and the prerequisites that enable it."""

    caller_index: int
    nodes: tuple[TraceNode, ...]


def _select_call_gate(
    env: _TraceEnv,
    subroutine: str,
    call_gates: list[_CallGateTrace],
) -> _CallGateTrace | None:
    """Choose one coherent call site for a subroutine used by this trace."""

    if not call_gates:
        return None

    locked_caller = env.caller_locks.get(subroutine)
    if locked_caller is not None:
        return next(
            (call_gate for call_gate in call_gates if call_gate.caller_index == locked_caller),
            None,
        )

    alternatives: list[_TraceAlternative[_CallGateTrace]] = []
    for call_gate in call_gates:
        nodes = list(call_gate.nodes)
        has_no_dead_end = _route_judgment.route_has_no_dead_end(nodes)
        alternatives.append(
            _trace_alternative(
                choice=call_gate,
                nodes=nodes,
                rank=(
                    0 if has_no_dead_end else 1,
                    *_route_judgment.trace_score(nodes, env.pdg),
                ),
                env=env,
            )
        )

    selection = _select_trace_alternative(
        tuple(alternatives),
        replace_rejected_choice=False,
    )
    alternative = selection.chosen or selection.blocked_alternative
    if alternative is None:
        return None
    env.caller_locks.setdefault(subroutine, alternative.choice.caller_index)
    return alternative.choice


def _owner_call_gate_nodes(
    env: _TraceEnv,
    owner: Any,
    provenance: tuple[str, ...],
    depth: int,
) -> list[TraceNode]:
    """Trace the call gate when an advance owner lives in a subroutine."""

    instruction = owner.instruction
    if instruction is None:
        return []
    subroutine: str | None = None
    for rung_node in env.pdg.rung_nodes:
        rung = resolve_rung(env.program, rung_node)
        if rung is not None and instruction in getattr(rung, "_instructions", ()):
            subroutine = rung_node.subroutine
            break
    if subroutine is None:
        return []

    call_gates: list[_CallGateTrace] = []
    for caller_index, caller in enumerate(env.pdg.rung_nodes):
        if subroutine not in caller.calls:
            continue
        rung = resolve_rung(env.program, caller)
        sp = rung.sp_tree() if rung is not None else None
        if sp is None:
            return []
        expression = _sp_to_expr(sp)
        if _expr_satisfied(expression, env.snapshot):
            return []
        call_gates.append(
            _CallGateTrace(
                caller_index=caller_index,
                nodes=tuple(
                    _trace_expression(
                        env,
                        expression,
                        "",
                        provenance=provenance,
                        _visited=set(),
                        _depth=depth + 1,
                    )
                ),
            )
        )
    selected = _select_call_gate(env, subroutine, call_gates)
    if selected is None:
        return []
    return list(selected.nodes)


def _advance_frontier(
    env: _TraceEnv,
    constraint: Constraint,
    provenance: tuple[str, ...],
    *,
    depth: int,
) -> TraceNode | None:
    """Return the one generic frontier for an instruction-owned channel."""

    if not isinstance(constraint, (Eq, Cmp, AffineCmp)):
        return None
    owner = env.advance_index.resolve(constraint.tag)
    if owner is None:
        conflict = env.advance_index.conflict_message(constraint.tag)
        if conflict is None:
            return None
        return TraceNode(
            tag=constraint.tag,
            value=getattr(constraint, "bound", None),
            provenance=provenance,
            note=conflict,
        )

    step = owner.profile.plan(constraint, env.snapshot)
    if step is None:
        return None
    if (
        owner.instruction is not None
        and owner.profile.linear is not None
        and owner.profile.done is not None
        and constraint.tag == owner.profile.done.name
        and owner.profile.linear.distance(constraint, env.snapshot) is None
        and step.progress is None
    ):
        # An ordinary instruction restore/clear is not forward scalar motion;
        # correction and regression policy own it. A harness coupling has no
        # program instruction and its declared upstream demand is the only
        # semantic route to the physical feedback boundary, so retain that
        # reader-owned chain.
        return None
    stage_boundary = owner.profile.linear is None
    atom = _trace_constraints._constraint_atom(step.until)
    boundary = TraceNode(
        tag=step.until.tag,
        value=(atom.operand if atom is not None else step.until),
        satisfied=constraint_holds(step.until, env.snapshot) is True,
        provenance=provenance,
        relational=isinstance(step.until, (Cmp, AffineCmp)) and stage_boundary,
        predicate=atom if stage_boundary else None,
        advance=step,
        owner_boundary=(
            (constraint.tag, constraint.bound)
            if isinstance(constraint, Cmp) and not constraint.bound_is_tag
            else (constraint.tag, next(iter(constraint.values)))
            if isinstance(constraint, Eq) and len(constraint.values) == 1
            else None
        ),
        owner_condition=constraint,
        stage_boundary=stage_boundary,
        linear_boundary=not stage_boundary,
    )
    if (
        isinstance(constraint, (Cmp, AffineCmp))
        and owner.profile.linear is not None
        and owner.profile.accumulator is not None
        and constraint.tag == owner.profile.accumulator.name
        and owner.instruction is not None
    ):
        # Keep the established scalar coast shape for an instruction-owned
        # accumulator threshold. Its enable is traced at the producer that
        # needs it; expanding that future enable here would add a second
        # prerequisite and distort route scoring and target distance.
        return boundary
    gate_nodes = _owner_call_gate_nodes(env, owner, provenance, depth)
    running_linear = owner.profile.linear is not None and step.progress is not None
    demand_nodes: list[TraceNode] = []
    for demand in step.holds:
        if running_linear and demand_holds(demand, env.snapshot):
            continue
        demand_nodes.extend(
            _trace_demand(
                env,
                demand,
                pulse=False,
                provenance=provenance,
                depth=depth + 1,
                retain_satisfied=owner.profile.linear is None,
            )
        )
    if step.pulse is not None:
        demand_nodes.extend(
            _trace_demand(
                env,
                step.pulse,
                pulse=True,
                provenance=provenance,
                depth=depth + 1,
                retain_satisfied=owner.profile.linear is None,
            )
        )
    establish_nodes = [
        node
        for node in (*gate_nodes, *demand_nodes)
        if node.data_flow == "enable" and not node.satisfied
    ]
    if establish_nodes:
        scalar_coast = (
            owner.profile.linear is not None
            and owner.profile.accumulator is not None
            and step.until.tag == owner.profile.accumulator.name
            and owner.instruction is not None
        )
        requires_action = any(
            not leaf.satisfied
            and (
                leaf.is_steerable
                or leaf.tag in env.steerable
                or env.pdg.tag_roles.get(leaf.tag) == TagRole.INPUT
            )
            for node in establish_nodes
            for leaf in node.leaves()
        )
        if (
            scalar_coast
            and not requires_action
            and _route_judgment.route_has_no_dead_end(establish_nodes)
        ):
            # The instruction is not active yet, but its owned program stage
            # will establish itself without an external act. Keep the future
            # scalar boundary: scanning/coasting is the only useful operation.
            return boundary
        # A persistent program prerequisite still needs an action. Let the
        # ordinary writer trace expose that nearer transition; after it lands,
        # retracing will make this profile the immediate frontier.
        return None
    children = [boundary, *gate_nodes, *demand_nodes]
    target_atom = _trace_constraints._constraint_atom(constraint)
    return TraceNode(
        tag=constraint.tag,
        value=(target_atom.operand if target_atom is not None else constraint),
        provenance=provenance,
        relational=isinstance(constraint, (Cmp, AffineCmp)),
        predicate=target_atom,
        children=children,
    )


def trace_target(
    target: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    *,
    constraints: _trace_read.TraceReadConstraints | None = None,
) -> TraceNode:
    """Read an entire objective, retaining all conjunctive terminal demands."""
    if target.members:
        from pyrung.core.analysis.pilot.multitarget import input_preferences

        read = constraints or _trace_read.TraceReadConstraints()
        env = _env_from_constraints(read, snapshot, pdg, program, steerable, max_depth=15)
        env = replace(env, preferred_actions=input_preferences(target.members, program, steerable))
        children = _trace_expression(env, target.predicate, target.tag, _visited=set(), _depth=0)
        root = TraceNode(
            tag=target.tag,
            value=True,
            predicate=target.predicate,
            satisfied=target_reached(snapshot, target.tag, target.value, target.predicate),
            children=children,
            goal_group=True,
        )
        _reconcile_relational(root, snapshot)
        return root
    if target.predicate is not None:
        return trace_relational(
            target.predicate, snapshot, pdg, program, steerable, constraints=constraints
        )
    return trace_back(
        target.tag, target.value, snapshot, pdg, program, steerable, constraints=constraints
    )


def trace_relational(
    predicate: Atom,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    *,
    clear_only: frozenset[str] = frozenset(),
    opaque_loop: frozenset[str] = frozenset(),
    pipeline_internal_tags: frozenset[str] = frozenset(),
    route: _trace_read.TraceChoice | None = None,
    prior: _trace_read.DomainPrior | None = None,
    avoid_pred: Any = None,
    rejected_actions: frozenset[tuple[str, Any]] = frozenset(),
    max_depth: int = 15,
    harness: Any = None,
    execution_memory: Mapping[str, Any] | None = None,
    constraints: _trace_read.TraceReadConstraints | None = None,
) -> TraceNode:
    """Backward trace for a relational *target* predicate (``A op B``).

    Routes the target through the same atom branch as a relational prerequisite,
    so a target inequality gets the live-predicate node, the up-to-two reactive
    levers, and the converging/coast disposition for free.  Returns the
    relational node (or a coast leaf / dead-end) as the tree root; a satisfied
    predicate yields a ``satisfied`` leaf (the drive loop's early-exit owns it).
    """
    read = constraints or _trace_read.TraceReadConstraints(
        clear_only=clear_only,
        opaque_loop=opaque_loop,
        pipeline_internal_tags=pipeline_internal_tags,
        route=route,
        prior=prior,
        avoid_pred=avoid_pred,
        rejected_actions=rejected_actions,
        harness=harness,
        execution_memory=execution_memory,
    )
    env = _env_from_constraints(read, snapshot, pdg, program, steerable, max_depth=max_depth)
    nodes = _trace_expression(env, predicate, predicate.tag, _visited=set(), _depth=0)
    if nodes:
        root = nodes[0]
        _reconcile_relational(root, snapshot)
        return root
    return TraceNode(
        tag=predicate.tag,
        value=getattr(predicate, "operand", None),
        satisfied=_expr_satisfied(predicate, snapshot),
    )


def _reconcile_relational(root: TraceNode, snapshot: dict[str, Any]) -> None:
    """Prune relational levers subsumed by a sibling concrete demand.

    A guard inequality (``ModeSel >= 1``) whose tag is *also* driven to a
    concrete value elsewhere in the tree (``ModeSel == 2``, from a copy-source)
    is already satisfied by that value — steering it to its own boundary (1)
    would conflict with the value the other goal needs.  Mark such a frontier
    ``satisfied`` and drop its lever so only the concrete demand survives as a
    candidate.  This is the conjunction/subsumption reconciliation: pure
    cleanup, re-run every iteration, never manufacturing a steer.

    Concrete demands are non-lever steerable leaves; lever leaves (a relational
    node's own boundary picks) are excluded so two levers never subsume each
    other.  The predicate is evaluated against the snapshot overlaid with the
    candidate value, so tag-operand thresholds (``PV >= Lower``) resolve too.
    """
    nodes = list(root.iter_nodes())
    concrete: dict[str, set[Any]] = {}
    for n in nodes:
        if n.is_steerable and n.lever is None:
            concrete.setdefault(n.tag, set()).add(n.value)
    for n in nodes:
        if not n.relational or n.predicate is None or n.satisfied:
            continue
        vals = concrete.get(n.tag)
        if not vals:
            continue
        if any(_eval_expr_from_state(n.predicate, {**snapshot, n.tag: v}) is True for v in vals):
            n.satisfied = True
            n.children = []


def _scope_ref(rung_index: int, rung_node: Any) -> str:
    scope = rung_node.subroutine or rung_node.scope or "Main"
    if str(scope).lower() == "main":
        scope = "Main"
    return f"{scope}:R{rung_index}"


def _route_actions_rejected(nodes: list[TraceNode], env: _TraceEnv) -> bool:
    """Whether this alternative is the exact disproved singleton artifact.

    Compass owns and world-scopes the evidence. Trace consumes only exact
    singleton actions admitted by Orientation, and uses them as an ordering
    hint among otherwise viable unlocked alternatives. A multi-leaf branch is
    a joint artifact and remains live until that exact joint act is tested.
    """

    action: tuple[str, Any] | None = None
    for root in nodes:
        for node in root.iter_nodes():
            if not node.is_steerable:
                continue
            pair = (node.tag, node.value)
            if action is None:
                action = pair
            elif pair != action:
                # Multiple leaves describe a different, still-untested joint
                # artifact. Independent singleton failures cannot be composed
                # into its rejection.
                return False
    return action is not None and action in env.rejected_actions


@dataclass(frozen=True)
class _TraceAlternative(Generic[_TraceChoicePayload]):
    """Literal facts about one already-read trace alternative."""

    choice: _TraceChoicePayload
    rank: tuple[Any, ...]
    violates_avoid: bool
    has_no_dead_end: bool
    exact_action_rejected: bool


@dataclass(frozen=True)
class _TraceSelection(Generic[_TraceChoicePayload]):
    """The selected alternative or the exact blocked branch kept for diagnosis."""

    chosen: _TraceAlternative[_TraceChoicePayload] | None
    blocked_alternative: _TraceAlternative[_TraceChoicePayload] | None

    @property
    def retained(self) -> _TraceAlternative[_TraceChoicePayload] | None:
        """The classified alternative whose complete read the caller adopts."""

        return self.chosen or self.blocked_alternative


@dataclass(frozen=True)
class _WriterAttempt:
    """One fully-read writer subtree plus the visited state it produced."""

    children: tuple[TraceNode, ...]
    writer_rung: int
    writer_availability: _availability._WriterAvailability
    live_guard: bool
    crossing_exact: bool | None
    visited_after: frozenset[tuple[str, Any]]


@dataclass
class _WriterBuild:
    """Fresh mutable state used to read exactly one ranked writer."""

    node: TraceNode
    visited: set[tuple[str, Any]]

    @classmethod
    def fresh(
        cls,
        parent: TraceNode,
        visited: set[tuple[str, Any]],
    ) -> _WriterBuild:
        """Start one writer from the caller's untouched recursion state."""

        return cls(
            node=TraceNode(tag=parent.tag, value=parent.value),
            visited=set(visited),
        )

    def complete(self) -> _WriterAttempt:
        """Freeze this isolated build for classification and later adoption."""

        if self.node.writer_rung is None:
            raise ValueError("a writer attempt requires its selected rung")
        return _WriterAttempt(
            children=tuple(self.node.children),
            writer_rung=self.node.writer_rung,
            writer_availability=self.node.writer_availability,
            live_guard=self.node.live_guard,
            crossing_exact=self.node.crossing_exact,
            visited_after=frozenset(self.visited),
        )


def _apply_writer_attempt(
    node: TraceNode,
    visited: set[tuple[str, Any]],
    attempt: _WriterAttempt,
) -> None:
    """Apply the selected writer subtree to the shared trace node once."""

    node.children.extend(attempt.children)
    node.writer_rung = attempt.writer_rung
    node.writer_availability = attempt.writer_availability
    node.live_guard = attempt.live_guard
    node.crossing_exact = attempt.crossing_exact
    visited.clear()
    visited.update(attempt.visited_after)


def _trace_alternative(
    *,
    choice: _TraceChoicePayload,
    nodes: list[TraceNode],
    rank: tuple[Any, ...],
    env: _TraceEnv,
) -> _TraceAlternative[_TraceChoicePayload]:
    """Read the common selection facts for one caller-ranked alternative."""

    if env.preferred_actions:
        conflicts = sum(
            1
            for node in nodes
            for tag, value in node.ordered_actions()
            if any(
                tag == preferred_tag and not _values_match(value, preferred_value)
                for preferred_tag, preferred_value in env.preferred_actions
            )
        )
        rank = (conflicts, *rank)
    return _TraceAlternative(
        choice=choice,
        rank=rank,
        violates_avoid=(
            env.avoid_pred is not None
            and bool(nodes)
            and _route_judgment.route_forces(nodes, env.snapshot, env.avoid_pred)
        ),
        has_no_dead_end=_route_judgment.route_has_no_dead_end(nodes),
        exact_action_rejected=_route_actions_rejected(nodes, env),
    )


def _select_trace_alternative(
    alternatives: tuple[_TraceAlternative[_TraceChoicePayload], ...],
    *,
    replace_rejected_choice: bool = True,
) -> _TraceSelection[_TraceChoicePayload]:
    """Apply the precedence shared by unlocked local trace alternatives.

    Avoided alternatives cannot be selected. A rejected baseline may be
    replaced only by an untried alternative with no dead end when the caller
    permits that replacement. If no allowed choice remains, the exact blocked
    baseline stays named for diagnosis.

    Subroutine call sites are distinct program contexts, not alternate
    recipes: a caller whose exact action was rejected is recorded but never
    redirects selection to a different caller — it remains the honest
    frontier for higher-level recovery.
    """

    if not alternatives:
        return _TraceSelection(chosen=None, blocked_alternative=None)

    allowed = tuple(alternative for alternative in alternatives if not alternative.violates_avoid)
    if not allowed:
        return _TraceSelection(
            chosen=None,
            blocked_alternative=min(alternatives, key=lambda alternative: alternative.rank),
        )

    baseline = min(allowed, key=lambda alternative: alternative.rank)
    if not baseline.exact_action_rejected or not replace_rejected_choice:
        return _TraceSelection(chosen=baseline, blocked_alternative=None)

    replacements = tuple(
        alternative
        for alternative in allowed
        if not alternative.exact_action_rejected and alternative.has_no_dead_end
    )
    if replacements:
        return _TraceSelection(
            chosen=min(replacements, key=lambda alternative: alternative.rank),
            blocked_alternative=None,
        )
    return _TraceSelection(
        chosen=None,
        blocked_alternative=baseline,
    )


def _trace_expression(
    env: _TraceEnv,
    expr: Any,
    self_tag: str,
    *,
    provenance: tuple[str, ...] = (),
    _visited: set[tuple[str, Any]],
    _ancestry: tuple[tuple[str, Any], ...] = (),
    _codemands: tuple[tuple[str, Any], ...] = (),
    _relational_goal: Any = None,
    _depth: int,
) -> list[TraceNode]:
    """Walk an expression tree, returning trace children.

    And: trace all terms (all must be satisfied).  The And's own concrete atom
        demands become *co-demands* for each term's writer selection — a writer
        that satisfies one atom must not clobber a sibling atom's register
        (state-consistent ranking, ``_writer_clobbers_codemand``).
    Or: if any branch is already satisfied, skip. Otherwise pick the
        best unsatisfied branch (fewest non-steerable unsatisfied nodes),
        skipping any arm whose assignment forces ``env.avoid_pred``.
    Atom: convert to (tag, value) and recurse via _trace_back.
    """
    if isinstance(expr, And):
        # Concurrent sibling demands: the equality/bit atoms of this And must all
        # hold at the same fire scan, so they constrain each other's writers.
        and_demands: list[tuple[str, Any]] = []
        for term in expr.terms:
            if isinstance(term, Atom):
                pairs = _required_from_atom(term)
                if pairs:
                    and_demands.extend(pairs)
        codemands = (*_codemands, *and_demands)
        relational_goal = expr
        children: list[TraceNode] = []
        for term in expr.terms:
            children.extend(
                _trace_expression(
                    env,
                    term,
                    self_tag,
                    provenance=provenance,
                    _visited=_visited,
                    _ancestry=_ancestry,
                    _codemands=codemands,
                    _relational_goal=relational_goal,
                    _depth=_depth,
                )
            )
        return children

    if isinstance(expr, Or):
        # Any satisfied branch means the Or doesn't block — skip it.
        if _expr_satisfied(expr, env.snapshot):
            return []

        lock_key = (self_tag, _expr_route_key(expr))
        locked_index = env.or_locks.get(lock_key) if env.or_locks is not None else None
        if locked_index is not None and 0 <= locked_index < len(expr.terms):
            term = expr.terms[locked_index]
            if not (isinstance(term, Atom) and term.tag == self_tag):
                return _trace_expression(
                    env,
                    term,
                    self_tag,
                    provenance=provenance,
                    _visited=set(_visited),
                    _ancestry=_ancestry,
                    _codemands=_codemands,
                    _relational_goal=term,
                    _depth=_depth,
                )

        # Pick the cheapest unsatisfied branch. Skip self-referencing
        # branches — Or(rise(Input), SealIn) where SealIn is the tag
        # we're already tracing (the engineer knows the seal-in path
        # is circular and looks at the trigger instead).
        alternatives: list[_TraceAlternative[tuple[TraceNode, ...]]] = []
        for term in expr.terms:
            if isinstance(term, Atom) and term.tag == self_tag:
                continue
            candidate = _trace_expression(
                env,
                term,
                self_tag,
                provenance=provenance,
                _visited=set(_visited),
                _ancestry=_ancestry,
                _codemands=_codemands,
                _relational_goal=term,
                _depth=_depth,
            )
            if not candidate:
                return []
            structural_score = sum(
                1
                for c in candidate
                if (not c.satisfied and not c.is_steerable and not c.pipeline_internal)
            )
            alternatives.append(
                _trace_alternative(
                    choice=tuple(candidate),
                    nodes=candidate,
                    rank=(structural_score,),
                    env=env,
                )
            )

        selection = _select_trace_alternative(tuple(alternatives))
        if selection.chosen is not None:
            return list(selection.chosen.choice)
        if (
            selection.blocked_alternative is not None
            and selection.blocked_alternative.exact_action_rejected
            and not selection.blocked_alternative.violates_avoid
        ):
            # Keep the exact rejected frontier visible when there is no untried
            # branch without a dead end. Avoided arms remain excluded.
            return list(selection.blocked_alternative.choice)
        if selection.blocked_alternative is None:
            return []
        return []

    if isinstance(expr, Atom):
        if expr.unsupported is not None:
            raise _trace_read.UnsupportedConstruct("condition", expr.unsupported, provenance)
        target = _trace_constraints._atom_target(expr, env.snapshot)
        if target is None:
            if expr.form in ("lt", "le", "gt", "ge", "ne"):
                op = {
                    "lt": "<",
                    "le": "<=",
                    "gt": ">",
                    "ge": ">=",
                    "ne": "!=",
                }[expr.form]
                constraint = (
                    AffineCmp(
                        expr.tag,
                        op,
                        expr.operand,
                        scale=expr.operand_scale,
                        offset=expr.operand_offset,
                    )
                    if expr.operand_is_tag and (expr.operand_scale != 1 or expr.operand_offset != 0)
                    else Cmp(
                        expr.tag,
                        op,
                        expr.operand,
                        bound_is_tag=expr.operand_is_tag,
                    )
                )
                advance = _advance_frontier(
                    env,
                    constraint,
                    provenance,
                    depth=_depth,
                )
                if advance is not None:
                    return [advance]
                # A threshold (Acc > N) on a self-advancing accumulator (timer
                # or counter) is a coast leaf: wait for it to cross on its own.
                if _expr_satisfied(expr, env.snapshot):
                    return []
                crossing_branches = _trace_crossing_branches(
                    env,
                    expr,
                    provenance,
                    visited=_visited,
                    ancestry=_ancestry,
                    relational_goal=(_relational_goal if _relational_goal is not None else expr),
                    depth=_depth,
                )
                if not crossing_branches:
                    crossing_branches = _trace_frozen_crossing_branches(
                        env,
                        expr,
                        provenance,
                        visited=_visited,
                        ancestry=_ancestry,
                        relational_goal=(
                            _relational_goal if _relational_goal is not None else expr
                        ),
                        depth=_depth,
                    )
                if crossing_branches:
                    return [
                        TraceNode(
                            tag=expr.tag,
                            value=expr.operand,
                            relational=True,
                            predicate=expr,
                            provenance=provenance,
                            crossing_branches=crossing_branches,
                        )
                    ]
                # Carry the predicate live as a relational frontier (Stage A)
                # and surface up-to-two reactive levers (Stage B): steer the LHS
                # toward B, or steer the RHS toward A.  Both ride as children so
                # both surface as candidates; the ranker + try-verify-learn loop
                # picks one and switches if it was a no-op.  Distance counts the
                # predicate once (the relational node stops recursion), so the
                # levers do not double-count as separate goals.
                levers = _trace_constraints._inequality_levers(
                    expr, env.snapshot, env.steerable, env.pdg, env.prior, env.program
                )
                lever_children: list[TraceNode] = []
                for lever in levers:
                    child = _trace_back(
                        env,
                        lever.tag,
                        lever.value,
                        _visited=set(_visited),
                        _ancestry=_ancestry,
                        _preserve_predicate=(
                            _relational_goal if _relational_goal is not None else expr
                        ),
                        _depth=_depth + 1,
                    )
                    if child.is_steerable and not child.provenance:
                        child.provenance = provenance
                    child.lever = lever.label
                    child.heuristic = lever.heuristic
                    child.note = lever.note
                    lever_children.append(child)
                # Converging disposition (Stage B1): when the LHS advances on its
                # own and PILOT cannot steer it — a free input with no writers
                # (a harness-linked sensor that ramps under the held state) —
                # add a coast leaf so let-run can carry the frontier across the
                # boundary even when no lever is productive (e.g. Temp >= Limit
                # with Limit pinned: lowering the bar dead-ends, but Temp ramps
                # under the held Enable and crosses on its own).  Without this the
                # frontier looks like an opaque dead-end and the loop bails before
                # the coast.
                if lever_children:
                    return [
                        TraceNode(
                            tag=expr.tag,
                            value=expr.operand,
                            relational=True,
                            predicate=expr,
                            provenance=provenance,
                            children=lever_children,
                        )
                    ]
            return []
        tag, val = target
        # Rise/fall need a transition — if the tag is already at the
        # target value, the edge won't fire.  Mark it steerable so
        # PILOT knows to re-pulse it.
        if (
            expr.form in ("rise", "fall")
            and tag in env.steerable
            and _values_match(env.snapshot.get(tag), val)
        ):
            return [TraceNode(tag=tag, value=val, is_steerable=True, provenance=provenance)]
        child = _trace_back(
            env,
            tag,
            val,
            _visited=_visited,
            _ancestry=_ancestry,
            _codemands=_codemands,
            _depth=_depth + 1,
        )
        if child.is_steerable and not child.provenance:
            child.provenance = provenance
        return [child]

    raise _trace_read.UnsupportedConstruct("expression", expr, provenance)


def trace_back(
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
    route: _trace_read.TraceChoice | None = None,
    writer_locks: dict[tuple[str, Any], int] | None = None,
    or_locks: dict[tuple[str, str], int] | None = None,
    prior: _trace_read.DomainPrior | None = None,
    avoid_pred: Any = None,
    rejected_actions: frozenset[tuple[str, Any]] = frozenset(),
    max_depth: int = 15,
    harness: Any = None,
    execution_memory: Mapping[str, Any] | None = None,
    constraints: _trace_read.TraceReadConstraints | None = None,
    _visited: set[tuple[str, Any]] | None = None,
    _ancestry: tuple[tuple[str, Any], ...] = (),
    _depth: int = 0,
    _writer_rank_memo: _WriterRankMemo | None = None,
) -> TraceNode:
    """Recursive backward trace from ``(tag, value)``.

    Public entry point: bundles the invariant trace context (graph, steerable
    set, locks, domain prior, avoid predicate, ...) into a :class:`_TraceEnv`
    and delegates to :func:`_trace_back`, which threads that one value down the
    recursion instead of a dozen kwargs.  A ``TraceChoice`` resolves to its lock
    maps here, once.
    """
    read = constraints or _trace_read.TraceReadConstraints(
        clear_only=clear_only,
        opaque_loop=opaque_loop,
        pipeline_internal_tags=pipeline_internal_tags,
        route=route,
        prior=prior,
        avoid_pred=avoid_pred,
        rejected_actions=rejected_actions,
        harness=harness,
        execution_memory=execution_memory,
    )
    env = _env_from_constraints(
        read,
        snapshot,
        pdg,
        program,
        steerable,
        writer_locks=writer_locks,
        or_locks=or_locks,
        max_depth=max_depth,
        writer_rank_memo=_writer_rank_memo,
    )
    return _trace_back(env, tag, value, _visited=_visited, _ancestry=_ancestry, _depth=_depth)


def _trace_back(
    env: _TraceEnv,
    tag: str,
    value: Any,
    *,
    _visited: set[tuple[str, Any]] | None = None,
    _ancestry: tuple[tuple[str, Any], ...] = (),
    _codemands: tuple[tuple[str, Any], ...] = (),
    _preserve_predicate: Any = None,
    _depth: int = 0,
) -> TraceNode:
    """Backward-trace worker over a fixed :class:`_TraceEnv`.

    Returns a ``TraceNode`` tree.  Leaves are steerable inputs (actions),
    already-satisfied conditions, or cycle/depth terminations.  Uses
    ``(tag, value)`` visited keys so the same tag at different values
    (e.g. ``StateCurrent==1`` then ``StateCurrent==2``) can be traced
    independently.  ``_ancestry`` is the per-path chain of expanded
    ``(tag, value)`` nodes; it powers the feedback-loop guard below.
    """
    if _visited is None:
        _visited = set()

    vkey = _visit_key(tag, value)

    if _values_match(env.snapshot.get(tag), value):
        return TraceNode(tag=tag, value=value, satisfied=True)

    if vkey in _visited:
        return TraceNode(tag=tag, value=value)

    # ``max_depth`` is the fail-closed boundary for value walks that keep
    # manufacturing fresh visit keys.  A self-affine stepper with no reachable
    # base writer can otherwise invert forever (107 <- 105 <- 103 <- ...), so
    # the ordinary ``(tag, value)`` cycle guard never fires.  Returning an
    # unresolved leaf preserves the honest frontier; recursion exhaustion is
    # never a valid analysis result.
    if _depth >= env.max_depth:
        return TraceNode(tag=tag, value=value)

    # Feedback-loop guard: a jump-table state register (``opaque_loop``) that
    # already appears at another value along the ancestor path means we are
    # inverting the state-machine feedback cycle, not a finite prerequisite
    # chain. Stop and emit a dead-end leaf so Compass can learn the transition
    # from action observations.
    if tag in env.opaque_loop and tag not in env.steerable:
        # Key values via _visit_key so an unhashable expression value (a
        # relational sub-goal on a state register) is counted without crashing
        # the set membership; identical to raw values for the scalar case.
        prior_keys = {_visit_key(t, v) for (t, v) in _ancestry if t == tag}
        if _visit_key(tag, value) not in prior_keys and len(prior_keys) >= _SAME_TAG_VALUE_BUDGET:
            return TraceNode(tag=tag, value=value)

    _visited.add(vkey)

    if tag in env.steerable:
        return TraceNode(tag=tag, value=value, is_steerable=True)

    if tag in env.pipeline_internal_tags:
        return TraceNode(tag=tag, value=value, pipeline_internal=True)

    advance = _advance_frontier(
        env,
        Eq(tag, frozenset((value,))),
        (),
        depth=_depth,
    )
    if advance is not None:
        return advance

    # Counter/timer Done bit: reaching Done==True means driving the accumulator
    # to preset (a coast), not firing the writer rung once.  Surface the
    # accumulator frontier instead of the naive rung-condition walk — the counter
    # branch adds an advance driver; the timer branch owns only the already-running
    # case (enable satisfied, nothing left to hold).
    _child_ancestry = (*_ancestry, (tag, value))

    if env.pdg.tag_roles.get(tag) == TagRole.INPUT:
        return TraceNode(tag=tag, value=value)

    writers = env.pdg.writers_of.get(tag, frozenset())
    if not writers:
        return TraceNode(tag=tag, value=value)

    node = TraceNode(tag=tag, value=value)

    rank_key = (
        vkey,
        tuple(_visit_key(item_tag, item_value) for item_tag, item_value in _ancestry),
        tuple(_visit_key(item_tag, item_value) for item_tag, item_value in _codemands),
    )
    rank_receipt = env.writer_rank_memo.get(rank_key)
    if rank_receipt is None:
        writer_availability: dict[int, _availability._WriterAvailability] = {}
        writer_ranking: list[_WriterRank] = []
        writer_reverses: dict[int, ReverseResult] = {}
        ranked_writers = _rank_writers(
            writers,
            env.pdg,
            env.program,
            tag,
            value,
            env.snapshot,
            env.opaque_loop,
            env.clear_only,
            steerable=env.steerable,
            ancestry=_ancestry,
            codemands=_codemands,
            availability_out=writer_availability,
            ranking_out=writer_ranking,
            reverse_out=writer_reverses,
        )
        rank_receipt = _WriterRankReceipt(
            ranked=tuple(ranked_writers),
            availability=tuple(writer_availability.items()),
            ranking=tuple(writer_ranking),
            reverses=tuple(writer_reverses.items()),
        )
        env.writer_rank_memo[rank_key] = rank_receipt
    ranked_writers = list(rank_receipt.ranked)
    writer_availability = dict(rank_receipt.availability)
    writer_ranking = list(rank_receipt.ranking)
    writer_reverses = dict(rank_receipt.reverses)
    # Recording only: the full ranking (winner + losers) that chose this frontier's
    # writer, and the writers the loop below actively skips before settling.
    node.writer_ranking = tuple(writer_ranking)
    writer_skips: list[tuple[int, str]] = []
    locked_writer = env.writer_locks.get(vkey) if env.writer_locks is not None else None
    if locked_writer is not None and locked_writer in ranked_writers:
        ranked_writers = [locked_writer]

    writer_alternatives: list[_TraceAlternative[_WriterAttempt]] = []
    writer_selection: _TraceSelection[_WriterAttempt] | None = None

    for writer_order, ri in enumerate(ranked_writers):
        rung_node = env.pdg.rung_nodes[ri]
        ro = resolve_rung(env.program, rung_node)
        if ro is None:
            writer_skips.append((ri, "unresolved_rung"))
            continue

        wv = _written_value_for_tag(ro, tag)
        if not _can_produce(wv, value):
            writer_skips.append((ri, "cant_produce"))
            continue

        reverse_result = writer_reverses.get(ri)
        if reverse_result is None:
            reverse_result = _reverse_writer(ro, tag, value, env.snapshot, env.pdg)
        if normalize_reverse_result(reverse_result).contradiction:
            writer_skips.append((ri, "reverse_contradiction"))
            continue
        producer_target = eq_target(tag, value)
        producer_constraints = _producer_constraints(reverse_result, producer_target)
        producer_pins = _producer_pins(reverse_result, producer_target)
        # A forward affine relationship can still offer one useful *proposal*
        # when the sound reverse contract must fall through (floating rounding
        # aliases, modular multiplication, clamp rails).  Keep that candidate
        # out of ReverseResult: it is not a complete preimage and the drive
        # layer must verify it in the interpreted fork.  A self-copy candidate
        # equal to the target is only a hold, so it contributes no progress.
        affine_candidate: tuple[str, Any] | None = None
        if (
            normalize_reverse_result(reverse_result).fallthrough
            and not producer_constraints
            and isinstance(wv, Affine)
        ):
            candidate = _invert_affine(wv, value)
            if candidate is not None and not (wv.source == tag and _values_match(candidate, value)):
                affine_candidate = (wv.source, candidate)

        sp = ro.sp_tree()
        guard_expr = _sp_to_expr(sp) if sp is not None else None
        # OTE deactivation: tracing tag=False through out(tag) means the rung
        # must NOT fire — negate the expression.
        if guard_expr is not None and _values_match(value, False) and tag in rung_node.ote_writes:
            guard_expr = _negate(guard_expr)

        # Reduce the guard by the copy-source pin: reject the writer if the pin
        # violates a source-only guard atom (it can never emit this value), else
        # drop the atoms the pin already satisfies so a redundant ``src`` guard
        # (``UnitModeCmd != 0`` beside source ``== 2``) does not surface as a second
        # frontier fighting the source pin.
        if reverse_result.exact and guard_expr is not None:
            for pin_tag, pin_value in producer_pins.items():
                guard_expr = _availability._reduce_guard_by_pin(
                    guard_expr, pin_tag, pin_value, env.snapshot
                )
                if guard_expr is _availability._GUARD_CONTRADICTION:
                    writer_skips.append((ri, "guard_pin_contradiction"))
                    break
            if guard_expr is _availability._GUARD_CONTRADICTION:
                continue

        if guard_expr is not None:
            guard_expr = _availability._reduce_guard_by_fire_pins(
                guard_expr, ro, tag, value, env.snapshot, env.pdg, env.program
            )
            if guard_expr is _availability._GUARD_CONTRADICTION:
                writer_skips.append((ri, "guard_fire_pin_contradiction"))
                continue

        # Rejection arm (tide_tables.guard_verdict): a writer whose guard is
        # *provably unsatisfiable* over complete finite free-tag domains — under
        # the fire-time pins the writer itself forces to produce ``value`` — can
        # never fire to produce it.  Skip it exactly as a False ``_can_produce``
        # would, so a provably-dead writer never burns drive-loop trials.
        # Punt-biased and sound: ONLY a definite ``GUARD_DEAD`` rejects; ``SAT``
        # and ``PUNT`` retain the writer.
        guard_punted = False
        if guard_expr is not None:
            from pyrung.core.analysis.pilot.tide_tables import GUARD_DEAD, GUARD_PUNT

            verdict = _writer_guard_verdict(env, ri, tag, value, reverse_result, guard_expr)
            if verdict == GUARD_DEAD:
                writer_skips.append((ri, "guard_dead"))
                continue
            guard_punted = verdict == GUARD_PUNT

        # Every viable ranked writer is read in a fresh shell from the same
        # caller-owned visited state. Only selection adopts one completed
        # attempt into ``node`` and ``_visited`` below.
        writer_build = _WriterBuild.fresh(node, _visited)
        attempt_node = writer_build.node
        attempt_visited = writer_build.visited
        attempt_node.writer_rung = ri
        attempt_node.writer_availability = writer_availability.get(
            ri, _availability._WriterAvailability.UNKNOWN
        )
        normalized_reverse = normalize_reverse_result(reverse_result)
        attempt_node.crossing_exact = (
            None if normalized_reverse.fallthrough else normalized_reverse.exact
        )

        if guard_expr is not None:
            attempt_node.children.extend(
                _trace_expression(
                    env,
                    guard_expr,
                    tag,
                    provenance=(_scope_ref(ri, rung_node),),
                    _visited=attempt_visited,
                    _ancestry=_child_ancestry,
                    _depth=_depth,
                )
            )

        # Reaching this writer at all requires no upstream return_early() to have
        # fired — its negated guard is a prerequisite of the rung executing.
        for guard_expr in return_early_guard_exprs(env.program, rung_node):
            attempt_node.children.extend(
                _trace_expression(
                    env,
                    guard_expr,
                    tag,
                    provenance=(_scope_ref(ri, rung_node),),
                    _visited=attempt_visited,
                    _ancestry=_child_ancestry,
                    _depth=_depth,
                )
            )

        if rung_node.subroutine:
            call_gates: list[_CallGateTrace] = []
            for ci, cn in enumerate(env.pdg.rung_nodes):
                if rung_node.subroutine in cn.calls:
                    call_ro = resolve_rung(env.program, cn)
                    if call_ro is None:
                        continue
                    call_sp = call_ro.sp_tree()
                    if call_sp is None:
                        call_gates.append(_CallGateTrace(caller_index=ci, nodes=()))
                        continue
                    children = _trace_expression(
                        env,
                        _sp_to_expr(call_sp),
                        tag,
                        provenance=(_scope_ref(ci, cn),),
                        _visited=set(attempt_visited),
                        _ancestry=_child_ancestry,
                        _depth=_depth + 1,
                    )
                    call_gates.append(_CallGateTrace(caller_index=ci, nodes=tuple(children)))
            selected_call_gate = _select_call_gate(env, rung_node.subroutine, call_gates)
            if selected_call_gate is not None:
                attempt_node.children.extend(selected_call_gate.nodes)

        for constraint in producer_constraints:
            if isinstance(constraint, Eq):
                child = _trace_back(
                    env,
                    constraint.tag,
                    next(iter(constraint.values)),
                    _visited=attempt_visited,
                    _ancestry=_child_ancestry,
                    _depth=_depth + 1,
                )
                child.data_flow = "producer"
                attempt_node.children.append(child)
                continue
            atom = _trace_constraints._constraint_atom(constraint)
            if atom is None:
                continue
            children = _trace_expression(
                env,
                atom,
                tag,
                provenance=(_scope_ref(ri, rung_node),),
                _visited=attempt_visited,
                _ancestry=_child_ancestry,
                _depth=_depth + 1,
                _relational_goal=atom,
            )
            for child in children:
                child.data_flow = "producer"
            attempt_node.children.extend(children)

        if affine_candidate is not None:
            source_tag, source_value = affine_candidate
            child = _trace_back(
                env,
                source_tag,
                source_value,
                _visited=attempt_visited,
                _ancestry=_child_ancestry,
                _depth=_depth + 1,
            )
            child.data_flow = "producer"
            attempt_node.children.append(child)

        # Enablement gate decided by a constant-table predicate (PackML
        # state-enable / cmd-valid mask): the flag on this transition is a
        # dh[...] & dh[...] over the target state and a steerable index (mode),
        # whose snapshot value is stale w.r.t. the planned transition — so trace
        # would wrongly read the gate as satisfied.  Keys on the semantic shape
        # (fire-time pins derivable from the writer's data flow or its guard),
        # not the identity-copy silhouette: identity/converting copies, affine
        # calc transitions, and guard-pinned decodes all reach the tide tables,
        # which are asked which mode makes the gate hold under those pins.
        attempt_node.children.extend(
            _table_enablement_prereqs(
                env,
                ro,
                tag,
                value,
                reverse_result,
                _visited=attempt_visited,
                _ancestry=_child_ancestry,
                _depth=_depth,
            )
        )

        if isinstance(wv, Aggregate) and wv.operation == "sum" and not attempt_node.children:
            for child_node in _decompose_sum(
                env,
                wv,
                value,
                _visited=attempt_visited,
                _ancestry=_child_ancestry,
                _depth=_depth,
            ):
                attempt_node.children.append(child_node)

        # Indirect copy: block[pointer] → invert the lookup table.
        if not attempt_node.children:
            from pyrung.core.analysis.pilot.tide_tables import invert_indirect_copy

            inv = invert_indirect_copy(ro, tag, value, env.snapshot, env.pdg, env.program)
            if inv is not None:
                idx_tag, idx_vals = inv
                for iv in idx_vals:
                    child = _trace_back(
                        env,
                        idx_tag,
                        iv,
                        _visited=attempt_visited,
                        _ancestry=_child_ancestry,
                        _depth=_depth + 1,
                    )
                    child.data_flow = "lookup"
                    attempt_node.children.append(child)

        # Preserve: the writer above *establishes* the value; a retentive target
        # must also be kept from being clobbered by a competing writer.
        attempt_node.children.extend(
            _preserve_children(
                env,
                tag,
                value,
                ri,
                predicate=_preserve_predicate,
                _visited=attempt_visited,
                _ancestry=_child_ancestry,
                _depth=_depth,
            )
        )

        # Punt signal for skiff and option building: the tide tables could not
        # decide this writer's guard (a genuinely-live word / undecidable term)
        # and the backward walk ended at a dead end. Skiff may probe the marked
        # frontier; options keeps it open as unreadable work.
        attempt_node.live_guard = guard_punted and not _route_judgment.route_has_no_dead_end(
            [attempt_node]
        )

        attempt = writer_build.complete()
        alternative = _trace_alternative(
            choice=attempt,
            nodes=[attempt_node],
            rank=(writer_order,),
            env=env,
        )
        writer_alternatives.append(alternative)

        writer_selection = _select_trace_alternative(tuple(writer_alternatives))

        if alternative.violates_avoid:
            writer_skips.append((ri, "avoid_shadowed"))
            continue

        # Root/user locks remain binding. Orientation owns their exhaustion and
        # possible revocation rather than an inner writer redirect.
        if locked_writer is not None:
            break

        if alternative.exact_action_rejected:
            writer_skips.append((ri, "empirically_rejected"))
            continue

        if writer_selection.chosen is None:
            # A prior rejected branch remains the honest frontier until another
            # writer supplies an untried alternative with no dead end.
            writer_skips.append((ri, "alternative_has_dead_end"))
            continue

        break

    node.writer_skips = tuple(writer_skips)
    if writer_selection is None:
        writer_selection = _select_trace_alternative(tuple(writer_alternatives))
    selected_writer = writer_selection.retained
    if selected_writer is not None:
        _apply_writer_attempt(node, _visited, selected_writer.choice)

    if _depth == 0:
        # Reconcile relational guards against concrete demands once, on the full
        # tree (a relational guard satisfied by a sibling's needed value should
        # not steer to its own boundary and conflict).
        _reconcile_relational(node, env.snapshot)
    return node


def _preserve_children(
    env: _TraceEnv,
    tag: str,
    value: Any,
    establish_ri: int,
    *,
    predicate: Any = None,
    _visited: set[tuple[str, Any]],
    _ancestry: tuple[tuple[str, Any], ...],
    _depth: int,
) -> list[TraceNode]:
    """Preserve prerequisites: keep a retentive ``tag=value`` from being clobbered.

    The writer walk *establishes* the value (finds the writer that produces it).
    For a **retentive** target — a latch/SET coil or a copy/calc into a held
    register — the value also has to *persist*: any competing writer that
    provably drives the tag to a different value overwrites it on a later scan
    unless its guard is suppressed.  An engineer reading ``latch(Running)``
    immediately asks "where's the reset, and is it active?"; this surfaces that
    half of the latch's boolean semantics, which the establish walk omits.

    For each clobbering writer, emit the **negation** of its guard as ordinary
    prerequisite children (``reset gated ~StopBtn`` -> ``StopBtn=True``).  They
    ride the same candidate / widening / hold pipeline as the establish leaves,
    and ``_trace_back`` marks the already-healthy ones ``satisfied`` so they drop
    out.  De Morgan turns a compound reset guard into an ``Or`` of suppression
    options, which ``_trace_expression`` resolves like any route choice.

    For an exact target, a competing writer whose written value *could* be the
    target (``_can_produce`` True — affine / aggregate / unknown) is not
    suppressed. For a relational target, preservation is deliberately narrower:
    only a writer whose guard is active now and whose concrete output falsifies
    the predicate is an antagonist. The interpreted fork verifies the proposal
    and the next scan replans, so trace need not suppress every inactive state
    transition merely because it produces a different exact witness.
    """
    establish_node = env.pdg.rung_nodes[establish_ri]
    # Only retentive targets need preserving — an OTE coil is recomputed every
    # scan from its own condition, so there is no held value to clobber.
    if tag in establish_node.ote_writes:
        return []

    children: list[TraceNode] = []
    seen_guards: set[str] = set()
    establish_ro = resolve_rung(env.program, establish_node)
    if establish_ro is not None:
        establish_sp = establish_ro.sp_tree()
        if establish_sp is not None:
            # The establish walk already traced this guard. A competing writer
            # may have its exact negation (``Mode`` / ``~Mode``), making the
            # suppression prerequisite identical to the establish prerequisite.
            # Do not trace the same demand twice: the shared visited set would
            # turn the duplicate into a childless pseudo-frontier.
            seen_guards.add(_expr_route_key(_sp_to_expr(establish_sp)))
    for ri in sorted(env.pdg.writers_of.get(tag, frozenset())):
        if ri == establish_ri:
            continue
        rung_node = env.pdg.rung_nodes[ri]
        ro = resolve_rung(env.program, rung_node)
        if ro is None:
            continue
        sp = ro.sp_tree()
        if sp is None:
            continue
        guard = _sp_to_expr(sp)
        if _eval_expr_from_state(guard, env.snapshot) is True:
            from pyrung.core.analysis.activation import read_activation

            spent = read_activation(ro, tag, env.execution_memory).needs_rearm
        else:
            spent = False
        if spent:
            # Keeping the guard conductive keeps the instruction spent. If a
            # later bearing changes that condition, the committed scan updates
            # executor memory and the next ordinary trace read sees it rearmed.
            continue
        if predicate is None:
            # An exact clobber provably drives the tag away from the value.
            if _can_produce(_written_value_for_tag(ro, tag), value):
                continue
        else:
            # A range witness is only a means to the relational goal. Surface
            # the active writer that currently defeats that goal; do not turn
            # every other state-machine transition into an exact-value hold.
            if _eval_expr_from_state(guard, env.snapshot) is not True:
                continue
            produced = _concrete_written_value(_written_value_for_tag(ro, tag), env.snapshot)
            if produced is _UNRESOLVED:
                continue
            prospective = {**env.snapshot, tag: produced}
            if _eval_expr_from_state(predicate, prospective) is not False:
                continue
        suppress = _negate(guard)
        key = _expr_route_key(suppress)
        if key in seen_guards:
            continue
        seen_guards.add(key)
        children.extend(
            _trace_expression(
                env,
                suppress,
                tag,
                provenance=(_scope_ref(ri, rung_node),),
                _visited=_visited,
                _ancestry=_ancestry,
                _depth=_depth,
            )
        )
    return children


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _decompose_sum(
    env: _TraceEnv,
    wv: Aggregate,
    value: Any,
    *,
    _visited: Any,
    _ancestry: Any,
    _depth: int,
) -> list[TraceNode]:
    """Decompose a sum-aggregate writer into per-element children.

    For ``value != 0``: the sum is non-zero because at least one element
    is non-zero.  Find contributing elements, trace each one.
    For ``value == 0``: every element must be zero.  Trace each non-zero
    element as a prerequisite to clear.
    """
    if _values_match(value, 0):
        element_tags = [t for t in wv.tags if not _values_match(env.snapshot.get(t), 0)]
        target_value: Any = 0
    elif isinstance(value, (int, float)) and value != 0:
        element_tags = [t for t in wv.tags if not _values_match(env.snapshot.get(t), 0)]
        if not element_tags:
            return []
        target_value = None
    else:
        return []

    children: list[TraceNode] = []
    for elem_tag in element_tags:
        elem_value = env.snapshot.get(elem_tag, 0) if target_value is None else target_value
        child = _trace_back(
            env,
            elem_tag,
            elem_value,
            _visited=_visited,
            _ancestry=_ancestry,
            _depth=_depth + 1,
        )
        child.data_flow = "aggregate"
        children.append(child)
    return children


_FORM_TO_CMP_OP = {"eq": "==", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}


def _atom_comparison(atom: Any) -> tuple[str, str, Any] | None:
    """``(tag, op, literal_bound)`` for a comparison atom against a constant."""
    form = getattr(atom, "form", None)
    op = _FORM_TO_CMP_OP.get(form) if isinstance(form, str) else None
    if op is None:
        return None
    operand = getattr(atom, "operand", None)
    if not isinstance(operand, (int, float)) or isinstance(operand, bool):
        return None
    return (atom.tag, op, operand)


def _condition_required_values(expr: Any) -> list[tuple[str, Any]]:
    """``(tag, value)`` conjuncts a condition requires (``And`` of xic/xio/eq)."""
    from pyrung.core.analysis.simplified import And, Atom
    from pyrung.core.analysis.sp_values import _required_from_atom

    out: list[tuple[str, Any]] = []

    def visit(e: Any) -> None:
        if isinstance(e, Atom):
            pairs = _required_from_atom(e)
            if pairs:
                out.extend(pairs)
        elif isinstance(e, And):
            for t in e.terms:
                visit(t)

    visit(expr)
    return out


def _flag_gate_comparisons(
    env: _TraceEnv, flag_tag: str, flag_val: Any
) -> list[tuple[str, str, Any]]:
    """Comparison atoms gating a writer that sets *flag_tag* to *flag_val*."""
    from pyrung.core.analysis.simplified import And, Atom, Or

    out: list[tuple[str, str, Any]] = []
    for ri in sorted(env.pdg.writers_of.get(flag_tag, frozenset())):
        ro = resolve_rung(env.program, env.pdg.rung_nodes[ri])
        if ro is None:
            continue
        if not _can_produce(_written_value_for_tag(ro, flag_tag), flag_val):
            continue
        sp = ro.sp_tree()
        if sp is None:
            continue

        def visit(e: Any) -> None:
            if isinstance(e, Atom):
                cmp = _atom_comparison(e)
                if cmp is not None:
                    out.append(cmp)
            elif isinstance(e, (And, Or)):
                for t in e.terms:
                    visit(t)

        visit(_sp_to_expr(sp))
    return out


def _transition_fire_pins(
    env: _TraceEnv,
    tag: str,
    value: Any,
    reverse_result: ReverseResult,
) -> dict[str, Any]:
    """Data-flow pins a transition writer imposes the scan it produces *value*.

    The semantic key for the tide-tables trigger: an enablement gate recomputed
    each scan from constant-table lookups is indexed by the transition's own
    pins, so evaluating it needs the *fire-time* source values, not the
    snapshot's.  Soundly derivable in three writer shapes, none guessed:

    - a registered writer reverse — copy, aligned block-copy, or affine calc —
      returns singleton ``Eq`` constraints for the producer values forced on its
      firing scan;
    - a **non-affine calc** — ``calc(A * B, tag)``, ``calc(A & mask, tag)``,
      ``calc((A << 2) | B, tag)`` — that the crossing can't invert symbolically,
      solved by enumerate-and-evaluate over the sources' *complete* finite
      domains (:func:`~pyrung.core.analysis.pilot.tide_tables.solve_calc_preimage`),
      pinning only the FORCED source values (those shared by every satisfying
      assignment).

    A decode transition (literal write gated on ``src == v``) carries its pin in
    its *guard*, which the caller layers on separately.  Empty dict when no
    data-flow pin is derivable — never a fabricated binding.
    """
    pins = _producer_pins(reverse_result, eq_target(tag, value))
    if pins:
        return pins
    # Non-affine calc decode: no symbolic inverse, so solve the expression over
    # the sources' complete finite domains and pin only the forced values.
    from pyrung.core.analysis.pilot.tide_tables import solve_calc_preimage

    domains = env.prior.nd_domains if env.prior is not None else None
    pins = solve_calc_preimage(tag, value, env.snapshot, env.pdg, env.program, domains=domains)
    return pins or {}


def _writer_guard_verdict(
    env: _TraceEnv,
    ri: int,
    tag: str,
    value: Any,
    reverse_result: ReverseResult,
    guard_expr: Any,
) -> str:
    """Tide-tables verdict for a candidate writer's guard under its own fire pins.

    Fixes the pins the writer itself forces to produce ``value``
    (:func:`_transition_fire_pins` — the
    inverted copy/affine source, never a borrowed pin) and enumerates the
    remaining guard operands over the ``DomainPrior``'s ``nd_domains`` (the
    prover-derived complete domains; a Bool resolves to ``(False, True)``, a
    missing domain punts inside the tide tables). Returns one of
    ``GUARD_DEAD``/``GUARD_SAT``/``GUARD_PUNT``.

    Memoized on ``(rung id, fire-pins, guard route key)``: the verdict is a pure
    function of those plus the trace-invariant snapshot/domains, so one enumeration
    per distinct writer/pin/guard suffices for the whole ``trace_back`` recursion.

    ``guard_verdict`` owns the complete-domain requirement before returning
    ``GUARD_DEAD``. This adapter owns only the writer's fire pins and trace-local
    memoization.
    """
    from pyrung.core.analysis.pilot.tide_tables import guard_verdict

    pins = _transition_fire_pins(env, tag, value, reverse_result)
    key = (ri, tuple(sorted(pins.items(), key=lambda kv: kv[0])), _expr_route_key(guard_expr))
    cached = env.guard_memo.get(key)
    if cached is not None:
        return cached

    nd_domains = env.prior.nd_domains if env.prior is not None else None
    verdict = guard_verdict(
        guard_expr,
        fixed=pins,
        snapshot=env.snapshot,
        pdg=env.pdg,
        program=env.program,
        domains=nd_domains,
    )
    env.guard_memo[key] = verdict
    return verdict


def _select_table_enablement_value(
    env: _TraceEnv,
    arms: list[TraceNode],
) -> _TraceSelection[TraceNode]:
    """Choose one value proved to satisfy a constant-table enablement gate."""

    alternatives: list[_TraceAlternative[TraceNode]] = []
    for arm in arms:
        nodes = [arm]
        has_no_dead_end = _route_judgment.route_has_no_dead_end(nodes)
        alternatives.append(
            _trace_alternative(
                choice=arm,
                nodes=nodes,
                rank=(
                    0 if has_no_dead_end else 1,
                    *_route_judgment.trace_score(nodes, env.pdg),
                ),
                env=env,
            )
        )
    return _select_trace_alternative(tuple(alternatives))


def _table_enablement_prereqs(
    env: _TraceEnv,
    ro: Any,
    tag: str,
    value: Any,
    reverse_result: ReverseResult,
    *,
    _visited: set[tuple[str, Any]],
    _ancestry: tuple[tuple[str, Any], ...],
    _depth: int,
) -> list[TraceNode]:
    """Prerequisites for an enablement flag decided by a constant-table predicate.

    A PackML transition (``copy(StateReq, StateCur)``, an affine
    ``calc(StateReq + k, StateCur)``, or a decode ``copy(10, StateCur)`` gated on
    ``StateReq == 10``) is gated by ``isStateEnbl_Yes==1``, whose own writer is
    gated by ``stateMask[StateReq] & disabledMask[Mode] == 0``.  That predicate
    register is recomputed from ``StateReq`` every scan, so its snapshot value is
    stale w.r.t. the planned transition and trace would otherwise read the gate
    as satisfied.  The trigger is *semantic*, not idiom-shaped: whenever the
    transition's fire-time pins are soundly derivable
    (:func:`_transition_fire_pins` — the inverted data-flow source and/or the
    guard's own required conjuncts), consult the tide tables with those pins
    fixed and surface the steerable index — the mode — that makes the gate hold,
    as an ``Or`` whose cheapest arm trace drives.  No derivable pin ⇒ punt
    (never enumerate an unpinned predicate — that would surface prerequisites
    unconditioned on the actual transition).
    """
    sp = ro.sp_tree()
    if sp is None:
        return []
    pins = _transition_fire_pins(env, tag, value, reverse_result)

    from pyrung.core.analysis.pilot.tide_tables import solve_table_predicate

    domains = env.prior.nd_domains if env.prior is not None else None
    required = _condition_required_values(_sp_to_expr(sp))
    prereqs: list[TraceNode] = []
    seen_idx: set[str] = set()
    for flag_tag, flag_val in required:
        if flag_tag == tag or flag_tag in pins:
            continue
        # Fire-time pins for this flag's gate: the data-flow pin plus the
        # guard's *other* required conjuncts (a decode transition carries its
        # source pin in its own guard, not in data flow — those conjuncts hold
        # the scan the writer fires, so they are sound pins for the recomputed
        # predicate).  Nothing pinned means the planned transition constrains
        # nothing the tide tables could key on, so this path punts.
        fixed = dict(pins)
        for other_tag, other_val in required:
            if other_tag not in (tag, flag_tag) and other_tag not in fixed:
                fixed[other_tag] = other_val
        if not fixed:
            continue
        for pred_tag, op, bound in _flag_gate_comparisons(env, flag_tag, flag_val):
            sol = solve_table_predicate(
                pred_tag,
                bound,
                op,
                env.snapshot,
                env.pdg,
                env.program,
                fixed=fixed,
                domains=domains,
            )
            if sol is None:
                continue
            for idx_tag, idx_vals in sol.per_tag.items():
                if idx_tag in seen_idx or not idx_vals:
                    continue
                if any(_values_match(env.snapshot.get(idx_tag), v) for v in idx_vals):
                    continue  # already in a satisfying mode — gate genuinely holds
                seen_idx.add(idx_tag)
                arms = [
                    _trace_back(
                        env,
                        idx_tag,
                        v,
                        _visited=set(_visited),
                        _ancestry=_ancestry,
                        _depth=_depth + 1,
                    )
                    for v in idx_vals
                ]
                selection = _select_table_enablement_value(env, arms)
                selected = selection.chosen or selection.blocked_alternative
                if selected is None:
                    continue
                # A blocked value is retained only as the exact frontier. The
                # containing writer and action gates still own exclusion.
                selected.choice.data_flow = "enable"
                prereqs.append(selected.choice)
    return prereqs
