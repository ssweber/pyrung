"""Tests for the autoharness: automatic feedback synthesis from Physical + link= declarations."""

from __future__ import annotations

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Char,
    Coupling,
    Field,
    Harness,
    Int,
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
ENCODER = Physical("Encoder", profile="test_encoder")


# --- Fixtures: UDTs ---


@udt()
class SimplePair:
    En: Bool
    Fb: Bool = Field(physical=LIMIT_SWITCH, link="En")


@udt()
class AsymmetricValve:
    En: Bool
    Fb: Bool = Field(physical=SLOW_VALVE, link="En")


@udt()
class MultiFeedback:
    En: Bool
    Fb_Contact: Bool = Field(physical=LIMIT_SWITCH, link="En")
    Fb_Vacuum: Bool = Field(physical=FAST_SENSOR, link="En")


@udt()
class MixedDevice:
    En: Bool
    Fb_Contact: Bool = Field(physical=LIMIT_SWITCH, link="En")
    Fb_Temp: Real = Field(physical=TEMP_SENSOR, link="En", min=0, max=250, uom="degC")


@udt()
class EncoderDevice:
    En: Bool
    Fb_Pulse: Bool = Field(physical=ENCODER, link="En")


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


class TestBoolProfileAutoharness:
    def setup_method(self):
        _profile_registry.clear()

    def test_bool_profile_toggles_feedback(self):
        phase = [0.0]

        @profile("test_encoder")
        def encoder(cur, en, dt):
            if not en:
                phase[0] = 0.0
                return False
            phase[0] += dt
            period = 0.050
            return (phase[0] % period) < (period / 2)

        plc, Cmd, dev = _make_plc(EncoderDevice, dt=0.005)
        harness = Harness(plc)
        harness.install()

        plc.patch({Cmd: True})
        values = []
        for _ in range(20):
            plc.step()
            values.append(_fb(plc, dev[1].Fb_Pulse))

        assert True in values
        assert False in values

    def test_bool_profile_inactive_when_en_false(self):
        @profile("test_encoder")
        def encoder(cur, en, dt):
            if not en:
                return False
            return True

        plc, Cmd, dev = _make_plc(EncoderDevice, dt=0.010)
        harness = Harness(plc)
        harness.install()

        plc.run_for(0.050)
        assert _fb(plc, dev[1].Fb_Pulse) is False


# --- Value-Trigger Fixtures ---

STATE_CHOICES = {0: "IDLE", 1: "RUNNING", 2: "SORTING"}


@udt()
class IntTriggerPair:
    State: Int = Field(choices=STATE_CHOICES)
    Fb: Bool = Field(physical=LIMIT_SWITCH, link="State:SORTING")


@udt()
class IntTriggerLiteral:
    State: Int
    Fb: Bool = Field(physical=LIMIT_SWITCH, link="State:2")


@udt()
class MultiTrigger:
    State: Int = Field(choices=STATE_CHOICES)
    RunFb: Bool = Field(physical=FAST_SENSOR, link="State:RUNNING")
    SortFb: Bool = Field(physical=LIMIT_SWITCH, link="State:SORTING")


@udt()
class CharTriggerPair:
    Status: Char
    Fb: Bool = Field(physical=LIMIT_SWITCH, link="Status:Y")


@udt()
class IntTriggerAnalog:
    State: Int = Field(choices=STATE_CHOICES)
    Fb_Temp: Real = Field(physical=TEMP_SENSOR, link="State:SORTING", min=0, max=250, uom="degC")


def _make_trigger_plc(device_udt, en_field="State", dt=0.010):
    with Program() as logic:
        pass
    plc = PLC(logic, dt=dt)
    plc._register_known_tag(getattr(device_udt[1], en_field))
    return plc, device_udt


# --- Value-Trigger Bool Tests ---


class TestTriggerValueAutoharness:
    def test_fb_rises_on_trigger_match(self):
        plc, dev = _make_trigger_plc(IntTriggerPair)
        harness = Harness(plc)
        harness.install()

        plc.patch({dev[1].State: 2})
        plc.step()  # scan 1: State becomes SORTING, edge detected
        assert _fb(plc, dev[1].Fb) is False

        plc.step()  # scan 2: not yet (on_delay=20ms, dt=10ms → 2 scans)
        assert _fb(plc, dev[1].Fb) is False

        plc.step()  # scan 3: Fb arrives
        assert _fb(plc, dev[1].Fb) is True

    def test_fb_falls_on_trigger_leave(self):
        plc, dev = _make_trigger_plc(IntTriggerPair)
        harness = Harness(plc)
        harness.install()

        plc.patch({dev[1].State: 2})
        plc.run_for(0.050)
        assert _fb(plc, dev[1].Fb) is True

        plc.patch({dev[1].State: 0})
        plc.step()  # off-edge detected (off_delay=10ms, dt=10ms → 1 scan)
        assert _fb(plc, dev[1].Fb) is True

        plc.step()  # Fb falls
        assert _fb(plc, dev[1].Fb) is False

    def test_non_matching_transition_no_effect(self):
        plc, dev = _make_trigger_plc(IntTriggerPair)
        harness = Harness(plc)
        harness.install()

        plc.patch({dev[1].State: 1})  # RUNNING, not SORTING
        plc.run_for(0.050)
        assert _fb(plc, dev[1].Fb) is False

    def test_truthy_to_truthy_on_edge(self):
        plc, dev = _make_trigger_plc(IntTriggerPair)
        harness = Harness(plc)
        harness.install()

        plc.patch({dev[1].State: 1})
        plc.run_for(0.050)
        assert _fb(plc, dev[1].Fb) is False

        plc.patch({dev[1].State: 2})
        plc.run_for(0.050)
        assert _fb(plc, dev[1].Fb) is True

    def test_truthy_to_truthy_off_edge(self):
        plc, dev = _make_trigger_plc(IntTriggerPair)
        harness = Harness(plc)
        harness.install()

        plc.patch({dev[1].State: 2})
        plc.run_for(0.050)
        assert _fb(plc, dev[1].Fb) is True

        plc.patch({dev[1].State: 1})
        plc.run_for(0.050)
        assert _fb(plc, dev[1].Fb) is False

    def test_literal_int_without_choices(self):
        plc, dev = _make_trigger_plc(IntTriggerLiteral)
        harness = Harness(plc)
        harness.install()

        plc.patch({dev[1].State: 2})
        plc.run_for(0.050)
        assert _fb(plc, dev[1].Fb) is True

        plc.patch({dev[1].State: 0})
        plc.run_for(0.050)
        assert _fb(plc, dev[1].Fb) is False

    def test_multiple_triggers_same_enable(self):
        plc, dev = _make_trigger_plc(MultiTrigger)
        harness = Harness(plc)
        harness.install()

        plc.patch({dev[1].State: 1})
        plc.run_for(0.050)
        assert _fb(plc, dev[1].RunFb) is True
        assert _fb(plc, dev[1].SortFb) is False

        plc.patch({dev[1].State: 2})
        plc.run_for(0.050)
        assert _fb(plc, dev[1].RunFb) is False
        assert _fb(plc, dev[1].SortFb) is True

    def test_coexists_with_plain_bool_coupling(self):
        @udt()
        class Mixed:
            En: Bool
            State: Int = Field(choices=STATE_CHOICES)
            BoolFb: Bool = Field(physical=FAST_SENSOR, link="En")
            TrigFb: Bool = Field(physical=LIMIT_SWITCH, link="State:SORTING")

        plc, dev = _make_trigger_plc(Mixed)
        plc._register_known_tag(dev[1].En)
        harness = Harness(plc)
        harness.install()

        plc.patch({dev[1].En: True})
        plc.run_for(0.050)
        assert _fb(plc, dev[1].BoolFb) is True
        assert _fb(plc, dev[1].TrigFb) is False

        plc.patch({dev[1].State: 2})
        plc.run_for(0.050)
        assert _fb(plc, dev[1].BoolFb) is True
        assert _fb(plc, dev[1].TrigFb) is True

    def test_flat_tag_int_trigger(self):
        State = Int("FlatState", choices={0: "IDLE", 2: "SORTING"})
        Fb = Bool("FlatFb", physical=LIMIT_SWITCH, link="FlatState:SORTING")

        with Program() as logic:
            pass
        plc = PLC(logic, dt=0.010)
        plc._register_known_tag(State)
        plc._register_known_tag(Fb)
        harness = Harness(plc)
        harness.install()

        plc.patch({State: 2})
        plc.run_for(0.050)
        assert _fb(plc, Fb) is True

        plc.patch({State: 0})
        plc.run_for(0.050)
        assert _fb(plc, Fb) is False


# --- Value-Trigger Char Tests ---


class TestCharTriggerAutoharness:
    def test_char_trigger_fires(self):
        plc, dev = _make_trigger_plc(CharTriggerPair, en_field="Status")
        harness = Harness(plc)
        harness.install()

        plc.patch({dev[1].Status: "Y"})
        plc.run_for(0.050)
        assert _fb(plc, dev[1].Fb) is True

    def test_char_trigger_clears(self):
        plc, dev = _make_trigger_plc(CharTriggerPair, en_field="Status")
        harness = Harness(plc)
        harness.install()

        plc.patch({dev[1].Status: "Y"})
        plc.run_for(0.050)
        assert _fb(plc, dev[1].Fb) is True

        plc.patch({dev[1].Status: "N"})
        plc.run_for(0.050)
        assert _fb(plc, dev[1].Fb) is False


# --- Value-Trigger Analog Tests ---


class TestTriggerValueAnalogAutoharness:
    def setup_method(self):
        _profile_registry.clear()

    def test_analog_activates_on_trigger_match(self):
        @profile("test_thermal")
        def thermal(cur, en, dt):
            if en:
                return cur + 10.0 * dt
            return cur

        plc, dev = _make_trigger_plc(IntTriggerAnalog)
        harness = Harness(plc)
        harness.install()

        plc.patch({dev[1].State: 2})
        plc.run_for(0.100)
        fb_temp = plc.current_state.tags.get(dev[1].Fb_Temp.name, 0.0)
        assert fb_temp > 0.5

    def test_analog_en_false_on_mismatch(self):
        calls: list[tuple[float, bool, float]] = []

        @profile("test_thermal")
        def capture(cur, en, dt):
            calls.append((cur, en, dt))
            return cur + (10.0 if en else -5.0) * dt

        plc, dev = _make_trigger_plc(IntTriggerAnalog)
        harness = Harness(plc)
        harness.install()

        plc.patch({dev[1].State: 2})
        plc.run_for(0.050)
        peak = plc.current_state.tags.get(dev[1].Fb_Temp.name, 0.0)
        assert peak > 0

        en_while_match = [en for _, en, _ in calls]
        assert all(en_while_match)

        calls.clear()
        plc.patch({dev[1].State: 0})
        plc.step()  # pre_scan ticks with old State=2 still, then patch applied
        plc.step()  # now State=0, profile sees en=False
        assert calls[-1][1] is False


# --- Value-Trigger Validation Tests ---


class TestTriggerValidation:
    def test_invalid_choice_label_raises(self):
        with pytest.raises(ValueError, match="not found in choices"):

            @udt()
            class Bad:
                State: Int = Field(choices=STATE_CHOICES)
                Fb: Bool = Field(physical=LIMIT_SWITCH, link="State:MISSING")

    def test_non_numeric_without_choices_raises(self):
        with pytest.raises(ValueError, match="has no choices map"):

            @udt()
            class Bad:
                State: Int
                Fb: Bool = Field(physical=LIMIT_SWITCH, link="State:SORTING")

    def test_trigger_on_bool_enable_raises(self):
        with pytest.raises(ValueError, match="not valid for BOOL"):

            @udt()
            class Bad:
                En: Bool
                Fb: Bool = Field(physical=LIMIT_SWITCH, link="En:1")

    def test_int_literal_without_choices_is_valid(self):
        @udt()
        class Valid:
            State: Int
            Fb: Bool = Field(physical=LIMIT_SWITCH, link="State:2")

        assert Valid is not None

    def test_flat_tag_invalid_trigger_raises(self):
        State = Int("BadState")
        Bool("BadFb", physical=LIMIT_SWITCH, link="BadState:SORTING")

        with Program() as logic:
            pass
        plc = PLC(logic, dt=0.010)
        plc._register_known_tag(State)
        plc._register_known_tag(Bool("BadFb", physical=LIMIT_SWITCH, link="BadState:SORTING"))
        harness = Harness(plc)
        with pytest.raises(ValueError, match="has no choices map"):
            harness.install()


# --- Coupling Summary Display ---


class TestTriggerCoupingSummary:
    def test_summary_includes_trigger_value(self):
        plc, dev = _make_trigger_plc(IntTriggerPair)
        harness = Harness(plc)
        harness.install()

        summary = harness.coupling_summary()
        bc = summary["bool_couplings"][0]
        assert bc["trigger_value"] == 2

    def test_summary_plain_coupling_trigger_is_none(self):
        plc, Cmd, dev = _make_plc(SimplePair)
        harness = Harness(plc)
        harness.install()

        summary = harness.coupling_summary()
        bc = summary["bool_couplings"][0]
        assert bc["trigger_value"] is None


# --- Public Coupling Iterator ---


class TestCouplings:
    def test_iterates_bool_couplings(self):
        plc, Cmd, dev = _make_plc(SimplePair)
        harness = Harness(plc)
        harness.install()

        couplings = list(harness.couplings())
        assert len(couplings) == 1
        c = couplings[0]
        assert isinstance(c, Coupling)
        assert c.en_name == dev[1].En.name
        assert c.fb_name == dev[1].Fb.name
        assert c.physical == LIMIT_SWITCH
        assert c.trigger_value is None

    def test_iterates_profile_couplings(self):
        @profile("test_thermal")
        def ramp(cur, en, dt):
            return cur + 1.0 * dt if en else cur

        plc, Cmd, dev = _make_plc(MixedDevice)
        harness = Harness(plc)
        harness.install()

        couplings = list(harness.couplings())
        assert len(couplings) == 2
        names = {c.fb_name for c in couplings}
        assert dev[1].Fb_Contact.name in names
        assert dev[1].Fb_Temp.name in names

    def test_trigger_value_populated(self):
        plc, dev = _make_trigger_plc(IntTriggerPair)
        harness = Harness(plc)
        harness.install()

        couplings = list(harness.couplings())
        assert len(couplings) == 1
        assert couplings[0].trigger_value == 2

    def test_empty_when_no_physical(self):
        Cmd = Bool("Cmd")
        Fb = Bool("Fb")
        with Program() as logic:
            with Rung(Cmd):
                out(Fb)
        plc = PLC(logic, dt=0.010)
        harness = Harness(plc)
        harness.install()

        assert list(harness.couplings()) == []

    def test_coupling_is_frozen(self):
        plc, Cmd, dev = _make_plc(SimplePair)
        harness = Harness(plc)
        harness.install()

        c = next(harness.couplings())
        with pytest.raises(AttributeError):
            c.en_name = "other"  # ty: ignore[invalid-assignment]
