"""The harness runs each bool feedback as the CLICK coil the device behaves like.

``Physical(on_delay=, off_delay=)`` runs as ``debounce`` under its enable, and a ``Pulse``
as ``flasher``.  Each test here holds the harness's feedback to that literal coil in a
program, scan by scan, or to what stepping does.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from pyrung import (
    PLC,
    Bool,
    Harness,
    Physical,
    Program,
    Pulse,
    Rung,
    Timer,
    debounce,
    flasher,
    out,
)
from pyrung.core.crossing import Eq
from pyrung.core.harness import Coupling


def _pair(
    physical: Physical, coil: Any, on_ms: int, off_ms: int, dt: float = 0.010
) -> tuple[PLC, Bool]:
    """A harness feedback ``Fb`` and the literal coil ``Coil``, both driven by ``Cmd``.

    The coil's rung comes first, so it reads ``Cmd`` as the last scan left it, as the
    plant does.
    """
    Btn = Bool("Btn", external=True)
    Cmd = Bool("Cmd")
    Fb = Bool("Fb", physical=physical, link="Cmd")
    Coil = Bool("Coil")
    on_timer, off_timer = Timer.clone("CoilOn"), Timer.clone("CoilOff")
    with Program(strict=False) as logic:
        with Rung(Cmd):
            coil(Coil, on=(on_timer, on_ms), off=(off_timer, off_ms))
        with Rung(Btn):
            out(Cmd)
        with Rung(Fb):
            out(Bool("Seen"))
    plc = PLC(logic, dt=dt)
    Harness(plc).install()
    return plc, Btn


def _drive(plc: PLC, btn: Bool, pattern: list[bool]) -> tuple[list[bool], list[bool]]:
    fb, coil = [], []
    for pressed in pattern:
        plc.patch({btn: pressed})
        plc.step()
        fb.append(plc.current_state.tags["Fb"])
        coil.append(plc.current_state.tags["Coil"])
    return fb, coil


def _random_pattern(rng: random.Random, scans: int) -> list[bool]:
    pattern: list[bool] = []
    level = False
    while len(pattern) < scans:
        level = not level
        pattern.extend([level] * rng.choice([1, 1, 2, 3, 5, 8, 13, 30]))
    return pattern[:scans]


def _bits(values: list[bool]) -> str:
    return "".join("#" if v else "." for v in values)


class TestBoolFeedbackIsADebounce:
    @pytest.mark.parametrize("dip", [1, 3])
    def test_a_dip_shorter_than_the_off_delay_keeps_the_feedback_on(self, dip: int) -> None:
        """on=100 ms and off=50 ms at 10 ms scans: an on-delay feeding an off-delay dropped it."""
        plc, btn = _pair(Physical("Dev", on_delay="100ms", off_delay="50ms"), debounce, 100, 50)
        fb, _coil = _drive(plc, btn, [True] * 20 + [False] * dip + [True] * 25)
        # On after 10 held scans and the scan the plant reads the command a scan late.
        assert _bits(fb) == "." * 10 + "#" * (35 + dip)

    def test_a_dip_as_long_as_the_off_delay_drops_it(self) -> None:
        plc, btn = _pair(Physical("Dev", on_delay="100ms", off_delay="50ms"), debounce, 100, 50)
        fb, _coil = _drive(plc, btn, [True] * 20 + [False] * 6 + [True] * 25)
        # The off side reaches 50 ms on the fifth scan the plant sees the dip and
        # turns Fb off on the sixth.  The on side then counts 100 ms again.
        assert _bits(fb) == "." * 10 + "#" * 16 + "." * 10 + "#" * 15

    def test_a_clean_command_turns_it_off_a_scan_after_its_off_delay(self) -> None:
        plc, btn = _pair(Physical("Dev", on_delay="30ms", off_delay="50ms"), debounce, 30, 50)
        fb, _coil = _drive(plc, btn, [True] * 10 + [False] * 10)
        # The plant sees the fall on scan 11 and counts 50 ms on scans 11..15.
        # It compares nothing on the scan of the fall, so Fb turns off on scan 17.
        assert _bits(fb) == "..." + "#" * 13 + "." * 4

    @pytest.mark.parametrize("dt", [0.010, 0.007])
    @pytest.mark.parametrize(("on_ms", "off_ms"), [(30, 20), (25, 25), (10, 40), (0, 0), (0, 30)])
    def test_it_matches_a_debounce_coil_scan_by_scan(self, on_ms, off_ms, dt) -> None:
        rng = random.Random(on_ms * 1000 + off_ms)
        physical = Physical("Dev", on_delay=f"{on_ms}ms", off_delay=f"{off_ms}ms")
        for _ in range(5):
            plc, btn = _pair(physical, debounce, on_ms, off_ms, dt)
            fb, coil = _drive(plc, btn, _random_pattern(rng, 150))
            assert _bits(fb) == _bits(coil)


class TestPulseIsAFlasher:
    def test_the_feedback_starts_on_the_scan_after_the_enable(self) -> None:
        plc, btn = _pair(Physical("Enc", profile=Pulse("30ms", "20ms")), flasher, 30, 20)
        fb, _coil = _drive(plc, btn, [True] * 18)
        # Cmd is on from scan 1 and the plant reads it on scan 2.  The first on phase
        # counts that scan, so it is a scan short: 2 scans, then 2 off and 3 on.
        assert _bits(fb) == ".##..###..###..###"

    def test_the_feedback_is_off_while_disabled(self) -> None:
        plc, btn = _pair(Physical("Enc", profile=Pulse("30ms", "20ms")), flasher, 30, 20)
        fb, _coil = _drive(plc, btn, [True] * 4 + [False] * 4)
        assert _bits(fb) == ".##.." + "..."

    @pytest.mark.parametrize("dt", [0.010, 0.007])
    @pytest.mark.parametrize(("on_ms", "off_ms"), [(30, 20), (25, 25), (10, 40), (0, 20)])
    def test_it_matches_a_flasher_coil_scan_by_scan(self, on_ms, off_ms, dt) -> None:
        rng = random.Random(on_ms * 1000 + off_ms)
        physical = Physical("Enc", profile=Pulse(f"{on_ms}ms", f"{off_ms}ms"))
        for _ in range(5):
            plc, btn = _pair(physical, flasher, on_ms, off_ms, dt)
            fb, coil = _drive(plc, btn, _random_pattern(rng, 150))
            assert _bits(fb) == _bits(coil)


def _linked(physical: Physical) -> tuple[PLC, Bool, Bool]:
    En = Bool("En")
    Fb = Bool("Fb", physical=physical, link="En")
    with Program(strict=False) as logic:
        with Rung(En):
            out(Bool("Echo"))
        with Rung(Fb):
            out(Bool("Seen"))
    plc = PLC(logic, dt=0.010)
    Harness(plc).install()
    return plc, En, Fb


def _profile_for(plc: PLC, fb: str, value: bool) -> Any:
    harness = plc._harness
    assert harness is not None
    snapshot = plc.current_state.tags
    steps = [
        profile.plan(Eq(fb, frozenset({value})), snapshot)
        for profile in harness.advance_profiles()
        if any(channel.name == fb for channel in profile.channels)
    ]
    return [step for step in steps if step is not None]


def _hold_until(plc: PLC, step: Any, fb: str, value: bool, budget: int = 200) -> int | None:
    """Step under the plan's holds; the scans until ``fb`` has *value*, or ``None``."""
    (demand,) = step.holds
    en = demand.condition.tag.name
    for scans in range(1, budget + 1):
        plc.patch({en: demand.value})
        plc.step()
        if plc.current_state.tags[fb] is value:
            return scans
    return None


class TestPlansThroughTheCoils:
    """how() reads each coupling's AdvanceProfile; stepping under its holds reaches the value."""

    @pytest.mark.parametrize("value", [True, False])
    def test_a_debounce_holds_its_enable_until_the_feedback_follows(self, value: bool) -> None:
        plc, En, _Fb = _linked(Physical("Dev", on_delay="50ms", off_delay="30ms"))
        if not value:
            plc.patch({En: True})
            plc.run(10)
            assert plc.current_state.tags["Fb"] is True
        (step,) = _profile_for(plc, "Fb", value)
        assert [(d.condition.tag.name, d.value) for d in step.holds] == [("En", value)]
        # The plant reads the enable a scan late, and the off side compares a scan
        # after the fall.
        assert _hold_until(plc, step, "Fb", value) == (6 if value else 5)

    @pytest.mark.parametrize("value", [True, False])
    def test_a_flasher_holds_its_enable_until_the_feedback_comes(self, value: bool) -> None:
        plc, En, _Fb = _linked(Physical("Enc", profile=Pulse("30ms", "20ms")))
        if not value:
            plc.patch({En: True})
            plc.run(2)
            assert plc.current_state.tags["Fb"] is True
        (step,) = _profile_for(plc, "Fb", value)
        assert [(d.condition.tag.name, d.value) for d in step.holds] == [("En", True)]
        # On the scan after the enable, and off after a first on phase a scan short.
        assert _hold_until(plc, step, "Fb", value) == 2

    def test_a_flasher_with_a_dwell_of_0_offers_no_way_on(self) -> None:
        plc, _En, _Fb = _linked(Physical("Enc", profile=Pulse("0ms", "20ms")))
        assert _profile_for(plc, "Fb", True) == []
        (step,) = _profile_for(plc, "Fb", False)
        assert _hold_until(plc, step, "Fb", True, budget=50) is None


class TestPendingMs:
    """``pending_ms`` says when the debounce will flip, by stepping."""

    @staticmethod
    def _flip_ms(plc: PLC, fb: str) -> int:
        fork = plc.fork()
        start = fork.current_state.tags[fb]
        for scans in range(1, 500):
            fork.step()
            if fork.current_state.tags[fb] != start:
                return round(scans * plc._dt * 1000)
        raise AssertionError("no flip")

    @pytest.mark.parametrize("held", [0, 1, 3])
    def test_rising(self, held: int) -> None:
        plc, En, _Fb = _linked(Physical("Dev", on_delay="50ms", off_delay="30ms"))
        plc.patch({En: True})
        plc.run(1 + held)
        assert plc._harness.pending_ms()["Fb"] == self._flip_ms(plc, "Fb")

    @pytest.mark.parametrize("held", [0, 1, 3])
    def test_falling(self, held: int) -> None:
        plc, En, _Fb = _linked(Physical("Dev", on_delay="50ms", off_delay="30ms"))
        plc.patch({En: True})
        plc.run(10)
        plc.force(En, False)
        plc.run(1 + held)
        assert plc._harness.pending_ms()["Fb"] == self._flip_ms(plc, "Fb")

    def test_a_rise_after_a_kept_on_count_counts_from_0(self) -> None:
        plc, En, _Fb = _linked(Physical("Dev", on_delay="50ms", off_delay="0ms"))
        plc.patch({En: True})
        plc.run(3)
        plc.force(En, False)
        plc.run(2)
        # The plant reads the enable a scan late, so the on side counts 30 ms and keeps it.
        assert plc.current_state.tags["__cpl_on__Fb"] == 30
        plc.unforce(En)
        plc.patch({En: True})
        plc.run(1)
        assert plc._harness.pending_ms()["Fb"] == self._flip_ms(plc, "Fb") == 50


class TestALinkCutBeforeAnother:
    """Each coil keeps its memory of its rung when a cut moves it in the plant."""

    def test_the_other_coil_keeps_counting(self) -> None:
        def build() -> tuple[PLC, Bool]:
            EnA, EnB = Bool("EnA"), Bool("EnB")
            Bool("FbA", physical=Physical("A", on_delay="0ms"), link="EnA")
            FbB = Bool("FbB", physical=Physical("B", on_delay="100ms"), link="EnB")
            with Program(strict=False) as logic:
                with Rung(EnA, EnB):
                    out(Bool("Echo"))
                with Rung(Bool("FbA"), FbB):
                    out(Bool("Seen"))
            plc = PLC(logic, dt=0.010)
            Harness(plc).install()
            plc.patch({EnA: False, EnB: True})
            plc.run(4)
            return plc, FbB

        plc, FbB = build()
        cut, _FbB = build()
        cut._harness.set_overrides({"FbA": None})
        flips = []
        for runner in (plc, cut):
            for scans in range(1, 50):
                runner.step()
                if runner.current_state.tags[FbB.name]:
                    flips.append(scans)
                    break
        assert flips == [7, 7]

    def test_a_link_cut_and_linked_again_keeps_the_time_counted(self) -> None:
        plc, En, Fb = _linked(Physical("Dev", on_delay="100ms"))
        plc.patch({En: True})
        plc.run(5)  # 40 ms counted
        plc._harness.set_overrides({"Fb": None})
        plc.run(3)
        plc._harness.set_overrides({"Fb": Coupling("En", "Fb", Physical("Dev", on_delay="100ms"))})
        scans = 0
        while not plc.current_state.tags[Fb.name]:
            plc.step()
            scans += 1
        assert scans == 6


class TestEveryEngineAgrees:
    """The compiled replay and the interpreted one rebuild the coils' scans as they ran."""

    @pytest.mark.parametrize(
        "physical",
        [
            Physical("Dev", on_delay="30ms", off_delay="50ms"),
            Physical("Dev", on_delay="0ms", off_delay="0ms"),
            Physical("Enc", profile=Pulse("30ms", "20ms")),
        ],
        ids=["debounce", "debounce-0", "flasher"],
    )
    def test_replays_match_the_live_run(self, physical: Physical) -> None:
        En = Bool("En", external=True)
        Fb = Bool("Fb", physical=physical, link="En")
        with Program(strict=False) as logic:
            with Rung(Fb):
                out(Bool("Seen"))
        plc = PLC(logic, dt=0.010, cache=0, history=10_000, checkpoint_interval=5)
        Harness(plc).install()
        pattern = _random_pattern(random.Random(3), 60)
        live = []
        for value in pattern:
            plc.patch({En: value})
            plc.step()
            live.append(dict(plc.current_state.tags))

        kernel = plc._compiled_replay_supported_kernel()
        assert kernel is not None
        for scan in range(1, len(pattern) + 1):
            assert plc._replay_to_compiled(scan, kernel).current_state.tags == live[scan - 1]
            assert plc._replay_to_interpreted(scan).current_state.tags == live[scan - 1]

    def test_a_run_equals_stepping(self) -> None:
        def build() -> PLC:
            plc, _En, _Fb = _linked(Physical("Dev", on_delay="30ms", off_delay="50ms"))
            return plc

        stepped, ran = build(), build()
        for plc in (stepped, ran):
            plc.patch({"En": True})
        for _ in range(40):
            stepped.step()
        ran.run(40)
        assert ran.current_state.tags == stepped.current_state.tags
        assert ran.current_state.memory == stepped.current_state.memory
