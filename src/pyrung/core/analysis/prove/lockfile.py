"""Lock-file utilities for the prove subsystem."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .results import Intractable, StateDiff

if TYPE_CHECKING:
    from collections.abc import Callable

    from pyrung.core.program import Program


def diff_states(
    before: frozenset[frozenset[tuple[str, Any]]],
    after: frozenset[frozenset[tuple[str, Any]]],
) -> StateDiff:
    """Compare two reachable state sets."""
    return StateDiff(added=after - before, removed=before - after)


def _default_projection(program: Program) -> list[str]:
    """Choose default projection tags: tags marked lock=True."""
    from pyrung.core.analysis.pdg import build_program_graph

    graph = build_program_graph(program)
    return sorted(name for name, tag in graph.tags.items() if getattr(tag, "lock", False))


def _resolve_choice_labels(
    states: frozenset[frozenset[tuple[str, Any]]],
    choice_labels: dict[str, dict[Any, str]],
) -> frozenset[frozenset[tuple[str, Any]]]:
    """Replace raw choice-key values with their human-readable labels."""
    if not choice_labels:
        return states
    resolved: set[frozenset[tuple[str, Any]]] = set()
    for state in states:
        new_pairs: list[tuple[str, Any]] = []
        for name, value in state:
            tag_labels = choice_labels.get(name)
            if tag_labels is not None and value in tag_labels:
                new_pairs.append((name, tag_labels[value]))
            else:
                new_pairs.append((name, value))
        resolved.add(frozenset(new_pairs))
    return frozenset(resolved)


_BAND_CMP_RE = re.compile(r"^(==|!=|>=?|<=?)\s*(-?\d+(?:\.\d+)?)$")
_BAND_RANGE_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*\.\.\s*(-?\d+(?:\.\d+)?)$")


def _parse_band_number(s: str) -> int | float:
    f = float(s)
    return int(f) if f == int(f) else f


def _match_band_predicate(value: Any, predicate: int | float | str) -> bool:
    if isinstance(predicate, int | float):
        return value == predicate
    if not isinstance(predicate, str):
        return False
    if predicate == "*":
        return True
    if not isinstance(value, int | float):
        return False
    m = _BAND_CMP_RE.match(predicate)
    if m:
        num = _parse_band_number(m.group(2))
        op = m.group(1)
        if op == "==":
            return value == num
        if op == "!=":
            return value != num
        if op == ">":
            return value > num
        if op == ">=":
            return value >= num
        if op == "<":
            return value < num
        if op == "<=":
            return value <= num
    m = _BAND_RANGE_RE.match(predicate)
    if m:
        lo = _parse_band_number(m.group(1))
        hi = _parse_band_number(m.group(2))
        return lo <= value <= hi
    return False


def _apply_band(value: Any, band: dict[str, Any]) -> str | None:
    for label, predicate in band.items():
        if _match_band_predicate(value, predicate):
            return label
    return None


def _build_band_maps(
    projection: list[str],
    tags: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if tags is None:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for name in projection:
        tag = tags.get(name)
        if tag is not None and getattr(tag, "band", None):
            result[name] = tag.band
    return result


def _resolve_band_labels(
    states: frozenset[frozenset[tuple[str, Any]]],
    band_maps: dict[str, dict[str, Any]],
) -> frozenset[frozenset[tuple[str, Any]]]:
    if not band_maps:
        return states
    resolved: set[frozenset[tuple[str, Any]]] = set()
    for state in states:
        new_pairs: list[tuple[str, Any]] = []
        for name, value in state:
            bmap = band_maps.get(name)
            if bmap is not None:
                label = _apply_band(value, bmap)
                new_pairs.append((name, label if label is not None else value))
            else:
                new_pairs.append((name, value))
        resolved.add(frozenset(new_pairs))
    return frozenset(resolved)


def _states_to_json(
    states: frozenset[frozenset[tuple[str, Any]]],
) -> list[dict[str, Any]]:
    """Convert state frozensets to sorted list of dicts.

    Omits tags whose value is False — False is the implied default for
    Bool projections, keeping each state entry as "what's ON."
    """
    rows = [dict(sorted((k, v) for k, v in s if v is not False)) for s in states]
    rows.sort(key=lambda d: tuple((k, str(v)) for k, v in sorted(d.items())))
    return rows


def _json_to_states(
    rows: list[dict[str, Any]],
    projection: list[str] | None = None,
) -> frozenset[frozenset[tuple[str, Any]]]:
    """Convert list of dicts back to state frozensets.

    When *projection* is given, missing tags are filled with False
    (the implied default omitted during serialization).
    """
    if projection is None:
        return frozenset(frozenset(d.items()) for d in rows)
    result: set[frozenset[tuple[str, Any]]] = set()
    for d in rows:
        state = {name: d.get(name, False) for name in projection}
        result.add(frozenset(state.items()))
    return frozenset(result)


def _build_choice_labels(
    projection: list[str],
    tags: dict[str, Any] | None,
) -> dict[str, dict[Any, str]]:
    """Build {tag_name: {value: label}} for projected tags with choices."""
    if tags is None:
        return {}
    labels: dict[str, dict[Any, str]] = {}
    for name in projection:
        tag = tags.get(name)
        if tag is not None and getattr(tag, "choices", None):
            labels[name] = {v: lbl for v, lbl in tag.choices.items()}
    return labels


def write_lock(
    path: Path,
    states: frozenset[frozenset[tuple[str, Any]]],
    projection: list[str],
    program_hash: str,
    unreachable_examples: list[dict[str, Any]] | None = None,
) -> None:
    """Write a state-space lock file (states must already be label-resolved)."""
    rows = _states_to_json(states)
    data = {
        "version": 1,
        "program_hash": program_hash,
        "projection": sorted(projection),
        "reachable": rows,
        "unreachable_examples": unreachable_examples or [],
    }
    path.write_text(json.dumps(data, indent=2, default=_json_default) + "\n")


def _json_default(obj: Any) -> Any:
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float, str)):
        return obj
    msg = f"Object of type {type(obj).__name__} is not JSON serializable"
    raise TypeError(msg)


def read_lock(path: Path) -> dict[str, Any]:
    """Read a state-space lock file."""
    return json.loads(path.read_text())


def program_hash(program: Program) -> str:
    """Compute a hash of the program's compiled kernel source."""
    from pyrung.circuitpy.codegen import compile_kernel

    compiled = compile_kernel(program, blockless=True)
    return hashlib.sha256(compiled.source.encode()).hexdigest()[:16]


def check_lock(
    program: Program,
    lock_path: Path = Path("pyrung.lock"),
    depth_budget: int = 50,
    max_states: int = 100_000,
    progress: bool | Callable[[int, int, float], None] = False,
) -> StateDiff | None:
    """Recompute reachable states and diff against a lock file.

    Returns None if the lock matches, or a ``StateDiff`` if changed.
    """
    from . import reachable_states

    lock_data = read_lock(lock_path)
    projection = lock_data["projection"]
    old_states = _json_to_states(lock_data["reachable"], projection)

    new_states = reachable_states(
        program,
        project=projection,
        depth_budget=depth_budget,
        max_states=max_states,
        progress=progress,
    )
    if isinstance(new_states, Intractable):
        msg = f"Verification intractable: {new_states.reason}"
        raise RuntimeError(msg)

    d = diff_states(old_states, new_states)
    if not d.added and not d.removed:
        return None
    return d
