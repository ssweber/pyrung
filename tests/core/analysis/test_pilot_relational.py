"""Relational-goal tests for PILOT — carry ``A op B`` live, don't collapse.

Stage A: the inequality survives the trace boundary as a relational ``TraceNode``
(``relational=True``, ``predicate`` set), the single-lever resolution rides as its
child (steering unchanged), and distance counts the predicate once.
"""

from __future__ import annotations

from pyrung import PLC, Bool, Int, Program, Real, Rung, copy, out
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.analysis.pilot.trace import (
    TraceNode,
    trace_back,
)
from pyrung.core.analysis.steerable import compute_steerable
from pyrung.core.physical import Physical, Ramp

# Harness-linked thermal ramp: Temp rises while its link (Enable) is held.
_RAMP = Physical("RelThermal", profile=Ramp(up=1.0, down=-0.5))


def _known(logic: Program) -> dict:
    return PLC(logic)._known_tags_by_name


def _relational_nodes(tree: TraceNode) -> list[TraceNode]:
    return [n for n in tree.iter_nodes() if n.relational]


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

    replay = path.replay()
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
    steered = set(path.changes)
    assert "B" in steered
    assert "A" not in steered

    replay = path.replay()
    assert replay.state.tags["Target"] is True
    assert replay.state.tags["A"] == 2


# ---------------------------------------------------------------------------
# Regression: prior must propagate through Bool-chain (xic) recursion, so a
# literal-operand inequality prereq one+ hops deep keeps its domain.
# ---------------------------------------------------------------------------


def test_literal_inequality_prereq_through_bool_chain() -> None:
    """A literal-operand inequality one Bool-hop deep keeps its domain and solves.

    ``Temp > 75`` gates ``Hot``, which gates ``Alarm``.  Resolving ``Temp > 75``
    needs the prover domain (literal operand) — which only reaches the atom if
    ``prior`` propagates through the ``Hot`` (xic) recursion.  Before that fix
    the inequality dropped one hop down and ``Alarm`` was unreachable.
    """
    Temp = Int("Temp", external=True, min=0, max=100)
    Hot = Bool("Hot")
    Alarm = Bool("Alarm")

    with Program() as prog:
        with Rung(Temp > 75):
            out(Hot)
        with Rung(Hot):
            out(Alarm)

    plc = PLC(prog, dt=0.010)
    path = pilot_how(plc, Alarm)
    assert path.reachable

    replay = path.replay()
    assert replay.state.tags["Alarm"] is True
    assert replay.state.tags["Temp"] > 75


def test_program_owned_relational_guard_surfaces_active_clobber_permissive() -> None:
    """An internal step range must expose both its starter and active clobber."""
    Start = Bool("OwnedRange_Start", external=True)
    Permissive = Bool("OwnedRange_Permissive", external=True)
    Step = Int("OwnedRange_Step")
    Up = Bool("OwnedRange_Up")

    with Program() as prog:
        with Rung(Start):
            copy(40, Step)
        with Rung(~Permissive):
            copy(99, Step)
        with Rung(Step >= 40, Step < 50):
            out(Up)

    events = []
    path = pilot_how(PLC(prog), Up, max_scans=100, on_event=events.append)
    assert path.reachable
    tree = next(event.data["tree"] for event in events if event.kind == "iteration")

    assert set(tree.steerable_leaves()) == {
        (Start.name, True),
        (Permissive.name, True),
    }


def test_program_owned_witness_preserves_boundary_scalar_type() -> None:
    """An integral domain key stays float in a floating comparison coordinate."""
    Start = Bool("OwnedCoordinate_Start", external=True)
    ProcessValue = Real("OwnedCoordinate_PV", external=True, default=0.0)
    Bound = Int("OwnedCoordinate_Bound")
    Target = Bool("OwnedCoordinate_Target")

    with Program() as prog:
        with Rung(Start):
            copy(1, Bound)
        with Rung(ProcessValue < Bound):
            out(Target)

    events = []
    path = pilot_how(PLC(prog), Target, max_scans=100, on_event=events.append)
    assert path.reachable
    tree = next(event.data["tree"] for event in events if event.kind == "iteration")
    writer_witnesses = [
        node.value
        for node in tree.iter_nodes()
        if node.tag == Bound.name and node.writer_rung is not None
    ]

    assert writer_witnesses
    assert all(isinstance(value, float) for value in writer_witnesses)


def test_relational_guard_defers_to_concrete_demand() -> None:
    """A guard inequality yields to a sibling concrete demand on the same tag.

    ``Mode == 2`` needs ``ModeSel == 2`` (copy source); the same ``ModeSel`` is
    guarded by ``ModeSel >= 1``.  The value 2 already satisfies the guard, so
    reconciliation must drop the guard's boundary lever (``ModeSel == 1``) —
    otherwise PILOT steers ``ModeSel`` to 1, ``Mode`` copies 1, and ``Mode == 2``
    never holds.
    """
    ModeSel = Int("ModeSel", external=True, min=0, max=5)
    Mode = Int("Mode")
    Target = Bool("Target")

    with Program() as prog:
        with Rung(ModeSel >= 1):
            copy(ModeSel, Mode)
        with Rung(Mode == 2):
            out(Target)

    plc = PLC(prog, dt=0.010)
    path = pilot_how(plc, Target)
    assert path.reachable

    # ModeSel must be driven to 2 (the concrete demand), not the guard boundary 1.
    assert path.changes.get("ModeSel") == 2

    replay = path.replay()
    assert replay.state.tags["Target"] is True
    assert replay.state.tags["Mode"] == 2


# ---------------------------------------------------------------------------
# Stage C: relational how() targets (rejected before this change)
# ---------------------------------------------------------------------------


def test_relational_target_literal_threshold() -> None:
    """``how(Temp > 75)`` — a relational target with a literal threshold."""
    Temp = Int("Temp", external=True, min=0, max=100)
    Hot = Bool("Hot")

    with Program() as prog:
        with Rung(Temp > 75):
            out(Hot)

    plc = PLC(prog, dt=0.010)
    path = pilot_how(plc, Temp > 75)
    assert path.reachable

    replay = path.replay()
    assert replay.state.tags["Temp"] > 75


def test_relational_target_tag_vs_tag() -> None:
    """``how(A > B)`` — a relational target comparing two live tags."""
    A = Int("A", external=True, default=0, min=0, max=10)
    B = Int("B", external=True, default=5, min=0, max=10)
    Flag = Bool("Flag")

    with Program() as prog:
        with Rung(A > B):
            out(Flag)

    plc = PLC(prog, dt=0.010)
    path = pilot_how(plc, A > B)
    assert path.reachable

    replay = path.replay()
    assert replay.state.tags["A"] > replay.state.tags["B"]


# ---------------------------------------------------------------------------
# Stage B1: relational let-run — coast when the LHS converges on the boundary
# ---------------------------------------------------------------------------


def test_relational_frontier_coasts_to_converge() -> None:
    """A converging frontier coasts across a threshold that can't be steered.

    ``Temp`` is harness-driven (ramps while ``Enable`` is held) — PILOT can't
    set it.  ``Limit`` is pinned to 8 by ``copy(8, Limit)`` — the lower-the-bar
    lever dead-ends.  Neither operand is steerable to satisfaction, so the only
    move is to hold ``Enable`` and let ``Temp`` ramp across ``Limit``.  PILOT must
    surface the frontier as a coast leaf and escalate to let-run rather than
    bailing at the opaque dead-end.
    """
    Enable = Bool("Enable", external=True)
    Temp = Real("Temp", physical=_RAMP, link="Enable")
    Limit = Int("Limit")
    Stage = Int("Stage")

    with Program() as prog:
        with Rung():
            copy(8, Limit)
        with Rung(Enable, Temp >= Limit):
            copy(1, Stage)

    plc = PLC(prog, dt=0.010)
    path = pilot_how(plc, Stage == 1, max_scans=3000)
    assert path.reachable

    replay = path.replay()
    assert replay.state.tags["Stage"] == 1
    assert replay.state.tags["Temp"] >= replay.state.tags["Limit"]
