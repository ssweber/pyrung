"""Relational-goal tests for PILOT — carry ``A op B`` live, don't collapse.

Stage A: the inequality survives the trace boundary as a relational ``TraceNode``
(``relational=True``, ``predicate`` set), the single-lever resolution rides as its
child (steering unchanged), and distance counts the predicate once.
"""

from __future__ import annotations

from pyrung import PLC, Bool, Int, Program, Rung, copy, out
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


# ---------------------------------------------------------------------------
# Stage B2: reactive lever-selection (raise A / lower B)
# ---------------------------------------------------------------------------


def test_relational_emits_both_levers() -> None:
    """``A > B`` with both operands steerable surfaces two lever subtrees."""
    A = Int("A", external=True, default=0)
    B = Int("B", external=True, default=5)
    Target = Bool("Target")

    with Program() as prog:
        with Rung(A > B):
            out(Target)

    pdg = build_program_graph(prog)
    steerable = compute_steerable(pdg, _known(prog), prog)

    tree = trace_back("Target", True, {"A": 3, "B": 5}, pdg, prog, steerable)
    rels = _relational_nodes(tree)
    assert len(rels) == 1
    assert {c.lever for c in rels[0].children} == {"left", "right"}

    leaves = dict(tree.steerable_leaves())
    assert leaves["A"] == 6  # left lever: raise A above B (=5)
    assert leaves["B"] == 2  # right lever: lower B below A (=3), via B < A


def test_lever_selection_uses_rhs_when_lhs_blocked() -> None:
    """When raising A is impossible, PILOT discovers the lower-B lever and solves.

    ``A`` is pinned at 2 by an unconditional ``copy(2, A)`` — its only writer can
    never produce a higher value, so the raise-A lever dead-ends.  The only
    steerable route to ``A > B`` is lowering ``B``.  Without the right lever this
    is unreachable (the pre-Stage-B trace only offered raise-A).
    """
    A = Int("A", default=0)  # internal, pinned low — not steerable
    B = Int("B", external=True, default=5)
    Target = Bool("Target")

    with Program() as prog:
        with Rung():
            copy(2, A)
        with Rung(A > B):
            out(Target)

    plc = PLC(prog, dt=0.010)
    plc.step()  # settle A = 2
    assert plc.state.tags["A"] == 2

    path = pilot_how(plc, Target)
    assert path.reachable

    # The chosen lever is B (A is internal, never steered).
    steered = {tag for step in path.steps for tag in step.action}
    assert "B" in steered
    assert "A" not in steered

    replay = PLC(prog, dt=0.010)
    for step in path.steps:
        replay.patch(step.action)
        for _ in range(step.scans):
            replay.step()
    assert replay.state.tags["Target"] is True
    assert replay.state.tags["A"] == 2
