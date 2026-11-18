"""Tests for the static facts that how() steers with."""

from __future__ import annotations

from pyrung.core import (
    Block,
    Bool,
    Int,
    Program,
    TagType,
    Timer,
    copy,
    on_delay,
    out,
    run_function,
    rung,
)
from pyrung.core.analysis.prove import Intractable
from pyrung.core.analysis.prove.classify import _classify_dimensions


class TestIntractableTags:
    """Item 3 follow-up: Intractable.tags field is populated."""

    def test_unbounded_domain_tags(self):
        """Intractable from unbounded domain carries tag names."""
        trigger = Bool("Trigger", external=True)
        result_tag = Int("Result")

        def compute() -> dict:
            return {"result": 42}

        with Program(strict=False) as logic:
            with rung(trigger):
                run_function(compute, outs={"result": result_tag})
            with rung(result_tag == 1):
                out(Bool("Output"))

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert "Result" in result.tags


class TestIntractableHints:
    """Intractable results carry actionable hints for the user."""

    def test_pointer_tag_hint(self):
        """Pointer into a block > 1000 elements gets a hint naming the block."""
        from pyrung.core import copy

        blk = Block("Regs", TagType.INT, 1, 1500)
        idx = Int("Idx", external=True)
        dest = Int("Out")

        with Program(strict=False) as logic:
            with rung():
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert any("pointer" in h and "Regs" in h for h in result.hints)
        assert any("Idx" in h for h in result.hints)

    def test_wide_range_hint(self):
        """Tag with min/max range > 1000 gets a 'too wide' hint."""
        level = Int("Level", external=True, min=0, max=5000)
        threshold = Int("Threshold", external=True)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with rung(level > threshold):
                out(alarm)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert any("too wide" in h and "Level" in h for h in result.hints)

    def test_no_constraint_hint(self):
        """Tag with no choices/min/max gets a generic hint."""
        val = Int("Val", external=True)
        other = Int("Other", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with rung(val > other):
                out(flag)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert any("no domain constraint" in h for h in result.hints)

    def test_function_output_hint(self):
        """Unannotated function output gets a hint."""
        trigger = Bool("Trigger", external=True)
        result_tag = Int("Result")

        def compute() -> dict:
            return {"result": 42}

        with Program(strict=False) as logic:
            with rung(trigger):
                run_function(compute, outs={"result": result_tag})
            with rung(result_tag == 1):
                out(Bool("Output"))

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert any("function output" in h and "Result" in h for h in result.hints)

    def test_hints_suggest_choices_or_testing(self):
        """Unbounded hints suggest choices= or dt= testing."""
        val = Int("Val", external=True)
        other = Int("Other", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with rung(val > other):
                out(flag)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert all("choices=" in h or "dt= testing" in h for h in result.hints)


class TestThresholdBlockerHints:
    """Threshold tags are bounded by what writes them. Hints name the rest."""

    def test_shared_threshold_blocks_absorption(self):
        """One threshold shared across progress sources stays explicit."""
        enable_a = Bool("EnableA", external=True)
        enable_b = Bool("EnableB", external=True)
        shared_ts = Int("SharedWatchdogTs")
        t_a = Timer.clone("SharedA")
        t_b = Timer.clone("SharedB")
        alarm_a = Bool("SharedAlarmA")
        alarm_b = Bool("SharedAlarmB")

        with Program(strict=False) as logic:
            with rung():
                copy(500, shared_ts)
            with rung(enable_a):
                on_delay(t_a, preset=1000)
            with rung(enable_b):
                on_delay(t_b, preset=1000)
            with rung(t_a.Acc > shared_ts):
                out(alarm_a)
            with rung(t_b.Acc > shared_ts):
                out(alarm_b)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, done_acc, _done_presets, _done_kinds = result
        assert "SharedWatchdogTs" in stateful
        assert "SharedA_Acc" in stateful
        assert "SharedB_Acc" in stateful
        assert done_acc == {}

    def test_threshold_tag_non_threshold_comparison_blocks_absorption(self):
        """Threshold tags used in other comparisons stay explicit."""
        enable = Bool("Enable", external=True)
        ts = Int("ComparedThreshold")
        t = Timer.clone("ComparedTmr")
        alarm = Bool("ComparedAlarm")
        mode = Bool("ThresholdMode")

        with Program(strict=False) as logic:
            with rung():
                copy(500, ts)
            with rung(enable):
                on_delay(t, preset=1000)
            with rung(t.Acc > ts):
                out(alarm)
            with rung(ts == 500):
                out(mode)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, done_acc, _done_presets, _done_kinds = result
        assert "ComparedThreshold" in stateful
        assert "ComparedTmr_Acc" in stateful
        assert done_acc == {}

    def test_data_read_blocker_falls_back_to_structural_bounding(self):
        """Data-flow reads can block absorption without forcing Intractable."""
        enable = Bool("Enable", external=True)
        threshold = Int("DataReadThreshold", final=True)
        t = Timer.clone("DataReadTmr")
        saved = Int("SavedValue")
        alarm = Bool("DataReadAlarm")

        with Program(strict=False) as logic:
            with rung():
                copy(500, threshold)
            with rung(enable):
                on_delay(t, preset=1000)
            with rung():
                copy(t.Acc, saved)
            with rung(t.Acc > threshold):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, done_acc, _done_presets, _done_kinds = result
        assert "DataReadTmr_Acc" in stateful
        assert "DataReadThreshold" in stateful
        assert done_acc == {}


class TestMultipleBlockers:
    """Intractable reports all blockers at once, not one at a time."""

    def test_multiple_unbounded_tags_all_reported(self):
        """Two independent unbounded tags both appear in a single Intractable."""
        a = Int("A", external=True)
        b = Int("B", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with rung(a > b):
                out(flag)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert "A" in result.tags
        assert "B" in result.tags
