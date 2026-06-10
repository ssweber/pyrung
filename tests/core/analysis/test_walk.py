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

from pyrung import (
    Bool,
    Counter,
    Int,
    Or,
    Program,
    Rung,
    calc,
    copy,
    count_down,
    count_up,
    fall,
    latch,
    out,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.prove.classify import _compute_stepping_tags
from pyrung.core.analysis.walk import engine as walk
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

        with caplog.at_level(logging.INFO, logger="pyrung.core.analysis.walk"):
            path = plc.how(Stage == 1)

        assert path.reachable
        # The corridor walker — not the BFS fallback — produced this path.
        assert "corridor on" in caplog.text
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

        with caplog.at_level(logging.INFO, logger="pyrung.core.analysis.walk"):
            path = plc.how(Stage == 1)

        assert path.reachable
        assert "corridor on" in caplog.text
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
        iteration guard.  This is the safety the dynamic cap buys.

        Spin is read by another rung, so it is *visible* churn — the
        fold-kind plateau exclusions (unread churn etc.) must not apply and
        the reaction budget stays the relevant backstop."""
        Press = Bool("Press", external=True)
        Spin = Int("Spin")  # self-incrementing calc: visible churn, not an accumulator
        Indicator = Bool("Indicator")
        with Program() as prog:
            with Rung(Press):
                calc(Spin + 1, Spin)
            with Rung(Spin > 100):
                out(Indicator)
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


class TestDriveLowSteer:
    """Transitions gated by NOT-input or fall(input) need a drive-LOW steer."""

    def test_not_input_gate(self, caplog):
        """A copy gated by ~Input (XIO) should be reachable via a LOW steer."""
        Gate = Bool("Gate", external=True)
        Stage = Int("Stage")
        with Program() as prog:
            with Rung(~Gate):
                copy(1, Stage)
        plc = PLC(prog, dt=0.010)
        plc.patch({"Gate": True})
        plc.step()
        assert plc.state.tags["Stage"] == 0

        with caplog.at_level(logging.INFO, logger="pyrung.core.analysis.walk"):
            path = plc.how(Stage == 1)

        assert path.reachable
        assert "corridor on" in caplog.text
        # The walker drives Gate LOW.
        assert any(s.action.get("Gate") is False for s in path.steps)

        replay = PLC(prog, dt=0.010)
        replay.patch({"Gate": True})
        replay.step()
        for step in path.steps:
            replay.patch(step.action)
            for _ in range(step.scans):
                replay.step()
        assert replay.state.tags["Stage"] == 1

    def test_fall_input_gate(self, caplog):
        """A copy gated by fall(Input) needs a falling edge (high→low)."""
        Trigger = Bool("Trigger", external=True)
        Stage = Int("Stage")
        with Program() as prog:
            with Rung(fall(Trigger)):
                copy(1, Stage)
        plc = PLC(prog, dt=0.010)
        plc.patch({"Trigger": True})
        plc.step()
        assert plc.state.tags["Stage"] == 0

        with caplog.at_level(logging.INFO, logger="pyrung.core.analysis.walk"):
            path = plc.how(Stage == 1)

        assert path.reachable
        assert "corridor on" in caplog.text

        replay = PLC(prog, dt=0.010)
        replay.patch({"Trigger": True})
        replay.step()
        for step in path.steps:
            replay.patch(step.action)
            for _ in range(step.scans):
                replay.step()
        assert replay.state.tags["Stage"] == 1

    def test_fall_from_low_needs_low_steer(self, caplog):
        """fall(Input) where Input starts FALSE: pulse can't produce the edge.

        Pulse drives Input high and holds — fall() never fires. The LOW
        steer explicitly sequences high→low, creating the falling edge.
        Without drive-LOW this corridor is unsolvable by the walker.
        """
        Trigger = Bool("Trigger", external=True)
        Stage = Int("Stage")
        with Program() as prog:
            with Rung(fall(Trigger)):
                copy(1, Stage)
        plc = PLC(prog, dt=0.010)
        # Trigger starts FALSE (default).  fall() needs prev=True, cur=False.
        assert plc.state.tags.get("Trigger", False) is False

        with caplog.at_level(logging.INFO, logger="pyrung.core.analysis.walk"):
            path = plc.how(Stage == 1)

        assert path.reachable
        assert "corridor on" in caplog.text

        replay = PLC(prog, dt=0.010)
        for step in path.steps:
            replay.patch(step.action)
            for _ in range(step.scans):
                replay.step()
        assert replay.state.tags["Stage"] == 1


# ---------------------------------------------------------------------------
# Tripwire tests — programs the walker CANNOT solve today.
#
# Each test documents a concrete limitation.  When the limitation is lifted
# the test will start passing; flip the xfail to an assertion.
# ---------------------------------------------------------------------------


class TestWalkerTripwires:
    """Programs that exercise known walker gaps."""

    def test_multi_input_steer_two_key_interlock(self):
        """Transition gated by two external inputs simultaneously.

        Real pattern: two-hand safety interlock — both buttons must be held
        within the same scan window to start the press.  The walker tries
        each input individually but never combines them.
        """
        A = Bool("A", external=True)
        B = Bool("B", external=True)
        Running = Bool("Running")

        with Program() as prog:
            with Rung(A, B):
                latch(Running)

        plc = PLC(prog, dt=0.010)
        path = plc.how(Running)
        assert path.reachable

        replay = PLC(prog, dt=0.010)
        for step in path.steps:
            replay.patch(step.action)
            for _ in range(step.scans):
                replay.step()
        assert replay.state.tags["Running"] is True

    def test_multi_input_steer_mixed_polarity(self):
        """Transition needing one input high and another low simultaneously.

        Real pattern: selector switch — "manual AND NOT auto" to enter manual
        mode.  The walker must generate a multi-input steer with mixed polarity.
        """
        Manual = Bool("Manual", external=True)
        Auto = Bool("Auto", external=True)
        Mode = Int("Mode")

        with Program() as prog:
            with Rung(Manual, ~Auto):
                copy(1, Mode)

        plc = PLC(prog, dt=0.010)
        path = plc.how(Mode == 1)
        assert path.reachable

        replay = PLC(prog, dt=0.010)
        for step in path.steps:
            replay.patch(step.action)
            for _ in range(step.scans):
                replay.step()
        assert replay.state.tags["Mode"] == 1

    def test_latch_reset_path(self):
        """Reaching a state that requires breaking a seal-in first.

        Real pattern: alarm acknowledgment — the alarm seal-in holds via
        out(), and reaching the clear state requires pulsing Reset to
        break the seal-in condition so the out() writes False.
        """
        Fault = Bool("Fault", external=True)
        Reset = Bool("Reset", external=True)
        Alarm = Bool("Alarm")
        Clear = Bool("Clear")

        with Program() as prog:
            with Rung(Or(Fault, Alarm), ~Reset):
                out(Alarm)
            with Rung(~Alarm):
                out(Clear)

        plc = PLC(prog, dt=0.010)
        plc.patch({"Fault": True})
        plc.step()
        assert plc.state.tags["Alarm"] is True
        plc.patch({"Fault": False})
        plc.step()
        assert plc.state.tags["Alarm"] is True  # sealed in

        path = plc.how(Clear)
        assert path.reachable

        replay = plc.fork()
        for step in path.steps:
            replay.patch(step.action)
            for _ in range(step.scans):
                replay.step()
        assert replay.state.tags["Clear"] is True


class TestSteppingTags:
    """Pipeline stepping-tag classification for governing-tag selection."""

    def test_latched_bool_not_stepping(self):
        """A seal-in Bool (one literal write + latch hold) is NOT stepping."""
        Fault = Bool("Fault", external=True)
        Alarm = Bool("Alarm")

        with Program() as prog:
            with Rung(Or(Fault, Alarm)):
                latch(Alarm)

        pdg = build_program_graph(prog)
        stepping = _compute_stepping_tags(prog, pdg)
        assert "Alarm" not in stepping

    def test_intflag_two_literal_writes_is_stepping(self):
        """An Int written with copy(0,...) and copy(1,...) IS stepping."""
        Enable = Bool("Enable", external=True)
        Flag = Int("Flag")

        with Program() as prog:
            with Rung(Enable):
                copy(1, Flag)
            with Rung(~Enable):
                copy(0, Flag)

        pdg = build_program_graph(prog)
        stepping = _compute_stepping_tags(prog, pdg)
        assert "Flag" in stepping

    def test_arithmetic_self_ref_is_stepping(self):
        """calc(Counter + 1, Counter) is stepping."""
        Enable = Bool("Enable", external=True)
        Counter_ = Int("Counter_", min=0, max=10)

        with Program() as prog:
            with Rung(Enable):
                calc(Counter_ + 1, Counter_)

        pdg = build_program_graph(prog)
        stepping = _compute_stepping_tags(prog, pdg)
        assert "Counter_" in stepping

    def test_modulo_self_ref_is_stepping(self):
        """calc((Step + 1) % 6, Step) — self-referential with wrapper op."""
        Enable = Bool("Enable", external=True)
        Step = Int("Step", min=0, max=5)

        with Program() as prog:
            with Rung(Enable):
                calc((Step + 1) % 6, Step)

        pdg = build_program_graph(prog)
        stepping = _compute_stepping_tags(prog, pdg)
        assert "Step" in stepping

    def test_copy_coupled_inherits_stepping(self):
        """A tag copy-coupled to a stepping source inherits stepping."""
        Enable = Bool("Enable", external=True)
        Mode = Int("Mode")
        ModeView = Int("ModeView")

        with Program() as prog:
            with Rung(Enable):
                copy(1, Mode)
            with Rung(~Enable):
                copy(0, Mode)
            with Rung():
                copy(Mode, ModeView)

        pdg = build_program_graph(prog)
        stepping = _compute_stepping_tags(prog, pdg)
        assert "Mode" in stepping
        assert "ModeView" in stepping


class TestGoverningWithSteppingTags:
    """_governing uses pipeline stepping_tags when available."""

    def test_latched_bool_delegates_with_pipeline(self):
        """Latched Bool delegates to richer upstream when pipeline is available."""
        Trigger = Bool("Trigger", external=True)
        Stage = Int("Stage", choices={0: "Idle", 1: "Run", 2: "Done"})
        Active = Bool("Active")

        with Program() as prog:
            with Rung(Trigger, Stage == 0):
                copy(1, Stage)
            with Rung(Stage == 1):
                latch(Active)
            with Rung(Stage == 2):
                copy(0, Stage)

        pdg = build_program_graph(prog)
        stepping = _compute_stepping_tags(prog, pdg)
        assert "Active" not in stepping
        assert "Stage" in stepping

        class _FakeContext:
            stepping_tags = stepping
            stateful_dims = {"Stage": (0, 1, 2), "Active": (False, True)}
            nondeterministic_dims = {"Trigger": (False, True)}
            combinational_tags: frozenset[str] = frozenset()
            elided_tags: dict[str, str] = {}
            init_constant_projections: dict[str, tuple[str, object]] = {}

        gov, _ = walk._governing("Active", True, pdg, prog, explore_context=_FakeContext())
        assert gov != "Active"

    def test_governing_fallback_without_context(self):
        """Without explore_context, _governing falls back to _value_richness."""
        Trigger = Bool("Trigger", external=True)
        Stage = Int("Stage", choices={0: "Idle", 1: "Run", 2: "Done"})
        Active = Bool("Active")

        with Program() as prog:
            with Rung(Trigger, Stage == 0):
                copy(1, Stage)
            with Rung(Stage == 1):
                latch(Active)
            with Rung(Stage == 2):
                copy(0, Stage)

        pdg = build_program_graph(prog)
        gov, _ = walk._governing("Active", True, pdg, prog)
        assert gov != "Active"

    def test_governing_probe_finds_calc_driven_stepping(self):
        """Simulation probe discovers a tag steps when the write is a
        non-self-referential calc expression — opaque to static literal
        counting but observable by fork-steer-observe."""
        Cmd = Bool("Cmd", external=True)
        Selector = Int("Selector")
        Enable = Bool("Enable")
        StateCur = Int("StateCur", choices={0: "A", 1: "B", 2: "C"})

        with Program() as prog:
            with Rung(Cmd):
                latch(Enable)
            with Rung(Enable, StateCur == 0):
                calc(Selector + 1, StateCur)
            with Rung(Enable, StateCur == 1):
                calc(Selector + 2, StateCur)

        pdg = build_program_graph(prog)
        stepping = _compute_stepping_tags(prog, pdg)
        assert "StateCur" not in stepping, "test premise: StateCur not in stepping_tags"

        class _FakeContext:
            stepping_tags = stepping
            stateful_dims = {"Enable": (False, True)}
            nondeterministic_dims = {"Cmd": (False, True)}
            combinational_tags: frozenset[str] = frozenset()
            elided_tags: dict[str, str] = {}
            init_constant_projections: dict[str, tuple[str, object]] = {}

        plc = PLC(prog, dt=0.010)
        plc.step()

        # With probe: fork-steer-observe finds StateCur steps.
        gov_probed, _ = walk._governing(
            "StateCur", 2, pdg, prog, explore_context=_FakeContext(), plc=plc
        )
        assert gov_probed == "StateCur"


class TestBidirectionalCounterFold:
    """Fold for a bidirectional counter counting backward past a crossing.

    CountUpInstruction with a down_condition can have negative delta when
    up is disabled and down is active.  The fold must track the backward
    crossing (done_bit going False) and patch the accumulator backward.
    """

    @staticmethod
    def _bidir_program(preset: int = 50):
        """Bidir counter whose done_bit is read by downstream rungs.

        Stage 0 → 1 when Done goes True (counted up past preset).
        Stage 1 → 2 when Done goes False again (counted back down).
        """
        UpBtn = Bool("UpBtn", external=True)
        DownBtn = Bool("DownBtn", external=True)
        Rst = Bool("Rst", external=True)
        Ctr = Counter.clone("BiDir")
        Stage = Int("Stage")
        with Program() as prog:
            with Rung(UpBtn):
                count_up(Ctr, preset=preset).down(DownBtn).reset(Rst)
            with Rung(Ctr.Done, Stage == 0):
                copy(1, Stage)
            with Rung(~Ctr.Done, Stage == 1):
                copy(2, Stage)
        return prog, Stage

    def test_advance_time_folds_backward_dwell(self, monkeypatch):
        """_advance_time folds a backward-counting bidir counter to the
        done_bit un-crossing in far fewer real steps than the dwell length."""
        preset = 50
        prog, _ = self._bidir_program(preset)
        plc = PLC(prog, dt=0.010)
        # Wind the counter up past preset → Stage goes to 1.
        plc.patch({"UpBtn": True})
        for _ in range(preset + 1):
            plc.step()
        assert plc.state.tags["BiDir_Acc"] == preset + 1
        assert plc.state.tags["BiDir_Done"] is True
        assert plc.state.tags["Stage"] == 1

        # Now hold DownBtn, release UpBtn — counter decrements.
        plc.patch({"UpBtn": False, "DownBtn": True})
        plc.step()
        assert plc.state.tags["BiDir_Acc"] == preset  # still at preset, Done still True

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
        # Watch BiDir_Done: it leaves True when acc drops below preset.
        auto = walk._advance_time(work, "BiDir_Done", True, ctx, walk._MAX_ADVANCE_ITERS)

        assert auto is not None
        assert work.state.tags["BiDir_Done"] is False
        assert work.state.tags["BiDir_Acc"] < preset
        assert calls < 10  # folded, not stepped one-by-one

    def test_path_replays_at_normal_dt(self):
        """Fold actions replayed at normal dt reproduce the crossing."""
        preset = 50
        prog, _ = self._bidir_program(preset)
        plc = PLC(prog, dt=0.010)
        plc.patch({"UpBtn": True})
        for _ in range(preset + 1):
            plc.step()

        plc.patch({"UpBtn": False, "DownBtn": True})
        plc.step()

        pdg = build_program_graph(prog)
        ctx = walk._build_jump_context(plc, pdg, prog)
        work = plc.fork()
        scans = walk._advance_time(work, "BiDir_Done", True, ctx, walk._MAX_ADVANCE_ITERS)
        assert scans is not None

        # Replay scan-by-scan on a separate fork.
        replay = plc.fork()
        for _ in range(scans):
            replay.step()
        assert replay.state.tags["BiDir_Done"] is False
        assert replay.state.tags["BiDir_Acc"] == work.state.tags["BiDir_Acc"]


class TestScansToCross:
    """Unit tests for the crossing arithmetic helpers."""

    @pytest.mark.parametrize(
        "pa, delta, target, strict, expected",
        [
            # non-strict (ge boundary): first scan where progress >= target
            (0, 1, 5, False, 5),
            (4, 1, 5, False, 1),
            (5, 1, 5, False, None),  # already at target
            (6, 1, 5, False, None),  # already past
            (0, 2, 5, False, 3),  # ceil(5/2) = 3
            (0, 3, 10, False, 4),  # ceil(10/3) = 4
            # strict (gt boundary): first scan where progress > target
            (0, 1, 5, True, 6),  # floor(5/1) + 1
            (4, 1, 5, True, 2),  # floor(1/1) + 1
            (5, 1, 5, True, 1),  # at target, need to exceed
            (6, 1, 5, True, None),  # already past
        ],
    )
    def test_scans_to_cross(self, pa, delta, target, strict, expected):
        assert walk._scans_to_cross(pa, delta, target, strict) == expected

    @pytest.mark.parametrize(
        "pa, delta, target, strict, expected",
        [
            # non-strict (ge boundary): first scan where progress < target
            (10, -1, 5, False, 6),  # 10 - 6 = 4 < 5
            (6, -1, 5, False, 2),  # 6 - 2 = 4 < 5
            (5, -1, 5, False, 1),  # at target, one scan drops below
            (4, -1, 5, False, None),  # already below
            (10, -2, 5, False, 3),  # floor(5/2) + 1 = 3; 10 - 6 = 4 < 5
            # strict (gt boundary): first scan where progress <= target
            (10, -1, 5, True, 5),  # 10 - 5 = 5 = target
            (6, -1, 5, True, 1),  # 6 - 1 = 5 = target
            (5, -1, 5, True, None),  # already at target (not above)
            (4, -1, 5, True, None),  # already below
            (11, -2, 5, True, 3),  # ceil((-6)/(-2)) = 3; 11 - 6 = 5
        ],
    )
    def test_scans_to_uncross(self, pa, delta, target, strict, expected):
        assert walk._scans_to_uncross(pa, delta, target, strict) == expected
