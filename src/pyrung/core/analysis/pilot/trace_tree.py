"""The immutable-ish result tree produced by backward trace.

This module owns trace result records and structural views. It does not read a
program to choose writers or routes.
"""

from __future__ import annotations

import typing
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from itertools import product
from typing import Any

from pyrung.core.analysis.pilot.availability import _WriterAvailability
from pyrung.core.analysis.pilot.effects import EffectPathStep
from pyrung.core.analysis.pilot.navigation_contracts import CrossingFidelity
from pyrung.core.analysis.pilot.overlay import OperationReceipt
from pyrung.core.analysis.pilot.writer_selection import _WriterRank
from pyrung.core.analysis.prove.expr import _eval_expr_from_state
from pyrung.core.analysis.simplified import Atom
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.crossing import AffineCmp, Cmp, Constraint, Eq

_FORM_TO_OP = {"gt": ">", "ge": ">=", "lt": "<", "le": "<="}
_OP_TO_FORM = {op: form for form, op in _FORM_TO_OP.items()}


@dataclass(frozen=True)
class TraceAction:
    """A steerable action discovered by backward trace, with source context."""

    tag: str
    value: Any
    provenance: tuple[str, ...] = ()
    downstream_reach: int | None = None
    # The nearest self-advancing frontier this action serves.  A prerequisite
    # becomes a PilotRung only while this predicate remains unresolved; None
    # means the trace supplied no honest rung lifetime, so the action stays a
    # patch/pulse candidate.
    until: Any = None
    # The complete owner receipt behind ``until``. Keeping this distinct from
    # the flattened action lets correction replay and option ordering preserve
    # an operation that has already started instead of toggling its lever.
    operation: OperationReceipt | None = None
    # Conditions from the selected writer path that make this action applicable.
    # A rung-managed physical input can reuse them as an honest local guard when
    # trace discovers that the input must be asserted again in a new context.
    guard_atoms: tuple[Any, ...] = ()
    # True when the current operation needs repeated release/assert edges.
    pulse: bool = False
    # True when this action sits under an unsatisfied ``data_flow=="enable"`` node —
    # it *establishes* a table-enablement precondition (e.g. the mode that unblocks
    # a mask-disabled state) whose effect is a settled cross-register recompute.  It
    # cannot fire in the same scan as the command it gates, so options.py makes
    # it the sole bearing (stage 0) and defers the gated commands.
    establish: bool = False
    # Exact nearest program-owned transition this action serves. Keep the
    # selected trace branch intact and carry its observable handoff boundary.
    operation_boundary: tuple[str, Any] | None = None
    # Stage-3 heuristic boundary proposal on a steerable free word: the value is
    # an example that satisfies a relation, not a sound derivation.  ``note`` is
    # the relational report threaded to ``PlanStep.notes``.
    heuristic: bool = False
    note: str = ""
    # Worst writer availability on this leaf's path from the target (worst-wins,
    # matching the And-rule): the chain producing this leaf's need is only as
    # reachable-from-here as its least-available writer.  options.py demotes
    # (never drops) leaves serving UNKNOWN / UNAVAILABLE chains below AVAILABLE /
    # AFTER_PREREQ ones so command-leaf sprawl on a cyclic state machine sinks
    # the counterfactual commands.  Availability orders, it never rejects.
    availability: _WriterAvailability = _WriterAvailability.AVAILABLE_NOW
    # Exact selected writer nodes from the target down to this action.  This is
    # a read receipt, not a second route: candidate policy can inspect necessary
    # co-effects of the already-selected program path without retracing it.
    writer_path: tuple[int, ...] = ()
    # Typed selected-path receipt used to mint the final producer-to-consumer
    # expectation while candidate selection still knows the exact route.
    effect_path: tuple[EffectPathStep, ...] = ()

    @property
    def pair(self) -> tuple[str, Any]:
        return (self.tag, self.value)


@dataclass(frozen=True)
class TraceCrossingBranch:
    """One conjunctive predecessor branch retained through navigation.

    ``actions`` is the complete atomic overlay for this DNF branch. Reverse or
    proposal fidelity belongs here rather than on the generic Boolean ``Atom``
    used by unrelated simplified-expression consumers.
    """

    actions: tuple[TraceAction, ...]
    fidelity: CrossingFidelity
    effect_path: tuple[EffectPathStep, ...] = ()

    @property
    def pairs(self) -> tuple[tuple[str, Any], ...]:
        return tuple(action.pair for action in self.actions)

    @property
    def constraints(self) -> tuple[Constraint, ...]:
        return self.fidelity.constraints

    @property
    def reason(self) -> str:
        return self.fidelity.reason

    @property
    def verify_required(self) -> bool:
        return self.fidelity.verify_required

    @property
    def exact(self) -> bool | None:
        return self.fidelity.exact

    @property
    def proposed(self) -> bool:
        return self.fidelity.proposed

@dataclass
class TraceNode:
    """One node in the backward-trace tree.

    Leaves are steerable inputs (actions to take), satisfied conditions
    (nothing to do), or depth/cycle terminations.
    """

    tag: str
    value: Any
    satisfied: bool = False
    is_steerable: bool = False
    writer_rung: int | None = None
    children: list[TraceNode] = field(default_factory=list)
    data_flow: str | None = None  # "copy" | "calc" | None
    provenance: tuple[str, ...] = ()
    pipeline_internal: bool = False
    advance: Any = None
    # Public result requested from the advance owner, distinct from the
    # internal crossing in ``advance.until`` (a timer's Done=True versus
    # Acc>=preset). Coast heads to and verifies this result; the profile keeps
    # the internal crossing as folding metadata.
    owner_boundary: tuple[str, Any] | None = None
    owner_condition: Any = None
    # A non-linear profile's boundary is itself the next stage heading. Linear
    # profiles keep the existing earned-work/terminal-coast path.
    stage_boundary: bool = False
    # A real linear profile keeps prerequisite holds until its parent producer
    # settles. Synthetic and non-linear boundaries use ``AdvanceStep.until``.
    linear_boundary: bool = False
    # Edge-gated accumulator driver: a steerable leaf that must *toggle* each scan
    # (not hold steady) to keep firing the rise/fall that advances the counter.
    pulse: bool = False
    # Relational frontier: a live predicate (``A op B``) carried past the trace
    # boundary instead of collapsed to ``A == k``.  ``predicate`` is the source
    # ``Atom`` (evaluable via ``_eval_expr_from_state``).  The single-lever
    # resolution rides as the child subtree so steering is unchanged; distance
    # counts the predicate once and does not recurse into the lever (means, not
    # a separate goal).
    relational: bool = False
    predicate: Any = None
    lever: str | None = None  # "left"/"right" — which operand this subtree steers
    # Lever provenance (set alongside ``lever``): a stage-3 heuristic boundary
    # proposal and its relational report (see ``trace_constraints._Lever``).
    heuristic: bool = False
    note: str = ""
    # Set when the writer chosen for this (tag, value) frontier is gated by a
    # guard the tide tables could only *punt* on (a genuinely-live word or an
    # undecidable term) — not proved satisfiable, not proved dead.  Skiff uses
    # this signal to select isolated-probe frontiers; option building keeps the
    # marked node open as unreadable work until probing supplies evidence.
    live_guard: bool = False
    # Availability of the writer chosen for this (tag, value) frontier, as
    # classified by ``_rank_writers`` against the live snapshot.  AVAILABLE_NOW
    # for nodes with no chosen writer (steerable leaves, satisfied, dead-ends) so
    # it is neutral in the worst-wins path fold ``_collect_ordered`` performs.
    writer_availability: _WriterAvailability = _WriterAvailability.AVAILABLE_NOW
    # Recording only (no drive-loop behavior keys on it): the FULL ``_rank_writers``
    # ranking that chose ``writer_rung`` — winner first, then the losers, each with
    # its ``(availability, bucket, clobber)`` sort dimensions.  ``None`` on nodes
    # with no writer walk.  Surfaces the "why this writer, not that one" decision
    # the ranker otherwise discards.
    writer_ranking: tuple[_WriterRank, ...] | None = None
    # Recording only: ranked writers the ``_trace_back`` loop actively SKIPPED
    # before settling on ``writer_rung`` — ``(ri, reason)`` where reason names the
    # silent gate that dropped it (``cant_produce`` / ``guard_pin_contradiction`` /
    # ``guard_fire_pin_contradiction`` / ``guard_dead`` / ``avoid_shadowed`` /
    # ``empirically_rejected`` / ``alternative_has_dead_end`` /
    # ``unresolved_rung``). Empty when the top-ranked writer was taken directly.
    writer_skips: tuple[tuple[int, str], ...] = ()
    # Fidelity of the ReverseResult used for this selected writer conclusion.
    # ``None`` for non-crossing nodes and forward-only verified candidates.
    crossing_exact: bool | None = None
    # Alternative proposal branches are intentionally not ordinary children:
    # each inner conjunction must survive into navigation as one atomic act.
    crossing_branches: tuple[TraceCrossingBranch, ...] = ()

    def iter_nodes(
        self,
        *,
        order: typing.Literal["breadth_first", "depth_first"] = "breadth_first",
    ) -> Iterator[TraceNode]:
        """Yield this trace in one of its two stable structural orders.

        Breadth-first is the package default; callers that need left-to-right
        preorder request ``depth_first`` explicitly.
        """

        if order == "breadth_first":
            pending: list[TraceNode] = [self]
            index = 0
            while index < len(pending):
                node = pending[index]
                pending.extend(node.children)
                index += 1
                yield node
            return
        if order == "depth_first":
            pending = [typing.cast(TraceNode, self)]
            while pending:
                node = pending.pop()
                pending.extend(reversed(node.children))
                yield node
            return
        raise ValueError(f"unknown trace traversal order: {order!r}")

    @property
    def is_interior_frontier(self) -> bool:
        """Whether this is an unresolved structural requirement with children."""

        return (
            bool(self.children or self.crossing_branches)
            and not self.satisfied
            and not self.is_steerable
            and not self.pipeline_internal
        )

    def leaves(self) -> list[TraceNode]:
        return [node for node in self.iter_nodes(order="depth_first") if not node.children]

    def steerable_leaves(self) -> list[tuple[str, Any]]:
        return [(n.tag, n.value) for n in self.leaves() if n.is_steerable]

    def ordered_actions(self) -> list[tuple[str, Any]]:
        """Depth-first action list with same-tag prerequisites first.

        Returns deduplicated ``(tag, value)`` pairs, deepest first — the
        natural temporal ordering for state-machine programs.
        """
        return [action.pair for action in self.ordered_action_details()]

    def ordered_action_details(self) -> list[TraceAction]:
        """Depth-first action list with provenance for diagnostics/scoring."""
        actions: list[TraceAction] = []
        seen: set[tuple[str, Any]] = set()
        self._collect_ordered(actions, seen)
        return actions

    def ordered_crossing_branches(self) -> tuple[TraceCrossingBranch, ...]:
        """Crossing DNF composed with safe outer ``And`` siblings.

        A crossing nested under an ordinary prerequisite is executable only
        when that prerequisite is already satisfied or is itself one direct
        steerable leaf. Two crossing siblings are Cartesian-composed. Anything
        requiring writer traversal, staging, a hold, or a pulse is declined
        until navigation has an explicit grouped-staging contract.
        """

        has_crossing, branches = _compose_crossing_subtree(self)
        return branches if has_crossing else ()

    def _collect_ordered(
        self,
        out: list[TraceAction],
        seen: set[tuple[str, Any]],
        under_enable: bool = False,
        path_availability: _WriterAvailability = _WriterAvailability.AVAILABLE_NOW,
        until: Any = None,
        operation: OperationReceipt | None = None,
        guard_atoms: tuple[Any, ...] = (),
        operation_boundary: tuple[str, Any] | None = None,
        writer_path: tuple[int, ...] = (),
        effect_path: tuple[EffectPathStep, ...] = (),
    ) -> None:
        # Stage ordering remains structural: an unsatisfied enable ancestor puts
        # its leaves in stage 0. The exact operation boundary is broader than
        # that category: every chosen writer node names the local output its
        # descendant lever is meant to establish. A deeper writer owns the nearer
        # receipt, and retracing hands off to the next operation.
        child_enable = under_enable or (self.data_flow == "enable" and not self.satisfied)
        child_operation_boundary = (
            (self.tag, self.value)
            if not self.satisfied
            and not self.is_steerable
            and (self.writer_rung is not None or self.data_flow == "enable")
            else operation_boundary
        )
        # Worst-wins: a leaf is only as available as the least-available writer on
        # the path from the target down to it (the And-rule — every writer in the
        # chain must fire).  Neutral (AVAILABLE_NOW) for nodes with no writer.
        node_availability = max(path_availability, self.writer_availability)
        child_writer_path = (
            (*writer_path, self.writer_rung) if self.writer_rung is not None else writer_path
        )
        local_requirements = tuple(
            (child.tag, child.value)
            for child in self.children
            if not child.heuristic and not child.relational
        )
        child_effect_path = (
            (
                *effect_path,
                EffectPathStep(
                    self.writer_rung,
                    self.tag,
                    self.value,
                    local_requirements,
                ),
            )
            if self.writer_rung is not None
            else effect_path
        )
        # A self-advancing child is the clock/frontier that sibling steering
        # keeps alive.  The nearest such parent owns the action's lifetime.
        child_until = until
        child_operation = operation
        unsatisfied_children = [child for child in self.children if not child.satisfied]
        if (
            child_until is None
            and self.writer_rung is not None
            and len(unsatisfied_children) > 1
            and any(
                any(node.advance is not None and not node.satisfied for node in child.leaves())
                for child in unsatisfied_children
            )
        ):
            # A writer with concurrent prerequisites owns their shared lifetime.
            # Nested timers/profiles provide headings, but completing one cannot
            # release a sibling needed for the outer result (rendezvous).
            child_until = self.predicate or Atom(self.tag, "eq", self.value)
        advance_child = next(
            (child for child in self.children if child.advance is not None and not child.satisfied),
            None,
        )
        if advance_child is not None and child_until is None:
            child_until = (
                self.predicate or Atom(self.tag, "eq", self.value)
                if advance_child.linear_boundary
                else advance_child.advance.until
            )
        if advance_child is not None and child_operation is None:
            child_operation = OperationReceipt(
                until=child_until or advance_child.advance.until,
                progress=getattr(advance_child.advance, "progress", None),
            )
        for child in self.children:
            child_guard_atoms = list(guard_atoms)
            for sibling in self.children:
                if sibling is child:
                    continue
                atom = sibling.predicate or Atom(sibling.tag, "eq", sibling.value)
                if atom not in child_guard_atoms:
                    child_guard_atoms.append(atom)
            child._collect_ordered(
                out,
                seen,
                child_enable,
                node_availability,
                child_until,
                child_operation,
                tuple(child_guard_atoms),
                child_operation_boundary,
                child_writer_path,
                child_effect_path,
            )
        if self.is_steerable:
            key = (self.tag, self.value)
            detail = TraceAction(
                tag=self.tag,
                value=self.value,
                provenance=self.provenance,
                until=until,
                operation=operation,
                guard_atoms=guard_atoms,
                pulse=self.pulse,
                establish=under_enable,
                operation_boundary=operation_boundary,
                heuristic=self.heuristic,
                note=self.note,
                availability=node_availability,
                writer_path=writer_path,
                effect_path=effect_path,
            )
            if key in seen:
                index = next(i for i, existing in enumerate(out) if existing.pair == key)
                existing = out[index]
                if existing.effect_path != detail.effect_path:
                    # The same physical lever can serve distinct selected
                    # producer paths. They are alternative expectations, not
                    # one spliced conjunctive path.
                    out.append(detail)
                    return
                out[index] = replace(
                    existing,
                    until=existing.until if existing.until is not None else detail.until,
                    operation=(existing.operation or detail.operation),
                    guard_atoms=tuple(dict.fromkeys((*existing.guard_atoms, *detail.guard_atoms))),
                    pulse=existing.pulse or detail.pulse,
                    establish=existing.establish or detail.establish,
                    operation_boundary=(existing.operation_boundary or detail.operation_boundary),
                    heuristic=existing.heuristic or detail.heuristic,
                    note=existing.note or detail.note,
                    availability=max(existing.availability, detail.availability),
                    writer_path=tuple(dict.fromkeys((*existing.writer_path, *detail.writer_path))),
                    effect_path=(existing.effect_path or detail.effect_path),
                )
            else:
                seen.add(key)
                out.append(detail)

    def pivot_tags(self) -> set[str]:
        """Tags in the trace tree that are gate conditions — the pivots.

        These are the tags PILOT should monitor for progress/regression:
        non-leaf, non-steerable nodes that have children (meaning the
        trace walked through them as intermediate conditions).
        """
        return {
            node.tag
            for node in self.iter_nodes()
            if node.is_interior_frontier and not node.relational
        }

    def unsatisfied_count(self) -> int:
        """Number of *distinct* unsatisfied, non-steerable conditions.

        This is the "distance to target" — fewer = closer; an action that
        increases it moved further from the goal.  Deduplicated by
        ``(tag, value)`` so a register that recurs across many branches (the
        same need reached by several paths) counts once.  Without this the
        count tracks tree *size*, which the cyclic state machine inflates
        (~2x on the burner), drowning the Layer 4 trend signal.
        """
        return len(self.unsatisfied_conditions())

    def unsatisfied_conditions(self) -> set[tuple[str, Any]]:
        """Distinct unresolved, non-steerable conditions in this trace."""

        seen: set[tuple[str, Any]] = set()
        self._collect_unsatisfied(seen)
        return seen

    def _collect_unsatisfied(self, seen: set[tuple[str, Any]]) -> None:
        if self.relational:
            # A relational frontier is one logical unmet goal — count it once;
            # its lever child(ren) are alternatives (means), not separate goals,
            # so do not recurse.  ``satisfied`` is set by reconciliation when a
            # sibling concrete demand already covers the predicate (a guard
            # whose value comes from elsewhere); such a frontier is not a goal.
            # The advance boundary is a receipt for its parent owned result,
            # not a second logical goal.
            if not self.satisfied and self.advance is None:
                seen.add(self._relational_key())
            return
        if self.is_interior_frontier:
            seen.add(_visit_key(self.tag, self.value))
        for child in self.children:
            child._collect_unsatisfied(seen)

    def _relational_key(self) -> tuple[str, Any]:
        """Dedup key for a relational frontier: tag + (form, operand)."""
        p = self.predicate
        return (self.tag, (getattr(p, "form", None), getattr(p, "operand", self.value)))
def _trace_node_constraint(node: TraceNode) -> Constraint | None:
    """Concrete condition represented by one ordinary sibling trace node."""

    predicate = node.predicate
    if isinstance(predicate, Atom):
        if predicate.form in ("xic", "rise", "truthy"):
            return Eq(predicate.tag, frozenset((True,)))
        if predicate.form in ("xio", "fall"):
            return Eq(predicate.tag, frozenset((False,)))
        if predicate.form == "eq" and not predicate.operand_is_tag:
            return Eq(predicate.tag, frozenset((predicate.operand,)))
        op = _FORM_TO_OP.get(predicate.form)
        if op is not None:
            if predicate.operand_is_tag and (
                predicate.operand_scale != 1 or predicate.operand_offset != 0
            ):
                return AffineCmp(
                    predicate.tag,
                    op,
                    predicate.operand,
                    scale=predicate.operand_scale,
                    offset=predicate.operand_offset,
                )
            return Cmp(
                predicate.tag,
                op,
                predicate.operand,
                bound_is_tag=predicate.operand_is_tag,
            )
    if node.value is not None and not isinstance(node.value, Atom):
        return Eq(node.tag, frozenset((node.value,)))
    return None


def _empty_crossing_branch(
    *,
    constraints: tuple[Constraint, ...] = (),
    actions: tuple[TraceAction, ...] = (),
) -> TraceCrossingBranch:
    """Neutral element used while composing an outer conjunction."""

    return TraceCrossingBranch(
        actions=actions,
        fidelity=CrossingFidelity(
            constraints=constraints,
            reason="",
            verify_required=False,
            exact=True,
            proposed=False,
        ),
    )


def _merge_crossing_branches(
    left: TraceCrossingBranch,
    right: TraceCrossingBranch,
) -> TraceCrossingBranch | None:
    """Conjoin two branch receipts, rejecting contradictory action overlays."""

    by_tag: dict[str, TraceAction] = {}
    for action in (*left.actions, *right.actions):
        existing = by_tag.get(action.tag)
        if existing is not None and not _values_match(existing.value, action.value):
            return None
        by_tag.setdefault(action.tag, action)
    actions = tuple(sorted(by_tag.values(), key=lambda action: (action.tag, repr(action.value))))
    exact_values = (left.exact, right.exact)
    exact = None if None in exact_values else bool(left.exact and right.exact)
    return TraceCrossingBranch(
        actions=actions,
        fidelity=CrossingFidelity(
            constraints=tuple(dict.fromkeys((*left.constraints, *right.constraints))),
            reason="; ".join(dict.fromkeys(part for part in (left.reason, right.reason) if part)),
            verify_required=left.verify_required or right.verify_required,
            exact=exact,
            proposed=left.proposed or right.proposed,
        ),
        effect_path=left.effect_path or right.effect_path,
    )


def _crossing_at_node(node: TraceNode, branch: TraceCrossingBranch) -> TraceCrossingBranch:
    if node.writer_rung is None:
        return branch
    step = EffectPathStep(
        node.writer_rung,
        node.tag,
        node.value,
        tuple(
            (child.tag, child.value)
            for child in node.children
            if not child.heuristic and not child.relational
        ),
    )
    if branch.effect_path and branch.effect_path[0] == step:
        return branch
    return replace(branch, effect_path=(step, *branch.effect_path))


def _compose_crossing_subtree(
    node: TraceNode,
    *,
    under_lifetime: bool = False,
) -> tuple[bool, tuple[TraceCrossingBranch, ...]]:
    """Return ``(has_crossing, atomic conjunction alternatives)`` for *node*."""

    if node.crossing_branches:
        return True, tuple(_crossing_at_node(node, branch) for branch in node.crossing_branches)
    if node.satisfied:
        constraint = _trace_node_constraint(node)
        return False, (
            _empty_crossing_branch(constraints=((constraint,) if constraint is not None else ())),
        )
    if node.data_flow == "enable":
        # This node owns a prior stage. Its leaves cannot be folded into the
        # same physical overlay as a crossing branch until navigation has a
        # grouped staging contract.
        return False, ()
    if node.is_steerable:
        if node.children or node.pulse or under_lifetime:
            return False, ()
        constraint = _trace_node_constraint(node)
        return False, (
            _empty_crossing_branch(
                constraints=((constraint,) if constraint is not None else ()),
                actions=(
                    TraceAction(
                        node.tag,
                        node.value,
                        provenance=node.provenance,
                        heuristic=node.heuristic,
                        note=node.note,
                    ),
                ),
            ),
        )
    if node.advance is not None:
        # The crossing actions remain direct; execution's ordinary settle/coast
        # machinery owns this instruction boundary after the atomic patch.
        return False, (_empty_crossing_branch(),)
    if not node.children:
        return False, ()

    has_crossing = False
    combined: tuple[TraceCrossingBranch, ...] = (_empty_crossing_branch(),)
    for child in node.children:
        child_has_crossing, child_branches = _compose_crossing_subtree(
            child,
            under_lifetime=under_lifetime or node.advance is not None,
        )
        if not child_branches:
            return False, ()
        has_crossing = has_crossing or child_has_crossing
        merged: list[TraceCrossingBranch] = []
        for left, right in product(combined, child_branches):
            receipt = _merge_crossing_branches(left, right)
            if receipt is not None and receipt not in merged:
                merged.append(receipt)
        if not merged:
            return False, ()
        combined = tuple(merged)
    return has_crossing, tuple(_crossing_at_node(node, branch) for branch in combined)


def frontier_pairs(tree: TraceNode, snap: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    """The tree's outstanding non-steerable frontier as ``(tag, value)`` pairs.

    The registers the target still *needs*: unsatisfied, non-steerable,
    non-pipeline-internal interior nodes whose snapshot value differs from the
    needed one (``Heat_CurStep = 3``-shaped progress registers, never steerable
    buttons).  The single definition shared by the iteration payload's
    ``still_need`` display and the checkpoint ``frontier`` capture that feeds
    ``hold_defeats_needed`` — the two must not drift.

    Notion **#1** of three "what's still needed" — the whole-tree aggregate residual,
    read AFTER writer selection.  Distinct from #2 ``_projected_guard_frontier``
    (per-writer, projected fire-time) and #3 ``_expr_availability`` (per-writer, live
    tier); see the agreement gate
    ``tests/core/analysis/test_pilot_needed_vocabulary.py``.
    """
    pairs: list[tuple[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for n in tree.iter_nodes():
        if n.is_interior_frontier:
            if n.relational and n.predicate is not None:
                # The need is the *relation* — emit the Atom, never the operand
                # tag-name posing as an equality value (a string a downstream
                # ``_values_match`` would garbage-compare against numbers).
                # Consumers treat pairs as opaque membership except
                # ``hold_defeats_needed``, which isinstance-skips Atoms.
                if _eval_expr_from_state(n.predicate, snap) is not True:
                    key = (n.tag, repr(n.predicate))
                    if key not in seen:
                        seen.add(key)
                        pairs.append((n.tag, n.predicate))
                continue
            cur = snap.get(n.tag)
            if cur != n.value:
                key = (n.tag, repr(n.value))
                if key not in seen:
                    seen.add(key)
                    pairs.append((n.tag, n.value))
    return tuple(pairs)


def _visit_key(tag: str, value: Any) -> tuple[str, Any]:
    if isinstance(value, (bool, int, float, str, type(None))):
        return (tag, value)
    return (tag, id(value))
