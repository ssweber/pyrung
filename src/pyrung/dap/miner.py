"""Candidate invariant miner for recorded DAP sessions.

After ``record stop``, the miner walks the captured scan range and
proposes candidate invariants the engineer can accept, deny, or
suppress via the review console verbs.
"""

from __future__ import annotations

from dataclasses import dataclass
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

    result: list[Candidate] = []
    for i, c in enumerate(deduped, 1):
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

            a_arrow = "↑" if a_rising else "↓"
            b_arrow = "↑" if b_rising else "↓"
            desc = f"{a_tag}{a_arrow} → {b_tag}{b_arrow} within {max_delay} scan{'s' if max_delay != 1 else ''}"
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

    candidates: list[Candidate] = []

    for a_tag in sorted(bool_tags):
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
            b_in_a_true = [s.get(b_tag) for s in a_true_scans]
            if all(v is True for v in b_in_a_true):
                desc = f"{a_tag} ⟹ {b_tag}"
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
                desc = f"{a_tag} ⟹ ¬{b_tag}"
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
                    f"{a_tag}={trigger_val} ⟹ {b_tag}={b_repr} "
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
