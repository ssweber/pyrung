"""Relational-goal tests for PILOT — carry ``A op B`` live, don't collapse.

Stage A: the inequality survives the trace boundary as a relational ``TraceNode``
(``relational=True``, ``predicate`` set), the single-lever resolution rides as its
child (steering unchanged), and distance counts the predicate once.
"""

from __future__ import annotations

from pyrung import PLC, Bool, Int, Program, Rung, out
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.analysis.pilot.trace import (
    TraceNode,
    _all_nodes,
    compute_steerable,
    trace_back,
)


def _known(logic: Program) -> dict:
    return PLC(logic)._known_tags_by_name


def _relational_nodes(tree: TraceNode) -> list[TraceNode]:
    return [n for n in _all_nodes(tree) if n.relational]


# ---------------------------------------------------------------------------
# Stage A: relational node carried past the trace boundary
# ---------------------------------------------------------------------------


def test_relational_prereq_surfaces_as_node() -> None:
    """A writer gated on ``A > B`` yields a relational node, not a frozen A==k."""
    A = Int("A", external=True, default=0)
    B = Int("B", external=True, default=5)
    Target = Bool("Target")

    with Program() as prog:
        with Rung(A > B):
            out(Target)

    pdg = build_program_graph(prog)
    steerable = compute_steerable(pdg, _known(prog), prog)

    tree = trace_back("Target", True, {"A": 0, "B": 5}, pdg, prog, steerable)

    rels = _relational_nodes(tree)
    assert len(rels) == 1
    rel = rels[0]
    assert rel.predicate is not None
    assert rel.predicate.form == "gt"
    assert rel.tag == "A"
    # The single-lever resolution rides as the child so steering is unchanged:
    # the steerable input A still surfaces as an action.
    assert ("A", 6) in tree.steerable_leaves()


def test_relational_node_counts_once_in_distance() -> None:
    """The relational frontier contributes exactly one to the distance metric.

    Target (writer) + the A>B relational frontier = 2; the steerable A leaf and
    the lever subtree are *means*, not separate goals.
    """
    A = Int("A", external=True, default=0)
    B = Int("B", external=True, default=5)
    Target = Bool("Target")

    with Program() as prog:
        with Rung(A > B):
            out(Target)

    pdg = build_program_graph(prog)
    steerable = compute_steerable(pdg, _known(prog), prog)

    tree = trace_back("Target", True, {"A": 0, "B": 5}, pdg, prog, steerable)
    assert tree.unsatisfied_count() == 2


def test_relational_prereq_solves_end_to_end() -> None:
    """PILOT still drives ``A > B`` to satisfaction (single-lever, Stage A parity)."""
    A = Int("A", external=True, default=0)
    B = Int("B", external=True, default=5)
    Target = Bool("Target")

    with Program() as prog:
        with Rung(A > B):
            out(Target)

    plc = PLC(prog, dt=0.010)
    path = pilot_how(plc, Target)
    assert path.reachable

    replay = PLC(prog, dt=0.010)
    for step in path.steps:
        replay.patch(step.action)
        for _ in range(step.scans):
            replay.step()
    assert replay.state.tags["Target"] is True
