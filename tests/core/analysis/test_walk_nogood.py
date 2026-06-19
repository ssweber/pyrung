"""Phase 4 nogood learning (precondition accumulation) for the corridor walker.

The serial-clobber recovery loop (``_recover_via_oracle``) re-derives the
blocking sub-goals from ``cause(tag, to=value)`` every iteration but keeps **no
memory** of what blocked a transition.  When two latches *mutually* clobber each
other through symmetric cross-guards, the naive loop re-attempts the same
ordering each round, re-clobbering, and exhausts ``_MAX_RECHECK_ITERS`` without
converging.

Nogood learning records, after a failed/clobbering round, a nogood keyed on
``(from_value, to_value, frozenset(blocking))`` — the cause()-named
still-unsatisfied ``(tag, needed_value)`` pairs.  The ``_explore`` seen-key is
projected onto the learned blocking-tag names, so a re-walk can re-enter a
governing value under different learned constraints, and a repeat of a
proven-dead config trips ``is_blocked`` and bails immediately instead of
burning another iteration.

See ``scratchpad/corridor_walker_plan.md`` and the approved plan
``stateful-snacking-orbit.md``.
"""

from __future__ import annotations

import logging

import pytest

from pyrung import (
    And,
    Bool,
    Or,
    Program,
    Rung,
    Timer,
    on_delay,
    out,
)
from pyrung.core.analysis.walk import engine as walk
from pyrung.core.runner import PLC


def _program() -> tuple[Program, Bool]:
    """Cross-guard mutual-clobber tripwire (self-sealing, both sides timer-gated).

    Two latches feed ``Target``.  Each latch *self-seals* once set, but its
    **arming** is gated by the *other* latch's guard, and arming seals that
    guard:

    - ``Latch_A`` arms when a 0.100 s on-delay (``TimerA``, driven by holding
      ``Input_A``) completes, only while ``~Guard_B``; once set it self-seals.
    - ``Latch_B`` arms when ``TimerB`` (holding ``Input_B``) completes, only
      while ``~Guard_A``; once set it self-seals.
    - ``Guard_A`` seals when ``TimerA`` completes (the same action that arms
      ``Latch_A``); ``Guard_B`` seals when ``TimerB`` completes.  Both guards
      are cleared by ``Reset_Cmd``.
    - ``Target`` requires both latches True.

    The clobber is **mutual at arm time**: arming A seals ``Guard_A``, which
    blocks B's arm; arming B seals ``Guard_B``, which blocks A's arm.  But once
    a latch is sealed it is *immune* (its self-seal has no guard), so a guard
    can be cleared after sealing without dropping its latch.  Two timers (no
    ``rise()`` edges anywhere) kill the multi-input "arm both in one scan"
    shortcut that would otherwise side-step the clobber, and keep the pulse
    steer from co-firing an edge input.

    Why the naive loop fails (measured): the walker drives ``Latch_A`` first
    (which seals ``Guard_A``).  Recovering ``Latch_B`` requires both
    ``TimerB_Done=True`` (hold ``Input_B``) and ``Guard_A=False`` (a
    ``Reset_Cmd`` pulse) — and ``cause(Latch_B, to=True)`` correctly names both
    at the clobbered state.  But the goals are order-coupled through the
    *non-retentive* ``on_delay``: a scoped sub-walk holds ``Input_B`` (so
    ``TimerB_Done`` goes True), then *releases* ``Input_B`` when it ends, which
    drops ``TimerB_Done`` again; the later ``Reset_Cmd`` clears ``Guard_A`` but
    the timer condition is already gone, so ``Latch_B`` never arms.  Worse, once
    the timer has pulsed True the scan log *contains* that transition, so the
    next projected ``cause()`` reclassifies ``TimerB_Done`` from a blocker into a
    trigger and then suppresses it (the all-or-nothing blocker rule fires because
    ``Latch_B``'s self-seal leaf is still unobserved) — the re-check no longer
    even names the timer goal.  And ``_explore`` keys ``seen`` only on the
    governing value (``Latch_B``): pulsing ``Reset_Cmd`` does not change
    ``Latch_B``, so that intermediate node collapses onto the start node and is
    pruned — the "Reset, then hold-B" corridor is never discovered.  The naive
    re-check loop has no memory of the coupling.

    The correct sequence (proven by ``test_program_is_forward_reachable``):
    hold ``Input_A`` (``Latch_A`` self-seals, ``Guard_A`` seals), ``Reset_Cmd``
    (clears ``Guard_A``; A holds via self-seal), hold ``Input_B`` (``Guard_A``
    clear so ``Latch_B`` arms and self-seals).  Nogood learning records the
    cause()-named blocking assignment; the seen-key projection onto those tags
    plus the blocker-clearing move in ``_explore`` then makes the post-Reset
    node distinct, opening the guard-clearing corridor.
    """
    Input_A = Bool("Input_A", external=True)
    Input_B = Bool("Input_B", external=True)
    Reset_Cmd = Bool("Reset_Cmd", external=True)
    Latch_A = Bool("Latch_A")
    Latch_B = Bool("Latch_B")
    Guard_A = Bool("Guard_A")
    Guard_B = Bool("Guard_B")
    TimerA = Timer.clone("TimerA")
    TimerB = Timer.clone("TimerB")
    Target = Bool("Target")

    with Program() as prog:
        # Timers: hold Input_A / Input_B for 0.100 s = 10 scans at dt=0.010.
        with Rung(Input_A):
            on_delay(TimerA, 100, "ms")
        with Rung(Input_B):
            on_delay(TimerB, 100, "ms")
        # Latch A: timer-gated arm while ~Guard_B; self-seals.
        with Rung(Or(And(TimerA.Done, ~Guard_B), Latch_A)):
            out(Latch_A)
        # Latch B: timer-gated arm while ~Guard_A; self-seals.
        with Rung(Or(And(TimerB.Done, ~Guard_A), Latch_B)):
            out(Latch_B)
        # Guard A: seals when TimerA completes (arms Latch_A); cleared by Reset.
        with Rung(Or(TimerA.Done, Guard_A), ~Reset_Cmd):
            out(Guard_A)
        # Guard B: seals when TimerB completes (arms Latch_B); cleared by Reset.
        with Rung(Or(TimerB.Done, Guard_B), ~Reset_Cmd):
            out(Guard_B)
        # Target.
        with Rung(Latch_A, Latch_B):
            out(Target)

    return prog, Target


def _slow_program() -> tuple[Program, Bool]:
    """Slow-but-solvable variant: the proven ``_clobber_program`` shape.

    The naive loop *does* converge on ``_clobber_program`` (B self-seals, so
    one Reset-then-re-arm-A round suffices); kept as efficiency-evidence
    fallback should the capability tripwire ever be weakened.
    """
    from tests.core.analysis.test_walk_decomposition import _clobber_program

    return _clobber_program()


# ---------------------------------------------------------------------------
# Phase A premise + capability tests
# ---------------------------------------------------------------------------


def test_program_is_forward_reachable() -> None:
    """Premise: a correct manual action sequence drives Target=True.

    1. Hold Input_A  -> TimerA completes; Latch_A self-seals, Guard_A seals.
    2. Pulse Reset   -> Guard_A clears; Latch_A holds (self-seal).
    3. Hold Input_B  -> TimerB completes; Guard_A clear so Latch_B arms +
                        self-seals; Guard_B seals.  Target=True.
    """
    prog, _Target = _program()
    plc = PLC(prog, dt=0.010)

    # 1. Hold Input_A until the on-delay completes -> Latch_A self-seals,
    #    Guard_A seals.
    plc.patch({"Input_A": True})
    for _ in range(15):
        plc.step()
    plc.patch({"Input_A": False})
    plc.step()
    assert plc.state.tags["Latch_A"] is True
    assert plc.state.tags["Guard_A"] is True
    assert plc.state.tags["Latch_B"] is False

    # 2. Reset -> Guard_A clears; Latch_A holds via its self-seal.
    plc.patch({"Reset_Cmd": True})
    plc.step()
    plc.patch({"Reset_Cmd": False})
    plc.step()
    assert plc.state.tags["Guard_A"] is False
    assert plc.state.tags["Latch_A"] is True  # self-sealed, unaffected by Reset

    # 3. Hold Input_B until the on-delay completes -> Latch_B arms (Guard_A
    #    clear) and self-seals; Guard_B seals but cannot drop the sealed A.
    plc.patch({"Input_B": True})
    for _ in range(15):
        plc.step()
    assert plc.state.tags["Latch_B"] is True
    assert plc.state.tags["Latch_A"] is True
    assert plc.state.tags["Target"] is True


def test_nogood_walker_recovers() -> None:
    """``plc.how(Target)`` solves the cross-guard mutual clobber.

    The naive recovery loop cannot solve this (it re-walks the same ordering
    every round); nogood learning + the refined seen-key opens the
    guard-clearing ordering.
    """
    prog, Target = _program()
    plc = PLC(prog, dt=0.010)
    path = plc.how(Target)
    assert path.reachable


def test_nogood_walker_replay() -> None:
    """The recovered plan replays to Target=True on a fresh PLC."""
    prog, Target = _program()
    plc = PLC(prog, dt=0.010)
    path = plc.how(Target)
    assert path.reachable

    replay = PLC(prog, dt=0.010)
    for step in path.steps:
        replay.patch(step.action)
        for _ in range(step.scans):
            replay.step()
    assert replay.state.tags["Target"] is True


# ---------------------------------------------------------------------------
# NoGoodStore unit test
# ---------------------------------------------------------------------------


def test_nogood_store_add_query_project() -> None:
    """Pure unit test on ``walk.NoGoodStore``."""
    store = walk.NoGoodStore()

    # Empty-store identity: project drops everything, recovery counter at 0.
    assert store.project({"X": True, "Y": False}) == ()
    assert store.recovery_iters == 0
    assert store.blocking_tag_names() == frozenset()

    blocking = frozenset({("Guard_B", False), ("Latch_A", True)})

    # First add grows the store; re-add of the same nogood does not.
    assert store.add("S_State", 0, 1, blocking) is True
    assert store.add("S_State", 0, 1, blocking) is False

    # Exact membership.
    assert store.is_blocked("S_State", 0, 1, blocking) is True
    assert store.is_blocked("S_State", 0, 1, frozenset({("Guard_B", False)})) is False
    assert store.is_blocked("S_State", 1, 0, blocking) is False
    # Different tag — same transition values must not match.
    assert store.is_blocked("Other_Tag", 0, 1, blocking) is False

    # Projection basis = union of tag names across nogoods; name-only.
    assert store.blocking_tag_names() == frozenset({"Guard_B", "Latch_A"})
    snap = {"Guard_B": False, "Latch_A": True, "Unrelated": 7}
    proj = store.project(snap)
    assert dict(proj) == {"Guard_B": False, "Latch_A": True}
    assert "Unrelated" not in dict(proj)

    # all_orderings_blocked: matches on the transition alone — the caller's
    # prereqs come from the static SP-tree while nogood keys are cause()-named
    # assignments, so blocking-set equality is deliberately ignored.
    assert (
        store.all_orderings_blocked("S_State", 0, 1, [("Guard_B", False), ("Latch_A", True)])
        is True
    )
    assert store.all_orderings_blocked("S_State", 0, 1, [("Guard_B", False)]) is True
    assert (
        store.all_orderings_blocked("S_State", 5, 6, [("Guard_B", False), ("Latch_A", True)])
        is False
    )
    # Different tag with same from/to values must not match.
    assert store.all_orderings_blocked("Other_Tag", 0, 1, [("Guard_B", False)]) is False


def test_nogood_store_records_relation_facts() -> None:
    """Relation facts preserve comparison shape and project mentioned tags."""
    store = walk.NoGoodStore()
    relation = walk.NoGoodFact.relation(
        "pv_LevelHt",
        "<",
        "calc_levelSvLowerWBand",
        0.0,
        ("pv_LevelHt", "calc_levelSvLowerWBand"),
    )
    blocking = frozenset({relation})

    assert store.add("Level_Ok", False, True, blocking) is True
    assert store.add("Level_Ok", False, True, blocking) is False
    assert store.is_blocked("Level_Ok", False, True, blocking) is True

    assert store.blocking_tag_names() == frozenset({"pv_LevelHt", "calc_levelSvLowerWBand"})
    assert dict(store.project({"pv_LevelHt": 100.0, "calc_levelSvLowerWBand": 0.0})) == {
        "pv_LevelHt": 100.0,
        "calc_levelSvLowerWBand": 0.0,
    }
    entry = store.entries()[0][3][0]
    assert entry == "pv_LevelHt < calc_levelSvLowerWBand (rhs=0.0)"


# ---------------------------------------------------------------------------
# Iteration-efficiency test (direct _walk_to_goal call)
# ---------------------------------------------------------------------------


def test_nogood_solves_in_few_recovery_iters(caplog: pytest.LogCaptureFixture) -> None:
    """Nogood-aware recovery converges in <= 2 recovery iterations.

    Naive baseline (measured during exploration with the pre-Phase-4 loop):
    ``plc.how(Target)`` returned ``reachable=False`` — the loop re-derived
    ``cause(Latch_B)`` once, blindly re-walked the cause goals serially (a scoped
    sub-walk held ``Input_B`` for the timer, then released it on exit — dropping
    the non-retentive ``TimerB_Done`` — so the later ``Reset_Cmd`` cleared
    ``Guard_A`` with the timer condition already gone), made no net progress, and
    gave up.  It never converged.
    Nogood-aware recovery converges in exactly 2 recovery iters here (iter 1 at
    the ``Target`` level surfaces ``Latch_B``; iter 2 at the ``Latch_B`` level
    records the ``{Guard_A, TimerB_Done}`` blocker and the refined seen-key +
    blocker-clearing move opens the ``Reset``-then-hold-B corridor).  Recorded
    so a regression that re-introduces re-clobbering is caught.
    """
    prog, Target = _program()
    plc = PLC(prog, dt=0.010)

    from pyrung.core.analysis.pdg import build_program_graph

    work = plc.fork()
    walk._install_walk_harness(work)
    pdg = build_program_graph(work._program)
    known = work._known_tags_by_name
    ext_inputs = walk._external_bool_inputs(pdg, known)
    edge_ext = walk._edge_tags(pdg, work._program) & set(ext_inputs)

    governing, gov_value = walk._governing(Target.name, True, pdg, work._program, plc=work)

    store = walk.NoGoodStore()
    with caplog.at_level(logging.INFO, logger="pyrung.core.analysis.walk"):
        steps = walk._walk_to_goal(
            work,
            governing,
            gov_value,
            pdg,
            work._program,
            known,
            ext_inputs,
            edge_ext,
            64,
            nogoods=store,
        )

    assert steps is not None
    assert store.recovery_iters <= 2
    # Cross-check via the per-iteration INFO line.
    recovery_lines = [r for r in caplog.records if "recovery iter" in r.getMessage()]
    assert len(recovery_lines) <= 2
