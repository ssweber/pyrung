"""Tests for the autoharness: automatic feedback synthesis from Physical + link= declarations."""

from __future__ import annotations

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Field,
    Harness,
    Program,
    Real,
    Rung,
    out,
    profile,
    udt,
)
from pyrung.core.harness import _profile_registry
from pyrung.core.physical import Physical

# --- Fixtures: Physical declarations ---

LIMIT_SWITCH = Physical("LimitSwitch", on_delay="20ms", off_delay="10ms")
SLOW_VALVE = Physical("SlowValve", on_delay="100ms", off_delay="200ms")
FAST_SENSOR = Physical("FastSensor", on_delay="5ms", off_delay="5ms")
TEMP_SENSOR = Physical("TempSensor", profile="test_thermal")


# --- Fixtures: UDTs ---


@udt()
class SimplePair:
    En: Bool
    Fb: Bool = Field(physical=LIMIT_SWITCH, link="En")  # ty: ignore[invalid-assignment]


@udt()
class AsymmetricValve:
    En: Bool
    Fb: Bool = Field(physical=SLOW_VALVE, link="En")  # ty: ignore[invalid-assignment]


@udt()
class MultiFeedback:
    En: Bool
    Fb_Contact: Bool = Field(physical=LIMIT_SWITCH, link="En")  # ty: ignore[invalid-assignment]
    Fb_Vacuum: Bool = Field(physical=FAST_SENSOR, link="En")  # ty: ignore[invalid-assignment]


@udt()
class MixedDevice:
    En: Bool
    Fb_Contact: Bool = Field(physical=LIMIT_SWITCH, link="En")  # ty: ignore[invalid-assignment]
    Fb_Temp: Real = Field(physical=TEMP_SENSOR, link="En", min=0, max=250, uom="degC")  # ty: ignore[invalid-assignment]


# --- Helpers ---


def _make_plc(device_udt, dt: float = 0.010):
    """Build a PLC that drives En from a Cmd tag via out()."""
    Cmd = Bool("Cmd")
    with Program() as logic:
        with Rung(Cmd):
            out(device_udt[1].En)
    plc = PLC(logic, dt=dt)
    return plc, Cmd, device_udt


def _fb(plc, tag):
    return plc.current_state.tags.get(tag.name, tag.default)


# --- Bool Fb Tests ---


class TestBoolAutoharness:
    def test_fb_rises_after_on_delay(self):
        plc, Cmd, dev = _make_plc(SimplePair)
        harness = Harness(plc)
        harness.install()

        plc.patch({Cmd: True})
        # on_delay=20ms, dt=10ms → 2 scan delay
        # Edge at scan 1, Fb scheduled at scan 3, arrives after 3rd step
        plc.step()  # scan 1: En rises
        assert _fb(plc, dev[1].En) is True
        assert _fb(plc, dev[1].Fb) is False

        plc.step()  # scan 2: not yet
        assert _fb(plc, dev[1].Fb) is False

        plc.step()  # scan 3: Fb arrives
        assert _fb(plc, dev[1].Fb) is True

    def test_fb_falls_after_off_delay(self):
        plc, Cmd, dev = _make_plc(SimplePair)
        harness = Harness(plc)
        harness.install()

        # Raise En and wait for Fb
        plc.patch({Cmd: True})
        plc.run_for(0.050)
        assert _fb(plc, dev[1].Fb) is True

        # Drop En — off_delay=10ms, dt=10ms → 1 scan delay
        plc.patch({Cmd: False})
        plc.step()  # En falls, schedules Fb=False at +1
        assert _fb(plc, dev[1].En) is False
        assert _fb(plc, dev[1].Fb) is True  # not yet

        plc.step()  # Fb=False arrives
        assert _fb(plc, dev[1].Fb) is False

    def test_asymmetric_delay(self):
        plc, Cmd, dev = _make_plc(AsymmetricValve)
        harness = Harness(plc)
        harness.install()

        # on_delay=100ms at dt=10ms → 10 scan delay
        plc.patch({Cmd: True})
        plc.run_for(0.150)  # 150ms > 100ms
        assert _fb(plc, dev[1].Fb) is True

        # off_delay=200ms at dt=10ms → 20 scan delay
        plc.patch({Cmd: False})
        plc.run_for(0.250)  # 250ms > 200ms
        assert _fb(plc, dev[1].Fb) is False

    def test_multiple_fb_schedule_independently(self):
        plc, Cmd, dev = _make_plc(MultiFeedback)
        harness = Harness(plc)
        harness.install()

        # FastSensor: on_delay=5ms at dt=10ms → 1 scan
        # LimitSwitch: on_delay=20ms at dt=10ms → 2 scans
        plc.patch({Cmd: True})
        plc.step()  # scan 1: En rises

        plc.step()  # scan 2: FastSensor due (target=scan+1)
        assert _fb(plc, dev[1].Fb_Vacuum) is True
        assert _fb(plc, dev[1].Fb_Contact) is False

        plc.step()  # scan 3: LimitSwitch due (target=scan+2)
        assert _fb(plc, dev[1].Fb_Contact) is True

    def test_1_tick_floor(self):
        # on_delay=20ms at dt=100ms → ceil(0.2) = 1 (floor)
        plc, Cmd, dev = _make_plc(SimplePair, dt=0.100)
        harness = Harness(plc)
        harness.install()

        plc.patch({Cmd: True})
        plc.step()  # En rises, schedules Fb at +1
        plc.step()  # Fb arrives
        assert _fb(plc, dev[1].Fb) is True

    def test_different_dt_produces_correct_ticks(self):
        # on_delay=20ms at dt=1ms → 20 scan delay
        plc, Cmd, dev = _make_plc(SimplePair, dt=0.001)
        harness = Harness(plc)
        harness.install()

        plc.patch({Cmd: True})
        plc.run_for(0.030)  # 30ms > 20ms
        assert _fb(plc, dev[1].Fb) is True

    def test_force_overrides_harness_patch(self):
        plc, Cmd, dev = _make_plc(SimplePair)
        harness = Harness(plc)
        harness.install()

        plc.force(dev[1].Fb.name, False)
        plc.patch({Cmd: True})
        plc.run_for(0.050)  # harness patches Fb=True, but force holds it
        assert _fb(plc, dev[1].Fb) is False

        plc.unforce(dev[1].Fb.name)
        # Re-trigger edge
        plc.patch({Cmd: False})
        plc.step()
        plc.patch({Cmd: True})
        plc.run_for(0.050)
        assert _fb(plc, dev[1].Fb) is True

    def test_user_patch_coexists_with_harness(self):
        plc, Cmd, dev = _make_plc(SimplePair)
        harness = Harness(plc)
        harness.install()
        other = Bool("OtherTag")

        plc.patch({Cmd: True, other: True})
        plc.step()
        assert plc.current_state.tags.get("OtherTag") is True

        plc.run_for(0.050)
        assert _fb(plc, dev[1].Fb) is True

    def test_rapid_toggle(self):
        plc, Cmd, dev = _make_plc(AsymmetricValve)
        harness = Harness(plc)
        harness.install()

        # on_delay=100ms, off_delay=200ms at dt=10ms
        plc.patch({Cmd: True})
        plc.step()  # En rises, Fb=True scheduled at +10

        plc.patch({Cmd: False})
        plc.step()  # En falls, Fb=False scheduled at +20

        # Fb=True arrives first
        plc.run_for(0.120)
        assert _fb(plc, dev[1].Fb) is True

        # Then Fb=False arrives
        plc.run_for(0.250)
        assert _fb(plc, dev[1].Fb) is False

    def test_install_idempotent(self):
        plc, Cmd, dev = _make_plc(SimplePair)
        harness = Harness(plc)
        harness.install()
        harness.install()
        assert len(plc._pre_scan_callbacks) == 1

    def test_uninstall(self):
        plc, Cmd, dev = _make_plc(SimplePair)
        harness = Harness(plc)
        harness.install()
        harness.uninstall()
        assert len(plc._pre_scan_callbacks) == 0

        plc.patch({Cmd: True})
        plc.run_for(0.050)
        assert _fb(plc, dev[1].Fb) is False

    def test_run_for_with_harness(self):
        plc, Cmd, dev = _make_plc(SimplePair)
        harness = Harness(plc)
        harness.install()

        plc.patch({Cmd: True})
        plc.run_for(0.050)  # 50ms > 20ms on_delay
        assert _fb(plc, dev[1].Fb) is True


# --- Analog Fb Tests ---


class TestAnalogAutoharness:
    def setup_method(self):
        _profile_registry.clear()

    def test_analog_fb_driven_by_profile(self):
        @profile("test_thermal")
        def thermal(cur, en, dt):
            if en:
                return cur + 10.0 * dt
            return cur

        plc, Cmd, dev = _make_plc(MixedDevice)
        harness = Harness(plc)
        harness.install()

        plc.patch({Cmd: True})
        plc.run_for(0.100)
        fb_temp = plc.current_state.tags.get(dev[1].Fb_Temp.name, 0.0)
        assert fb_temp > 0.5

    def test_profile_receives_correct_args(self):
        calls: list[tuple[float, bool, float]] = []

        @profile("test_thermal")
        def capture(cur, en, dt):
            calls.append((cur, en, dt))
            return cur + 1.0 * dt

        plc, Cmd, dev = _make_plc(MixedDevice, dt=0.005)
        harness = Harness(plc)
        harness.install()

        plc.patch({Cmd: True})
        plc.step()  # En rises, activates analog coupling
        plc.step()  # first profile tick

        assert len(calls) >= 1
        cur, en, dt = calls[0]
        assert en is True
        assert dt == pytest.approx(0.005)

    def test_profile_dt_stable(self):
        @profile("test_thermal")
        def ramp(cur, en, dt):
            return cur + 100.0 * dt if en else cur

        results = {}
        for test_dt in [0.001, 0.010, 0.100]:
            plc, Cmd, dev = _make_plc(MixedDevice, dt=test_dt)
            harness = Harness(plc)
            harness.install()

            plc.patch({Cmd: True})
            plc.run_for(0.5)
            results[test_dt] = plc.current_state.tags.get(dev[1].Fb_Temp.name, 0.0)

        for dt_val, result in results.items():
            assert result == pytest.approx(50.0, rel=0.25), f"dt={dt_val} gave {result}"

    def test_mixed_bool_and_analog_on_same_en(self):
        @profile("test_thermal")
        def thermal(cur, en, dt):
            return cur + 1.0 * dt if en else cur

        plc, Cmd, dev = _make_plc(MixedDevice)
        harness = Harness(plc)
        harness.install()

        plc.patch({Cmd: True})
        plc.run_for(0.050)

        assert _fb(plc, dev[1].Fb_Contact) is True
        fb_temp = plc.current_state.tags.get(dev[1].Fb_Temp.name, 0.0)
        assert fb_temp > 0.0

    def test_missing_profile_silently_skips(self):
        plc, Cmd, dev = _make_plc(MixedDevice)
        harness = Harness(plc)
        harness.install()

        plc.patch({Cmd: True})
        plc.run_for(0.050)
        fb_temp = plc.current_state.tags.get(dev[1].Fb_Temp.name, 0.0)
        assert fb_temp == 0.0
        assert _fb(plc, dev[1].Fb_Contact) is True

    def test_analog_profile_responds_to_en_state(self):
        @profile("test_thermal")
        def thermal(cur, en, dt):
            if en:
                return cur + 10.0 * dt
            return cur - 5.0 * dt

        plc, Cmd, dev = _make_plc(MixedDevice)
        harness = Harness(plc)
        harness.install()

        plc.patch({Cmd: True})
        plc.run_for(0.100)
        peak = plc.current_state.tags.get(dev[1].Fb_Temp.name, 0.0)
        assert peak > 0

        plc.patch({Cmd: False})
        plc.run_for(0.100)
        decayed = plc.current_state.tags.get(dev[1].Fb_Temp.name, 0.0)
        assert decayed < peak
