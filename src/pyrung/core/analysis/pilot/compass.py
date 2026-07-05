"""compass.py — the bearing layer of PILOT.

The compass is the generalized transition graph that PILOT navigates by. It is a
*bearing*, not a route: it keeps pointing at the target while the pilot is free
to detour. See ``pilot/CLAUDE.md``.

Edges carry two generalized axes:

* **driver** — *how the edge fires*: an ``Action`` ``(tag, value)`` to pulse, a
  ``WaitCause`` (let-run — hold state and let scans coast), or a request to be
  re-entered into the trace.
* **provenance** — *how the edge was learned*: a static route read by ``trace``,
  a runtime observation from ``sandbox``, or a learned transition.

One :class:`Compass` object holds them all: the static per-register value graph
(``CompassGraph``) plus the learned transition table (formerly ``InfluenceMap``).
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeGuard

from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionRoute, expand_routes
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph

logger = logging.getLogger(__name__)

ActionPair = tuple[str, Any]
Action = ActionPair
ANY_FROM = object()


# ===========================================================================
# Drivers — how an edge fires
# ===========================================================================


@dataclass(frozen=True)
class WaitCause:
    """Let-run driver: a transition caused by time passing, not a tag write."""

    def __repr__(self) -> str:
        return "WAIT"


WAIT = WaitCause()
TransitionCause = Action | WaitCause


def is_action(cause: TransitionCause) -> TypeGuard[Action]:
    return isinstance(cause, tuple)


def is_composite_action(cause: Any) -> bool:
    """A skiff-learned *joint* cause: a tuple of action pairs that must fire in
    one window (``((tag, val), (tag, val))``), as opposed to a single
    ``(tag, val)`` action.  Both satisfy :func:`is_action`; this distinguishes
    the shape so consumers can propose the members as one batch."""
    return (
        isinstance(cause, tuple)
        and len(cause) > 0
        and all(isinstance(m, tuple) and len(m) == 2 and isinstance(m[0], str) for m in cause)
        and not isinstance(cause[0], str)
    )


# ===========================================================================
# Static value-graph (provenance: static route) — folded in from compass.py
# ===========================================================================


@dataclass(frozen=True)
class CompassEdge:
    """One normalized transition edge for a governing pipeline register.

    ``action`` is the primary steerable pulse that fires the edge (the command
    button, bridged from a non-steerable ``CtrlCmd``-style convergence enabler).
    ``co_actions`` are the steerable inputs that must fire *in the same scan* as
    ``action`` — the one-shot edge gate (``rise(CmdChgRequest)``) — without which
    the command rung never executes.  Completion (let-run) edges carry neither.
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


@dataclass(frozen=True)
class CompassPlan:
    """A BFS route through one pipeline's transition graph."""

    needed_tag: str
    needed_value: Any
    role: PipelineRoles
    target_value: Any
    edges: tuple[CompassEdge, ...]

    @property
    def first_edge(self) -> CompassEdge:
        return self.edges[0]


class CompassGraph:
    """Value graph for one :class:`PipelineRoles` owner."""

    def __init__(
        self,
        role: PipelineRoles,
        routes: tuple[TransitionRoute, ...],
        action_lookup: dict[tuple[str, str], tuple[ActionPair, ...]] | None = None,
    ) -> None:
        self.role = role
        self.routes = routes
        self.edges = _edges_from_routes(role, routes, action_lookup or {})

    def target_values_for_need(self, needed_tag: str, needed_value: Any) -> tuple[Any, ...]:
        values: list[Any] = []
        for route in self.routes:
            if route.destination_value is None:
                continue
            if needed_tag == self.role.governing_tag and _values_match(
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

    def find_path(self, from_value: Any, target_values: tuple[Any, ...]) -> CompassPlan | None:
        if not target_values:
            return None
        if any(_values_match(from_value, target) for target in target_values):
            return None

        queue: deque[tuple[Any, tuple[CompassEdge, ...]]] = deque([(from_value, ())])
        visited = {_value_key(from_value)}
        while queue:
            state, path = queue.popleft()
            for edge in _rank_edges_for_state(self.edges, state):
                if not _edge_matches(edge, state):
                    continue
                key = _value_key(edge.to_value)
                if key in visited:
                    continue
                next_path = (*path, edge)
                if any(_values_match(edge.to_value, target) for target in target_values):
                    return CompassPlan(
                        needed_tag="",
                        needed_value=None,
                        role=self.role,
                        target_value=edge.to_value,
                        edges=next_path,
                    )
                visited.add(key)
                queue.append((edge.to_value, next_path))
        return None


def build_compass_graphs(
    roles: tuple[PipelineRoles, ...],
    pdg: Any,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    evidence: Any,
) -> tuple[CompassGraph, ...]:
    graphs: list[CompassGraph] = []
    for role in roles:
        routes = tuple(
            expand_routes(
                role.governing_tag,
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
        graph = CompassGraph(role, routes, action_lookup)
        if graph.edges:
            graphs.append(graph)
    return tuple(graphs)


def best_compass_plan(
    needed_tag: str,
    needed_value: Any,
    snapshot: dict[str, Any],
    graphs: tuple[CompassGraph, ...],
) -> CompassPlan | None:
    """Best known pipeline path for a need, if any."""

    plans: list[CompassPlan] = []
    for graph in graphs:
        if needed_tag != graph.role.governing_tag and needed_tag not in graph.role.request_tags:
            continue
        current = snapshot.get(graph.role.governing_tag)
        targets = graph.target_values_for_need(needed_tag, needed_value)
        plan = graph.find_path(current, targets)
        if plan is None:
            continue
        plans.append(
            CompassPlan(
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
    action_lookup: dict[tuple[str, str], tuple[ActionPair, ...]],
) -> tuple[CompassEdge, ...]:
    edges: list[CompassEdge] = []
    for route in routes:
        if route.destination_value is None:
            continue
        from_values = _route_from_values(role, route)
        # Actions: directly-steerable enablers, plus enablers bridged through a
        # convergence pipeline to a steerable button (``CtrlCmd==1 -> CmdReset``).
        action_pairs = _route_action_pairs(route) + _enabler_action_pairs(route, action_lookup)
        if not action_pairs:
            action_pairs = _constraint_action_pairs(role, route, action_lookup)
        # Co-actions ride only on action-bearing (command) edges; the one-shot
        # edge gate (``rise(CmdChgRequest)``) must fire the same scan as the
        # button.  Completion edges have no edge gates → coast.
        co_actions = tuple(route.edge_gates)
        if not from_values and action_pairs:
            from_values = [ANY_FROM]
        if not from_values:
            continue
        for from_value in from_values:
            if action_pairs:
                for action in action_pairs:
                    edges.append(_edge(role, route, from_value, action, co_actions))
            else:
                edges.append(_edge(role, route, from_value, None, ()))
    return tuple(edges)


def _route_from_values(role: PipelineRoles, route: TransitionRoute) -> list[Any]:
    """Governing from-states for a route's edges.

    Prefer ``route.from_values`` — read off the writer's own condition, so a
    disjunctive source becomes several edges and a call-site alias never
    pollutes (``StateCompleteBool==1`` aliasing to a spurious ``StateCurrent``).
    Fall back to the single-valued governing source constraints only when the
    writer's condition names no governing value (an alias-gated rung).
    """
    if route.from_values:
        return list(_dedupe_values(list(route.from_values)))
    fallback = [value for tag, value in route.source_constraints if tag == role.governing_tag]
    return list(_dedupe_values(fallback))


def _build_action_lookup(
    routes: tuple[TransitionRoute, ...],
    pdg: Any,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    evidence: Any,
) -> dict[tuple[str, str], tuple[ActionPair, ...]]:
    constraint_tags = {tag for route in routes for tag, _value in route.source_constraints}
    # Enabler tags too: a command's cause is a convergence enabler (``CtrlCmd``),
    # not a source constraint, and bridging it to the steerable button is what
    # makes a command edge fireable.
    constraint_tags |= {tag for route in routes for tag, _value in route.enablers}
    lookup: dict[tuple[str, str], tuple[ActionPair, ...]] = {}
    for tag in sorted(constraint_tags):
        if tag not in pdg.tags:
            continue
        for route in expand_routes(tag, pdg, program, steerable, opaque_loop, evidence):
            if route.destination_value is None:
                continue
            pairs = _route_action_pairs(route)
            if pairs:
                lookup[(tag, _value_key(route.destination_value))] = pairs
    return lookup


def _constraint_action_pairs(
    role: PipelineRoles,
    route: TransitionRoute,
    action_lookup: dict[tuple[str, str], tuple[ActionPair, ...]],
) -> tuple[ActionPair, ...]:
    pairs: list[ActionPair] = []
    for tag, value in route.source_constraints:
        if tag == role.governing_tag:
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
    action_lookup: dict[tuple[str, str], tuple[ActionPair, ...]],
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
) -> CompassEdge:
    return CompassEdge(
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
    )


def _plan_score(plan: CompassPlan) -> tuple[int, int, str]:
    # Direct governing needs win ties over request-owned needs.
    direct = 0 if plan.needed_tag == plan.role.governing_tag else 1
    return (len(plan.edges), direct, plan.role.governing_tag)


def _dedupe_values(values: list[Any]) -> tuple[Any, ...]:
    result: list[Any] = []
    for value in values:
        if not any(_values_match(value, seen) for seen in result):
            result.append(value)
    return tuple(result)


def _value_key(value: Any) -> str:
    if value is ANY_FROM:
        return "*"
    return repr(value)


def _edge_matches(edge: CompassEdge, state: Any) -> bool:
    return edge.from_value is ANY_FROM or _values_match(edge.from_value, state)


def _rank_edges_for_state(
    edges: tuple[CompassEdge, ...],
    state: Any,
) -> tuple[CompassEdge, ...]:
    exact: list[CompassEdge] = []
    wildcard: list[CompassEdge] = []
    for edge in edges:
        if edge.from_value is ANY_FROM:
            wildcard.append(edge)
        elif _values_match(edge.from_value, state):
            exact.append(edge)
    if exact:
        return tuple(exact)
    return tuple(wildcard)


# ===========================================================================
# Opaque-pipeline detection (folded in from influence.py)
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


# ===========================================================================
# The unified graph — static value graph + learned transitions in one object
# (absorbs the former InfluenceMap)
# ===========================================================================


class Compass:
    """One transition graph for a program, navigated by PILOT.

    Holds the static per-register value graph (``CompassGraph`` per role) and a
    learned transition table seeded at startup from static routes and extended
    by runtime observations.  An ``Action`` cause is a ``(tag, value)`` pair; a
    ``WaitCause`` is a let-run (time-passing) transition.
    """

    def __init__(self, slices: list[PipelineSlice] | None = None) -> None:
        self._slices: list[PipelineSlice] = list(slices or [])
        self._action_tags: frozenset[str] = (
            frozenset().union(*(s.action_tags for s in self._slices))
            if self._slices
            else frozenset()
        )
        self._transitions: dict[str, dict[tuple[Any, TransitionCause], Any]] = {}
        self._probed: dict[str, set[tuple[Any, TransitionCause]]] = {}
        self._graphs: tuple[CompassGraph, ...] = ()

    # -- static value-graph side --------------------------------------------

    @property
    def graphs(self) -> tuple[CompassGraph, ...]:
        return self._graphs

    def set_graphs(self, graphs: tuple[CompassGraph, ...]) -> None:
        self._graphs = graphs

    def best_plan(
        self,
        needed_tag: str,
        needed_value: Any,
        snapshot: dict[str, Any],
    ) -> CompassPlan | None:
        """Best static value-graph path for a need, if any."""
        return best_compass_plan(needed_tag, needed_value, snapshot, self._graphs)

    def seed_routes(self, target_tag: str, routes: list[TransitionRoute]) -> int:
        """Seed learned transitions from statically-known routes.

        Only seeds routes where both a source constraint on *target_tag*
        (giving the ``from_val``) and a steerable action tag in the enablers
        (giving the ``cause``) are known.  Returns the number of entries seeded.
        """
        seeded = 0
        for route in routes:
            if route.destination_value is None:
                continue
            from_val: Any = None
            for tag, value in route.source_constraints:
                if tag == target_tag:
                    from_val = value
                    break
            if from_val is None:
                continue
            for action_tag in sorted(route.action_tags):
                for tag, value in route.enablers:
                    if tag == action_tag:
                        self.record(
                            target_tag, (action_tag, value), from_val, route.destination_value
                        )
                        seeded += 1
                        break
        if seeded:
            logger.info("compass: seeded %d transitions for %s", seeded, target_tag)
        return seeded

    # -- learned transition side (was InfluenceMap) -------------------------

    @property
    def action_tags(self) -> frozenset[str]:
        return self._action_tags

    def has_transitions(self, tag: str) -> bool:
        return tag in self._transitions

    def record(
        self,
        tag: str,
        cause: TransitionCause,
        from_val: Any,
        to_val: Any,
    ) -> None:
        table = self._transitions.setdefault(tag, {})
        table[(from_val, cause)] = to_val
        self._probed.setdefault(tag, set()).add((from_val, cause))

    def record_no_change(self, tag: str, cause: TransitionCause, from_val: Any) -> None:
        self._probed.setdefault(tag, set()).add((from_val, cause))

    def contradict(self, tag: str, cause: TransitionCause, from_val: Any) -> bool:
        """Live evidence falsified a learned edge — remove it.

        A statically-seeded route (``seed_routes``) records the writer's edge
        without its unreadable enablers; when the live trial applies *cause*
        from *from_val* and the register does NOT reach the recorded
        destination, the entry is a disproven hypothesis and must not keep
        shadowing genuine (skiff-learned) edges in ``find_path``.  The probe
        mark stays — the cause was genuinely tried.  Returns True if an entry
        was removed.
        """
        table = self._transitions.get(tag)
        removed = False
        if table is not None:
            for key in [k for k in table if k[1] == cause and _values_match(k[0], from_val)]:
                del table[key]
                removed = True
        self._probed.setdefault(tag, set()).add((from_val, cause))
        return removed

    def find_path(
        self,
        tag: str,
        from_val: Any,
        to_val: Any,
    ) -> list[TransitionCause] | None:
        """BFS shortest transition-cause sequence through the learned table."""
        table = self._transitions.get(tag)
        if not table:
            return None
        if _values_match(from_val, to_val):
            return []

        queue: deque[tuple[Any, list[TransitionCause]]] = deque([(from_val, [])])
        visited: set[Any] = {from_val}

        while queue:
            state, path = queue.popleft()
            for (s, cause), dest in table.items():
                if not _values_match(s, state):
                    continue
                if dest in visited:
                    continue
                new_path = [*path, cause]
                if _values_match(dest, to_val):
                    return new_path
                visited.add(dest)
                queue.append((dest, new_path))

        return None

    def unprobed_actions(
        self,
        tag: str,
        from_val: Any,
        available_actions: set[Action] | frozenset[Action],
    ) -> list[Action]:
        """Available actions not yet tried from *from_val* for *tag*."""
        return sorted(available_actions - self.probed_actions(tag, from_val))

    def probed_actions(self, tag: str, from_val: Any) -> set[Action]:
        """Actions already probed from *from_val* for *tag*."""
        return {
            cause
            for (fv, cause) in self._probed.get(tag, set())
            if fv == from_val and is_action(cause)
        }

    def transition_dest(
        self,
        tag: str,
        from_val: Any,
        cause: TransitionCause,
    ) -> Any | None:
        """Observed destination for one transition cause from *from_val*."""
        for (fv, candidate_cause), dest in self._transitions.get(tag, {}).items():
            if candidate_cause == cause and _values_match(fv, from_val):
                return dest
        return None

    def off_path_actions(self, tag: str, from_val: Any, to_val: Any) -> set[Action]:
        """Actions known to move *tag* away from the BFS path toward *to_val*.

        Once we know the shortest path, any action from the current state
        that goes to a state NOT on that path (or with no path to the
        target) is off-path and should be tried after path actions.
        """
        path = self.find_path(tag, from_val, to_val)
        if not path:
            return set()
        good_cause = path[0]
        table = self._transitions.get(tag, {})

        # Compute states on the BFS path
        on_path: set[Any] = {from_val}
        state = from_val
        for cause in path:
            dest = table.get((state, cause))
            if dest is not None:
                on_path.add(dest)
                state = dest

        off_path: set[Action] = set()
        for (fv, cause), dest in table.items():
            if not _values_match(fv, from_val):
                continue
            if cause == good_cause or not is_action(cause):
                continue
            if dest not in on_path:
                off_path.add(cause)
        return off_path
