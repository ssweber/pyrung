"""Holds (protection intervals): the walker's external-input commitments.

A hold = (external input, value, the committed goal that depends on it).
``_steer_prefix`` skips protected names in its implicit releases, so a later
sub-walk no longer clobbers what an earlier one established (prevention,
where the oracle recovery loop is repair).  A steer that *intends* to change
a protected input passes the divest probe first — allowed only when the
hold's goal survives the change (seal-in case).

Three behaviors pinned here:

1. **Prevention** — a shared-cone program whose serial walk used to clobber
   and recover now solves with zero recovery iterations, and ``Path.holds``
   names the held inputs.
2. **Divest** — a sealed latch makes its arming input releasable; the divest
   probe approves the release and the hold is reconciled away at commit.
3. **Conflict skip** — a goal that genuinely needs its input held rejects
   the conflicting steer; the unreachable answer stays honest.
"""

from __future__ import annotations

import logging

import pytest

from pyrung import (
    Bool,
    Or,
    Program,
    Rung,
    Timer,
    latch,
    on_delay,
    out,
    rise,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.walk import engine as walk
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Pattern 1: shared-cone prerequisites (prevention)
# ---------------------------------------------------------------------------


def _shared_gate_program() -> tuple[Program, Bool]:
    """Two stages sharing a ``Common`` gate; Target needs both.

    - ``StageA`` is level-held: it drops the scan either ``EnableA`` or
      ``Common`` is released.
    - ``StageB`` seals on a rising ``EnableB`` edge but its seal-in is gated
      by ``Common`` — its upstream cone shares ``Common`` with StageA, so the
      independent-fork walk's disjoint-cone gate refuses and the walker must
      go serial.
    - Pre-holds, the serial walk clobbered: pulsing ``EnableB`` released
      ``EnableA``/``Common`` (global release), dropping StageA, and only the
      oracle recovery loop could repair it.  With holds, the registered
      ``EnableA``/``Common`` commitments survive the pulse.
    """
    EnableA = Bool("EnableA", external=True)
    EnableB = Bool("EnableB", external=True)
    Common = Bool("Common", external=True)
    StageA = Bool("StageA")
    StageB = Bool("StageB")
    Target = Bool("Target")

    with Program() as prog:
        with Rung(EnableA, Common):
            out(StageA)
        with Rung(Or(rise(EnableB), StageB), Common):
            out(StageB)
        with Rung(StageA, StageB):
            out(Target)

    return prog, Target


def test_shared_gate_premise() -> None:
    """Premise: hold EnableA+Common, then pulse EnableB -> Target."""
    prog, _Target = _shared_gate_program()
    plc = PLC(prog, dt=0.010)

    plc.patch({"EnableA": True, "Common": True})
    plc.step()
    assert plc.state.tags["StageA"] is True

    plc.patch({"EnableB": True})
    plc.step()
    assert plc.state.tags["StageB"] is True
    assert plc.state.tags["Target"] is True


def test_prevention_zero_recovery_iters() -> None:
    """With holds, the serial walk never clobbers — recovery stays idle.

    A/B inside one test: the hold-aware walk solves with zero recovery
    iterations; the hold-blind walk (holds=None — the pre-holds code path)
    either needs the oracle recovery loop or fails outright.
    """
    prog, Target = _shared_gate_program()
    plc = PLC(prog, dt=0.010)
    pdg = build_program_graph(prog)
    known = plc._known_tags_by_name
    ext_inputs = walk._external_bool_inputs(pdg, known)
    edge_ext = walk._edge_tags(pdg, prog) & set(ext_inputs)

    # A: hold-aware.
    work = plc.fork()
    walk._install_walk_harness(work)
    nogoods = walk.NoGoodStore()
    holds = walk.HoldStore()
    steps = walk._walk_to_goal(
        work,
        Target.name,
        True,
        pdg,
        prog,
        known,
        ext_inputs,
        edge_ext,
        64,
        nogoods=nogoods,
        holds=holds,
    )
    assert steps is not None
    assert work.state.tags["Target"] is True
    assert nogoods.recovery_iters == 0
    assert {"EnableA", "Common"} <= holds.protected_names()

    # B: hold-blind (pre-holds behavior) — clobbers, so recovery must fire
    # (or the walk fails) on the very same program.
    blind = plc.fork()
    walk._install_walk_harness(blind)
    blind_nogoods = walk.NoGoodStore()
    blind_steps = walk._walk_to_goal(
        blind,
        Target.name,
        True,
        pdg,
        prog,
        known,
        ext_inputs,
        edge_ext,
        64,
        nogoods=blind_nogoods,
    )
    assert blind_steps is None or blind_nogoods.recovery_iters >= 1


def test_prevention_path_holds_surface() -> None:
    """how() reports the held inputs with the goals they protect.

    The exact goal attribution depends on the solve route (a divested and
    re-registered hold moves to the later goal), so pin only the invariants:
    all three inputs must stay held high, and every hold names a real
    program goal.
    """
    prog, Target = _shared_gate_program()
    plc = PLC(prog, dt=0.010)

    path = plc.how(Target)
    assert path.reachable
    assert path.holds is not None
    by_name = {name: (value, goal) for name, value, goal in path.holds}
    assert {"EnableA", "EnableB", "Common"} <= set(by_name)
    assert all(value is True for value, _goal in by_name.values())
    assert all(goal in {"StageA", "StageB", "Target"} for _value, goal in by_name.values())
    assert "Holds:" in str(path)


def test_prevention_replay() -> None:
    """The prevention plan replays to Target=True on a fresh PLC."""
    prog, Target = _shared_gate_program()
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
# Pattern 2: seal-then-release (divest point)
# ---------------------------------------------------------------------------


def _seal_release_program() -> tuple[Program, Bool, Bool]:
    """Arming seals a latch; firing requires releasing the arm input.

    ``Armed`` seals on a rising ``Arm`` edge and survives the release
    (seal-in).  ``Fired`` requires ``Armed AND ~Arm`` — the walker must
    release the very input it held to establish ``Armed``.  The divest probe
    verifies ``Armed`` survives the release and approves it.
    """
    Arm = Bool("Arm", external=True)
    Armed = Bool("Armed")
    Fired = Bool("Fired")

    with Program() as prog:
        with Rung(Or(rise(Arm), Armed)):
            out(Armed)
        with Rung(Armed, ~Arm):
            out(Fired)

    return prog, Armed, Fired


def test_divest_point_releases_hold(caplog: pytest.LogCaptureFixture) -> None:
    """The Arm hold (protecting Armed) is divested so Fired can be reached."""
    prog, Armed, Fired = _seal_release_program()
    plc = PLC(prog, dt=0.010)

    with caplog.at_level(logging.INFO, logger="pyrung.core.analysis.walk"):
        path = plc.how(Armed, Fired)

    assert path.reachable
    divest_lines = [r for r in caplog.records if "divest point" in r.getMessage()]
    assert divest_lines, "expected the Arm hold to be divested at commit"
    # The stale Arm=True hold is gone; whatever remains protects Fired's need.
    assert path.holds is None or all(
        not (name == "Arm" and value is True) for name, value, _goal in path.holds
    )

    replay = PLC(prog, dt=0.010)
    for step in path.steps:
        replay.patch(step.action)
        for _ in range(step.scans):
            replay.step()
    assert replay.state.tags["Armed"] is True
    assert replay.state.tags["Fired"] is True


# ---------------------------------------------------------------------------
# Pattern 3: hold genuinely load-bearing (conflict skip, honest answer)
# ---------------------------------------------------------------------------


def _hold_dependent_program() -> tuple[Program, Bool, Bool]:
    """StageB needs a fresh EnableA rise while StageA (timer-held) is up.

    ``StageA`` requires ``EnableA`` held through a 200 ms on-delay and drops
    the moment it is released.  ``StageB`` latches on
    ``rise(EnableA) AND StageA`` — but re-rising ``EnableA`` requires
    releasing it, which kills StageA before the edge lands.  Unreachable by
    construction; the divest probe must reject the release (StageA does not
    survive) and the walker must answer honestly.
    """
    EnableA = Bool("EnableA", external=True)
    StageA = Bool("StageA")
    StageB = Bool("StageB")
    HoldTmr = Timer.clone("HoldTmr")

    with Program() as prog:
        with Rung(EnableA):
            on_delay(HoldTmr, 200, "ms")
        with Rung(HoldTmr.Done):
            out(StageA)
        with Rung(rise(EnableA), StageA):
            latch(StageB)

    return prog, StageA, StageB


def test_conflict_skip_stays_honest(caplog: pytest.LogCaptureFixture) -> None:
    """The conflicting steer is probe-rejected; no false plan is returned."""
    prog, StageA, StageB = _hold_dependent_program()
    plc = PLC(prog, dt=0.010)

    with caplog.at_level(logging.INFO, logger="pyrung.core.analysis.walk"):
        path = plc.how(StageA, StageB)

    assert not path.reachable
    # The probe rejected the release — no divest was committed.
    assert not [r for r in caplog.records if "divest point" in r.getMessage()]

    # StageA alone remains solvable (the hold itself is not the problem).
    path_a = PLC(prog, dt=0.010).how(StageA)
    assert path_a.reachable


# ---------------------------------------------------------------------------
# Post-serial re-explore mode (Stage D4 decision)
# ---------------------------------------------------------------------------


def test_post_serial_reexplore_is_hold_aware(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every corridor explore in the establish flow receives the hold store.

    The post-serial-prereq re-explore ran hold-blind (``holds=None``) from
    before holds existed; Stage D4 switched it hold-aware after a suite-level
    A/B showed zero behavioral shift either way.  This pins the decision:
    a walk that reaches the post-serial site must pass the store, so a later
    edit can't silently regress the site to hold-blind.
    """
    from pyrung.core.analysis.walk import agenda

    seen_holds: list[object] = []
    real = agenda._explore_corridor

    def spy(ctx, work, governing, gov_value, alphabet, *, holds, must_stay=()):
        seen_holds.append(holds)
        return real(ctx, work, governing, gov_value, alphabet, holds=holds, must_stay=must_stay)

    monkeypatch.setattr(agenda, "_explore_corridor", spy)

    prog, Target = _shared_gate_program()
    plc = PLC(prog, dt=0.010)
    pdg = build_program_graph(prog)
    known = plc._known_tags_by_name
    ext_inputs = walk._external_bool_inputs(pdg, known)
    edge_ext = walk._edge_tags(pdg, prog) & set(ext_inputs)

    work = plc.fork()
    walk._install_walk_harness(work)
    holds = walk.HoldStore()
    steps = walk._walk_to_goal(
        work,
        Target.name,
        True,
        pdg,
        prog,
        known,
        ext_inputs,
        edge_ext,
        64,
        nogoods=walk.NoGoodStore(),
        holds=holds,
    )
    assert steps is not None
    # The shared-gate walk goes serial (shared Common cone), so the
    # post-serial re-explore site is exercised; no agenda explore may run
    # hold-blind when a store exists.
    assert len(seen_holds) >= 2
    assert all(h is holds for h in seen_holds)


# ---------------------------------------------------------------------------
# Path.holds rendering (pure graph.py unit test)
# ---------------------------------------------------------------------------


def test_path_holds_rendering() -> None:
    from pyrung.core.analysis.graph import Path, ReachabilityStep

    step = ReachabilityStep(action={"X": True}, source_key=(), dest_key=(), scans=1)
    p = Path(
        reachable=True,
        steps=(step,),
        total_changes=1,
        total_scans=1,
        holds=(("EnableA", True, "StageA"), ("EnableB", True, "StageB")),
    )
    assert "Holds: EnableA=true (for StageA), EnableB=true (for StageB)" in str(p)

    bare = Path(reachable=True, steps=(step,), total_changes=1, total_scans=1)
    assert "Holds" not in str(bare)


# ---------------------------------------------------------------------------
# HoldStore unit test
# ---------------------------------------------------------------------------


def test_hold_store_protect_release_snapshot() -> None:
    store = walk.HoldStore()
    assert len(store) == 0
    assert store.protected_names() == frozenset()
    assert store.protected() == {}

    store.protect("A", True, ("GoalA", True))
    store.protect("B", False, ("GoalB", True))
    assert store.protected() == {"A": True, "B": False}
    assert store.goal_of("A") == ("GoalA", True)

    # First registration wins — same name, conflicting value is kept out.
    store.protect("A", False, ("GoalC", True))
    assert store.protected()["A"] is True
    assert store.goal_of("A") == ("GoalA", True)

    # Snapshot/restore brackets speculative sections.
    snap = store.snapshot()
    store.protect("C", True, ("GoalC", True))
    store.release("B")
    assert store.protected_names() == frozenset({"A", "C"})
    store.restore(snap)
    assert store.protected_names() == frozenset({"A", "B"})

    store.release("A")
    assert store.goal_of("A") is None
    assert store.protected_names() == frozenset({"B"})
