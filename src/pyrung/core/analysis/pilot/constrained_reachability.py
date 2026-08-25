"""Constrained, non-action-selecting reachability evidence queries."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, cast

from pyrung.core.analysis.pilot.avoid import _avoid_forces
from pyrung.core.analysis.pilot.navigation_contracts import (
    EvidenceScope,
    NavigationConstraints,
    OrientationWorld,
    TargetSpec,
    _ActionPair,
    is_action,
    is_composite_action,
    pulse_identity,
)
from pyrung.core.analysis.pilot.pipeline_graph import _best_static_path
from pyrung.core.analysis.pilot.world_key import wait_edge_nogood
from pyrung.core.analysis.sp_values import _values_match


@dataclass(frozen=True)
class Reachable:
    """At least one fully constrained continuation is known."""

    provenance: tuple[str, ...]


@dataclass(frozen=True)
class Unknown:
    """No continuation is currently known and the evidence is incomplete."""

    reason: str
    frontier: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class NoRoute:
    """Complete evidence proves no constrained continuation exists."""

    proof: str


FrontierStatus = Reachable | Unknown | NoRoute


class StaticEdgeExclusionReason(StrEnum):
    """Machine-readable reasons a static chart edge cannot join a path."""

    STATIC_STATUS = "static_status"
    PAIR_NOGOOD = "pair_nogood"
    WAIT_NOGOOD = "wait_nogood"
    PULSE_NOGOOD = "pulse_nogood"
    ROUTE_BLOCKED = "route_blocked"
    AVOID_FORCED = "avoid_forced"


@dataclass(frozen=True)
class StaticEdgeExclusion:
    """One exact reason and artifact excluded from a static path search."""

    reason: StaticEdgeExclusionReason
    evidence: tuple[Any, ...]


@dataclass(frozen=True)
class StaticEdgeAdmission:
    """Whether one static chart edge may participate in this world's path search."""

    exclusions: tuple[StaticEdgeExclusion, ...] = ()

    @property
    def allowed(self) -> bool:
        """Boolean projection consumed by the graph path APIs."""

        return not self.exclusions


class NavigationKnowledge(Protocol):
    """Read-only Compass knowledge consumed by constrained evidence queries."""

    def live_edges(
        self,
        tag: str,
        *,
        world_key: tuple[Any, ...],
        snapshot: dict[str, Any],
    ) -> dict[Any, Any]: ...

    def nogood_pairs(self, world_key: tuple[Any, ...]) -> frozenset[Any]: ...

    def static_edge_status(
        self,
        edge: Any,
        *,
        evidence_scope: EvidenceScope | None,
    ) -> str | None: ...

    def act_is_nogood(self, world_key: tuple[Any, ...], identity: tuple[Any, ...]) -> bool: ...


def _learned_reachable(
    world: OrientationWorld,
    target: TargetSpec,
    constraints: NavigationConstraints,
    knowledge: NavigationKnowledge,
) -> bool:
    current = world.snapshot.get(target.tag)
    if _values_match(current, target.value):
        return True
    live = knowledge.live_edges(
        target.tag,
        world_key=world.world_key,
        snapshot=world.snapshot,
    )
    queue: deque[Any] = deque([current])
    visited = {repr(current)}
    pair_nogoods = knowledge.nogood_pairs(world.world_key)
    while queue:
        state = queue.popleft()
        for (from_value, cause), destination in live.items():
            if not _values_match(from_value, state):
                continue
            if not NavigationEvidence.learned_cause_allowed(
                target.tag,
                from_value,
                cause,
                destination,
                world_key=world.world_key,
                snapshot=world.snapshot,
                knowledge=knowledge,
                context=world.context,
                blocked_actions=constraints.blocked_actions,
                pair_nogoods=pair_nogoods,
            ):
                continue
            if _values_match(destination, target.value):
                return True
            key = repr(destination)
            if key not in visited:
                visited.add(key)
                queue.append(destination)
    return False


class NavigationEvidence:
    """Shared constrained evidence layer for orientation and verification."""

    @staticmethod
    def learned_cause_allowed(
        tag: str,
        source: Any,
        cause: Any,
        destination: Any,
        *,
        world_key: tuple[Any, ...] | None,
        snapshot: dict[str, Any],
        knowledge: NavigationKnowledge,
        context: Any,
        blocked_actions: frozenset[_ActionPair] = frozenset(),
        pair_nogoods: set[Any] | frozenset[Any] | None = None,
    ) -> bool:
        """Whether one complete learned cause may enter a current-world path."""

        if pair_nogoods is None:
            current_nogoods = (
                knowledge.nogood_pairs(world_key) if world_key is not None else frozenset()
            )
        else:
            current_nogoods = frozenset(pair_nogoods)
        if not is_action(cause):
            return wait_edge_nogood(tag, source, destination) not in current_nogoods

        members = cast(tuple[_ActionPair, ...], cause) if is_composite_action(cause) else (cause,)
        return (
            all(pair not in blocked_actions and pair not in current_nogoods for pair in members)
            and not _avoid_forces(context, members, snapshot)
            and not (
                world_key is not None
                and knowledge.act_is_nogood(world_key, pulse_identity(members))
            )
        )

    @staticmethod
    def static_edge_admission(
        edge: Any,
        *,
        world_key: tuple[Any, ...] | None,
        snapshot: dict[str, Any],
        knowledge: NavigationKnowledge,
        context: Any,
        evidence_scope: EvidenceScope | None,
        blocked_actions: frozenset[tuple[str, Any]] = frozenset(),
        pair_nogoods: set[tuple[str, Any]] | frozenset[tuple[str, Any]] | None = None,
    ) -> StaticEdgeAdmission:
        """Decide whether one chart edge may join a current-world path search.

        Evaluates the complete required action overlay, including co-actions,
        and returns machine-readable exclusions for static status,
        current-world pair/wait/Pulse nogoods, blocked required actions, and
        avoid.  Graph search consumes only the ``allowed`` projection;
        options keeps unavailable-producer edges local to its candidate read.
        """

        exclusions: list[StaticEdgeExclusion] = []
        status = knowledge.static_edge_status(
            edge,
            evidence_scope=evidence_scope,
        )
        if status in {"contradicted", "no_change"}:
            exclusions.append(
                StaticEdgeExclusion(
                    StaticEdgeExclusionReason.STATIC_STATUS,
                    (status,),
                )
            )

        if pair_nogoods is None:
            current_nogoods = (
                knowledge.nogood_pairs(world_key) if world_key is not None else frozenset()
            )
        else:
            current_nogoods = frozenset(pair_nogoods)
        if edge.action is None:
            wait_identity = wait_edge_nogood(
                edge.role.channel_tag,
                edge.from_value,
                edge.to_value,
            )
            if wait_identity in current_nogoods:
                exclusions.append(
                    StaticEdgeExclusion(
                        StaticEdgeExclusionReason.WAIT_NOGOOD,
                        (wait_identity,),
                    )
                )
        else:
            required_actions = (edge.action, *edge.co_actions)
            exclusions.extend(
                StaticEdgeExclusion(
                    StaticEdgeExclusionReason.PAIR_NOGOOD,
                    (action,),
                )
                for action in required_actions
                if action in current_nogoods
            )
            pulse_artifact = pulse_identity(required_actions)
            if world_key is not None and knowledge.act_is_nogood(world_key, pulse_artifact):
                exclusions.append(
                    StaticEdgeExclusion(
                        StaticEdgeExclusionReason.PULSE_NOGOOD,
                        (pulse_artifact,),
                    )
                )
            exclusions.extend(
                StaticEdgeExclusion(
                    StaticEdgeExclusionReason.ROUTE_BLOCKED,
                    (action,),
                )
                for action in required_actions
                if action in blocked_actions
            )
            if _avoid_forces(context, required_actions, snapshot):
                exclusions.append(
                    StaticEdgeExclusion(
                        StaticEdgeExclusionReason.AVOID_FORCED,
                        (required_actions,),
                    )
                )

        return StaticEdgeAdmission(
            exclusions=tuple(exclusions),
        )

    @staticmethod
    def frontier_status(
        world: OrientationWorld,
        target: TargetSpec,
        constraints: NavigationConstraints,
        knowledge: NavigationKnowledge,
    ) -> FrontierStatus:
        compass = world.context.compass
        evidence_scope = EvidenceScope.capture(
            world.world_key,
            world.snapshot.items(),
        )

        def edge_allowed(edge: Any) -> bool:
            admission = NavigationEvidence.static_edge_admission(
                edge,
                world_key=world.world_key,
                snapshot=world.snapshot,
                knowledge=knowledge,
                context=world.context,
                evidence_scope=evidence_scope,
                blocked_actions=constraints.blocked_actions,
            )
            return admission.allowed

        static = _best_static_path(
            target.tag,
            target.value,
            world.snapshot,
            compass.catalog.graphs,
            edge_allowed=edge_allowed,
        )
        learned = _learned_reachable(world, target, constraints, knowledge)
        provenance: list[str] = []
        if static is not None:
            provenance.append("static")
        if learned:
            provenance.append("empirical")
        if provenance:
            return Reachable(tuple(provenance))

        frontier = tuple(
            (node.tag, node.value) for node in world.frame.tree.leaves() if not node.satisfied
        )
        if frontier:
            return Unknown("no constrained route is currently established", frontier)
        return NoRoute("complete trace has no outstanding reachable frontier")

    @staticmethod
    def channel_continuation(
        graphs: tuple[Any, ...],
        channel_tag: str,
        start: Any,
        goals: tuple[Any, ...],
        *,
        edge_allowed: Any,
    ) -> FrontierStatus:
        """Whether any static channel graph has a caller-constrained continuation.

        The inspected path is intentionally discarded: recovery may classify
        the evidence, but it cannot execute or retain a suffix.
        """

        if any(_values_match(start, goal) for goal in goals):
            return Reachable(("already-at-goal",))
        saw_graph = False
        for graph in graphs:
            if graph.role.channel_tag != channel_tag:
                continue
            saw_graph = True
            if graph.find_path(start, goals, edge_allowed=edge_allowed) is not None:
                return Reachable(("static-channel",))
        if saw_graph:
            return NoRoute("all constrained channel continuations are excluded")
        return Unknown("no transition graph exists for the channel")
