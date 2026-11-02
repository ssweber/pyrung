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
    branch,
    calc,
    call,
    copy,
    forloop,
    latch,
    on_delay,
    out,
    rise,
    subroutine,
)
from pyrung.click import ClickBlocks
from pyrung.core.analysis.pilot.cyclefold import cycle_fold_until
from pyrung.core.harness import Coupling, Harness
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
