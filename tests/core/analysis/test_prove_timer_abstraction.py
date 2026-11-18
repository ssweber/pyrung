"""Tests for the static facts that how() steers with."""

from __future__ import annotations

from typing import Any

from pyrung.core import (
    PLC,
    Bool,
    Counter,
    Dint,
    Int,
    Program,
    Rung,
    Timer,
    copy,
    count_down,
    count_up,
    math,
    on_delay,
    out,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.prove import Intractable
from pyrung.core.analysis.prove.classify import (
    _classify_dimensions,
    _classify_dimensions_from_graph,
    _collect_all_exprs,
)


class TestTimerFastForward:
    """Item 18: timer with large preset exercises fast-forward budget."""

    def test_done_preset_extracted(self):
        """_classify_dimensions extracts constant done presets by tag name."""
        enable = Bool("Enable", external=True)
        t = Timer.clone("T1")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=5000)
            with Rung(t.Done):
                out(Bool("Output"))

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _s, _n, _c, _d, done_presets, _done_kinds = result
        assert done_presets["T1_Done"] == 5000


class TestTimerAccumulatorAgainstItsPreset:
    """A timer whose Acc is compared with its own dynamic preset."""

    def test_preset_data_use_elsewhere_is_bounded_but_not_absorbed(self):
        """A preset copied elsewhere stays explicit instead of being absorbed."""
        enable = Bool("Enable", external=True)
        active_preset = Int("ActivePreset", final=True)
        copied_preset = Int("CopiedPreset")
        t = Timer.clone("DynT")
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung():
                copy(1, active_preset)
                copy(active_preset, copied_preset)
            with Rung(enable):
                on_delay(t, preset=active_preset)
            with Rung(t.Acc >= active_preset):
                out(output)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, done_acc, done_presets, _done_kinds = result
        assert "ActivePreset" in stateful
        assert "DynT_Acc" in stateful
        assert done_acc == {}
        assert done_presets == {}


class TestAccumulatorThresholds:
    """An accumulator compared against thresholds.  A timer's tick may land on
    any value that a comparison splits at, so a threshold far past the budget's
    scans is still crossed.  A counter or a Math accumulator steps concretely,
    so its thresholds must lie within depth_budget."""

    def test_timer_threshold_event_nonzero_acc_assignment_stays_explicit(self):
        enable = Bool("AssignedTimerEnable", external=True)
        force = Bool("AssignedTimerForce", external=True)
        active_threshold = Int("AssignedTimerThreshold", final=True)
        t = Timer.clone("AssignedThresholdTmr")
        alarm = Bool("AssignedTimerAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, active_threshold)
            with Rung(force):
                copy(7, t.Acc)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > active_threshold):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, done_acc, _done_presets, _done_kinds = result
        assert "AssignedThresholdTmr_Acc" in stateful
        assert "AssignedTimerThreshold" in stateful
        assert done_acc == {}

    def test_nonconstant_stride_tag_int_progress_stays_explicit(self):
        enable = Bool("Enable", external=True)
        sel = Bool("StrideSelect", external=True)
        ticks = Int("VariableStepTicks")
        stride = Int("Stride")
        threshold = Int("VariableTickThreshold", final=True)
        alarm = Bool("VariableTickAlarm")

        with Program(strict=False) as logic:
            with Rung(sel):
                copy(1, stride)
            with Rung(~sel):
                copy(2, stride)
            with Rung():
                copy(500, threshold)
            with Rung(enable):
                math(ticks + stride, ticks)
            with Rung(ticks > threshold):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert "VariableStepTicks" in stateful
        assert "Stride" in stateful
        assert "VariableTickThreshold" in stateful

    def test_non_threshold_accumulator_read_stays_explicit(self):
        enable = Bool("Enable", external=True)
        threshold = Int("SavedAccThreshold", final=True)
        t = Timer.clone("SavedAccTmr")
        saved = Int("SavedAccValue")
        alarm = Bool("SavedAccAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, threshold)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung():
                copy(t.Acc, saved)
            with Rung(t.Acc > threshold):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, done_acc, _done_presets, _done_kinds = result
        assert "SavedAccTmr_Acc" in stateful
        assert "SavedAccThreshold" in stateful
        assert done_acc == {}


def _stateful(logic: Program, unclassified: set[str] | None = None) -> dict[str, Any]:
    graph = build_program_graph(logic)
    result = _classify_dimensions_from_graph(
        logic, graph, _collect_all_exprs(logic, graph), unclassified=unclassified
    )
    assert not isinstance(result, Intractable)
    return result[0]


class TestAccumulatorStartValues:
    """An Acc's domain has every value it starts from and every value it counts or times on to."""

    def test_down_counter_staged_by_a_copy(self):
        tray = Counter.clone("StagedTray")
        with Program(strict=False) as logic:
            with Rung(Bool("Load", external=True)):
                copy(5, tray.Acc)
            with Rung(Bool("Pick", external=True)):
                count_down(tray, 3).reset(Bool("Clear", external=True))
            with Rung():
                copy(tray.Acc, Dint("TrayLeft"))
        assert _stateful(logic)["StagedTray_Acc"] == (0, 1, 2, 3, 4, 5)

    def test_timer_staged_by_a_math(self):
        recipe = Int("Recipe", external=True, min=1, max=2)
        oven = Timer.clone("StagedOven")
        with Program(strict=False) as logic:
            with Rung(Bool("Resume", external=True)):
                math(recipe * 30, oven.Acc)
            with Rung(Bool("Heat", external=True)):
                on_delay(oven, 50)
            with Rung():
                copy(oven.Acc, Int("OvenShown"))
        assert _stateful(logic)["StagedOven_Acc"] == tuple(range(61))

    def test_up_counter_with_a_down_input_crosses_its_preset_both_ways(self):
        bins = Counter.clone("BothWays")
        with Program(strict=False) as logic:
            with Rung(Bool("BinIn", external=True)):
                count_up(bins, 4).down(Bool("BinOut", external=True)).reset(
                    Bool("BinClear", external=True)
                )
            with Rung():
                copy(bins.Acc, Dint("BinsShown"))
        assert _stateful(logic)["BothWays_Acc"] == (0, 1, 2, 3, 4, 5)

    def test_acc_staged_with_no_list_of_values_has_no_domain(self):
        first, second = Counter.clone("FirstCount"), Counter.clone("SecondCount")
        clear = Bool("CountClear", external=True)
        with Program(strict=False) as logic:
            with Rung(Bool("Pulse", external=True)):
                count_up(first, 3).reset(clear)
            with Rung(Bool("Handover", external=True)):
                copy(first.Acc, second.Acc)
            with Rung(Bool("Take", external=True)):
                count_down(second, -2).reset(clear)
            with Rung():
                copy(second.Acc, Dint("SecondShown"))
        unclassified: set[str] = set()
        stateful = _stateful(logic, unclassified)
        assert "SecondCount_Acc" not in stateful
        assert "SecondCount_Acc" in unclassified


class TestComparedAccumulators:
    """Two Accs the rungs compare with each other share the values of their domains."""

    def test_both_domains_hold_a_common_value_between_split_points(self):
        lead, trail = Timer.clone("PairLead"), Timer.clone("PairTrail")
        run = Bool("PairRun", external=True)
        with Program(strict=False) as logic:
            with Rung(run):
                on_delay(lead, 300)
            with Rung(run, ~Bool("PairHold", external=True)):
                on_delay(trail, 300)
            with Rung(lead.Acc == trail.Acc, trail.Acc != 0):
                out(Bool("PairInStep"))
            with Rung(lead.Acc > trail.Acc, lead.Acc >= 100):
                out(Bool("PairBehind"))
            with Rung():
                copy(lead.Acc, Int("PairShown"))
                copy(trail.Acc, Int("PairTrailShown"))
        stateful = _stateful(logic)
        lead_values, trail_values = set(stateful["PairLead_Acc"]), set(stateful["PairTrail_Acc"])
        assert lead_values == trail_values
        # One value between 0 and 100 stands for both Accs equal there.
        assert any(0 < v < 100 for v in lead_values)


class TestTimeEventDrum:
    """A time & event drum's timed steps, in the facts and in how()."""

    @staticmethod
    def _program(hit_reads_y3: bool) -> Program:
        from pyrung.core import time_event_drum

        with Program(strict=False) as logic:
            with Rung():
                time_event_drum(
                    outputs=[Bool("Y1"), Bool("Y2"), Bool("Y3")],
                    presets=[30, None, 20],
                    events=[None, Bool("A"), Bool("B")],
                    unit="ms",
                    pattern=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                    current_step=Int("Step"),
                    accumulator=Int("Acc"),
                    completion_flag=Bool("Done"),
                ).reset(Bool("R"))
            with Rung(Bool("Done"), ~Bool("Y3")) if hit_reads_y3 else Rung(Bool("Done")):
                out(Bool("H"))
        return logic

    def test_how_keeps_its_prover_context(self) -> None:
        from pyrung.core.analysis.pilot.drive_setup import build_prover_context

        logic = self._program(False)
        plc = PLC(logic, dt=0.01)
        prover = build_prover_context(logic)
        assert prover.nd_domains
        assert prover.key_config is not None
        assert "Step" in prover.key_config.stateful_names
        assert plc.how(Bool("Done"), max_scans=200).reachable
