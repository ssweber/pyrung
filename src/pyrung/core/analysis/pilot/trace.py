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

import typing
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from pyrung.core.analysis import steerable as _steerable
from pyrung.core.analysis.pdg import TagRole, resolve_rung
from pyrung.core.analysis.pilot.advance import demand_holds
from pyrung.core.analysis.pilot.availability import (
    _GUARD_CONTRADICTION,
    _equality_gated_coil,
    _reduce_guard_by_fire_pins,
    _reduce_guard_by_pin,
    _writer_availability,
    _WriterAvailability,
)
from pyrung.core.analysis.pilot.overlay import OperationReceipt
from pyrung.core.analysis.pilot.static_expressions import (
    _atom_text,
    _heuristic_inequality_target,
    _resolve_inequality_target,
    single_calc_source,
)
from pyrung.core.analysis.prove.expr import _eval_expr_from_state
from pyrung.core.analysis.return_guards import _return_early_guard_exprs
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
    _expr_tag_names,
    _invert_affine,
    _required_from_atom,
    _values_match,
    _writer_for_tag,
    _writer_projection,
    _written_value_for_tag,
)
from pyrung.core.crossing import (
    REVERSE_FALLTHROUGH,
    Affine,
    Aggregate,
    Cmp,
    Constraint,
    CrossingContext,
    Eq,
    Literal,
    ReverseResult,
    eq_target,
)
from pyrung.core.instruction.advance import constraint_holds

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph

_TraceChoicePayload = TypeVar("_TraceChoicePayload")


class UnsupportedConstruct(Exception):
    """Trace encountered a program construct for which it has no read rule.

    Raised at read time and caught at exactly one drive boundary in
    ``pilot.py``; ``recording.py`` renders the caret/source diagnostic.  Test
    mode propagates the exception; drive mode degrades to a named terminal
    result instead of probing a construct the reader did not understand.
    """

    def __init__(
        self,
        construct_kind: str,
        unsupported: Any,
        provenance: tuple[str, ...] = (),
    ) -> None:
        self.construct_kind = construct_kind
        self.unsupported = unsupported
        self.provenance = provenance
        self.source_file = getattr(unsupported, "source_file", None)
        self.source_line = getattr(unsupported, "source_line", None)
        name = type(unsupported).__name__
        context = f" at {provenance[-1]}" if provenance else ""
        super().__init__(f"unsupported {construct_kind} {name}{context}")


# The availability-layer names imported above are re-exported *by that import* —
# external importers (``options.py``, ``tide_tables.py``, the pilot tests
# that reach for these ``_``-prefixed names directly) keep importing them from
# ``trace``.  The recursion core below calls them by their bare names.


@dataclass(frozen=True)
class DomainPrior:
    """Prover-derived domain prior for resolving inequality atoms.

    ``nd_domains`` maps a free/steerable input to its value domain
    (``_ExploreContext.nondeterministic_dims``); ``stateful_domains`` maps a
    program-owned tag to the values its writers can produce
    (``_ExploreContext.stateful_dims``); and ``func_deps`` is the affine
    projection map ``{tag: (source, scale, offset)}`` for derived scratch
    (``_ExploreContext.functional_dep_projections``).  These feed
    :func:`_resolve_inequality_target` so an inequality (``PV >= Lower``,
    ``ModeSel >= 1``) resolves to a *reachable* satisfying value instead of a
    blind arithmetic boundary.  ``None`` everywhere reproduces the pre-domain
    snapshot-boundary behavior — the prior is a completeness aid, never
    correctness-bearing (the interpreted fork verifies every plan).
    """

    nd_domains: dict[str, tuple[Any, ...]] | None = None
    stateful_domains: dict[str, tuple[Any, ...]] | None = None
    func_deps: dict[str, tuple[str, int, Any]] | None = None


@dataclass(frozen=True)
class _TraceEnv:
    """Invariant context threaded through one backward trace.

    Everything here is constant for the whole trace — only ``tag``/``value`` (or
    ``expr``), ``provenance``, ``_visited``, ``_ancestry`` and ``_depth`` change
    between recursive calls. ``avoid_pred`` excludes alternatives that force
    the avoided condition (``None`` for an unconstrained trace).

    The world-describing subset — ``snapshot`` / ``pdg`` / ``program`` /
    ``steerable`` / ``opaque_loop`` / ``prior`` — is the **read-side seam**: this
    env structurally satisfies :class:`~pyrung.core.analysis.pilot.types.WalkContext`,
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
    prior: DomainPrior | None = None
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
    advance_index: Any = None
    # Per-trace memo for the writer-guard rejection arm (:func:`_writer_guard_verdict`).
    # Pure over the trace-invariant env (frozen snapshot + constant domains), so a
    # verdict is deterministic in ``(rung id, fire-pins, guard route key)`` and can
    # be cached for the whole recursion.  Fresh dict per :func:`_env_for` call.
    guard_memo: dict[Any, str] = field(default_factory=dict)
    # One coherent call-site route per subroutine for this trace.  A subroutine
    # writer can be reached repeatedly through different ancestry/visited
    # contexts; choosing its caller independently at every occurrence unions
    # mutually alternative triggers into one action set (for example normal
    # ModeChangeRequest plus SimulateFirstScan).  The first ranked caller is the
    # route; subsequent occurrences reuse it, matching root writer/OR locks.
    caller_locks: dict[str, int] = field(default_factory=dict)


def _env_for(
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    *,
    clear_only: frozenset[str] = frozenset(),
    opaque_loop: frozenset[str] = frozenset(),
    pipeline_internal_tags: frozenset[str] = frozenset(),
    route: TraceChoice | None = None,
    writer_locks: dict[tuple[str, Any], int] | None = None,
    or_locks: dict[tuple[str, str], int] | None = None,
    prior: DomainPrior | None = None,
    avoid_pred: Any = None,
    rejected_actions: frozenset[tuple[str, Any]] = frozenset(),
    max_depth: int = 15,
    harness: Any = None,
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
        advance_index=build_advance_index(program, harness),
    )


@dataclass(frozen=True)
class TraceChoice:
    """One enumerated route through a multi-writer / OR-over-coils Bool trace.

    Internal: ``enumerate_trace_choices`` produces these so ``_prepare_route``
    can pick a deterministic default and record the rest as pivots on
    :class:`~pyrung.core.analysis.graph.RouteTaken`. ``route_condition`` is the
    concrete ``(tag, value)`` that distinguishes the route and can be named by
    ``avoid=`` to exclude it.
    """

    id: str
    label: str
    route: tuple[str, ...]
    writer_locks: tuple[tuple[str, Any, int], ...] = ()
    or_locks: tuple[tuple[str, str, int], ...] = ()
    route_condition: tuple[str, Any] | None = None

    def __str__(self) -> str:
        detail = " -> ".join(_compact_route(self.route))
        return f"route={self.id}: {self.label}" + (f" ({detail})" if detail else "")

    def writer_lock_map(self) -> dict[tuple[str, Any], int]:
        return {(tag, value): rung for tag, value, rung in self.writer_locks}

    def or_lock_map(self) -> dict[tuple[str, str], int]:
        return {(tag, key): index for tag, key, index in self.or_locks}


@dataclass(frozen=True)
class TraceReadConstraints:
    """The complete caller-owned constraint set for one backward trace read."""

    clear_only: frozenset[str] = frozenset()
    opaque_loop: frozenset[str] = frozenset()
    pipeline_internal_tags: frozenset[str] = frozenset()
    route: TraceChoice | None = None
    prior: DomainPrior | None = None
    avoid_pred: Any = None
    rejected_actions: frozenset[tuple[str, Any]] = frozenset()
    harness: Any = None

    @classmethod
    def from_context(
        cls,
        ctx: Any,
        work: Any,
        *,
        route: TraceChoice | None,
        avoid_pred: Any,
        rejected_actions: frozenset[tuple[str, Any]] = frozenset(),
    ) -> TraceReadConstraints:
        """Read the invariant trace constraints from an explicit pilot context."""

        return cls(
            clear_only=ctx.clear_only,
            opaque_loop=ctx.opaque_loop,
            pipeline_internal_tags=ctx.pipeline_internal_tags,
            route=route,
            prior=ctx.domain_prior,
            avoid_pred=avoid_pred,
            rejected_actions=rejected_actions,
            harness=getattr(work, "_harness", None),
        )

    def env(
        self,
        snapshot: dict[str, Any],
        pdg: ProgramGraph,
        program: Any,
        steerable: frozenset[str],
        *,
        writer_locks: dict[tuple[str, Any], int] | None = None,
        or_locks: dict[tuple[str, str], int] | None = None,
        max_depth: int = 15,
    ) -> _TraceEnv:
        """Lower this read receipt to Trace's recursive environment."""

        return _env_for(
            snapshot,
            pdg,
            program,
            steerable,
            clear_only=self.clear_only,
            opaque_loop=self.opaque_loop,
            pipeline_internal_tags=self.pipeline_internal_tags,
            route=self.route,
            writer_locks=writer_locks,
            or_locks=or_locks,
            prior=self.prior,
            avoid_pred=self.avoid_pred,
            rejected_actions=self.rejected_actions,
            max_depth=max_depth,
            harness=self.harness,
        )


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
    # Exact nearest program-owned transition this action serves. Backward trace
    # used to retain the lever but discard the output that made it useful;
    # verification then depended on a later frontier or ambient settling. Keep
    # the selected trace branch intact and carry its observable handoff boundary.
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

    @property
    def pair(self) -> tuple[str, Any]:
        return (self.tag, self.value)


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


def _expr_route_key(expr: Any) -> str:
    return repr(expr)


# ---------------------------------------------------------------------------
# TraceNode — the backward-trace tree
# ---------------------------------------------------------------------------


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
    # proposal and its relational report (see ``_Lever`` / ``_lever_note``).
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
            bool(self.children)
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
            )
            if key in seen:
                index = next(i for i, existing in enumerate(out) if existing.pair == key)
                existing = out[index]
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
# so Layer 6 owns the transition by observation.
_SAME_TAG_VALUE_BUDGET = 1


def _atom_target(
    atom: Atom,
    snapshot: dict[str, Any] | None = None,
) -> tuple[str, Any] | None:
    """Convert an Atom to the ``(tag, value)`` needed to satisfy it.

    ``rise``/``fall`` need the tag at the transition target value.
    ``truthy`` needs a truthy value — use ``True`` as proxy.
    """
    form = atom.form
    if form == "xic":
        return (atom.tag, True)
    if form == "xio":
        return (atom.tag, False)
    if form == "eq":
        if atom.operand_is_tag:
            if snapshot is None or atom.operand not in snapshot:
                return None
            return (atom.tag, snapshot[atom.operand])
        return (atom.tag, atom.operand)
    if form == "rise":
        return (atom.tag, True)
    if form == "fall":
        return (atom.tag, False)
    if form == "truthy":
        return (atom.tag, True)
    if form in {"ne", "lt", "le", "gt", "ge"}:
        return None
    return None


@dataclass(frozen=True)
class _Lever:
    """One actionable lever for an inequality atom, with provenance.

    ``heuristic`` marks a stage-3 boundary proposal (no sound derivation —
    the value is an example, the relation is the requirement); ``note`` is the
    human-readable relational report threaded to ``PlanStep.notes``.
    """

    label: str  # "left" | "right" — which operand this lever steers
    tag: str
    value: Any
    heuristic: bool = False
    note: str = ""


def _sole_write_instr(tag: str, pdg: ProgramGraph, program: Any) -> Any:
    """The sole instruction writing *tag*, or ``None`` (multi/zero writer or
    unresolved rung) — the structural reader's narrow entry."""
    writers = pdg.writers_of.get(tag, frozenset())
    if len(writers) != 1:
        return None
    ro = resolve_rung(program, pdg.rung_nodes[next(iter(writers))])
    if ro is None:
        return None
    for instr in ro._instructions:
        if getattr(getattr(instr, "dest", None), "name", None) == tag:
            return instr
    return None


def _reverse_writer(
    ro: Any,
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    prior: DomainPrior | None = None,
) -> ReverseResult:
    """Reverse the exact instruction selected inside a writer rung.

    ``pdg.writers_of`` owns writer selection (including subroutine and branch
    nodes); this helper only resolves the instruction inside that already-chosen
    rung and asks its registered crossing for the producer-side receipt.
    """
    instr = _writer_for_tag(ro, tag)
    if instr is None:
        return REVERSE_FALLTHROUGH
    from pyrung.core.analysis import crossings

    return crossings.reverse(
        instr,
        ro,
        eq_target(tag, value),
        CrossingContext(
            snapshot=snapshot,
            tags_by_name=pdg.tags,
            nd_domains=prior.nd_domains if prior is not None else None,
        ),
    )


def _producer_constraints(
    result: ReverseResult,
    target: Constraint,
) -> tuple[Constraint, ...]:
    """The deterministic producer requirements carried by a reverse receipt.

    Trace can currently consume one conjunctive branch of scalar ``Eq``/``Cmp``
    requirements. Other algebra shapes and disjunctions remain with their
    established specialized consumers; fallthrough never fabricates a need.
    A constraint identical to the target is a hold/self-copy, not progress.
    """
    if result.fallthrough or len(result.branches) != 1:
        return ()
    (branch,) = result.branches
    requirements: list[Constraint] = []
    for constraint in branch:
        if not isinstance(constraint, (Eq, Cmp)):
            return ()
        if constraint == target:
            continue
        if isinstance(constraint, Eq) and len(constraint.values) != 1:
            return ()
        requirements.append(constraint)
    return tuple(requirements)


def _producer_pins(result: ReverseResult, target: Constraint) -> dict[str, Any]:
    """Singleton equality pins from one deterministic producer receipt."""
    pins: dict[str, Any] = {}
    for constraint in _producer_constraints(result, target):
        if isinstance(constraint, Eq):
            pins[constraint.tag] = next(iter(constraint.values))
    return pins


#: simplified comparison form <-> Crossings ``Cmp`` operator symbol.
_FORM_TO_OP = {"gt": ">", "ge": ">=", "lt": "<", "le": "<="}
_OP_TO_FORM = {op: form for form, op in _FORM_TO_OP.items()}


def _rewrite_internal_compare(
    atom: Atom,
    steerable: frozenset[str],
    pdg: ProgramGraph,
    program: Any,
    snapshot: dict[str, Any],
    *,
    _depth: int = 0,
) -> list[Atom]:
    """Rewrite an inequality on an internal copy/calc register onto input-level
    atoms by reversing through its writer via the Crossings registry.

    The prover *dissolves* transparent pass-throughs (``copy(Temp, TempCopy)``,
    ``calc(Sensor + 10, Adjusted)``, ``calc(A + B, Sum)``) — back-propagating the
    boundary into the source domains and dropping the intermediate — so an
    inequality guard on the intermediate (``TempCopy > 50``, ``Sum > 8``) has no
    domain and would dead-end.  Reverse the guard through the writer instead
    (``crossings.reverse`` on a ``Cmp`` target):

    - single-source affine (copy / ``scale*src + offset``): one rewritten atom on
      ``src`` with the threshold shifted (form flipped on a negative scale).
      Recurses, so copy/calc chains collapse to the steerable source.
    - two-tag ``A ± B`` against a threshold: the calc crossing freezes each
      partner at its *snapshot* value and returns one branch per operand
      (``A op bound-B_now`` ∨ ``B op bound-A_now``); each becomes an alternative
      atom whose lever re-points against the live partner next scan.  Subsumes the
      old subtraction-at-zero special case (it is the ``bound == 0`` instance).

    Returns ``[atom]`` unchanged when the tag is steerable or the registry falls
    through — honest: the caller dead-ends, it never fabricates a lever.  The
    per-instruction inversion lives in ``core/analysis/crossings/``; this is the
    consumer that drives it and recurses for multi-hop chains.
    """
    if _depth > 6 or atom.tag in steerable:
        return [atom]
    op = _FORM_TO_OP.get(atom.form)
    if op is None:
        return [atom]  # not an inequality form
    instr = _sole_write_instr(atom.tag, pdg, program)
    if instr is None:
        return [atom]

    from pyrung.core.analysis import crossings
    from pyrung.core.crossing import Cmp, CrossingContext

    target = Cmp(atom.tag, op, atom.operand, bound_is_tag=atom.operand_is_tag)
    result = crossings.reverse(instr, None, target, CrossingContext(snapshot=snapshot))
    if result.fallthrough or not result.branches:
        return [atom]

    rewritten: list[Atom] = []
    for branch in result.branches:
        cmps = [c for c in branch if isinstance(c, Cmp)]
        if not cmps:
            return [atom]  # no inequality atom (Eq / unsat) -> stay honest
        if len(cmps) > 1 and len(cmps) != len(branch):
            # A conjunctive branch mixed with a non-Cmp atom (an Eq we cannot
            # represent as a lever) -> stay honest rather than drop the conjunct.
            return [atom]
        # A single Cmp is the affine/two-tag case unchanged; a branch of two or
        # more Cmps is a conjunction (both source atoms must hold), so surface
        # every conjunct as its own rewritten lever.
        for c in cmps:
            form = _OP_TO_FORM.get(c.op)
            if form is None:
                return [atom]
            rewritten.extend(
                _rewrite_internal_compare(
                    Atom(
                        tag=c.tag,
                        form=form,
                        operand=c.bound,
                        operand_is_tag=c.bound_is_tag,
                    ),
                    steerable,
                    pdg,
                    program,
                    snapshot,
                    _depth=_depth + 1,
                )
            )
    return rewritten or [atom]


def _lever_note(req: Atom, orig: Atom, tag: str, value: Any, marker: str = "") -> str:
    """The relational report for one lever — the requirement, with the proposed
    value as an *example* (``held Band < -100.0 to satisfy PV < Lower (e.g.,
    Band = -100.000001; heuristic …)``)."""
    req_txt = _atom_text(req)
    orig_txt = _atom_text(orig)
    body = req_txt if req._key() == orig._key() else f"{req_txt} to satisfy {orig_txt}"
    note = f"held {body} (e.g., {tag} = {value!r}"
    if marker:
        note += f"; {marker}"
    return note + ")"


def _inequality_levers(
    atom: Atom,
    snapshot: dict[str, Any],
    steerable: frozenset[str],
    pdg: ProgramGraph,
    prior: DomainPrior | None,
    program: Any = None,
) -> list[_Lever]:
    """Actionable levers for ``A op B``, as :class:`_Lever` values.

    The **left** lever steers the LHS (``A`` toward the boundary set by the
    current ``B``); the **right** lever — only when the operand is itself a tag —
    steers the RHS (``B`` toward the boundary set by the current ``A``), via the
    flipped comparison (``A > B`` ⟺ ``B < A``).  Both are reactive *candidates*:
    the loop tries one, verifies, and the existing ranker/nogood learning
    switches to the other if it was a no-op — nothing is pre-committed.

    A lever is kept only when its tag is **actionable** — steerable, or
    logic-written so trace can chase it.  A free, non-actionable operand (a
    harness-linked sensor) yields no lever; that is the converging/coast
    disposition's job (handled by the self-advancing branch upstream).

    When *program* is given, an inequality on an internal copy/calc register is
    first rewritten onto its input-level source(s) (``_rewrite_internal_compare``)
    so the levers land on steerable inputs the prover dissolved.  A tag-bound
    compare (``PV < Lower``) cannot cross the registry as-is (the calc crossing
    defers on a tag bound), so each side is *also* rewritten with the partner
    frozen at its snapshot value — the same freeze the two-tag calc branch
    applies one level down — landing literal-operand atoms on the sources
    (``Band < -100.0``, ``Level > 100.0``) that the resolver, or failing it the
    stage-3 heuristic (:func:`_heuristic_inequality_target`, steerable free
    words only), can turn into levers.
    """
    levers: list[_Lever] = []
    seen: set[str] = set()

    def _actionable(tag: str) -> bool:
        return tag in steerable or bool(pdg.writers_of.get(tag))

    def _add(label: str, req: Atom) -> None:
        heuristic = False
        marker = ""
        target = _resolve_inequality_target(req, snapshot, prior, pdg)
        if target is None:
            hit = _heuristic_inequality_target(req, snapshot, steerable, pdg)
            if hit is None:
                return
            value, marker = hit
            target = (req.tag, value)
            heuristic = True
        tag, value = target
        if tag in seen or not _actionable(tag):
            return
        seen.add(tag)
        note = _lever_note(req, atom, tag, value, marker)
        levers.append(_Lever(label, tag, value, heuristic=heuristic, note=note))

    def _rewrite(a: Atom) -> list[Atom]:
        if program is None:
            return [a]
        return _rewrite_internal_compare(a, steerable, pdg, program, snapshot)

    base = _rewrite(atom)
    operand = atom.operand
    if atom.operand_is_tag and program is not None:
        # Snapshot-frozen variants of both sides (see docstring).  Dedup by
        # atom key so an unchanged rewrite adds nothing new.
        seen_atoms = {a._key() for a in base}
        frozen: list[Atom] = []
        threshold = snapshot.get(operand)
        if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
            frozen.extend(_rewrite(Atom(tag=atom.tag, form=atom.form, operand=threshold)))
        lhs_now = snapshot.get(atom.tag)
        if (
            atom.form in _FLIP_FORM
            and isinstance(lhs_now, (int, float))
            and not isinstance(lhs_now, bool)
        ):
            frozen.extend(_rewrite(Atom(tag=operand, form=_FLIP_FORM[atom.form], operand=lhs_now)))
        for a in frozen:
            if a._key() not in seen_atoms:
                seen_atoms.add(a._key())
                base.append(a)

    for a in base:
        _add("left", a)
        if a.operand_is_tag and a.form in _FLIP_FORM:
            _add(
                "right",
                Atom(
                    tag=a.operand,
                    form=_FLIP_FORM[a.form],
                    operand=a.tag,
                    operand_is_tag=True,
                ),
            )

    return levers


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


def _constraint_atom(constraint: Constraint) -> Atom | None:
    """Render an advance boundary in the trace predicate language."""

    if isinstance(constraint, Eq):
        if len(constraint.values) != 1:
            return None
        return Atom(constraint.tag, "eq", next(iter(constraint.values)))
    if isinstance(constraint, Cmp):
        form = {
            "==": "eq",
            "!=": "ne",
            "<": "lt",
            "<=": "le",
            ">": "gt",
            ">=": "ge",
        }.get(constraint.op, constraint.op)
        return Atom(
            constraint.tag,
            form,
            constraint.bound,
            operand_is_tag=constraint.bound_is_tag,
        )
    return None


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
        has_no_dead_end = _route_has_no_dead_end(nodes)
        alternatives.append(
            _trace_alternative(
                choice=call_gate,
                nodes=nodes,
                rank=(
                    0 if has_no_dead_end else 1,
                    *_trace_score(nodes, env.pdg),
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

    if not isinstance(constraint, (Eq, Cmp)):
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
        owner.profile.linear is not None
        and owner.profile.done is not None
        and constraint.tag == owner.profile.done.name
        and owner.profile.linear.distance(constraint, env.snapshot) is None
        and step.progress is None
    ):
        # Restore/clear knowledge is useful to correction handling, but it is
        # not forward scalar motion and must not make an alternative writer
        # route look coastable.
        return None
    stage_boundary = owner.profile.linear is None
    atom = _constraint_atom(step.until)
    boundary = TraceNode(
        tag=step.until.tag,
        value=(atom.operand if atom is not None else step.until),
        satisfied=constraint_holds(step.until, env.snapshot) is True,
        provenance=provenance,
        relational=isinstance(step.until, Cmp) and stage_boundary,
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
        isinstance(constraint, Cmp)
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
    # This instruction is not the next operation while a persistent program
    # prerequisite is still closed.  Let the ordinary writer trace expose that
    # nearer state transition; after it lands, retracing will make this profile
    # the immediate frontier.
    if establish_nodes:
        return None
    children = [boundary, *gate_nodes, *demand_nodes]
    target_atom = _constraint_atom(constraint)
    return TraceNode(
        tag=constraint.tag,
        value=(target_atom.operand if target_atom is not None else constraint),
        provenance=provenance,
        relational=isinstance(constraint, Cmp),
        predicate=target_atom,
        children=children,
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
    route: TraceChoice | None = None,
    prior: DomainPrior | None = None,
    avoid_pred: Any = None,
    rejected_actions: frozenset[tuple[str, Any]] = frozenset(),
    max_depth: int = 15,
    harness: Any = None,
    constraints: TraceReadConstraints | None = None,
) -> TraceNode:
    """Backward trace for a relational *target* predicate (``A op B``).

    Routes the target through the same atom branch as a relational prerequisite,
    so a target inequality gets the live-predicate node, the up-to-two reactive
    levers, and the converging/coast disposition for free.  Returns the
    relational node (or a coast leaf / dead-end) as the tree root; a satisfied
    predicate yields a ``satisfied`` leaf (the drive loop's early-exit owns it).
    """
    read = constraints or TraceReadConstraints(
        clear_only=clear_only,
        opaque_loop=opaque_loop,
        pipeline_internal_tags=pipeline_internal_tags,
        route=route,
        prior=prior,
        avoid_pred=avoid_pred,
        rejected_actions=rejected_actions,
        harness=harness,
    )
    env = read.env(snapshot, pdg, program, steerable, max_depth=max_depth)
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


def _is_dead_end_leaf(leaf: TraceNode) -> bool:
    """A terminal the backward walk could not resolve to any action.

    Not satisfied (does not hold now), not steerable (PILOT can't set it), not
    self-advancing (no timer/counter to coast), not pipeline-internal, not a
    relational frontier — and childless (no further writer to chase).  The
    canonical case is ``InitDone == False`` once init has latched it True: no
    writer produces ``False``, so a route that needs it is dead.
    """
    return (
        not leaf.children
        and not leaf.satisfied
        and not leaf.is_steerable
        and leaf.advance is None
        and not leaf.pipeline_internal
        and not leaf.relational
    )


def _route_has_no_dead_end(nodes: list[TraceNode]) -> bool:
    """Whether a route has no unresolved leaf that PILOT cannot act on.

    A route is an AND of prerequisites; one dead-end leaf (see
    :func:`_is_dead_end_leaf`) means the route has a dead end. This is the
    filter to apply *before* :func:`_trace_score`, which only ranks: a dead route
    has no steerable leaves and therefore the cheapest (zero) downstream
    reach, so scoring alone would always prefer it over a live one.
    """
    return not any(_is_dead_end_leaf(leaf) for node in nodes for leaf in node.leaves())


def _route_actions_rejected(nodes: list[TraceNode], env: _TraceEnv) -> bool:
    """Whether this alternative is the exact disproved singleton artifact.

    Compass owns and world-scopes the evidence. Trace consumes only exact
    singleton actions admitted by Orientation, and uses them as an ordering
    hint among otherwise viable unlocked alternatives. A multi-leaf branch is
    a joint artifact and remains live until that exact joint act is tested.
    """

    actions = tuple(
        dict.fromkeys(detail.pair for node in nodes for detail in node.ordered_action_details())
    )
    # Multiple leaves describe a different, still-untested joint artifact.
    # Independent singleton failures cannot be composed into its rejection.
    return len(actions) == 1 and actions[0] in env.rejected_actions


def _trace_score(nodes: list[TraceNode], pdg: ProgramGraph) -> tuple[int, int, int]:
    """Rank alternative trace routes: low downstream reach, few pivots, few leaves."""
    steerable = [leaf for node in nodes for leaf in node.leaves() if leaf.is_steerable]
    downstream_reach = sum(
        len(pdg.downstream_slice(leaf.tag, follow_calls=True)) for leaf in steerable
    )
    pivots = sum(node.unsatisfied_count() for node in nodes)
    return downstream_reach, pivots, len(steerable)


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
    writer_availability: _WriterAvailability
    live_guard: bool
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

    return _TraceAlternative(
        choice=choice,
        rank=rank,
        violates_avoid=(
            env.avoid_pred is not None
            and bool(nodes)
            and _route_forces(nodes, env.snapshot, env.avoid_pred)
        ),
        has_no_dead_end=_route_has_no_dead_end(nodes),
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


def _route_forces(nodes: list[TraceNode], snapshot: dict[str, Any], pred: Any) -> bool:
    """Whether the concrete demands across *nodes* satisfy *pred*.

    Overlays each node's ``(tag, value)`` (skipping relational representatives
    and valueless nodes) onto the snapshot and evaluates the predicate.  Shared
    by the OR-arm avoid skip (an arm whose assignment forces the avoided
    condition is dropped from selection) and route avoid-pruning (a choice whose
    route forces it is pruned before the ambiguity check) — ``Or(Manual, Auto)``
    with ``avoid=Manual`` picks the ``Auto``/``Start`` route instead of only
    vetoing ``Manual`` at verify time.  Caller guards ``pred is not None``.
    """
    overlay = dict(snapshot)
    for root in nodes:
        for n in root.iter_nodes():
            if n.relational or n.value is None:
                continue
            overlay[n.tag] = n.value
    try:
        return bool(pred(overlay))
    except Exception:
        return False


def _route_forced_names(
    nodes: list[TraceNode], snapshot: dict[str, Any], avoid: Any
) -> tuple[str, ...]:
    """The avoid-condition names *nodes*' concrete demands satisfy.

    Same overlay as :func:`_route_forces`, but returns the violated member names
    (via ``avoid.violated``) so a route-pruned decline can name what excluded it.
    A bare callable avoid yields a generic name.
    """
    overlay = dict(snapshot)
    for root in nodes:
        for n in root.iter_nodes():
            if n.relational or n.value is None:
                continue
            overlay[n.tag] = n.value
    violated = getattr(avoid, "violated", None)
    if violated is not None:
        try:
            return tuple(violated(overlay))
        except Exception:
            return ()
    try:
        return ("avoided condition",) if bool(avoid(overlay)) else ()
    except Exception:
        return ()


def _value_sets_intersect(a: Any, b: Any) -> bool:
    """Whether any value in *a* loosely matches any value in *b* (``_values_match``).

    Small operands (channel-value sets, singleton pins), so the pairwise sweep
    is cheap and preserves ``1 == True`` semantics that a raw set intersection
    would only get by luck of Python hashing.
    """
    return any(_values_match(x, y) for x in a for y in b)


@dataclass(frozen=True, order=True)
class _RouteConflictPin:
    """One stable side of a route-conflict witness.

    Trace nodes are rebuilt independently for every route, so object identity
    cannot say whether two routes carry the same conflict.  Values and source
    metadata can: the normalized value keys preserve type plus representation,
    while ``source`` identifies the original trace demand rather than the
    channel alias it was reduced to.
    """

    values: tuple[str, ...]
    source: tuple[str, int, tuple[str, ...]]


@dataclass(frozen=True, order=True)
class _RouteConflict:
    """A concrete pair of incompatible demands on one channel tag."""

    tag: str
    left: _RouteConflictPin
    right: _RouteConflictPin


def _route_conflict_pin(values: Any, node: TraceNode) -> _RouteConflictPin:
    """Canonical, hashable identity for one conflict demand."""
    value_keys = tuple(
        sorted(f"{type(value).__module__}.{type(value).__qualname__}:{value!r}" for value in values)
    )
    return _RouteConflictPin(
        values=value_keys,
        source=(
            node.tag,
            node.writer_rung if node.writer_rung is not None else -1,
            node.provenance,
        ),
    )


def _route_conflicts(tree: TraceNode, pdg: ProgramGraph, program: Any) -> frozenset[_RouteConflict]:
    """Incompatible demand pairs in *tree* that must hold together.

    Every node in a resolved trace tree is a required condition (Or-arms are
    already chosen), so two nodes pinning the same tag to disjoint value sets
    clash **unless** one is an ancestor of the other — that is temporal
    sequencing (reach ``v1`` first, then ``v2``), not a
    simultaneous contradiction.  A plain node pins its scalar value (a singleton
    set); a mode flag is normalized through :func:`_equality_gated_coil` into the
    channel-register value *set* it implies, so a manual-mode caller gate
    (``S_ManualMode=True`` → ``S_UnitModeCurrent ∈ {3}``) clashes with a body that
    needs ``S_UnitModeCurrent=1``, while a set-valued alias (``Reg ∈ {3, 5}``)
    only clashes when the needed value falls *outside* the set.

    This is a *relative* signal, not an absolute feasibility verdict: sibling
    flags can also encode an SFC that legitimately sequences one register
    (``S_StateCurrent`` 3→6 appears here as ``S_Starting`` beside ``S_Execute``).
    The ranker discounts an identical conflict *witness* shared by **every**
    route as inherent to the goal and penalizes only route-unique witnesses.
    Witness identity includes both incompatible value sets and their trace
    sources.  Comparing only the tag name loses the distinction between common
    sequencing noise (``Mode 0 ↔ 1``) and a route's own contradiction
    (``ManualMode`` implying ``Mode 3`` beside a body needing ``Mode 1``).
    """
    entries: list[tuple[str, Any, TraceNode, int, frozenset[int]]] = []

    def walk(node: TraceNode, anc: frozenset[int]) -> None:
        if not (node.relational or node.value is None):
            alias = _equality_gated_coil(node.tag, node.value, pdg, program)
            if alias is not None:
                demand_tag, demand_vals = alias
            else:
                demand_tag, demand_vals = node.tag, (node.value,)
            entries.append((demand_tag, demand_vals, node, id(node), anc))
        child_anc = anc | {id(node)}
        for child in node.children:
            walk(child, child_anc)

    walk(tree, frozenset())

    by_tag: dict[str, list[tuple[Any, TraceNode, int, frozenset[int]]]] = {}
    for tag, vals, node, nid, anc in entries:
        by_tag.setdefault(tag, []).append((vals, node, nid, anc))

    conflicts: set[_RouteConflict] = set()
    for tag, pins in by_tag.items():
        if len(pins) < 2:
            continue  # single pin — no clash possible
        for i in range(len(pins)):
            vi, node_i, ni, ai = pins[i]
            for j in range(i + 1, len(pins)):
                vj, node_j, nj, aj = pins[j]
                if _value_sets_intersect(vi, vj):
                    continue  # a shared value satisfies both pins — compatible
                if nj in ai or ni in aj:
                    continue  # ancestor/descendant → temporal, not a clash
                left, right = sorted(
                    (_route_conflict_pin(vi, node_i), _route_conflict_pin(vj, node_j))
                )
                conflicts.add(_RouteConflict(tag=tag, left=left, right=right))
    return frozenset(conflicts)


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
            raise UnsupportedConstruct("condition", expr.unsupported, provenance)
        target = _atom_target(expr, env.snapshot)
        if target is None:
            if expr.form in ("lt", "le", "gt", "ge", "ne"):
                constraint = Cmp(
                    expr.tag,
                    {
                        "lt": "<",
                        "le": "<=",
                        "gt": ">",
                        "ge": ">=",
                        "ne": "!=",
                    }[expr.form],
                    expr.operand,
                    bound_is_tag=expr.operand_is_tag,
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
                # Carry the predicate live as a relational frontier (Stage A)
                # and surface up-to-two reactive levers (Stage B): steer the LHS
                # toward B, or steer the RHS toward A.  Both ride as children so
                # both surface as candidates; the ranker + try-verify-learn loop
                # picks one and switches if it was a no-op.  Distance counts the
                # predicate once (the relational node stops recursion), so the
                # levers do not double-count as separate goals.
                levers = _inequality_levers(
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

    raise UnsupportedConstruct("expression", expr, provenance)


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
    route: TraceChoice | None = None,
    writer_locks: dict[tuple[str, Any], int] | None = None,
    or_locks: dict[tuple[str, str], int] | None = None,
    prior: DomainPrior | None = None,
    avoid_pred: Any = None,
    rejected_actions: frozenset[tuple[str, Any]] = frozenset(),
    max_depth: int = 15,
    harness: Any = None,
    constraints: TraceReadConstraints | None = None,
    _visited: set[tuple[str, Any]] | None = None,
    _ancestry: tuple[tuple[str, Any], ...] = (),
    _depth: int = 0,
) -> TraceNode:
    """Recursive backward trace from ``(tag, value)``.

    Public entry point: bundles the invariant trace context (graph, steerable
    set, locks, domain prior, avoid predicate, ...) into a :class:`_TraceEnv`
    and delegates to :func:`_trace_back`, which threads that one value down the
    recursion instead of a dozen kwargs.  A ``TraceChoice`` resolves to its lock
    maps here, once.
    """
    read = constraints or TraceReadConstraints(
        clear_only=clear_only,
        opaque_loop=opaque_loop,
        pipeline_internal_tags=pipeline_internal_tags,
        route=route,
        prior=prior,
        avoid_pred=avoid_pred,
        rejected_actions=rejected_actions,
        harness=harness,
    )
    env = read.env(
        snapshot,
        pdg,
        program,
        steerable,
        writer_locks=writer_locks,
        or_locks=or_locks,
        max_depth=max_depth,
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
    # chain.  Stop and emit a dead-end leaf so Layer 6 owns the transition.
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

    writer_availability: dict[int, _WriterAvailability] = {}
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
            reverse_result = _reverse_writer(ro, tag, value, env.snapshot, env.pdg, env.prior)
        producer_target = eq_target(tag, value)
        producer_constraints = _producer_constraints(reverse_result, producer_target)
        producer_pins = _producer_pins(reverse_result, producer_target)

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
                guard_expr = _reduce_guard_by_pin(guard_expr, pin_tag, pin_value, env.snapshot)
                if guard_expr is _GUARD_CONTRADICTION:
                    writer_skips.append((ri, "guard_pin_contradiction"))
                    break
            if guard_expr is _GUARD_CONTRADICTION:
                continue

        if guard_expr is not None:
            guard_expr = _reduce_guard_by_fire_pins(
                guard_expr, ro, tag, value, env.snapshot, env.pdg, env.program
            )
            if guard_expr is _GUARD_CONTRADICTION:
                writer_skips.append((ri, "guard_fire_pin_contradiction"))
                continue

        # Rejection arm (tide_tables.guard_verdict): a writer whose guard is
        # *provably unsatisfiable* over complete finite free-tag domains — under
        # the fire-time pins the writer itself forces to produce ``value`` — can
        # never fire to produce it.  Skip it exactly as a False ``_can_produce``
        # would, so a provably-dead writer never burns drive-loop trials.
        # Punt-biased and sound: ONLY a definite ``GUARD_DEAD`` rejects; ``SAT``
        # and ``PUNT`` keep today's behavior untouched.
        guard_punted = False
        if guard_expr is not None:
            from pyrung.core.analysis.pilot.tide_tables import GUARD_DEAD, GUARD_PUNT

            verdict = _writer_guard_verdict(env, ri, ro, tag, value, reverse_result, guard_expr)
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
        attempt_node.writer_availability = writer_availability.get(ri, _WriterAvailability.UNKNOWN)

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
        for guard_expr in _return_early_guard_exprs(env.program, rung_node):
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
            atom = _constraint_atom(constraint)
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
        attempt_node.live_guard = guard_punted and not _route_has_no_dead_end([attempt_node])

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


def _arm_fully_steerable(e: Any, self_tag: str, steerable: frozenset[str]) -> bool:
    """True when *e* is reachable by directly-steerable inputs alone.

    An OR arm qualifies when PILOT can assert it with inputs only, recursively:

    * ``And`` — *every* term must be steerable (``And(Manual, DiverterBtn)``).
    * ``Or`` — *any* term suffices, since asserting one satisfies it
      (``And(Manual, Or(BtnA, BtnB))``).
    * ``Atom`` — a steerable input the trace can drive: a bit/equality whose
      ``_atom_target`` tag is steerable, **or** an inequality (``Size > 100``)
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
        target = _atom_target(e)
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
            if _expr_satisfied(e, snapshot):
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
) -> tuple[TraceChoice, ...]:
    """Enumerate route choices for an ambiguous ``tag == value`` trace.

    General over the target value — ``Bool == True``, ``Bool == False`` (the
    writer guard is negated for an ``out`` coil, or only reset writers are
    viable for a retentive coil), or a word ``tag == value`` (only writers whose
    ``_can_produce`` admits *value* are viable).  The route/OR-arm derivation,
    Or-scorer collapse, and rank/tie-break rules are all target-agnostic.

    A "route" is a top-level decision in how *tag* reaches *value*: which
    writer rung drives it, and which arm of each OR in that writer's
    condition is taken.  Choices are **root-only** — each locks just this
    decision; ``trace_back`` re-traces everything below it from current
    state.  Deeper ambiguity (an OR in a downstream tag's writer) is not
    enumerated, by design: the engineer picks the output route, PILOT plans
    the rest.  This reuses ``trace_back``'s lock mechanism rather than
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

    choices: list[TraceChoice] = []
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
            TraceChoice(
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


def route_rung_order(choice: TraceChoice) -> tuple[int, ...]:
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
    prior: DomainPrior | None = None,
    avoid_pred: Any = None,
    rejected_actions: frozenset[tuple[str, Any]] = frozenset(),
    harness: Any = None,
    constraints: TraceReadConstraints | None = None,
) -> tuple[tuple[TraceChoice, ...], tuple[tuple[TraceChoice, TraceNode], ...]]:
    """Enumerate and rank current-world root choices once.

    The complete enumerated set is returned for route reporting; the ranked set
    contains only choices admitted by the user's avoidance predicate. Both drive
    preparation and Orientation consume this reader so route order is not
    independently re-derived at the two ownership boundaries.
    """

    read = constraints or TraceReadConstraints(
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
    traced: list[tuple[TraceChoice, TraceNode]] = []
    for choice in choices:
        tree = trace_back(
            tag,
            value,
            snapshot,
            pdg,
            program,
            steerable,
            constraints=replace(read, route=choice, avoid_pred=None),
        )
        if read.avoid_pred is not None and _route_forces([tree], snapshot, read.avoid_pred):
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
    route_conflicts = [frozenset(_route_conflicts(tree, pdg, program)) for _choice, tree in traced]
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
            _trace_score([tree], pdg),
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


def _compact_route(route: tuple[str, ...], *, max_items: int = 8) -> tuple[str, ...]:
    compact: list[str] = []
    seen: set[str] = set()
    for item in route:
        if item in seen:
            continue
        seen.add(item)
        compact.append(item)
    if len(compact) <= max_items:
        return tuple(compact)
    head = compact[: max_items - 2]
    return (*head, "...", compact[-1])


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
        if _expr_satisfied(expr, snapshot):
            return [_RouteDraft()]
        key = _expr_route_key(expr)
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
        target = _atom_target(expr)
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
        return _atom_target(expr)
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


def compute_reference_constants(
    pdg: ProgramGraph, program: Any, known: dict[str, Any] | None = None
) -> frozenset[str]:
    """Never-written, non-external tags used as program reference values.

    Two structural families qualify:

    * a copy/fill source feeding a lookup-table pointer chain (the generated
      state-machine reference idiom); or
    * a tag used as both ``copy(REFERENCE, State)`` and the live RHS of
      ``State == REFERENCE`` (the direct named-state idiom).

    In either family, all four conditions hold:

    1. Tag has no writers (initial-value only)
    2. Used as a copy/fill source feeding some destination D
    3. D consumes that tag as a static reference, either through a table
       pipeline or an explicit tag-valued comparison
    4. Tag is **not** ``external`` — a declared program constant, not an
       operator/field interface (given *known*; unchecked when *known* is None).

    The functional dep collapse is key: ``sm__jump_target_ds_idx =
    S_StateRequested + 150`` means S_StateRequested is the representative
    of the pointer.  So ``copy(sm__STATESTARTINGREF, S_StateRequested)``
    makes sm__STATESTARTINGREF a reference constant — it feeds into the
    lookup-table machinery through the collapsed pointer chain.

    Condition 4 is what keeps an *operator command* that happens to index a
    table (``copy(ToolReqCmd, ToolReq)`` with ``calc(ToolReq + k, idx)``) out of
    the constant set: the operator chooses the value, so it stays steerable.
    """
    from pyrung.core.instruction.data_transfer import CopyInstruction, FillInstruction
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef

    # Step 1: find direct pointer tags from indirect copies.
    pointer_tags: set[str] = set()

    def _scan_pointers(rungs: Any) -> None:
        for r in rungs:
            for instr in getattr(r, "_instructions", ()):
                if isinstance(instr, CopyInstruction):
                    src = instr.source
                    if isinstance(src, IndirectRef):
                        name = getattr(src.pointer, "name", None)
                        if name:
                            pointer_tags.add(name)
                    elif isinstance(src, IndirectExprRef):
                        names = _expr_tag_names(src.expr)
                        if names:
                            pointer_tags.update(names)
            _scan_pointers(getattr(r, "_branches", ()))

    _scan_pointers(program.rungs)
    for sub_rungs in getattr(program, "subroutines", {}).values():
        _scan_pointers(sub_rungs)

    # Step 2: follow functional deps (calc-defined scratch) to find
    # representative tags.  ptr = calc(rep + offset) → rep drives ptr.
    pipeline_tags = set(pointer_tags)
    for ptr in list(pointer_tags):
        tag = ptr
        for _ in range(3):
            defn = single_calc_source(tag, pdg, program)
            if defn is None:
                break
            _expr, rep = defn
            pipeline_tags.add(rep)
            tag = rep

    # Direct named-state references preserve their identity in Atom. Pair the
    # operand with the exact LHS it names a value for; this deliberately does
    # not classify an ordinary threshold merely because it appears on the RHS
    # of a comparison.
    from pyrung.core.analysis.simplified import And, Atom, Or, _conditions_list_to_expr

    comparison_reference_pairs: set[tuple[str, str]] = set()

    def _scan_reference_expr(expr: Any) -> None:
        if isinstance(expr, Atom):
            if expr.operand_is_tag:
                comparison_reference_pairs.add((expr.operand, expr.tag))
            return
        if isinstance(expr, (And, Or)):
            for term in expr.terms:
                _scan_reference_expr(term)

    def _scan_reference_conditions(rungs: Any) -> None:
        for r in rungs:
            _scan_reference_expr(_conditions_list_to_expr(getattr(r, "_conditions", [])))
            _scan_reference_conditions(getattr(r, "_branches", ()))

    _scan_reference_conditions(program.rungs)
    for sub_rungs in getattr(program, "subroutines", {}).values():
        _scan_reference_conditions(sub_rungs)

    # Step 3: find never-written tags used as copy sources into either family.
    candidates: set[str] = set()

    def _is_direct_declaration(name: str) -> bool:
        # A block-backed slot is mutable PLC memory even when this program has
        # no writer for it; the operator, recipe loader, or another controller
        # may own it. The direct named-state idiom is a standalone DSL
        # declaration. Table analysis separately proves the block-backed
        # reference slots it is allowed to classify.
        return known is None or getattr(known.get(name), "_pyrung_block", None) is None

    def _scan_sources(rungs: Any) -> None:
        for r in rungs:
            for instr in getattr(r, "_instructions", ()):
                if isinstance(instr, CopyInstruction):
                    src_name = getattr(instr.source, "name", None)
                    dest_name = getattr(instr.dest, "name", None)
                    if (
                        src_name
                        and dest_name
                        and (
                            dest_name in pipeline_tags
                            or (
                                (src_name, dest_name) in comparison_reference_pairs
                                and _is_direct_declaration(src_name)
                            )
                        )
                    ):
                        candidates.add(src_name)
                elif isinstance(instr, FillInstruction):
                    src_name = getattr(instr.value, "name", None)
                    dest_name = getattr(instr.dest, "name", None)
                    if (
                        src_name
                        and dest_name
                        and (
                            dest_name in pipeline_tags
                            or (
                                (src_name, dest_name) in comparison_reference_pairs
                                and _is_direct_declaration(src_name)
                            )
                        )
                    ):
                        candidates.add(src_name)
            _scan_sources(getattr(r, "_branches", ()))

    _scan_sources(program.rungs)
    for sub_rungs in getattr(program, "subroutines", {}).values():
        _scan_sources(sub_rungs)

    # Step 3b: the table *contents*.  A never-written ds/dh slot reached ONLY
    # through ``ds[computed]`` (a computed-index read, never a plain copy source)
    # is data, not a lever — but when the pointer is bounded (choices / min-max)
    # the PDG registers those slots as readers, so the steerability predicate would
    # otherwise classify each as steerable and the skiff would waste probes on a
    # data-only constant.  Add the slots an indirect read can land on; the
    # ``_is_program_constant`` filter below keeps a written or ``external`` slot
    # (an operator-indexed table) out, exactly as condition 4 does for sources.
    from pyrung.core.analysis.pdg import _indirect_expr_base_tag, _indirect_ref_tags

    def _indirect_read_slots(src: Any) -> list[str]:
        if isinstance(src, IndirectRef):
            tags = _indirect_ref_tags(src.block, src.pointer)
        elif isinstance(src, IndirectExprRef):
            base = _indirect_expr_base_tag(src.expr)
            tags = _indirect_ref_tags(src.block, base) if base is not None else None
        else:
            return []
        return [t.name for t in tags] if tags is not None else []

    def _scan_indirect_reads(rungs: Any) -> None:
        for r in rungs:
            for instr in getattr(r, "_instructions", ()):
                if isinstance(instr, CopyInstruction):
                    candidates.update(_indirect_read_slots(instr.source))
            _scan_indirect_reads(getattr(r, "_branches", ()))

    _scan_indirect_reads(program.rungs)
    for sub_rungs in getattr(program, "subroutines", {}).values():
        _scan_indirect_reads(sub_rungs)

    def _is_program_constant(n: str) -> bool:
        if pdg.writers_of.get(n, frozenset()):
            return False  # written by the program — a pipeline tag, not a constant
        if known is not None and getattr(known.get(n), "external", False):
            return False  # an operator/field interface, not a program constant
        return True

    return frozenset(n for n in candidates if _is_program_constant(n))


def compute_edge_tags(pdg: ProgramGraph, program: Any) -> set[str]:
    """Tag names read through ``rise()``/``fall()`` anywhere in the program."""
    from pyrung.core.analysis.simplified import And, Atom, Or

    result: set[str] = set()

    def visit(e: Any) -> None:
        if isinstance(e, Atom):
            if e.form in ("rise", "fall"):
                result.add(e.tag)
        elif isinstance(e, (And, Or)):
            for term in e.terms:
                visit(term)

    seen: set[int] = set()
    for rung_node in pdg.rung_nodes:
        ro = resolve_rung(program, rung_node)
        if ro is None or id(ro) in seen:
            continue
        seen.add(id(ro))
        sp = ro.sp_tree()
        if sp is not None:
            visit(_sp_to_expr(sp))
    return result


def compute_resting_values(
    steerable: frozenset[str],
    known: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
) -> dict[str, Any]:
    """Map each steerable input to its resting value.

    For pure INPUTs: the tag's declared default.
    For ack-cleared Bools: the program's clearing value via
    ``_scan_transient_rest``.
    """
    resting: dict[str, Any] = {}
    for name in steerable:
        t = known.get(name)
        if t is None:
            resting[name] = False
            continue
        transient, rest_val = _scan_transient_rest(name, pdg, program)
        if transient and rest_val is not None:
            resting[name] = rest_val
        else:
            resting[name] = getattr(t, "default", False)
    return resting


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
    ro: Any,
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
        has_no_dead_end = _route_has_no_dead_end(nodes)
        alternatives.append(
            _trace_alternative(
                choice=arm,
                nodes=nodes,
                rank=(
                    0 if has_no_dead_end else 1,
                    *_trace_score(nodes, env.pdg),
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
        # nothing the tide tables could key on — punt, exactly as before.
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


def _visit_key(tag: str, value: Any) -> tuple[str, Any]:
    if isinstance(value, (bool, int, float, str, type(None))):
        return (tag, value)
    return (tag, id(value))


def _can_produce(wv: Any, value: Any) -> bool:
    if isinstance(wv, Literal):
        return _values_match(wv.value, value)
    if isinstance(wv, Affine):
        return True
    if isinstance(wv, Aggregate):
        return True
    return True  # UNKNOWN — assume it could


_UNRESOLVED = object()


def _concrete_written_value(wv: Any, snapshot: dict[str, Any]) -> Any:
    """The concrete value *wv* provably drives this scan, or ``_UNRESOLVED``.

    A ``Literal`` produces its value; an ``Affine`` (identity/scaled copy)
    produces ``source * scale + offset`` when the source's live value is a
    known number.  Anything else — an opaque copy, an aggregate, an absent or
    non-numeric source — is unresolved (punt, never fabricate).
    """
    if isinstance(wv, Literal):
        return wv.value
    if isinstance(wv, Affine):
        if wv.source not in snapshot:
            return _UNRESOLVED
        src = snapshot[wv.source]
        if not isinstance(src, (int, float, bool)):
            return _UNRESOLVED
        try:
            return wv.scale * src + wv.offset
        except TypeError:
            return _UNRESOLVED
    return _UNRESOLVED


def _writer_clobbers_codemand(
    ro: Any,
    tag: str,
    codemands: tuple[tuple[str, Any], ...],
    snapshot: dict[str, Any],
) -> bool:
    """Whether *ro*'s co-writes provably falsify a concurrent sibling demand.

    ``codemands`` are the concrete ``(tag, value)`` demands of the enclosing
    guard's *other* atoms — pins that must all hold at the same fire scan.  A
    writer selected to satisfy one atom that, via a **different** co-write,
    provably drives a co-demanded register to a conflicting value defeats the
    joint requirement (firing the C_Start rung for ``CmdReq == 1`` also writes
    ``Cmd := 2``, clobbering the sibling need ``Cmd == 5``).  State-consistent
    ranking demotes such a writer below a tied sibling whose co-writes preserve
    the demand.  Only a *provable* conflict counts — an unresolved co-write
    never demotes (punt, never fabricate).  Ordering only; never drops a writer.
    """
    for cd_tag, cd_val in codemands:
        if cd_tag == tag:
            continue
        produced = _concrete_written_value(_written_value_for_tag(ro, cd_tag), snapshot)
        if produced is _UNRESOLVED:
            continue
        if not _values_match(produced, cd_val):
            return True
    return False


def _is_self_gated(rn: Any, pdg: ProgramGraph, tag: str) -> bool:
    """A writer is self-gated if its condition or call-gate reads the tag.

    ``with rung(State == 1): copy(1, State)`` is a hold/latch — it can
    never *cause* a transition, only sustain one.  The trace should prefer
    transition writers that can actually move the tag to the target value.
    """
    if tag in rn.condition_reads:
        return True
    if rn.subroutine:
        for cn in pdg.rung_nodes:
            if rn.subroutine in cn.calls and tag in cn.condition_reads:
                return True
    return False


@dataclass(frozen=True)
class _WriterRank:
    """One writer's place in a ``_rank_writers`` ordering, with its sort key.

    Recording only: carries the three sort dimensions the ranker computes and
    otherwise throws away — ``availability`` (the ``_WriterAvailability`` verdict),
    ``bucket`` (writer role: literal-establish / affine / counterfactual / …), and
    ``clobber`` (1 iff the writer's co-writes defeat a concurrent sibling demand).
    Stashed on ``TraceNode.writer_ranking`` so the "why this writer" decision is
    legible after the fact.
    """

    ri: int
    availability: _WriterAvailability
    bucket: int
    clobber: int


def _rank_writers(
    writers: frozenset[int],
    pdg: ProgramGraph,
    program: Any,
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    opaque_loop: frozenset[str] = frozenset(),
    clear_only: frozenset[str] = frozenset(),
    *,
    steerable: frozenset[str] = frozenset(),
    ancestry: tuple[tuple[str, Any], ...] = (),
    codemands: tuple[tuple[str, Any], ...] = (),
    availability_out: dict[int, _WriterAvailability] | None = None,
    ranking_out: list[_WriterRank] | None = None,
    reverse_out: dict[int, ReverseResult] | None = None,
) -> list[int]:
    """Rank viable writers by current-state availability, then writer role.

    - Prevents dead-ending on a latch writer (``if State == 1: copy(1, State)``)
      when a transition writer (``copy(C_UnitMode, State)``) exists.
    - Prevents selecting a *counterfactual* writer — one whose guard, evaluated in
      the projected prerequisite state, has a false leaf on a pinned tag.  Covers
      both the one-hot case (``copy(1, S_StateCompleteBool)`` under ``S_Clearing``
      while we hold ``S_Starting``) and the self-referential affine step counter
      (the even-step rung gated ``valstepisodd != 1`` cannot produce
      ``CurStep == 2``, because at the source state ``CurStep == 1`` the parity is
      odd; the transition rung gated ``Trans == 1`` stays live).  See
      ``_writer_projection``.
    - Demotes a *maintenance* writer — a literal init/reset rung fireable only by
      pressing a clear-only (ack-cleared momentary) lever off the natural path
      (``fill(1, CurStep)`` gated ``Or(xInit, xReset)``).  Its guard is not
      consistent with the state the plan drives toward; a self-advancing value-step
      writer whose guard the pipeline establishes (``CurStep+1`` under the caller
      gate) is the state-consistent choice, so the maintenance writer ranks below
      it — kept as a fallback, never the default route.
    """
    pinned_overlay = {t: snapshot.get(t) for t in opaque_loop}
    pinned = frozenset(opaque_loop)
    ranked: list[tuple[_WriterAvailability, int, int, int]] = []
    prior_same_tag_values = tuple(v for t, v in ancestry if t == tag)
    # Non-steerable ancestry registers count as current-state tags: a writer
    # whose guard demands a different value of a register this walk already
    # derives through is state-inconsistent (circular), so it sinks below the
    # writer whose state guard the live snapshot satisfies.  Ordering only.
    ancestry_tags = frozenset(t for t, _v in ancestry if t not in steerable)
    for ri in sorted(writers):
        rn = pdg.rung_nodes[ri]
        ro = resolve_rung(program, rn)
        if ro is None:
            continue
        wv = _written_value_for_tag(ro, tag)
        if not _can_produce(wv, value):
            continue
        reverse_result = _reverse_writer(ro, tag, value, snapshot, pdg)
        if reverse_out is not None:
            reverse_out[ri] = reverse_result
        proj = _writer_projection(ro, tag, value, snapshot, pdg, program, pinned_overlay, pinned)
        is_counterfactual = proj is not None and proj[0]
        availability = _writer_availability(
            ro,
            rn,
            wv,
            tag,
            value,
            snapshot,
            pdg,
            program,
            steerable,
            opaque_loop,
            is_counterfactual,
            ancestry_tags,
        )
        if isinstance(wv, Affine) and wv.source == tag:
            src_val = _invert_affine(wv, value)
            if src_val is not None and any(
                _values_match(src_val, prior) for prior in prior_same_tag_values
            ):
                availability = _WriterAvailability.UNAVAILABLE_FROM_HERE

        bucket = 1  # ordinary non-literal / affine writer
        if isinstance(wv, Literal) and _values_match(wv.value, value):
            if _is_self_gated(rn, pdg, tag):
                bucket = 4
            elif is_counterfactual:
                bucket = 3
            elif proj is not None and any(t in clear_only for t in proj[1]):
                # Fireable only by pressing a clear-only maintenance lever off the
                # natural path — rank below any self-advancing value-step writer.
                bucket = 2
            else:
                bucket = 0
        else:
            producer_pins = _producer_pins(reverse_result, eq_target(tag, value))
            if producer_pins and all(
                _values_match(snapshot.get(src_tag), src_val)
                for src_tag, src_val in producer_pins.items()
            ):
                bucket = 0
            if is_counterfactual:
                bucket = 3

        # State-consistent co-write tie-break: among writers otherwise tied on
        # availability and role, one whose *other* co-writes provably clobber a
        # concurrent sibling demand (the C_Start rung produces ``CmdReq == 1`` but
        # also writes ``Cmd := 2``, defeating the sibling ``Cmd == 5`` the same guard
        # needs) sinks below a sibling whose co-writes preserve it.  Ordering only.
        clobber = (
            1 if (codemands and _writer_clobbers_codemand(ro, tag, codemands, snapshot)) else 0
        )

        ranked.append((availability, bucket, clobber, ri))
        if availability_out is not None:
            availability_out[ri] = availability
    ordered = sorted(ranked)
    if ranking_out is not None:
        ranking_out.extend(
            _WriterRank(ri=ri, availability=av, bucket=bkt, clobber=clb)
            for av, bkt, clb, ri in ordered
        )
    return [ri for _availability, _bucket, _clobber, ri in ordered]


def _scan_transient_rest(
    tag: str,
    pdg: ProgramGraph,
    program: Any,
) -> tuple[bool, Any]:
    """Whether *tag* provably rests at one value at every scan boundary."""
    from pyrung.core.analysis.simplified import Atom, Or

    if pdg.tag_roles.get(tag) == TagRole.INPUT:
        return False, None

    writer_idxs = pdg.writers_of.get(tag, frozenset())
    if not writer_idxs:
        return False, None
    writes: list[tuple[Any, Any, Any]] = []
    for ri in writer_idxs:
        rung_node = pdg.rung_nodes[ri]
        ro = resolve_rung(program, rung_node)
        if ro is None:
            return False, None
        if tag in rung_node.ote_writes:
            return False, None
        lw = _steerable._literal_write(ro, tag)
        if lw is None:
            return False, None
        writes.append((rung_node, ro, lw))

    candidate_rests: list[Any] = []
    for _n, _r, v in writes:
        if not any(_values_match(v, c) for c in candidate_rests):
            candidate_rests.append(v)

    def _main_exec_pos(node: Any) -> int | None:
        """*node*'s position in the flattened per-scan execution order, as a
        main-program rung index.  A main-scope node is its own ``rung_index``; a
        subroutine node runs at its call site — resolvable to a single position
        only when that subroutine has exactly one main call site.  ``None`` means
        the node cannot be placed in a single scan order (multiple / zero call
        sites, or a subroutine called only from another subroutine)."""
        if node.subroutine is None:
            return node.rung_index
        sites = pdg.call_site_rung_indices().get(node.subroutine, frozenset())
        return next(iter(sites)) if len(sites) == 1 else None

    for rest in candidate_rests:
        producers = [(n, v) for n, _r, v in writes if not _values_match(v, rest)]
        clearers = [(n, r) for n, r, v in writes if _values_match(v, rest)]
        if not producers or not clearers:
            continue
        prod_scopes = {n.subroutine for n, _v in producers}
        produced_vals = [v for _n, v in producers]

        def _fires_when_set(e: Any, produced: tuple[Any, ...] = tuple(produced_vals)) -> bool:
            if isinstance(e, Atom):
                if e.tag != tag:
                    return False
                if e.form in ("xic", "truthy"):
                    return all(bool(v) for v in produced)
                return e.form == "eq" and all(_values_match(e.operand, v) for v in produced)
            if isinstance(e, Or):
                return any(_fires_when_set(term, produced) for term in e.terms)
            return False

        if len(prod_scopes) == 1:
            pscope = next(iter(prod_scopes))
            last_producer = max(n.rung_index for n, _v in producers)
            for c_node, c_ro in clearers:
                sp = c_ro.sp_tree()
                if sp is not None and not _fires_when_set(_sp_to_expr(sp)):
                    continue
                if c_node.subroutine == pscope:
                    if c_node.rung_index > last_producer:
                        return True, rest
                    continue
                if c_node.subroutine is None:
                    continue
                for cnode in pdg.rung_nodes:
                    if (
                        c_node.subroutine in cnode.calls
                        and cnode.subroutine == pscope
                        and cnode.rung_index > last_producer
                    ):
                        cro = resolve_rung(program, cnode)
                        if cro is None:
                            continue
                        csp = cro.sp_tree()
                        if csp is not None and _fires_when_set(_sp_to_expr(csp)):
                            return True, rest
            continue

        # Multi-scope producers: rest is provable only when the producers can be
        # linearized in the per-scan execution order *and* a self-gated main-scope
        # clearer runs strictly after all of them — then it clears whatever any
        # producer set, every scan.  Each producer is placed at its main-program
        # execution position (its call site, for a subroutine producer); if any
        # cannot be placed (a producer scope with multiple/zero main call sites)
        # the ordering is ambiguous and we punt.  The clearer is required to be in
        # the main scope so it provably executes after cross-scope producers; a
        # clearer nested in a subroutine, or sitting between producer call sites,
        # is not proven and falls through to the punt — never a fabricated rest.
        prod_positions = [_main_exec_pos(n) for n, _v in producers]
        if any(p is None for p in prod_positions):
            continue
        last_prod_pos = max(p for p in prod_positions if p is not None)
        for c_node, c_ro in clearers:
            if c_node.subroutine is not None:
                continue
            sp = c_ro.sp_tree()
            if sp is not None and not _fires_when_set(_sp_to_expr(sp)):
                continue
            if c_node.rung_index > last_prod_pos:
                return True, rest
    return False, None
