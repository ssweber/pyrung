"""Tests for the static facts that how() steers with."""

from __future__ import annotations

from pyrung.core import (
    Bool,
    Counter,
    Int,
    Program,
    copy,
    count_down,
    count_up,
    out,
    rung,
)
from pyrung.core.analysis.prove import Intractable
from pyrung.core.analysis.prove.classify import _classify_dimensions

# ===================================================================
# Absorption gap regression tests
# ===================================================================


class TestCountDownAbsorptionGaps:
    """CountDown counters are excluded from all absorption paths."""

    def test_count_down_reset_via_data_copy_of_acc_blocks_absorption(self):
        """Reset fed by a data copy of the accumulator is NOT threshold-mediated."""
        enable = Bool("Enable", external=True)
        acc_mirror = Int("AccMirror")
        reset_helper = Bool("ResetHelper")
        counter = Counter.clone("DataFedCtd")

        with Program(strict=False) as logic:
            with rung(enable):
                count_down(counter, preset=-5).reset(reset_helper)
            with rung():
                copy(counter.Acc, acc_mirror)
            with rung(acc_mirror <= -3):
                out(reset_helper)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert "DataFedCtd_Acc" in stateful

    def test_large_preset_counter_acc_is_bounded_by_its_preset(self):
        """A counter reset by its own Done is counted concretely up to its preset."""
        enable = Bool("Enable", external=True)
        gate = Bool("Gate", external=True)
        counter_a = Counter.clone("CA")
        counter_b = Counter.clone("CB")
        helper = Bool("Helper")

        with Program(strict=False) as logic:
            with rung(enable):
                count_up(counter_a, 100).reset(counter_a.Done)
            with rung(counter_a.Done):
                out(helper)
            with rung(gate):
                count_down(counter_b, preset=-5).reset(helper)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        if "CA_Acc" in stateful:
            assert max(stateful["CA_Acc"]) <= 100
