"""The recovery spin guard (findings §2c).

Recovery rounds at every level recreate each other's goals: a parent's
recovery round respawns a child goal, the child re-runs its own rounds,
and the same failing subtree is re-walked ~3^depth times (probe7 on the
PackML template: recovery iters 4-8 all "skipping known-blocked config"
for goals that had already failed under an unchanged nogood store).

The guard: a goal that already failed at the same nogood-projected state,
with nothing learned since (the add-only store has not grown), cannot
succeed — fail the re-request without re-walking the subtree.  A pruned
re-walk is at worst a premature None (the safe direction); never a wrong
plan.
"""

from __future__ import annotations

import logging

import pytest

from pyrung import Bool, Program, Rung, latch, out, reset, rise
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.walk import engine as walk
from pyrung.core.analysis.walk import scheduler as scheduler_mod
from pyrung.core.runner import PLC


def _dead_chain_program():
    """Target needs Mid needs Dead — and Dead/Dead2 are mutually circular
    latches (each needs the other first), so neither can ever fire.

    Every level's recovery keeps re-deriving the same dead sub-goals; the
    verdict is honestly unreachable either way, but without the guard the
    failing subtree is re-walked once per recovery round per level.  Note
    Dead has a non-default writer, so it is neither an ack-cleared input
    nor scan-transient — nothing legitimizes patching it directly.
    """
    GoA = Bool("GoA", external=True)
    GoB = Bool("GoB", external=True)
    GoC = Bool("GoC", external=True)
    Run = Bool("Run", external=True)
    Dead = Bool("Dead")
    Dead2 = Bool("Dead2")
    A = Bool("A")
    B = Bool("B")
    C = Bool("C")
    Target = Bool("Target")

    with Program() as prog:
        with Rung(Dead2):
            latch(Dead)
        with Rung(Dead):
            latch(Dead2)
        with Rung(rise(GoA), Dead):
            latch(A)
        with Rung(rise(GoB), Dead):
            latch(B)
        with Rung(rise(GoC), Dead):
            latch(C)
        with Rung(A, B, C, rise(Run)):
            latch(Target)

    return prog, Target


def _walk_dead_chain() -> tuple[object, int]:
    prog, target = _dead_chain_program()
    plc = PLC(prog, dt=0.010)
    work = plc.fork()
    walk._install_walk_harness(work)
    pdg = build_program_graph(work._program)
    known = work._known_tags_by_name
    ext_inputs = walk._external_bool_inputs(pdg, known)
    edge_ext = walk._edge_tags(pdg, work._program) & set(ext_inputs)
    nogoods = walk.NoGoodStore()
    steps = walk._walk_to_goal(
        work,
        target.name,
        True,
        pdg,
        work._program,
        known,
        ext_inputs,
        edge_ext,
        64,
        nogoods=nogoods,
        holds=walk.HoldStore(),
    )
    return steps, nogoods.recovery_iters


def test_spin_guard_prunes_re_walks_and_keeps_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(scheduler_mod, "_SPIN_GUARD", False)
    steps_off, iters_off = _walk_dead_chain()
    assert steps_off is None, "premise: the chain is honestly unreachable"

    monkeypatch.setattr(scheduler_mod, "_SPIN_GUARD", True)
    with caplog.at_level(logging.INFO, logger="pyrung.core.analysis.walk"):
        steps_on, iters_on = _walk_dead_chain()
    assert steps_on is None  # same honest verdict
    # The guard engages and strictly reduces the recovery effort.
    assert any("spin guard" in r.message for r in caplog.records)
    assert iters_on < iters_off


def test_spin_guard_does_not_block_progress_after_learning() -> None:
    """A goal that failed, then became feasible after a nogood was learned
    and its blocker cleared, must still be re-walkable: the guard keys on
    the store generation, and learning grows the store."""
    Arm = Bool("Arm", external=True)
    Guard = Bool("Guard")
    Reset_ = Bool("Reset_", external=True)
    Set_ = Bool("Set_", external=True)
    Latch_ = Bool("Latch_")
    Target = Bool("Target")

    with Program() as prog:
        with Rung(rise(Arm)):
            latch(Guard)
        with Rung(rise(Set_), ~Guard):
            latch(Latch_)
        with Rung(rise(Reset_)):
            reset(Guard)
        with Rung(Latch_):
            out(Target)

    plc = PLC(prog, dt=0.010)
    # Pre-arm the guard so the first attempt fails and recovery must
    # clear it (the cross-guard dynamic the recovery loop exists for).
    plc.patch({"Arm": True})
    plc.step()
    plc.patch({"Arm": False})
    plc.step()
    assert plc.state.tags["Guard"] is True
    assert plc.state.tags["Latch_"] is False

    path = plc.how(Target)
    assert path.reachable
