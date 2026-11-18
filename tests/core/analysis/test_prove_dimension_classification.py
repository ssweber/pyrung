"""Tests for the static facts that how() steers with."""

from __future__ import annotations

from pyrung.core import (
    Bool,
    Int,
    Program,
    copy,
    latch,
    out,
    reset,
    rung,
)
from pyrung.core.analysis.prove import Intractable
from pyrung.core.analysis.prove.classify import _classify_dimensions

# ===================================================================
# Group 1: Dimension classification
# ===================================================================


class TestDimensionClassification:
    def test_ote_only_all_combinational(self):
        """Program with only OTE writes — all tags combinational, zero state dims."""
        input_a = Bool("InputA", external=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with rung(input_a):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, combinational, _done_acc, _done_presets, _done_kinds = result
        assert "Light" not in stateful
        assert "Light" in combinational
        assert "InputA" in nd

    def test_latch_reset_are_stateful(self):
        """Latch/reset writes make tags stateful when referenced."""
        button = Bool("Button", external=True)
        stop = Bool("Stop", external=True)
        running = Bool("Running")
        light = Bool("Light")

        with Program(strict=False) as logic:
            with rung(button):
                latch(running)
            with rung(stop):
                reset(running)
            with rung(running):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, combinational, _done_acc, _done_presets, _done_kinds = result
        assert "Running" in stateful
        assert stateful["Running"] == (False, True)
        assert "Button" in nd
        assert "Stop" in nd

    def test_unreferenced_bool_excluded(self):
        """Written Bool not referenced in any expression is excluded."""
        button = Bool("Button", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with rung(button):
                latch(flag)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, combinational, _done_acc, _done_presets, _done_kinds = result
        assert "Flag" not in stateful
        assert "Flag" in combinational

    def test_write_only_terminal_is_combinational(self):
        """Tag written by copy but never read is combinational (dead output)."""
        sensor = Bool("Sensor", external=True)
        level = Int("Level", external=True, min=0, max=10)
        output = Int("Output")

        with Program(strict=False) as logic:
            with rung(sensor):
                copy(level, output)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, combinational, _done_acc, _done_presets, _done_kinds = result
        assert "Output" not in stateful
        assert "Output" in combinational

    def test_external_tags_are_nondeterministic(self):
        """Tags with external=True are nondeterministic."""
        sensor = Bool("Sensor", external=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with rung(sensor):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        assert "Sensor" in nd

    def test_readonly_tags_excluded(self):
        """Readonly tags are excluded from enumeration."""
        config = Bool("Config", readonly=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with rung(config):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        assert "Config" not in nd
        assert "Config" not in stateful
