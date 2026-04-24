"""Candidate invariant miner for recorded DAP sessions.

After ``record stop``, the miner walks the captured scan range and
proposes candidate invariants the engineer can accept, deny, or
suppress via the review console verbs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pyrung.dap.capture import CaptureEntry

CandidateKind = Literal["edge_correlation", "steady_implication", "value_temporal"]

_MIN_EDGE_OBSERVATIONS = 2
_MIN_IMPLICATION_SCANS = 3
_MAX_TEMPORAL_WINDOW = 50


@dataclass(frozen=True)
class Candidate:
    """A proposed invariant mined from a recorded session."""

    id: str
    kind: CandidateKind
    description: str
    formula: str
    antecedent_tag: str
    consequent_tag: str
    observed_delay_scans: int
    physics_floor_scans: int | None
    dt_seconds: float
    observation_count: int
    violation_count: int
    scan_range: tuple[int, int]


Edge = tuple[int, Any, Any]  # (scan_id, old_value, new_value)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def mine_candidates(
    action: str,
    entries: list[CaptureEntry],
    runner: Any,
    *,
    start_scan_id: int | None = None,
    suppressed: frozenset[str] = frozenset(),
) -> list[Candidate]:
    """Mine candidate invariants from a recorded capture."""

    if not entries:
        return []

    scan_ids = [e.scan_id for e in entries if e.scan_id is not None]
    if not scan_ids:
        return []

    scan_start = start_scan_id if start_scan_id is not None else min(scan_ids)
    scan_end = max(scan_ids)
    if scan_end <= scan_start:
        return []

    history = getattr(runner, "history", None)
    if history is None:
        return []

    relevant = _relevant_tags(entries, runner)
    graph = _program_graph(runner)
    tag_meta = getattr(runner, "_known_tags_by_name", {})
    dt = _dt_seconds(runner)
    scan_range = (scan_start, scan_end)
    owned = _instruction_owned_tags(runner)
    relevant -= owned

    edges = _build_edge_map(runner, relevant, scan_start, scan_end)

    raw: list[Candidate] = []
    raw.extend(_mine_edge_correlations(edges, relevant, graph, tag_meta, dt, scan_range, runner))
    raw.extend(_mine_steady_implications(relevant, graph, tag_meta, dt, scan_range, runner))
    raw.extend(_mine_value_temporals(edges, relevant, graph, tag_meta, dt, scan_range, runner))

    filtered = [c for c in raw if c.formula not in suppressed]

    seen: set[tuple[str, str, str]] = set()
    deduped: list[Candidate] = []
    for c in filtered:
        key = (c.kind, c.antecedent_tag, c.consequent_tag)
        if key not in seen:
            seen.add(key)
            deduped.append(c)

    reduced = _transitive_reduce(deduped)
    reduced = _lock_filter(reduced, runner)

    result: list[Candidate] = []
    for i, c in enumerate(reduced, 1):
        result.append(
            Candidate(
                id=f"c-{i:02d}",
                kind=c.kind,
                description=c.description,
                formula=c.formula,
                antecedent_tag=c.antecedent_tag,
                consequent_tag=c.consequent_tag,
                observed_delay_scans=c.observed_delay_scans,
                physics_floor_scans=c.physics_floor_scans,
                dt_seconds=c.dt_seconds,
                observation_count=c.observation_count,
                violation_count=c.violation_count,
                scan_range=c.scan_range,
            )
        )
    return result


# ---------------------------------------------------------------------------
# Reused condenser helpers
# ---------------------------------------------------------------------------


def _relevant_tags(entries: list[CaptureEntry], runner: Any) -> set[str]:
    from pyrung.dap.condenser import _relevant_tags as _rt

    return _rt(entries, runner)


def _program_graph(runner: Any) -> Any | None:
    from pyrung.dap.condenser import _program_graph as _pg

    return _pg(runner)


def _dt_seconds(runner: Any) -> float:
    return float(getattr(runner, "_dt", 0.010))


def _instruction_owned_tags(runner: Any) -> frozenset[str]:
    """Return tags that are internal plumbing of timer/counter/drum instructions.

    These are the bookkeeping fields (done_bit, accumulator, current_step,
    completion_flag) — not user-meaningful outputs like drum output coils.
    """
    program = getattr(runner, "program", None)
    if program is None:
        return frozenset()

    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.instruction.drums import EventDrumInstruction, TimeDrumInstruction
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction
    from pyrung.core.tag import Tag
    from pyrung.core.validation._common import walk_instructions

    _PLUMBING_ATTRS = frozenset(
        {
            "done_bit",
            "accumulator",
            "current_step",
            "completion_flag",
        }
    )
    _OWNED_TYPES = (
        OnDelayInstruction,
        OffDelayInstruction,
        CountUpInstruction,
        CountDownInstruction,
        EventDrumInstruction,
        TimeDrumInstruction,
    )

    owned: set[str] = set()
    for instr in walk_instructions(program):
        if not isinstance(instr, _OWNED_TYPES):
            continue
        for attr in _PLUMBING_ATTRS:
            val = getattr(instr, attr, None)
            if isinstance(val, Tag):
                owned.add(val.name)
    return frozenset(owned)


# ---------------------------------------------------------------------------
# Transitive reduction
# ---------------------------------------------------------------------------


def _is_negated(c: Candidate) -> bool:
    return c.kind == "steady_implication" and "=> ~" in c.description


def _find_sccs(nodes: set[str], edges: set[tuple[str, str]]) -> list[frozenset[str]]:
    """Kosaraju's algorithm for strongly connected components."""
    adj: dict[str, set[str]] = {n: set() for n in nodes}
    adj_rev: dict[str, set[str]] = {n: set() for n in nodes}
    for u, v in edges:
        adj[u].add(v)
        adj_rev[v].add(u)

    visited: set[str] = set()
    order: list[str] = []
    for node in sorted(nodes):
        if node in visited:
            continue
        stack: list[tuple[str, bool]] = [(node, False)]
        while stack:
            n, processed = stack.pop()
            if processed:
                order.append(n)
                continue
            if n in visited:
                continue
            visited.add(n)
            stack.append((n, True))
            for neighbor in sorted(adj[n]):
                if neighbor not in visited:
                    stack.append((neighbor, False))

    visited.clear()
    sccs: list[frozenset[str]] = []
    for node in reversed(order):
        if node in visited:
            continue
        component: set[str] = set()
        stack_simple = [node]
        while stack_simple:
            n = stack_simple.pop()
            if n in visited:
                continue
            visited.add(n)
            component.add(n)
            for neighbor in adj_rev[n]:
                if neighbor not in visited:
                    stack_simple.append(neighbor)
        sccs.append(frozenset(component))
    return sccs


def _transitive_reduce(candidates: list[Candidate]) -> list[Candidate]:
    """Remove steady implications that are transitively implied by others.

    Edges within SCCs (equivalence classes like Conv_Motor <=> Running) are
    protected.  Only inter-SCC edges are candidates for greedy removal.
    """
    positive = [c for c in candidates if c.kind == "steady_implication" and not _is_negated(c)]

    if len(positive) < 3:
        return candidates

    all_edges = {(c.antecedent_tag, c.consequent_tag) for c in positive}
    nodes = {t for pair in all_edges for t in pair}

    sccs = _find_sccs(nodes, all_edges)
    node_to_scc: dict[str, int] = {}
    for i, scc in enumerate(sccs):
        for node in scc:
            node_to_scc[node] = i

    intra = {(u, v) for u, v in all_edges if node_to_scc[u] == node_to_scc[v]}
    inter = sorted(all_edges - intra)

    if not inter:
        return candidates

    remaining = set(all_edges)

    for u, v in inter:
        remaining.discard((u, v))
        adj: dict[str, set[str]] = {}
        for a, b in remaining:
            adj.setdefault(a, set()).add(b)
        visited: set[str] = set()
        stack = list(adj.get(u, set()))
        found = False
        while stack:
            node = stack.pop()
            if node == v:
                found = True
                break
            if node in visited:
                continue
            visited.add(node)
            stack.extend(adj.get(node, set()) - visited)
        if not found:
            remaining.add((u, v))

    redundant = all_edges - remaining
    if not redundant:
        return candidates

    return [
        c
        for c in candidates
        if not (
            c.kind == "steady_implication"
            and not _is_negated(c)
            and (c.antecedent_tag, c.consequent_tag) in redundant
        )
    ]


# ---------------------------------------------------------------------------
# Lock-aware filtering
# ---------------------------------------------------------------------------


def _find_lock(runner: Any) -> Path | None:
    """Look for pyrung.lock next to the program source file."""
    program_path = getattr(runner, "_program_path", None)
    if program_path is not None:
        lock = Path(program_path).parent / "pyrung.lock"
        if lock.exists():
            return lock
    lock = Path("pyrung.lock")
    if lock.exists():
        return lock
    return None


def _lock_filter(candidates: list[Candidate], runner: Any) -> list[Candidate]:
    """Drop steady implications already proven by the lock file's reachable states."""
    lock_path = _find_lock(runner)
    if lock_path is None:
        return candidates

    try:
        from pyrung.core.analysis.prove import read_lock

        lock_data = read_lock(lock_path)
    except Exception:
        return candidates

    projection = set(lock_data.get("projection", []))
    reachable = lock_data.get("reachable", [])
    if not projection or not reachable:
        return candidates

    proven: set[tuple[str, str, bool]] = set()
    for c in candidates:
        if c.kind != "steady_implication":
            continue
        ant, cons = c.antecedent_tag, c.consequent_tag
        if ant not in projection or cons not in projection:
            continue
        negated = _is_negated(c)
        holds = True
        for state in reachable:
            if state.get(ant) is True:
                if negated and state.get(cons) is True:
                    holds = False
                    break
                if not negated and state.get(cons) is not True:
                    holds = False
                    break
        if holds:
            proven.add((ant, cons, negated))

    if not proven:
        return candidates

    return [
        c
        for c in candidates
        if not (
            c.kind == "steady_implication"
            and (c.antecedent_tag, c.consequent_tag, _is_negated(c)) in proven
        )
    ]


# ---------------------------------------------------------------------------
# Edge detection
# ---------------------------------------------------------------------------


def _build_edge_map(
    runner: Any,
    relevant: set[str],
    scan_start: int,
    scan_end: int,
) -> dict[str, list[Edge]]:
    history = runner.history
    edges: dict[str, list[Edge]] = {tag: [] for tag in relevant}

    for scan_id in range(scan_start + 1, scan_end + 1):
        if not history.contains(scan_id - 1) or not history.contains(scan_id):
            continue
        before = history.at(scan_id - 1).tags
        after = history.at(scan_id).tags
        for tag in relevant:
            old = before.get(tag)
            new = after.get(tag)
            if old != new:
                edges[tag].append((scan_id, old, new))

    return edges


# ---------------------------------------------------------------------------
# Physics floor
# ---------------------------------------------------------------------------


def _physics_floor_scans(
    tag_name: str, tag_meta: dict[str, Any], dt: float, *, rising: bool
) -> int | None:
    tag = tag_meta.get(tag_name)
    if tag is None:
        return None
    physical = getattr(tag, "physical", None)
    if physical is None:
        return None
    if physical.feedback_type != "bool":
        return None
    delay_ms = physical.on_delay_ms if rising else physical.off_delay_ms
    if delay_ms is None:
        return None
    dt_ms = dt * 1000.0
    if dt_ms <= 0:
        return None
    return max(1, int(delay_ms / dt_ms))


def _is_rising(old: Any, new: Any) -> bool:
    if isinstance(new, bool):
        return new is True and old is not True
    if isinstance(new, (int, float)) and isinstance(old, (int, float)):
        return new > old
    return False


# ---------------------------------------------------------------------------
# Strategy 1: Edge correlation
# ---------------------------------------------------------------------------


def _downstream_of(tag: str, graph: Any) -> frozenset[str]:
    if graph is None:
        return frozenset()
    try:
        return graph.downstream_slice(tag)
    except Exception:
        return frozenset()


def _mine_edge_correlations(
    edges: dict[str, list[Edge]],
    relevant: set[str],
    graph: Any,
    tag_meta: dict[str, Any],
    dt: float,
    scan_range: tuple[int, int],
    runner: Any,
) -> list[Candidate]:
    candidates: list[Candidate] = []

    for a_tag in sorted(relevant):
        a_edges = edges.get(a_tag, [])
        if not a_edges:
            continue

        downstream = _downstream_of(a_tag, graph)
        targets = (downstream & relevant) - {a_tag}
        if not targets:
            continue

        for b_tag in sorted(targets):
            b_edges = edges.get(b_tag, [])
            if not b_edges:
                continue

            obs_count = 0
            viol_count = 0
            max_delay = 0

            for a_scan, _a_old, _a_new in a_edges:
                matched = False
                for b_scan, _b_old, _b_new in b_edges:
                    delay = b_scan - a_scan
                    if 0 <= delay <= _MAX_TEMPORAL_WINDOW:
                        matched = True
                        max_delay = max(max_delay, delay)
                        break
                if matched:
                    obs_count += 1
                else:
                    viol_count += 1

            if obs_count < _MIN_EDGE_OBSERVATIONS:
                continue
            if viol_count > 0:
                continue

            a_sample_new = a_edges[0][2]
            b_sample_new = b_edges[0][2]
            a_rising = _is_rising(a_edges[0][1], a_sample_new)
            b_rising = _is_rising(b_edges[0][1], b_sample_new)

            floor = _physics_floor_scans(b_tag, tag_meta, dt, rising=b_rising)
            if floor is not None and max_delay < floor:
                continue

            a_arrow = "^" if a_rising else "v"
            b_arrow = "^" if b_rising else "v"
            desc = f"{a_tag}{a_arrow} -> {b_tag}{b_arrow} within {max_delay} scan{'s' if max_delay != 1 else ''}"
            formula = f"{a_tag}{a_arrow} -> {b_tag}{b_arrow} within {max_delay} scans [dt={dt}]"

            candidates.append(
                Candidate(
                    id="",
                    kind="edge_correlation",
                    description=desc,
                    formula=formula,
                    antecedent_tag=a_tag,
                    consequent_tag=b_tag,
                    observed_delay_scans=max_delay,
                    physics_floor_scans=floor,
                    dt_seconds=dt,
                    observation_count=obs_count,
                    violation_count=viol_count,
                    scan_range=scan_range,
                )
            )

    return candidates


# ---------------------------------------------------------------------------
# Strategy 2: Steady-state implication
# ---------------------------------------------------------------------------


def _tags_forced_entire_window(runner: Any, scan_start: int, scan_end: int) -> frozenset[str]:
    """Return tags forced True for every scan in [scan_start, scan_end]."""
    scan_log = getattr(runner, "_scan_log", None)
    if scan_log is None:
        return frozenset()

    force_changes = scan_log._force_changes_by_scan
    sorted_scans = sorted(force_changes.keys())

    current_forces: dict[str, Any] = {}
    for sid in sorted_scans:
        if sid > scan_start:
            break
        current_forces = dict(force_changes[sid])

    forced_true = {tag for tag, val in current_forces.items() if val is True}
    if not forced_true:
        return frozenset()

    candidates = set(forced_true)
    for sid in sorted_scans:
        if sid <= scan_start:
            continue
        if sid > scan_end:
            break
        snapshot = force_changes[sid]
        candidates = {tag for tag in candidates if snapshot.get(tag) is True}
        if not candidates:
            return frozenset()

    return frozenset(candidates)


def _structurally_provable(
    a_tag: str, b_tag: str, negated: bool, forms: dict[str, Any], program: Any
) -> bool:
    """Check if A => B (or A => ~B) is provable from simplified forms or reset dominance."""
    from pyrung.core.analysis.simplified import expr_requires, reset_dominance

    form = forms.get(a_tag)
    if form is not None and expr_requires(form.expr, b_tag, negated=negated):
        return True
    if program is not None:
        try:
            return reset_dominance(program, a_tag, b_tag, negated=negated)
        except Exception:
            pass
    return False


def _mine_steady_implications(
    relevant: set[str],
    graph: Any,
    tag_meta: dict[str, Any],
    dt: float,
    scan_range: tuple[int, int],
    runner: Any,
) -> list[Candidate]:
    from pyrung.core.tag import TagType

    history = runner.history
    scan_start, scan_end = scan_range

    bool_tags = set()
    for tag_name in relevant:
        meta = tag_meta.get(tag_name)
        if meta is not None and meta.type == TagType.BOOL:
            bool_tags.add(tag_name)

    if len(bool_tags) < 2:
        return []

    scans: list[dict[str, Any]] = []
    for sid in range(scan_start, scan_end + 1):
        if history.contains(sid):
            scans.append(dict(history.at(sid).tags))

    if len(scans) < _MIN_IMPLICATION_SCANS:
        return []

    noisy = _tags_forced_entire_window(runner, scan_start, scan_end)

    program = getattr(runner, "program", None)
    forms: dict[str, Any] = {}
    if program is not None:
        try:
            forms = program.simplified()
        except Exception:
            pass

    edges = _build_edge_map(runner, bool_tags, scan_start, scan_end)
    varied = {tag for tag, tag_edges in edges.items() if tag_edges}

    candidates: list[Candidate] = []

    for a_tag in sorted(bool_tags):
        if a_tag in noisy:
            continue

        a_true_scans = [s for s in scans if s.get(a_tag) is True]
        if len(a_true_scans) < _MIN_IMPLICATION_SCANS:
            continue

        downstream = _downstream_of(a_tag, graph)
        upstream = frozenset()
        if graph is not None:
            try:
                upstream = graph.upstream_slice(a_tag)
            except Exception:
                pass
        connected = (downstream | upstream) & bool_tags - {a_tag}
        if not connected:
            continue

        for b_tag in sorted(connected):
            if b_tag in noisy:
                continue

            b_in_a_true = [s.get(b_tag) for s in a_true_scans]
            if all(v is True for v in b_in_a_true):
                structural = _structurally_provable(a_tag, b_tag, False, forms, program)
                if not structural and b_tag not in varied:
                    continue
                desc = f"{a_tag} => {b_tag}"
                formula = f"{a_tag} => {b_tag} [dt={dt}]"
                candidates.append(
                    Candidate(
                        id="",
                        kind="steady_implication",
                        description=desc,
                        formula=formula,
                        antecedent_tag=a_tag,
                        consequent_tag=b_tag,
                        observed_delay_scans=0,
                        physics_floor_scans=None,
                        dt_seconds=dt,
                        observation_count=len(a_true_scans),
                        violation_count=0,
                        scan_range=scan_range,
                    )
                )
            elif all(v is not True for v in b_in_a_true):
                structural = _structurally_provable(a_tag, b_tag, True, forms, program)
                if not structural and b_tag not in varied:
                    continue
                desc = f"{a_tag} => ~{b_tag}"
                formula = f"{a_tag} => ~{b_tag} [dt={dt}]"
                candidates.append(
                    Candidate(
                        id="",
                        kind="steady_implication",
                        description=desc,
                        formula=formula,
                        antecedent_tag=a_tag,
                        consequent_tag=b_tag,
                        observed_delay_scans=0,
                        physics_floor_scans=None,
                        dt_seconds=dt,
                        observation_count=len(a_true_scans),
                        violation_count=0,
                        scan_range=scan_range,
                    )
                )

    return candidates


# ---------------------------------------------------------------------------
# Strategy 3: Value-state temporal
# ---------------------------------------------------------------------------


def _mine_value_temporals(
    edges: dict[str, list[Edge]],
    relevant: set[str],
    graph: Any,
    tag_meta: dict[str, Any],
    dt: float,
    scan_range: tuple[int, int],
    runner: Any,
) -> list[Candidate]:
    from pyrung.core.tag import TagType

    history = runner.history
    scan_start, scan_end = scan_range

    non_bool: set[str] = set()
    for tag_name in relevant:
        meta = tag_meta.get(tag_name)
        if meta is not None and meta.type != TagType.BOOL:
            non_bool.add(tag_name)

    if not non_bool:
        return []

    candidates: list[Candidate] = []

    for a_tag in sorted(non_bool):
        a_edges = edges.get(a_tag, [])
        if not a_edges:
            continue

        downstream = _downstream_of(a_tag, graph)
        targets = (downstream & relevant) - {a_tag}
        if not targets:
            continue

        trigger_values: dict[Any, list[int]] = {}
        for scan_id, _old, new in a_edges:
            trigger_values.setdefault(new, []).append(scan_id)

        for trigger_val, trigger_scans in sorted(trigger_values.items(), key=lambda kv: str(kv[0])):
            if len(trigger_scans) < _MIN_EDGE_OBSERVATIONS:
                continue

            a_meta = tag_meta.get(a_tag)
            if a_meta is not None:
                if a_meta.min is not None and trigger_val < a_meta.min:
                    continue
                if a_meta.max is not None and trigger_val > a_meta.max:
                    continue

            for b_tag in sorted(targets):
                b_meta = tag_meta.get(b_tag)
                b_is_bool = b_meta is not None and b_meta.type == TagType.BOOL

                obs_count = 0
                viol_count = 0
                max_delay = 0
                observed_b_val: Any = None

                for t_scan in trigger_scans:
                    window_end = min(t_scan + _MAX_TEMPORAL_WINDOW, scan_end)
                    found = False
                    for sid in range(t_scan, window_end + 1):
                        if not history.contains(sid):
                            continue
                        b_val = history.at(sid).tags.get(b_tag)

                        if observed_b_val is None:
                            if sid > t_scan and b_val != history.at(t_scan).tags.get(b_tag):
                                observed_b_val = b_val
                                found = True
                                max_delay = max(max_delay, sid - t_scan)
                                break
                        elif b_val == observed_b_val:
                            found = True
                            max_delay = max(max_delay, sid - t_scan)
                            break

                    if found:
                        obs_count += 1
                    else:
                        viol_count += 1

                if obs_count < _MIN_EDGE_OBSERVATIONS or viol_count > 0:
                    continue
                if observed_b_val is None:
                    continue

                if b_meta is not None and not b_is_bool:
                    if b_meta.min is not None and observed_b_val < b_meta.min:
                        continue
                    if b_meta.max is not None and observed_b_val > b_meta.max:
                        continue

                rising = _is_rising(None, observed_b_val)
                floor = _physics_floor_scans(b_tag, tag_meta, dt, rising=rising)
                if floor is not None and max_delay < floor:
                    continue

                b_repr = str(observed_b_val)
                if b_is_bool:
                    b_repr = str(observed_b_val).lower()
                desc = (
                    f"{a_tag}={trigger_val} => {b_tag}={b_repr} "
                    f"within {max_delay} scan{'s' if max_delay != 1 else ''}"
                )
                formula = (
                    f"{a_tag}={trigger_val} => {b_tag}={b_repr} within {max_delay} scans [dt={dt}]"
                )

                candidates.append(
                    Candidate(
                        id="",
                        kind="value_temporal",
                        description=desc,
                        formula=formula,
                        antecedent_tag=a_tag,
                        consequent_tag=b_tag,
                        observed_delay_scans=max_delay,
                        physics_floor_scans=floor,
                        dt_seconds=dt,
                        observation_count=obs_count,
                        violation_count=viol_count,
                        scan_range=scan_range,
                    )
                )

    return candidates
