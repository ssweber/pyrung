"""Progress-aware prerequisite depth for the corridor walker."""

from __future__ import annotations

from types import SimpleNamespace

from pyrung import Bool, Program, Rung, latch
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.walk.agenda import (
    _credit_progress,
    _drive,
    _establish,
    _PlanNode,
    _Request,
)
from pyrung.core.analysis.walk.base import (
    _MAX_PREREQ_DEPTH,
    _progress_depth_limit,
    _WalkBudget,
    _WalkContext,
    HoldStore,
    NoGoodStore,
)
from pyrung.core.analysis.walk.fold import _build_jump_context
from pyrung.core.analysis.walk.passes import run_walk_passes
from pyrung.core.runner import PLC


def _go_target_program() -> tuple[Program, Bool, Bool]:
    go = Bool("Go", external=True)
    target = Bool("Target")
    with Program() as prog:
        with Rung(go):
            latch(target)
    return prog, go, target


def _context(
    plc: PLC,
    prog: Program,
    target: Bool,
) -> _WalkContext:
    pdg = build_program_graph(prog)
    advice, journal = run_walk_passes(prog, pdg)
    return _WalkContext(
        pdg=pdg,
        program=prog,
        known=plc._known_tags_by_name,
        ext_inputs=["Go"],
        edge_ext=set(),
        jump_ctx=_build_jump_context(
            plc,
            pdg,
            prog,
            target_names=frozenset({target.name}),
            advice=advice,
            journal=journal,
        ),
        nogoods=NoGoodStore(),
        holds=HoldStore(),
        budget=_WalkBudget(),
        advice=advice,
        journal=journal,
    )


def _run_at_depth(ctx: _WalkContext, plc: PLC, target: Bool, depth: int):
    req = _Request(
        runner=plc,
        goal=(target.name, True),
        depth=depth,
        visited=frozenset(),
        budget=16,
        provenance="test",
    )
    node = _PlanNode(goal=req.goal, provenance=req.provenance, depth=req.depth)
    result = _drive(ctx, _establish(ctx, req, node), node, plc)
    return result, node


def test_progress_depth_limit_counts_distinct_progress_goals() -> None:
    ctx = SimpleNamespace(progress_goals=set())
    assert _progress_depth_limit(ctx) == _MAX_PREREQ_DEPTH

    assert _credit_progress(ctx, "A", 1)
    assert _progress_depth_limit(ctx) == _MAX_PREREQ_DEPTH + 2

    assert not _credit_progress(ctx, "A", 1)
    assert _progress_depth_limit(ctx) == _MAX_PREREQ_DEPTH + 2

    assert _credit_progress(ctx, "B", 1)
    assert _progress_depth_limit(ctx) == _MAX_PREREQ_DEPTH + 4


def test_progress_depth_limit_caps_at_max_bonus() -> None:
    ctx = SimpleNamespace(progress_goals=set())
    for i in range(20):
        _credit_progress(ctx, f"Tag{i}", True)
    assert _progress_depth_limit(ctx) == _MAX_PREREQ_DEPTH + 12


def test_depth_seven_refused_without_progress_credit() -> None:
    prog, _go, target = _go_target_program()
    plc = PLC(prog, dt=0.010)
    ctx = _context(plc, prog, target)

    result, node = _run_at_depth(ctx, plc, target, depth=7)

    assert result is None
    assert node.failure == "bounds"
    assert plc.state.tags[target.name] is False


def test_depth_seven_admitted_after_two_committed_progress_credits() -> None:
    prog, _go, target = _go_target_program()
    plc = PLC(prog, dt=0.010)
    ctx = _context(plc, prog, target)
    _credit_progress(ctx, "Mode", 1)
    _credit_progress(ctx, "DryStep", True)

    result, node = _run_at_depth(ctx, plc, target, depth=7)

    assert result
    assert node.status == "solved"
    assert plc.state.tags[target.name] is True
    assert (target.name, True) in ctx.progress_goals
    assert _progress_depth_limit(ctx) == _MAX_PREREQ_DEPTH + 6
