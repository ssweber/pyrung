"""Read exact writer evidence from an observed scan.

Static program structure says which rungs *could* write a value.  ``RungRun``
records say which exact rung occurrences ran and what each attempted to write.
This module joins those two facts without choosing a route or interpreting why
the write mattered.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.executor import RungRun
    from pyrung.core.program import Program
    from pyrung.core.rung import Rung


def runs_for_node(
    pdg: ProgramGraph,
    program: Program,
    node_index: int,
    runs: Iterable[RungRun],
) -> tuple[RungRun, ...]:
    """Return every observed occurrence of one exact PDG rung node."""
    node = pdg.rung_nodes[node_index]
    rung = resolve_rung(program, node)
    if rung is None:
        return ()
    return tuple(run for run in runs if run.rung is rung)


def writer_runs_for_node(
    pdg: ProgramGraph,
    program: Program,
    node_index: int,
    tag_name: str,
    value: Any,
    runs: Iterable[RungRun],
) -> tuple[RungRun, ...]:
    """Occurrences of one exact node that attempted ``tag_name=value``."""
    result: list[RungRun] = []
    for run in runs_for_node(pdg, program, node_index, runs):
        written = dict(run.writes)
        if tag_name in written and _values_match(written[tag_name], value):
            result.append(run)
    return tuple(result)


def latest_writer_run(
    rung: Rung,
    tag_name: str,
    value: Any,
    runs: Iterable[RungRun],
) -> RungRun | None:
    """Last occurrence of *rung* that attempted the requested write."""
    for run in reversed(tuple(runs)):
        if run.rung is not rung:
            continue
        written = dict(run.writes)
        if tag_name in written and _values_match(written[tag_name], value):
            return run
    return None
