"""Build and query PILOT's static transition graphs.

Expanded routes from ``evidence.py`` become per-register ``StaticTransitionGraph``
objects whose edges describe source values, destinations, and possible
drivers. ``_best_static_path`` searches those graphs without executing the
program. The module also detects opaque transition-pipeline slices used when
building the analysis context.

Static graphs enumerate every source match, exact edges before wildcard edges:
specificity is precedence, never a pre-filter veto of a surviving wildcard
route.  A convergence lookup is an ordered multimap of primary-action
alternatives — construction fans each alternative into its own edge, and only
a route's ``edge_gates`` are simultaneous co-actions.

Runtime observations and probe history belong to ``compass.py``.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionRoute, expand_routes
from pyrung.core.analysis.pilot.navigation_contracts import _context_value_key
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph

logger = logging.getLogger(__name__)

ActionPair = tuple[str, Any]
ActionLookup = dict[tuple[str, str], tuple[ActionPair, ...]]
ANY_FROM = object()


def _canonical_applied(applied: Iterable[ActionPair]) -> tuple[ActionPair, ...]:
    """Canonical effective action overlay (last write per tag, tag ordered)."""
    return tuple(sorted(dict(applied).items()))


def _applied_key(applied: Iterable[ActionPair]) -> tuple[tuple[str, Any], ...]:
    """Hashable identity of the complete action overlay used by a trial."""
    return tuple((tag, _context_value_key(value)) for tag, value in _canonical_applied(applied))


# ===========================================================================
# Static value-graph (provenance: static route) — folded in from compass.py
# ===========================================================================


@dataclass(frozen=True)
class StaticTransitionEdge:
    """One normalized transition edge for a channel pipeline register.

    ``action`` is the primary steerable pulse that fires the edge (the command
    button, bridged from a non-steerable ``CtrlCmd``-style convergence enabler).
    ``co_actions`` are the steerable inputs that must fire *in the same scan* as
    ``action`` — the one-shot edge gate (``rise(CmdChgRequest)``) — without which
    the command rung never executes.  Completion (let-run) edges carry neither.

    ``completion`` carries the charted gate pairs a completion edge
    (``action is None``) waits on — the route's recorded condition, verbatim,
    minus the channel's own from-value and the operator's buttons.  The wait's
    bearing: the next trace re-reads it and does all
    classification; nothing is invented here.  Action-bearing edges and routes
    whose gate names nothing else carry ``()``.
    """

    role: PipelineRoles
    from_value: Any
    to_value: Any
    action: ActionPair | None
    request_tag: str | None
    request_value: Any
    source_constraints: tuple[tuple[str, Any], ...]
    enablers: tuple[tuple[str, Any], ...]
    route: TransitionRoute
    co_actions: tuple[ActionPair, ...] = ()
    completion: tuple[tuple[str, Any], ...] = ()
    program_producers: tuple[Any, ...] = ()

    @property
    def identity(self) -> tuple[Any, ...]:
        """Stable catalog identity independent of object allocation."""

        return (
            self.role.channel_tag,
            "*" if self.from_value is ANY_FROM else repr(self.from_value),
            repr(self.to_value),
            self.action,
            self.co_actions,
            self.request_tag,
            repr(self.request_value),
            tuple((tag, repr(value)) for tag, value in self.source_constraints),
            tuple((tag, repr(value)) for tag, value in self.enablers),
            tuple((tag, repr(value)) for tag, value in self.completion),
            tuple(producer.rung_index for producer in self.program_producers),
        )

    def exercised_by(
        self,
        observation: Any,
        applied: tuple[ActionPair, ...],
    ) -> bool:
        """Whether a runtime trial exercised this exact static artifact."""
        if observation.world_key is None:
            return True
        required = () if self.action is None else (self.action, *self.co_actions)
        if _applied_key(applied) != _applied_key(required):
            return False
        overlay = {**dict(observation.context), **dict(applied)}
        return all(
            tag in overlay and _values_match(overlay[tag], value)
            for tag, value in (*self.source_constraints, *self.enablers)
        )


@dataclass(frozen=True)
class StaticPath:
    """A BFS route through one pipeline's transition graph."""

    needed_tag: str
    needed_value: Any
    role: PipelineRoles
    target_value: Any
    edges: tuple[StaticTransitionEdge, ...]

    @property
    def first_edge(self) -> StaticTransitionEdge:
        return self.edges[0]


class StaticTransitionGraph:
    """Value graph for one :class:`PipelineRoles` owner."""

    def __init__(
        self,
        role: PipelineRoles,
        routes: tuple[TransitionRoute, ...],
        action_lookup: ActionLookup | None = None,
        program_producers: dict[tuple[str, str], tuple[Any, ...]] | None = None,
    ) -> None:
        self.role = role
        self.routes = routes
        self.edges = _edges_from_routes(
            role,
            routes,
            action_lookup or {},
            program_producers or {},
        )

    def target_values_for_need(self, needed_tag: str, needed_value: Any) -> tuple[Any, ...]:
        values: list[Any] = []
        for route in self.routes:
            if route.destination_value is None:
                continue
            if needed_tag == self.role.channel_tag and _values_match(
                route.destination_value,
                needed_value,
            ):
                values.append(route.destination_value)
            elif (
                needed_tag in self.role.request_tags
                and route.request_tag == needed_tag
                and _values_match(route.request_value, needed_value)
            ):
                values.append(route.destination_value)
        return _dedupe_values(values)

    def find_path(
        self,
        from_value: Any,
        target_values: tuple[Any, ...],
        *,
        edge_allowed: Callable[[StaticTransitionEdge], bool] | None = None,
        first_edge_allowed: Callable[[StaticTransitionEdge], bool] | None = None,
    ) -> StaticPath | None:
        """Return the shortest charted path allowed from this world.

        ``first_edge_allowed`` is deliberately narrower than ``edge_allowed``:
        it describes Bearings disproved at the current source value. Such an
        edge may still be used later in the path after another transition has
        changed the world in which its steer will be verified.
        """

        if not target_values:
            return None
        if any(_values_match(from_value, target) for target in target_values):
            return None

        queue: deque[tuple[Any, tuple[StaticTransitionEdge, ...]]] = deque([(from_value, ())])
        visited = {_value_key(from_value)}
        while queue:
            state, path = queue.popleft()
            for edge in _rank_edges_for_state(self.edges, state):
                if not _edge_matches(edge, state):
                    continue
                if not path and first_edge_allowed is not None and not first_edge_allowed(edge):
                    continue
                if edge_allowed is not None and not edge_allowed(edge):
                    continue
                key = _value_key(edge.to_value)
                if key in visited:
                    continue
                next_path = (*path, edge)
                if any(_values_match(edge.to_value, target) for target in target_values):
                    return StaticPath(
                        needed_tag="",
                        needed_value=None,
                        role=self.role,
                        target_value=edge.to_value,
                        edges=next_path,
                    )
                visited.add(key)
                queue.append((edge.to_value, next_path))
        return None


def target_reachable_values(
    graph: StaticTransitionGraph,
    target_value: Any,
) -> tuple[Any, ...]:
    """Return concrete values with a charted path to ``target_value``."""

    reachable: list[Any] = [target_value]
    changed = True
    while changed:
        changed = False
        for edge in graph.edges:
            if edge.from_value is ANY_FROM or not any(
                _values_match(edge.to_value, value) for value in reachable
            ):
                continue
            if any(_values_match(edge.from_value, value) for value in reachable):
                continue
            reachable.append(edge.from_value)
            changed = True
    return tuple(reachable)


def _static_graph_identity(graph: StaticTransitionGraph) -> tuple[Any, ...]:
    """Semantic chart identity, including the role that owns its edges."""

    role = graph.role
    return (
        role.channel_tag,
        tuple(sorted(role.request_tags)),
        tuple(sorted(role.observation_tags)),
        tuple(sorted(role.guard_internal_tags)),
        tuple(sorted(role.scratch_internal_tags)),
        tuple(edge.identity for edge in graph.edges),
    )


def build_static_transition_graphs(
    roles: tuple[PipelineRoles, ...],
    pdg: Any,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    evidence: Any,
) -> tuple[StaticTransitionGraph, ...]:
    graphs: list[StaticTransitionGraph] = []
    for role in roles:
        routes = tuple(
            expand_routes(
                role.channel_tag,
                pdg,
                program,
                steerable,
                opaque_loop,
                evidence,
            )
        )
        action_lookup = _build_action_lookup(
            routes,
            pdg,
            program,
            steerable,
            opaque_loop,
            evidence,
        )
        from pyrung.core.analysis.pilot.awaited_actions import sibling_producer_family
        from pyrung.core.analysis.pilot.trace_read import WorldView

        world = WorldView(
            snapshot={name: getattr(tag, "default", None) for name, tag in pdg.tags.items()},
            pdg=pdg,
            program=program,
            steerable=steerable,
            opaque_loop=opaque_loop,
        )
        program_producers: dict[tuple[str, str], tuple[Any, ...]] = {}
        for route in routes:
            for tag, value in (*route.enablers, *route.source_constraints):
                family = sibling_producer_family(world, tag, value)
                if family is not None and family.program_owned:
                    program_producers[(tag, _value_key(value))] = family.program_owned
                    logger.debug(
                        "chart automatic producer: %s=%r -> %s=%r",
                        tag,
                        value,
                        route.destination_tag,
                        route.destination_value,
                    )
        graph = StaticTransitionGraph(
            role,
            routes,
            action_lookup,
            program_producers,
        )
        if graph.edges:
            graphs.append(graph)
    return tuple(graphs)


def _best_static_path(
    needed_tag: str,
    needed_value: Any,
    snapshot: dict[str, Any],
    graphs: tuple[StaticTransitionGraph, ...],
    *,
    edge_allowed: Callable[[StaticTransitionEdge], bool],
    first_edge_allowed: Callable[[StaticTransitionEdge], bool] | None = None,
) -> StaticPath | None:
    """Best constrained static path for a need, if any."""

    plans: list[StaticPath] = []
    for graph in graphs:
        if needed_tag != graph.role.channel_tag and needed_tag not in graph.role.request_tags:
            continue
        current = snapshot.get(graph.role.channel_tag)
        targets = graph.target_values_for_need(needed_tag, needed_value)
        plan = graph.find_path(
            current,
            targets,
            edge_allowed=edge_allowed,
            first_edge_allowed=first_edge_allowed,
        )
        if plan is None:
            continue
        plans.append(
            StaticPath(
                needed_tag=needed_tag,
                needed_value=needed_value,
                role=plan.role,
                target_value=plan.target_value,
                edges=plan.edges,
            )
        )

    if not plans:
        return None
    return min(plans, key=_plan_score)


def _edges_from_routes(
    role: PipelineRoles,
    routes: tuple[TransitionRoute, ...],
    action_lookup: ActionLookup,
    program_producers: dict[tuple[str, str], tuple[Any, ...]] | None = None,
) -> tuple[StaticTransitionEdge, ...]:
    edges: list[StaticTransitionEdge] = []
    for route in routes:
        if route.destination_value is None:
            continue
        from_values = _route_from_values(role, route)
        # Actions: directly-steerable enablers, plus enablers bridged through a
        # convergence pipeline to a steerable button (``CtrlCmd==1 -> CmdReset``).
        action_pairs = _dedupe_action_pairs(
            (*_route_action_pairs(route), *_enabler_action_pairs(route, action_lookup))
        )
        if not action_pairs:
            action_pairs = _dedupe_action_pairs(
                _constraint_action_pairs(role, route, action_lookup)
            )
        # Co-actions ride only on action-bearing (command) edges; the one-shot
        # edge gate (``rise(CmdChgRequest)``) must fire the same scan as the
        # button.  Completion edges have no edge gates → coast.
        co_actions = tuple(route.edge_gates)
        # The wait's bearing, recorded on completion (``action is None``) edges:
        # the route's charted gate pairs, verbatim.  Part 2's sibling trace does
        # all classification — a coil gate descends to its driving predicate, a
        # satisfied pair contributes nothing — so no producer analysis happens
        # here; the chart's recorded condition is the whole claim.
        completion = _route_completion_pairs(role, route)
        route_producers = tuple(
            producer
            for tag, value in (*route.enablers, *route.source_constraints)
            for producer in (program_producers or {}).get((tag, _value_key(value)), ())
        )
        if not from_values and action_pairs:
            from_values = [ANY_FROM]
        if not from_values:
            continue
        for from_value in from_values:
            if action_pairs:
                for action in action_pairs:
                    edges.append(_edge(role, route, from_value, action, co_actions))
                if route_producers:
                    edges.append(
                        _edge(
                            role,
                            route,
                            from_value,
                            None,
                            (),
                            completion,
                            route_producers,
                        )
                    )
            else:
                edges.append(_edge(role, route, from_value, None, (), completion))
    return tuple(edges)


def _route_completion_pairs(
    role: PipelineRoles,
    route: TransitionRoute,
) -> tuple[tuple[str, Any], ...]:
    """The route's charted gate pairs — what its completion edge waits on.

    Everything the writer's condition requires besides the channel's own
    from-value and the operator's own buttons (``action_tags`` — pressing one is
    the *alternative* to waiting, never part of the wait).  Recorded chart
    evidence, verbatim and read-side; a route whose gate names nothing else
    records ``()``.
    """
    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, Any]] = []
    for tag, value in (*route.enablers, *route.source_constraints):
        if tag == role.channel_tag or tag in route.action_tags:
            continue
        key = (tag, _value_key(value))
        if key not in seen:
            seen.add(key)
            pairs.append((tag, value))
    return tuple(pairs)


def _route_from_values(role: PipelineRoles, route: TransitionRoute) -> list[Any]:
    """Channel from-states for a route's edges.

    Prefer ``route.from_values`` — read off the writer's own condition, so a
    disjunctive source becomes several edges and a call-site alias never
    pollutes (``StateCompleteBool==1`` aliasing to a spurious ``StateCurrent``).
    Fall back to the single-valued channel source constraints only when the
    writer's condition names no channel value (an alias-gated rung).
    """
    if route.from_values:
        return list(_dedupe_values(list(route.from_values)))
    fallback = [value for tag, value in route.source_constraints if tag == role.channel_tag]
    return list(_dedupe_values(fallback))


def _build_action_lookup(
    routes: tuple[TransitionRoute, ...],
    pdg: Any,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    evidence: Any,
) -> ActionLookup:
    """Map an intermediate value to every primary action that can produce it.

    The values are ordered alternatives, not a batch: ``_edges_from_routes``
    fans them into distinct edges.  Only a route's ``edge_gates`` become
    simultaneous co-actions.
    """
    constraint_tags = {tag for route in routes for tag, _value in route.source_constraints}
    # Enabler tags too: a command's cause is a convergence enabler (``CtrlCmd``),
    # not a source constraint, and bridging it to the steerable button is what
    # makes a command edge fireable.
    constraint_tags |= {tag for route in routes for tag, _value in route.enablers}
    alternatives: dict[tuple[str, str], list[ActionPair]] = {}
    for tag in sorted(constraint_tags):
        if tag not in pdg.tags:
            continue
        for route in expand_routes(tag, pdg, program, steerable, opaque_loop, evidence):
            if route.destination_value is None:
                continue
            pairs = _route_action_pairs(route)
            if pairs:
                key = (tag, _value_key(route.destination_value))
                alternatives.setdefault(key, []).extend(pairs)
    return {key: _dedupe_action_pairs(pairs) for key, pairs in alternatives.items()}


def _constraint_action_pairs(
    role: PipelineRoles,
    route: TransitionRoute,
    action_lookup: ActionLookup,
) -> tuple[ActionPair, ...]:
    pairs: list[ActionPair] = []
    for tag, value in route.source_constraints:
        if tag == role.channel_tag:
            continue
        pairs.extend(action_lookup.get((tag, _value_key(value)), ()))
    return tuple(pairs)


def _route_action_pairs(route: TransitionRoute) -> tuple[ActionPair, ...]:
    pairs: list[ActionPair] = []
    for action_tag in sorted(route.action_tags):
        for tag, value in route.enablers:
            if tag == action_tag:
                pairs.append((tag, value))
                break
    return tuple(pairs)


def _enabler_action_pairs(
    route: TransitionRoute,
    action_lookup: ActionLookup,
) -> tuple[ActionPair, ...]:
    """Bridge a route's non-steerable enablers to steerable actions.

    A command's enabler is a convergence register (``CtrlCmd==1``), not a
    button; the lookup resolves it to the steerable input that drives that value
    (``CmdReset=True``).  Source-constraint bridging is handled separately by
    :func:`_constraint_action_pairs`; this covers the enabler side.
    """
    pairs: list[ActionPair] = []
    for tag, value in route.enablers:
        pairs.extend(action_lookup.get((tag, _value_key(value)), ()))
    return tuple(pairs)


def _edge(
    role: PipelineRoles,
    route: TransitionRoute,
    from_value: Any,
    action: ActionPair | None,
    co_actions: tuple[ActionPair, ...] = (),
    completion: tuple[tuple[str, Any], ...] = (),
    program_producers: tuple[Any, ...] = (),
) -> StaticTransitionEdge:
    return StaticTransitionEdge(
        role=role,
        from_value=from_value,
        to_value=route.destination_value,
        action=action,
        request_tag=route.request_tag,
        request_value=route.request_value,
        source_constraints=route.source_constraints,
        enablers=route.enablers,
        route=route,
        co_actions=co_actions,
        completion=completion,
        program_producers=program_producers,
    )


def _plan_score(plan: StaticPath) -> tuple[int, int, str]:
    # A graph that owns the needed channel outranks a graph which merely lists
    # that tag as one of its request inputs. Otherwise a short observation or
    # indirect-table projection can hijack navigation of the structural carrier
    # before route_options has a chance to rank target ownership.
    direct = 0 if plan.needed_tag == plan.role.channel_tag else 1
    return (direct, len(plan.edges), plan.role.channel_tag)


def _dedupe_values(values: list[Any]) -> tuple[Any, ...]:
    result: list[Any] = []
    for value in values:
        if not any(_values_match(value, seen) for seen in result):
            result.append(value)
    return tuple(result)


def _dedupe_action_pairs(
    pairs: tuple[ActionPair, ...] | list[ActionPair],
) -> tuple[ActionPair, ...]:
    """Keep the first occurrence of each action without changing preference."""

    seen: set[tuple[str, str]] = set()
    result: list[ActionPair] = []
    for tag, value in pairs:
        key = (tag, _value_key(value))
        if key not in seen:
            seen.add(key)
            result.append((tag, value))
    return tuple(result)


def _value_key(value: Any) -> str:
    if value is ANY_FROM:
        return "*"
    return repr(value)


def _edge_matches(edge: StaticTransitionEdge, state: Any) -> bool:
    return edge.from_value is ANY_FROM or _values_match(edge.from_value, state)


def _rank_edges_for_state(
    edges: tuple[StaticTransitionEdge, ...],
    state: Any,
) -> tuple[StaticTransitionEdge, ...]:
    exact: list[StaticTransitionEdge] = []
    wildcard: list[StaticTransitionEdge] = []
    for edge in edges:
        if edge.from_value is ANY_FROM:
            wildcard.append(edge)
        elif _values_match(edge.from_value, state):
            exact.append(edge)
    # Exact source evidence is more specific, so BFS considers it first.  A
    # wildcard remains a matching alternative, however: ``edge_allowed`` may
    # reject every exact edge in this world because of a route constraint,
    # avoid predicate, or contextual nogood.  Dropping wildcard edges here
    # would turn ordering into an unconditional veto before the policy owner
    # has evaluated either alternative.
    return (*exact, *wildcard)


# ===========================================================================
# Opaque-pipeline detection
# ===========================================================================


def _is_declared_mutable_tag(tag: object, pdg: ProgramGraph) -> bool:
    """Filter only tags that the program explicitly marks as immutable."""
    tag_ref = pdg.tags.get(tag) if isinstance(tag, str) else None
    return tag_ref is not None and not tag_ref.readonly


@dataclass(frozen=True)
class PipelineSlice:
    """Steerable tags that may participate in an opaque transition.

    The slice does not choose values.  PILOT turns these tags into concrete
    actions using the current snapshot, known domains, and trace-derived needs.
    """

    action_tags: frozenset[str]


def _find_convergent_steers(
    opaque_tag: str,
    pdg: ProgramGraph,
    steerable: frozenset[str],
    *,
    max_hops: int = 8,
    min_writers: int = 2,
) -> frozenset[str]:
    """Bounded upstream BFS to find convergence-point steerable inputs.

    A convergence point is an intermediate tag written by multiple rungs
    where each writer is conditioned on a different steerable input
    (e.g. ``C_CtrlCmd`` written by 10 rungs, each gated by a different
    command button).  Returns the union of those steerable condition reads.

    Falls back to the full ``upstream_slice & steerable`` if no
    convergence point is found within *max_hops*.
    """
    visited_tags: set[str] = set()
    visited_rungs: set[int] = set()
    queue: list[tuple[str, int]] = [(opaque_tag, 0)]
    convergent: set[str] = set()

    while queue:
        tag, depth = queue.pop(0)
        if tag in visited_tags or depth > max_hops:
            continue
        visited_tags.add(tag)
        tag_steer_conds: set[str] = set()
        for ri in pdg.writers_of.get(tag, frozenset()):
            if ri in visited_rungs:
                continue
            visited_rungs.add(ri)
            node = pdg.rung_nodes[ri]
            tag_steer_conds |= node.condition_reads & steerable
            for rt in node.condition_reads | node.data_reads:
                if rt not in visited_tags:
                    queue.append((rt, depth + 1))
        if len(tag_steer_conds) >= min_writers:
            convergent |= tag_steer_conds

    if convergent:
        return frozenset(convergent)
    return pdg.upstream_slice(opaque_tag) & steerable


def _scan_indirect_copy_targets(program: Any) -> set[str]:
    """Destination tag names of ``copy(block[ptr], tag)`` indirect copies."""
    from pyrung.core.instruction.data_transfer import CopyInstruction
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef

    targets: set[str] = set()

    def _scan(rungs: Any) -> None:
        for r in rungs:
            for instr in getattr(r, "_instructions", ()):
                if isinstance(instr, CopyInstruction) and isinstance(
                    instr.source, (IndirectRef, IndirectExprRef)
                ):
                    dest_name = getattr(instr.dest, "name", None)
                    if dest_name:
                        targets.add(dest_name)
            _scan(getattr(r, "_branches", ()))

    _scan(program.rungs)
    for sub_rungs in getattr(program, "subroutines", {}).values():
        _scan(sub_rungs)
    return targets


def detect_opaque_loop(
    pdg: ProgramGraph,
    program: Any,
    *,
    max_hops: int = 3,
) -> frozenset[str]:
    """Tags in a feedback loop through an opaque (indirect-copy) pipeline.

    These are the jump-table state-machine registers (``S_StateCurrent``,
    ``isStateEnbl_Yes``, ``S_StateRequested``, the ``S_<state>`` flags,
    ``C_CtrlCmd`` …) that mutually drive each other through the indirect-copy
    machinery.  ``trace_back`` must not invert them as a finite prerequisite
    chain — it walks the entire state-transition graph backward (e.g.
    ``StateCurrent=6 → enable → StateRequested=2 → Stopping → StateCurrent=7
    → …``), scrambling depth and inflating the unsatisfied count.  They are
    compass territory: learned by observation, not static inversion.

    A tag qualifies when it is BOTH within *max_hops* downstream of an
    indirect-copy target AND upstream of one — i.e. it participates in the
    loop.  Simple state machines built from direct copies have no
    indirect-copy targets, so this returns empty and ``trace_back`` is
    unaffected.
    """
    targets = _scan_indirect_copy_targets(program)
    if not targets:
        return frozenset()

    # Bounded downstream BFS: tag -> rungs reading it -> their written tags.
    seen: set[str] = set(targets)
    frontier: set[str] = set(targets)
    for _ in range(max_hops):
        nxt: set[str] = set()
        for tag in frontier:
            for ri in pdg.readers_of.get(tag, frozenset()):
                for w in pdg.rung_nodes[ri].all_writes:
                    if w not in seen:
                        seen.add(w)
                        nxt.add(w)
        frontier = nxt

    upstream: set[str] = set()
    for t in targets:
        upstream |= pdg.upstream_slice(t)

    return frozenset(seen & upstream)


def detect_opaque_pipelines(
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
) -> list[PipelineSlice]:
    """Find indirect-copy write targets and their steerable upstream tags.

    Scans the program for ``CopyInstruction`` with ``IndirectRef`` or
    ``IndirectExprRef`` sources (the ``copy(block[ptr], tag)`` pattern).
    For each, follows downstream via the PDG to find affected output tags,
    and uses convergence-point detection to find the steerable inputs that
    actually enter the pipeline (not the full upstream cone).

    Deduplicates slices that share the same free args (e.g. multiple
    indirect copies in the same subroutine).
    """
    opaque_targets = _scan_indirect_copy_targets(program)
    if not opaque_targets:
        return []

    # Deduplicate: multiple opaque targets may share convergent steers.
    seen_tags: set[frozenset[str]] = set()
    slices: list[PipelineSlice] = []
    for opaque_tag in sorted(opaque_targets):
        action_tags = frozenset(
            tag
            for tag in _find_convergent_steers(opaque_tag, pdg, steerable)
            if _is_declared_mutable_tag(tag, pdg)
        )
        if not action_tags or action_tags in seen_tags:
            continue
        seen_tags.add(action_tags)
        slices.append(PipelineSlice(action_tags=frozenset(action_tags)))
        logger.info(
            "pilot: opaque pipeline (%s) -> %d action tags: %s",
            opaque_tag,
            len(action_tags),
            sorted(action_tags),
        )

    return slices
