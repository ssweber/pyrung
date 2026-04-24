"""Tests for runtime bounds-check at scan commit."""

from __future__ import annotations

import pytest

from pyrung.core import PLC, Bool, Int, Program, Real, Rung, calc, copy


class TestRangeViolation:
    def test_max_violation(self):
        Enable = Bool("Enable")
        P = Real("P", min=0, max=100)

        with Program() as logic:
            with Rung(Enable):
                calc(P + 150, P)

        plc = PLC(logic, dt=0.01)
        plc.patch({"Enable": True, "P": 50.0})
        plc.step()

        assert "P" in plc.bounds_violations
        assert plc.bounds_violations["P"].kind == "range"
        assert plc.current_state.tags["P"] == 200.0

    def test_min_violation(self):
        Enable = Bool("Enable")
        P = Real("P", min=0, max=100)

        with Program() as logic:
            with Rung(Enable):
                calc(P - 10, P)

        plc = PLC(logic, dt=0.01)
        plc.patch({"Enable": True, "P": 5.0})
        plc.step()

        assert "P" in plc.bounds_violations
        assert plc.bounds_violations["P"].kind == "range"
        assert plc.current_state.tags["P"] == -5.0

    def test_within_range(self):
        Enable = Bool("Enable")
        P = Real("P", min=0, max=100)

        with Program() as logic:
            with Rung(Enable):
                calc(P + 10, P)

        plc = PLC(logic, dt=0.01)
        plc.patch({"Enable": True, "P": 40.0})
        plc.step()

        assert plc.bounds_violations == {}
        assert plc.current_state.tags["P"] == 50.0

    def test_min_only(self):
        Enable = Bool("Enable")
        Temp = Real("Temp", min=-40)

        with Program() as logic:
            with Rung(Enable):
                calc(Temp - 100, Temp)

        plc = PLC(logic, dt=0.01)
        plc.patch({"Enable": True, "Temp": 0.0})
        plc.step()

        assert "Temp" in plc.bounds_violations

    def test_max_only(self):
        Enable = Bool("Enable")
        Speed = Int("Speed", max=1000)

        with Program() as logic:
            with Rung(Enable):
                calc(Speed + 500, Speed)

        plc = PLC(logic, dt=0.01)
        plc.patch({"Enable": True, "Speed": 800})
        plc.step()

        assert "Speed" in plc.bounds_violations
        assert plc.current_state.tags["Speed"] == 1300


class TestChoicesViolation:
    def test_choices_violation(self):
        Enable = Bool("Enable")
        Mode = Int("Mode", choices={0: "Off", 1: "On", 2: "Auto"})
        Src = Int("Src")

        with Program() as logic:
            with Rung(Enable):
                copy(Src, Mode)

        plc = PLC(logic, dt=0.01)
        plc.patch({"Enable": True, "Src": 5})
        plc.step()

        assert "Mode" in plc.bounds_violations
        assert plc.bounds_violations["Mode"].kind == "choices"
        assert plc.current_state.tags["Mode"] == 5

    def test_choices_ok(self):
        Enable = Bool("Enable")
        Mode = Int("Mode", choices={0: "Off", 1: "On", 2: "Auto"})
        Src = Int("Src")

        with Program() as logic:
            with Rung(Enable):
                copy(Src, Mode)

        plc = PLC(logic, dt=0.01)
        plc.patch({"Enable": True, "Src": 1})
        plc.step()

        assert plc.bounds_violations == {}


class TestTypeClampsBeforeBoundsCheck:
    def test_int_type_clamp_then_tag_constraint(self):
        Enable = Bool("Enable")
        Lvl = Int("Lvl", min=0, max=500)

        with Program() as logic:
            with Rung(Enable):
                copy(40000, Lvl)

        plc = PLC(logic, dt=0.01)
        plc.patch({"Enable": True})
        plc.step()

        assert plc.current_state.tags["Lvl"] == 32767
        assert "Lvl" in plc.bounds_violations
        assert plc.bounds_violations["Lvl"].kind == "range"


class TestNoConstraints:
    def test_unconstrained_tag_never_violates(self):
        Enable = Bool("Enable")
        X = Int("X")

        with Program() as logic:
            with Rung(Enable):
                copy(32000, X)

        plc = PLC(logic, dt=0.01)
        plc.patch({"Enable": True})
        plc.step()

        assert plc.bounds_violations == {}


class TestMultipleViolations:
    def test_two_tags_violated(self):
        Enable = Bool("Enable")
        A = Real("A", min=0, max=10)
        B = Real("B", min=0, max=10)

        with Program() as logic:
            with Rung(Enable):
                copy(99, A)
                copy(99, B)

        plc = PLC(logic, dt=0.01)
        plc.patch({"Enable": True})
        plc.step()

        assert "A" in plc.bounds_violations
        assert "B" in plc.bounds_violations


class TestViolationsClearedNextScan:
    def test_cleared_on_clean_scan(self):
        Enable = Bool("Enable")
        P = Real("P", min=0, max=100)

        with Program() as logic:
            with Rung(Enable):
                copy(200, P)

        plc = PLC(logic, dt=0.01)
        plc.patch({"Enable": True})
        plc.step()
        assert "P" in plc.bounds_violations

        plc.patch({"Enable": False})
        plc.step()
        assert plc.bounds_violations == {}


class TestWarningEmitted:
    def test_warning_on_violation(self):
        Enable = Bool("Enable")
        P = Real("P", min=0, max=100)

        with Program() as logic:
            with Rung(Enable):
                copy(200, P)

        plc = PLC(logic, dt=0.01)
        plc.patch({"Enable": True})

        with pytest.warns(UserWarning, match="P"):
            plc.step()
