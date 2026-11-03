"""Folds over harness analog feedback, and cycle jumps over slots a run touches late.

A ``Ramp`` or ``Approach`` plant moves its value every scan, so a window has to end
where a rung would read that value crossed and land the value where stepping leaves
it.  A value nothing can see, or one that has settled, keeps folding.  A value still
moving does not repeat, so a cycle jump declines and the run steps.

A slot absent from the state when a cycle fold starts joins it on the first scan
that touches it; the ring has to watch it from then on.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest

from pyrung import (
    Block,
    Bool,
    Counter,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    Timer,
    blockcopy,
    branch,
    calc,
    call,
    copy,
    count_down,
    count_up,
    forloop,
    latch,
    on_delay,
    out,
    rise,
    subroutine,
    system,
    time_drum,
)
from pyrung.click import ClickBlocks
from pyrung.core.analysis.pilot.cyclefold import cycle_fold_until
from pyrung.core.harness import Coupling, Harness
from pyrung.core.physical import Approach, Physical, Pulse, Ramp
from pyrung.core.runner import PLC
from tests.core.scan_loop_counts import ScansOutsideLoop

pytestmark = pytest.mark.usefixtures("loop_from_first_scan")

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


def _kernel_scans(plc: PLC) -> ScansOutsideLoop:
    return ScansOutsideLoop(plc)


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

    def test_a_seek_does_not_fold_a_plant_with_no_harness(self) -> None:
        """A replay keeps the plant and not the harness, so the fold cannot model it.

        A single scan's step is under half an ulp here, so Temp has stopped.  A landing
        scan with the window's dt moves it.
        """
        plc = _plant(Approach(100.0, 5.0), self._copy_reader, far_s=800)
        ran = {}
        for _ in range(4_000):
            plc.step()
            ran[plc.state.scan_id] = plc.state
        replay = plc.replay_to(1_000)
        assert replay._harness is None
        assert replay._synthesis is not None and replay._synthesis.plant
        replay.run_for(30.0)
        assert replay.state.scan_id == 4_000

        replay._recent_state_cache.clear()
        replay._replay_slabs.clear()
        for scan in (2_000, 3_000, 3_999, 4_000):
            assert replay.history.at(scan).tags["Temp"].hex() == ran[scan].tags["Temp"].hex(), scan
        assert replay._last_replay_seek_stats["ordinary_folded_scans"] == 0

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

    def test_a_cycle_jump_runs_over_a_settled_plant(self) -> None:
        make = lambda: _plant(Approach(0.0, 0.3), _latch_at(1e30), toggle=True)  # noqa: E731
        plc = make()
        _reached, stats = _jump(plc, _hit, budget=3_000, predicate_reads=frozenset({"Hit"}))
        assert stats["cycle_folded_scans"] > 2_500
        _assert_exact(plc, make)

    def test_a_cycle_jump_runs_beside_a_plant_at_rest(self) -> None:
        def enable() -> None:
            with Rung(Bool("Never")):
                out(Enable)

        make = lambda: _plant(RAMP, _latch_at(1e30), toggle=True, enable=enable)  # noqa: E731
        plc = make()
        _reached, stats = _jump(plc, _hit, budget=3_000, predicate_reads=frozenset({"Hit"}))
        assert stats["cycle_folded_scans"] > 2_500
        _assert_exact(plc, make)

    def test_a_cycle_jump_runs_over_a_plant_that_repeats_with_it(self) -> None:
        def enable() -> None:
            with Rung(Tog):
                out(Enable)

        ramp = Ramp(up=1.0, down=-1.0)
        make = lambda: _plant(ramp, _latch_at(1e30), toggle=True, enable=enable)  # noqa: E731
        plc = make()
        _reached, stats = _jump(plc, _hit, budget=3_000, predicate_reads=frozenset({"Hit"}))
        assert stats["cycle_folded_scans"] > 2_500
        _assert_exact(plc, make)


class TestCycleOverAMovingPlant:
    """A plant still moving does not repeat, so the cycle is not jumped and the run steps."""

    @pytest.mark.parametrize(("profile", "threshold"), PROFILES)
    def test_jump_lands_on_the_crossing(self, profile: Any, threshold: float) -> None:
        make = lambda: _plant(profile, _latch_at(threshold), toggle=True)  # noqa: E731
        expected = _first_hit(make)
        plc = make()
        reached, stats = _jump(plc, _hit, predicate_reads=frozenset({"Hit"}))
        assert reached
        assert plc.state.scan_id == expected
        assert stats["cycle_folded_scans"] == 0
        _assert_exact(plc, make)

    def test_the_plant_is_exact_with_no_crossing(self) -> None:
        make = lambda: _plant(RAMP, _latch_at(1e30), toggle=True)  # noqa: E731
        plc = make()
        _jump(plc, _hit, predicate_reads=frozenset({"Hit"}))
        assert plc.state.tags["Temp"] == pytest.approx(0.01 * (plc.state.scan_id - 1))
        _assert_exact(plc, make)

    def test_an_enable_toggled_by_the_cycle_is_exact(self) -> None:
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


def _counter() -> Any:
    counter = Counter.clone("C")
    with Rung(Tog):
        count_up(counter, 99999).reset(Bool("R"))
    return counter.Acc


def _down_counter() -> Any:
    counter = Counter.clone("C")
    with Rung(Tog):
        count_down(counter, -99999).reset(Bool("R"))
    return counter.Acc


def _timer() -> Any:
    timer = Timer.clone("Q")
    with Rung():
        on_delay(timer, 900, "s")
    return timer.Acc


def _drum() -> Any:
    acc = Int("DAcc")
    with Rung():
        time_drum(
            outputs=[Bool("O1"), Bool("O2")],
            presets=[900, 900],
            unit="s",
            pattern=[[1, 0], [0, 1]],
            current_step=Int("Step"),
            accumulator=acc,
            completion_flag=Bool("DDone"),
        ).reset(Bool("R"))
    return acc


def _on_an_accumulator(
    source: Callable[[], Any],
    feedback: str,
    profile: Any,
    threshold: float,
    *,
    mirrored: bool = False,
) -> Callable[[], PLC]:
    """A plant writing an accumulator under a cycle the toggle keeps repeating."""

    def make() -> PLC:
        with Program(strict=False) as prog:
            with Rung(~Tog):
                out(Tog)
            with Rung():
                out(Enable)
            acc = source()
            shown = acc
            if mirrored:
                shown = Int("Mir")
                with Rung():
                    copy(acc, shown)
            with Rung(shown >= threshold):
                latch(Hit)
            with Rung():
                on_delay(T, 40, "s")
        plc = PLC(prog, dt=0.010)
        harness = Harness(plc)
        harness.set_overrides(
            {feedback: Coupling("Enable", feedback, Physical("S", profile=profile))}
        )
        harness.install()
        return plc

    return make


FAST_APPROACH = Approach(30000.0, 0.1)
SHORT_APPROACH = Approach(5000.0, 0.2)
STEEP_RAMP = Ramp(up=100.0)
ACCUMULATOR_PLANTS = [
    pytest.param(_counter, "C_Acc", FAST_APPROACH, 20000, id="counter-approach"),
    pytest.param(_counter, "C_Acc", SHORT_APPROACH, 4000, id="counter-short-approach"),
    pytest.param(_counter, "C_Acc", STEEP_RAMP, 800, id="counter-ramp"),
    pytest.param(_down_counter, "C_Acc", FAST_APPROACH, 20000, id="down-counter-approach"),
    pytest.param(_timer, "Q_Acc", FAST_APPROACH, 20000, id="timer-approach"),
    pytest.param(_timer, "Q_Acc", STEEP_RAMP, 800, id="timer-ramp"),
    pytest.param(_drum, "DAcc", FAST_APPROACH, 20000, id="drum-approach"),
    pytest.param(_drum, "DAcc", STEEP_RAMP, 800, id="drum-ramp"),
]


class TestPlantOnAnAccumulator:
    """A plant on an accumulator is no constant rate, so a jump never extrapolates it."""

    @pytest.mark.parametrize(("source", "feedback", "profile", "threshold"), ACCUMULATOR_PLANTS)
    def test_the_cycle_steps_to_the_crossing(
        self, source: Any, feedback: str, profile: Any, threshold: float
    ) -> None:
        make = _on_an_accumulator(source, feedback, profile, threshold)
        expected = _first_hit(make)
        assert expected is not None
        plc = make()
        reached, stats = _jump(plc, _hit, predicate_reads=frozenset({"Hit"}))
        assert reached
        assert plc.state.scan_id == expected
        assert stats["cycle_folded_scans"] == 0
        _assert_exact(plc, make)

    @pytest.mark.parametrize(
        ("source", "feedback"),
        [
            pytest.param(_counter, "C_Acc", id="counter"),
            pytest.param(_timer, "Q_Acc", id="timer"),
            pytest.param(_drum, "DAcc", id="drum"),
        ],
    )
    def test_a_copy_of_it_steps_too(self, source: Any, feedback: str) -> None:
        make = _on_an_accumulator(source, feedback, SHORT_APPROACH, 4000, mirrored=True)
        expected = _first_hit(make)
        assert expected is not None
        plc = make()
        reached, stats = _jump(plc, _hit, predicate_reads=frozenset({"Hit"}))
        assert reached
        assert plc.state.scan_id == expected
        assert stats["cycle_folded_scans"] == 0
        _assert_exact(plc, make)

    @pytest.mark.parametrize(("source", "feedback", "profile", "threshold"), ACCUMULATOR_PLANTS)
    def test_a_plain_fold_lands_on_the_crossing(
        self, source: Any, feedback: str, profile: Any, threshold: float
    ) -> None:
        make = _on_an_accumulator(source, feedback, profile, threshold)
        expected = _first_hit(make)
        plc = make()
        plc.run_until(Hit, max_cycles=3_000)
        assert plc.state.scan_id == expected
        _assert_exact(plc, make)


class TestAPlantLinkedToAnAccumulator:
    """A plant at rest repeats, and its link on an accumulator bounds the jump to it."""

    @staticmethod
    def _make(link: str, source: Callable[[], Any], threshold: float) -> Callable[[], PLC]:
        def make() -> PLC:
            temp = Real("Temp", physical=Physical("S", profile=Ramp(up=1.0, down=-0.5)), link=link)
            with Program(strict=False) as prog:
                with Rung(~Tog):
                    out(Tog)
                source()
                with Rung(temp >= threshold):
                    latch(Hit)
            plc = PLC(prog, dt=0.010)
            Harness(plc).install()
            return plc

        return make

    @pytest.mark.parametrize(
        ("link", "source", "threshold", "first", "jumped"),
        [
            pytest.param("C_Acc:300", _counter, 0.005, 600, 590, id="counter"),
            pytest.param("C_Acc:50", _counter, 0.01, 100, 90, id="counter-near"),
            pytest.param("Q_Acc:5", _timer, 0.5, 550, 490, id="timer"),
        ],
    )
    def test_the_jump_reaches_the_link(
        self, link: str, source: Any, threshold: float, first: int, jumped: int
    ) -> None:
        make = self._make(link, source, threshold)
        assert _first_hit(make) == first
        plc = make()
        reached, stats = _jump(plc, _hit, predicate_reads=frozenset({"Hit"}))
        assert reached
        assert plc.state.scan_id == first
        assert stats["cycle_folded_scans"] == jumped
        _assert_exact(plc, make)

    def test_the_plant_is_exact_after_the_link_passes(self) -> None:
        make = self._make("C_Acc:300", _counter, 1e30)
        plc = make()
        _jump(plc, _hit, budget=2_600, predicate_reads=frozenset({"Hit"}))
        reference = _stepped(make, plc.state.scan_id)
        assert _tags(plc) == _tags(reference)
        assert plc.state.tags["Temp"] != 0.0
        for _ in range(5):
            plc.step()
            reference.step()
        assert _tags(plc) == _tags(reference)


class TestDeclaredPredicateOnTheFeedback:
    """A declared read of a plant with no comparison leaves only its value to go by."""

    @staticmethod
    def _past_five(state: Any) -> bool:
        return state.tags.get("Temp", 0.0) >= 5.0

    @pytest.mark.parametrize("toggle", [False, True])
    def test_it_ends_the_run_on_the_crossing(self, toggle: bool) -> None:
        make = lambda: _plant(RAMP, _latch_at(1e30), toggle=toggle)  # noqa: E731
        stepped = make()
        while not self._past_five(stepped.state):
            stepped.step()
        plc = make()
        reached, _stats = _jump(plc, self._past_five, predicate_reads=frozenset({"Temp"}))
        assert reached
        assert plc.state.scan_id == stepped.state.scan_id
        assert _tags(plc) == _tags(stepped)

    def test_a_comparison_from_the_caller_keeps_it_folding(self) -> None:
        make = lambda: _plant(RAMP, _latch_at(1e30))  # noqa: E731
        stepped = make()
        while not self._past_five(stepped.state):
            stepped.step()
        plc = make()
        counter = _kernel_scans(plc)
        reached, _stats = _jump(
            plc,
            self._past_five,
            predicate_reads=frozenset({"Temp"}),
            extra_comparisons={"Temp": (("ge", 5.0),)},
        )
        assert reached
        assert plc.state.scan_id == stepped.state.scan_id
        assert counter["n"] < 50
        assert _tags(plc) == _tags(stepped)


def _banked(
    reader: Callable[[Any, Int], None],
    *,
    toggle: bool = False,
    named: bool = True,
    size: int = 8,
    slot: int = 2,
    profile: Any = RAMP,
    enable: bool = True,
) -> Callable[[], PLC]:
    """A plant in a block slot, *reader*'s rungs, and a far timer.

    With *named* a far comparison names the slot, which is what declares the
    plant; otherwise an override adds it and no rung names the slot.
    """

    def make() -> PLC:
        bank = Block("Bk", TagType.REAL, 1, size)
        if named:
            bank.slot(slot, physical=Physical("S", profile=profile), link="Enable")
        pointer = Int("Idx", default=slot)
        with Program(strict=False) as prog:
            if toggle:
                with Rung(~Tog):
                    out(Tog)
            if enable:
                with Rung():
                    out(Enable)
            else:
                with Rung(Bool("Never")):
                    out(Enable)
            reader(bank, pointer)
            if named:
                with Rung(bank[slot] >= 1e30):
                    out(Bool("Far"))
            with Rung():
                on_delay(T, 40, "s")
        plc = PLC(prog, dt=0.010)
        harness = Harness(plc)
        if not named:
            physical = Physical("S", profile=profile)
            harness.set_overrides({f"Bk{slot}": Coupling("Enable", f"Bk{slot}", physical)})
        harness.install()
        return plc

    return make


def _compare_through(bank: Any, pointer: Int) -> None:
    with Rung(bank[pointer] >= 5.0):
        latch(Hit)


def _compare_through_a_bounded_pointer(bank: Any, _pointer: Int) -> None:
    with Rung(bank[Int("Bounded", default=2, min=1, max=4)] >= 5.0):
        latch(Hit)


def _copy_through(bank: Any, pointer: Int) -> None:
    with Rung():
        copy(bank[pointer], Shown)
    with Rung(Shown >= 5.0):
        latch(Hit)


def _compare_in_a_loop(bank: Any, _pointer: Int) -> None:
    with Rung():
        with forloop(4) as loop:
            with Rung(bank[loop.idx + 1] >= 5.0):
                latch(Hit)


def _compare_through_two_pointers(bank: Any, pointer: Int) -> None:
    with Rung(bank[pointer + Int("Off")] >= 5.0):
        latch(Hit)


def _compare_on_a_branch(bank: Any, pointer: Int) -> None:
    with Rung():
        with branch(bank[pointer] >= 5.0):
            latch(Hit)


def _compare_in_a_subroutine(bank: Any, pointer: Int) -> None:
    with subroutine("ReadBank"):
        with Rung(bank[pointer] >= 5.0):
            latch(Hit)
    with Rung():
        call("ReadBank")


POINTER_READERS = [
    pytest.param(_compare_through, id="compare"),
    pytest.param(_compare_through_a_bounded_pointer, id="bounded"),
    pytest.param(_copy_through, id="copy"),
    pytest.param(_compare_in_a_loop, id="loop"),
    pytest.param(_compare_through_two_pointers, id="two-pointers"),
    pytest.param(_compare_on_a_branch, id="branch"),
    pytest.param(_compare_in_a_subroutine, id="subroutine"),
]


class TestPlantReadThroughAPointer:
    """``B[Idx]`` names no slot, so the read goes by the block the plant lives in."""

    @pytest.mark.parametrize("reader", POINTER_READERS)
    def test_run_until_lands_on_the_read(self, reader: Any) -> None:
        make = _banked(reader)
        expected = _first_hit(make)
        plc = make()
        plc.run_until(Hit, max_cycles=800)
        assert plc.state.scan_id == expected
        _assert_exact(plc, make)

    @pytest.mark.parametrize("reader", POINTER_READERS)
    def test_a_cycle_does_not_carry_it_past_the_read(self, reader: Any) -> None:
        make = _banked(reader, toggle=True)
        expected = _first_hit(make)
        plc = make()
        reached, _stats = _jump(plc, _hit, predicate_reads=frozenset({"Hit"}))
        assert reached
        assert plc.state.scan_id == expected
        _assert_exact(plc, make)

    def test_a_slot_no_rung_names(self) -> None:
        make = _banked(_compare_through, named=False)
        expected = _first_hit(make)
        plc = make()
        plc.run_until(Hit, max_cycles=800)
        assert plc.state.scan_id == expected
        _assert_exact(plc, make)

    def test_a_block_too_big_to_list(self) -> None:
        make = _banked(_compare_through_two_pointers, size=2_000, slot=1_500)
        expected = _first_hit(make)
        plc = make()
        plc.run_until(Hit, max_cycles=800)
        assert plc.state.scan_id == expected
        _assert_exact(plc, make)

    def test_a_timer_behind_the_read_runs_exact(self) -> None:
        def reader(bank: Any, pointer: Int) -> None:
            with Rung(bank[pointer] >= 5.0):
                on_delay(Win, 99, "s")
            with Rung(Win.Acc > 0):
                latch(Hit)

        make = _banked(reader)
        plc = make()
        plc.run_for(8.0)
        assert plc.state.tags["Hit"] is True
        _assert_exact(plc, make)

    def test_an_instruction_condition_runs_exact(self) -> None:
        def reader(bank: Any, pointer: Int) -> None:
            with Rung():
                on_delay(Win, 99, "s").reset(bank[pointer] >= 5.0)

        make = _banked(reader)
        plc = make()
        plc.run_for(8.0)
        _assert_exact(plc, make)
        ref = _stepped(make, plc.state.scan_id)
        assert plc.state.memory.get("_frac:Win_Acc") == ref.state.memory.get("_frac:Win_Acc")

    def test_compiled_replay_seek_matches(self) -> None:
        def reader(bank: Any, pointer: Int) -> None:
            with Rung(bank[pointer] >= 14.5):
                latch(Hit)

        plc = _banked(reader)()
        for _ in range(1_600):
            plc.step()
        for target in (1_449, 1_460, 1_599):
            seek = plc._replay_seek(target)
            ref = plc.replay_to(target)
            stats = plc._last_replay_seek_stats
            assert stats["kernel_scans"] == stats["logical_scans"]
            assert seek.state.scan_id == ref.state.scan_id
            assert _tags(seek) == _tags(ref)

    @pytest.mark.parametrize("kind", ["int", "real"])
    def test_a_tag_mapped_onto_the_bank(self, kind: str) -> None:
        def make() -> PLC:
            blocks = ClickBlocks()
            if kind == "int":
                bank = blocks.ds
                ramp = Physical("S", profile=Ramp(up=130.0))
                temp: Any = Int("Mapped", physical=ramp, link="Enable")
                threshold: float = 500
            else:
                bank = blocks.df
                temp = Real("Mapped", physical=Physical("S", profile=RAMP), link="Enable")
                threshold = 5.0
            temp.map_to(bank[2])
            pointer = Int("Idx", default=2)
            with Program(strict=False) as prog:
                with Rung():
                    out(Enable)
                with Rung(bank[pointer] >= threshold):
                    latch(Hit)
                with Rung():
                    on_delay(T, 40, "s")
            plc = PLC(prog, dt=0.010)
            Harness(plc).install()
            return plc

        expected = _first_hit(make)
        plc = make()
        plc.run_until(Hit, max_cycles=800)
        assert plc.state.scan_id == expected
        _assert_exact(plc, make)


class TestPointerReadsKeepFolding:
    """A pointer that cannot show a moving plant is not a reason to step."""

    @staticmethod
    def _folds(make: Callable[[], PLC]) -> None:
        plc = make()
        counter = _kernel_scans(plc)
        plc.run_for(8.0)
        assert counter["n"] < 50
        _assert_exact(plc, make)

    def test_a_read_nothing_can_see(self) -> None:
        def reader(bank: Any, pointer: Int) -> None:
            with Rung():
                copy(bank[pointer], Shown)

        self._folds(_banked(reader))

    def test_a_pointer_into_another_block(self) -> None:
        def reader(_bank: Any, pointer: Int) -> None:
            other = Block("Ot", TagType.REAL, 1, 8)
            with Rung(other[pointer] >= 5.0):
                latch(Hit)

        self._folds(_banked(reader))

    def test_a_write_through_a_pointer(self) -> None:
        def reader(bank: Any, pointer: Int) -> None:
            with Rung(Bool("Never")):
                copy(0.0, bank[pointer])

        self._folds(_banked(reader))

    def test_a_settled_plant(self) -> None:
        self._folds(_banked(_compare_through, profile=Approach(0.0, 0.3)))

    def test_a_plant_at_rest(self) -> None:
        self._folds(_banked(_compare_through, enable=False))

    def test_a_settled_plant_under_a_cycle_jumps(self) -> None:
        make = _banked(_compare_through, profile=Approach(0.0, 0.3), toggle=True)
        plc = make()
        _reached, stats = _jump(plc, _hit, predicate_reads=frozenset({"Hit"}))
        assert stats["cycle_folded_scans"] > 2_500
        _assert_exact(plc, make)


def _behind_a_pointer(
    writer: Callable[[Any], None],
    *,
    plant: bool = True,
    kind: TagType = TagType.REAL,
) -> Callable[[], PLC]:
    """*writer* fills ``Cb[3]`` and rungs read that slot as ``Cb[Jx]``."""

    def make() -> PLC:
        slots = Block("Cb", kind, 1, 8)
        index = Int("Jx", default=3)
        with Program(strict=False) as prog:
            if plant:
                with Rung():
                    out(Enable)
            writer(slots)
            with Rung(slots[index] >= 5):
                latch(Hit)
            with Rung(slots[index] >= 5):
                on_delay(Timer.clone("Q"), 99, "s")
            with Rung():
                on_delay(T, 40, "s")
        plc = PLC(prog, dt=0.010)
        Harness(plc).install()
        return plc

    return make


def _copy_a_plant_through_a_pointer(slots: Any) -> None:
    bank = Block("Bk", TagType.REAL, 1, 8)
    bank.slot(2, physical=Physical("S", profile=Ramp(up=1.0)), link="Enable")
    with Rung():
        copy(bank[Int("Idx", default=2)], slots[3])
    with Rung(bank[2] >= 1e30):
        out(Bool("Far"))


def _copy_a_plant_slot(slots: Any) -> None:
    bank = Block("Bk", TagType.REAL, 1, 8)
    bank.slot(2, physical=Physical("S", profile=Ramp(up=1.0)), link="Enable")
    with Rung():
        copy(bank[2], slots[3])
    with Rung(bank[2] >= 1e30):
        out(Bool("Far"))


def _copy_a_plant(slots: Any) -> None:
    with Rung():
        copy(_temp(Ramp(up=1.0)), slots[3])


def _copy_an_accumulator(slots: Any) -> None:
    timer = Timer.clone("D")
    with Rung():
        on_delay(timer, 90, "s")
    with Rung():
        copy(timer.Acc, slots[3])


def _count_in_a_slot(slots: Any) -> None:
    with Rung():
        calc(slots[3] + 1, slots[3])


REBUILT_THROUGH_A_POINTER = [
    pytest.param(_copy_a_plant_through_a_pointer, True, TagType.REAL, id="plant-by-pointer"),
    pytest.param(_copy_a_plant_slot, True, TagType.REAL, id="plant-slot"),
    pytest.param(_copy_a_plant, True, TagType.REAL, id="plant-tag"),
    pytest.param(_copy_an_accumulator, False, TagType.INT, id="timer"),
    pytest.param(_count_in_a_slot, False, TagType.INT, id="self-calc"),
]


class TestOutputReadThroughAPointer:
    """A slot a pointer can reach has a reader, whatever writes it and however."""

    @pytest.mark.parametrize(("writer", "plant", "kind"), REBUILT_THROUGH_A_POINTER)
    def test_run_until_stops_on_the_crossing(self, writer: Any, plant: bool, kind: TagType) -> None:
        make = _behind_a_pointer(writer, plant=plant, kind=kind)
        expected = _first_hit(make, budget=4_000)
        assert expected is not None
        plc = make()
        plc.run_until(Hit, max_cycles=4_000)
        assert plc.state.scan_id == expected
        _assert_exact(plc, make)

    @pytest.mark.parametrize(("writer", "plant", "kind"), REBUILT_THROUGH_A_POINTER)
    def test_run_for_is_exact(self, writer: Any, plant: bool, kind: TagType) -> None:
        make = _behind_a_pointer(writer, plant=plant, kind=kind)
        plc = make()
        plc.run_for(30.0)
        _assert_exact(plc, make)

    @pytest.mark.parametrize(("writer", "plant", "kind"), REBUILT_THROUGH_A_POINTER)
    def test_a_cycle_jump_stops_on_the_crossing(
        self, writer: Any, plant: bool, kind: TagType
    ) -> None:
        make = _behind_a_pointer(writer, plant=plant, kind=kind)
        expected = _first_hit(make, budget=4_000)
        plc = make()
        reached, _stats = _jump(plc, _hit, budget=4_000, predicate_reads=frozenset({"Hit"}))
        assert reached
        assert plc.state.scan_id == expected
        _assert_exact(plc, make)

    def test_a_mapped_accumulator(self) -> None:
        def make() -> PLC:
            blocks = ClickBlocks()
            timer = Timer.clone("D")
            timer.Acc.map_to(blocks.ds[5])
            with Program(strict=False) as prog:
                with Rung():
                    on_delay(timer, 90, "s")
                with Rung(blocks.ds[Int("Jx", default=5)] >= 5):
                    latch(Hit)
                with Rung():
                    on_delay(T, 40, "s")
            return PLC(prog, dt=0.010)

        expected = _first_hit(make, budget=4_000)
        assert expected == 500
        plc = make()
        plc.run_until(Hit, max_cycles=4_000)
        assert plc.state.scan_id == expected
        _assert_exact(plc, make)

    def test_copies_a_value_walks_through(self) -> None:
        """Each copy reads the slot the next one writes, so a value takes scans to arrive."""

        def make() -> PLC:
            slots = Block("Cb", TagType.INT, 1, 8)
            index = Int("Jx", default=1)
            timer = Timer.clone("D")
            with Program(strict=False) as prog:
                with Rung():
                    on_delay(timer, 5, "s")
                with Rung(slots[index + 3] >= 7):
                    latch(Hit)
                with Rung():
                    copy(slots[index + 2], slots[4])
                with Rung():
                    copy(slots[index + 1], slots[3])
                with Rung():
                    copy(slots[index], slots[2])
                with Rung(timer.Done):
                    copy(7, slots[1])
                with Rung():
                    on_delay(T, 40, "s")
            return PLC(prog, dt=0.010)

        expected = _first_hit(make, budget=4_000)
        assert expected is not None
        plc = make()
        plc.run_until(Hit, max_cycles=4_000)
        assert plc.state.scan_id == expected
        _assert_exact(plc, make)

    def test_a_range_through_a_pointer(self) -> None:
        def make() -> PLC:
            slots = Block("Cb", TagType.INT, 1, 8)
            shown = Block("Od", TagType.INT, 1, 2)
            index = Int("Jx", default=3)
            with Program(strict=False) as prog:
                _copy_an_accumulator(slots)
                with Rung():
                    blockcopy(slots.select(index, index + 1), shown.select(1, 2))
                with Rung(shown[1] >= 5):
                    latch(Hit)
                with Rung():
                    on_delay(T, 40, "s")
            return PLC(prog, dt=0.010)

        expected = _first_hit(make, budget=4_000)
        assert expected == 500
        plc = make()
        plc.run_until(Hit, max_cycles=4_000)
        assert plc.state.scan_id == expected
        _assert_exact(plc, make)


def _readout_behind_a_pointer(*, cycle: bool, gated: bool, through_copy: bool) -> Callable[[], PLC]:
    """``calc(Total - Timer.Acc, Cb[3])`` read as ``Cb[Jx]``: a display of a slow timer."""

    def make() -> PLC:
        slots = Block("Cb", TagType.INT, 1, 8)
        index = Int("Jx", default=3)
        total = Int("TotalT", default=60)
        go = Bool("Go", default=True)
        toggle = Bool("Tog")
        timer = Timer.clone("D")
        with Program(strict=False) as prog:
            if cycle:
                with Rung(~toggle):
                    out(toggle)
            with Rung():
                on_delay(timer, 90, "min")
            with Rung(go) if gated else Rung():
                calc(total - timer.Acc, slots[3])
            if through_copy:
                seen = Int("Seen")
                with Rung():
                    copy(slots[index], seen)
                with Rung(seen != 0, seen <= 58):
                    latch(Hit)
            else:
                with Rung(slots[index] != 0, slots[index] <= 58):
                    latch(Hit)
            with Rung():
                on_delay(T, 90, "min")
        return PLC(prog, dt=0.1)

    return make


class TestAReadoutThroughAPointer:
    """Its value holds longer than a cycle's ring is long, so only the reader can stop a jump."""

    @pytest.mark.parametrize("cycle", [False, True], ids=["plain", "cycling"])
    @pytest.mark.parametrize("gated", [False, True], ids=["always", "gated"])
    @pytest.mark.parametrize("through_copy", [False, True], ids=["contact", "copied"])
    def test_a_stop_lands_on_the_crossing(
        self, cycle: bool, gated: bool, through_copy: bool
    ) -> None:
        make = _readout_behind_a_pointer(cycle=cycle, gated=gated, through_copy=through_copy)
        expected = _first_hit(make, budget=2_000)
        assert expected is not None

        plain = make()
        plain.run_until(Hit, max_cycles=2_000)
        assert plain.state.scan_id == expected
        _assert_exact(plain, make)

        jumped = make()
        reached, _stats = _jump(jumped, _hit, budget=2_000, predicate_reads=frozenset({"Hit"}))
        assert reached
        assert jumped.state.scan_id == expected
        _assert_exact(jumped, make)


SECOND = system.rtc.second


def _mapped_slot(slots: Any) -> Int:
    """A name on ``Cb[3]``, which carries the name and not the block."""
    name = Int("Cp")
    name.map_to(slots[3])
    return name


def _read_by_contact(slots: Any, index: Int) -> None:
    with Rung(slots[index] == Int("Want")):
        latch(Hit)


def _clock_copied(slots: Any, index: Int) -> None:
    with Rung():
        copy(SECOND, _mapped_slot(slots))
    _read_by_contact(slots, index)


def _clock_calculated(slots: Any, index: Int) -> None:
    with Rung():
        calc(SECOND + 1, _mapped_slot(slots))
    _read_by_contact(slots, index)


def _clock_chained(slots: Any, index: Int) -> None:
    middle = Int("Mid")
    with Rung():
        copy(SECOND, middle)
    with Rung():
        copy(middle, _mapped_slot(slots))
    _read_by_contact(slots, index)


def _clock_read_in_a_subroutine(slots: Any, index: Int) -> None:
    with Rung():
        copy(SECOND, _mapped_slot(slots))
    with subroutine("Sb"):
        _read_by_contact(slots, index)
    with Rung():
        call("Sb")


def _clock_read_in_a_loop(slots: Any, index: Int) -> None:
    seen = Int("Seen")
    with Rung():
        copy(SECOND, _mapped_slot(slots))
    with Rung():
        with forloop(1):
            copy(slots[index], seen)
    with Rung(seen == Int("Want")):
        latch(Hit)


def _clock_read_gating_an_out(slots: Any, index: Int) -> None:
    held = Bool("Held")
    with Rung():
        copy(SECOND, _mapped_slot(slots))
    with Rung(slots[index] == Int("Want")):
        out(held)
    with Rung(held):
        latch(Hit)


def _clock_in_a_mapped_block(slots: Any, index: Int) -> None:
    names = Block("Sn", TagType.INT, 1, 4)
    names.map_to(slots.select(1, 4))
    with Rung():
        copy(SECOND, names[3])
    _read_by_contact(slots, index)


CLOCK_ON_A_MAPPED_NAME = [
    pytest.param(_clock_copied, id="copy"),
    pytest.param(_clock_calculated, id="calc"),
    pytest.param(_clock_chained, id="chain"),
    pytest.param(_clock_read_in_a_subroutine, id="subroutine"),
    pytest.param(_clock_read_in_a_loop, id="forloop"),
    pytest.param(_clock_read_gating_an_out, id="gating-out"),
    pytest.param(_clock_in_a_mapped_block, id="mapped-block"),
]


def _clock_behind_a_pointer(body: Callable[[Any, Int], None], *, cycle: bool) -> Callable[[], PLC]:
    def make() -> PLC:
        slots = Block("Cb", TagType.INT, 1, 8)
        index = Int("Jx", default=3)
        with Program(strict=False) as prog:
            if cycle:
                with Rung(~Tog):
                    out(Tog)
            body(slots, index)
            with Rung():
                on_delay(T, 400, "s")
        plc = PLC(prog, dt=0.010)
        plc.patch({"Want": 7})
        plc.set_rtc(datetime(2026, 1, 5, 12, 0, 0))
        return plc

    return make


class TestAClockCopyThroughAPointer:
    """A mirror of the clock on a mapped name has a reader only the block can show."""

    @pytest.mark.parametrize("body", CLOCK_ON_A_MAPPED_NAME)
    def test_run_until_stops_on_the_crossing(self, body: Any) -> None:
        make = _clock_behind_a_pointer(body, cycle=False)
        expected = _first_hit(make)
        assert expected is not None
        plc = make()
        plc.run_until(Hit, max_cycles=3_000)
        assert plc.state.scan_id == expected
        _assert_exact(plc, make)

    @pytest.mark.parametrize("body", CLOCK_ON_A_MAPPED_NAME)
    def test_run_for_is_exact(self, body: Any) -> None:
        make = _clock_behind_a_pointer(body, cycle=False)
        plc = make()
        plc.run_for(30.0)
        _assert_exact(plc, make)

    @pytest.mark.parametrize("body", CLOCK_ON_A_MAPPED_NAME)
    def test_a_cycle_jump_stops_on_the_crossing(self, body: Any) -> None:
        make = _clock_behind_a_pointer(body, cycle=True)
        expected = _first_hit(make)
        assert expected is not None
        plc = make()
        reached, _stats = _jump(plc, _hit, predicate_reads=frozenset({"Hit"}))
        assert reached
        assert plc.state.scan_id == expected
        _assert_exact(plc, make)

    def test_a_declared_index_keeps_the_mirror(self) -> None:
        """Bounded, the graph lists the reader and the fold takes the clock's ticks."""

        def make() -> PLC:
            slots = Block("Cb", TagType.INT, 1, 8)
            with Program(strict=False) as prog:
                _clock_copied(slots, Int("Jx", default=3, min=1, max=8))
                with Rung():
                    on_delay(T, 400, "s")
            plc = PLC(prog, dt=0.010)
            plc.patch({"Want": 7})
            plc.set_rtc(datetime(2026, 1, 5, 12, 0, 0))
            return plc

        plc = make()
        scans = _kernel_scans(plc)
        plc.run_until(Hit, max_cycles=3_000)
        assert plc.state.scan_id == _first_hit(make)
        assert scans["n"] < 100
        _assert_exact(plc, make)


class TestOutputsThatStayUnread:
    """A pointer that cannot show the output is not a reason to step."""

    @staticmethod
    def _folds(reader: Callable[[Any], None]) -> None:
        def make() -> PLC:
            slots = Block("Cb", TagType.INT, 1, 8)
            with Program(strict=False) as prog:
                _copy_an_accumulator(slots)
                reader(slots)
                with Rung():
                    on_delay(T, 40, "s")
            return PLC(prog, dt=0.010)

        plc = make()
        counter = _kernel_scans(plc)
        plc.run_for(8.0)
        assert counter["n"] < 50
        _assert_exact(plc, make)

    def test_a_write_through_a_pointer(self) -> None:
        def reader(slots: Any) -> None:
            with Rung(Bool("Never")):
                copy(0, slots[Int("Jx", default=3)])

        self._folds(reader)

    def test_a_pointer_into_another_block(self) -> None:
        def reader(_slots: Any) -> None:
            other = Block("Ot", TagType.INT, 1, 8)
            with Rung(other[Int("Jx", default=3)] >= 5):
                latch(Hit)

        self._folds(reader)

    def test_a_bounded_pointer_short_of_the_output(self) -> None:
        def reader(slots: Any) -> None:
            with Rung(slots[Int("Jx", default=5, min=5, max=6)] >= 5):
                latch(Hit)

        self._folds(reader)


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
