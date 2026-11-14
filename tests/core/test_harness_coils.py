"""The harness runs each bool feedback as the CLICK coil the device behaves like.

``Physical(on_delay=, off_delay=)`` runs as ``debounce`` under its enable,
``Physical(flasher_on=, flasher_off=)`` as ``flasher``, and ``timed`` and ``toggle`` as the
coils of those names.  Each test here holds the harness's feedback to that literal coil in
a program, scan by scan, or to what stepping does.
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
    Real,
    Rung,
    Timer,
    debounce,
    flasher,
    out,
    timed,
    toggle,
)
from pyrung.core.crossing import Eq
from pyrung.core.harness import Coupling
from pyrung.core.site_memory import _first_tag_name, instruction_memory


def _pair(
    physical: Physical, coil: Any, on_ms: int, off_ms: int, dt: float = 0.010
) -> tuple[PLC, Bool]:
    """A harness feedback ``Fb`` and the literal two-timer coil ``Coil``."""
    on_timer, off_timer = Timer.clone("CoilOn"), Timer.clone("CoilOff")
    return _pair_with(
        physical, lambda target: coil(target, on=(on_timer, on_ms), off=(off_timer, off_ms)), dt
    )


def _pair_with(physical: Physical, write: Any, dt: float = 0.010) -> tuple[PLC, Bool]:
    """A harness feedback ``Fb`` and the literal coil ``Coil`` that *write* puts in a rung.

    Both are driven by ``Cmd``.  The coil's rung comes first, so it reads ``Cmd`` as the
    last scan left it, as the plant does.
    """
    Btn = Bool("Btn", external=True)
    Cmd = Bool("Cmd")
    Fb = Bool("Fb", physical=physical, link="Cmd")
    Coil = Bool("Coil")
    with Program(strict=False) as logic:
        with Rung(Cmd):
            write(Coil)
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
        # The plant sees the fall on scan 12 and counts 50 ms on scans 12..16.
        # Each scan compares the count from before its own tick, so Fb turns off on scan 17.
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


class TestFlasherFeedbackIsAFlasher:
    def test_the_feedback_starts_on_the_scan_after_the_enable(self) -> None:
        plc, btn = _pair(Physical("Enc", flasher_on="30ms", flasher_off="20ms"), flasher, 30, 20)
        fb, _coil = _drive(plc, btn, [True] * 18)
        # Cmd is on from scan 1 and the plant reads it on scan 2.  Each phase shows
        # through the scan its time runs out: 3 scans on, then 2 off.
        assert _bits(fb) == ".###..###..###..##"

    def test_the_feedback_is_off_while_disabled(self) -> None:
        plc, btn = _pair(Physical("Enc", flasher_on="30ms", flasher_off="20ms"), flasher, 30, 20)
        fb, _coil = _drive(plc, btn, [True] * 4 + [False] * 4)
        assert _bits(fb) == ".###." + "..."

    @pytest.mark.parametrize("dt", [0.010, 0.007])
    @pytest.mark.parametrize(("on_ms", "off_ms"), [(30, 20), (25, 25), (10, 40), (0, 20)])
    def test_it_matches_a_flasher_coil_scan_by_scan(self, on_ms, off_ms, dt) -> None:
        rng = random.Random(on_ms * 1000 + off_ms)
        physical = Physical("Enc", flasher_on=f"{on_ms}ms", flasher_off=f"{off_ms}ms")
        for _ in range(5):
            plc, btn = _pair(physical, flasher, on_ms, off_ms, dt)
            fb, coil = _drive(plc, btn, _random_pattern(rng, 150))
            assert _bits(fb) == _bits(coil)


def _timed_coil(on_ms: int, maintained: bool) -> Any:
    timer = Timer.clone("CoilT")
    return lambda target: timed(target, timer, on_ms, maintained=maintained)


class TestTimedFeedbackIsATimedCoil:
    def test_each_command_gives_one_pulse(self) -> None:
        plc, btn = _pair_with(Physical("Horn", timed="50ms"), _timed_coil(50, False))
        fb, _coil = _drive(plc, btn, [True] * 8 + [False] * 3 + [True] * 8)
        # The rise's scan counts toward the 50 ms, so Fb shows for 4 scans, not 5.
        assert _bits(fb) == ".####......." + "####..."

    def test_maintained_it_follows_the_command_after_the_pulse(self) -> None:
        physical = Physical("Horn", timed="50ms", maintained=True)
        plc, btn = _pair_with(physical, _timed_coil(50, True))
        fb, _coil = _drive(plc, btn, [True] * 2 + [False] * 8 + [True] * 8 + [False] * 3)
        assert _bits(fb) == ".####....." + ".########" + ".."

    @pytest.mark.parametrize("dt", [0.010, 0.007])
    @pytest.mark.parametrize("maintained", [False, True])
    @pytest.mark.parametrize("on_ms", [0, 10, 30, 75])
    def test_it_matches_a_timed_coil_scan_by_scan(self, on_ms, maintained, dt) -> None:
        rng = random.Random(on_ms * 10 + maintained)
        physical = Physical("Horn", timed=f"{on_ms}ms", maintained=maintained)
        for _ in range(5):
            plc, btn = _pair_with(physical, _timed_coil(on_ms, maintained), dt)
            fb, coil = _drive(plc, btn, _random_pattern(rng, 150))
            assert _bits(fb) == _bits(coil)


class TestToggleFeedbackIsAToggleCoil:
    def test_each_command_flips_it(self) -> None:
        plc, btn = _pair_with(Physical("Lamp", toggle=True), toggle)
        fb, _coil = _drive(plc, btn, [True] * 3 + [False] * 2 + [True] * 3 + [False] * 2)
        assert _bits(fb) == ".#####...."

    @pytest.mark.parametrize("dt", [0.010, 0.007])
    def test_it_matches_a_toggle_coil_scan_by_scan(self, dt) -> None:
        rng = random.Random(7)
        for _ in range(5):
            plc, btn = _pair_with(Physical("Lamp", toggle=True), toggle, dt)
            fb, coil = _drive(plc, btn, _random_pattern(rng, 150))
            assert _bits(fb) == _bits(coil)


def _linked(physical: Physical, *, enabled: bool = False) -> tuple[PLC, Bool, Bool]:
    En = Bool("En", default=enabled)
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
    """Step under the plan's holds and count the scans until ``fb`` has *value* (``None`` if never)."""
    (demand,) = step.holds
    en = demand.condition.tag.name
    for scans in range(1, budget + 1):
        plc.patch({en: demand.value})
        plc.step()
        if plc.current_state.tags[fb] is value:
            return scans
    return None


class TestPlansThroughTheCoils:
    """how() reads each coupling's AdvanceProfile.  Stepping under its holds reaches the value."""

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
        plc, En, _Fb = _linked(Physical("Enc", flasher_on="30ms", flasher_off="20ms"))
        if not value:
            plc.patch({En: True})
            plc.run(2)
            assert plc.current_state.tags["Fb"] is True
        (step,) = _profile_for(plc, "Fb", value)
        assert [(d.condition.tag.name, d.value) for d in step.holds] == [("En", True)]
        # On the scan after the enable, and off after a whole first on phase.
        assert _hold_until(plc, step, "Fb", value) == (2 if value else 3)

    def test_a_flasher_with_a_dwell_of_0_offers_no_way_on(self) -> None:
        plc, _En, _Fb = _linked(Physical("Enc", flasher_on="0ms", flasher_off="20ms"))
        assert _profile_for(plc, "Fb", True) == []
        (step,) = _profile_for(plc, "Fb", False)
        assert _hold_until(plc, step, "Fb", True, budget=50) is None


def _pulse_until(plc: PLC, step: Any, fb: str, value: bool, budget: int = 50) -> int | None:
    """Give the plan's pulse a rise, then step under its holds until ``fb`` has *value*."""
    demand = step.pulse
    en = demand.condition.tag.name
    if plc.current_state.tags[en] is demand.value:
        plc.patch({en: not demand.value})
        plc.step()
    plc.patch({en: demand.value})
    for scans in range(1, budget + 1):
        plc.step()
        if plc.current_state.tags[fb] is value:
            return scans
    return None


class TestPlansThroughTimedAndToggle:
    """A timed or toggle feedback comes on a rise of its enable.  Stepping the plan reaches it.

    An enable that is off is held on.  One that is on is pulsed, which takes a fall first.
    """

    @pytest.mark.parametrize(
        "physical",
        [Physical("Horn", timed="50ms"), Physical("Lamp", toggle=True)],
        ids=["timed", "toggle"],
    )
    def test_an_enable_that_is_off_is_held_on(self, physical: Physical) -> None:
        plc, _En, _Fb = _linked(physical)
        (step,) = _profile_for(plc, "Fb", True)
        assert step.pulse is None
        assert [(d.condition.tag.name, d.value) for d in step.holds] == [("En", True)]
        assert _hold_until(plc, step, "Fb", True) == 2

    @pytest.mark.parametrize(
        "physical",
        [Physical("Horn", timed="50ms"), Physical("Lamp", toggle=True)],
        ids=["timed", "toggle"],
    )
    def test_an_enable_that_is_on_is_pulsed(self, physical: Physical) -> None:
        plc, _En, _Fb = _linked(physical, enabled=True)
        (step,) = _profile_for(plc, "Fb", True)
        assert (step.pulse.condition.tag.name, step.pulse.value, step.holds) == ("En", True, ())
        assert _pulse_until(plc, step, "Fb", True) == 2

    def test_a_toggle_that_is_on_is_pulsed_off(self) -> None:
        plc, En, _Fb = _linked(Physical("Lamp", toggle=True))
        plc.patch({En: True})
        plc.run(3)
        assert plc.current_state.tags["Fb"] is True
        (step,) = _profile_for(plc, "Fb", False)
        assert (step.pulse.condition.tag.name, step.pulse.value) == ("En", True)
        assert _pulse_until(plc, step, "Fb", False) == 2

    def test_a_timed_pulse_ends_by_itself(self) -> None:
        plc, En, _Fb = _linked(Physical("Horn", timed="50ms"))
        plc.patch({En: True})
        plc.run(2)
        (step,) = _profile_for(plc, "Fb", False)
        assert (step.pulse, step.holds) == (None, ())
        scans = 0
        while plc.current_state.tags["Fb"]:
            plc.step()
            scans += 1
        assert scans == 4

    def test_a_maintained_timed_feedback_holds_its_enable_off(self) -> None:
        plc, En, _Fb = _linked(Physical("Horn", timed="50ms", maintained=True))
        plc.patch({En: True})
        plc.run(10)
        (step,) = _profile_for(plc, "Fb", False)
        assert [(d.condition.tag.name, d.value) for d in step.holds] == [("En", False)]
        assert _hold_until(plc, step, "Fb", False) == 2

    @pytest.mark.parametrize("on_time", ["0ms", "10ms"])
    def test_an_on_time_of_a_scan_or_less_offers_no_way_on(self, on_time: str) -> None:
        plc, _En, _Fb = _linked(Physical("Horn", timed=on_time))
        assert _profile_for(plc, "Fb", True) == []
        plc.patch({"En": True})
        assert not any(_scan(plc, 10))


class TestInstallWithTheEnableOn:
    """A coil whose enable is already on has long since seen its rise, so it does not fire."""

    @pytest.mark.parametrize(("maintained", "fb"), [(False, "......"), (True, "######")])
    def test_timed(self, maintained: bool, fb: str) -> None:
        plc, _En, _Fb = _linked(Physical("Horn", timed="50ms", maintained=maintained), enabled=True)
        assert _bits(_scan(plc, 6)) == fb

    def test_toggle(self) -> None:
        plc, _En, _Fb = _linked(Physical("Lamp", toggle=True), enabled=True)
        assert _bits(_scan(plc, 6)) == "......"


_SETTLED = [
    (Physical("Lamp", toggle=True), "........"),
    (Physical("Horn", timed="50ms"), "........"),
    (Physical("Horn", timed="50ms", maintained=True), "########"),
    (Physical("Dev", on_delay="50ms"), "########"),
]
_SETTLED_IDS = ["toggle", "timed", "timed-maintained", "on-delay"]


class TestAReinstallSettles:
    """An install after an uninstall settles a held enable, as a first install does."""

    @pytest.mark.parametrize(("physical", "settled"), _SETTLED, ids=_SETTLED_IDS)
    def test_an_enable_turned_on_while_uninstalled(self, physical: Physical, settled: str) -> None:
        plc, En, _Fb = _linked(physical)
        plc.run(5)  # each coil remembers its rung as off
        harness = plc._harness
        assert harness is not None
        harness.uninstall()
        plc.patch({En: True})
        plc.run(100)
        Harness(plc).install()
        assert _bits(_scan(plc, 8)) == settled

    @pytest.mark.parametrize(("physical", "settled"), _SETTLED, ids=_SETTLED_IDS)
    def test_a_coil_key_at_its_default_does_not_stop_the_settle(
        self, physical: Physical, settled: str
    ) -> None:
        """An absent key and a key at its default are the same state, so both settle."""
        En = Bool("En", default=True)
        Fb = Bool("Fb", physical=physical, link="En")
        with Program(strict=False) as logic:
            with Rung(En):
                out(Bool("Echo"))
            with Rung(Fb):
                out(Bool("Seen"))
        plc = PLC(logic, dt=0.010)
        prefix = {"toggle": "_toggle", "timed": "_timed_rung", "delay": "_debounce_rung"}
        plc._edit_tip_tags({}, memory={f"{prefix[physical.kind]}:plant:fb.Fb": False})
        Harness(plc).install()
        assert _bits(_scan(plc, 8)) == settled

    @pytest.mark.parametrize(("physical", "settled"), _SETTLED, ids=_SETTLED_IDS)
    def test_a_first_install_settles_the_same(self, physical: Physical, settled: str) -> None:
        plc, _En, _Fb = _linked(physical, enabled=True)
        assert _bits(_scan(plc, 8)) == settled


def _scan(plc: PLC, scans: int) -> list[bool]:
    seen = []
    for _ in range(scans):
        plc.step()
        seen.append(plc.current_state.tags["Fb"])
    return seen


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


def _two_links(en_a: bool, en_b: bool, scans: int) -> PLC:
    """``FbA`` (on 100 ms, off 50 ms) and ``FbB`` (0 ms), with ``FbB``'s coil first in the plant."""
    EnA, EnB = Bool("EnA"), Bool("EnB")
    FbA = Bool("FbA", physical=Physical("A", on_delay="100ms", off_delay="50ms"), link="EnA")
    FbB = Bool("FbB", physical=Physical("B", on_delay="0ms"), link="EnB")
    with Program(strict=False) as logic:
        with Rung(EnA, EnB):
            out(Bool("Echo"))
        with Rung(FbA, FbB):
            out(Bool("Seen"))
    plc = PLC(logic, dt=0.010)
    Harness(plc).install()
    plc.patch({EnA: en_a, EnB: en_b})
    plc.run(scans)
    return plc


def _coil_order(plc: PLC) -> list[str]:
    assert plc._synthesis is not None
    return [rung._instructions[0].target.name for rung in plc._synthesis.plant]


def _scans_to(plc: PLC, tag: str, value: bool) -> int | None:
    for scans in range(1, 50):
        plc.step()
        if plc.current_state.tags[tag] is value:
            return scans
    return None


class TestALinkCutBeforeAnother:
    """Each coil reads its own memory after a cut moves it up the plant."""

    @pytest.mark.parametrize("en_b", [False, True])
    def test_at_the_tip(self, en_b: bool) -> None:
        plc = _two_links(True, en_b, 4)
        assert _coil_order(plc) == ["FbB", "FbA"]
        plc._harness.set_overrides({"FbB": None})
        assert _coil_order(plc) == ["FbA"]
        assert _scans_to(plc, "FbA", True) == _scans_to(_two_links(True, en_b, 4), "FbA", True) == 7

    @pytest.mark.parametrize("en_b", [False, True])
    def test_in_a_fork_from_before_the_cut(self, en_b: bool) -> None:
        plc = _two_links(True, en_b, 4)
        before = plc.current_state.scan_id - 1
        plc._harness.set_overrides({"FbB": None})
        never_cut = _two_links(True, en_b, 4).fork(scan_id=before)
        assert _scans_to(plc.fork(scan_id=before), "FbA", True) == 8
        assert _scans_to(never_cut, "FbA", True) == 8

    def test_a_flasher_in_a_fork_from_before_the_cut(self) -> None:
        def build() -> PLC:
            EnA, EnB = Bool("EnA"), Bool("EnB")
            FbA = Bool(
                "FbA", physical=Physical("A", flasher_on="30ms", flasher_off="20ms"), link="EnA"
            )
            FbB = Bool(
                "FbB", physical=Physical("B", flasher_on="30ms", flasher_off="20ms"), link="EnB"
            )
            with Program(strict=False) as logic:
                with Rung(EnA, EnB):
                    out(Bool("Echo"))
                with Rung(FbA, FbB):
                    out(Bool("Seen"))
            plc = PLC(logic, dt=0.010)
            Harness(plc).install()
            plc.patch({EnA: True, EnB: False})
            plc.run(4)
            return plc

        def bits(plc: PLC) -> str:
            seen = ""
            for _ in range(12):
                plc.step()
                seen += "#" if plc.current_state.tags["FbA"] else "."
            return seen

        plc = build()
        assert _coil_order(plc) == ["FbB", "FbA"]
        before = plc.current_state.scan_id - 1
        plc._harness.set_overrides({"FbB": None})
        assert bits(plc.fork(scan_id=before)) == bits(build().fork(scan_id=before))

    @pytest.mark.parametrize(("off_scans", "left"), [(2, 5), (6, 1)])
    def test_after_an_uninstall_and_install(self, off_scans: int, left: int) -> None:
        """``FbA``'s off side is counting, and ``FbB``'s coil last saw its rung true.

        Had ``FbA``'s coil read ``FbB``'s memory, its next scan would be a fall,
        and a fall scan never turns the coil off.
        """

        def build() -> PLC:
            plc = _two_links(True, True, 15)
            plc.patch({"EnA": False})
            plc.run(off_scans)
            return plc

        plc = build()
        harness = plc._harness
        assert harness is not None
        harness.uninstall()
        harness.set_overrides({"FbB": None})
        harness.install()
        assert _coil_order(plc) == ["FbA"]
        assert _scans_to(plc, "FbA", False) == _scans_to(build(), "FbA", False) == left

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


def test_every_plant_instruction_with_memory_is_keyed_by_the_tag_it_drives() -> None:
    """A position key would move when a link before it is cut (see above)."""
    En = Bool("En")
    FbD = Bool("FbD", physical=Physical("D", on_delay="30ms", off_delay="20ms"), link="En")
    FbP = Bool("FbP", physical=Physical("P", flasher_on="30ms", flasher_off="20ms"), link="En")
    FbR = Real("FbR", physical=Physical("R", ramp_on=1.0, ramp_off=-1.0), link="En")
    FbT = Bool("FbT", physical=Physical("T", timed="30ms"), link="En")
    FbG = Bool("FbG", physical=Physical("G", toggle=True), link="En")
    with Program(strict=False) as logic:
        with Rung(En):
            out(Bool("Echo"))
        with Rung(FbD, FbP, FbR > 5.0, FbT, FbG):
            out(Bool("Seen"))
    plc = PLC(logic, dt=0.010)
    Harness(plc).install()
    assert plc._synthesis is not None
    keyed = {
        instr.state_key: _first_tag_name(instr)
        for rung in plc._synthesis.plant
        for instr in rung._instructions
        if instruction_memory(instr)
    }
    assert {"FbD", "FbP", "FbT", "FbG"} <= set(keyed.values())
    assert all(key == f"plant:fb.{tag}" for key, tag in keyed.items())


class TestEveryEngineAgrees:
    """The compiled replay and the interpreted one rebuild the coils' scans as they ran."""

    @pytest.mark.parametrize(
        "physical",
        [
            Physical("Dev", on_delay="30ms", off_delay="50ms"),
            Physical("Dev", on_delay="0ms", off_delay="0ms"),
            Physical("Enc", flasher_on="30ms", flasher_off="20ms"),
            Physical("Horn", timed="30ms"),
            Physical("Horn", timed="30ms", maintained=True),
            Physical("Lamp", toggle=True),
        ],
        ids=["debounce", "debounce-0", "flasher", "timed", "timed-maintained", "toggle"],
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
            assert plc._replay_to(scan, kernel).current_state.tags == live[scan - 1]
            assert plc._replay_to_interpreted(scan).current_state.tags == live[scan - 1]

    @pytest.mark.parametrize(
        "physical",
        [
            Physical("Dev", on_delay="30ms", off_delay="50ms"),
            Physical("Horn", timed="30ms"),
            Physical("Horn", timed="30ms", maintained=True),
            Physical("Lamp", toggle=True),
        ],
        ids=["debounce", "timed", "timed-maintained", "toggle"],
    )
    def test_a_run_equals_stepping(self, physical: Physical) -> None:
        def build() -> PLC:
            plc, _En, _Fb = _linked(physical)
            return plc

        stepped, ran = build(), build()
        for plc in (stepped, ran):
            plc.patch({"En": True})
        for _ in range(40):
            stepped.step()
        ran.run(40)
        assert ran.current_state.tags == stepped.current_state.tags
        assert ran.current_state.memory == stepped.current_state.memory
