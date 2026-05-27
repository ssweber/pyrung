"""Transition graph and reachability path-finding for explored state spaces."""

from __future__ import annotations

import heapq
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    StateKey = tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class TransitionEdge:
    source_key: tuple[Any, ...]
    dest_key: tuple[Any, ...]
    inputs: dict[str, Any]
    scans: int
    caveats: tuple[str, ...] = ()
    dest_tags: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ReachabilityStep:
    action: dict[str, Any]
    source_key: tuple[Any, ...]
    dest_key: tuple[Any, ...]
    scans: int
    intermediates: tuple[Any, ...] = ()


@dataclass(frozen=True)
class Path:
    reachable: bool
    steps: tuple[ReachabilityStep, ...]
    total_changes: int
    total_scans: int
    reason: str | None = None

    def __str__(self) -> str:
        if not self.reachable:
            return f"Unreachable: {self.reason}"
        if not self.steps:
            return "Already at target state"
        lines = [f"Path ({len(self.steps)} step(s), {self.total_changes} input change(s)):"]
        for i, step in enumerate(self.steps, 1):
            inputs = ", ".join(f"{k}={v}" for k, v in sorted(step.action.items()))
            if inputs:
                lines.append(f"  Step {i}: {inputs}  ({step.scans} scan(s))")
            else:
                lines.append(f"  Step {i}: (wait)  ({step.scans} scan(s))")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return (
            f"Path(reachable={self.reachable}, steps={len(self.steps)}, "
            f"total_changes={self.total_changes}, total_scans={self.total_scans})"
        )


class TransitionGraph:
    """Complete transition graph from BFS state-space exploration.

    Nodes are compressed state keys (hashable tuples).  Edges record which
    input assignment caused each transition and how many scans it took.
    """

    __slots__ = ("_adjacency", "_state_tags", "_initial_key", "_tag_names")

    def __init__(
        self,
        adjacency: dict[tuple[Any, ...], list[TransitionEdge]],
        state_tags: dict[tuple[Any, ...], dict[str, Any]],
        initial_key: tuple[Any, ...],
        tag_names: frozenset[str],
    ) -> None:
        self._adjacency = adjacency
        self._state_tags = state_tags
        self._initial_key = initial_key
        self._tag_names = tag_names

    @property
    def state_count(self) -> int:
        return len(self._state_tags)

    @property
    def edge_count(self) -> int:
        return sum(len(edges) for edges in self._adjacency.values())

    @property
    def initial_key(self) -> tuple[Any, ...]:
        return self._initial_key

    def state_tags(self, key: tuple[Any, ...]) -> dict[str, Any]:
        return dict(self._state_tags[key])

    def find_matching_keys(
        self,
        tags: dict[str, Any],
        *,
        exclude: frozenset[str] = frozenset(),
    ) -> list[tuple[Any, ...]]:
        """Find all graph keys whose stored tag values match *tags*.

        Args:
            tags: Tag name → value mapping to match against.
            exclude: Tag names to skip during comparison (e.g. external
                inputs whose snapshot values are incidental, not
                identity-forming).
        """
        result: list[tuple[Any, ...]] = []
        for key, stored in self._state_tags.items():
            if all(
                tags.get(name) == value
                for name, value in stored.items()
                if name not in exclude and name in tags
            ):
                result.append(key)
        return result

    # ------------------------------------------------------------------
    # Path-finding
    # ------------------------------------------------------------------

    def shortest_path(
        self,
        target_predicate: Callable[[dict[str, Any]], bool],
        *,
        source_key: tuple[Any, ...] | None = None,
        avoid: Callable[[dict[str, Any]], bool] | None = None,
        max_steps: int = 20,
        minimize: Literal["steps", "changes"] = "steps",
    ) -> Path:
        source = source_key if source_key is not None else self._initial_key

        if source not in self._state_tags:
            return Path(
                reachable=False,
                steps=(),
                total_changes=0,
                total_scans=0,
                reason="source state not in graph",
            )

        if target_predicate(self._state_tags[source]):
            return Path(reachable=True, steps=(), total_changes=0, total_scans=0)

        if minimize == "steps":
            return self._bfs_shortest(source, target_predicate, avoid, max_steps)
        return self._dijkstra_shortest(source, target_predicate, avoid, max_steps)

    def _edge_tags(self, edge: TransitionEdge) -> dict[str, Any] | None:
        if edge.dest_tags is not None:
            return edge.dest_tags
        return self._state_tags.get(edge.dest_key)

    def _bfs_shortest(
        self,
        source: tuple[Any, ...],
        target_pred: Callable[[dict[str, Any]], bool],
        avoid: Callable[[dict[str, Any]], bool] | None,
        max_steps: int,
    ) -> Path:
        queue: deque[tuple[tuple[Any, ...], int]] = deque([(source, 0)])
        parent: dict[tuple[Any, ...], tuple[tuple[Any, ...], TransitionEdge] | None] = {
            source: None
        }

        while queue:
            current, depth = queue.popleft()
            if depth >= max_steps:
                continue
            for edge in self._adjacency.get(current, ()):
                dest = edge.dest_key
                dest_tags = self._edge_tags(edge)
                if dest_tags is None:
                    continue
                if avoid is not None and avoid(dest_tags):
                    continue
                if target_pred(dest_tags):
                    parent[dest] = (current, edge)
                    return self._reconstruct(parent, dest)
                if dest in parent:
                    continue
                parent[dest] = (current, edge)
                queue.append((dest, depth + 1))

        return Path(
            reachable=False,
            steps=(),
            total_changes=0,
            total_scans=0,
            reason="no path within max_steps" if max_steps < 1000 else "no path exists",
        )

    def _dijkstra_shortest(
        self,
        source: tuple[Any, ...],
        target_pred: Callable[[dict[str, Any]], bool],
        avoid: Callable[[dict[str, Any]], bool] | None,
        max_steps: int,
    ) -> Path:
        dist: dict[tuple[Any, ...], int] = {source: 0}
        parent: dict[tuple[Any, ...], tuple[tuple[Any, ...], TransitionEdge] | None] = {
            source: None
        }
        counter = 0
        heap: list[tuple[int, int, tuple[Any, ...]]] = [(0, counter, source)]

        while heap:
            cost, _, current = heapq.heappop(heap)
            if cost > dist.get(current, float("inf")):  # type: ignore[arg-type]
                continue

            # Count steps to bound depth.
            steps_so_far = 0
            node = current
            while parent.get(node) is not None:
                steps_so_far += 1
                node = parent[node][0]  # type: ignore[index]
            if steps_so_far >= max_steps:
                continue

            for edge in self._adjacency.get(current, ()):
                dest = edge.dest_key
                dest_tags = self._edge_tags(edge)
                if dest_tags is None:
                    continue
                if avoid is not None and avoid(dest_tags):
                    continue
                if target_pred(dest_tags):
                    parent[dest] = (current, edge)
                    return self._reconstruct(parent, dest)
                src_tags = self._state_tags[current]
                change_count = sum(1 for k, v in edge.inputs.items() if src_tags.get(k) != v)
                edge_cost = max(change_count, 1)
                new_cost = cost + edge_cost
                if new_cost < dist.get(dest, float("inf")):  # type: ignore[arg-type]
                    dist[dest] = new_cost
                    parent[dest] = (current, edge)
                    counter += 1
                    heapq.heappush(heap, (new_cost, counter, dest))

        return Path(
            reachable=False,
            steps=(),
            total_changes=0,
            total_scans=0,
            reason="no path found",
        )

    def _reconstruct(
        self,
        parent: dict[tuple[Any, ...], tuple[tuple[Any, ...], TransitionEdge] | None],
        target: tuple[Any, ...],
    ) -> Path:
        steps: list[ReachabilityStep] = []
        current = target
        while (link := parent[current]) is not None:
            prev_key, edge = link
            src_tags = self._state_tags[edge.source_key]
            action = {k: v for k, v in edge.inputs.items() if src_tags.get(k) != v}
            steps.append(
                ReachabilityStep(
                    action=action,
                    source_key=edge.source_key,
                    dest_key=edge.dest_key,
                    scans=edge.scans,
                )
            )
            current = prev_key
        steps.reverse()
        total_changes = sum(len(s.action) for s in steps)
        total_scans = sum(s.scans for s in steps)
        return Path(
            reachable=True,
            steps=tuple(steps),
            total_changes=total_changes,
            total_scans=total_scans,
        )
