"""Crossings Phase 0 — the empirical counterfactual hold sweep.

The sweep is the universal floor under a cause-chain dead-end: when a goal
depends on an external input through a writer that recorded reverse cannot
cross (here a ``calc`` writing an Int — opaque to recorded cause), perturb each
external input in the goal's upstream cone away from its anchor value and keep
the ones whose change breaks the goal.  Those load-bearing inputs become
protective holds the agenda installs and the replay validates.

Acceptance (plan §"Phase 0"): (a) a steady anchor with no break proposes
nothing / a non-steady anchor falls through; (b) the sweep finds the
load-bearing input only; (c) the proposed hold keeps the goal when applied;
(d) with the ``counterfactual_fallback`` pass ablated, the opaque-writer
regression yields no hold.
"""

from __future__ import annotations

from pyrung import Bool, Int, Program, Rung, calc, copy
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.walk.base import (
    HoldStore,
    NoGoodStore,
    _DebugSink,
    _WalkBudget,
    _WalkContext,
)
from pyrung.core.analysis.walk.explore import _counterfactual_hold_sweep
from pyrung.core.analysis.walk.fold import _build_jump_context
from pyrung.core.analysis.walk.passes import run_walk_passes
from pyrung.core.analysis.walk.rules import _last_committed_scan
from pyrung.core.analysis.walk.scheduler import (
    _check_progress_regression,
    _PlanNode,
)
from pyrung.core.runner import PLC


def _opaque_door_state() -> Program:
    """``State`` mirrors ``DoorClosed`` through an opaque ``calc`` writer.

    ``Gate`` is an Int written by ``calc(DoorClosed, Gate)`` — a non-Boolean
    writer, so recorded cause goes opaque at it and cannot name ``DoorClosed``.
    ``Spare`` gates the *set* of ``State`` but ``State`` is retentive, so once
    ``State`` holds 1 only ``DoorClosed`` is load-bearing for keeping it.
    """
    door = Bool("DoorClosed", external=True)
    spare = Bool("Spare", external=True)
    gate = Int("Gate")
    state = Int("State")
    with Program() as prog:
        with Rung():
            calc(door, gate)
        with Rung(spare, gate >= 1):
            copy(1, state)
        with Rung(gate < 1):
            copy(9, state)
    return prog


def _ctx(prog: Program, plc: PLC, work: PLC, *, disabled: frozenset[str] = frozenset()):
    pdg = build_program_graph(prog)
    advice, journal = run_walk_passes(prog, pdg, disabled=disabled)
    return _WalkContext(
        pdg=pdg,
        program=prog,
        known=plc._known_tags_by_name,
        ext_inputs=["DoorClosed", "Spare"],
        edge_ext=set(),
        jump_ctx=_build_jump_context(
            work,
            pdg,
            prog,
            target_names=frozenset({"State"}),
            advice=advice,
            journal=journal,
        ),
        nogoods=NoGoodStore(),
        holds=HoldStore(),
        budget=_WalkBudget(),
        advice=advice,
        journal=journal,
        debug_sink=_DebugSink(),
    )


def _hold_state(prog: Program) -> tuple[PLC, PLC]:
    """Return ``(plc, anchor)`` with ``State == 1`` held via ``DoorClosed``."""
    plc = PLC(prog, dt=0.010)
    anchor = plc.fork()
    anchor.patch({"DoorClosed": True, "Spare": True})
    anchor.step()
    assert anchor.state.tags["State"] == 1
    return plc, anchor


# --------------------------------------------------------------------------
# (b) the sweep finds the load-bearing input only
# --------------------------------------------------------------------------


def test_sweep_finds_only_load_bearing_input() -> None:
    prog = _opaque_door_state()
    plc, anchor = _hold_state(prog)
    ctx = _ctx(prog, plc, anchor)

    holds = _counterfactual_hold_sweep(ctx, anchor, "State", ("State", 1))

    # DoorClosed breaks State when perturbed; Spare only gated the set, so the
    # retentive State survives its release — it is not load-bearing.
    assert holds == [("DoorClosed", True)]
    assert ctx.budget.forks > 0  # it actually forked to probe


# --------------------------------------------------------------------------
# (a) non-steady anchor: stabilisation sweep finds the sustaining input
# --------------------------------------------------------------------------


def test_stabilisation_sweep_finds_sustaining_input() -> None:
    """When the goal departs on its own, the stabilisation sweep tries each
    candidate at its alternative value.  DoorClosed=True stabilises the goal
    (State==1) even though the anchor starts at State==9."""
    prog = _opaque_door_state()
    plc = PLC(prog, dt=0.010)
    anchor = plc.fork()
    anchor.patch({"DoorClosed": False, "Spare": True})
    anchor.step()
    assert anchor.state.tags["State"] == 9  # goal does not hold yet
    ctx = _ctx(prog, plc, anchor)

    holds = _counterfactual_hold_sweep(ctx, anchor, "State", ("State", 1))

    assert holds == [("DoorClosed", True)]


# --------------------------------------------------------------------------
# wired regression seam + (d) ablation
# --------------------------------------------------------------------------


def _regressed_work(prog: Program) -> PLC:
    """A work fork whose ``State`` held 1 then departed to 9 (door released)."""
    plc = PLC(prog, dt=0.010)
    work = plc.fork()
    work.patch({"DoorClosed": True, "Spare": True})
    work.step()
    assert work.state.tags["State"] == 1
    work.patch({"DoorClosed": False})
    work.step()
    assert work.state.tags["State"] == 9
    return work


def _force_mine_empty(monkeypatch) -> None:
    """Simulate a Tier-3 opaque writer: the bespoke miner names nothing.

    Phase 1's recorded read-diff now crosses calc/copy writers, so a small
    fixture won't leave ``mine_regression_holds`` empty.  The realistic trigger
    for the empirical fallback is an un-enumerable (unbounded-indirect) writer
    whose footprint Tier 1 can't diff — modelled here by stubbing the miner so
    the agenda *gate* is what's under test, not a particular opaque program.
    """
    import pyrung.core.analysis.walk.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "mine_regression_holds", lambda *a, **k: [])


def test_regression_fallback_sweeps_when_mine_is_empty(monkeypatch) -> None:
    prog = _opaque_door_state()
    work = _regressed_work(prog)
    ctx = _ctx(prog, PLC(prog, dt=0.010), work)
    ctx.committed_values[("State", 1)] = 1
    _force_mine_empty(monkeypatch)

    # The pre-departure anchor the sweep forks from is recoverable.
    assert _last_committed_scan(work, "State", 1) is not None

    completed = _PlanNode(goal=("Other", True), provenance="test-child", depth=1)
    holds = _check_progress_regression(ctx, work, completed)

    assert holds == [("DoorClosed", True)]
    events = ctx.debug_sink.events
    assert any(e.kind == "progress-regression" and e.tag == "State" for e in events)
    assert any(e.kind == "counterfactual-fallback" and "DoorClosed" in e.detail for e in events)

    # (c) the proposed hold keeps the goal: replay the regressing release with
    # DoorClosed pinned and confirm State no longer departs.
    replay = PLC(prog, dt=0.010).fork()
    replay.patch({"DoorClosed": True, "Spare": True})
    replay.step()
    replay.patch(dict(holds))  # the protective hold
    replay.step()
    assert replay.state.tags["State"] == 1


def test_regression_fallback_disabled_yields_no_hold(monkeypatch) -> None:
    prog = _opaque_door_state()
    work = _regressed_work(prog)
    ctx = _ctx(prog, PLC(prog, dt=0.010), work, disabled=frozenset({"counterfactual_fallback"}))
    ctx.committed_values[("State", 1)] = 1
    _force_mine_empty(monkeypatch)

    completed = _PlanNode(goal=("Other", True), provenance="test-child", depth=1)
    holds = _check_progress_regression(ctx, work, completed)

    assert holds == []
    events = ctx.debug_sink.events
    assert any(e.kind == "progress-regression" and e.tag == "State" for e in events)
    assert not any(e.kind == "counterfactual-fallback" for e in events)


# --------------------------------------------------------------------------
# (e) unprotectable regression is skipped on subsequent frames
# --------------------------------------------------------------------------


def test_unprotectable_regression_skipped_on_second_check(monkeypatch) -> None:
    """When both miners return [] the regression is marked unprotectable.

    A second call to ``_check_progress_regression`` for the same committed
    goal must skip the regression entirely — no re-mining, no budget burn.
    """
    prog = _opaque_door_state()
    work = _regressed_work(prog)
    ctx = _ctx(prog, PLC(prog, dt=0.010), work, disabled=frozenset({"counterfactual_fallback"}))
    ctx.committed_values[("State", 1)] = 1
    _force_mine_empty(monkeypatch)

    completed = _PlanNode(goal=("Other", True), provenance="test-child", depth=1)

    # First check: both miners return [], regression marked unprotectable.
    holds_1 = _check_progress_regression(ctx, work, completed)
    assert holds_1 == []
    assert ("State", 1) in ctx.unprotectable_commits
    events_1 = list(ctx.debug_sink.events)
    assert any(e.kind == "unprotectable-regression" and e.tag == "State" for e in events_1)

    # Second check: skipped entirely — no new events emitted.
    event_count_before = len(ctx.debug_sink.events)
    holds_2 = _check_progress_regression(ctx, work, completed)
    assert holds_2 == []
    assert len(ctx.debug_sink.events) == event_count_before
