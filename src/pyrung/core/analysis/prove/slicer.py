"""Whole-rung program slicing helpers for reachable-state exploration."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.program import Program

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph, RungNode

_UnitKey = tuple[str, int | str]


@dataclass(frozen=True)
class _UnitSummary:
    key: _UnitKey
    read_tags: frozenset[str]
    write_tags: frozenset[str]
    calls: tuple[str, ...]


def _unit_key_for_node(node: RungNode) -> _UnitKey:
    if node.scope == "main":
        return ("main", node.rung_index)
    if node.subroutine is None:
        raise RuntimeError("Subroutine graph node is missing its subroutine name")
    return ("subroutine", node.subroutine)


def _build_unit_summaries(
    program: Program,
    graph: ProgramGraph,
) -> tuple[
    dict[_UnitKey, _UnitSummary],
    dict[str, frozenset[_UnitKey]],
    dict[str, frozenset[_UnitKey]],
    dict[str, frozenset[_UnitKey]],
]:
    grouped: dict[_UnitKey, dict[str, set[Any]]] = {}

    for rung_index in range(len(program.rungs)):
        grouped.setdefault(
            ("main", rung_index),
            {"reads": set(), "writes": set(), "calls": set()},
        )
    for sub_name in program.subroutines:
        grouped.setdefault(
            ("subroutine", sub_name),
            {"reads": set(), "writes": set(), "calls": set()},
        )

    for node in graph.rung_nodes:
        key = _unit_key_for_node(node)
        entry = grouped.setdefault(key, {"reads": set(), "writes": set(), "calls": set()})
        entry["reads"].update(node.condition_reads | node.data_reads)
        entry["writes"].update(node.writes)
        entry["calls"].update(node.calls)

    summaries = {
        key: _UnitSummary(
            key=key,
            read_tags=frozenset(sorted(entry["reads"])),
            write_tags=frozenset(sorted(entry["writes"])),
            calls=tuple(sorted(entry["calls"])),
        )
        for key, entry in grouped.items()
    }

    writers_by_tag_mut: dict[str, set[_UnitKey]] = defaultdict(set)
    readers_by_tag_mut: dict[str, set[_UnitKey]] = defaultdict(set)
    callers_by_sub_mut: dict[str, set[_UnitKey]] = defaultdict(set)

    for key, summary in summaries.items():
        for tag_name in summary.write_tags:
            writers_by_tag_mut[tag_name].add(key)
        for tag_name in summary.read_tags:
            readers_by_tag_mut[tag_name].add(key)
        for sub_name in summary.calls:
            callers_by_sub_mut[sub_name].add(key)

    writers_by_tag = {k: frozenset(v) for k, v in writers_by_tag_mut.items()}
    readers_by_tag = {k: frozenset(v) for k, v in readers_by_tag_mut.items()}
    callers_by_sub = {k: frozenset(v) for k, v in callers_by_sub_mut.items()}
    return summaries, writers_by_tag, readers_by_tag, callers_by_sub


def _select_slice_units(program: Program, seed_tags: list[str]) -> set[_UnitKey]:
    from pyrung.core.analysis.pdg import build_program_graph

    graph = build_program_graph(program)
    summaries, writers_by_tag, readers_by_tag, callers_by_sub = _build_unit_summaries(
        program, graph
    )
    selected: set[_UnitKey] = set()
    queue: deque[_UnitKey] = deque()

    def _add_unit(key: _UnitKey) -> None:
        if key not in summaries or key in selected:
            return
        selected.add(key)
        queue.append(key)

    unique_seed_tags = sorted(set(seed_tags))
    for tag_name in unique_seed_tags:
        writer_units = writers_by_tag.get(tag_name)
        if writer_units:
            for unit_key in writer_units:
                _add_unit(unit_key)
            continue
        for unit_key in readers_by_tag.get(tag_name, ()):
            _add_unit(unit_key)

    while queue:
        unit_key = queue.popleft()
        summary = summaries[unit_key]

        for tag_name in summary.read_tags:
            for writer_key in writers_by_tag.get(tag_name, ()):
                _add_unit(writer_key)

        for sub_name in summary.calls:
            _add_unit(("subroutine", sub_name))

        if unit_key[0] == "subroutine":
            sub_name = unit_key[1]
            assert isinstance(sub_name, str)
            for caller_key in callers_by_sub.get(sub_name, ()):
                _add_unit(caller_key)
            continue

        rung_index = unit_key[1]
        assert isinstance(rung_index, int)
        while rung_index > 0 and program.rungs[rung_index]._use_prior_snapshot:
            rung_index -= 1
            _add_unit(("main", rung_index))

    return selected


def _slice_program_for_reachability(program: Program, seed_tags: list[str]) -> Program:
    """Build an ephemeral whole-rung slice for reachable-state exploration."""
    selected_units = _select_slice_units(program, seed_tags)
    if not selected_units:
        return program

    all_units: set[_UnitKey] = {("main", idx) for idx in range(len(program.rungs))}
    all_units.update(("subroutine", sub_name) for sub_name in program.subroutines)
    if selected_units == all_units:
        return program

    sliced = Program(strict=False)
    sliced.rungs = [
        rung for idx, rung in enumerate(program.rungs) if ("main", idx) in selected_units
    ]
    sliced.subroutines = {
        sub_name: list(rungs)
        for sub_name, rungs in program.subroutines.items()
        if ("subroutine", sub_name) in selected_units
    }
    return sliced
