"""Static independence relation for free-input factoring.

Two input actions are *independent* when they commute within a single scan:
applying either one leads to the same single-scan successor regardless of
order.  Concretely, actions are independent when their influenced-rung cones
are disjoint and neither's write set intersects the other's read or write set.

The BFS uses the independence relation for **free-input factoring**: partition
free inputs into independent groups, evaluate each group's single-scan delta
independently, and compose results via delta merging.  Replaces O(product)
kernel evaluations with O(sum) evaluations plus O(product) cheap dictionary
merges.

Note: the relation is a *per-scan* commutativity check only.  It does not (and
must not) be used to reorder whole scans across BFS depth — a PLC scan carries
an implicit global tick, so level-gated accumulators do not commute across
scans even when their write cones are disjoint.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph

    from .inputs import _ExclusiveInputGroup


@dataclass(frozen=True, slots=True)
class IndependenceRelation:
    """Precomputed pairwise independence for input actions."""

    action_names: tuple[str, ...]
    independent: tuple[frozenset[int], ...]
    action_index_by_name: dict[str, int]
    write_tags: tuple[frozenset[str], ...]


@dataclass(frozen=True, slots=True)
class FreeInputFactoring:
    """Partition of free inputs into independent groups for factored evaluation."""

    groups: tuple[frozenset[str], ...]
    write_tags: tuple[frozenset[str], ...]
    shared_inputs: frozenset[str] = frozenset()


def _influenced_rungs(
    tag_name: str,
    graph: ProgramGraph,
    barrier_tags: frozenset[str] = frozenset(),
) -> tuple[frozenset[int], frozenset[str], frozenset[str]]:
    """Return (influenced rung indices, read tags, written tags) for *tag_name*.

    Starts from rungs that read *tag_name* in conditions or data, then follows
    write→read edges transitively.  Also follows call edges: when a cone rung
    calls a subroutine, all subroutine rungs are included; when a cone rung
    lives inside a subroutine, its caller rungs are included.

    *barrier_tags* stops transitive expansion: writes to barrier tags are
    recorded but the traversal does not follow through to their readers.
    Used by split_at to prevent promoted tags from bridging independent cones.
    """
    sub_members: dict[str, list[int]] = defaultdict(list)
    callers: dict[str, list[int]] = defaultdict(list)
    for idx, node in enumerate(graph.rung_nodes):
        if node.subroutine is not None:
            sub_members[node.subroutine].append(idx)
        for sub_name in node.calls:
            callers[sub_name].append(idx)

    visited_rungs: set[int] = set()
    read_tags: set[str] = set()
    write_tags: set[str] = set()
    queue: list[str] = [tag_name]
    visited_tags: set[str] = set()
    rung_queue: list[int] = []

    def _visit_rung(rung_idx: int) -> None:
        if rung_idx in visited_rungs:
            return
        visited_rungs.add(rung_idx)
        node = graph.rung_nodes[rung_idx]
        read_tags.update(node.condition_reads)
        read_tags.update(node.data_reads)
        for written_tag in node.writes:
            write_tags.add(written_tag)
            if written_tag not in visited_tags and written_tag not in barrier_tags:
                queue.append(written_tag)
        for sub_name in node.calls:
            for member_idx in sub_members.get(sub_name, ()):
                rung_queue.append(member_idx)
        if node.subroutine is not None:
            for caller_idx in callers.get(node.subroutine, ()):
                rung_queue.append(caller_idx)

    while queue or rung_queue:
        while queue:
            current = queue.pop()
            if current in visited_tags:
                continue
            visited_tags.add(current)
            for rung_idx in graph.readers_of.get(current, frozenset()):
                rung_queue.append(rung_idx)
        while rung_queue:
            _visit_rung(rung_queue.pop())

    read_tags.discard(tag_name)
    return frozenset(visited_rungs), frozenset(read_tags), frozenset(write_tags)


def _build_independence_relation(
    graph: ProgramGraph,
    nondeterministic_dims: dict[str, tuple[object, ...]],
    exclusive_input_groups: tuple[_ExclusiveInputGroup, ...],
    nondeterministic_names: tuple[str, ...],
    free_input_names: frozenset[str],
    split_tags: frozenset[str] = frozenset(),
) -> IndependenceRelation:
    """Build a static independence relation over all input actions.

    Covers exclusive input groups, edge-bearing singletons, and free
    singletons.  Free-input factoring uses the relation to derive a partition;
    ``_find_bridge_tags`` uses it for Intractable hint generation.

    *split_tags* are free inputs promoted by ``split_at``.  They act as
    barriers in the cone traversal (writes to a split tag don't expand the
    cone to its readers) and are excluded from write/read overlap checks
    (the BFS explores them at all values independently, so writes to them
    don't create real dependencies between other actions).
    """
    grouped_members: dict[str, int] = {}
    actions: list[tuple[str, tuple[str, ...]]] = []

    for group in exclusive_input_groups:
        idx = len(actions)
        actions.append((group.target_name or group.members[0], group.members))
        for member in group.members:
            grouped_members[member] = idx

    for name in nondeterministic_names:
        if name in grouped_members:
            continue
        actions.append((name, (name,)))

    for name in sorted(free_input_names):
        if name in grouped_members:
            continue
        actions.append((name, (name,)))

    n = len(actions)
    if n < 2:
        action_names = tuple(a[0] for a in actions)
        index_by_name: dict[str, int] = {}
        wt: list[frozenset[str]] = []
        for i, (_, members) in enumerate(actions):
            for m in members:
                index_by_name[m] = i
            all_w: set[str] = set()
            for member in members:
                _, _, writes = _influenced_rungs(member, graph, barrier_tags=split_tags)
                all_w.update(writes)
            wt.append(frozenset(all_w))
        return IndependenceRelation(
            action_names=action_names,
            independent=tuple(frozenset() for _ in range(n)),
            action_index_by_name=index_by_name,
            write_tags=tuple(wt),
        )

    cones: list[tuple[frozenset[int], frozenset[str], frozenset[str]]] = []
    for _, members in actions:
        all_rungs: set[int] = set()
        all_reads: set[str] = set()
        all_writes: set[str] = set()
        for member in members:
            rungs, reads, writes = _influenced_rungs(member, graph, barrier_tags=split_tags)
            all_rungs.update(rungs)
            all_reads.update(reads)
            all_writes.update(writes)
        cones.append((frozenset(all_rungs), frozenset(all_reads), frozenset(all_writes)))

    indep: list[set[int]] = [set() for _ in range(n)]
    for i in range(n):
        rungs_i, reads_i, writes_i = cones[i]
        eff_writes_i = writes_i - split_tags
        for j in range(i + 1, n):
            rungs_j, reads_j, writes_j = cones[j]
            if rungs_i & rungs_j:
                continue
            eff_writes_j = writes_j - split_tags
            if eff_writes_i & eff_writes_j:
                # Both actions write a shared tag (possibly a reader-less output
                # via different rungs).  Cone-disjoint, but the delta-merge winner
                # would depend on group-iteration order rather than the true
                # single-scan rung order — so they are NOT independent.
                continue
            if eff_writes_i & reads_j:
                continue
            if eff_writes_j & reads_i:
                continue
            indep[i].add(j)
            indep[j].add(i)

    action_names = tuple(a[0] for a in actions)
    index_by_name = {}
    for i, (_, members) in enumerate(actions):
        for m in members:
            index_by_name[m] = i

    action_write_tags = tuple(cones[i][2] for i in range(n))

    return IndependenceRelation(
        action_names=action_names,
        independent=tuple(frozenset(s) for s in indep),
        action_index_by_name=index_by_name,
        write_tags=action_write_tags,
    )


# ---------------------------------------------------------------------------
# Free-input factoring
# ---------------------------------------------------------------------------


def _partition_free_inputs(
    relation: IndependenceRelation,
    free_names: frozenset[str],
    split_tags: frozenset[str] = frozenset(),
) -> FreeInputFactoring | None:
    """Partition free inputs into independent groups using the independence relation.

    Returns ``None`` when all non-split free inputs land in a single group (no
    factoring benefit) or when there are fewer than two non-split free actions.

    *split_tags* are excluded from the partition and reported as
    ``shared_inputs`` — the BFS varies them with every group.
    """
    partitioned_names = free_names - split_tags

    free_indices: list[int] = []
    for name in sorted(partitioned_names):
        idx = relation.action_index_by_name.get(name)
        if idx is not None and idx not in free_indices:
            free_indices.append(idx)

    if len(free_indices) < 2:
        return None

    parent = {i: i for i in free_indices}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i in free_indices:
        for j in free_indices:
            if j <= i:
                continue
            if j not in relation.independent[i]:
                union(i, j)

    components: dict[int, list[int]] = defaultdict(list)
    for i in free_indices:
        components[find(i)].append(i)

    if len(components) < 2:
        return None

    groups: list[frozenset[str]] = []
    write_tags: list[frozenset[str]] = []
    for indices in components.values():
        members: set[str] = set()
        wt: set[str] = set()
        for idx in indices:
            name = relation.action_names[idx]
            members.add(name)
            wt.update(relation.write_tags[idx])
        groups.append(frozenset(members))
        write_tags.append(frozenset(wt))

    live_split = frozenset(name for name in split_tags if name in relation.action_index_by_name)

    return FreeInputFactoring(
        groups=tuple(groups),
        write_tags=tuple(write_tags),
        shared_inputs=live_split,
    )


def _find_bridge_tags(
    graph: ProgramGraph,
    stateful_dims: dict[str, tuple[object, ...]],
    nondeterministic_dims: dict[str, tuple[object, ...]],
    exclusive_input_groups: tuple[_ExclusiveInputGroup, ...],
    free_input_names: frozenset[str],
    nondeterministic_names: tuple[str, ...],
) -> list[tuple[str, int]]:
    """Find stateful tags whose promotion to free would enable factoring.

    Returns (tag_name, group_count) pairs sorted by partition improvement.
    Only considers Bool and choices= tags with small domains.
    """
    if len(free_input_names) < 2:
        return []

    from pyrung.core.tag import TagType

    candidates: list[str] = []
    for tag_name in stateful_dims:
        tag = graph.tags.get(tag_name)
        if tag is None:
            continue
        if tag.type is TagType.BOOL:
            candidates.append(tag_name)
        elif tag.choices is not None:
            candidates.append(tag_name)
    if not candidates:
        return []

    results: list[tuple[str, int]] = []
    for tag_name in candidates[:10]:
        sim_nd = dict(nondeterministic_dims)
        sim_nd[tag_name] = stateful_dims[tag_name]
        sim_free = free_input_names | {tag_name}
        sim_split = frozenset({tag_name})
        sim_relation = _build_independence_relation(
            graph,
            sim_nd,
            exclusive_input_groups,
            nondeterministic_names,
            sim_free,
            split_tags=sim_split,
        )
        sim_factoring = _partition_free_inputs(sim_relation, sim_free, split_tags=sim_split)
        if sim_factoring is not None:
            results.append((tag_name, len(sim_factoring.groups)))

    results.sort(key=lambda x: x[1], reverse=True)
    return results
