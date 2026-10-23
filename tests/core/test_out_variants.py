"""Tests for the CLICK v4 Out coil variants: toggle, debounce, flasher, timed.

The probes O1-O5 (scratchpad/click-v4/RESEARCH.md) are not yet run on a CLICK;
these tests pin pyrung's answers.
"""

import pytest

from pyrung.core import (
    Bool,
    Int,
    Program,
    Rung,
    Timer,
    debounce,
    flasher,
    immediate,
    timed,
    toggle,
)

T = Timer.clone("OT", count=8)
In = Bool("In")


def _trace(runner, inputs, *names):
    rows = []
    for value in inputs:
        runner.patch({In: bool(value)})
        runner.step()
        tags = runner.current_state.tags
        rows.append(tuple(tags.get(name) for name in names))
    return rows


class TestToggle:
    def test_flips_on_each_rising_edge_and_holds_while_false(self, runner_factory):
        Light = Bool("Light")
        with Program() as logic:
            with Rung(In):
                toggle(Light)

        runner = runner_factory(logic, dt=0.010)
        rows = _trace(runner, [0, 1, 1, 0, 0, 1, 0, 1, 1], "Light")
        assert [r[0] for r in rows] == [
            False,
            True,
            True,
            True,
            True,
            False,
            False,
            True,
            True,
        ]

    def test_rung_true_on_first_scan_toggles(self, runner_factory):
        """O1, pyrung's answer: the previous rung state starts false."""
        Light = Bool("Light")
        with Program() as logic:
            with Rung(In):
                toggle(Light)

        runner = runner_factory(logic, dt=0.010)
        assert _trace(runner, [1], "Light") == [(True,)]

    def test_immediate_target(self, runner_factory):
        Light = Bool("Light")
        with Program() as logic:
            with Rung(In):
                toggle(immediate(Light))

        runner = runner_factory(logic, dt=0.010)
        assert _trace(runner, [1, 0, 1], "Light") == [(True,), (True,), (False,)]

    def test_rejects_non_bool_and_ranges(self):
        Count = Int("Count")
        with Program():
            with Rung(In):
                with pytest.raises(TypeError, match="one Bool tag"):
                    toggle(Count)


@pytest.mark.parametrize(
    "build",
    [
        lambda lamp: debounce(immediate(lamp), on=(T[1], 10), off=(T[2], 10)),
        lambda lamp: flasher(immediate(lamp), on=(T[1], 10), off=(T[2], 10)),
        lambda lamp: timed(immediate(lamp), T[1], 10),
    ],
    ids=["debounce", "flasher", "timed"],
)
def test_only_toggle_takes_an_immediate_target(build):
    lamp = Bool("Lamp")
    with Program():
        with Rung(In):
            with pytest.raises(TypeError, match="takes no immediate"):
                build(lamp)


class TestDebounce:
    def test_each_side_times_like_on_delay(self, runner_factory):
        Valve = Bool("Valve")
        with Program() as logic:
            with Rung(In):
                debounce(Valve, on=(T[1], 30), off=(T[2], 20))

        runner = runner_factory(logic, dt=0.010)
        rows = _trace(runner, [1, 1, 1, 1, 0, 0, 1], "Valve", "OT1_Acc", "OT1_Done", "OT2_Acc")
        assert rows == [
            (False, 10, False, 0),
            (False, 20, False, 0),
            (True, 30, True, 0),
            (True, 40, True, 0),  # O2: the TD keeps counting, as on_delay's does
            (True, 0, False, 10),
            (False, 0, False, 20),
            (False, 10, False, 0),
        ]

    def test_zero_presets_follow_the_rung(self, runner_factory):
        Valve = Bool("Valve")
        with Program() as logic:
            with Rung(In):
                debounce(Valve, on=(T[1], 0), off=(T[2], 0))

        runner = runner_factory(logic, dt=0.010)
        rows = _trace(runner, [1, 0, 1, 1, 0], "Valve")
        assert [r[0] for r in rows] == [True, False, True, True, False]

    def test_both_sides_a_bounce_resets_the_side_timing(self, runner_factory):
        """O3, pyrung's answer: the side that was timing starts over."""
        Valve = Bool("Valve")
        with Program() as logic:
            with Rung(In):
                debounce(Valve, on=(T[1], 30), off=(T[2], 30))

        runner = runner_factory(logic, dt=0.010)
        rows = _trace(runner, [1, 1, 0, 1, 1, 1, 0, 0, 1, 0, 0, 0], "Valve")
        assert [r[0] for r in rows] == [
            False,
            False,
            False,  # bounce: the on side restarts
            False,
            False,
            True,
            True,
            True,
            True,  # bounce back on: the off side restarts, the output holds
            True,
            True,
            False,
        ]

    def test_needs_both_sides(self):
        """CLICK's Debounce dialog requires an on and an off side."""
        Valve = Bool("Valve")
        with Program():
            with Rung(In):
                with pytest.raises(TypeError, match="off"):
                    debounce(Valve, on=(T[1], 10))  # ty: ignore[missing-argument]

    def test_half_written_phase_fails_at_call_time(self):
        Valve = Bool("Valve")
        with Program():
            with Rung(In):
                with pytest.raises(TypeError, match=r"on= takes \(timer, preset\)"):
                    debounce(Valve, on=T[1], off=(T[2], 10))  # type: ignore[arg-type]

    def test_sides_need_two_timers(self):
        Valve = Bool("Valve")
        with Program():
            with Rung(In):
                with pytest.raises(ValueError, match="two different timers"):
                    debounce(Valve, on=(T[1], 10), off=(T[1], 10))


class TestFlasher:
    def test_alternates_starting_on_and_clears_when_the_rung_drops(self, runner_factory):
        Beacon = Bool("Beacon")
        with Program() as logic:
            with Rung(In):
                flasher(Beacon, on=(T[3], 30), off=(T[4], 20))

        runner = runner_factory(logic, dt=0.010)
        rows = _trace(
            runner,
            [1] * 11 + [0, 1],
            "Beacon",
            "OT3_Done",
            "OT3_Acc",
            "OT4_Done",
            "OT4_Acc",
        )
        assert rows == [
            # The first on phase counts the rung's first scan, as on_delay does.
            (True, False, 10, False, 0),
            (True, False, 20, False, 0),
            (False, True, 30, False, 0),
            (False, True, 30, False, 10),
            (True, False, 0, True, 20),
            (True, False, 10, True, 20),
            (True, False, 20, True, 20),
            (False, True, 30, False, 0),
            (False, True, 30, False, 10),
            (True, False, 0, True, 20),
            (True, False, 10, True, 20),
            (False, False, 0, False, 0),  # O4: both timers clear
            (True, False, 10, False, 0),  # a full on phase again
        ]

    def test_needs_both_phases(self):
        Beacon = Bool("Beacon")
        with Program():
            with Rung(In):
                with pytest.raises(TypeError):
                    flasher(Beacon, on=(T[3], 30))  # ty: ignore[missing-argument]

    def test_seconds_unit_keeps_its_remainder(self, runner_factory):
        Beacon = Bool("Beacon")
        with Program() as logic:
            with Rung(In):
                flasher(Beacon, on=(T[3], 1), off=(T[4], 1), unit="sec")

        runner = runner_factory(logic, dt=0.250)
        rows = _trace(runner, [1] * 12, "Beacon")
        assert [r[0] for r in rows] == [True] * 3 + [False] * 4 + [True] * 4 + [False]


class TestShortPhases:
    """A phase no longer than a scan still shows for one scan."""

    def test_flasher_starts_on_with_an_on_time_under_a_scan(self, runner_factory):
        Beacon = Bool("Beacon")
        with Program() as logic:
            with Rung(In):
                flasher(Beacon, on=(T[3], 5), off=(T[4], 30))

        runner = runner_factory(logic, dt=0.010)
        rows = _trace(runner, [1] * 8, "Beacon")
        assert [r[0] for r in rows] == [True, False, False, True, False, False, False, True]

    @pytest.mark.parametrize("maintained", [False, True])
    def test_timed_pulse_under_a_scan_shows_once(self, runner_factory, maintained):
        Horn = Bool("Horn")
        with Program() as logic:
            with Rung(In):
                timed(Horn, T[5], 10, maintained=maintained)

        runner = runner_factory(logic, dt=0.010)
        rows = _trace(runner, [1, 0, 0, 1, 1], "Horn", "OT5_Done")
        assert rows == [
            (True, True),
            (False, False),
            (False, False),
            (True, True),
            (maintained, True),
        ]


class TestTimed:
    def test_pulse_ignores_the_rung_once_started(self, runner_factory):
        Horn = Bool("Horn")
        with Program() as logic:
            with Rung(In):
                timed(Horn, T[5], 40)

        runner = runner_factory(logic, dt=0.010)
        rows = _trace(runner, [1, 0, 1, 0, 0, 0, 0], "Horn", "OT5_Done", "OT5_Acc")
        assert rows == [
            (True, False, 10),
            (True, False, 20),
            (True, False, 30),  # O5: an edge during the on time does not restart it
            (False, True, 40),
            (False, False, 0),
            (False, False, 0),
            (False, False, 0),
        ]

    def test_not_maintained_needs_a_fresh_edge(self, runner_factory):
        Horn = Bool("Horn")
        with Program() as logic:
            with Rung(In):
                timed(Horn, T[5], 20)

        runner = runner_factory(logic, dt=0.010)
        rows = _trace(runner, [1, 1, 1, 1, 0, 1], "Horn", "OT5_Done")
        assert rows == [
            (True, False),
            (False, True),
            (False, True),
            (False, True),
            (False, False),
            (True, False),
        ]

    def test_maintained_follows_the_rung_after_the_on_time(self, runner_factory):
        Lamp = Bool("Lamp")
        with Program() as logic:
            with Rung(In):
                timed(Lamp, T[6], 30, maintained=True)

        runner = runner_factory(logic, dt=0.010)
        rows = _trace(runner, [1, 0, 0, 0, 1, 1, 1, 1, 0], "Lamp")
        assert [r[0] for r in rows] == [
            True,
            True,  # on for the on time though the rung dropped
            False,
            False,
            True,
            True,
            True,
            True,  # then it follows the rung
            False,
        ]

    def test_tag_preset(self, runner_factory):
        Horn = Bool("Horn")
        OnTime = Int("OnTime", default=20)
        with Program() as logic:
            with Rung(In):
                timed(Horn, T[5], OnTime)

        runner = runner_factory(logic, dt=0.010)
        assert [r[0] for r in _trace(runner, [1, 1, 1], "Horn")] == [True, False, False]


def test_all_variants_in_one_rung_agree_across_backends(runner_factory):
    Light, Valve, Beacon, Horn = Bool("Light"), Bool("Valve"), Bool("Beacon"), Bool("Horn")
    with Program() as logic:
        with Rung(In):
            toggle(Light)
            debounce(Valve, on=(T[1], 25), off=(T[2], 15), unit="ms")
            flasher(Beacon, on=(T[3], 30), off=(T[4], 20))
            timed(Horn, T[5], 40, maintained=True)

    runner = runner_factory(logic, dt=0.007)
    pattern = [0, 1, 1, 1, 0, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 1, 0, 1, 1, 1]
    _trace(runner, pattern * 3, "Light")
