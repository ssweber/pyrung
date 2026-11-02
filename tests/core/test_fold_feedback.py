"""Folds over harness analog feedback, and cycle jumps over slots a run touches late.

A ``Ramp`` or ``Approach`` plant moves its value every scan, so a window has to end
where a rung would read that value crossed and land the value where stepping leaves
it.  A value nothing can see, or one that has settled, keeps folding.

A slot absent from the state when a cycle fold starts joins it on the first scan
that touches it; the ring has to watch it from then on.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

import pytest

from pyrung import (
    Block,
    Bool,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    Timer,
    blockcopy,
    calc,
    copy,
    latch,
    on_delay,
    out,
    rise,
)
from pyrung.core.analysis.pilot.cyclefold import cycle_fold_until
from pyrung.core.harness import Harness
from pyrung.core.physical import Approach, Physical, Pulse, Ramp
from pyrung.core.runner import PLC

Enable, Hit, Tog = Bool("Enable"), Bool("Hit"), Bool("Tog")
Shown = Real("Shown")
T, Win = Timer.clone("T"), Timer.clone("Win")

_FRESH = itertools.count()

RAMP = Ramp(up=1.0, down=-0.5)
ODD_RAMP = Ramp(up=0.37, down=-0.11)
APPROACH = Approach(100.0, 0.3)
PROFILES = [
    pytest.param(RAMP, 5.0, id="ramp"),
    pytest.param(ODD_RAMP, 2.0, id="odd-ramp"),
    pytest.param(APPROACH, 30.0, id="approach"),
]


def _temp(profile: Any) -> Real:
    return Real("Temp", physical=Physical("TempSensor", profile=profile), link="Enable")


def _plant(
    profile: Any,
    reader: Callable[[Real], None],
    *,
    toggle: bool = False,
    enable: Callable[[], None] | None = None,
    far_s: int = 40,
) -> PLC:
    """A harness plant, *reader*'s rungs, and a far timer that opens a long window."""
    Temp = _temp(profile)
    with Program(strict=False) as prog:
        if toggle:
            with Rung(~Tog):
                out(Tog)
        if enable is not None:
            enable()
        else:
            with Rung():
                out(Enable)
        reader(Temp)
        with Rung():
            on_delay(T, far_s, "s")
    plc = PLC(prog, dt=0.010)
    Harness(plc).install()
    return plc


def _latch_at(threshold: float) -> Callable[[Real], None]:
    def reader(temp: Real) -> None:
        with Rung(temp >= threshold):
            latch(Hit)

    return reader


def _stepped(make: Callable[[], PLC], scan_id: int) -> PLC:
    ref = make()
    while ref.state.scan_id < scan_id:
        ref.step()
    return ref


def _tags(plc: PLC) -> dict[str, Any]:
    return {k: v for k, v in plc.state.tags.items() if k != "sys.scan_time"}


def _assert_exact(plc: PLC, make: Callable[[], PLC]) -> None:
    ref = _stepped(make, plc.state.scan_id)
    assert _tags(plc) == _tags(ref)


def _first_hit(make: Callable[[], PLC], budget: int = 3_000) -> int | None:
    ref = make()
    for _ in range(budget):
        ref.step()
        if ref.state.tags.get("Hit"):
            return ref.state.scan_id
    return None


def _kernel_scans(plc: PLC) -> dict[str, int]:
    counter = {"n": 0}
    original = plc._run_single_scan

    def wrapped(**options: Any) -> Any:
        counter["n"] += 1
        return original(**options)

    plc._run_single_scan = wrapped  # ty: ignore[invalid-assignment]
    return counter


def _jump(plc: PLC, predicate: Any, budget: int = 3_000, **kwargs: Any) -> tuple[bool, dict]:
    stats: dict[str, int] = {}
    reached = cycle_fold_until(
        plc, predicate, budget=budget, kernel_budget=False, stats=stats, **kwargs
    )
    return reached, stats


def _hit(state: Any) -> bool:
    return bool(state.tags.get("Hit"))


class TestPlainFoldOverAnalogFeedback:
    """A rung comparing the feedback bounds the window; the landing is the stepped value."""

    @pytest.mark.parametrize(("profile", "threshold"), PROFILES)
    def test_run_until_lands_on_the_crossing(self, profile: Any, threshold: float) -> None:
        make = lambda: _plant(profile, _latch_at(threshold))  # noqa: E731
        expected = _first_hit(make)
        assert expected is not None
        plc = make()
        counter = _kernel_scans(plc)
        plc.run_until(Hit, max_cycles=3_000)
        assert plc.state.scan_id == expected
        _assert_exact(plc, make)
        assert counter["n"] < expected / 5  # it still folds up to the crossing

    @pytest.mark.parametrize(("profile", "threshold"), PROFILES)
    def test_run_for_is_exact_past_the_crossing(self, profile: Any, threshold: float) -> None:
        make = lambda: _plant(profile, _latch_at(threshold))  # noqa: E731
        plc = make()
        plc.run_for(30.0)
        assert plc.state.tags.get("Hit") is True
        _assert_exact(plc, make)

    @pytest.mark.parametrize(("profile", "threshold"), PROFILES)
    def test_a_condition_on_the_feedback_ends_the_run(self, profile: Any, threshold: float) -> None:
        make = lambda: _plant(profile, _latch_at(1e30))  # noqa: E731
        Temp = _temp(profile)
        stepped = make()
        while not stepped.state.tags.get("Temp", 0.0) >= threshold:
            stepped.step()
        plc = make()
        plc.run_until(Temp >= threshold, max_cycles=3_000)
        assert plc.state.scan_id == stepped.state.scan_id
        assert _tags(plc) == _tags(stepped)

    def test_an_opaque_predicate_on_the_feedback_ends_the_run(self) -> None:
        make = lambda: _plant(RAMP, _latch_at(1e30))  # noqa: E731
        stepped = make()
        while not stepped.state.tags.get("Temp", 0.0) >= 5.0:
            stepped.step()
        plc = make()
        plc.run_until(lambda s: s.tags.get("Temp", 0.0) >= 5.0, max_cycles=3_000)
        assert plc.state.scan_id == stepped.state.scan_id
        assert _tags(plc) == _tags(stepped)

    def test_a_copy_reader_sees_every_value(self) -> None:
        def reader(temp: Real) -> None:
            with Rung():
                copy(temp, Shown)
            with Rung(Shown >= 5.0):
                latch(Hit)

        make = lambda: _plant(RAMP, reader)  # noqa: E731
        expected = _first_hit(make)
        plc = make()
        plc.run_until(Hit, max_cycles=3_000)
        assert plc.state.scan_id == expected
        _assert_exact(plc, make)

    def test_decay_after_the_enable_drops_is_exact(self) -> None:
        def enable() -> None:
            with Rung():
                on_delay(Win, 1_000)
            with Rung(~Win.Done):
                out(Enable)

        make = lambda: _plant(RAMP, _latch_at(1e30), enable=enable)  # noqa: E731
        plc = make()
        counter = _kernel_scans(plc)
        plc.run_for(30.0)
        _assert_exact(plc, make)
        assert counter["n"] < 500
        assert plc.state.tags["Temp"] < 0.0

    def test_a_tag_setpoint_moved_by_a_rung_is_exact(self) -> None:
        SetPt = Real("SetPt", default=50.0)

        def make() -> PLC:
            def reader(temp: Real) -> None:
                with Rung():
                    on_delay(Win, 7_000)
                with Rung(Win.Done):
                    copy(5.0, SetPt)
                with Rung(temp >= 20.0):
                    latch(Hit)

            return _plant(Approach("SetPt", 0.3), reader)

        plc = make()
        plc.run_for(30.0)
        _assert_exact(plc, make)

    def test_a_forced_feedback_is_exact(self) -> None:
        def make() -> PLC:
            plc = _plant(RAMP, _latch_at(5.0))
            plc.force("Temp", 2.0)
            return plc

        plc = make()
        plc.run_for(30.0)
        assert plc.state.tags.get("Temp") == 2.0
        _assert_exact(plc, make)

    def test_compiled_replay_seek_matches(self) -> None:
        plc = _plant(RAMP, _latch_at(5.0))
        for _ in range(1_200):
            plc.step()
        seek = plc._replay_seek(1_150)
        ref = plc.replay_to(1_150)
        assert plc._last_replay_seek_stats["kernel_scans"] < 150
        assert seek.state.scan_id == ref.state.scan_id
        assert _tags(seek) == _tags(ref)


class TestFeedbackKeepsFolding:
    """A settled plant, or one no reader can see cross, is not a reason to step."""

    @staticmethod
    def _copy_reader(temp: Real) -> None:
        with Rung():
            copy(temp, Shown)
        with Rung(Shown >= 1e30):
            latch(Hit)

    def test_a_settled_approach_folds_under_a_copy_reader(self) -> None:
        def make() -> PLC:
            return _plant(Approach(100.0, 5.0), self._copy_reader, far_s=800)

        plc = make()
        plc.run_for(40.0)  # moving, and a rung reads every value: this stretch steps
        assert plc.state.tags["Temp"] < 100.0
        _assert_exact(plc, make)
        while plc.state.tags["Temp"] != plc.state.tags["Shown"] or plc.state.scan_id < 12_000:
            plc.step()
        counter = _kernel_scans(plc)
        plc.run_for(600.0)
        assert counter["n"] < 50
        assert plc.state.tags["Temp"] == pytest.approx(100.0)

    def test_a_plant_at_rest_folds_under_a_copy_reader(self) -> None:
        def enable() -> None:
            with Rung(Tog, ~Tog):
                out(Enable)

        plc = _plant(Ramp(up=1.0), self._copy_reader, enable=enable)
        counter = _kernel_scans(plc)
        plc.run_for(30.0)
        assert counter["n"] < 50
        assert plc.state.tags.get("Temp", 0.0) == 0.0

    def test_an_unread_value_folds(self) -> None:
        def reader(temp: Real) -> None:
            with Rung():
                calc(temp * 2.0, Shown)

        make = lambda: _plant(RAMP, reader)  # noqa: E731
        plc = make()
        counter = _kernel_scans(plc)
        plc.run_for(30.0)
        assert counter["n"] < 50
        assert plc.state.tags["Shown"] > 50.0
        _assert_exact(plc, make)

    def test_a_far_comparison_folds_to_the_timer(self) -> None:
        make = lambda: _plant(RAMP, _latch_at(1e30))  # noqa: E731
        plc = make()
        counter = _kernel_scans(plc)
        plc.run_for(30.0)
        assert counter["n"] < 50
        _assert_exact(plc, make)

    def test_a_cycle_jump_runs_with_a_far_comparison(self) -> None:
        make = lambda: _plant(RAMP, _latch_at(1e30), toggle=True)  # noqa: E731
        plc = make()
        _reached, stats = _jump(plc, _hit, budget=3_000, predicate_reads=frozenset({"Hit"}))
        assert stats["cycle_folded_scans"] > 2_500
        _assert_exact(plc, make)


class TestCycleJumpOverAnalogFeedback:
    """A jump walks the plant through each skipped scan, and stops short of a crossing."""

    @pytest.mark.parametrize(("profile", "threshold"), PROFILES)
    def test_jump_lands_on_the_crossing(self, profile: Any, threshold: float) -> None:
        make = lambda: _plant(profile, _latch_at(threshold), toggle=True)  # noqa: E731
        expected = _first_hit(make)
        plc = make()
        reached, stats = _jump(plc, _hit, predicate_reads=frozenset({"Hit"}))
        assert reached
        assert plc.state.scan_id == expected
        assert stats["cycle_folded_scans"] > 0
        _assert_exact(plc, make)

    def test_jump_carries_the_plant_with_no_crossing(self) -> None:
        make = lambda: _plant(RAMP, _latch_at(1e30), toggle=True)  # noqa: E731
        plc = make()
        _jump(plc, _hit, predicate_reads=frozenset({"Hit"}))
        assert plc.state.tags["Temp"] == pytest.approx(0.01 * (plc.state.scan_id - 1))
        _assert_exact(plc, make)

    def test_enable_toggled_by_the_cycle_is_walked_by_phase(self) -> None:
        def enable() -> None:
            with Rung(Tog):
                out(Enable)

        make = lambda: _plant(ODD_RAMP, _latch_at(2.0), toggle=True, enable=enable)  # noqa: E731
        expected = _first_hit(make)
        plc = make()
        reached, _stats = _jump(plc, _hit, predicate_reads=frozenset({"Hit"}))
        assert reached
        assert plc.state.scan_id == expected
        _assert_exact(plc, make)

    def test_an_opaque_predicate_on_the_feedback_is_not_jumped(self) -> None:
        make = lambda: _plant(RAMP, _latch_at(1e30), toggle=True)  # noqa: E731
        stepped = make()
        while not stepped.state.tags.get("Temp", 0.0) >= 5.0:
            stepped.step()
        plc = make()
        reached, _stats = _jump(plc, lambda s: s.tags.get("Temp", 0.0) >= 5.0)
        assert reached
        assert plc.state.scan_id == stepped.state.scan_id
        assert _tags(plc) == _tags(stepped)

    def test_a_callback_beside_the_plant_cannot_hide_its_crossing(self) -> None:
        def make() -> PLC:
            plc = _plant(RAMP, _latch_at(5.0), toggle=True)
            plc.when(lambda s: bool(s.tags.get("Tog"))).do(lambda s: None)
            return plc

        expected = _first_hit(make)
        plc = make()
        reached, _stats = _jump(plc, _hit, predicate_reads=frozenset({"Hit"}))
        assert reached
        assert plc.state.scan_id == expected
        _assert_exact(plc, make)


class TestBoolFeedbackUnderACycleJump:
    """A bool plant runs on real timers, so a cold start must watch them as they appear."""

    @staticmethod
    def _bool_plant(physical: Physical, reader: Callable[[Bool], None]) -> PLC:
        Fb = Bool("Fb", physical=physical, link="Enable")
        with Program(strict=False) as prog:
            with Rung(~Tog):
                out(Tog)
            with Rung():
                out(Enable)
            reader(Fb)
            with Rung():
                on_delay(T, 40, "s")
        plc = PLC(prog, dt=0.010)
        Harness(plc).install()
        return plc

    @staticmethod
    def _contact(fb: Bool) -> None:
        with Rung(fb):
            latch(Hit)

    @staticmethod
    def _third_rise(fb: Bool) -> None:
        count = Int("Cnt")
        with Rung(rise(fb)):
            calc(count + 1, count)
        with Rung(count == 3):
            latch(Hit)

    @pytest.mark.parametrize("reader", ["_contact", "_third_rise"])
    def test_a_pulse_from_a_cold_start_lands_on_the_reader(self, reader: str) -> None:
        physical = Physical("Sensor", profile=Pulse(on_dwell="2s", off_dwell="3s"))
        make = lambda: self._bool_plant(physical, getattr(self, reader))  # noqa: E731
        expected = _first_hit(make)
        assert expected is not None
        plc = make()
        reached, stats = _jump(plc, _hit, predicate_reads=frozenset({"Hit"}))
        assert reached
        assert plc.state.scan_id == expected
        assert stats["cycle_folded_scans"] > 0
        _assert_exact(plc, make)

    def test_a_dwell_from_a_cold_start_lands_on_the_reader(self) -> None:
        physical = Physical("Sensor", on_delay="5s")
        make = lambda: self._bool_plant(physical, self._contact)  # noqa: E731
        expected = _first_hit(make)
        assert expected is not None
        plc = make()
        reached, _stats = _jump(plc, _hit, predicate_reads=frozenset({"Hit"}))
        assert reached
        assert plc.state.scan_id == expected
        _assert_exact(plc, make)


class TestApproachToAKnownTag:
    def test_harness_installs_with_a_setpoint_the_program_uses(self) -> None:
        SetPt = Real("SetPt", default=50.0)

        def reader(temp: Real) -> None:
            with Rung(SetPt > 1000.0, temp >= 0.0):
                latch(Hit)

        plc = _plant(Approach("SetPt", 0.3), reader)
        plc.run_for(5.0)
        assert plc.state.tags["Temp"] > 0.0


class TestSlotFirstTouchedInsideTheRun:
    """A slot absent when the fold starts is in the ring from the scan that adds it."""

    @staticmethod
    def _run(
        body: Callable[[Any, Timer, Bool], None], watched: int, first: dict[str, Any] | None = None
    ) -> None:
        # A fresh block, folded first: an earlier PLC on it would seed the slot.
        bank = Block(f"FreshBank{next(_FRESH)}", TagType.INT, 1, 400)
        Delay = Timer.clone("Delay")

        def make() -> PLC:
            with Program(strict=False) as prog:
                with Rung(~Tog):
                    out(Tog)
                with Rung():
                    on_delay(Delay, 1_000)
                body(bank, Delay, Hit)
                with Rung():
                    on_delay(T, 40, "s")
            plc = PLC(prog, dt=0.010)
            plc.patch(first or {})
            plc.step()
            return plc

        plc = make()
        present_at_start = set(plc.state.tags)
        reached, _stats = _jump(plc, _hit, predicate_reads=frozenset({"Hit"}))
        expected = _first_hit(make)
        assert expected is not None
        assert bank[watched].name not in present_at_start
        assert reached
        assert plc.state.scan_id == expected
        # The block now holds a slot per use, so a later PLC starts with them all.
        ref = _stepped(make, plc.state.scan_id)
        fresh = {k: v for k, v in _tags(ref).items() if k not in plc.state.tags}
        assert not any(fresh.values())
        assert _tags(plc) == {k: v for k, v in _tags(ref).items() if k not in fresh}

    def test_a_ramp_held_only_in_a_slot(self) -> None:
        Ptr, A, B = Int("Ptr"), Int("A"), Int("B")

        def body(bank: Any, delay: Timer, hit: Bool) -> None:
            with Rung(delay.Done):
                copy(bank[Ptr], A)
                calc(A + 1, B)
                copy(B, bank[Ptr])
                copy(0, A)
                copy(0, B)
            with Rung(bank[Ptr] >= 500):
                latch(hit)

        self._run(body, 300, {"Ptr": 300})

    def test_an_equality_crossed_in_a_slot(self) -> None:
        Ptr, A, B = Int("Ptr"), Int("A"), Int("B")

        def body(bank: Any, delay: Timer, hit: Bool) -> None:
            with Rung(delay.Done):
                copy(bank[Ptr], A)
                calc(A + 2, B)
                copy(B, bank[Ptr])
                copy(0, A)
                copy(0, B)
            with Rung(bank[Ptr] == 1_000):
                latch(hit)

        self._run(body, 300, {"Ptr": 300})

    def test_a_shift_through_slots_reached_by_a_pointer(self) -> None:
        Ptr, Src = Int("Ptr"), Int("Src")

        def body(bank: Any, delay: Timer, hit: Bool) -> None:
            with Rung(delay.Done):
                blockcopy(bank.select(101, 109), bank.select(102, 110))
                copy(1, bank[Ptr])
            with Rung(bank[Src] > 0):
                latch(hit)

        self._run(body, 110, {"Ptr": 101, "Src": 110})
