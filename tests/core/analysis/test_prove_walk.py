"""Tests for the corridor walker (``plan_walk``) — the sequential-simulation
planner that ``how()`` tries before the waypoint/BFS path.

These focus on the **counter** time-jump path: ``_do_jump``'s per-scan
acc-patch lever.  The timer (dt-knob) path is already exercised by the
packml_bench ``_CurStep`` corridor; the counter arithmetic — patching the
accumulator forward by ``(skip-1)*delta`` and letting the jump scan's own
``execute`` supply the final increment — is the genuinely-new code here.
"""

from __future__ import annotations

import logging

import pytest

from pyrung import Bool, Counter, Int, Program, Rung, calc, copy, count_down, count_up
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.prove import walk
from pyrung.core.runner import PLC


def _counter_dwell_program(preset: int, kind: str):
    """Run-gated dwell *counter* gating a 0 -> 1 stage.

    While ``Run`` is held and the stage is 0 the counter advances one per
    scan; on completion (``acc`` reaches the preset) the stage advances to 1.
    The accumulator is per-scan (``timed=False``), so a held-wait fold must
    ride ``_do_jump``'s acc-patch lever, not the time (dt) knob.
    """
    builder = count_up if kind == "up" else count_down
    Run = Bool("Run", external=True)
    Stage = Int("Stage")
    DwellCtr = Counter.clone("DwellCtr")
    with Program() as prog:
        with Rung(Run, Stage == 0):
            builder(DwellCtr, preset=preset).reset(Stage == 1)
        with Rung(DwellCtr.Done):
            copy(1, Stage)
    return prog, Stage


def _running_plc(prog) -> PLC:
    """PLC with ``Run`` latched high and one scan elapsed (counter live).

    Mirrors the packml corridor's entry: the dwell is already running, so the
    walker reaches the crossing by *holding* inputs (the empty steer, which is
    the only steer granted a large fold cap).
    """
    plc = PLC(prog, dt=0.010)
    plc.patch({"Run": True})
    plc.step()
    return plc


@pytest.mark.parametrize("kind", ["up", "down"])
class TestCounterCorridor:
    def test_reaches_goal_via_corridor_walk(self, kind, caplog):
        preset = 50
        prog, Stage = _counter_dwell_program(preset, kind)
        plc = _running_plc(prog)

        with caplog.at_level(logging.INFO, logger="pyrung.core.analysis.prove.walk"):
            path = plc.how(Stage == 1)

        assert path.reachable
        # The corridor walker — not the BFS fallback — produced this path.
        assert "corridor on Stage" in caplog.text
        # A held-wait fold: a single step, no input change, the dwell folded.
        assert len(path.steps) == 1
        assert path.steps[0].action == {}
        # acc was already 1 (one scan into the setup), so preset-1 remain.
        assert path.steps[0].scans == preset - 1

    def test_path_replays_at_normal_dt(self, kind):
        """plan_walk emits the fold as one ``({}, scans)`` step; a plain
        normal-dt replay must reproduce the crossing exactly."""
        preset = 50
        prog, Stage = _counter_dwell_program(preset, kind)
        plc = _running_plc(prog)
        path = plc.how(Stage == 1)
        assert path.reachable

        replay = _running_plc(prog)
        for step in path.steps:
            replay.patch(step.action)
            for _ in range(step.scans):
                replay.step()
        assert replay.state.tags["Stage"] == 1
        assert replay.state.tags["DwellCtr_Acc"] == (preset if kind == "up" else -preset)

    def test_advance_time_folds_dwell(self, kind, monkeypatch):
        """The fold lands the accumulator exactly on the crossing in far
        fewer real steps than the dwell length — the acc-patch phase juggling
        (patch ``skip-1``, let the jump scan supply the last increment)."""
        preset = 50
        prog, _ = _counter_dwell_program(preset, kind)
        plc = _running_plc(prog)
        pdg = build_program_graph(prog)
        ctx = walk._build_jump_context(plc, pdg, prog)
        work = plc.fork()

        orig_step = PLC.step
        calls = 0

        def counting_step(self, *a, **k):
            nonlocal calls
            calls += 1
            return orig_step(self, *a, **k)

        monkeypatch.setattr(PLC, "step", counting_step)
        auto = walk._advance_time(work, "Stage", 0, ctx, walk._MAX_ADVANCE_ITERS)

        assert auto == preset - 1  # equivalent normal-dt scans advanced
        assert calls < 10  # folded via one acc-patch jump, not preset single steps
        assert work.state.tags["Stage"] == 1
        assert work.state.tags["DwellCtr_Acc"] == (preset if kind == "up" else -preset)
        assert work.state.tags["DwellCtr_Done"] is True


class TestPulseTriggeredFold:
    """A pulse that *starts* a counter dwell must fold the dwell it triggered.

    The reaction budget bounds only pre-plateau churn; once the pulsed input
    establishes an accumulation plateau the fold runs to the crossing.  (Before
    the dynamic-cap lift, the pulse steer's small fixed cap stopped at 6 scans
    and how() fell back to BFS for this shape.)
    """

    def test_pulse_then_fold_reaches_goal(self, caplog):
        preset = 200
        prog, Stage = _counter_dwell_program(preset, "up")
        plc = PLC(prog, dt=0.010)  # Run starts low — the walker must pulse it.

        with caplog.at_level(logging.INFO, logger="pyrung.core.analysis.prove.walk"):
            path = plc.how(Stage == 1)

        assert path.reachable
        assert "corridor on Stage" in caplog.text
        # Pulse Run high (1 scan), then fold the dwell it started.
        assert len(path.steps) == 2
        assert path.steps[0].action == {"Run": True}
        assert path.steps[1].action == {}
        assert path.steps[1].scans == preset - 1  # acc reached 1 on the pulse scan

        replay = PLC(prog, dt=0.010)
        for step in path.steps:
            replay.patch(step.action)
            for _ in range(step.scans):
                replay.step()
        assert replay.state.tags["Stage"] == 1
        assert replay.state.tags["DwellCtr_Acc"] == preset

    def test_churning_pulse_bails_within_reaction_budget(self, monkeypatch):
        """A pulse that churns a non-accumulator every scan (a plateau never
        forms) must give up on the reaction budget — not run to the 4000-scan
        iteration guard.  This is the safety the dynamic cap buys."""
        Press = Bool("Press", external=True)
        Spin = Int("Spin")  # self-incrementing calc: visible churn, not an accumulator
        with Program() as prog:
            with Rung(Press):
                calc(Spin + 1, Spin)
        plc = PLC(prog, dt=0.010)
        pdg = build_program_graph(prog)
        ctx = walk._build_jump_context(plc, pdg, prog)
        plc.patch({"Press": True})
        plc.step()
        work = plc.fork()

        orig_step = PLC.step
        calls = 0

        def counting_step(self, *a, **k):
            nonlocal calls
            calls += 1
            return orig_step(self, *a, **k)

        monkeypatch.setattr(PLC, "step", counting_step)
        # Wait on a tag that is never written: it never leaves its (absent)
        # value, so the only state motion is Spin's churn — which must exhaust
        # the budget rather than fold.
        held = work.state.tags.get("Goal")  # None, and stays None
        auto = walk._advance_time(work, "Goal", held, ctx, walk._PULSE_REACT_CAP)

        assert auto is None
        # Bailed on the reaction budget, far short of _MAX_ADVANCE_ITERS.
        assert calls == walk._PULSE_REACT_CAP + 1
