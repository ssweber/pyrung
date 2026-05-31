"""Transition graph and reachability path-finding for explored state spaces."""

from __future__ import annotations

import heapq
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Callable as _Callable

    from pyrung.core.analysis.simplified import Atom

    StateKey = tuple[Any, ...]

_PENDING = "Pending"
_OFF_DELAY = "off_delay"


def _done_acc_abstract(kind: str, done_val: Any, acc_val: Any) -> bool | str:
    acc_nonzero = bool(acc_val and acc_val != 0)
    if kind == _OFF_DELAY:
        if done_val and acc_nonzero:
            return _PENDING
        return bool(done_val)
    if done_val:
        return True
    if acc_nonzero:
        return _PENDING
    return False


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
    constraints: dict[str, str] | None = None


_FORM_TO_OP = {"gt": ">", "ge": ">=", "lt": "<", "le": "<=", "eq": "==", "ne": "!="}
_FORM_FLIP = {"gt": "<", "ge": "<=", "lt": ">", "le": ">=", "eq": "==", "ne": "!="}
_TIER3_SOURCES = frozenset({"bool", "choices", "done_acc_tri_state"})
_COMPARISON_FORMS = frozenset({"eq", "ne", "lt", "le", "gt", "ge"})


def _enrich_atom_index(
    atom_index: dict[str, list[Atom]],
    reverse_edge_map: dict[str, list[tuple[str, _Callable[[Any], Any]]]],
) -> dict[str, list[Atom]]:
    """Propagate comparison atoms backward through copy/calc chains.

    Given ``copy(Source, Target)`` and ``Target > 50``, adds an effective
    atom ``Source > 50`` so the path renderer can display constraints in
    terms of the input the user controls, not an intermediate variable.

    Tag-vs-tag operands only propagate through identity transforms (copy);
    literal operands are transformed via the inverse function (calc).
    """
    from pyrung.core.analysis.reverse_edges import IDENTITY, compose_invert
    from pyrung.core.analysis.simplified import Atom

    target_to_sources: dict[str, list[tuple[str, _Callable[[Any], Any]]]] = {}
    for source, edges in reverse_edge_map.items():
        for target, invert in edges:
            target_to_sources.setdefault(target, []).append((source, invert))

    if not target_to_sources:
        return atom_index

    enriched: dict[str, list[Atom]] = {tag: list(atoms) for tag, atoms in atom_index.items()}
    existing_keys: dict[str, set[tuple[str, str, Any]]] = {
        tag: {a._key() for a in atoms} for tag, atoms in enriched.items()
    }

    for tag, atoms in atom_index.items():
        for atom in atoms:
            if atom.form not in _COMPARISON_FORMS or atom.tag != tag:
                continue

            queue: list[tuple[str, _Callable[[Any], Any]]] = list(target_to_sources.get(tag, []))
            visited: set[str] = {tag}

            while queue:
                source, composed_invert = queue.pop(0)
                if source in visited:
                    continue
                visited.add(source)

                if isinstance(atom.operand, str):
                    if composed_invert is not IDENTITY:
                        continue
                    new_atom = Atom(tag=source, form=atom.form, operand=atom.operand)
                else:
                    new_threshold = composed_invert(atom.operand)
                    if new_threshold is None or not isinstance(new_threshold, (int, float)):
                        continue
                    new_atom = Atom(tag=source, form=atom.form, operand=new_threshold)

                key = new_atom._key()
                if key not in existing_keys.get(source, set()):
                    enriched.setdefault(source, []).append(new_atom)
                    existing_keys.setdefault(source, set()).add(key)
                    if isinstance(new_atom.operand, str):
                        enriched.setdefault(new_atom.operand, []).append(new_atom)
                        existing_keys.setdefault(new_atom.operand, set()).add(key)

                for next_src, next_inv in target_to_sources.get(source, []):
                    if next_src not in visited:
                        queue.append((next_src, compose_invert(next_inv, composed_invert)))

    return enriched


def _eval_comparison(form: str, left: Any, right: Any) -> bool:
    """Evaluate whether a comparison holds for given values."""
    try:
        if form == "gt":
            return left > right
        if form == "ge":
            return left >= right
        if form == "lt":
            return left < right
        if form == "le":
            return left <= right
        if form == "eq":
            return left == right
        if form == "ne":
            return left != right
    except TypeError:
        pass
    return False


def _classify_step_inputs(
    action: dict[str, Any],
    atom_index: dict[str, list[Atom]],
    domain_sources: dict[str, str],
    dest_tags: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Classify each input in a step and return semantic display strings.

    Returns a dict mapping tag names to their display string. Tags consumed
    by a Tier 2 group are keyed under a synthetic ``_group:<tag1>,<tag2>`` key
    so the renderer can suppress their individual entries.
    """
    action_tags = set(action.keys())
    constraints: dict[str, str] = {}
    suppressed: set[str] = set()
    seen_pairs: set[frozenset[str]] = set()

    # --- Tier 2: tag-vs-tag relational constraints ---
    for tag in sorted(action_tags):
        source = domain_sources.get(tag, "unknown")
        if source in _TIER3_SOURCES:
            continue
        atoms = atom_index.get(tag, [])
        for atom in atoms:
            if atom.form not in _FORM_TO_OP:
                continue
            if not isinstance(atom.operand, str):
                continue
            other = atom.operand if atom.tag == tag else atom.tag
            if other not in action_tags:
                continue
            other_source = domain_sources.get(other, "unknown")
            if other_source in _TIER3_SOURCES:
                continue
            pair = frozenset({tag, other})
            if pair in seen_pairs:
                continue
            # Verify this constraint is actually satisfied in dest state
            if dest_tags is not None:
                left_val = dest_tags.get(atom.tag)
                right_val = dest_tags.get(atom.operand)
                if left_val is not None and right_val is not None:
                    if not _eval_comparison(atom.form, left_val, right_val):
                        continue
            seen_pairs.add(pair)
            if atom.tag == tag:
                op = _FORM_TO_OP[atom.form]
                display = f"{atom.tag} {op} {atom.operand}"
            else:
                op = _FORM_FLIP[atom.form]
                display = f"{other} {op} {atom.tag}"
            group_key = f"_group:{min(tag, other)},{max(tag, other)}"
            constraints[group_key] = display
            suppressed.add(tag)
            suppressed.add(other)
            break

    # --- Tier 1 / Tier 2 solo: remaining non-bool tags ---
    for tag in sorted(action_tags):
        if tag in suppressed:
            continue
        source = domain_sources.get(tag, "unknown")
        if source in _TIER3_SOURCES:
            continue
        atoms = atom_index.get(tag, [])
        value = action[tag]

        # Collect literal thresholds and tag-vs-tag constraints for this tag
        best_literal: tuple[str, Any] | None = None
        best_relational: str | None = None
        has_literal = False
        for atom in atoms:
            if atom.form not in _FORM_TO_OP:
                continue
            if isinstance(atom.operand, str):
                # Tag-vs-tag — check if satisfied using dest state
                if dest_tags is not None:
                    left_val = dest_tags.get(atom.tag)
                    right_val = dest_tags.get(atom.operand)
                    if left_val is not None and right_val is not None:
                        if not _eval_comparison(atom.form, left_val, right_val):
                            continue
                if atom.tag == tag:
                    best_relational = f"{atom.tag} {_FORM_TO_OP[atom.form]} {atom.operand}"
                else:
                    best_relational = f"{tag} {_FORM_FLIP[atom.form]} {atom.tag}"
            elif isinstance(atom.operand, (int, float)) and atom.tag == tag:
                has_literal = True
                threshold = atom.operand
                if best_literal is None or abs(value - threshold) < abs(value - best_literal[1]):
                    best_literal = (_FORM_TO_OP[atom.form], threshold)

        if best_literal is not None:
            op, thresh = best_literal
            constraints[tag] = f"{tag}={value} ({op} {thresh})"
        elif not has_literal and best_relational is not None:
            # No literal anchor — value is arbitrary, show the constraint
            constraints[tag] = best_relational

    if suppressed:
        for tag in suppressed:
            constraints.setdefault(f"_suppress:{tag}", "")

    return constraints if constraints else {}


def _render_step_inputs(step: ReachabilityStep) -> str:
    """Render a step's inputs using semantic constraints when available."""
    if not step.constraints:
        return ", ".join(f"{k}={v}" for k, v in sorted(step.action.items()))

    suppressed = {k.split(":", 1)[1] for k in step.constraints if k.startswith("_suppress:")}
    groups = [(k, v) for k, v in sorted(step.constraints.items()) if k.startswith("_group:")]

    parts: list[str] = []
    for _, display in groups:
        parts.append(display)
    for tag in sorted(step.action.keys()):
        if tag in suppressed:
            continue
        if tag in step.constraints:
            parts.append(step.constraints[tag])
        else:
            parts.append(f"{tag}={step.action[tag]}")
    return ", ".join(parts)


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
            inputs = _render_step_inputs(step)
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

    __slots__ = (
        "_adjacency",
        "_state_tags",
        "_initial_key",
        "_tag_names",
        "_stateful_names",
        "_done_specs",
        "_atom_index",
        "_domain_sources",
    )

    def __init__(
        self,
        adjacency: dict[tuple[Any, ...], list[TransitionEdge]],
        state_tags: dict[tuple[Any, ...], dict[str, Any]],
        initial_key: tuple[Any, ...],
        tag_names: frozenset[str],
        stateful_names: tuple[str, ...] = (),
        done_specs: tuple[tuple[int, str, str], ...] = (),
        atom_index: dict[str, list[Atom]] | None = None,
        domain_sources: dict[str, str] | None = None,
    ) -> None:
        self._adjacency = adjacency
        self._state_tags = state_tags
        self._initial_key = initial_key
        self._tag_names = tag_names
        self._stateful_names = stateful_names
        self._done_specs = done_specs
        self._atom_index = atom_index
        self._domain_sources = domain_sources

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

    def find_state_keys(self, tags: dict[str, Any]) -> list[tuple[Any, ...]]:
        """Find graph keys matching *tags* on stateful dimensions only.

        Compares only the tags that form the BFS state identity
        (after elision), ignoring external inputs and kernel-internal
        tags whose snapshot values are incidental.  Done-spec transforms
        are applied so timer/counter tags use the same three-valued
        abstraction the BFS used when building the graph.
        """
        if not self._stateful_names:
            return self.find_matching_keys(tags)
        target = list(tags.get(n) for n in self._stateful_names)
        for idx, acc_name, kind in self._done_specs:
            target[idx] = _done_acc_abstract(kind, target[idx], tags.get(acc_name))
        target_t = tuple(target)
        result: list[tuple[Any, ...]] = []
        for key, stored in self._state_tags.items():
            candidate = list(stored.get(n) for n in self._stateful_names)
            for idx, acc_name, kind in self._done_specs:
                candidate[idx] = _done_acc_abstract(kind, candidate[idx], stored.get(acc_name))
            if tuple(candidate) == target_t:
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
            constraints = None
            if self._atom_index is not None and self._domain_sources is not None:
                constraints = _classify_step_inputs(
                    action, self._atom_index, self._domain_sources, edge.dest_tags
                )
            steps.append(
                ReachabilityStep(
                    action=action,
                    source_key=edge.source_key,
                    dest_key=edge.dest_key,
                    scans=edge.scans,
                    constraints=constraints if constraints else None,
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
