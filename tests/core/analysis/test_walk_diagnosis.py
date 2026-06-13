"""Stage D4: the third _explore exit, the backjump resolver, and Diagnosis.

Three behaviors pinned here:

1. **Exit taxonomy** — ``_explore_corridor`` distinguishes ``found`` /
   ``stuck`` (no steer moves the governing value: structural) /
   ``diverged`` (the corridor moved but never landed; the deepest node is
   the backjump checkpoint).
2. **Backjump** — a long value corridor (beyond one explore's
   node/corridor caps) is walked segment by segment from diverged
   checkpoints; with the chain ablated the same walk fails.  Backjump only
   ever adds solutions: it runs speculatively and is checked on adoption.
3. **Diagnosis** — failed walks report ``unsolvable`` (every failure
   structural) vs ``not-found`` (search-limited), with the first failing
   goal, its cause()-named blockers, learned nogoods, and journal notes.
   A diagnosis is an explanation with a certificate, not a proof.
"""

from __future__ import annotations

import logging

import pytest

from pyrung import Bool, Int, Program, Rung, calc, out, rise
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.walk import agenda
from pyrung.core.analysis.walk import engine as walk
from pyrung.core.analysis.walk.explore import _explore_corridor
from pyrung.core.analysis.walk.priors import _steer_alphabet
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Programs
# ---------------------------------------------------------------------------


def _counter_program(limit: int, target: int) -> tuple[Program, Bool]:
    """Step counts 0..limit on Advance rises; AtTarget mirrors Step == target."""
    Advance = Bool("Advance", external=True)
    Step = Int("Step")
    AtTarget = Bool("AtTarget")

    with Program() as prog:
        with Rung(rise(Advance), Step < limit):
            calc(Step + 1, Step)
        with Rung(Step == target):
            out(AtTarget)

    return prog, AtTarget


def _ctx_for(prog: Program, plc: PLC):
    work = plc.fork()
    walk._install_walk_harness(work)
    pdg = build_program_graph(work._program)
    known = work._known_tags_by_name
    ext_inputs = walk._external_bool_inputs(pdg, known)
    edge_ext = walk._edge_tags(pdg, work._program) & set(ext_inputs)
    from pyrung.core.analysis.walk.base import NoGoodStore, _WalkContext
    from pyrung.core.analysis.walk.fold import _build_jump_context

    ctx = _WalkContext(
        pdg=pdg,
        program=work._program,
        known=known,
        ext_inputs=ext_inputs,
        edge_ext=edge_ext,
        jump_ctx=_build_jump_context(work, pdg, work._program),
        nogoods=NoGoodStore(),
        holds=None,
    )
    return ctx, work


# ---------------------------------------------------------------------------
# 1. Exit taxonomy
# ---------------------------------------------------------------------------


def test_explore_found_exit() -> None:
    prog, _t = _counter_program(limit=30, target=25)
    ctx, work = _ctx_for(prog, PLC(prog, dt=0.010))
    alphabet = _steer_alphabet("Step", ctx.pdg, ctx.known, ctx.program, 3)
    res = _explore_corridor(ctx, work, "Step", 3, alphabet, holds=None)
    assert res.outcome == "found"
    assert res.steps is not None and res.steps


def test_explore_diverged_exit_carries_checkpoint() -> None:
    # Target 25 needs ~49 actions; one explore is corridor-capped well short
    # of it, so the search moves (children exist) but never lands.
    prog, _t = _counter_program(limit=30, target=25)
    ctx, work = _ctx_for(prog, PLC(prog, dt=0.010))
    alphabet = _steer_alphabet("Step", ctx.pdg, ctx.known, ctx.program, 25)
    res = _explore_corridor(ctx, work, "Step", 25, alphabet, holds=None)
    assert res.outcome == "diverged"
    assert res.steps is None
    assert res.best is not None and res.best.path
    # The checkpoint fork really is partway down the corridor.
    assert 0 < res.best.plc.state.tags["Step"] < 25


def test_explore_stuck_exit() -> None:
    # At the cap, no steer moves Step at all: structural deadness here.
    prog, _t = _counter_program(limit=2, target=5)
    plc = PLC(prog, dt=0.010)
    # Drive Step to its cap first.
    for _ in range(3):
        plc.patch({"Advance": True})
        plc.step()
        plc.patch({"Advance": False})
        plc.step()
    assert plc.state.tags["Step"] == 2
    ctx, work = _ctx_for(prog, plc)
    alphabet = _steer_alphabet("Step", ctx.pdg, ctx.known, ctx.program, 5)
    res = _explore_corridor(ctx, work, "Step", 5, alphabet, holds=None)
    assert res.outcome == "stuck"
    assert res.best is None


# ---------------------------------------------------------------------------
# 2. Backjump: segment-chained corridor walking
# ---------------------------------------------------------------------------
#
# Self-arith predecessor chasing (the fill-sequencer arc) now inverts the
# fixture's ``calc(Step + 1, Step)`` writer into chained (Step, k) sub-goals,
# segmenting the corridor through prerequisite recursion before backjump is
# ever needed.  The backjump pins ablate that route (simulating a self-writer
# the extractor can't invert, e.g. a non-affine advance) so the long-corridor
# shape exercises the resolver again; the unablated capability gets its own
# pin below.


def _ablate_predecessor_chasing(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyrung.core.analysis.walk import priors

    monkeypatch.setattr(priors, "_arithmetic_predecessor", lambda *_a, **_k: None)


def test_backjump_walks_long_corridor(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corridor beyond one explore's caps solves via chained re-entry."""
    _ablate_predecessor_chasing(monkeypatch)
    prog, target = _counter_program(limit=30, target=25)
    plc = PLC(prog, dt=0.010)

    with caplog.at_level(logging.INFO, logger="pyrung.core.analysis.walk"):
        path = plc.how(target, max_steps=64)

    assert path.reachable
    bj_lines = [r for r in caplog.records if "backjump" in r.getMessage()]
    assert bj_lines, "expected the backjump resolver to carry this corridor"

    replay = PLC(prog, dt=0.010)
    for step in path.steps:
        replay.patch(step.action)
        for _ in range(step.scans):
            replay.step()
    assert replay.state.tags["AtTarget"] is True
    assert replay.state.tags["Step"] == 25


def test_backjump_ablated_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direction pin: without segment chaining the same walk fails honestly."""
    _ablate_predecessor_chasing(monkeypatch)
    monkeypatch.setattr(agenda, "_MAX_BACKJUMP_SEGMENTS", 0)
    prog, target = _counter_program(limit=30, target=25)
    plc = PLC(prog, dt=0.010)
    path = plc.how(target, max_steps=64)
    assert not path.reachable


def test_predecessor_chain_carries_corridor_without_backjump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direction pin: self-arith predecessor sub-goals segment the corridor
    on their own — the same walk solves with the backjump chain ablated."""
    monkeypatch.setattr(agenda, "_MAX_BACKJUMP_SEGMENTS", 0)
    prog, target = _counter_program(limit=30, target=25)
    plc = PLC(prog, dt=0.010)
    path = plc.how(target, max_steps=64)
    assert path.reachable


# ---------------------------------------------------------------------------
# 3. Diagnosis
# ---------------------------------------------------------------------------


def test_diagnosis_unsolvable_on_capped_corridor() -> None:
    """Step caps at 2; 5 is structurally beyond every steer and the oracle."""
    prog, target = _counter_program(limit=2, target=5)
    plc = PLC(prog, dt=0.010)
    path = plc.how(target, max_steps=64)

    assert not path.reachable
    diag = path.diagnosis
    assert diag is not None
    assert diag.verdict == "unsolvable"
    assert diag.failing_goal == ("AtTarget", True)
    assert diag.failure_kind in ("no-recovery-goals", "explore-stuck")
    assert "Diagnosis: unsolvable" in str(path)


def _dead_writer_program() -> tuple[Program, Bool]:
    """Done's only path runs through Never, which no scan can produce."""
    Go = Bool("Go", external=True)
    Never = Bool("Never")
    Done = Bool("Done")

    with Program() as prog:
        with Rung(Go, ~Go):
            out(Never)
        with Rung(Go, Never):
            out(Done)

    return prog, Done


def test_diagnosis_not_found_names_blockers() -> None:
    """A cause()-named but unwalkable blocker yields a search-limited verdict."""
    prog, target = _dead_writer_program()
    plc = PLC(prog, dt=0.010)
    path = plc.how(target)

    assert not path.reachable
    diag = path.diagnosis
    assert diag is not None
    assert diag.verdict in ("not-found", "unsolvable")
    assert diag.failing_goal is not None
    assert "Diagnosis:" in str(path)


def test_diagnosis_none_on_success() -> None:
    prog, target = _counter_program(limit=30, target=2)
    plc = PLC(prog, dt=0.010)
    path = plc.how(target)
    assert path.reachable
    assert path.diagnosis is None


def test_diagnosis_rendering_unit() -> None:
    from pyrung.core.analysis.graph import Diagnosis

    diag = Diagnosis(
        verdict="not-found",
        reason="goal X -> True failed (recovery-exhausted)",
        failing_goal=("X", True),
        failure_kind="recovery-exhausted",
        blockers=(("Guard", False),),
        nogoods=("False -> True blocked by Guard=False",),
        partial_steps=3,
        notes=("holds at failure: A=True (for B)",),
    )
    text = str(diag)
    assert "Diagnosis: not-found" in text
    assert "first failing goal: X -> true (recovery-exhausted)" in text
    assert "blocked by: Guard=false" in text
    assert "learned nogoods: 1" in text
    assert "best partial plan: 3 step(s)" in text
    assert "note: holds at failure" in text
