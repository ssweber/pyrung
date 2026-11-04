"""Tests for the autoharness: automatic feedback synthesis from Physical + link= declarations."""

from __future__ import annotations

import pytest

from pyrung.core import (
    PLC,
    Block,
    Bool,
    Char,
    Coupling,
    Field,
    Harness,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    copy,
    latch,
    out,
    udt,
)
from pyrung.core.analysis.graph import PlanStatus
from pyrung.core.harness import _parse_link_spec, format_link_spec, link_problem
from pyrung.core.physical import Approach, Physical, Pulse, Ramp
from pyrung.core.synthesis import plant_timer

# --- Fixtures: Physical declarations ---

LIMIT_SWITCH = Physical("LimitSwitch", on_delay="20ms", off_delay="10ms")
SLOW_VALVE = Physical("SlowValve", on_delay="100ms", off_delay="200ms")
FAST_SENSOR = Physical("FastSensor", on_delay="5ms", off_delay="5ms")
TEMP_SENSOR = Physical("TempSensor", profile=Ramp(up=10.0, down=-5.0))
BURNER = Physical("Burner", profile=Ramp(up=10.0, down=-2.0))
ENCODER = Physical("Encoder", profile=Pulse(on_dwell="25ms", off_dwell="25ms"))


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
class RampDevice:
    En: Bool
    Fb_Temp: Real = Field(physical=BURNER, link="En")


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
        # on_delay=20ms, dt=10ms → 2 scans of dwell.  The pre-logic plant reads
        # the *previous* commit's En, so En rises this scan but the plant only
        # sees it next scan: dwell begins scan 2, Fb True after scan 3 (the
        # command→feedback latency is on_delay + one input-read scan).
        plc.step()  # scan 1: En rises (committed); plant hasn't seen it yet
        assert _fb(plc, dev[1].En) is True
        assert _fb(plc, dev[1].Fb) is False

        plc.step()  # scan 2: plant sees En, dwell begins
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

        # Drop En — off_delay=10ms, dt=10ms → 1 dt of dwell.  The pre-logic plant
        # reads the *previous* commit's En, so the scan En falls the plant still
        # sees it high (Fb holds); it sees the drop next scan, then the off-delay
        # clears Fb — one input-read scan later than the command.
        plc.patch({Cmd: False})
        plc.step()  # En falls (committed); plant still saw it high → Fb holds
        assert _fb(plc, dev[1].En) is False
        assert _fb(plc, dev[1].Fb) is True

        plc.step()  # plant sees En low; off-delay clears Fb
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

        # FastSensor: on_delay=5ms at dt=10ms → crosses in 1 dt
        # LimitSwitch: on_delay=20ms at dt=10ms → 2 dt
        # The pre-logic plant sees En one scan after it commits, so each crossing
        # lands one input-read scan later than its raw dwell.
        plc.patch({Cmd: True})
        plc.step()  # scan 1: En commits; plant hasn't seen it yet
        assert _fb(plc, dev[1].Fb_Vacuum) is False
        assert _fb(plc, dev[1].Fb_Contact) is False

        plc.step()  # scan 2: FastSensor due, LimitSwitch not yet
        assert _fb(plc, dev[1].Fb_Vacuum) is True
        assert _fb(plc, dev[1].Fb_Contact) is False

        plc.step()  # scan 3: LimitSwitch due
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

    def test_rapid_toggle_glitch_suppressed(self):
        # Dwell: a command pulse shorter than on_delay must NOT fabricate Fb.
        # (Pre-dwell the transport-delay heap raised Fb from this 1-scan glitch.)
        plc, Cmd, dev = _make_plc(AsymmetricValve)
        harness = Harness(plc)
        harness.install()

        # on_delay=100ms (10 scans) >> the 1-scan pulse below, at dt=10ms.
        plc.patch({Cmd: True})
        plc.step()  # En rises for one scan
        plc.patch({Cmd: False})
        plc.step()  # En drops again — sub-on_delay glitch

        plc.run_for(0.300)  # well past both delays
        assert _fb(plc, dev[1].Fb) is False  # glitch never fabricated Fb

        # A *sustained* command still asserts (and then clears) the feedback.
        plc.patch({Cmd: True})
        plc.run_for(0.150)  # 150ms > 100ms on_delay
        assert _fb(plc, dev[1].Fb) is True

        plc.patch({Cmd: False})
        plc.run_for(0.250)  # 250ms > 200ms off_delay
        assert _fb(plc, dev[1].Fb) is False

    def test_install_idempotent(self):
        plc, Cmd, dev = _make_plc(SimplePair)
        harness = Harness(plc)
        harness.install()
        harness.install()
        assert plc._harness is harness

    def test_uninstall(self):
        plc, Cmd, dev = _make_plc(SimplePair)
        harness = Harness(plc)
        harness.install()
        plc.step()
        installed_epoch = plc._causal_lineage.current_epoch()
        harness.uninstall()
        assert plc._harness is None
        assert plc._synthesis is None or not plc._synthesis.plant
        assert plc._causal_lineage.current_epoch() is not installed_epoch

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
    """Analog feedback via a declarative ``Ramp`` on a mixed bool+analog device.

    ``MixedDevice.Fb_Temp`` is a ``Ramp(up=10.0, down=-5.0)`` — the drive/decay/
    dt-stability mechanics themselves are covered by :class:`TestAnalogRampSpec`;
    these exercise a *mixed* device where a bool and an analog Fb share one enable.
    """

    def test_analog_fb_driven_by_ramp(self):
        plc, Cmd, dev = _make_plc(MixedDevice)
        Harness(plc).install()

        plc.patch({Cmd: True})
        plc.run_for(0.100)
        fb_temp = plc.current_state.tags.get(dev[1].Fb_Temp.name, 0.0)
        assert fb_temp > 0.5

    def test_mixed_bool_and_analog_on_same_en(self):
        plc, Cmd, dev = _make_plc(MixedDevice)
        Harness(plc).install()

        plc.patch({Cmd: True})
        plc.run_for(0.050)

        assert _fb(plc, dev[1].Fb_Contact) is True
        fb_temp = plc.current_state.tags.get(dev[1].Fb_Temp.name, 0.0)
        assert fb_temp > 0.0

    def test_analog_ramp_responds_to_en_state(self):
        plc, Cmd, dev = _make_plc(MixedDevice)
        Harness(plc).install()

        plc.patch({Cmd: True})
        plc.run_for(0.100)
        peak = plc.current_state.tags.get(dev[1].Fb_Temp.name, 0.0)
        assert peak > 0

        plc.patch({Cmd: False})
        plc.run_for(0.100)
        decayed = plc.current_state.tags.get(dev[1].Fb_Temp.name, 0.0)
        assert decayed < peak


class TestAnalogRampSpec:
    """A declarative ``Ramp`` lowers to transparent plant rungs — no Python tick."""

    def test_ramp_is_lowered_to_plant_rungs(self):
        plc, Cmd, dev = _make_plc(RampDevice)
        harness = Harness(plc)
        harness.install()
        # Pure data → plant rungs (no Python tick, no pre-scan callback).
        assert plc._synthesis is not None and plc._synthesis.plant

    def test_ramp_drives_fb_upward_while_energized(self):
        plc, Cmd, dev = _make_plc(RampDevice)
        Harness(plc).install()
        plc.patch({Cmd: True})
        plc.run_for(0.100)  # ~10 degC/s while enabled
        assert _fb(plc, dev[1].Fb_Temp) > 0.5

    def test_ramp_no_decay_before_first_energize(self):
        # The arm latch keeps a never-energized plant at rest (no decay-from-cold).
        plc, Cmd, dev = _make_plc(RampDevice)
        Harness(plc).install()
        plc.run_for(0.100)
        assert _fb(plc, dev[1].Fb_Temp) == pytest.approx(0.0)

    def test_ramp_decays_after_disable(self):
        plc, Cmd, dev = _make_plc(RampDevice)
        Harness(plc).install()
        plc.patch({Cmd: True})
        plc.run_for(0.100)
        peak = _fb(plc, dev[1].Fb_Temp)
        assert peak > 0.5
        plc.patch({Cmd: False})
        plc.run_for(0.100)  # decays at -2 degC/s once armed and off-enable
        assert _fb(plc, dev[1].Fb_Temp) < peak

    def test_ramp_is_dt_stable(self):
        # Per-second rates against sys.dt → same convergence across scan periods.
        results = {}
        for test_dt in [0.001, 0.010, 0.050]:
            plc, Cmd, dev = _make_plc(RampDevice, dt=test_dt)
            Harness(plc).install()
            plc.patch({Cmd: True})
            plc.run_for(0.5)  # 10 degC/s * 0.5s ≈ 5.0
            results[test_dt] = _fb(plc, dev[1].Fb_Temp)
        for dt_val, result in results.items():
            assert result == pytest.approx(5.0, rel=0.25), f"dt={dt_val} gave {result}"


class TestPulseSpec:
    """A declarative ``Pulse`` lowers to an astable plant flasher (bool pulse train)."""

    def test_pulse_toggles_feedback_while_enabled(self):
        plc, Cmd, dev = _make_plc(EncoderDevice, dt=0.005)
        Harness(plc).install()

        plc.patch({Cmd: True})
        values = []
        for _ in range(20):
            plc.step()
            values.append(_fb(plc, dev[1].Fb_Pulse))

        # 25ms on / 25ms off at dt=5ms cycles high and low.
        assert True in values
        assert False in values

    def test_pulse_rests_low_when_disabled(self):
        plc, Cmd, dev = _make_plc(EncoderDevice, dt=0.010)
        Harness(plc).install()

        plc.run_for(0.100)  # never enabled → rests low
        assert _fb(plc, dev[1].Fb_Pulse) is False


# --- Delays longer than a millisecond timer counts ---


def _long_delay_plc(physical: Physical, *, dt: float = 0.25, default: bool = False):
    Cmd = Bool("Cmd", default=default)
    En = Bool("En")
    Fb = Bool("Fb", physical=physical, link="En")
    Seen = Bool("Seen")
    with Program(strict=False) as logic:
        with Rung(Cmd):
            out(En)
        with Rung(Fb):
            out(Seen)
    plc = PLC(logic, dt=dt)
    Harness(plc).install()
    return plc, Cmd, Fb


def _whole_state(plc) -> tuple:
    s = plc.current_state
    return s.scan_id, s.timestamp, dict(s.tags), dict(s.memory)


class TestDelaysPastTheMillisecondRange:
    """A plant timer's accumulator stops at 32767, 32.767 s in milliseconds."""

    @pytest.mark.parametrize(
        ("delay_ms", "expected"),
        [
            (0, (0, "Tms", 1)),
            (32_767, (32_767, "Tms", 1)),
            (32_768, (33, "Ts", 1_000)),
            (60_000, (60, "Ts", 1_000)),
            (32_767_000, (32_767, "Ts", 1_000)),
            (32_767_001, (547, "Tm", 60_000)),
            (86_400_000, (1_440, "Tm", 60_000)),
            (32_767 * 60_000 + 1, (547, "Th", 3_600_000)),
            (32_767 * 3_600_000 + 1, (1_366, "Td", 86_400_000)),
            (32_767 * 86_400_000, (32_767, "Td", 86_400_000)),
        ],
    )
    def test_the_finest_unit_that_can_count_the_delay(self, delay_ms, expected):
        assert plant_timer(delay_ms) == expected

    def test_a_delay_no_timer_can_count_says_so(self):
        with pytest.raises(ValueError, match="longer than a timer can count"):
            plant_timer(32_767 * 86_400_000 + 1)

    @pytest.mark.parametrize("delay", ["30s", "33s", "60s", "10min"])
    def test_on_delay_rises(self, delay):
        physical = Physical("Slow", on_delay=delay)
        plc, Cmd, Fb = _long_delay_plc(physical, dt=0.25)
        plc.patch({Cmd: True})
        # One scan to command the enable, then ceil(delay / dt) held scans: the count
        # a millisecond timer gives a delay it can hold (30 s here).
        on_delay_ms = physical.on_delay_ms
        assert on_delay_ms is not None
        expected = on_delay_ms // 250 + 1
        plc.run(expected - 1)
        assert _fb(plc, Fb) is False
        plc.step()
        assert _fb(plc, Fb) is True

    def test_off_delay_holds_then_drops(self):
        plc, Cmd, Fb = _long_delay_plc(Physical("Slow", off_delay="60s"), dt=0.25)
        plc.patch({Cmd: True})
        plc.run(5)
        assert _fb(plc, Fb) is True
        plc.force(Cmd, False)
        plc.run(238)
        assert _fb(plc, Fb) is True
        plc.run(5)
        assert _fb(plc, Fb) is False

    def test_a_long_pulse_flips_both_ways(self):
        physical = Physical("Flow", profile=Pulse(on_dwell="60s", off_dwell="40s"))
        plc, Cmd, Fb = _long_delay_plc(physical, dt=0.25)
        plc.patch({Cmd: True})
        flips = []
        last = _fb(plc, Fb)
        for _ in range(1_000):
            plc.step()
            value = _fb(plc, Fb)
            if value != last:
                flips.append((plc.current_state.scan_id, value))
                last = value
        assert [value for _scan, value in flips][:4] == [True, False, True, False]
        gaps = [later - earlier for (earlier, _), (later, _) in zip(flips, flips[1:], strict=False)]
        # 60 s high and 40 s low at 4 scans a second, give or take the scan a pair of
        # timers takes to hand over.
        assert gaps[0] == pytest.approx(240, abs=2)
        assert gaps[1] == pytest.approx(160, abs=2)

    def test_a_delay_in_the_millisecond_range_keeps_its_timer(self):
        plc, _Cmd, _Fb = _long_delay_plc(Physical("Fast", on_delay="32s500ms"))
        ton = plc._synthesis.plant[0]._instructions[0]
        assert (ton.preset, ton.unit.name) == (32_500, "Tms")

    def test_pending_ms_counts_down_past_the_millisecond_range(self):
        plc, Cmd, Fb = _long_delay_plc(Physical("Slow", on_delay="60s"), dt=0.25)
        harness = plc._harness
        plc.patch({Cmd: True})
        plc.run(5)
        # One scan to command the enable, then each held scan counts a quarter second,
        # most of which sits in the timer's fraction, not its whole seconds.
        assert harness.pending_ms()[Fb.name] == 60_000 - 4 * 250
        plc.run(40)
        assert harness.pending_ms()[Fb.name] == 60_000 - 44 * 250
        plc.run(1)
        assert harness.pending_ms()[Fb.name] == 60_000 - 45 * 250

    def test_pending_ms_counts_a_falling_delay_the_same(self):
        plc, Cmd, Fb = _long_delay_plc(Physical("Slow", off_delay="60s"), dt=0.25)
        harness = plc._harness
        plc.patch({Cmd: True})
        plc.run(4)
        plc.force(Cmd, False)
        plc.run(3)
        first = harness.pending_ms()[Fb.name]
        plc.run(3)
        assert harness.pending_ms()[Fb.name] == first - 3 * 250

    def test_pending_ms_is_when_the_timer_will_flip(self):
        # 32.768 s counts in whole seconds, so the flip comes at 33 s.
        plc, Cmd, Fb = _long_delay_plc(Physical("Slow", on_delay="32768ms"), dt=0.25)
        harness = plc._harness
        plc.patch({Cmd: True})
        plc.run(2)
        assert harness.pending_ms()[Fb.name] == 33_000 - 250

    def test_an_enable_already_on_at_install_settles_without_dropping(self):
        En = Bool("En", default=True)
        Fb = Bool("Fb", physical=Physical("Slow", on_delay="60s"), link="En")
        with Program(strict=False) as logic:
            with Rung(En):
                out(Bool("Echo"))
            with Rung(Fb):
                out(Bool("Seen"))
        plc = PLC(logic, dt=0.25)
        Harness(plc).install()
        plc.run(10)
        assert _fb(plc, Fb) is True

    @pytest.mark.parametrize(
        ("physical", "scans"),
        [
            (Physical("Slow", on_delay="60s"), 300),
            (Physical("Slow", on_delay="60s", off_delay="60s"), 300),
            (Physical("Flow", profile=Pulse(on_dwell="60s", off_dwell="40s")), 700),
        ],
        ids=["on", "on-off", "pulse"],
    )
    @pytest.mark.parametrize("dt", [0.25, 0.1], ids=["binary-dt", "decimal-dt"])
    def test_a_long_run_equals_stepping(self, physical, scans, dt):
        stepped, stepped_cmd, _ = _long_delay_plc(physical, dt=dt)
        stepped.patch({stepped_cmd: True})
        for _ in range(scans):
            stepped.step()

        run, run_cmd, _ = _long_delay_plc(physical, dt=dt)
        run.patch({run_cmd: True})
        run.run(scans)

        assert _whole_state(run) == _whole_state(stepped)

    @pytest.mark.parametrize("dt", [0.25, 0.1], ids=["binary-dt", "decimal-dt"])
    def test_run_until_lands_on_the_stepped_flip(self, dt):
        physical = Physical("Slow", on_delay="60s")
        stepped, stepped_cmd, stepped_fb = _long_delay_plc(physical, dt=dt)
        stepped.patch({stepped_cmd: True})
        for _ in range(2_000):
            stepped.step()
            if _fb(stepped, stepped_fb):
                break
        assert _fb(stepped, stepped_fb) is True

        run, run_cmd, run_fb = _long_delay_plc(physical, dt=dt)
        run.patch({run_cmd: True})
        run.run_until(run_fb, max_cycles=2_000)

        assert _whole_state(run) == _whole_state(stepped)


class TestALinkChangedAcrossUnits:
    """A link changed mid-run keeps the time counted, whatever unit either delay counts in."""

    @staticmethod
    def _linked(on_delay: str, *, dt: float = 0.25, default: bool = False):
        En = Bool("En", default=default)
        Fb = Bool("Fb", physical=Physical("S", on_delay=on_delay), link="En")
        with Program(strict=False) as logic:
            with Rung(En):
                out(Bool("Echo"))
            with Rung(Fb):
                out(Bool("Seen"))
        plc = PLC(logic, dt=dt)
        Harness(plc).install()
        return plc, En, Fb

    @staticmethod
    def _relink(plc, **physical) -> None:
        plc._harness.set_overrides({"Fb": Coupling("En", "Fb", Physical("S", **physical), None)})

    @staticmethod
    def _scans_to_flip(plc, Fb, budget: int = 1_000) -> int | None:
        start = _fb(plc, Fb)
        for i in range(budget):
            plc.step()
            if _fb(plc, Fb) != start:
                return i + 1
        return None

    @pytest.mark.parametrize(
        ("first", "then", "left_s"),
        [
            ("5s", "10s", 7.25),  # same unit, the control
            ("5s", "60s", 57.25),
            ("40s", "60s", 57.25),  # same unit, the control
            ("40s", "5s", 2.25),
        ],
    )
    def test_mid_count_the_time_counted_carries(self, first, then, left_s):
        plc, En, Fb = self._linked(first)
        plc.patch({En: True})
        plc.run(12)  # one to command the enable, 11 held: 2.75 s counted
        assert _fb(plc, Fb) is False
        self._relink(plc, on_delay=then)
        assert self._scans_to_flip(plc, Fb) == left_s * 4

    @pytest.mark.parametrize(
        ("first", "then", "holds"),
        [
            ("10s", "5s", True),  # same unit, the control
            ("40s", "5s", True),
            ("60s", "5s", True),
            ("60s", "40s", True),  # same unit, the control
            ("5s", "10s", False),  # same unit, the control
            ("5s", "60s", False),
            ("5s", "40s", False),
            ("40s", "60s", False),  # same unit, the control
        ],
    )
    def test_settled_feedback_holds_only_if_it_has_counted_the_new_delay(self, first, then, holds):
        plc, _En, Fb = self._linked(first, default=True)
        plc.run(4)
        assert _fb(plc, Fb) is True
        self._relink(plc, on_delay=then)
        plc.run(2)
        assert _fb(plc, Fb) is holds

    def test_a_fresh_harness_given_the_same_links_keeps_an_off_delay_counting(self):
        """How how()'s fork rebuilds the plant: an empty old accumulator gives nothing."""
        plc, En, Fb = self._linked("0ms")
        plc.patch({En: True})
        plc.run(3)
        self._relink(plc, off_delay="60s")
        plc.run(1)
        plc.force(En, False)
        plc.run(13)
        assert _fb(plc, Fb) is True
        before = _whole_state(plc)
        assert plc.current_state.tags["__cpl_off_Ts__Fb"] == 3

        overrides = plc._harness.overrides
        fresh = Harness(plc)
        fresh.install()
        fresh.set_overrides(overrides)

        assert _whole_state(plc) == before

    def test_there_and_back_leaves_no_stale_count(self):
        plc, En, Fb = self._linked("5s", default=True)
        plc.run(4)
        self._relink(plc, on_delay="60s")  # counts 5 s + 1 s in seconds
        plc.run(4)
        plc.force(En, False)
        plc.run(2)  # the seconds timer drops to 0 with its enable
        plc.unforce(En)
        self._relink(plc, on_delay="5s")
        assert plc.current_state.tags["__cpl_on__Fb"] == 0
        assert plc.current_state.tags["__cpl_on_Ts__Fb"] == 0
        plc.run(2)
        assert _fb(plc, Fb) is False

    def test_an_off_delay_carries(self):
        plc, En, Fb = self._linked("0ms")
        plc.patch({En: True})
        plc.run(3)
        self._relink(plc, off_delay="5s")
        plc.run(1)
        plc.force(En, False)
        plc.run(13)  # 3 s into the off-delay
        assert _fb(plc, Fb) is True
        self._relink(plc, off_delay="60s")
        assert self._scans_to_flip(plc, Fb) == pytest.approx(57 * 4, abs=2)

    def test_a_pulse_dwell_carries(self):
        En = Bool("En")
        Fb = Bool("Fb", physical=Physical("F", profile=Pulse("5s", "5s")), link="En")
        with Program(strict=False) as logic:
            with Rung(En):
                out(Bool("Echo"))
            with Rung(Fb):
                out(Bool("Seen"))
        plc = PLC(logic, dt=0.25)
        Harness(plc).install()
        plc.patch({En: True})
        plc.run(5)  # part way through the first low dwell
        assert _fb(plc, Fb) is False
        count = plc.current_state.tags["__pls_off__Fb"]
        assert count > 0
        self._relink(plc, profile=Pulse("60s", "60s"))
        assert self._scans_to_flip(plc, Fb) == pytest.approx((60_000 - count) // 250, abs=2)

    def test_a_delay_staying_in_its_unit_writes_nothing(self):
        plc, En, _Fb = self._linked("5s")
        plc.patch({En: True})
        plc.run(5)
        before = _whole_state(plc)
        self._relink(plc, on_delay="10s", off_delay="20s")
        assert _whole_state(plc) == before

    @pytest.mark.parametrize("dt", [0.25, 0.1], ids=["binary-dt", "decimal-dt"])
    def test_runs_history_and_forks_equal_stepping_after_the_change(self, dt):
        def changed(scans_after: int):
            plc, En, Fb = self._linked("5s", dt=dt)
            plc.patch({En: True})
            plc.run(12)
            self._relink(plc, on_delay="60s")
            for _ in range(scans_after):
                plc.step()
            return plc

        changed_at = changed(0).current_state.scan_id
        stepped = changed(700)
        ran = changed(0)
        ran.run(700)
        assert _whole_state(ran) == _whole_state(stepped)
        assert ran.current_state.tags["Fb"] is True

        reference = changed(100)
        at = stepped.history.at(changed_at + 100)
        assert dict(at.tags) == dict(reference.current_state.tags)
        assert dict(at.memory) == dict(reference.current_state.memory)
        at_change = stepped.history.at(changed_at)
        assert dict(at_change.tags) == dict(changed(0).current_state.tags)

        fork = stepped.fork(scan_id=changed_at + 100)
        fork.run(50)
        twin = changed(150)
        assert dict(fork.current_state.tags) == dict(twin.current_state.tags)
        assert dict(fork.current_state.memory) == dict(twin.current_state.memory)

    def test_a_fork_from_before_the_change_does_not_misread_the_count(self):
        plc, En, Fb = self._linked("5s")
        plc.patch({En: True})
        plc.run(12)
        before = plc.current_state.scan_id
        self._relink(plc, on_delay="60s")
        plc.run(5)

        stepped = plc.fork(scan_id=before)
        ran = plc.fork(scan_id=before)
        for _ in range(100):
            stepped.step()
        ran.run(100)
        assert _whole_state(ran)[2:] == _whole_state(stepped)[2:]
        assert _fb(stepped, Fb) is False


class TestHowAroundALinkChange:
    """how() and the replay it reads take the scans before a change as they ran."""

    @staticmethod
    def _program(on_delay: str):
        En = Bool("En", default=True)
        Fb = Bool("Fb", physical=Physical("S", on_delay=on_delay), link="En")
        Alarm = Bool("Alarm")
        with Program(strict=False) as logic:
            with Rung(En):
                out(Bool("Echo"))
            with Rung(En, ~Fb):
                latch(Alarm)
        plc = PLC(logic, dt=0.25)
        Harness(plc).install()
        plc.run(4)
        return plc, Alarm

    @staticmethod
    def _relink(plc, on_delay: str) -> None:
        plc._harness.set_overrides(
            {"Fb": Coupling("En", "Fb", Physical("S", on_delay=on_delay), None)}
        )

    @pytest.mark.parametrize(
        ("first", "then"), [("5s", "10s"), ("40s", "5s"), ("5s", "40s"), ("60s", "40s")]
    )
    def test_a_scan_before_the_change_replays_on_the_plant_it_ran(self, first, then):
        plc, _Alarm = self._program(first)
        scan = plc.current_state.scan_id
        self._relink(plc, then)

        projection = plc._replay_rung_write_projection_at(scan)

        assert projection.exit_tags["Fb"] is True
        assert projection.exit_tags["Alarm"] is False

    @pytest.mark.parametrize(
        ("first", "then"),
        [
            ("5s", "5s"),
            ("10s", "5s"),
            ("32s", "5s"),
            ("40s", "5s"),
            ("60s", "5s"),
            ("60s", "40s"),
            ("5s", "10s"),
            ("5s", "20s"),
            ("10s", "20s"),
            ("5s", "32s"),
            ("5s", "40s"),
            ("5s", "60s"),
            ("40s", "60s"),
        ],
    )
    def test_how_answers_under_a_changed_link(self, first, then):
        plc, Alarm = self._program(first)
        self._relink(plc, then)

        plan = plc.how(Alarm, wall_clock_s=30, max_scans=2_000)

        assert plan.status is PlanStatus.REACHED

    def test_how_answers_when_the_scan_it_starts_from_made_the_target(self):
        plc, Alarm = self._program("5s")
        self._relink(plc, "10s")
        plc.step()
        assert plc.current_state.tags["Alarm"] is True

        plan = plc.how(Alarm, wall_clock_s=30, max_scans=2_000)

        assert plan.status is PlanStatus.REACHED
        assert plan.total_scans == 0


class TestPlantOnASlotOnlyAPointerReaches:
    """The plant reads and writes the program's slot, with the slot's own default."""

    @staticmethod
    def _approach_program():
        En = Bool("En")
        Ptr = Int("Ptr", default=2)
        Sps = Block("Sps", TagType.REAL, 1, 3)
        Sps.slot(2, default=100.0)
        Temp = Real(
            "Temp",
            default=20.0,
            physical=Physical("T", profile=Approach(toward="Sps2", rate=0.3)),
            link="En",
        )
        CopySp = Real("CopySp")
        with Program(strict=False) as logic:
            with Rung(En):
                out(Bool("Echo"))
            with Rung(Temp > 1000.0):
                out(Bool("Seen"))
            with Rung():
                copy(Sps[Ptr], CopySp)
        return logic, En

    def test_approach_runs_toward_the_slots_default(self):
        logic, En = self._approach_program()
        plc = PLC(logic, dt=0.01)
        Harness(plc).install()
        plc.patch({En: True})
        plc.run(500)
        tags = plc.current_state.tags
        assert tags["Sps2"] == 100.0
        assert tags["CopySp"] == 100.0
        assert tags["Temp"] == pytest.approx(100.0 - 80.0 * (1 - 0.3 * 0.01) ** 499, rel=1e-3)

    def test_approach_equals_stepping(self):
        def started():
            logic, En = self._approach_program()
            plc = PLC(logic, dt=0.01)
            Harness(plc).install()
            plc.patch({En: True})
            return plc

        stepped = started()
        for _ in range(500):
            stepped.step()
        ran = started()
        ran.run(500)
        assert _whole_state(ran) == _whole_state(stepped)

    @staticmethod
    def _ambient_program():
        """A setpoint no rung names: an ambient temperature a test may patch."""
        En = Bool("En")
        Temp = Real(
            "Temp",
            default=20.0,
            physical=Physical("T", profile=Approach(toward="Sp", rate=0.3)),
            link="En",
        )
        Lamp = Bool("Lamp")
        with Program(strict=False) as logic:
            with Rung(En):
                latch(Lamp)
            with Rung(Temp > 1000.0):
                out(Bool("Seen"))
        return logic, En, Lamp

    # Temp after 500 scans of dt 0.01 toward a setpoint, from 20.0: the first only arms it.
    @staticmethod
    def _temp_toward(setpoint: float) -> float:
        return setpoint - (setpoint - 20.0) * (1 - 0.3 * 0.01) ** 499

    def test_a_setpoint_nobody_knows_warns_at_install_and_runs_from_zero(self):
        logic, En, _Lamp = self._ambient_program()
        plc = PLC(logic, dt=0.01)
        harness = Harness(plc)
        with pytest.warns(UserWarning, match="Sp, which no rung names: it starts from 0") as seen:
            harness.install()
        assert str(seen[0].message).isascii()
        assert plc._harness is harness
        assert plc.current_state.tags["Sp"] == 0.0
        plc.patch({En: True})
        plc.run(500)
        assert plc.current_state.tags["Temp"] == pytest.approx(self._temp_toward(0.0), rel=1e-3)

    @pytest.mark.parametrize("when", ["before install", "after install"])
    def test_a_setpoint_no_rung_names_can_be_patched_by_name(self, when, recwarn):
        logic, En, _Lamp = self._ambient_program()
        plc = PLC(logic, dt=0.01)
        if when == "before install":
            plc.patch({"Sp": 150.0})
        Harness(plc).install()
        if when == "after install":
            plc.patch({"Sp": 150.0})
        plc.patch({En: True})
        plc.run(500)
        assert plc.current_state.tags["Temp"] == pytest.approx(self._temp_toward(150.0), rel=1e-3)

    def test_a_setpoint_already_in_state_is_known_and_keeps_its_value(self, recwarn):
        logic, En, _Lamp = self._ambient_program()
        plc = PLC(logic, dt=0.01)
        plc.patch({"Sp": 150.0})
        plc.step()
        Harness(plc).install()
        assert [w for w in recwarn if "no rung names" in str(w.message)] == []
        assert plc.current_state.tags["Sp"] == 150.0
        plc.patch({En: True})
        plc.run(500)
        assert plc.current_state.tags["Temp"] == pytest.approx(self._temp_toward(150.0), rel=1e-3)

    def test_how_on_an_unrelated_target_runs_with_such_a_setpoint(self):
        logic, _En, Lamp = self._ambient_program()
        plc = PLC(logic, dt=0.01)
        with pytest.warns(UserWarning, match="no rung names"):
            plan = plc.how(Lamp, wall_clock_s=30, max_scans=2_000)
        assert plan.status is PlanStatus.REACHED

    def test_how_after_install_does_not_warn_again(self, recwarn):
        logic, _En, Lamp = self._ambient_program()
        plc = PLC(logic, dt=0.01)
        with pytest.warns(UserWarning, match="no rung names"):
            Harness(plc).install()
        recwarn.clear()
        plan = plc.how(Lamp, wall_clock_s=30, max_scans=2_000)
        assert plan.status is PlanStatus.REACHED
        assert [w for w in recwarn if "no rung names" in str(w.message)] == []

    def test_a_bench_link_toward_a_setpoint_no_rung_names_runs_and_warns(self):
        En, Temp = Bool("En"), Real("Temp")
        with Program(strict=False) as logic:
            with Rung(En):
                out(Bool("Echo"))
            with Rung(Temp > 1000.0):
                out(Bool("Seen"))
        plc = PLC(logic, dt=0.01)
        harness = Harness(plc)
        harness.install()
        coupling = Coupling(
            "En", "Temp", Physical("T", profile=Approach(toward="Sp", rate=0.3)), None
        )
        assert link_problem(plc, coupling) is None
        with pytest.warns(UserWarning, match="no rung names"):
            harness.set_overrides({"Temp": coupling})
        assert harness.overrides == {"Temp": coupling}

    def test_run_equals_step_toward_a_setpoint_no_rung_names(self, recwarn):
        def started():
            logic, En, _Lamp = self._ambient_program()
            plc = PLC(logic, dt=0.01)
            plc.patch({"Sp": 150.0})
            Harness(plc).install()
            plc.patch({En: True})
            return plc

        stepped = started()
        for _ in range(300):
            stepped.step()
        ran = started()
        ran.run(300)
        assert _whole_state(ran) == _whole_state(stepped)

    def test_a_setpoint_only_a_pointer_reaches_passes_link_problem(self):
        logic, _En = self._approach_program()
        plc = PLC(logic, dt=0.01)
        coupling = Coupling(
            "En", "Temp", Physical("T", profile=Approach(toward="Sps2", rate=0.3)), None
        )
        assert link_problem(plc, coupling) is None

    def test_a_bench_ramp_starts_from_the_slots_default(self):
        En = Bool("En")
        Ptr = Int("Ptr", default=4)
        Temps = Block("Temps", TagType.REAL, 1, 5)
        Temps.slot(4, default=20.0)
        Seen = Real("Seen")
        with Program(strict=False) as logic:
            with Rung(En):
                out(Bool("Echo"))
            with Rung():
                copy(Temps[Ptr], Seen)
        plc = PLC(logic, dt=0.01)
        harness = Harness(plc)
        harness.install()
        plc.run(5)
        assert plc.current_state.tags["Seen"] == 20.0
        harness.set_overrides(
            {"Temps4": Coupling("En", "Temps4", Physical("T", profile=Ramp(up=1.0)), None)}
        )
        assert plc.current_state.tags["Temps4"] == 20.0
        plc.run(5)
        assert plc.current_state.tags["Seen"] == 20.0
        plc.patch({En: True})
        plc.run(101)
        assert plc.current_state.tags["Temps4"] == pytest.approx(21.0, abs=0.02)

    def test_a_bench_bool_link_keeps_the_slots_default(self):
        En = Bool("En", default=False)
        Ptr = Int("Ptr", default=2)
        Bits = Block("Bits", TagType.BOOL, 1, 3)
        Bits.slot(2, default=True)
        Seen = Bool("Seen")
        with Program(strict=False) as logic:
            with Rung(En):
                out(Bool("Echo"))
            with Rung(Bits[Ptr] == True):  # noqa: E712
                out(Seen)
        plc = PLC(logic, dt=0.01)
        harness = Harness(plc)
        harness.install()
        plc.run(2)
        assert plc.current_state.tags["Seen"] is True
        link = Coupling("En", "Bits2", Physical("S", on_delay="10ms"), None)
        harness.set_overrides({"Bits2": link})
        assert plc.current_state.tags["Bits2"] is True


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
        plc.step()  # scan 1: State commits SORTING; plant hasn't seen it yet
        assert _fb(plc, dev[1].Fb) is False

        plc.step()  # scan 2: plant sees SORTING, dwell begins
        assert _fb(plc, dev[1].Fb) is False

        plc.step()  # scan 3: Fb arrives (on_delay=20ms, dt=10ms → 2 dt + 1 read)
        assert _fb(plc, dev[1].Fb) is True

    def test_fb_falls_on_trigger_leave(self):
        plc, dev = _make_trigger_plc(IntTriggerPair)
        harness = Harness(plc)
        harness.install()

        plc.patch({dev[1].State: 2})
        plc.run_for(0.050)
        assert _fb(plc, dev[1].Fb) is True

        plc.patch({dev[1].State: 0})
        plc.step()  # off-edge commits; the plant still saw SORTING → Fb holds
        assert _fb(plc, dev[1].Fb) is True

        plc.step()  # plant sees the leave; off_delay=10ms (1 dt) clears Fb
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
    def test_analog_activates_on_trigger_match(self):
        plc, dev = _make_trigger_plc(IntTriggerAnalog)
        Harness(plc).install()

        plc.patch({dev[1].State: 2})  # SORTING — the trigger value
        plc.run_for(0.100)
        fb_temp = plc.current_state.tags.get(dev[1].Fb_Temp.name, 0.0)
        assert fb_temp > 0.5

    def test_analog_decays_on_trigger_mismatch(self):
        plc, dev = _make_trigger_plc(IntTriggerAnalog)
        Harness(plc).install()

        plc.patch({dev[1].State: 2})  # match → ramp up
        plc.run_for(0.100)
        peak = plc.current_state.tags.get(dev[1].Fb_Temp.name, 0.0)
        assert peak > 0

        plc.patch({dev[1].State: 0})  # mismatch → disabled → decays
        plc.run_for(0.100)
        after = plc.current_state.tags.get(dev[1].Fb_Temp.name, 0.0)
        assert after < peak


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


# --- Normally-Closed (~) Links ---


@udt()
class NormallyClosedPair:
    En: Bool
    Fb: Bool = Field(physical=LIMIT_SWITCH, link="~En")


class TestNormallyClosedLink:
    def test_fb_follows_the_bit_while_it_is_off(self):
        plc, dev = _make_trigger_plc(NormallyClosedPair, en_field="En")
        harness = Harness(plc)
        harness.install()

        plc.run_for(0.050)
        assert _fb(plc, dev[1].Fb) is True

        plc.patch({dev[1].En: True})
        plc.run_for(0.050)
        assert _fb(plc, dev[1].Fb) is False

    def test_coupling_triggers_on_zero(self):
        plc, dev = _make_trigger_plc(NormallyClosedPair, en_field="En")
        harness = Harness(plc)
        harness.install()

        (coupling,) = harness.couplings()
        assert coupling.trigger_value == 0

    def test_flat_tag(self):
        Sol = Bool("VentSol")
        Open = Bool("VentOpen", physical=LIMIT_SWITCH, link="~VentSol")

        with Program() as logic:
            pass
        plc = PLC(logic, dt=0.010)
        plc._register_known_tag(Sol)
        plc._register_known_tag(Open)
        Harness(plc).install()

        plc.run_for(0.050)
        assert _fb(plc, Open) is True
        plc.patch({Sol: True})
        plc.run_for(0.050)
        assert _fb(plc, Open) is False

    def test_on_a_non_bool_field_raises(self):
        with pytest.raises(ValueError, match="normally-closed BOOL enable"):

            @udt()
            class Bad:
                State: Int
                Fb: Bool = Field(physical=LIMIT_SWITCH, link="~State")

    def test_on_a_non_bool_flat_tag_raises(self):
        State = Int("NcState")
        Fb = Bool("NcFb", physical=LIMIT_SWITCH, link="~NcState")

        with Program() as logic:
            pass
        plc = PLC(logic, dt=0.010)
        plc._register_known_tag(State)
        plc._register_known_tag(Fb)
        with pytest.raises(ValueError, match="write NcState:0"):
            Harness(plc).install()

    def test_cannot_combine_with_a_trigger(self):
        with pytest.raises(ValueError, match="cannot combine ~"):
            _parse_link_spec("~En:1")

    def test_format_is_canonical(self):
        assert format_link_spec("En", None, bit=True) == "En"
        assert format_link_spec("En", 0, bit=True) == "~En"
        assert format_link_spec("Mode", 0, bit=False) == "Mode:0"
        assert format_link_spec("Mode", "BAKE", bit=False) == "Mode:BAKE"


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
