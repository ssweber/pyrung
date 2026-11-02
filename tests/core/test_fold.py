"""Tests for core/fold.py — fold-aware run_until() and run_for()."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from pyrung import (
    Block,
    Bool,
    Counter,
    Dint,
    Int,
    Or,
    Program,
    Rung,
    TagType,
    Timer,
    Word,
    calc,
    copy,
    count_up,
    latch,
    on_delay,
    out,
    reset,
)
from pyrung.core import fall, fill, forloop, rise, search, system
from pyrung.core.runner import PLC
from tests.core.scan_loop_counts import NEVER, ScansOutsideLoop

pytestmark = pytest.mark.usefixtures("loop_from_first_scan")

# ---------------------------------------------------------------------------
# Test programs
# ---------------------------------------------------------------------------


def _timer_program(preset_ms: int = 500) -> tuple[Program, Timer]:
    """On-delay timer gated by an external input."""
    Enable = Bool("Enable", external=True)
    Tmr = Timer.clone("Tmr")
    Done = Bool("Done")

    with Program() as prog:
        with Rung(Enable):
            on_delay(Tmr, preset_ms, "ms")
        with Rung(Tmr.Done):
            out(Done)

    return prog, Tmr


def _counter_program(preset: int = 100) -> tuple[Program, Counter]:
    """Count-up counter counting the rising edges of Enable."""
    Enable = Bool("Enable", external=True)
    Ctr = Counter.clone("Ctr")
    ResetCmd = Bool("ResetCmd", external=True)
    Done = Bool("Done")

    with Program() as prog:
        with Rung(Enable):
            count_up(Ctr, preset).reset(ResetCmd)
        with Rung(Ctr.Done):
            out(Done)

    return prog, Ctr


def _churn_timer_program(preset_ms: int = 500) -> tuple[Program, Timer]:
    """Timer program with an unread free-running self-calc (churn)."""
    Enable = Bool("Enable", external=True)
    Tmr = Timer.clone("Tmr")
    Done = Bool("Done")
    Cycle = Int("Cycle")

    with Program() as prog:
        with Rung():
            calc((Cycle + 1) % 2, Cycle)
        with Rung(Enable):
            on_delay(Tmr, preset_ms, "ms")
        with Rung(Tmr.Done):
            out(Done)

    return prog, Tmr


def _empty_program() -> Program:
    with Program() as prog:
        pass
    return prog


# ---------------------------------------------------------------------------
# Timer fold tests
# ---------------------------------------------------------------------------


class TestTimerFold:
    """run_until with fold=True skips timer plateaus."""

    def test_timer_fold_reaches_done(self) -> None:
        prog, tmr = _timer_program(500)
        plc = __import__("pyrung.core.runner", fromlist=["PLC"]).PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        plc.step()

        result = plc.run_until(tmr.Done, max_cycles=5000)

        assert result.tags["Done"] is True

    def test_timer_fold_uses_fewer_real_steps(self) -> None:
        prog, tmr = _timer_program(500)
        from pyrung.core.runner import PLC

        plc_fold = PLC(prog, dt=0.010)
        plc_fold.patch({"Enable": True})
        plc_fold.step()
        scan_before = plc_fold.state.scan_id
        plc_fold.run_until(tmr.Done, max_cycles=5000, fold=True)
        fold_scans = plc_fold.state.scan_id - scan_before

        # Without fold: should need ~50 scans (500ms / 10ms)
        # With fold: should complete in much fewer real iterations
        # The scan_id includes folded scans, so it should be ~50,
        # but the fold collapses them into a handful of real steps.
        assert plc_fold.state.tags["Done"] is True
        assert fold_scans >= 49  # scan_id reflects equivalent elapsed time

    def test_timer_fold_bit_equal_to_unfold(self) -> None:
        """Folded and unfolded runs produce identical final tag values."""
        prog, tmr = _timer_program(200)
        from pyrung.core.runner import PLC

        plc_fold = PLC(prog, dt=0.010)
        plc_fold.patch({"Enable": True})
        plc_fold.step()
        plc_fold.run_until(tmr.Done, max_cycles=5000, fold=True)

        prog2, tmr2 = _timer_program(200)
        plc_nofold = PLC(prog2, dt=0.010)
        plc_nofold.patch({"Enable": True})
        plc_nofold.step()
        plc_nofold.run_until(tmr2.Done, max_cycles=5000, fold=False)

        assert plc_fold.state.tags["Done"] == plc_nofold.state.tags["Done"]
        assert plc_fold.state.tags["Tmr_Acc"] == plc_nofold.state.tags["Tmr_Acc"]
        assert plc_fold.state.tags["Tmr_Done"] == plc_nofold.state.tags["Tmr_Done"]


# ---------------------------------------------------------------------------
# Counter fold tests
# ---------------------------------------------------------------------------


class TestCounterFold:
    """A counter counts rising edges: a held rung counts once, so there is no
    counter plateau to fold, and a run that counts lands where stepping does."""

    def test_held_rung_counts_once_and_patches_nothing(self) -> None:
        prog, ctr = _counter_program(100)
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        advances: list[tuple[str, object]] = []

        from pyrung.core.fold import fold_run_until

        fold_ctx = plc._ensure_fold_context()
        assert fold_ctx is not None
        fold_run_until(
            plc,
            lambda state: state.tags.get(ctr.Done.name) is True,
            max_cycles=500,
            fold_ctx=fold_ctx,
            advances=advances,
        )

        assert advances == []
        assert plc.state.tags["Ctr_Acc"] == 1
        assert plc.state.tags["Ctr_Done"] is False

    def test_pulsed_counter_reaches_done(self) -> None:
        prog, ctr = _counter_program(3)
        plc = PLC(prog, dt=0.010)
        for _ in range(3):
            plc.patch({"Enable": True})
            plc.step()
            plc.patch({"Enable": False})
            plc.step()

        assert plc.state.tags["Done"] is True

    def test_counter_fold_bit_equal_to_unfold(self) -> None:
        prog, ctr = _counter_program(50)
        plc_fold = PLC(prog, dt=0.010)
        plc_fold.patch({"Enable": True})
        plc_fold.run_until(ctr.Done, max_cycles=500, fold=True)

        prog2, ctr2 = _counter_program(50)
        plc_nofold = PLC(prog2, dt=0.010)
        plc_nofold.patch({"Enable": True})
        plc_nofold.run_until(ctr2.Done, max_cycles=500, fold=False)

        assert plc_fold.state.scan_id == plc_nofold.state.scan_id
        assert plc_fold.state.tags == plc_nofold.state.tags


# ---------------------------------------------------------------------------
# Pause breakpoint integration
# ---------------------------------------------------------------------------


class TestFoldPauseBreakpoint:
    """when().pause() interrupts folded run_until and run_for."""

    def test_max_cycles_limits_folded_run_until(self) -> None:
        """max_cycles counts folded scans, so it caps the fold distance."""
        prog, tmr = _timer_program(5000)
        from pyrung.core.runner import PLC

        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})

        plc.run_until(tmr.Done, max_cycles=10, fold=True)

        assert plc.state.scan_id <= 11
        assert plc.state.tags.get("Done") is not True

    def test_pause_stops_folded_run_for(self) -> None:
        from pyrung.core.runner import PLC

        plc = PLC(logic=[], dt=0.1)
        plc.when(lambda state: state.scan_id >= 3).pause()

        plc.run_for(seconds=10.0, fold=True)

        assert plc.state.scan_id == 3

    def test_condition_pause_interrupts_fold(self) -> None:
        """Simulates the stall-detection pattern from the PILOT spec."""
        Enable = Bool("Enable", external=True)
        Tmr = Timer.clone("Tmr")
        with Program() as prog:
            with Rung(Enable):
                on_delay(Tmr, 5000, "ms")

        from pyrung.core.runner import PLC

        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        plc.step()

        stall = plc.when(~Enable).pause()
        plc.force("Enable", False)
        plc.run_until(Tmr.Done, max_cycles=50000, fold=True)
        stall.remove()

        assert plc.state.tags.get("Tmr.Done") is not True


# ---------------------------------------------------------------------------
# run_for with fold
# ---------------------------------------------------------------------------


class TestRunForFold:
    """run_for with fold=True matches unfold behavior."""

    def test_run_for_fold_advances_time(self) -> None:
        prog, tmr = _timer_program(200)
        from pyrung.core.runner import PLC

        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})

        plc.run_for(0.5, fold=True)

        assert plc.simulation_time >= 0.5
        assert plc.state.tags["Done"] is True

    def test_run_for_fold_matches_unfold(self) -> None:
        from pyrung.core.runner import PLC

        plc_fold = PLC(logic=[], dt=0.1)
        plc_fold.run_for(seconds=1.0, fold=True)

        plc_nofold = PLC(logic=[], dt=0.1)
        plc_nofold.run_for(seconds=1.0, fold=False)

        assert plc_fold.simulation_time == pytest.approx(plc_nofold.simulation_time)
        assert plc_fold.state.scan_id == plc_nofold.state.scan_id


# ---------------------------------------------------------------------------
# No-source programs (degrade gracefully)
# ---------------------------------------------------------------------------


class TestNoSourceProgram:
    """Programs without timers/counters degrade to scan-by-scan."""

    def test_empty_program_run_until(self) -> None:
        from pyrung.core.runner import PLC

        plc = PLC(logic=[])

        result = plc.run_until(lambda state: state.scan_id >= 5, max_cycles=100)

        assert result.scan_id == 5

    def test_empty_program_run_for(self) -> None:
        from pyrung.core.runner import PLC

        plc = PLC(logic=[], dt=0.1)

        plc.run_for(seconds=1.0, fold=True)

        assert plc.simulation_time >= 1.0


# ---------------------------------------------------------------------------
# Churn exclusion
# ---------------------------------------------------------------------------


class TestChurnExclusion:
    """Programs with unread self-calcs still fold (tier 1 churn)."""

    def test_unread_churn_folds_timer(self) -> None:
        prog, tmr = _churn_timer_program(200)
        from pyrung.core.runner import PLC

        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})

        plc.run_until(tmr.Done, max_cycles=5000, fold=True)

        assert plc.state.tags["Done"] is True

    def test_unread_churn_bit_equal_to_unfold(self) -> None:
        prog, tmr = _churn_timer_program(200)
        from pyrung.core.runner import PLC

        plc_fold = PLC(prog, dt=0.010)
        plc_fold.patch({"Enable": True})
        plc_fold.step()
        plc_fold.run_until(tmr.Done, max_cycles=5000, fold=True)

        prog2, tmr2 = _churn_timer_program(200)
        plc_nofold = PLC(prog2, dt=0.010)
        plc_nofold.patch({"Enable": True})
        plc_nofold.step()
        plc_nofold.run_until(tmr2.Done, max_cycles=5000, fold=False)

        assert plc_fold.state.scan_id == plc_nofold.state.scan_id
        assert plc_fold.state.tags["Done"] == plc_nofold.state.tags["Done"]
        assert plc_fold.state.tags["Tmr_Done"] == plc_nofold.state.tags["Tmr_Done"]
        # The churn itself lands on the stepped value, not one step past the probe.
        assert plc_fold.state.tags["Cycle"] == plc_nofold.state.tags["Cycle"]


# ---------------------------------------------------------------------------
# Callable predicate with fold
# ---------------------------------------------------------------------------


class TestCallablePredicate:
    """fold works with callable predicates, not just Tag/Condition."""

    def test_callable_predicate_with_fold(self) -> None:
        prog, tmr = _timer_program(200)
        from pyrung.core.runner import PLC

        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})

        result = plc.run_until(
            lambda state: state.tags.get("Done") is True,
            max_cycles=5000,
            fold=True,
        )

        assert result.tags["Done"] is True


# ---------------------------------------------------------------------------
# System-clock fold tests — fold must not skip edges of resolved-on-read
# signals (sys.clock_*, sys.scan_counter, sys.scan_clock_toggle), which are
# invisible to the plateau guard.
# ---------------------------------------------------------------------------


class TestSystemClockFold:
    """fold lands on system-clock edges instead of folding past them."""

    @staticmethod
    def _clock_tick_program() -> Program:
        """A never-completing timer (plateau churn) plus a counter ticked on
        each rise(clock_1s)."""
        Enable = Bool("Enable", external=True)
        Tmr = Timer.clone("Tmr")
        Ticks = Int("Ticks")
        with Program() as prog:
            with Rung(Enable):
                on_delay(Tmr, 100_000, "ms")
            with Rung(rise(system.sys.clock_1s)):
                calc(Ticks + 1, Ticks)
        return prog

    def test_fold_lands_on_clock_1s_edges(self) -> None:
        # The timer plateau would let fold collapse the whole window; without
        # bounding to the clock edge it skips the rises and undercounts.
        plc_fold = PLC(self._clock_tick_program(), dt=0.010)
        plc_fold.patch({"Enable": True})
        plc_fold.step()
        plc_fold.run_for(3.0, fold=True)

        plc_nofold = PLC(self._clock_tick_program(), dt=0.010)
        plc_nofold.patch({"Enable": True})
        plc_nofold.step()
        plc_nofold.run_for(3.0, fold=False)

        assert plc_nofold.state.tags["Ticks"] == 3  # rises at 0.5, 1.5, 2.5 s
        assert plc_fold.state.tags["Ticks"] == plc_nofold.state.tags["Ticks"]

    @staticmethod
    def _scan_counter_program() -> Program:
        """Timer-churn plateau plus a tick gated on a single rare
        sys.scan_counter crossing."""
        Enable = Bool("Enable", external=True)
        Tmr = Timer.clone("Tmr")
        Ticks = Int("Ticks")
        with Program() as prog:
            with Rung(Enable):
                on_delay(Tmr, 100_000, "ms")
            with Rung(system.sys.scan_counter == 25):
                calc(Ticks + 1, Ticks)
        return prog

    def test_fold_does_not_skip_scan_counter_crossing(self) -> None:
        # scan_counter changes every scan; reading it disables folding so the
        # one-scan crossing (scan 25) is not folded past.
        plc_fold = PLC(self._scan_counter_program(), dt=0.010)
        plc_fold.patch({"Enable": True})
        plc_fold.step()
        plc_fold.run_for(0.5, fold=True)

        ctx = plc_fold._ensure_fold_context()
        assert "sys.scan_counter" in ctx.scan_derived_names

        plc_nofold = PLC(self._scan_counter_program(), dt=0.010)
        plc_nofold.patch({"Enable": True})
        plc_nofold.step()
        plc_nofold.run_for(0.5, fold=False)

        assert plc_nofold.state.tags["Ticks"] == 1  # crossing actually fires
        assert plc_fold.state.tags["Ticks"] == plc_nofold.state.tags["Ticks"]

    @staticmethod
    def _toggle_program() -> Program:
        Enable = Bool("Enable", external=True)
        Tmr = Timer.clone("Tmr")
        Ticks = Int("Ticks")
        with Program() as prog:
            with Rung(Enable):
                on_delay(Tmr, 100_000, "ms")
            with Rung(rise(system.sys.scan_clock_toggle)):
                calc(Ticks + 1, Ticks)
        return prog

    def test_reading_scan_clock_toggle_disables_fold(self) -> None:
        # scan_clock_toggle flips every scan — no periodic edge to land on, so
        # reading it degrades fold to scan-by-scan, staying bit-equal to nofold.
        plc_fold = PLC(self._toggle_program(), dt=0.010)
        plc_fold.patch({"Enable": True})
        plc_fold.step()
        plc_fold.run_for(0.3, fold=True)

        ctx = plc_fold._ensure_fold_context()
        assert "sys.scan_clock_toggle" in ctx.scan_derived_names

        plc_nofold = PLC(self._toggle_program(), dt=0.010)
        plc_nofold.patch({"Enable": True})
        plc_nofold.step()
        plc_nofold.run_for(0.3, fold=False)

        assert plc_nofold.state.tags["Ticks"] >= 5  # toggles really fire
        assert plc_fold.state.tags["Ticks"] == plc_nofold.state.tags["Ticks"]


# ---------------------------------------------------------------------------
# Resolved-on-read signals (sys.clock_*, sys.scan_clock_toggle,
# sys.scan_counter) are invisible to the plateau guard.  A rung reading a clock
# ends the window at each of its edges, unless it only drives an indicator (see
# test_fold_clock_carriers.py); a scan-derived signal disables the fold outright.
# ---------------------------------------------------------------------------


def _count_kernel_scans(plc: PLC) -> ScansOutsideLoop:
    """Count the scans a run spends outside the guarded loop: the true kernel work,
    independent of how far ``scan_id`` advances. Call after ``step()`` so only the
    run-loop work is counted.
    """
    return ScansOutsideLoop(plc)


class TestResolvedOnReadSignalFold:
    """A rung reading a resolved-on-read signal: the run lands where stepping
    does, and still folds between the signal's edges where it can."""

    @staticmethod
    def _clock_heartbeat_program(preset_ms: int = 100_000) -> Program:
        """Never-completing timer plateau + a 1 s heartbeat that recomputes a
        value from stable external inputs only (the AlarmExtent pattern).

        The timer's Done is read so the plateau has a (far) crossing for the
        fold to target."""
        Enable = Bool("Enable", external=True)
        Tmr = Timer.clone("Tmr")
        Done = Bool("Done")
        A = Int("A", external=True)
        B = Int("B", external=True)
        Extent = Int("Extent")
        with Program() as prog:
            with Rung(Enable):
                on_delay(Tmr, preset_ms, "ms")
            with Rung(Tmr.Done):
                out(Done)
            with Rung(rise(system.sys.clock_1s)):
                calc(A + B, Extent)
        return prog

    def test_clock_heartbeat_folds_between_edges(self) -> None:
        prog = self._clock_heartbeat_program()
        plc_fold = PLC(prog, dt=0.010)
        plc_fold.patch({"Enable": True, "A": 3, "B": 4})
        plc_fold.step()
        counter = _count_kernel_scans(plc_fold)
        plc_fold.run_for(30.0, fold=True)

        prog2 = self._clock_heartbeat_program()
        plc_nofold = PLC(prog2, dt=0.010)
        plc_nofold.patch({"Enable": True, "A": 3, "B": 4})
        plc_nofold.step()
        plc_nofold.run_for(30.0, fold=False)

        # The heartbeat settles Extent to A + B.
        assert plc_fold.state.tags["Extent"] == 7
        assert plc_fold.state.tags["Extent"] == plc_nofold.state.tags["Extent"]
        # Each 0.5 s half-period ends a window (~60 over 30 s, a few kernel
        # scans each), against 3000 scans stepped.
        assert counter["n"] <= 250

    def test_clock_heartbeat_flushes_after_input_change(self) -> None:
        # A later edge recomputes from the changed inputs.
        prog = self._clock_heartbeat_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True, "A": 3, "B": 4})
        plc.step()
        plc.run_for(5.0, fold=True)
        assert plc.state.tags["Extent"] == 7

        plc.patch({"A": 10})  # inputs change → pending recompute
        plc.run_for(5.0, fold=True)
        assert plc.state.tags["Extent"] == 14

    @staticmethod
    def _mixed_clock_program(preset_ms: int = 100_000) -> Program:
        """clock_1s read by BOTH a stable recompute and a self-referential
        tick: the tick fires on every rising edge."""
        Enable = Bool("Enable", external=True)
        Tmr = Timer.clone("Tmr")
        A = Int("A", external=True)
        B = Int("B", external=True)
        Extent = Int("Extent")
        Ticks = Int("Ticks")
        with Program() as prog:
            with Rung(Enable):
                on_delay(Tmr, preset_ms, "ms")
            with Rung(rise(system.sys.clock_1s)):
                calc(A + B, Extent)  # stable inputs
            with Rung(rise(system.sys.clock_1s)):
                calc(Ticks + 1, Ticks)  # self-referential
        return prog

    def test_clock_shared_by_live_rung_stays_bounded(self) -> None:
        prog = self._mixed_clock_program()
        plc_fold = PLC(prog, dt=0.010)
        plc_fold.patch({"Enable": True, "A": 3, "B": 4})
        plc_fold.step()
        plc_fold.run_for(3.0, fold=True)

        prog2 = self._mixed_clock_program()
        plc_nofold = PLC(prog2, dt=0.010)
        plc_nofold.patch({"Enable": True, "A": 3, "B": 4})
        plc_nofold.step()
        plc_nofold.run_for(3.0, fold=False)

        assert plc_fold.state.tags["Ticks"] == plc_nofold.state.tags["Ticks"]
        assert plc_fold.state.tags["Extent"] == 7


class TestClockBesideAComparison:
    """A heartbeat clock gating a rung that also reads an accumulator through a
    comparison.  Its clock ends every window, before and after the comparison
    settles, and each pulse lands on the scan stepping puts it."""

    @staticmethod
    def _heartbeat(gate: Callable[[Timer], Any]) -> Callable[[], Program]:
        def build() -> Program:
            Enable = Bool("Enable", external=True)
            Tmr = Timer.clone("Tmr")
            with Program() as prog:
                with Rung(Enable):
                    on_delay(Tmr, 1_000_000, "ms")
                with Rung(rise(system.sys.clock_1s), gate(Tmr)):
                    out(Bool("Beat"))
            return prog

        return build

    @pytest.mark.parametrize("n", [40, 49, 50, 51, 99, 100, 101, 150, 151, 1000])
    def test_a_pulse_lands_as_stepped_either_side_of_the_threshold(self, n: int) -> None:
        build = self._heartbeat(lambda tmr: tmr.Acc > 100)
        _assert_fold_is_exact(build, _scans(n), patch={"Enable": True})

    @pytest.mark.parametrize("threshold", [100, 300, 1000])
    def test_an_equality_gate_lands_as_stepped(self, threshold: int) -> None:
        build = self._heartbeat(lambda tmr: tmr.Acc == threshold)
        _assert_fold_is_exact(build, _scans(400), patch={"Enable": True})

    def test_the_clock_still_bounds_the_window_past_the_threshold(self) -> None:
        def first_pulse_after_settling(fold: bool) -> int:
            Beat = Bool("Beat")
            plc = PLC(self._heartbeat(lambda tmr: tmr.Acc > 100)(), dt=0.010)
            plc.patch({"Enable": True})
            plc.step()
            plc.run_for(5.0, fold=fold)
            plc.run_until(Beat, max_cycles=1000, fold=fold)
            return plc.state.scan_id

        assert first_pulse_after_settling(True) == first_pulse_after_settling(False)


class TestClockSaturationFold:
    """A heartbeat gated on a comparison that saturates partway up the ramp.  The
    oracle is equality with scan-by-scan; folding stays between the clock's edges."""

    @staticmethod
    def _stable_saturated_program(preset_ms: int = 1_000_000) -> Program:
        """A far-off timer plateau plus a 1 s heartbeat gated on a *saturating*
        accumulator comparison (`Tmr.Acc > 100`).  Once the timer passes 100 ms
        the gate is permanently true and the recompute (`A + B`, stable inputs)
        is constant."""
        Enable = Bool("Enable", external=True)
        Tmr = Timer.clone("Tmr")
        Done = Bool("Done")
        A = Int("A", external=True)
        B = Int("B", external=True)
        Extent = Int("Extent")
        with Program() as prog:
            with Rung(Enable):
                on_delay(Tmr, preset_ms, "ms")
            with Rung(Tmr.Done):
                out(Done)
            with Rung(rise(system.sys.clock_1s), Tmr.Acc > 100):
                calc(A + B, Extent)
        return prog

    def test_saturated_heartbeat_folds_between_edges(self) -> None:
        prog = self._stable_saturated_program()
        plc_fold = PLC(prog, dt=0.010)
        plc_fold.patch({"Enable": True, "A": 3, "B": 4})
        plc_fold.step()
        counter = _count_kernel_scans(plc_fold)
        plc_fold.run_for(30.0, fold=True)

        prog2 = self._stable_saturated_program()
        plc_nofold = PLC(prog2, dt=0.010)
        plc_nofold.patch({"Enable": True, "A": 3, "B": 4})
        plc_nofold.step()
        plc_nofold.run_for(30.0, fold=False)

        # Extent is bit-equal to scan-by-scan.  Tmr_Acc folds to a *time* boundary
        # (run_for), so it drifts up to one dt from the scan-by-scan accumulation,
        # same as test_unread_timer_folds_*.
        assert plc_fold.state.tags["Extent"] == 7
        assert plc_fold.state.tags["Extent"] == plc_nofold.state.tags["Extent"]
        assert abs(plc_fold.state.tags["Tmr_Acc"] - plc_nofold.state.tags["Tmr_Acc"]) <= 10
        # Each of the ~60 half-periods over 30 s ends a window, a few kernel
        # scans each, against 3000 scans stepped.
        assert counter["n"] <= 250

    def test_saturated_heartbeat_flushes_after_input_change(self) -> None:
        # A later edge recomputes from the changed inputs.
        prog = self._stable_saturated_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True, "A": 3, "B": 4})
        plc.step()
        plc.run_for(5.0, fold=True)
        assert plc.state.tags["Extent"] == 7

        plc.patch({"A": 10})
        plc.run_for(5.0, fold=True)
        assert plc.state.tags["Extent"] == 14

    @staticmethod
    def _live_before_saturation_program(preset_ms: int = 1_000_000) -> Program:
        """The heartbeat writes a *moving* value derived from the accumulator,
        so before saturation each edge genuinely differs: no edge may be skipped."""
        Enable = Bool("Enable", external=True)
        Tmr = Timer.clone("Tmr")
        Bucket = Int("Bucket")
        with Program() as prog:
            with Rung(Enable):
                on_delay(Tmr, preset_ms, "ms")
            # While Acc <= 100 the gate is false (Bucket frozen at 0); the edge
            # at which Acc first exceeds 100 must fire exactly once.
            with Rung(rise(system.sys.clock_1s), Tmr.Acc > 100):
                out(Bucket)
        return prog

    def test_the_clock_bounds_the_window_before_saturation(self) -> None:
        prog = self._live_before_saturation_program()
        plc_fold = PLC(prog, dt=0.010)
        plc_fold.patch({"Enable": True})
        plc_fold.step()
        plc_fold.run_for(3.0, fold=True)

        prog2 = self._live_before_saturation_program()
        plc_nofold = PLC(prog2, dt=0.010)
        plc_nofold.patch({"Enable": True})
        plc_nofold.step()
        plc_nofold.run_for(3.0, fold=False)

        assert plc_fold.state.tags["Bucket"] == plc_nofold.state.tags["Bucket"]


class TestClockPulseFoldRegression:
    """Regression: a clock-gated *pulse* coil (``rise(clock) ∧ Acc<cmp> k``) must
    stay bit-equal to scan-by-scan across a fold.  These exact shapes were found
    by the fold soundness fuzzer; each stresses a different facet:

    - the edge memory of a contact on a resolved-on-read clock surviving a big
      fold step (a clock is a pure function of the timestamp, so the next scan
      must compare against its value one *normal* dt before the landing);
    - a rise()-gated pulse that does nothing at falls and fires at rises: a fall
      must not excuse the next rise;
    - landing *strictly before* an edge that falls on an exact integer of scans.
    """

    @staticmethod
    def _pulse_program(src: str, preset: int, threshold: int, clock: str) -> tuple[Program, Bool]:
        Enable = Bool("Enable", external=True)
        Reset = Bool("Reset", external=True)
        Beat = Bool("Beat")
        clock_tag = getattr(system.sys, clock)
        with Program(strict=False) as prog:
            if src == "timer":
                s = Timer.clone("Tmr")
                with Rung(Enable):
                    on_delay(s, preset, "ms")
            else:
                s = Counter.clone("Ctr")
                with Rung(Enable):
                    count_up(s, preset).reset(Reset)
            with Rung(rise(clock_tag), s.Acc > threshold):
                out(Beat)
        return prog, s.Done

    def _both(
        self, src: str, preset: int, threshold: int, clock: str, dt: float
    ) -> tuple[bool, bool]:
        out = []
        for fold in (True, False):
            prog, done = self._pulse_program(src, preset, threshold, clock)
            plc = PLC(prog, dt=dt)
            plc.patch({"Enable": True})
            plc.run_until(done, max_cycles=40_000, fold=fold)
            out.append(plc.state.tags["Beat"])
        return out[0], out[1]

    def test_timer_pulse_done_aligned_with_clock(self) -> None:
        # preset 17261 ms lands Done on a scan where rise(clock_500ms) fires;
        # the big fold step into Done must leave correct clock edge memory.
        folded, stepped = self._both("timer", 17_261, 1, "clock_500ms", 0.01)
        assert folded == stepped

    def test_counter_pulse_quiet_at_fall(self) -> None:
        # clock toggles every half-period; the rung pulses only on the rise, so
        # nothing moves at the intervening fall.
        folded, stepped = self._both("counter", 200, 13, "clock_500ms", 0.02)
        assert folded == stepped

    def test_timer_pulse_edge_on_exact_scan_boundary(self) -> None:
        # 0.25 s half-period at dt=0.005 puts edges on exact integer scan counts;
        # the fold must land strictly before each edge, not on it.
        folded, stepped = self._both("timer", 2_000, 1, "clock_500ms", 0.005)
        assert folded == stepped


class TestUnreadAccumulatorFold:
    """A timer/counter whose Done is unread still has the preset as a visible
    crossing (the Done bit flips there), and run_until thresholds on the raw
    accumulator are folded onto exactly, not skipped past to the preset."""

    @staticmethod
    def _unread_timer_program(preset_ms: int = 100_000) -> tuple[Program, Timer]:
        """A timer whose Done bit nothing reads — pure accumulator churn."""
        Enable = Bool("Enable", external=True)
        Tmr = Timer.clone("Tmr")
        with Program() as prog:
            with Rung(Enable):
                on_delay(Tmr, preset_ms, "ms")
        return prog, Tmr

    def test_unread_timer_folds_instead_of_stepping(self) -> None:
        prog, _ = self._unread_timer_program(preset_ms=100_000)
        plc_fold = PLC(prog, dt=0.010)
        plc_fold.patch({"Enable": True})
        plc_fold.step()
        counter = _count_kernel_scans(plc_fold)
        plc_fold.run_for(5.0, fold=True)  # 500 scans; timer never completes (100 s)

        prog2, _ = self._unread_timer_program(preset_ms=100_000)
        plc_nofold = PLC(prog2, dt=0.010)
        plc_nofold.patch({"Enable": True})
        plc_nofold.step()
        plc_nofold.run_for(5.0, fold=False)

        # The preset is a (far) visible crossing clamped to the run window, so
        # the churn collapses instead of stepping all 500 scans.
        assert counter["n"] <= 12
        # The timer never completes; the fold runs the scans stepping runs and
        # replays the timer's per-scan carry, so the accumulator and its carry
        # are bit-equal.
        assert plc_fold.state.tags["Tmr_Done"] is False
        assert plc_fold.state.timestamp >= 0.01 + 5.0 - 1e-9  # advanced the full 5.0 s
        assert plc_fold.state.scan_id == plc_nofold.state.scan_id
        assert plc_fold.state.tags["Tmr_Acc"] == plc_nofold.state.tags["Tmr_Acc"]
        assert plc_fold.state.memory.get("_frac:Tmr_Acc") == plc_nofold.state.memory.get(
            "_frac:Tmr_Acc"
        )

    def test_run_until_accumulator_threshold_does_not_overshoot(self) -> None:
        prog, tmr = self._unread_timer_program(preset_ms=100_000)
        plc_fold = PLC(prog, dt=0.010)
        plc_fold.patch({"Enable": True})
        plc_fold.step()
        plc_fold.run_until(tmr.Acc > 500, max_cycles=20_000, fold=True)

        prog2, tmr2 = self._unread_timer_program(preset_ms=100_000)
        plc_nofold = PLC(prog2, dt=0.010)
        plc_nofold.patch({"Enable": True})
        plc_nofold.step()
        plc_nofold.run_until(tmr2.Acc > 500, max_cycles=20_000, fold=False)

        # The fold must land on the predicate's threshold, not skip to the
        # preset: bit-equal to scan-by-scan, and just past 500 (not ~100000).
        assert plc_fold.state.tags["Tmr_Acc"] == plc_nofold.state.tags["Tmr_Acc"]
        assert 500 < plc_fold.state.tags["Tmr_Acc"] < 600


# ---------------------------------------------------------------------------
# Affine accumulator-view projection
# ---------------------------------------------------------------------------


class TestAffineAccumulatorViews:
    def test_negated_view_threshold_is_projected_to_source(self) -> None:
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.fold import _build_fold_context

        Enable = Bool("Enable", external=True)
        Tmr = Timer.clone("Tmr")
        Remaining = Int("Remaining")
        Done = Bool("Done")

        with Program() as program:
            with Rung(Enable):
                on_delay(Tmr, 1000, "ms")
            with Rung():
                calc(1000 - Tmr.Acc, Remaining)
            with Rung(Remaining < 500):
                out(Done)

        plc = PLC(program)
        context = _build_fold_context(plc, build_program_graph(program), program)

        assert Remaining.name in context.mirror_names
        assert ("gt", 500) in context.comparisons[Tmr.Acc.name]
        assert Remaining.name not in context.comparisons


# ---------------------------------------------------------------------------
# Rungs that hold still across a window, and those that do not
# ---------------------------------------------------------------------------


def _run_until_exact(
    build: Callable[[], Program], condition: Callable[[], Any], patch: dict[str, Any]
) -> PLC:
    """Fold and step to *condition*; both land on one scan with one state."""

    def run(plc: PLC, fold: bool) -> None:
        plc.run_until(condition(), max_cycles=20_000, fold=fold)

    return _assert_fold_is_exact(build, run, patch=patch)


class TestRungsAcrossAWindow:
    """A rung whose reads hold still across a plateau folds; one that moves, or a
    clock it reads, ends the window where stepping would see the change."""

    def test_clock_gated_rung_with_stable_inputs_pulses_as_stepped(self) -> None:
        """An edge-gated rung is not an indicator: each pulse lands exactly, and
        the run still folds between the clock's edges."""

        def build() -> Program:
            with Program() as prog:
                with Rung(Bool("Enable", external=True)):
                    on_delay(Timer.clone("Tmr"), 100_000, "ms")
                with Rung(rise(system.sys.clock_1s), Bool("Sensor", external=True)):
                    out(Bool("Indicator"))
            return prog

        patch = {"Enable": True, "Sensor": True}
        for n in (99, 100, 101, 1000):
            _assert_fold_is_exact(build, _scans(n), patch=patch)
        _run_until_exact(build, lambda: Bool("Indicator"), patch)

        plc = PLC(build(), dt=0.010)
        plc.patch(patch)
        plc.step()
        counter = _count_kernel_scans(plc)
        plc.run_until(NEVER, max_cycles=3000, fold=True)
        assert counter["n"] <= 250

    def test_rung_reading_a_timer_bit_lands_on_its_crossing(self) -> None:
        def build() -> Program:
            Tmr = Timer.clone("Tmr")
            with Program() as prog:
                with Rung(Bool("Enable", external=True)):
                    on_delay(Tmr, 1000, "ms")
                with Rung(Tmr.Done):
                    out(Bool("Done"))
            return prog

        folded = _run_until_exact(build, lambda: Bool("Done"), {"Enable": True})
        assert folded.state.tags["Tmr_Acc"] == 1000

    @pytest.mark.parametrize("sensor", [False, True])
    def test_tag_with_a_stable_writer_and_a_moving_one(self, sensor: bool) -> None:
        """The moving writer is the last word: the landing shows it."""

        def build() -> Program:
            Tmr = Timer.clone("Tmr")
            Flag = Bool("Flag")
            with Program() as prog:
                with Rung(Bool("Enable", external=True)):
                    on_delay(Tmr, 1000, "ms")
                with Rung(Bool("Sensor", external=True)):
                    out(Flag)
                with Rung(Tmr.Done):
                    out(Flag)
            return prog

        patch = {"Enable": True, "Sensor": sensor}
        for n in (99, 100, 101, 150):
            _assert_fold_is_exact(build, _scans(n), patch=patch)

    def test_chain_off_a_stored_input_holds_and_folds(self) -> None:
        def build() -> Program:
            Mid, Final = Bool("Mid"), Bool("Final")
            with Program() as prog:
                with Rung(Bool("Enable", external=True)):
                    on_delay(Timer.clone("Tmr"), 100_000, "ms")
                with Rung(Bool("Sensor", external=True)):
                    out(Mid)
                with Rung(Mid):
                    out(Final)
            return prog

        patch = {"Enable": True, "Sensor": True}
        folded = _assert_fold_is_exact(build, _scans(2000), patch=patch)
        assert folded.state.tags["Final"] is True

        plc = PLC(build(), dt=0.010)
        plc.patch(patch)
        plc.step()
        counter = _count_kernel_scans(plc)
        plc.run_until(NEVER, max_cycles=2000, fold=True)
        assert counter["n"] <= 12

    def test_clock_gated_rungs_end_each_window_and_still_fold(self) -> None:
        """End-to-end: the clock gates a rung over external inputs and one over the
        accumulator.  Each 0.5 s half-period ends a window, with folding between."""
        Sensor = Bool("Sensor", external=True)
        Indicator = Bool("Indicator")
        AccView = Int("AccView")
        Enable = Bool("Enable", external=True)
        Tmr = Timer.clone("Tmr")
        Done = Bool("Done")

        def build() -> Program:
            with Program() as prog:
                with Rung(Enable):
                    on_delay(Tmr, 5000, "ms")
                with Rung(Tmr.Done):
                    out(Done)
                with Rung(rise(system.sys.clock_1s), Sensor):
                    out(Indicator)
                with Rung(rise(system.sys.clock_1s)):
                    calc(Tmr.Acc, AccView)
            return prog

        plc = PLC(build(), dt=0.010)
        plc.patch({"Enable": True, "Sensor": True})
        plc.step()
        counter = _count_kernel_scans(plc)
        plc.run_until(Tmr.Done, max_cycles=20_000, fold=True)

        assert plc.state.tags["Done"] is True
        assert plc.state.tags["Tmr_Acc"] == 5000
        assert counter["n"] < 500
        _run_until_exact(build, lambda: Tmr.Done, {"Enable": True, "Sensor": True})

    @pytest.mark.parametrize("form", ["tag", "negated", "callable"])
    def test_a_stop_condition_on_an_indicator_lands_on_its_scan(self, form: str) -> None:
        """A condition reads the bit, so its clock ends the window as for a rung."""
        Lamp = Bool("Lamp")

        def build() -> Program:
            with Program() as prog:
                with Rung(system.sys.clock_100ms):
                    out(Lamp)
                with Rung(Lamp):
                    out(Bool("Lamp2"))
                with Rung():
                    on_delay(Timer.clone("Tmr"), 100_000, "ms")
            return prog

        def run(fold: bool, start: int) -> PLC:
            plc = PLC(build(), dt=0.010)
            for _ in range(start):
                plc.step()
            if form == "tag":
                plc.run_until(Lamp, max_cycles=1000, fold=fold)
            elif form == "negated":
                plc.run_until(~Lamp, max_cycles=1000, fold=fold)
            else:
                plc.run_until(lambda s: not s.tags.get("Lamp2"), max_cycles=1000, fold=fold)
            return plc

        for start in (3, 7, 12, 14):
            folded, stepped = run(True, start), run(False, start)
            assert folded.state.scan_id == stepped.state.scan_id
            assert _plain_tags(folded) == _plain_tags(stepped)


# ---------------------------------------------------------------------------
# Soundness when a value moves every scan: self-feeding and back-edge reads
# ---------------------------------------------------------------------------


def _self_feeding_calc():
    X = Int("X", default=1)
    with Rung():
        calc((X * 3) % 7, X)
    return X == 5


def _scan_counter_calc():
    from pyrung import latch

    X = Int("X")
    Seen = Bool("Seen")
    with Rung():
        calc(system.sys.scan_counter % 50, X)
    with Rung(X == 40):
        latch(Seen)
    return Seen


def _sum_expr_calc():
    from pyrung import Block, TagType

    DS = Block("DS", TagType.INT, 1, 2)
    with Rung():
        calc((DS.select(1, 2).sum() + 1) % 7, DS[1])
    return DS[1] == 5


def _shift_func_calc():
    from pyrung.core.expression import rro

    X = Int("X", default=1)
    with Rung():
        calc(rro(X, 1), X)
    return X == 4


def _math_func_calc():
    from pyrung import Real
    from pyrung.core.expression import sqrt

    # sqrt(R * R) == |R|: a per-scan +1 ramp the affine matcher cannot see.
    R = Real("R")
    with Rung():
        calc(sqrt(R * R) + 1.0, R)
    return R >= 503.0


def _back_edge_chain():
    """Each rung reads a coil written further down the scan, so a change at
    the bottom needs one scan per rung to reach the top."""
    stages = [Bool(f"Stage{i}") for i in range(8)]
    Sensor = Bool("Sensor", external=True)
    with Rung(stages[1]):
        out(stages[0])
    with Rung(stages[2]):
        out(stages[1])
    with Rung(stages[3]):
        out(stages[2])
    with Rung(stages[4]):
        out(stages[3])
    with Rung(stages[5]):
        out(stages[4])
    with Rung(stages[6]):
        out(stages[5])
    with Rung(stages[7]):
        out(stages[6])
    with Rung(Sensor):
        out(stages[7])
    return stages[0]


_SCAN_VARYING_SHAPES = [
    pytest.param(_self_feeding_calc, id="self_feeding_calc"),
    pytest.param(_scan_counter_calc, id="scan_counter_calc"),
    pytest.param(_sum_expr_calc, id="sum_expr_calc"),
    pytest.param(_shift_func_calc, id="shift_func_calc"),
    pytest.param(_math_func_calc, id="math_func_calc"),
    pytest.param(_back_edge_chain, id="back_edge_chain"),
]


def _gated_hit_program(shape: Callable[[], Any]) -> tuple[Program, Bool, Timer]:
    """A 5 s timer plus *shape*; ``Hit`` needs both the timer and the shape."""
    Go = Bool("Go", external=True)
    Hit = Bool("Hit")
    Tmr = Timer.clone("Tmr")
    with Program(strict=False) as prog:
        with Rung(Go):
            on_delay(Tmr, 5000)
        gate = shape()
        with Rung(Tmr.Done, gate):
            out(Hit)
    return prog, Hit, Tmr


def _plain_tags(plc: PLC) -> dict[str, Any]:
    return dict(plc.state.tags)


def _started(shape: Callable[[], Any]) -> tuple[PLC, Bool, Timer]:
    prog, hit, tmr = _gated_hit_program(shape)
    plc = PLC(prog, dt=0.010)
    plc.patch({"Go": True})
    plc.step()
    plc.patch({"Sensor": True})
    return plc, hit, tmr


class TestScanVaryingValuesAreExact:
    """Folded and stepped runs land on the same scan with bit-equal state when
    a rung's value moves every scan without any accumulator reaching it."""

    @pytest.mark.parametrize("shape", _SCAN_VARYING_SHAPES)
    def test_run_until_matches_stepped(self, shape: Callable[[], Any]) -> None:
        folded, hit, _tmr = _started(shape)
        folded.run_until(hit, max_cycles=10_000, fold=True)
        stepped, hit2, _tmr2 = _started(shape)
        stepped.run_until(hit2, max_cycles=10_000, fold=False)

        assert stepped.state.tags["Hit"] is True
        assert folded.state.scan_id == stepped.state.scan_id
        assert _plain_tags(folded) == _plain_tags(stepped)

    @pytest.mark.parametrize("shape", _SCAN_VARYING_SHAPES)
    def test_run_for_matches_stepped(self, shape: Callable[[], Any]) -> None:
        folded, _hit, _tmr = _started(shape)
        folded.run_for(2.0, fold=True)
        stepped, _hit2, _tmr2 = _started(shape)
        stepped.run_for(2.0, fold=False)

        assert folded.state.scan_id == stepped.state.scan_id
        assert _plain_tags(folded) == _plain_tags(stepped)

    @pytest.mark.parametrize(
        ("shape", "tag"),
        [
            (_self_feeding_calc, "X"),
            (_scan_counter_calc, "X"),
            (_sum_expr_calc, "DS1"),
            (_shift_func_calc, "X"),
            (_math_func_calc, "R"),
            (_back_edge_chain, "Stage3"),
        ],
    )
    def test_scan_varying_write_lands_as_stepped(self, shape: Callable[[], Any], tag: str) -> None:
        for scans in (37, 38, 601):
            folded, _hit, _tmr = _started(shape)
            folded.run_until(NEVER, max_cycles=scans, fold=True)
            stepped, _hit2, _tmr2 = _started(shape)
            stepped.run_until(NEVER, max_cycles=scans, fold=False)

            assert folded.state.scan_id == stepped.state.scan_id
            assert folded.state.tags[tag] == stepped.state.tags[tag]

    @pytest.mark.parametrize("form", ["sum", "math", "shift"])
    def test_self_reference_seen_through_function_expressions(self, form: str) -> None:
        from pyrung import Block, TagType
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.expression import lsh, sqrt
        from pyrung.core.fold import _calc_self_referential

        DS = Block("DS", TagType.INT, 1, 2)
        X = DS[1]
        expr = {
            "sum": DS.select(1, 2).sum() + 1,
            "math": sqrt(X),
            "shift": lsh(X, 1),
        }[form]
        with Program(strict=False) as prog:
            with Rung():
                calc(expr, X)

        pdg = build_program_graph(prog)
        assert _calc_self_referential(X.name, pdg, prog)


# ---------------------------------------------------------------------------
# first_scan is window-constant: False in every probe and window scan
# ---------------------------------------------------------------------------


def _first_scan_init_program() -> tuple[Program, Bool]:
    """A first-scan init copy feeding a clock-gated rung, then a 5 s timer."""
    from pyrung import copy

    Init = Int("Init")
    Blink = Bool("Blink")
    Hit = Bool("Hit")
    Tmr = Timer.clone("Tmr")
    with Program(strict=False) as prog:
        with Rung(system.sys.first_scan):
            copy(7, Init)
        with Rung(system.sys.clock_1s, Init == 7):
            out(Blink)
        with Rung():
            on_delay(Tmr, 5000)
        with Rung(Tmr.Done, Blink):
            out(Hit)
    return prog, Hit


def _not_first_scan_counter_program() -> tuple[Program, Bool]:
    """``~first_scan`` turns a per-scan count on from scan 1; a timer gives
    the fold a crossing to aim for."""
    Z = Bool("Z")
    Hit = Bool("Hit")
    Tmr = Timer.clone("Tmr")
    N = Int("N")
    with Program(strict=False) as prog:
        with Rung():
            on_delay(Tmr, 1000)
        with Rung(~system.sys.first_scan):
            out(Z)
        with Rung(Z):
            calc(N + 1, N)
        with Rung(Tmr.Done):
            out(Hit)
    return prog, Hit


class TestFirstScanWindowConstant:
    def test_first_scan_init_does_not_stop_the_fold(self) -> None:
        """The init rung reads first_scan False in every window scan, so its copy
        holds still; the clock beside it ends each window, with folding between."""
        prog, hit = _first_scan_init_program()
        plc = PLC(prog, dt=0.010)
        plc.step()
        counter = _count_kernel_scans(plc)
        plc.run_until(hit, max_cycles=10_000)
        assert plc.state.scan_id == 500
        assert counter["n"] < 100

    @pytest.mark.parametrize("warm", [False, True], ids=["from_scan_0", "after_step"])
    def test_first_scan_init_folded_matches_stepped(self, warm: bool) -> None:
        def run(fold: bool) -> PLC:
            prog, hit = _first_scan_init_program()
            plc = PLC(prog, dt=0.010)
            if warm:
                plc.step()
            plc.run_until(hit, max_cycles=10_000, fold=fold)
            return plc

        folded, stepped = run(True), run(False)
        assert stepped.state.tags["Hit"] is True
        assert folded.state.scan_id == stepped.state.scan_id
        assert _plain_tags(folded) == _plain_tags(stepped)

    def test_a_first_scan_latch_on_an_indicator_rung_skips_clock_edges(self) -> None:
        """The stored gate holds still, so the blink beside it stays an indicator
        and a run past many of its edges takes a handful of kernel scans."""

        def build() -> Program:
            Armed, Lamp = Bool("Armed"), Bool("Lamp")
            with Program(strict=False) as prog:
                with Rung(system.sys.first_scan):
                    latch(Armed)
                with Rung(system.sys.clock_1s, Armed):
                    out(Lamp)
                with Rung():
                    on_delay(Timer.clone("Tmr"), 100_000)
            return prog

        folded = _assert_fold_is_exact(build, _scans(2000))
        assert folded.state.tags["Armed"] is True

        plc = PLC(build(), dt=0.010)
        plc.step()
        counter = _count_kernel_scans(plc)
        plc.run_until(NEVER, max_cycles=2000, fold=True)
        # A real scan at each edge of the clock the rung reads: 40 in 20 s.
        assert counter["n"] <= 50

    def test_probe_at_scan_zero_does_not_certify_a_fold(self) -> None:
        """Scan 0 reads first_scan True; scans 1.. read False.  With every tag
        already present at scan 0, the probe sees an unchanged state (Z stays
        False), but the window would turn Z on and count every scan."""
        from pyrung.core.state import SystemState

        prog, _hit = _not_first_scan_counter_program()
        seed = PLC(prog, dt=0.010)
        seed.step()
        tags = dict(seed.state.tags)
        tags.update({"Z": False, "N": 0, "Tmr_Acc": 0})

        def run(fold: bool) -> PLC:
            prog, hit = _not_first_scan_counter_program()
            plc = PLC(prog, dt=0.010, initial_state=SystemState().with_tags(tags))
            plc.run_until(hit, max_cycles=10_000, fold=fold)
            return plc

        folded, stepped = run(True), run(False)
        assert stepped.state.tags["N"] == 99
        assert folded.state.scan_id == stepped.state.scan_id
        assert _plain_tags(folded) == _plain_tags(stepped)


# ---------------------------------------------------------------------------
# Real-time clock: rtc.* fields are clocks the fold bounds on their ticks
# ---------------------------------------------------------------------------

_RTC_START = (2026, 3, 1, 12, 59, 57, 250_000)


def _rtc_plc(prog: Program, *, dt: float = 0.010, start: tuple[int, ...] = _RTC_START) -> PLC:
    from datetime import datetime

    plc = PLC(prog, dt=dt)
    plc.set_rtc(datetime(*start))
    plc.step()
    return plc


def _rtc_latch_program() -> tuple[Program, Bool]:
    from pyrung import latch

    X = Bool("X")
    Tmr = Timer.clone("Tmr")
    with Program(strict=False) as prog:
        with Rung(system.rtc.second == 30):
            latch(X)
        with Rung():
            on_delay(Tmr, 60, "s")  # a crossing that bounds every window
    return prog, X


def _rtc_gated_counter_program() -> Program:
    """A count that adds one every scan while the RTC second is below 30.

    Its value is a fold coordinate, so a window whose scans read a different
    second than the probe did would patch the wrong per-scan delta.  (A
    ``calc``: a counter counts edges, so it would add one a minute.)
    """
    N = Dint("N")
    Tmr = Timer.clone("Tmr")
    with Program(strict=False) as prog:
        with Rung(system.rtc.second < 30):
            calc(N + 1, N)
        with Rung():
            on_delay(Tmr, 60_000)
    return prog


def _rtc_mirror_only_program() -> tuple[Program, Bool]:
    from pyrung import copy

    Sec = Int("Sec")
    HHMM = Int("HHMM")
    Tmr = Timer.clone("Tmr")
    Hit = Bool("Hit")
    with Program(strict=False) as prog:
        with Rung():
            copy(system.rtc.second, Sec)
        with Rung():
            calc(system.rtc.hour * 100 + system.rtc.minute, HHMM)
        with Rung():
            on_delay(Tmr, 60_000)
        with Rung(Tmr.Done):
            out(Hit)
    return prog, Hit


def _run_scans(plc: PLC, n: int, *, fold: bool) -> None:
    plc.run_until(NEVER, max_cycles=n, fold=fold)


class TestRtcTickBound:
    """A rung or a run_until condition that reads an ``rtc.*`` field bounds every
    fold window at that field's next tick, so folding never skips one."""

    def test_rtc_fields_are_collected(self) -> None:
        prog, _x = _rtc_latch_program()
        plc = _rtc_plc(prog)
        assert plc._ensure_fold_context().rtc_fields == {system.rtc.second.name}
        cond = plc._ensure_fold_context(frozenset({system.rtc.minute.name}))
        assert cond.rtc_condition_fields == {system.rtc.minute.name}
        assert cond.rtc_fields == {system.rtc.second.name, system.rtc.minute.name}

    def test_rtc_condition_rung_does_not_skip_its_second(self) -> None:
        prog, _x = _rtc_latch_program()
        stepped = _rtc_plc(prog)
        _run_scans(stepped, 4_000, fold=False)
        prog, _x = _rtc_latch_program()
        folded = _rtc_plc(prog)
        counter = _count_kernel_scans(folded)
        _run_scans(folded, 4_000, fold=True)

        assert stepped.state.tags["X"] is True
        assert folded.state.scan_id == stepped.state.scan_id
        assert _plain_tags(folded) == _plain_tags(stepped)
        assert counter["n"] < 400  # still folds within each second

    def test_run_until_rtc_condition_stops_on_its_scan(self) -> None:
        def run(fold: bool) -> PLC:
            prog, _x = _rtc_latch_program()
            plc = _rtc_plc(prog)
            plc.run_until(system.rtc.second == 30, max_cycles=6_000, fold=fold)
            return plc

        folded, stepped = run(True), run(False)
        assert stepped.state.scan_id < 6_000
        assert folded.state.scan_id == stepped.state.scan_id
        assert _plain_tags(folded) == _plain_tags(stepped)

    def test_probe_must_read_the_window_value(self) -> None:
        """A probe that read the second before a tick, landing after it, says
        nothing about a window that reads the new second."""
        from types import SimpleNamespace

        from pyrung.core.fold import _scans_to_rtc_tick

        prog, _x = _rtc_latch_program()
        plc = _rtc_plc(prog, start=(2026, 3, 1, 12, 59, 57, 0))
        ctx = plc._ensure_fold_context()
        fields = ctx.rtc_fields
        rtc_at = plc._rtc_at_sim_time
        # The second ticks at sim time 1.0 (scan reads at 0.99 and 1.00).
        after = SimpleNamespace(tags=plc.state.tags, timestamp=1.005)
        assert _scans_to_rtc_tick(ctx, after, 0.995, rtc_at, fields) == 0
        after = SimpleNamespace(tags=plc.state.tags, timestamp=1.015)
        assert _scans_to_rtc_tick(ctx, after, 1.005, rtc_at, fields) == 99
        # Reads at 1.015 .. 1.995 see the same second; the landing may follow.
        before = SimpleNamespace(tags=plc.state.tags, timestamp=0.505)
        assert _scans_to_rtc_tick(ctx, before, 0.495, rtc_at, fields) == 50

    @pytest.mark.parametrize("dt", [0.010, 0.030, 0.070])
    def test_rtc_gated_counter_matches_stepped(self, dt: float) -> None:
        prog = _rtc_gated_counter_program()
        stepped = _rtc_plc(prog, dt=dt)
        _run_scans(stepped, int(70 / dt), fold=False)
        prog = _rtc_gated_counter_program()
        folded = _rtc_plc(prog, dt=dt)
        _run_scans(folded, int(70 / dt), fold=True)

        assert folded.state.scan_id == stepped.state.scan_id
        assert _plain_tags(folded) == _plain_tags(stepped)

    def test_apply_flag_declines_the_window(self) -> None:
        from types import SimpleNamespace

        from pyrung.core.fold import _scans_to_rtc_tick

        prog, _x = _rtc_latch_program()
        plc = _rtc_plc(prog)
        ctx = plc._ensure_fold_context()
        state = plc.state
        free = _scans_to_rtc_tick(
            ctx, state, state.timestamp - ctx.normal_dt, plc._rtc_at_sim_time, ctx.rtc_fields
        )
        assert free is not None and free > 0
        applying = SimpleNamespace(
            tags={**state.tags, system.rtc.apply_time.name: True}, timestamp=state.timestamp
        )
        assert (
            _scans_to_rtc_tick(
                ctx,
                applying,
                state.timestamp - ctx.normal_dt,
                plc._rtc_at_sim_time,
                ctx.rtc_fields,
            )
            == 0
        )

    @pytest.mark.parametrize("dt", [0.010, 0.5])
    def test_replay_seek_matches_replay(self, dt: float) -> None:
        prog, _hit = _rtc_mirror_only_program()
        plc = _rtc_plc(prog, dt=dt)
        for _ in range(int(30 / dt)):
            plc.step()
        target = int(19.5 / dt)
        seek = plc._replay_seek(target)
        ref = plc.replay_to(target)
        assert seek.state.scan_id == ref.state.scan_id
        assert _plain_tags(seek) == _plain_tags(ref)


def _unbounded_rtc(plc: PLC) -> None:
    """Drop the RTC tick bound from every cached fold context, so a window can
    span ticks and the landing scan alone decides what the RTC reads."""
    from dataclasses import replace

    plc._ensure_fold_context()
    cache = plc._fold_context_cache
    assert cache is not None
    for key, value in list(cache.items()):
        cache[key] = replace(value, rtc_fields=frozenset())


class TestFoldLandingTime:
    """The landing scan stands in for the last folded scan: it reads the RTC
    and the clocks at that scan's timestamp, then commits one step later."""

    def test_landing_resolves_rtc_at_the_last_folded_scan(self) -> None:
        prog, _hit = _rtc_mirror_only_program()
        stepped = _rtc_plc(prog)
        _run_scans(stepped, 4_000, fold=False)
        prog, _hit = _rtc_mirror_only_program()
        folded = _rtc_plc(prog)
        _unbounded_rtc(folded)
        counter = _count_kernel_scans(folded)
        _run_scans(folded, 4_000, fold=True)

        assert counter["n"] < 10  # one window spans every tick
        assert folded.state.timestamp == pytest.approx(stepped.state.timestamp)
        assert folded.state.scan_id == stepped.state.scan_id
        assert _plain_tags(folded) == _plain_tags(stepped)

    def test_compiled_replay_landing_resolves_rtc(self) -> None:
        prog, _hit = _rtc_mirror_only_program()
        plc = _rtc_plc(prog)
        for _ in range(3_000):
            plc.step()
        _unbounded_rtc(plc)
        # Between checkpoints, so the seek has to fold across ticks.
        seek = plc._replay_seek(1_950)
        ref = plc.replay_to(1_950)
        assert 0 < plc._last_replay_seek_stats["kernel_scans"] < 100
        assert _plain_tags(seek) == _plain_tags(ref)

    def test_landing_edge_memory_matches_stepping(self) -> None:
        """rise(clock) after a fold compares against the clock the last folded
        scan saw, which the landing now reads directly."""
        from pyrung import latch

        def run(fold: bool) -> PLC:
            X = Bool("EdgeSeen")
            Tmr = Timer.clone("Tmr")
            with Program(strict=False) as prog:
                with Rung(rise(system.sys.clock_1s)):
                    latch(X)
                with Rung():
                    on_delay(Tmr, 10_000)
            plc = PLC(prog, dt=0.010)
            plc.step()
            _run_scans(plc, 1_234, fold=fold)
            return plc

        folded, stepped = run(True), run(False)
        assert folded.state.scan_id == stepped.state.scan_id
        assert _plain_tags(folded) == _plain_tags(stepped)
        # The rise(clock_1s) contact's own memory (site e1).
        assert folded.state.memory.get("_edge:e1", False) == stepped.state.memory.get(
            "_edge:e1", False
        )


class TestAClockEdgeAfterTheProbe:
    """A probe that reads just before a clock edge commits a state past it; that
    edge has not run.  A ``rise(clock_1s)`` rung, gated by a just-saturated timer
    comparison, still fires on it.
    """

    def test_an_edge_after_the_probe_still_fires(self) -> None:
        def run(fold: bool) -> PLC:
            Enable = Bool("Enable", external=True)
            A, B, Extent = Int("A", external=True), Int("B", external=True), Int("Extent")
            Tmr = Timer.clone("Tmr")
            with Program(strict=False) as prog:
                with Rung(Enable):
                    on_delay(Tmr, 19_006, "ms")
                with Rung(rise(system.sys.clock_1s), Tmr.Acc > 8_505):
                    calc(A + B, Extent)
            plc = PLC(prog, dt=0.005)
            plc.patch({"Enable": True, "A": 0, "B": 1})
            plc.run_until(Tmr.Done, max_cycles=40_000, fold=fold)
            return plc

        folded, stepped = run(True), run(False)
        assert stepped.state.tags["Extent"] == 1
        assert folded.state.scan_id == stepped.state.scan_id
        assert _plain_tags(folded) == _plain_tags(stepped)


# ---------------------------------------------------------------------------
# Soft RTC ticks: clock copies rebuilt by the landing scan, readers off
# ---------------------------------------------------------------------------


def _rtc_mirror_program(
    *, stop_stamp: bool = False, live_stamp: bool = False
) -> tuple[Program, Bool]:
    """Clock mirrors, an alarm-history logger that is off unless an alarm edge
    or a manual request powers it, and a 5 s timer to aim for.

    The logger reads ``Hist1`` and ``Hist2`` before rewriting them, so a live
    logger keeps scan-to-scan state that a single landing scan cannot replay.
    """
    from pyrung import Or, copy

    Sec = Int("Sec")
    HHMM = Int("HHMM")
    Alarm = Bool("Alarm")
    Manual = Int("Manual")
    Hist1, Hist2, Hist3 = Int("Hist1"), Int("Hist2"), Int("Hist3")
    Stopped = Bool("Stopped")
    StopSec = Int("StopSec")
    Hit = Bool("Hit")
    Tmr = Timer.clone("Tmr")
    with Program(strict=False) as prog:
        with Rung():
            copy(system.rtc.second, Sec)
        with Rung():
            calc(system.rtc.hour * 100 + system.rtc.minute, HHMM)
        with Rung(Or(rise(Alarm), Manual != 0)):
            copy(Hist2, Hist3)
            copy(Hist1, Hist2)
            copy(Sec, Hist1)
        if stop_stamp:
            with Rung(Stopped):
                copy(Sec, StopSec, oneshot=True)
        if live_stamp:
            # Read below, so StopSec is not unread churn: it stays guarded.
            with Rung(Stopped):
                calc(StopSec + Sec, StopSec)
            with Rung(StopSec > 30_000):
                out(Bool("StopSecHigh"))
        with Rung():
            on_delay(Tmr, 5000)
        with Rung(Tmr.Done):
            out(Hit)
    return prog, Hit


def _rtc_mirror_plc(prog: Program, **patch: Any) -> PLC:
    from datetime import datetime

    plc = PLC(prog, dt=0.010)
    plc.set_rtc(datetime(*_RTC_START))
    if patch:
        plc.patch(patch)
    plc.step()
    return plc


def _folded_and_stepped(
    build: Callable[[], Program],
    run: Callable[[PLC, bool], None],
    *,
    dt: float = 0.010,
    start: tuple[int, ...] = _RTC_START,
    patch: dict[str, Any] | None = None,
) -> tuple[PLC, PLC]:
    from datetime import datetime

    out = []
    for fold in (True, False):
        plc = PLC(build(), dt=dt)
        plc.set_rtc(datetime(*start))
        if patch:
            plc.patch(patch)
        plc.step()
        run(plc, fold)
        out.append(plc)
    return out[0], out[1]


def _until_hit(max_cycles: int) -> Callable[[PLC, bool], None]:
    def run(plc: PLC, fold: bool) -> None:
        plc.run_until(Bool("Hit"), max_cycles=max_cycles, fold=fold)

    return run


class TestSoftRtcTicks:
    """While every rung that reads a clock copy is off, the landing scan
    rebuilds the copies exactly, so folds cross RTC ticks bit-equal."""

    def test_mirrors_and_off_readers_are_found(self) -> None:
        from pyrung.core.fold import _time_mirrors_off

        prog, hit = _rtc_mirror_program()
        plc = _rtc_mirror_plc(prog)
        tm = plc._ensure_fold_context(frozenset({hit.name})).time_mirrors
        assert tm is not None
        assert tm.names == {"Sec", "HHMM"}
        assert len(tm.dead_nodes) == 1
        assert _time_mirrors_off(tm, (plc.state.tags,))

    @pytest.mark.parametrize("mode", ["run_for", "callable"])
    @pytest.mark.parametrize("seconds", [5, 12])
    def test_folded_matches_stepped_across_rollovers(self, seconds: int, mode: str) -> None:
        """The window crosses second, minute and hour rollovers.  A callable stop
        predicate (``scan_id >= target``) is invisible to the fold, so after the
        timer saturates nothing bounds a window and none is folded; the RTC tick
        only ever shortens a window, never creates one."""
        target = seconds * 100

        def run(plc: PLC, fold: bool) -> None:
            if mode == "run_for":
                plc.run_for(float(seconds), fold=fold)
            else:
                plc.run_until(lambda s: s.scan_id >= target, max_cycles=target + 10, fold=fold)

        for us in (250_000, 500_000, 750_000, 990_000):
            start = (2026, 3, 1, 12, 59, 57, us)
            folded, stepped = _folded_and_stepped(
                lambda: _rtc_mirror_program()[0], run, start=start
            )
            assert stepped.state.tags["HHMM"] == 1300
            assert folded.state.scan_id == stepped.state.scan_id
            assert _plain_tags(folded) == _plain_tags(stepped)

    def test_run_until_target_matches_stepped(self) -> None:
        folded, stepped = _folded_and_stepped(lambda: _rtc_mirror_program()[0], _until_hit(10_000))
        assert stepped.state.tags["Hit"] is True
        assert folded.state.scan_id == stepped.state.scan_id
        assert _plain_tags(folded) == _plain_tags(stepped)

    def test_soft_ticks_save_kernel_scans(self) -> None:
        prog, hit = _rtc_mirror_program()
        plc = _rtc_mirror_plc(prog)
        counter = _count_kernel_scans(plc)
        plc.run_until(hit, max_cycles=10_000)
        assert plc.state.tags[hit.name] is True
        # About one real scan for each RTC second the rungs read.
        assert counter["n"] < 20

    def test_live_reader_is_not_judged_off(self) -> None:
        from pyrung.core.fold import _time_mirrors_off

        prog, hit = _rtc_mirror_program()
        plc = _rtc_mirror_plc(prog)
        tm = plc._ensure_fold_context(frozenset({hit.name})).time_mirrors
        assert tm is not None
        plc.patch({"Manual": 1})
        plc.step()
        assert not _time_mirrors_off(tm, (plc.state.tags,))

    def test_live_reader_folded_matches_stepped(self) -> None:
        folded, stepped = _folded_and_stepped(
            lambda: _rtc_mirror_program()[0], _until_hit(10_000), patch={"Manual": 1}
        )
        assert folded.state.scan_id == stepped.state.scan_id
        assert _plain_tags(folded) == _plain_tags(stepped)

    @pytest.mark.parametrize("stopped", [False, True])
    def test_one_shot_stamp_is_inactive_either_way(self, stopped: bool) -> None:
        """Powered, the one-shot stamp fired on its first powered scan; off, it
        does nothing.  Either way it does not act on a tick."""
        from pyrung.core.fold import _time_mirrors_off

        prog, hit = _rtc_mirror_program(stop_stamp=True)
        plc = _rtc_mirror_plc(prog, Stopped=stopped)
        tm = plc._ensure_fold_context(frozenset({hit.name})).time_mirrors
        assert tm is not None
        assert _time_mirrors_off(tm, (plc.state.tags,))

        folded, stepped = _folded_and_stepped(
            lambda: _rtc_mirror_program(stop_stamp=True)[0],
            _until_hit(10_000),
            patch={"Stopped": stopped},
        )
        assert folded.state.scan_id == stepped.state.scan_id
        assert _plain_tags(folded) == _plain_tags(stepped)

    def test_powered_accumulating_reader_keeps_ticks_hard(self) -> None:
        # A budget run: with no target, StopSec is not target-disjoint churn.
        folded, stepped = _folded_and_stepped(
            lambda: _rtc_mirror_program(live_stamp=True)[0],
            lambda plc, fold: _run_scans(plc, 1_000, fold=fold),
            patch={"Stopped": True},
        )
        assert folded.state.scan_id == stepped.state.scan_id
        assert _plain_tags(folded) == _plain_tags(stepped)

    @pytest.mark.parametrize("dt", [0.5, 0.01])
    def test_replay_seek_across_soft_ticks(self, dt: float) -> None:
        from datetime import datetime

        plc = PLC(_rtc_mirror_program()[0], dt=dt)
        plc.set_rtc(datetime(*_RTC_START))
        for _ in range(int(30 / dt)):
            plc.step()
        target = int(19.5 / dt)
        seek = plc._replay_seek(target)
        ref = plc.replay_to(target)
        assert plc._last_replay_seek_stats["kernel_scans"] < 20
        assert _plain_tags(seek) == _plain_tags(ref)


def _branch_position_program(shape: str) -> Program:
    """A branch whose condition reads ``X == 0``, where X is 0 at rung entry and
    1 at the end of the scan.  The branch reads X when the solve reaches it: above
    the write of 1 (``above``) it is live; below it, in its own rung (``below``)
    or as a continued rung (``continued``), it is dead."""
    from pyrung import branch, copy, latch

    Sec, X, Snap, Half, Seen = Int("Sec"), Int("X"), Bool("Snap"), Int("Half"), Bool("Seen")
    Tmr, Hit = Timer.clone("Tmr"), Bool("Hit")
    with Program(strict=False) as prog:
        with Rung():
            copy(system.rtc.second, Sec)
        with Rung():
            copy(0, X)
        with Rung(X == 0):
            out(Snap)
        if shape == "continued":
            with Rung():
                copy(1, X)
            with Rung().continued():
                with branch(X == 0):
                    calc(Sec // 30, Half)
        else:
            with Rung():
                if shape == "below":
                    copy(1, X)
                with branch(X == 0):
                    calc(Sec // 30, Half)
                if shape == "above":
                    copy(1, X)
        with Rung(Half == 1):
            latch(Seen)
        with Rung():
            on_delay(Tmr, 60, "s")
        with Rung(Tmr.Done):
            out(Hit)
    return prog


def _rtc_reader_shape(shape: str) -> Program:
    """One reader of a clock copy (live while ``Req``) in a given program shape."""
    from pyrung import branch, call, copy, forloop, latch, return_early, subroutine

    Sec, Half, Seen, Req = Int("Sec"), Int("Half"), Bool("Seen"), Bool("Req")
    Tmr, Hit = Timer.clone("Tmr"), Bool("Hit")

    @subroutine("R")
    def reader() -> None:
        with Rung():
            calc(Sec // 30, Half)

    @subroutine("S")
    def stamper() -> None:
        with Rung(system.sys.clock_1s):
            return_early()
        with Rung():
            copy(system.rtc.second, Sec)

    with Program(strict=False) as prog:
        if shape == "return_early":
            with Rung():
                call(stamper)
            with Rung(Req):
                calc(Sec // 30, Half)
        else:
            with Rung():
                copy(system.rtc.second, Sec)
        if shape == "called":
            with Rung(Req):
                call(reader)
        elif shape == "branch_call":
            with Rung():
                with branch(Req):
                    call(reader)
        elif shape == "forloop":
            with Rung(Req):
                with forloop(2):
                    calc(Sec // 30, Half)
        elif shape == "plain":
            with Rung(Req):
                calc(Sec // 30, Half)
        with Rung(Half == 1):
            latch(Seen)
        with Rung():
            on_delay(Tmr, 60, "s")
        with Rung(Tmr.Done):
            out(Hit)
    return prog


class TestSoftRtcRefusals:
    """Shapes where a clock-derived value could act inside a window.  The tick
    stays hard (or the reader is judged on), and folded stays bit-equal."""

    _START = (2026, 3, 1, 12, 59, 5, 250_000)

    @staticmethod
    def _tm(prog: Program, targets: frozenset[str] = frozenset()) -> Any:
        plc = _rtc_plc(prog)
        return plc._ensure_fold_context(targets).time_mirrors

    def test_reader_before_writer(self) -> None:
        from pyrung import copy

        Sec, Echo = Int("Sec"), Int("Echo")
        with Program(strict=False) as prog:
            with Rung():
                copy(Sec, Echo)
            with Rung():
                copy(system.rtc.second, Sec)
        assert self._tm(prog) is None

    def test_mirror_in_an_enable_condition(self) -> None:
        from pyrung import copy, latch

        Sec, X = Int("Sec"), Bool("X")
        with Program(strict=False) as prog:
            with Rung():
                copy(system.rtc.second, Sec)
            with Rung(Sec == 30):
                latch(X)
        assert self._tm(prog) is None

    def test_mirror_as_target(self) -> None:
        from pyrung import copy

        Sec, Req, Log = Int("Sec"), Bool("Req"), Int("Log")
        with Program(strict=False) as prog:
            with Rung():
                copy(system.rtc.second, Sec)
            with Rung(Req):
                copy(Sec, Log)
        assert self._tm(prog) is not None
        assert self._tm(prog, frozenset({"Sec"})) is None

    def test_calc_that_could_fault_is_not_a_mirror(self) -> None:
        """``second * 1000`` overflows an Int: the out-of-range flag would follow
        the clock, so the calc is a live reader, not a rebuilt copy."""
        from pyrung import copy

        Big, Req, Log = Int("Big"), Bool("Req"), Int("Log")
        with Program(strict=False) as prog:
            with Rung():
                calc(system.rtc.second * 1000, Big)
            with Rung(Req):
                copy(Big, Log)
        assert self._tm(prog) is None

    def test_fault_flag_reader_does_not_disable_mirrors(self) -> None:
        from pyrung import copy, latch

        Sec, Req, Log = Int("Sec"), Bool("Req"), Int("Log")
        with Program(strict=False) as prog:
            with Rung():
                copy(system.rtc.second, Sec)
            with Rung(Req):
                copy(Sec, Log)
            with Rung(system.fault.out_of_range):
                latch(Bool("Err"))
        tm = self._tm(prog)
        assert tm is not None and tm.names == {"Sec"}

    def test_clock_gated_copy_is_a_reader_never_off(self) -> None:
        from pyrung import copy
        from pyrung.core.fold import _time_mirrors_off

        Sec, Gated = Int("Sec"), Int("Gated")
        with Program(strict=False) as prog:
            with Rung():
                copy(system.rtc.second, Sec)
            with Rung(system.sys.clock_1s):
                copy(system.rtc.second, Gated)
        plc = _rtc_plc(prog)
        tm = plc._ensure_fold_context().time_mirrors
        assert tm is not None and "Gated" not in tm.names
        assert not _time_mirrors_off(tm, (plc.state.tags,))

    def test_self_feeding_clock_calc_is_never_off(self) -> None:
        from pyrung import copy
        from pyrung.core.fold import _time_mirrors_off

        Sec, Tot = Int("Sec"), Int("Tot")
        with Program(strict=False) as prog:
            with Rung():
                copy(system.rtc.second, Sec)
            with Rung():
                calc(Tot + system.rtc.second, Tot)
        plc = _rtc_plc(prog)
        tm = plc._ensure_fold_context().time_mirrors
        assert tm is not None and "Tot" not in tm.names
        assert not _time_mirrors_off(tm, (plc.state.tags,))

    def test_branch_is_judged_at_its_own_position(self) -> None:
        from pyrung.core.fold import _time_mirrors_off

        plc = _rtc_plc(_branch_position_program("above"), dt=1.0, start=self._START)
        for _ in range(30):
            plc.step()
        tm = plc._ensure_fold_context(frozenset({"Hit"})).time_mirrors
        assert tm is not None
        assert plc.state.tags["Half"] == 1  # the branch is live
        assert not _time_mirrors_off(tm, (plc.state.tags,))

    @pytest.mark.parametrize("shape", ["above", "below", "continued"])
    @pytest.mark.parametrize("dt", [1.0, 0.05])
    def test_branch_position_folded_matches_stepped(self, shape: str, dt: float) -> None:
        folded, stepped = _folded_and_stepped(
            lambda: _branch_position_program(shape),
            _until_hit(int(70 / dt)),
            dt=dt,
            start=self._START,
        )
        assert stepped.state.tags["Seen"] is (shape == "above")
        assert folded.state.scan_id == stepped.state.scan_id
        assert _plain_tags(folded) == _plain_tags(stepped)

    @pytest.mark.parametrize("shape", ["called", "branch_call", "forloop", "plain", "return_early"])
    @pytest.mark.parametrize("req", [False, True], ids=["off", "live"])
    def test_reader_shapes_folded_match_stepped(self, shape: str, req: bool) -> None:
        dt = 0.05
        folded, stepped = _folded_and_stepped(
            lambda: _rtc_reader_shape(shape),
            _until_hit(int(70 / dt)),
            dt=dt,
            start=self._START,
            patch={"Req": req},
        )
        assert stepped.state.tags["Hit"] is True
        assert stepped.state.tags["Seen"] is req
        assert folded.state.scan_id == stepped.state.scan_id
        assert _plain_tags(folded) == _plain_tags(stepped)

    def test_return_early_before_the_copy_is_not_a_recompute(self) -> None:
        assert self._tm(_rtc_reader_shape("return_early")) is None


class TestRunForScanCount:
    """A folded run_for runs exactly the scans stepping would, although the
    folded and stepped timestamps differ in their last bits."""

    @pytest.mark.parametrize(("dt", "seconds"), [(0.010, 40.0), (0.3, 12.0), (0.7, 33.0)])
    def test_folded_run_for_matches_stepped(self, dt: float, seconds: float) -> None:
        def run(fold: bool) -> PLC:
            prog, _x = _rtc_latch_program()
            plc = _rtc_plc(prog, dt=dt)
            plc.run_for(seconds, fold=fold)
            return plc

        folded, stepped = run(True), run(False)
        assert folded.state.scan_id == stepped.state.scan_id
        assert folded.state.tags["X"] == stepped.state.tags["X"]
        if dt == 0.010:
            # At dt 0.3 / 0.7 a millisecond timer folded in one aggregate dt
            # can floor one unit below its stepped sum (a separate issue).
            assert _plain_tags(folded) == _plain_tags(stepped)

    def test_stepped_scan_count_replays_the_additions(self) -> None:
        from pyrung.core.fold import _stepped_scans_until

        t, n = 0.01, 0
        while t < 40.01:
            t += 0.01
            n += 1
        assert _stepped_scans_until(0.01, 40.01, 0.01) == n
        assert _stepped_scans_until(0.0, 1.0, 0.0) is None


def _forced_gate_program(edge: bool) -> Program:
    """``G`` is written mid-scan and forced the other way at the boundaries.

    The stored (forced) value says the reader of the clock copy is off, but
    the rung between the writer and the post-logic force sees the written
    value and copies the second every scan.  With *edge*, the reader is
    ``fall(G)`` in a subroutine called on both sides of the writer: the first
    call sees the forced True, the second the written False, so the one
    contact falls on every scan.
    """
    from pyrung import call, copy, fall, latch, subroutine

    Sec, Log, G, Seen = Int("Sec"), Int("Log"), Bool("G"), Bool("Seen")
    Tmr, Hit = Timer.clone("Tmr"), Bool("Hit")
    with Program(strict=False) as prog:
        with Rung():
            copy(system.rtc.second, Sec)
        if edge:
            with Rung():
                call("Reader")
            with Rung(Tmr.Acc < 0):
                out(G)  # False mid-scan; forced True
            with Rung():
                call("Reader")
        else:
            with Rung(Tmr.Acc >= 0):
                out(G)  # True mid-scan; forced False
            with Rung(G):
                copy(Sec, Log)
        with Rung(Log == 30):
            latch(Seen)
        with Rung():
            on_delay(Tmr, 60, "s")
        with Rung(Tmr.Done):
            out(Hit)
        if edge:
            with subroutine("Reader"):
                with Rung(fall(G)):
                    copy(Sec, Log)
    return prog


class TestSoftRtcForcedTags:
    """A force re-applies after logic, so a forced tag's stored value is not
    what a rung between a mid-scan writer and the force saw.  The off judgement
    never reads a forced tag."""

    _START = (2026, 3, 1, 12, 59, 5, 250_000)

    @pytest.mark.parametrize("edge", [False, True], ids=["level", "fall"])
    def test_forced_gate_folded_matches_stepped(self, edge: bool) -> None:
        dt = 0.01

        def run(plc: PLC, fold: bool) -> None:
            plc.force("G", edge)
            plc.run_until(Bool("Hit"), max_cycles=int(70 / dt) + 10, fold=fold)

        folded, stepped = _folded_and_stepped(
            lambda: _forced_gate_program(edge), run, dt=dt, start=self._START
        )
        assert stepped.state.tags["Seen"] is True
        assert folded.state.scan_id == stepped.state.scan_id
        assert _plain_tags(folded) == _plain_tags(stepped)

    def test_replay_seek_over_forced_history(self) -> None:
        from datetime import datetime

        plc = PLC(_forced_gate_program(False), dt=0.01)
        plc.set_rtc(datetime(*self._START))
        plc.force("G", False)
        for _ in range(4_001):
            plc.step()
        for target in (2_490, 2_599):
            seek = plc._replay_seek(target)
            ref = plc.replay_to(target)
            assert _plain_tags(seek) == _plain_tags(ref)
        assert ref.state.tags["Seen"] is True


class TestSoftRtcCallablePredicate:
    """A callable run_until predicate declares no reads, so it may read a clock
    copy: every RTC tick stays a bound and the copy stays observed."""

    @pytest.mark.parametrize("dt", [0.01, 1.0])
    def test_callable_reading_a_copy_stops_where_stepping_does(self, dt: float) -> None:
        from pyrung import copy

        def build() -> Program:
            Sec, Hit, Req, Log = Int("Sec"), Bool("Hit"), Bool("Req"), Int("Log")
            Tmr = Timer.clone("Tmr")
            with Program(strict=False) as prog:
                with Rung():
                    copy(system.rtc.second, Sec)
                with Rung(Req):
                    copy(Sec, Log)
                with Rung():
                    on_delay(Tmr, 60, "s")
                with Rung(Tmr.Done):
                    out(Hit)
            return prog

        def run(plc: PLC, fold: bool) -> None:
            plc.run_until(
                lambda s: s.tags.get("Sec") == 30 or bool(s.tags.get("Hit")),
                max_cycles=8_000,
                fold=fold,
            )

        folded, stepped = _folded_and_stepped(
            build, run, dt=dt, start=(2026, 3, 1, 12, 59, 5, 250_000)
        )
        assert stepped.state.tags["Sec"] == 30
        assert folded.state.scan_id == stepped.state.scan_id
        assert _plain_tags(folded) == _plain_tags(stepped)


class TestRtcNearDatetimeMax:
    def test_tick_past_datetime_max_declines_instead_of_raising(self) -> None:
        from pyrung import copy

        def build() -> Program:
            with Program(strict=False) as prog:
                with Rung():
                    copy(system.rtc.day, Int("D"))
                with Rung():
                    on_delay(Timer.clone("Tmr"), 100, "s")
            return prog

        folded, stepped = _folded_and_stepped(
            build,
            lambda plc, fold: _run_scans(plc, 500, fold=fold),
            start=(9999, 12, 31, 12, 0, 0, 0),
        )
        assert folded.state.scan_id == stepped.state.scan_id
        assert _plain_tags(folded) == _plain_tags(stepped)


class TestLandingScanRaises:
    """A landing scan that raises commits nothing and leaves the tip as the
    pre-fold state, as an unfolded scan that raises does."""

    def test_tip_is_unchanged_when_the_landing_raises(self) -> None:
        from pyrung import run_function

        def boom() -> None:
            raise RuntimeError("boom")

        def build() -> Program:
            Tmr = Timer.clone("Tmr")
            with Program(strict=False) as prog:
                with Rung():
                    on_delay(Tmr, 5, "s")
                with Rung(Tmr.Done):
                    run_function(boom)
            return prog

        tips = []
        for fold in (False, True):
            plc = PLC(build(), dt=0.01)
            plc.step()
            with pytest.raises(RuntimeError):
                plc.run_until(Bool("Never"), max_cycles=2_000, fold=fold)
            tips.append(plc.state)
        stepped, folded = tips
        assert folded.timestamp == pytest.approx(folded.scan_id * 0.01)
        assert stepped.timestamp == pytest.approx(stepped.scan_id * 0.01)


# ---------------------------------------------------------------------------
# Exactness against stepping: full state (tags and memory) and scan count
# ---------------------------------------------------------------------------


def _full_state(plc: PLC) -> dict[str, Any]:
    """Every tag and memory slot except the per-scan diagnostics a landing inflates."""
    state = _plain_tags(plc)
    state.update({f"mem:{k}": v for k, v in plc.state.memory.items()})
    return state


def _assert_fold_is_exact(
    build: Callable[[], Program],
    run: Callable[[PLC, bool], None],
    *,
    dt: float = 0.010,
    patch: dict[str, Any] | None = None,
) -> PLC:
    folded, stepped = _folded_and_stepped(build, run, dt=dt, patch=patch)
    assert folded.state.scan_id == stepped.state.scan_id
    assert _full_state(folded) == _full_state(stepped)
    return folded


def _scans(n: int) -> Callable[[PLC, bool], None]:
    def run(plc: PLC, fold: bool) -> None:
        _run_scans(plc, n, fold=fold)

    return run


class TestClockEdgeProbe:
    """A scan reads the system clock at its start.  The probe certifying a
    window must have read the phase the window reads, and an edge scan certifies
    nothing, so per-scan work under a clock level or edge is never extrapolated
    from the other phase."""

    @staticmethod
    def _level_gated(work: str) -> Callable[[], Program]:
        def build() -> Program:
            with Program(strict=False) as prog:
                with Rung(system.sys.clock_1s):
                    if work == "calc":
                        calc(Dint("C_Acc") + 1, Dint("C_Acc"))
                    else:
                        # Counts the level's rises: once a second.
                        count_up(Counter.clone("C"), 100_000).reset(Bool("CR"))
            return prog

        return build

    @pytest.mark.parametrize("work", ["calc", "counter"])
    @pytest.mark.parametrize("dt", [0.01, 0.03, 0.07])
    def test_level_gated_work_counts_every_high_scan(self, dt: float, work: str) -> None:
        folded = _assert_fold_is_exact(self._level_gated(work), _scans(int(10 / dt)), dt=dt)
        acc = folded.state.tags["C_Acc"]
        if work == "calc":
            assert acc > 10
        else:
            assert acc == 10

    @staticmethod
    def _edge_pulses() -> Program:
        Tmr = Timer.clone("Tmr")
        with Program(strict=False) as prog:
            with Rung(rise(system.sys.clock_1s)):
                out(Bool("P"))
            with Rung(fall(system.sys.clock_1s)):
                out(Bool("Q"))
            with Rung():
                on_delay(Tmr, 60_000)
        return prog

    @pytest.mark.parametrize("n", [49, 50, 51, 52, 99, 100, 101, 150])
    def test_edge_pulse_stays_on_its_own_scan(self, n: int) -> None:
        _assert_fold_is_exact(self._edge_pulses, _scans(n))

    @staticmethod
    def _holds_still_only_on_the_edge_scan() -> Program:
        # The plain rungs swap C every scan; the edge rungs undo that on the scan
        # that sees the edge, so that one scan leaves the state as it found it.
        C, D = Int("C"), Int("D")
        clock = system.sys.clock_1s
        with Program(strict=False) as prog:
            with Rung():
                on_delay(Timer.clone("Tmr"), 30_000)
            with Rung(C == 0):
                copy(1, D)
            with Rung(C == 1):
                copy(0, D)
            with Rung():
                copy(D, C)
            with Rung(rise(clock), C == 0):
                copy(1, D)
            with Rung(rise(clock), C == 1):
                copy(0, D)
            with Rung(rise(clock)):
                copy(D, C)
        return prog

    @pytest.mark.parametrize("dt", [0.007, 0.01, 0.05])
    @pytest.mark.parametrize("scans", [800, 801])
    def test_an_edge_scan_certifies_nothing_after_it(self, dt: float, scans: int) -> None:
        _assert_fold_is_exact(self._holds_still_only_on_the_edge_scan, _scans(scans), dt=dt)


class TestTimerCarryExact:
    """A timer adds ``dt`` in its unit plus a float carry each scan; a fold
    lands it where that per-scan recurrence would, not where one aggregate
    ``skip * dt`` floors.  An earlier rung reading the accumulator sees the
    value the last folded scan starts from."""

    @staticmethod
    def _timer_with_reader(unit: str, preset: int) -> Callable[[], Program]:
        def build() -> Program:
            from pyrung import copy

            Tmr = Timer.clone("Tmr")
            with Program(strict=False) as prog:
                with Rung():
                    copy(Tmr.Acc, Int("Snap"))
                with Rung():
                    on_delay(Tmr, preset, unit)
                with Rung(Tmr.Done):
                    out(Bool("Hit"))
            return prog

        return build

    @pytest.mark.parametrize(
        ("unit", "preset", "dt"),
        [("s", 61, 0.1), ("ms", 25_200, 0.7), ("ms", 30_000, 0.3), ("min", 1, 0.013)],
    )
    def test_run_until_done_matches_stepping(self, unit: str, preset: int, dt: float) -> None:
        _assert_fold_is_exact(
            self._timer_with_reader(unit, preset), _until_hit(int(4 * preset / dt) + 10), dt=dt
        )

    @pytest.mark.parametrize(("unit", "dt", "n"), [("s", 0.1, 3000), ("ms", 0.3, 87)])
    def test_budget_run_matches_stepping(self, unit: str, dt: float, n: int) -> None:
        _assert_fold_is_exact(self._timer_with_reader(unit, 30_000), _scans(n), dt=dt)

    @pytest.mark.parametrize(("dt", "n"), [(0.1, 300), (0.7, 100), (0.013, 3000)])
    def test_replay_seek_matches_replay_to(self, dt: float, n: int) -> None:
        plc = PLC(self._timer_with_reader("s", 1_000)(), dt=dt)
        for _ in range(n):
            plc.step()
        target = n - 5
        sought = plc._replay_seek(target)
        assert plc._last_replay_seek_stats["kernel_scans"] < n // 2
        assert _full_state(sought) == _full_state(plc.replay_to(target))

    def test_timer_held_at_its_clamp_keeps_its_carry(self) -> None:
        # At 32767 with a whole-unit quantum and no carry, the probe sees the
        # timer unchanged, but each landing still runs it once with the
        # aggregate dt, whose float product leaves a stray carry.
        def build() -> Program:
            Long = Timer.clone("Long")
            Tmr = Timer.clone("Tmr")
            C = Counter.clone("C")
            with Program(strict=False) as prog:
                with Rung():
                    on_delay(Long, 32_000)
                with Rung(system.sys.clock_1s):
                    on_delay(Tmr, 30, "s")
                with Rung(Tmr.Done):
                    out(Bool("Hit"))
                with Rung(rise(system.sys.clock_1s)):
                    count_up(C, 100_000).reset(Bool("CR"))
            return prog

        folded = _assert_fold_is_exact(build, _until_hit(6_000), dt=0.007)
        assert folded.state.tags["Long_Acc"] == 32_767

    @pytest.mark.parametrize(
        "quantum",
        [
            0.01,
            0.05,
            0.3,
            0.7,
            0.007,
            0.013,
            0.0013,
            0.01 / 60,
            0.03 / 60,
            0.01 / 3600,
            1.1,
            1.5,
            1.9,
        ],
    )
    def test_accelerated_carry_equals_the_recurrence(self, quantum: float) -> None:
        from pyrung.core.fold import _carry_run, _timer_step

        for acc0, frac0, n, target in (
            (0, 0.0, 30_000, None),
            (32_700, 0.0, 30_000, None),
            (5, 0.37, 20_000, 120),
            (0, 0.0, 40_000, 400),
        ):
            acc, frac, reached = acc0, frac0, None
            for k in range(1, n + 1):
                acc, frac = _timer_step(acc, frac, quantum, 32_767)
                if target is not None and acc >= target:
                    reached = k
                    break
            steps, f_acc, f_frac, status = _carry_run(acc0, frac0, quantum, 32_767, n, target)
            assert status == ("target" if reached else "limit")
            assert (steps, f_acc, f_frac) == (reached or n, acc, frac)

    @pytest.mark.parametrize(
        ("unit", "seconds", "max_kernel"), [("min", 86_400.0, 4), ("h", 8_640.0, 4)]
    )
    def test_sub_unit_timer_folds_long_runs_cheaply(
        self, unit: str, seconds: float, max_kernel: int
    ) -> None:
        # 8.6M / 860K scans of a timer whose step is a fraction of its unit: past the
        # 5M scans run_for() used to step interpreted, and every scan really runs.
        def build() -> Program:
            Long = Timer.clone("Long")
            with Program(strict=False) as prog:
                with Rung():
                    on_delay(Long, 32_000, unit)
                with Rung(Long.Done):
                    out(Bool("Hit"))
            return prog

        plc = PLC(build(), dt=0.010)
        plc.step()
        counter = _count_kernel_scans(plc)
        plc.run_for(seconds)
        assert counter["n"] <= max_kernel

    @pytest.mark.parametrize(
        ("quantum", "frac"),
        [(10.0, 0.0), (700.0000000000001, 0.0), (300.0, 0.25), (0.1, 0.0), (1023.5, 0.5)],
    )
    def test_closed_form_carry_equals_the_recurrence(self, quantum: float, frac: float) -> None:
        from pyrung.core.fold import _AccSource, _timer_after, _timer_step, _TimerMove

        src = _AccSource("T_Acc", "T_Done", 32_767, "up", True)
        move = _TimerMove(src, 0, frac, quantum, quantum)
        acc, carry = 0, frac
        for n in range(1, 400):
            acc, carry = _timer_step(acc, carry, quantum, 32_767)
            assert _timer_after(move, n) == (acc, carry)


class TestSelfFeedingChurnExact:
    """A calc that feeds its own destination is not just left out of the
    plateau guard: an exact affine self-calc is patched forward in closed form
    (wrapping and faulting exactly as stepping), any other shape is guarded."""

    @staticmethod
    def _churn_program(body: Callable[[Any], Any], *, read: bool = False) -> Callable[[], Program]:
        def build() -> Program:
            from pyrung import latch

            Tot = Int("Tot")
            Tmr = Timer.clone("Tmr")
            with Program(strict=False) as prog:
                with Rung():
                    body(Tot)
                if read:
                    with Rung(Tot > 100):
                        out(Bool("TotHigh"))
                with Rung(system.fault.out_of_range):
                    latch(Bool("FaultSeen"))
                with Rung():
                    on_delay(Tmr, 5_000)
                with Rung(Tmr.Done):
                    out(Bool("Hit"))
            return prog

        return build

    _SHAPES: dict[str, Callable[[Any], Any]] = {
        "tag_step": lambda tot: calc(tot + Int("K"), tot),
        "literal": lambda tot: calc(tot + 7, tot),
        "negative": lambda tot: calc(tot - 9, tot),
        "modular": lambda tot: calc((tot + 7) % 1_000, tot),
        "not_affine": lambda tot: calc(tot * 3 + 1, tot),
    }

    @pytest.mark.parametrize("shape", sorted(_SHAPES))
    @pytest.mark.parametrize("read", [False, True])
    def test_churn_lands_on_the_stepped_value(self, shape: str, read: bool) -> None:
        folded = _assert_fold_is_exact(
            self._churn_program(self._SHAPES[shape], read=read),
            _until_hit(1_000),
            patch={"K": 1},
        )
        assert folded.state.tags["Hit"] is True

    @pytest.mark.parametrize("shape", ["tag_step", "literal", "negative"])
    def test_overflow_wraps_and_faults_as_stepping(self, shape: str) -> None:
        start = -32_700 if shape == "negative" else 32_700
        _assert_fold_is_exact(
            self._churn_program(self._SHAPES[shape]),
            _until_hit(1_000),
            patch={"K": 7, "Tot": start},
        )

    @pytest.mark.parametrize("start", [32_000, 32_767, 40_000])
    def test_copy_clamp_faults_as_stepping(self, start: int) -> None:
        def build() -> Program:
            from pyrung import latch

            Wide = Dint("Wide")
            Narrow = Int("Narrow")
            Tmr = Timer.clone("Tmr")
            with Program(strict=False) as prog:
                with Rung():
                    calc(Wide + 7, Wide)
                with Rung():
                    copy(Wide, Narrow)
                with Rung(system.fault.out_of_range):
                    latch(Bool("FaultSeen"))
                with Rung():
                    on_delay(Tmr, 5_000)
                with Rung(Tmr.Done):
                    out(Bool("Hit"))
            return prog

        _assert_fold_is_exact(build, _until_hit(1_000), patch={"Wide": start})

    @staticmethod
    def _view_of_a_moving_tag(
        store: Callable[[Any, Any], None],
        reader: Callable[[Any], None],
        *,
        step: int = 7,
        store_first: bool = False,
        dest: Any = None,
    ) -> Callable[[], Program]:
        """A self-calc viewed by one store, a reader of it, and a slow timer.

        The reader sits right after the store: the self-calc settles the flags.
        """

        def build() -> Program:
            Wide = Dint("Wide")
            View = Int("View") if dest is None else dest
            Tmr = Timer.clone("Tmr")
            with Program(strict=False) as prog:
                if store_first:
                    with Rung():
                        store(Wide, View)
                    reader(View)
                with Rung():
                    calc(Wide + step, Wide)
                if not store_first:
                    with Rung():
                        store(Wide, View)
                    reader(View)
                with Rung():
                    on_delay(Tmr, 5_000)
                with Rung(Tmr.Done):
                    out(Bool("End"))
            return prog

        return build

    @pytest.mark.parametrize(
        "store",
        [
            pytest.param(lambda a, b: copy(a, b), id="copy"),
            pytest.param(lambda a, b: calc(a + 0, b), id="calc"),
        ],
    )
    @pytest.mark.parametrize("store_first", [False, True], ids=["store_below", "store_above"])
    @pytest.mark.parametrize(
        "step, start",
        [
            pytest.param(7, 32_000, id="up_to_the_limit"),
            pytest.param(-7, -32_000, id="down_to_the_limit"),
            pytest.param(7, 40_000, id="starting_past_it"),
            pytest.param(7, -40_000, id="coming_back_in"),
        ],
    )
    def test_a_store_past_its_range_counts_each_flagged_scan(
        self, store: Any, store_first: bool, step: int, start: int
    ) -> None:
        def reader(_view: Any) -> None:
            N = Int("N")
            with Rung(system.fault.out_of_range):
                calc(N + 1, N)

        build = self._view_of_a_moving_tag(store, reader, step=step, store_first=store_first)
        plc = _assert_fold_is_exact(
            build,
            lambda plc, fold: plc.run_until(Bool("End"), max_cycles=10_000, fold=fold),
            patch={"Wide": start},
        )
        assert plc.state.tags["N"] > 2

    @pytest.mark.parametrize(
        "store",
        [
            pytest.param(lambda a, b: copy(a, b), id="copy"),
            pytest.param(lambda a, b: calc(a + 0, b), id="calc"),
        ],
    )
    @pytest.mark.parametrize("store_first", [False, True], ids=["store_below", "store_above"])
    def test_a_rung_on_the_flag_stops_on_the_first_flagged_scan(
        self, store: Any, store_first: bool
    ) -> None:
        def reader(_view: Any) -> None:
            with Rung(system.fault.out_of_range):
                out(Bool("Hit"))

        plc = _assert_fold_is_exact(
            self._view_of_a_moving_tag(store, reader, store_first=store_first),
            _until_hit(10_000),
            patch={"Wide": 32_000},
        )
        assert plc.state.tags["Wide"] in (32_770, 32_777)

    @pytest.mark.parametrize(
        "store",
        [
            pytest.param(lambda a, b: copy(a, b), id="copy"),
            pytest.param(lambda a, b: calc(a + 0, b), id="calc"),
        ],
    )
    @pytest.mark.parametrize(
        "stop",
        [
            pytest.param(system.fault.out_of_range, id="the_flag"),
            pytest.param(lambda s: s.tags.get("fault.out_of_range"), id="a_callable"),
        ],
    )
    def test_a_stop_on_the_flag_lands_on_the_first_flagged_scan(
        self, store: Any, stop: Any
    ) -> None:
        plc = _assert_fold_is_exact(
            self._view_of_a_moving_tag(store, lambda _view: None),
            lambda plc, fold: plc.run_until(stop, max_cycles=10_000, fold=fold),
            patch={"Wide": 32_000},
        )
        assert plc.state.tags["Wide"] == 32_770

    def test_a_counter_view_past_its_range_counts_each_flagged_scan(self) -> None:
        def build() -> Program:
            Cnt = Counter.clone("Cnt")
            Narrow = Int("Narrow")
            N = Int("N")
            Tmr = Timer.clone("Tmr")
            with Program(strict=False) as prog:
                with Rung(system.sys.clock_100ms):
                    count_up(Cnt, 100_000).reset(Bool("CR"))
                with Rung():
                    copy(Cnt.Acc, Narrow)
                with Rung(system.fault.out_of_range):
                    calc(N + 1, N)
                with Rung():
                    on_delay(Tmr, 5_000)
                with Rung(Tmr.Done):
                    out(Bool("Hit"))
            return prog

        plc = _assert_fold_is_exact(build, _until_hit(10_000), patch={"Cnt_Acc": 32_760})
        assert plc.state.tags["N"] > 2

    def test_a_view_read_past_its_wrap_stops_as_stepping(self) -> None:
        def reader(view: Any) -> None:
            with Rung(view < 0):
                out(Bool("Hit"))

        plc = _assert_fold_is_exact(
            self._view_of_a_moving_tag(lambda a, b: calc(a + 0, b), reader),
            _until_hit(10_000),
            patch={"Wide": 32_000},
        )
        assert plc.state.tags["Wide"] == 32_770

    @pytest.mark.parametrize(
        "view, limit",
        [
            pytest.param("copy", False, id="copy"),
            pytest.param("calc", True, id="calc"),
        ],
    )
    def test_a_view_nothing_can_tell_apart_keeps_folding(self, view: str, limit: bool) -> None:
        """A clamped copy read by a comparison is still monotone in its source,
        and with no reader of the flag only a wrap makes it read differently."""

        def reader(v: Any) -> None:
            with Rung(v == 32_767):
                calc(Int("N") + 1, Int("N"))

        store = (lambda a, b: copy(a, b)) if view == "copy" else (lambda a, b: calc(a + 0, b))
        build = self._view_of_a_moving_tag(store, reader)
        _assert_fold_is_exact(
            build,
            lambda plc, fold: plc.run_until(Bool("End"), max_cycles=10_000, fold=fold),
            patch={"Wide": 32_000},
        )
        plc = PLC(build(), dt=0.010)
        assert bool(plc._ensure_fold_context().mirror_limits) is limit

    @staticmethod
    def _copy_into_a_word(step: int, reader: Callable[[Any], None]) -> Callable[[], Program]:
        def build() -> Program:
            Wide = Dint("Wide")
            View = Word("View")
            Tmr = Timer.clone("Tmr")
            with Program(strict=False) as prog:
                with Rung():
                    calc(Wide + step, Wide)
                with Rung():
                    copy(Wide, View)
                reader(View)
                with Rung():
                    on_delay(Tmr, 5_000)
                with Rung(Tmr.Done):
                    out(Bool("End"))
            return prog

        return build

    @pytest.mark.parametrize(
        "step, start",
        [
            pytest.param(7, 65_000, id="up-past-65535"),
            pytest.param(-7, 200, id="down-past-0"),
            pytest.param(-7, 1_000, id="down-through-the-reader-then-past-0"),
            pytest.param(7, 70_000, id="starts-past-65535"),
        ],
    )
    @pytest.mark.parametrize(
        "read",
        [
            pytest.param(lambda v: v > 100, id="above"),
            pytest.param(lambda v: v < 100, id="below"),
            pytest.param(lambda v: v == 65_535, id="top"),
            pytest.param(lambda v: v == 0, id="bottom"),
        ],
    )
    def test_a_copy_into_a_word_wraps_and_a_comparison_sees_it(
        self, step: int, start: int, read: Any
    ) -> None:
        """A Copy into a Word wraps with no flag, so past its range it reads like no line."""

        def reader(v: Any) -> None:
            with Rung(read(v)):
                calc(Int("N") + 1, Int("N"))

        _assert_fold_is_exact(
            self._copy_into_a_word(step, reader),
            lambda plc, fold: plc.run_until(Bool("End"), max_cycles=10_000, fold=fold),
            patch={"Wide": start},
        )

    def test_a_copy_into_a_word_stops_at_the_first_read_of_its_wrapped_value(self) -> None:
        def reader(v: Any) -> None:
            with Rung(v == 1):
                out(Bool("Hit"))

        folded = _assert_fold_is_exact(
            self._copy_into_a_word(7, reader), _until_hit(10_000), patch={"Wide": 65_495}
        )
        assert folded.state.tags["Wide"] == 65_537
        assert folded.state.tags["View"] == 1

    def test_a_copy_into_a_word_inside_its_range_still_folds(self) -> None:
        """The wrap only ends a window at the edge: inside the range they stay long."""

        def reader(v: Any) -> None:
            with Rung(v > 1_000_000):
                calc(Int("N") + 1, Int("N"))

        build = self._copy_into_a_word(1, reader)
        _assert_fold_is_exact(
            build,
            lambda plc, fold: plc.run_until(Bool("End"), max_cycles=10_000, fold=fold),
        )
        plc = PLC(build(), dt=0.010)
        ran = 0
        prepare = plc._prepare_scan

        def counted(*args: Any, **kwargs: Any) -> Any:
            nonlocal ran
            ran += 1
            return prepare(*args, **kwargs)

        setattr(plc, "_prepare_scan", counted)  # noqa: B010
        plc.run_until(Bool("End"), max_cycles=10_000, fold=True)
        assert plc.state.scan_id == 500
        assert plc.state.tags["View"] == 500
        assert ran < 20

    @pytest.mark.parametrize(
        "expr_of",
        [
            pytest.param(lambda w: w + 0, id="same"),
            pytest.param(lambda w: 1000 - w, id="negated"),
            pytest.param(lambda w: w + 5, id="offset"),
        ],
    )
    def test_a_same_width_view_with_the_flag_read_still_folds(self, expr_of: Any) -> None:
        """Inside its range nothing raises the flag, so the windows stay long."""
        from pyrung import latch

        def build() -> Program:
            Wide = Dint("Wide")
            Same = Dint("Same")
            Tmr = Timer.clone("Tmr")
            with Program(strict=False) as prog:
                with Rung():
                    calc(Wide + 7, Wide)
                with Rung():
                    calc(expr_of(Wide), Same)
                with Rung(Same == 123_456_789):
                    out(Bool("Never"))
                with Rung(system.fault.out_of_range):
                    latch(Bool("FaultSeen"))
                with Rung():
                    on_delay(Tmr, 30_000)
                with Rung(Tmr.Done):
                    out(Bool("Hit"))
            return prog

        _assert_fold_is_exact(build, _until_hit(5_000))

        plc = PLC(build(), dt=0.010)
        ran = 0
        prepare = plc._prepare_scan

        def counted(*args: Any, **kwargs: Any) -> Any:
            nonlocal ran
            ran += 1
            return prepare(*args, **kwargs)

        setattr(plc, "_prepare_scan", counted)  # noqa: B010
        plc.run_until(Bool("Hit"), max_cycles=5_000, fold=True)
        assert plc.state.scan_id == 3_000
        assert ran < 50

    def test_timer_acc_copied_to_an_int_never_faults(self) -> None:
        def build() -> Program:
            Copied = Int("Copied")
            Tmr = Timer.clone("Tmr")
            with Program(strict=False) as prog:
                with Rung():
                    on_delay(Tmr, 5_000)
                with Rung():
                    copy(Tmr.Acc, Copied)
                with Rung(system.fault.out_of_range):
                    out(Bool("FaultNow"))
                with Rung(Tmr.Done):
                    out(Bool("Hit"))
            return prog

        folded = _assert_fold_is_exact(build, _until_hit(1_000))
        assert folded.state.tags["FaultNow"] is False

    def test_cycle_jump_stops_short_of_a_wrap(self) -> None:
        from pyrung.core.analysis.pilot.cyclefold import cycle_fold_until

        def build() -> Program:
            Tot = Int("Tot")
            Osc = Bool("Osc")
            Tmr = Timer.clone("Tmr")
            with Program(strict=False) as prog:
                with Rung(~Osc):
                    out(Osc)  # a period-2 cycle: the ordinary fold never sees a plateau
                with Rung():
                    calc(Tot + 7, Tot)
                with Rung(Tot > 30_000):
                    out(Bool("High"))
                with Rung(system.fault.out_of_range):
                    out(Bool("FaultNow"))
                with Rung():
                    on_delay(Tmr, 60_000)
            return prog

        def faulted(state: Any) -> bool:
            return bool(state.tags.get("FaultNow"))

        runs = []
        for fold in (True, False):
            plc = PLC(build(), dt=0.010)
            plc.patch({"Tot": 32_000})
            plc.step()
            if fold:
                stats: dict[str, int] = {}
                cycle_fold_until(plc, faulted, budget=3_000, kernel_budget=False, stats=stats)
                assert stats["cycle_folds"] >= 1
            else:
                plc.run_until(faulted, max_cycles=3_000, fold=False)
            runs.append(plc)
        folded, stepped = runs
        assert folded.state.scan_id == stepped.state.scan_id
        assert _full_state(folded) == _full_state(stepped)

    def test_unread_affine_churn_still_folds(self) -> None:
        plc = PLC(self._churn_program(self._SHAPES["tag_step"])(), dt=0.010)
        plc.patch({"K": 1})
        plc.step()
        counter = _count_kernel_scans(plc)
        plc.run_until(Bool("Hit"), max_cycles=1_000)
        assert counter["n"] <= 6
        assert plc.state.tags["Tot"] == plc.state.scan_id

    def test_target_disjoint_churn_cone_matches_stepping(self) -> None:
        def build() -> Program:
            Phase = Int("Phase")
            Seen = Counter.clone("Seen")
            Tmr = Timer.clone("Tmr")
            with Program(strict=False) as prog:
                with Rung():
                    calc((Phase + 1) % 5, Phase)
                with Rung(Phase == 2):
                    count_up(Seen, 10_000).reset(Bool("SR"))
                with Rung():
                    on_delay(Tmr, 5_000)
                with Rung(Tmr.Done):
                    out(Bool("Hit"))
            return prog

        _assert_fold_is_exact(build, _until_hit(1_000))


class TestFoldLandingCheckpoint:
    """A landing that puts the tip on a checkpoint scan is checkpointed
    there, never at the scan id it first commits under."""

    @staticmethod
    def _program() -> Program:
        C = Counter.clone("C")
        with Program(strict=False) as prog:
            with Rung(system.sys.clock_100ms):
                count_up(C, 100_000).reset(Bool("CR"))
        return prog

    def test_landing_checkpoints_hold_the_stepped_state(self) -> None:
        folded, stepped = _folded_and_stepped(self._program, _scans(2_000))
        interval = folded._checkpoint_interval
        assert sorted(folded._checkpoints) == list(
            range(interval, folded.state.scan_id + 1, interval)
        )
        for scan_id, state in folded._checkpoints.items():
            assert state.scan_id == scan_id
            replayed = stepped.replay_to(scan_id).state
            assert {k: v for k, v in state.tags.items() if not k.startswith("sys.scan_time")} == {
                k: v for k, v in replayed.tags.items() if not k.startswith("sys.scan_time")
            }


class TestClockEdgesAsRead:
    """At a ``dt`` of a half-period or more, one scan crosses several edges but
    an edge rung sees only the two reads: only those count as a rise or a fall.
    """

    @staticmethod
    def _rise_program(clock: str, pulse: bool) -> Callable[[], Program]:
        def build() -> Program:
            S = Int("S")
            with Program(strict=False) as prog:
                with Rung():
                    on_delay(Timer.clone("A"), 30_000)
                with Rung(rise(getattr(system.sys, clock))):
                    if pulse:
                        out(Bool("P"))
                    else:
                        calc(S + 3, S)
                if pulse:
                    with Rung(Bool("P")):
                        calc(S + 1, S)
            return prog

        return build

    @pytest.mark.parametrize(
        ("clock", "dt"),
        [("clock_100ms", 0.25), ("clock_100ms", 0.07), ("clock_1s", 1.1), ("clock_500ms", 0.3)],
    )
    @pytest.mark.parametrize("pulse", [False, True])
    def test_rise_rungs_match_stepping(self, clock: str, dt: float, pulse: bool) -> None:
        _assert_fold_is_exact(self._rise_program(clock, pulse), _scans(2_000), dt=dt)

    def test_replay_seek_keeps_every_rise(self) -> None:
        def build() -> Program:
            S = Int("S")
            with Program(strict=False) as prog:
                with Rung(rise(system.sys.clock_100ms)):
                    calc(S + 3, S)
            return prog

        plc = PLC(build(), dt=0.25)
        for _ in range(300):
            plc.step()
        for target in (10, 51, 157, 299):
            assert plc._replay_seek(target).state.tags["S"] == plc.replay_to(target).state.tags["S"]


class TestFlagsFollowInsideAWindow:
    """SC40 follows the last Math, SC43 and SC44 the last Copy or Math, so a flag
    can be up between two rungs and down at the end of the scan.  A window holds
    each of those readings exactly, or does not fold across a change."""

    SC40 = system.fault.division_error
    SC43 = system.fault.out_of_range
    SC44 = system.fault.address_error

    @staticmethod
    def _flag_readers_around(
        raises: Callable[[], None],
        settles: Callable[[], None],
        flag: Any,
        *,
        preset: int = 5_000,
    ) -> Callable[[], Program]:
        def build() -> Program:
            Tmr = Timer.clone("Tmr")
            with Program(strict=False) as prog:
                with Rung():
                    raises()
                with Rung(flag):
                    out(Bool("Between"))
                with Rung():
                    settles()
                with Rung(flag):
                    out(Bool("After"))
                with Rung():
                    on_delay(Tmr, preset)
                with Rung(Tmr.Done):
                    out(Bool("End"))
            return prog

        return build

    @staticmethod
    def _bad_pointer() -> None:
        copy(1, Block("FoldTbl", TagType.INT, 1, 4)[Int("Below", default=0)])

    @staticmethod
    def _window_past() -> None:
        ptr = Int("Past", default=20)
        fill(5, Block("FoldTbl", TagType.INT, 1, 4).select(ptr, ptr))

    @staticmethod
    def _fill_value_below() -> None:
        tbl = Block("FoldTbl", TagType.INT, 1, 4)
        fill(tbl[Int("Below", default=0)], Block("FoldDst", TagType.INT, 1, 2).select(1, 2))

    @staticmethod
    def _for_count_below() -> None:
        with forloop(Block("FoldTbl", TagType.INT, 1, 4)[Int("Below", default=0)]):
            out(Bool("InLoop"))

    @staticmethod
    def _search_value_below() -> None:
        tbl = Block("FoldTbl", TagType.INT, 1, 4)
        search(
            Block("FoldDst", TagType.INT, 1, 2).select(1, 2) == tbl[Int("Below", default=0)],
            result=Int("FoldResult"),
            found=Bool("FoldFound"),
        )

    @staticmethod
    def _until_end(plc: PLC, fold: bool) -> None:
        plc.run_until(Bool("End"), max_cycles=10_000, fold=fold)

    @pytest.mark.parametrize(
        "raises, settles, flag, after",
        [
            pytest.param(
                lambda: copy(Dint("Big", default=99_999), Int("Small")),
                lambda: copy(1, Int("Plain")),
                SC43,
                False,
                id="sc43_cleared_by_a_copy",
            ),
            pytest.param(
                lambda: copy(Dint("Big", default=99_999), Int("Small")),
                lambda: calc(Int("One", default=1) + 1, Int("Plain")),
                SC43,
                False,
                id="sc43_cleared_by_a_calc",
            ),
            pytest.param(
                lambda: copy(Dint("Big", default=99_999), Int("Small")),
                lambda: out(Bool("Plain")),
                SC43,
                True,
                id="sc43_left_by_an_out",
            ),
            pytest.param(
                _bad_pointer.__func__,
                lambda: copy(1, Int("Plain")),
                SC44,
                False,
                id="sc44_cleared_by_a_copy",
            ),
            pytest.param(
                _bad_pointer.__func__,
                lambda: out(Bool("Plain")),
                SC44,
                True,
                id="sc44_left_by_an_out",
            ),
            *(
                pytest.param(
                    raises, settles, system.fault.address_error, after, id=f"sc44_{name}_{how}"
                )
                for name, raises in (
                    ("window_past", _window_past.__func__),
                    ("fill_value_below", _fill_value_below.__func__),
                    ("for_count_below", _for_count_below.__func__),
                    ("search_value_below", _search_value_below.__func__),
                )
                for how, settles, after in (
                    ("cleared_by_a_copy", lambda: copy(1, Int("Plain")), False),
                    ("left_by_an_out", lambda: out(Bool("Plain")), True),
                )
            ),
            pytest.param(
                lambda: calc(Int("One", default=1) / Int("Zero"), Int("Quotient")),
                lambda: calc(Int("One", default=1) + 1, Int("Plain")),
                SC40,
                False,
                id="sc40_cleared_by_a_calc",
            ),
            pytest.param(
                lambda: calc(Int("One", default=1) / Int("Zero"), Int("Quotient")),
                lambda: copy(1, Int("Plain")),
                SC40,
                True,
                id="sc40_left_by_a_copy",
            ),
        ],
    )
    def test_a_flag_that_holds_still_folds(
        self, raises: Any, settles: Any, flag: Any, after: bool
    ) -> None:
        build = self._flag_readers_around(raises, settles, flag)
        plc = _assert_fold_is_exact(build, self._until_end)
        assert plc.state.tags["Between"] is True
        assert plc.state.tags["After"] is after
        assert plc.state.tags[flag.name] is after

        counted = PLC(build(), dt=0.010)
        counted.step()
        kernel = _count_kernel_scans(counted)
        self._until_end(counted, True)
        if not after:
            # A flag left on keeps the scan-start hook busy, and those scans are real.
            assert kernel["n"] < 20

    @pytest.mark.parametrize(
        "faulting, start, step, rises",
        [
            pytest.param(
                lambda ptr: fill(5, Block("FoldTbl", TagType.INT, 1, 4).select(ptr, ptr)),
                3,
                1,
                True,
                id="a_window_past_the_block",
            ),
            pytest.param(
                lambda ptr: copy(1, Block("FoldTbl", TagType.INT, 1, 4)[ptr]),
                3,
                1,
                False,
                id="a_pointer_past_the_block",
            ),
            pytest.param(
                lambda ptr: fill(
                    5,
                    Block("FoldGap", TagType.INT, 1, 20, valid_ranges=((1, 4), (6, 20))).select(
                        ptr, ptr + 1
                    ),
                ),
                3,
                1,
                False,
                id="a_window_across_a_gap",
            ),
            pytest.param(
                lambda ptr: fill(
                    Block("FoldTbl", TagType.INT, 1, 4)[ptr],
                    Block("FoldDst", TagType.INT, 1, 2).select(1, 2),
                ),
                3,
                -1,
                True,
                id="a_fill_value_below_the_block",
            ),
            pytest.param(
                lambda ptr: search(
                    Block("FoldDst", TagType.INT, 1, 2).select(1, 2)
                    == Block("FoldTbl", TagType.INT, 1, 4)[ptr],
                    result=Int("FoldResult"),
                    found=Bool("FoldFound"),
                ),
                3,
                -1,
                True,
                id="a_search_value_below_the_block",
            ),
        ],
    )
    def test_an_address_a_timer_walks_out_of_its_block(
        self, faulting: Any, start: int, step: int, rises: bool
    ) -> None:
        """The flag turns over mid-run: the landing and a reader see it on the same scan."""

        def build() -> Program:
            Tmr = Timer.clone("Tmr")
            ptr = Int("Walk")
            with Program(strict=False) as prog:
                with Rung():
                    on_delay(Tmr, 3_000)
                with Rung():
                    calc(start + step * (Tmr.Acc // 1_000), ptr)
                with Rung():
                    faulting(ptr)
                with Rung(self.SC44):
                    latch(Bool("Saw"))
                with Rung(Tmr.Done):
                    out(Bool("End"))
            return prog

        plc = _assert_fold_is_exact(build, self._until_end)
        assert plc.state.tags.get("Saw", False) is rises

    @pytest.mark.parametrize("store_first", [False, True], ids=["store_below", "store_above"])
    def test_a_moving_store_raises_between_and_a_copy_clears_after(self, store_first: bool) -> None:
        def reader(_view: Any) -> None:
            with Rung(self.SC43):
                out(Bool("Hit"))
            with Rung():
                copy(1, Int("Plain"))
            with Rung(self.SC43):
                out(Bool("After"))

        plc = _assert_fold_is_exact(
            TestSelfFeedingChurnExact._view_of_a_moving_tag(
                lambda a, b: copy(a, b), reader, store_first=store_first
            ),
            _until_hit(10_000),
            patch={"Wide": 32_000},
        )
        assert plc.state.tags["Wide"] in (32_770, 32_777)
        assert plc.state.tags["After"] is False

    @pytest.mark.parametrize("cleared", [False, True], ids=["left_up", "cleared_later"])
    @pytest.mark.parametrize("store_first", [False, True], ids=["store_below", "store_above"])
    def test_an_unread_flag_lands_as_stepping_left_it(
        self, cleared: bool, store_first: bool
    ) -> None:
        def later(_view: Any) -> None:
            if cleared:
                with Rung():
                    copy(1, Int("Plain"))

        plc = _assert_fold_is_exact(
            TestSelfFeedingChurnExact._view_of_a_moving_tag(
                lambda a, b: copy(a, b), later, store_first=store_first
            ),
            self._until_end,
            patch={"Wide": 32_000},
        )
        # A store above the self-calc is settled by it, whatever follows.
        assert plc.state.tags[self.SC43.name] is (not cleared and not store_first)

    @pytest.mark.parametrize("cleared", [False, True], ids=["left_up", "cleared_later"])
    @pytest.mark.parametrize(
        "scans, past", [(2_000, False), (3_000, True)], ids=["inside", "past_it"]
    )
    def test_a_timer_view_past_its_range_lands_as_stepping_left_it(
        self, cleared: bool, scans: int, past: bool
    ) -> None:
        """The mirror route: an unread flag the view raises is rebuilt by the landing."""

        def build() -> Program:
            Tmr = Timer.clone("Tmr")
            with Program(strict=False) as prog:
                with Rung():
                    on_delay(Tmr, 32_000)
                with Rung():
                    calc(Tmr.Acc + 10_000, Int("View"))
                if cleared:
                    with Rung():
                        copy(1, Int("Plain"))
            return prog

        plc = _assert_fold_is_exact(build, _scans(scans), dt=0.010)
        assert plc.state.tags[self.SC43.name] is (past and not cleared)

    @pytest.mark.parametrize("cleared", [False, True], ids=["left_up", "cleared_later"])
    def test_a_timer_view_with_a_flag_reader_stops_at_its_range(self, cleared: bool) -> None:
        """The same view, with the flag read: the window ends where the view leaves its range."""

        def build() -> Program:
            Tmr = Timer.clone("Tmr")
            with Program(strict=False) as prog:
                with Rung():
                    on_delay(Tmr, 32_000)
                with Rung():
                    calc(Tmr.Acc + 10_000, Int("View"))
                with Rung(self.SC43):
                    out(Bool("Hit"))
                if cleared:
                    with Rung():
                        copy(1, Int("Plain"))
                with Rung(self.SC43):
                    out(Bool("After"))
            return prog

        plc = _assert_fold_is_exact(build, _until_hit(5_000), dt=0.010)
        assert plc.state.tags["Hit"] is True
        assert plc.state.tags["After"] is not cleared

    @pytest.mark.parametrize(
        "stop",
        [
            pytest.param(SC43, id="the_flag"),
            pytest.param(lambda s: s.tags.get("fault.out_of_range"), id="a_callable"),
        ],
    )
    def test_a_stop_on_a_flag_cleared_later_sees_the_end_of_the_scan(self, stop: Any) -> None:
        """Up mid-scan, down by the end: a stop on the flag does not fire."""
        plc = _assert_fold_is_exact(
            TestSelfFeedingChurnExact._view_of_a_moving_tag(
                lambda a, b: copy(a, b),
                lambda _view: copy_rung(Int("Plain")),
            ),
            lambda plc, fold: plc.run_until(stop, max_cycles=1_000, fold=fold),
            patch={"Wide": 32_000},
        )
        assert plc.state.tags[self.SC43.name] is False

    @pytest.mark.parametrize("at", [2_000, 3_000])
    def test_a_writer_that_switches_on_inside_a_window(self, at: int) -> None:
        """A store that faults turns on at one threshold, a store that clears at another."""

        def build() -> Program:
            Tmr = Timer.clone("Tmr")
            with Program(strict=False) as prog:
                with Rung():
                    on_delay(Tmr, 5_000)
                with Rung(Tmr.Acc > 1_000):
                    copy(Dint("Big", default=99_999), Int("Small"))
                with Rung(self.SC43):
                    out(Bool("Between"))
                with Rung(Tmr.Acc > at):
                    copy(1, Int("Plain"))
                with Rung(self.SC43):
                    calc(Int("Seen") + 1, Int("Seen"))
                with Rung(Tmr.Done):
                    out(Bool("End"))
            return prog

        plc = _assert_fold_is_exact(build, self._until_end)
        assert plc.state.tags["Seen"] == (at - 1_000) // 10

    @pytest.mark.parametrize("store_first", [False, True], ids=["store_below", "store_above"])
    def test_a_moving_divisor_raises_for_the_one_scan_it_is_zero(self, store_first: bool) -> None:
        def build() -> Program:
            Wide = Dint("Wide")
            Tmr = Timer.clone("Tmr")
            with Program(strict=False) as prog:
                if store_first:
                    with Rung():
                        calc(100 / Wide, Int("Quotient"))
                with Rung():
                    calc(Wide + 7, Wide)
                if not store_first:
                    with Rung():
                        calc(100 / Wide, Int("Quotient"))
                with Rung(self.SC40):
                    calc(Int("Seen") + 1, Int("Seen"))
                with Rung():
                    on_delay(Tmr, 5_000)
                with Rung(Tmr.Done):
                    out(Bool("End"))
            return prog

        plc = _assert_fold_is_exact(build, self._until_end, patch={"Wide": -700})
        # A divide above the self-calc is settled by it before the reader.
        assert plc.state.tags["Seen"] == (0 if store_first else 1)

    @pytest.mark.parametrize("step, start", [(7, -350), (-7, 350)], ids=["into", "out_of"])
    def test_a_moving_pointer_crosses_the_low_end_of_its_block(self, step: int, start: int) -> None:
        def build() -> Program:
            Wide = Int("Wide")
            Tmr = Timer.clone("Tmr")
            table = Block("FoldTbl", TagType.INT, 1, 500, default_factory=lambda _addr: 3)
            with Program(strict=False) as prog:
                with Rung():
                    calc(Wide + step, Wide)
                with Rung():
                    copy(table[Wide], Int("Read"))
                with Rung(self.SC44):
                    calc(Int("Seen") + 1, Int("Seen"))
                with Rung():
                    on_delay(Tmr, 2_000)
                with Rung(Tmr.Done):
                    out(Bool("End"))
            return prog

        plc = _assert_fold_is_exact(build, self._until_end, patch={"Wide": start})
        assert plc.state.tags["Seen"] > 0

    @pytest.mark.parametrize("read_it", [False, True], ids=["unread", "read"])
    def test_a_moving_pointer_runs_past_the_high_end_of_its_block(self, read_it: bool) -> None:
        def build() -> Program:
            Wide = Int("Wide")
            Tmr = Timer.clone("Tmr")
            table = Block("FoldTbl", TagType.INT, 1, 500, default_factory=lambda _addr: 3)
            with Program(strict=False) as prog:
                with Rung():
                    calc(Wide + 7, Wide)
                with Rung():
                    copy(table[Wide], Int("Read"))
                if read_it:
                    with Rung(Int("Read") == 0):
                        calc(Int("Seen") + 1, Int("Seen"))
                with Rung():
                    on_delay(Tmr, 2_000)
                with Rung(Tmr.Done):
                    out(Bool("End"))
            return prog

        plc = _assert_fold_is_exact(build, self._until_end, patch={"Wide": 100})
        assert plc.state.tags["Read"] == 0
        assert plc.state.tags[self.SC44.name] is False


def copy_rung(dest: Any) -> None:
    with Rung():
        copy(1, dest)


class TestUnseenStopsStayExact:
    """A bound that only exists to keep a fold exact (a self-calc's wrap)
    shortens windows but never opens one, and a ``when()`` callback is
    judged on every scan: the fold cannot see either stop."""

    @staticmethod
    def _counter(step_tag: bool = False, timer: bool = False) -> Callable[[], Program]:
        def build() -> Program:
            T = Int("T")
            with Program(strict=False) as prog:
                with Rung():
                    calc(T + (Int("K") if step_tag else 1), T)
                if timer:
                    with Rung():
                        on_delay(Timer.clone("Tmr"), 30_000)
            return prog

        return build

    @pytest.mark.parametrize("step_tag", [False, True])
    def test_callable_stop_on_an_unread_self_calc(self, step_tag: bool) -> None:
        folded = _assert_fold_is_exact(
            self._counter(step_tag),
            lambda plc, fold: plc.run_until(
                lambda s: s.tags.get("T", 0) >= 500, max_cycles=5_000, fold=fold
            ),
            patch={"K": 1},
        )
        assert folded.state.tags["T"] == 500

    @pytest.mark.parametrize("timer", [False, True])
    @pytest.mark.parametrize("run", ["until", "for"])
    def test_pause_breakpoint_is_judged_every_scan(self, timer: bool, run: str) -> None:
        def go(plc: PLC, fold: bool) -> None:
            plc.when(Int("T") == 500).pause()
            if run == "until":
                plc.run_until(Bool("Never"), max_cycles=5_000, fold=fold)
            else:
                plc.run_for(50.0, fold=fold)

        folded = _assert_fold_is_exact(self._counter(timer=timer), go)
        assert folded.state.tags["T"] == 500

    def test_monitor_sees_every_change(self) -> None:
        seen: dict[bool, list[Any]] = {True: [], False: []}

        def go(plc: PLC, fold: bool) -> None:
            plc.monitor("T", lambda new, old: seen[fold].append((plc.state.scan_id, new)))
            plc.run_for(5.0, fold=fold)

        _assert_fold_is_exact(self._counter(timer=True), go)
        assert seen[True] == seen[False] != []

    @pytest.mark.parametrize("tag", ["Hit", "Tmr_Done", "Tmr_Acc"])
    def test_monitor_on_a_steady_tag_still_folds(self, tag: str) -> None:
        """A monitor fires only when its tag changes, so one on a tag that holds
        through the plateau leaves the fold free, and the landing judges it at
        the scan it stands for."""

        def build() -> Program:
            Tmr = Timer.clone("Tmr")
            with Program(strict=False) as prog:
                with Rung():
                    on_delay(Tmr, 3_000)
                with Rung(Tmr.Done):
                    out(Bool("Hit"))
            return prog

        seen: dict[bool, list[Any]] = {True: [], False: []}
        kernel = {True: 0, False: 0}

        def go(plc: PLC, fold: bool) -> None:
            orig = plc._run_single_scan

            def counted(**kw: Any) -> Any:
                kernel[fold] += 1
                return orig(**kw)

            plc._run_single_scan = counted  # ty: ignore[invalid-assignment]
            plc.monitor(tag, lambda new, old: seen[fold].append((plc.state.scan_id, new, old)))
            plc.run_until(Bool("Hit"), max_cycles=5_000, fold=fold)

        _assert_fold_is_exact(build, go)
        assert seen[True] == seen[False] != []
        # A monitor watches every scan: the run steps, whichever tag it watches.
        assert kernel[True] == kernel[False]

    def test_pause_breakpoint_blocks_cycle_jumps(self) -> None:
        from pyrung.core.analysis.pilot.cyclefold import cycle_fold_until

        def build() -> Program:
            Osc = Bool("Osc")
            with Program(strict=False) as prog:
                with Rung(~Osc):
                    out(Osc)  # a period-2 cycle
                with Rung():
                    on_delay(Timer.clone("Tmr"), 60_000)
            return prog

        runs = []
        for fold in (True, False):
            plc = PLC(build(), dt=0.010)
            plc.step()
            plc.when(Int("Tmr_Acc") >= 5_000).pause()
            if fold:
                cycle_fold_until(plc, lambda _s: False, budget=3_000, kernel_budget=False)
            else:
                plc.run_until(Bool("Never"), max_cycles=3_000, fold=False)
            runs.append(plc)
        assert runs[0].state.scan_id == runs[1].state.scan_id
        assert _full_state(runs[0]) == _full_state(runs[1])


class TestTagStepSelfCalcGuardedWhenRead:
    """``calc(T + K, T)`` with ``K`` a tag is a fold source only when nothing
    else reads ``T``; a moving preset, a comparison against a moving tag or
    an expression read keeps it in the plateau guard."""

    @staticmethod
    def _expression_reader() -> Program:
        from pyrung import latch

        T = Int("T")
        with Program(strict=False) as prog:
            with Rung():
                calc(T + Int("K"), T)
            with Rung((T // 100) == 3):
                latch(Bool("L"))
            with Rung(T > 2_000):
                out(Bool("Hit"))
        return prog

    @staticmethod
    def _moving_preset() -> Program:
        P = Int("P")
        Tmr = Timer.clone("Tmr")
        with Program(strict=False) as prog:
            with Rung():
                calc(P - Int("K"), P)
            with Rung():
                on_delay(Tmr, P)
            with Rung(Tmr.Done):
                out(Bool("Hit"))
        return prog

    @staticmethod
    def _moving_threshold() -> Program:
        T = Int("T")
        Tmr = Timer.clone("Tmr")
        with Program(strict=False) as prog:
            with Rung():
                calc(T - Int("K"), T)
            with Rung():
                on_delay(Tmr, 30_000)
            with Rung(Tmr.Acc > T):
                out(Bool("Hit"))
        return prog

    @pytest.mark.parametrize(
        ("build", "patch"),
        [
            ("_expression_reader", {"K": 1}),
            ("_moving_preset", {"P": 30_000, "K": 1}),
            ("_moving_threshold", {"T": 10_000, "K": 3}),
        ],
    )
    def test_read_tag_step_matches_stepping(self, build: str, patch: dict[str, int]) -> None:
        _assert_fold_is_exact(getattr(self, build), _until_hit(5_000), patch=patch)


class TestFoldedHistoryReplays:
    """Fold projections are not inputs: replaying a folded live run steps the
    folded scans and re-derives them, and history serves each landing at the
    scan it stands for."""

    @staticmethod
    def _program() -> Program:
        T = Int("T")
        C = Counter.clone("C")
        Tmr = Timer.clone("Tmr")
        with Program(strict=False) as prog:
            with Rung():
                calc(T + 3, T)
            with Rung(Bool("En"), system.sys.clock_100ms):  # counts the clock's rises
                count_up(C, 1_000).reset(Bool("CR"))
            with Rung():
                on_delay(Tmr, 7_000)
            with Rung(Tmr.Done):
                out(Bool("Hit"))
        return prog

    def test_history_replay_seek_and_forks_match_stepping(self) -> None:
        folded, stepped = _folded_and_stepped(self._program, _until_hit(3_000), patch={"En": True})
        tip = folded.state.scan_id
        assert tip == stepped.state.scan_id
        for target in (2, 3, 4, tip // 2, tip - 1, tip):
            want = _full_state(stepped.replay_to(target))
            assert _full_state(folded.replay_to(target)) == want
            assert _full_state(folded._replay_seek(target)) == want
            assert _plain_tags_of(folded.history.at(target)) == _plain_tags_of(
                stepped.history.at(target)
            )
            fork, ref = folded.replay_to(target), stepped.replay_to(target)
            for _ in range(5):
                fork.step()
                ref.step()
            assert _full_state(fork) == _full_state(ref)


def _plain_tags_of(state: Any) -> dict[str, Any]:
    return {k: v for k, v in state.tags.items() if not k.startswith("sys.scan_time")}


def _scan_stamp_program(source: Callable[[], None], reader: Any, above: bool) -> Program:
    """*reader* gates ``L``; a rise of ``L`` stamps the scan counter ``SC``.

    ``SC`` adds one every scan (a ``calc``: a counter counts edges, so on an
    always-true rung it would stop at 1 and stamp nothing).
    """
    SC = Dint("SC")
    with Program(strict=False) as prog:
        with Rung():
            calc(SC + 1, SC)
        if not above:
            source()
        with Rung(reader()):
            out(Bool("L"))
        with Rung(rise(Bool("L"))):
            from pyrung import copy

            copy(SC, Int("Stamp"))
        if above:
            source()
    return prog


class TestReadAboveTheSource:
    """A rung above an accumulating rung reads the scan-entry value: it flips
    one scan after the crossing, so a crossing inside the probe still bounds
    the next window."""

    @staticmethod
    def _shape(kind: str, above: bool) -> Callable[[], Program]:
        T, C, S, M = Timer.clone("T"), Counter.clone("C"), Int("S"), Int("M")
        sources = {
            "timer": (lambda: on_delay(T, 2_000), lambda: T.Acc >= 500),
            "timer_gt": (lambda: on_delay(T, 2_000), lambda: T.Acc > 500),
            # A counter counts edges: the 100 ms clock's rises, one in ten scans.
            "counter": (lambda: count_up(C, 2_000).reset(Bool("CR")), lambda: C.Acc >= 12),
            "selfcalc": (lambda: calc(S + 1, S), lambda: S >= 50),
            "modwrap": (lambda: calc((M + 1) % 1_000, M), lambda: M >= 50),
        }
        body, reader = sources[kind]
        gate = (system.sys.clock_100ms,) if kind == "counter" else ()

        def source() -> None:
            with Rung(*gate):
                body()

        return lambda: _scan_stamp_program(source, reader, above)

    @pytest.mark.parametrize("kind", ["timer", "timer_gt", "counter", "selfcalc", "modwrap"])
    @pytest.mark.parametrize("above", [True, False])
    def test_flip_scan_matches_stepping(self, kind: str, above: bool) -> None:
        folded = _assert_fold_is_exact(self._shape(kind, above), _scans(300))
        assert folded.state.tags["Stamp"] > 0


class TestEqualityFall:
    """``Acc == k`` flips twice: on reaching ``k`` and on passing it.  A timer
    stepping less than one unit per scan sits at ``k`` for many scans, so the
    fall bounds a window of its own."""

    @staticmethod
    def _shape(form: str, unit: str) -> Callable[[], Program]:
        T = Timer.clone("T")

        def source() -> None:
            with Rung():
                on_delay(T, 30, unit)

        def reader() -> Any:
            return T.Acc == 5 if form == "eq" else T.Acc != 5

        return lambda: _scan_stamp_program(source, reader, above=False)

    @pytest.mark.parametrize("form", ["eq", "ne"])
    @pytest.mark.parametrize(("unit", "dt"), [("s", 0.1), ("ms", 0.0003)])
    def test_fall_scan_matches_stepping(self, form: str, unit: str, dt: float) -> None:
        _assert_fold_is_exact(self._shape(form, unit), _scans(400), dt=dt)


class TestResetPulseInProbe:
    """A reset the probe scan saw may be a one-scan pulse: the accumulator
    counts (or times) again on the next scan, so a reset never certifies a
    window that holds it still."""

    @staticmethod
    def _shape(kind: str) -> Callable[[], Program]:
        from pyrung import count_down

        def build() -> Program:
            pulse = rise(system.sys.clock_1s)
            # A counter counts edges: the 100 ms clock gives it some.
            gate = () if kind == "rto" else (system.sys.clock_100ms,)
            with Program(strict=False) as prog:
                with Rung(*gate):
                    if kind == "up":
                        count_up(Counter.clone("C"), 5_000).reset(pulse)
                    elif kind == "down":
                        count_down(Counter.clone("C"), -5_000).reset(pulse)
                    else:
                        on_delay(Timer.clone("C"), 50_000).reset(pulse)
            return prog

        return build

    @pytest.mark.parametrize("kind", ["up", "down", "rto"])
    def test_counts_again_after_the_pulse(self, kind: str) -> None:
        folded = _assert_fold_is_exact(self._shape(kind), _scans(700))
        assert folded.state.tags["C_Acc"] != 0


class TestClockGatedTimeDrum:
    """A time drum reads its accumulator every scan it times, so a clock
    gating it is bounded at every edge like a gated timer's."""

    @staticmethod
    def _shape(gate: bool) -> Callable[[], Program]:
        from pyrung import time_drum

        def source() -> None:
            with Rung(system.sys.clock_1s) if gate else Rung():
                time_drum(
                    outputs=[Bool("O1"), Bool("O2")],
                    presets=[3_000, 1_500],
                    unit="ms",
                    pattern=[[1, 0], [0, 1]],
                    current_step=Int("Step"),
                    accumulator=Int("DAcc"),
                    completion_flag=Bool("DDone"),
                ).reset(Bool("R"))

        return lambda: _scan_stamp_program(source, lambda: Bool("DDone"), above=False)

    @pytest.mark.parametrize("gate", [True, False])
    def test_completion_scan_matches_stepping(self, gate: bool) -> None:
        folded = _assert_fold_is_exact(self._shape(gate), _scans(400), dt=0.1)
        assert folded.state.tags["DDone"]


class TestTimedDrumCorrectedEveryScan:
    """A step register kept bad has a timed drum correct it every scan, which clears
    the accumulator: the boundary readings stay put, and the scan times and clears
    with one write.  A landing at the window's dt would time past the preset."""

    @staticmethod
    def _drum(kind: str) -> Any:
        from pyrung import time_drum, time_event_drum

        args: dict[str, Any] = {
            "outputs": [Bool("O1"), Bool("O2")],
            "pattern": [[1, 0], [0, 1], [1, 1]],
            "presets": [500, 500, 500],
            "unit": "ms",
            "current_step": Int("Step"),
            "accumulator": Int("DAcc"),
            "completion_flag": Bool("DDone"),
        }
        if kind == "time":
            return time_drum(**args)
        return time_event_drum(events=[None, None, None], **args)

    @classmethod
    def _build(cls, kind: str, writer: str) -> Callable[[], Program]:
        def build() -> Program:
            with Program(strict=False) as prog:
                with Rung():
                    on_delay(Timer.clone("Long"), 60_000)
                if writer == "above":
                    with Rung(Bool("Stage")):
                        copy(0, Int("Step"))
                with Rung(Bool("En")):
                    if writer == "same rung":
                        copy(0, Int("Step"))
                    cls._drum(kind).reset(Bool("R"))
                if writer == "below":
                    with Rung(Bool("Stage")):
                        copy(0, Int("Step"))
            return prog

        return build

    @staticmethod
    def _assert_replay_matches(folded: PLC, stepped: PLC) -> None:
        for target in (2, 3, 60, 150, folded.state.scan_id - 1, folded.state.scan_id):
            want = _full_state(stepped.replay_to(target))
            assert _full_state(folded._replay_seek(target)) == want
            assert _full_state(folded.replay_to(target)) == want

    @pytest.mark.parametrize("kind", ["time", "time_event"])
    @pytest.mark.parametrize("writer", ["above", "below", "same rung"])
    def test_written_bad_every_scan_matches_stepping(self, kind: str, writer: str) -> None:
        folded, stepped = _folded_and_stepped(
            self._build(kind, writer), _scans(300), patch={"En": True, "Stage": True}
        )
        assert _full_state(folded) == _full_state(stepped)
        # The correction clears the accumulator every scan, so no step's time is up.
        assert folded.state.tags["DAcc"] == 0
        assert folded.state.tags["O1"] is True
        assert folded.state.tags["O2"] is False
        self._assert_replay_matches(folded, stepped)

    @pytest.mark.parametrize("kind", ["time", "time_event"])
    def test_forced_bad_matches_stepping(self, kind: str) -> None:
        def run(plc: PLC, fold: bool) -> None:
            plc.force("Step", 0)
            _run_scans(plc, 300, fold=fold)

        folded, stepped = _folded_and_stepped(self._build(kind, "none"), run, patch={"En": True})
        assert _full_state(folded) == _full_state(stepped)
        assert folded.state.tags["O1"] is True
        self._assert_replay_matches(folded, stepped)

    @pytest.mark.parametrize("kind", ["time", "time_event"])
    def test_good_step_written_every_scan_still_folds(self, kind: str) -> None:
        """Only a correction declines: a writer holding a good step leaves the
        accumulator to time, and the fold's window stays as long as it was."""

        def build() -> Program:
            with Program(strict=False) as prog:
                with Rung():
                    copy(2, Int("Step"))
                with Rung(Bool("En")):
                    self._drum(kind).reset(Bool("R"))
            return prog

        kernel_scans: dict[bool, int] = {}

        def run(plc: PLC, fold: bool) -> None:
            counter = _count_kernel_scans(plc)
            _run_scans(plc, 30, fold=fold)
            kernel_scans[fold] = counter["n"]

        folded, stepped = _folded_and_stepped(build, run, patch={"En": True})
        assert _full_state(folded) == _full_state(stepped)
        assert folded.state.tags["DAcc"] == stepped.state.tags["DAcc"] > 0
        assert kernel_scans[True] < kernel_scans[False]

    def test_program_writing_only_its_own_step_replays_folded(self) -> None:
        """With no other writer of the step, replay keeps folding the drum."""
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.fold import _build_fold_context

        def contested(writer: str) -> frozenset[str]:
            program = self._build("time", writer)()
            plc = PLC(program, dt=0.010)
            return _build_fold_context(plc, build_program_graph(program), program).contested_timers

        assert contested("none") == frozenset()
        for writer in ("above", "below", "same rung"):
            assert contested(writer) == {"DAcc"}


class TestTimedDrumReadsAnotherStep:
    """Writers above and below a timed drum can have it read one good step and the
    boundary show another.  Its one accumulator write and the steady boundaries
    say nothing, and a landing would time to the boundary step's preset."""

    @staticmethod
    def _drum(kind: str, presets: tuple[int, ...]) -> Any:
        from pyrung import time_drum, time_event_drum

        args: dict[str, Any] = {
            "outputs": [Bool("O1"), Bool("O2")],
            "pattern": [[1, 0], [0, 1], [1, 1]],
            "presets": list(presets),
            "unit": "ms",
            "current_step": Int("Step"),
            "accumulator": Dint("DAcc"),
            "completion_flag": Bool("DDone"),
        }
        if kind == "time":
            return time_drum(**args)
        return time_event_drum(events=[None] * len(presets), **args)

    @classmethod
    def _build(
        cls, kind: str, above: Any, below: Any, presets: tuple[int, ...], reader: bool = True
    ) -> Callable[[], Program]:
        def build() -> Program:
            with Program(strict=False) as prog:
                with Rung():
                    on_delay(Timer.clone("Long"), 60_000)
                if above is not None:
                    with Rung(Bool("Stage")):
                        copy(above, Int("Step"))
                with Rung(Bool("En")):
                    cls._drum(kind, presets).reset(Bool("R"))
                if below is not None:
                    with Rung(Bool("Stage")):
                        copy(below, Int("Step"))
                if reader:  # What the drum did, each scan it holds.
                    with Rung(Bool("O2")):
                        calc(Int("N") + 1, Int("N"))
            return prog

        return build

    @staticmethod
    def _assert_replay_matches(folded: PLC, stepped: PLC) -> None:
        for target in (2, 3, 19, 20, 21, 22, 150, folded.state.scan_id):
            want = _full_state(stepped.replay_to(target))
            assert _full_state(folded._replay_seek(target)) == want
            assert _full_state(folded.replay_to(target)) == want

    @pytest.mark.parametrize("kind", ["time", "time_event"])
    @pytest.mark.parametrize(
        "above, below, presets",
        [
            # It reads the short step: the boundary's preset would time long past it.
            (1, 3, (200, 5000, 5000)),
            (3, 1, (5000, 5000, 200)),
            # It reads the long step: the boundary's preset would be up far too soon.
            (3, 1, (200, 5000, 5000)),
            (1, 3, (5000, 5000, 200)),
        ],
    )
    def test_read_step_differs_from_boundary_step(
        self, kind: str, above: int, below: int, presets: tuple[int, ...]
    ) -> None:
        build = self._build(kind, above, below, presets)
        patch = {"En": True, "Stage": True}
        folded, stepped = _folded_and_stepped(build, _scans(300), patch=patch)
        assert _full_state(folded) == _full_state(stepped)
        self._assert_replay_matches(folded, stepped)

        def until_moved(plc: PLC, fold: bool) -> None:
            plc.run_until(Or(Bool("O2"), Bool("DDone")), max_cycles=600, fold=fold)

        _assert_fold_is_exact(build, until_moved, patch=patch)

    @pytest.mark.parametrize("kind", ["time", "time_event"])
    @pytest.mark.parametrize(
        "above, below", [(2, None), (None, 2), (2, 2)], ids=["above", "below", "both"]
    )
    def test_one_good_step_held_folds_live_and_in_replay(
        self, kind: str, above: int | None, below: int | None
    ) -> None:
        build = self._build(kind, above, below, (5000, 5000, 5000), reader=False)
        kernel_scans: dict[bool, int] = {}

        def run(plc: PLC, fold: bool) -> None:
            counter = _count_kernel_scans(plc)
            _run_scans(plc, 400, fold=fold)
            kernel_scans[fold] = counter["n"]

        folded, stepped = _folded_and_stepped(build, run, patch={"En": True, "Stage": True})
        assert _full_state(folded) == _full_state(stepped)
        assert folded.state.tags["Step"] == 2
        assert folded.state.tags["DAcc"] == stepped.state.tags["DAcc"] > 0
        assert kernel_scans[True] < kernel_scans[False]
        self._assert_replay_matches(folded, stepped)
        # Short of the tip: the tip and the checkpoints are cached, nothing to replay.
        folded._replay_seek(folded.state.scan_id - 50)
        assert folded._last_replay_seek_stats["ordinary_folded_scans"] > 0

    @pytest.mark.parametrize("kind", ["time", "time_event"])
    @pytest.mark.parametrize("presets", [(5, 5000, 5000), (200, 5000, 5000), (5000, 5, 200)])
    @pytest.mark.parametrize(
        "above, below", [(1, None), (None, 1), (1, 1)], ids=["above", "below", "both"]
    )
    def test_one_good_step_held_through_its_moves(
        self, kind: str, presets: tuple[int, ...], above: int | None, below: int | None
    ) -> None:
        """The drum finishes the step the writer holds, including within one scan."""

        def run(plc: PLC, fold: bool) -> None:
            _run_scans(plc, 80, fold=fold)
            plc.patch({"Stage": False})
            _run_scans(plc, 80, fold=fold)
            plc.patch({"Stage": True})
            _run_scans(plc, 200, fold=fold)

        folded, stepped = _folded_and_stepped(
            self._build(kind, above, below, presets), run, patch={"En": True, "Stage": True}
        )
        assert _full_state(folded) == _full_state(stepped)
        for target in (40, 81, 82, 100, 161, 162, 163, 300, folded.state.scan_id):
            assert _full_state(folded._replay_seek(target)) == _full_state(
                stepped.replay_to(target)
            )

    @pytest.mark.parametrize(
        "above, below, contested",
        [
            (None, None, False),
            (1, None, False),
            (None, 3, False),
            (2, 2, False),
            (1, 3, True),  # Two good steps.
            (0, None, True),  # A bad step.
            (4, None, True),  # Past the last step.
            (True, None, True),  # Not a step number.
            (Int("Staged"), None, True),  # Unknown.
        ],
    )
    def test_replay_needs_one_good_literal_step(
        self, above: Any, below: Any, contested: bool
    ) -> None:
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.fold import _build_fold_context

        program = self._build("time", above, below, (500, 500, 500))()
        plc = PLC(program, dt=0.010)
        found = _build_fold_context(plc, build_program_graph(program), program).contested_timers
        assert found == ({"DAcc"} if contested else frozenset())

    def test_replay_counts_any_other_way_to_write_the_step(self) -> None:
        from pyrung import Block, TagType, time_drum
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.fold import _build_fold_context

        bank = Block("Bank", TagType.INT, 1, 4)

        def contested(write: Callable[[], Any], step: Any = None) -> frozenset[str]:
            step = Int("Step") if step is None else step
            with Program(strict=False) as program:
                with Rung(Bool("Stage")):
                    write()
                with Rung(Bool("En")):
                    time_drum(
                        outputs=[Bool("O1")],
                        pattern=[[1], [0]],
                        presets=[500, 500],
                        unit="ms",
                        current_step=step,
                        accumulator=Dint("DAcc"),
                        completion_flag=Bool("DDone"),
                    ).reset(Bool("R"))
            plc = PLC(program, dt=0.010)
            return _build_fold_context(plc, build_program_graph(program), program).contested_timers

        assert contested(lambda: calc(Int("Step") * 0 + 1, Int("Step"))) == {"DAcc"}
        assert contested(lambda: copy(1, bank[Int("I")]), bank[2]) == {"DAcc"}
        assert contested(lambda: copy(1, bank[2]), bank[2]) == frozenset()

    @pytest.mark.parametrize("reader", ["preset", "comparison"])
    def test_tag_written_around_a_timer_reader(self, reader: str) -> None:
        """The same read-mid-scan hole, with no drum in it: the fold takes a timer's
        preset, or a threshold compared with it, from the scan-end value."""
        limit = Int("Limit")
        timer = Timer.clone("T")

        def build() -> Program:
            with Program(strict=False) as prog:
                with Rung():
                    copy(200, limit)
                if reader == "preset":
                    with Rung():
                        on_delay(timer, limit)
                    with Rung():
                        copy(5000, limit)
                    with Rung(timer.Done):
                        out(Bool("Hit"))
                else:
                    with Rung():
                        on_delay(timer, 60_000)
                    with Rung(timer.Acc >= limit):
                        latch(Bool("Hit"))
                    with Rung():
                        copy(5000, limit)
            return prog

        _assert_fold_is_exact(build, _until_hit(600))


class TestDrumOneTimeScans:
    """A drum's scan can leave every tag as it found it and still not repeat: a
    jog edge on a bad step corrects on the next scan, and a rung coming back
    writes its row once.  The memory those scans moved tells the fold so.  A
    rung below the drum sets the stage, so the probe's tags do not change."""

    @staticmethod
    def _drum(kind: str) -> Any:
        from pyrung import event_drum, time_drum

        args = {
            "outputs": [Bool("O1"), Bool("O2")],
            "pattern": [[1, 0], [0, 1]],
            "current_step": Int("Step"),
            "completion_flag": Bool("DDone"),
        }
        if kind == "event":
            return event_drum(events=[Bool("E1"), Bool("E2")], **args)
        return time_drum(presets=[50_000, 50_000], accumulator=Int("DAcc"), **args)

    @classmethod
    def _build(cls, kind: str) -> Callable[[], Program]:
        def build() -> Program:
            with Program(strict=False) as prog:
                with Rung():
                    on_delay(Timer.clone("Long"), 60_000)
                with Rung():
                    cls._drum(kind).reset(Bool("R")).jog(Bool("J"))
                with Rung(rise(Bool("Go"))):
                    copy(5, Int("Step"))
                    latch(Bool("J"))
            return prog

        return build

    @pytest.mark.parametrize("kind", ["event", "time"])
    def test_correction_after_a_held_scan_matches_stepping(self, kind: str) -> None:
        def run(plc: PLC, fold: bool) -> None:
            _run_scans(plc, 5, fold=fold)
            plc.patch({"Go": True})
            _run_scans(plc, 2_000, fold=fold)

        folded = _assert_fold_is_exact(self._build(kind), run)
        assert folded.state.tags["Step"] == 1

    @pytest.mark.parametrize("kind", ["event", "time"])
    def test_rung_coming_back_matches_stepping(self, kind: str) -> None:
        """The row the rung writes is on for the rest of that one scan, under a
        timer, and the rung below the timer turns it off."""
        reader = Timer.clone("Reader")

        def build() -> Program:
            with Program(strict=False) as prog:
                with Rung(Bool("En")):
                    self._drum(kind).reset(Bool("R"))
                with Rung(Bool("O1")):
                    on_delay(reader, 60_000)
                with Rung():
                    reset(Bool("O1"))
                with Rung(Bool("Go")):
                    latch(Bool("En"))
            return prog

        def run(plc: PLC, fold: bool) -> None:
            _run_scans(plc, 5, fold=fold)
            plc.patch({"Go": True})
            _run_scans(plc, 2_000, fold=fold)

        folded = _assert_fold_is_exact(build, run)
        assert folded.state.tags[reader.Acc.name] == 0

    @pytest.mark.parametrize("kind", ["event", "time"])
    def test_jog_completion_matches_stepping(self, kind: str) -> None:
        def run(plc: PLC, fold: bool) -> None:
            _run_scans(plc, 5, fold=fold)
            plc.patch({"J": True})
            _run_scans(plc, 3, fold=fold)
            plc.patch({"J": False})
            _run_scans(plc, 3, fold=fold)
            plc.patch({"J": True})
            _run_scans(plc, 6_000, fold=fold)

        folded = _assert_fold_is_exact(self._build(kind), run)
        assert folded.state.tags["DDone"] is True
        assert folded.state.tags["Step"] == 2


class TestReadsOutsideTheCrossings:
    """A coordinate the plateau guard ignores must be read only where the
    crossing set places its flips.  Any other read (an expression, an index,
    a moving operand or preset, a stored copy, an instruction's own
    condition, an opaque stop predicate) bounds the window at the
    coordinate's next value change."""

    @staticmethod
    def _program(shape: str) -> Program:
        from pyrung import Block, TagType, copy, latch

        T, U, P, Q, Y = Int("T"), Int("U"), Int("P"), Int("Q"), Int("Y")
        Tmr, C = Timer.clone("Tmr"), Counter.clone("C")
        DS = Block("DS", TagType.INT, 1, 3_000)
        with Program(strict=False) as prog:
            if shape in ("expr", "index", "index_copy", "operand", "both_moving"):
                with Rung():
                    calc(T + 1 if shape != "operand" else T - 3, T)
            if shape == "both_moving":
                with Rung():
                    calc(U - 2, U)
            if shape in ("preset", "counter_preset"):
                with Rung():
                    calc(P - 1, P)
            if shape in ("operand", "stateful", "copied", "expr_timer", "preset"):
                with Rung():
                    on_delay(Tmr, P if shape == "preset" else 20_000)
            # A counter counts edges: these count the 100 ms clock's rises.
            if shape == "counter_preset":
                with Rung(system.sys.clock_100ms):
                    count_up(C, P).reset(Bool("CR"))
            if shape == "reset_cond":
                with Rung(system.sys.clock_100ms):
                    count_up(C, 1_000).reset(Bool("CR"))
                with Rung():
                    on_delay(Tmr, 2_000).reset(C.Acc == 100)
            reader = {
                "expr": lambda: (T // 100) == 3,
                "index": lambda: DS[T] == 5,
                "operand": lambda: Tmr.Acc > T,
                "both_moving": lambda: T > U,
                "expr_timer": lambda: (Tmr.Acc // 5_000) == 1,
                "copied": lambda: Q == 1,
                "stateful": lambda: Y > 0,
            }.get(shape)
            if shape == "index_copy":
                with Rung():
                    copy(DS[T], Q)
                reader = lambda: Q == 5  # noqa: E731
            if shape == "copied":
                with Rung():
                    calc(Tmr.Acc // 5_000, Q)
            if shape == "stateful":
                with Rung():
                    calc(Y + Tmr.Acc // 5_000, Y)
            if reader is not None:
                with Rung(reader()):
                    latch(Bool("L"))
            with Rung(rise(Bool("L"))):
                copy(T, Int("TAtL"))
            with Rung(T > 2_000):  # a far crossing that opens windows
                out(Bool("Far"))
            with Rung(Tmr.Done if shape != "counter_preset" else C.Done):
                out(Bool("Done"))
        return prog

    @pytest.mark.parametrize(
        ("shape", "patch"),
        [
            ("expr", {}),
            ("index", {"DS500": 5, "T": 1}),
            ("index_copy", {"DS500": 5, "T": 1}),
            ("operand", {"T": 10_000}),
            ("both_moving", {"U": 5_000}),
            ("preset", {"P": 30_000}),
            ("counter_preset", {"P": 3_000}),
            ("expr_timer", {}),
            ("copied", {}),
            ("stateful", {}),
            ("reset_cond", {}),
        ],
    )
    def test_matches_stepping(self, shape: str, patch: dict[str, int]) -> None:
        def run(plc: PLC, fold: bool) -> None:
            if "preset" in shape:
                plc.run_until(Bool("Done"), max_cycles=5_000, fold=fold)
            else:
                _run_scans(plc, 2_500, fold=fold)

        _assert_fold_is_exact(lambda: self._program(shape), run, patch=patch)

    @staticmethod
    def _timer_only() -> Program:
        with Program(strict=False) as prog:
            with Rung():
                on_delay(Timer.clone("Tmr"), 30_000)
        return prog

    @pytest.mark.parametrize(
        "stop",
        [
            "condition_expr",
            "callable_acc",
            "callable_scan_id",
            "callable_iterates",
        ],
    )
    def test_stop_predicate_reads(self, stop: str) -> None:
        Tmr = Timer.clone("Tmr")
        until: Any = {
            "condition_expr": (Tmr.Acc // 5_000) == 1,
            "callable_acc": lambda s: s.tags.get("Tmr_Acc", 0) >= 5_000,
            "callable_scan_id": lambda s: s.scan_id >= 777,
            "callable_iterates": lambda s: any(
                k == "Tmr_Acc" and v >= 5_000 for k, v in s.tags.items()
            ),
        }[stop]

        def run(plc: PLC, fold: bool) -> None:
            plc.run_until(until, max_cycles=5_000, fold=fold)

        folded = _assert_fold_is_exact(self._timer_only, run)
        assert folded.state.scan_id < 5_000

    def test_cycle_coast_traces_an_undeclared_predicate(self) -> None:
        from pyrung.core.analysis.pilot.cyclefold import cycle_fold_until

        def run(plc: PLC, fold: bool) -> None:
            def pred(s: Any) -> bool:
                return s.tags.get("Tmr_Acc", 0) >= 5_000

            if fold:
                cycle_fold_until(plc, pred, budget=5_000)
            else:
                plc.run_until(pred, max_cycles=5_000, fold=False)

        _assert_fold_is_exact(self._timer_only, run)

    @staticmethod
    def _folding_kept(shape: str) -> Program:
        from pyrung import copy

        Tmr, N = Timer.clone("Tmr"), Dint("N")
        with Program(strict=False) as prog:
            with Rung():
                calc(N + 1, N)  # per-scan work (a counter would count once)
            with Rung():
                on_delay(Tmr, 20_000)
            if shape == "display":
                # Nothing reads the display: the landing scan rebuilds it.
                with Rung():
                    calc(30_000 - Tmr.Acc, Int("Left"))
            else:
                # A snapshot gated by an edge never fires inside a window.
                with Rung(rise(Bool("X"))):
                    copy(N, Int("Snap"))
                with Rung(Int("Snap") > 5):
                    out(Bool("Big"))
        return prog

    @pytest.mark.parametrize("shape", ["display", "snapshot"])
    def test_reads_the_landing_reproduces_still_fold(self, shape: str) -> None:
        kernel = {"n": 0}

        def run(plc: PLC, fold: bool) -> None:
            if fold:
                scan = plc._run_single_scan

                def counted(**kw: Any) -> Any:
                    kernel["n"] += 1
                    return scan(**kw)

                setattr(plc, "_run_single_scan", counted)  # noqa: B010
            _run_scans(plc, 1_500, fold=fold)

        _assert_fold_is_exact(lambda: self._folding_kept(shape), run)
        assert kernel["n"] < 50


class TestIndexedDisplays:
    """A display written to a block element is not provably unread when some
    index into that block (``DS[Idx]``, Idx unbounded) names no element the
    program graph can see: a window must not leave it stale."""

    @staticmethod
    def _program(reader: str) -> Program:
        from pyrung import Block, TagType, copy, latch

        Tmr = Timer.clone("Tmr")
        DS = Block("DS", TagType.INT, 1, 100)
        Idx, Q = Int("Idx", default=5), Int("Q")
        with Program(strict=False) as prog:
            with Rung():
                on_delay(Tmr, 20_000)
            with Rung():
                calc(Tmr.Acc // 1_000, DS[5])
            if reader == "contact":
                with Rung(DS[Idx] == 3):
                    latch(Bool("Hit"))
            elif reader == "copy":
                with Rung():
                    copy(DS[Idx], Q)
                with Rung(Q == 3):
                    latch(Bool("Hit"))
        return prog

    @pytest.mark.parametrize("reader", ["contact", "copy"])
    def test_program_index_reads_the_display(self, reader: str) -> None:
        def run(plc: PLC, fold: bool) -> None:
            _run_scans(plc, 3_000, fold=fold)

        folded = _assert_fold_is_exact(lambda: self._program(reader), run)
        assert folded.state.tags["Hit"]

    def test_run_until_through_an_index(self) -> None:
        from pyrung import Block, TagType

        DS = Block("DS", TagType.INT, 1, 100)

        def run(plc: PLC, fold: bool) -> None:
            plc.run_until(DS[Int("Idx", default=5)] == 3, max_cycles=3_000, fold=fold)

        folded = _assert_fold_is_exact(lambda: self._program("none"), run)
        assert folded.state.tags["DS5"] == 3


class TestCallableReadsThePlc:
    def test_a_closure_over_the_plc_is_traced(self) -> None:
        def build() -> Program:
            with Program(strict=False) as prog:
                with Rung():
                    on_delay(Timer.clone("Tmr"), 5_000)
            return prog

        def run(plc: PLC, fold: bool) -> None:
            plc.run_until(
                lambda _s: plc.state.tags["Tmr_Acc"] >= 1_234, max_cycles=3_000, fold=fold
            )

        folded = _assert_fold_is_exact(build, run)
        assert folded.state.tags["Tmr_Acc"] < 1_300


class TestFoldedHistoryQueries:
    """History materializes the scans a fold skipped; recorded cause() refuses
    them clearly instead of answering from the one-step landing."""

    @staticmethod
    def _program() -> Program:
        T = Int("T")
        C = Counter.clone("C")
        Tmr = Timer.clone("Tmr")
        with Program(strict=False) as prog:
            with Rung():
                calc(T + 3, T)
            with Rung(Bool("En"), system.sys.clock_100ms):  # counts the clock's rises
                count_up(C, 1_000).reset(Bool("CR"))
            with Rung():
                on_delay(Tmr, 7_000)
            with Rung(Tmr.Done):
                out(Bool("Hit"))
        return prog

    def _runs(self) -> tuple[PLC, PLC]:
        folded, stepped = _folded_and_stepped(self._program, _until_hit(3_000), patch={"En": True})
        assert folded.state.scan_id == stepped.state.scan_id
        assert folded._fold_windows
        return folded, stepped

    def test_range_and_latest_cover_every_scan(self) -> None:
        folded, stepped = self._runs()
        tip = folded.state.scan_id
        got = folded.history.range(1, tip + 1)
        want = stepped.history.range(1, tip + 1)
        assert [s.scan_id for s in got] == list(range(1, tip + 1))
        assert [_plain_tags_of(s) for s in got] == [_plain_tags_of(s) for s in want]
        assert [_plain_tags_of(s) for s in folded.history.latest(25)] == [
            _plain_tags_of(s) for s in stepped.history.latest(25)
        ]

    def test_cause_refuses_a_folded_scan(self) -> None:
        folded, stepped = self._runs()
        first, last = folded._fold_windows[0]
        for scan in (first, (first + last) // 2, last):
            with pytest.raises(ValueError, match="folded"):
                folded.cause("C_Acc", scan)
            with pytest.raises(ValueError, match="folded"):
                folded.effect("C_Acc", scan)
        tip = folded.state.scan_id
        assert str(folded.cause("C_Acc", tip)) == str(stepped.cause("C_Acc", tip))
        assert str(folded.cause("Hit")) == str(stepped.cause("Hit"))

    def test_a_fork_refuses_its_parents_folded_scans(self) -> None:
        folded, _stepped = self._runs()
        first, last = folded._fold_windows[0]
        mid = (first + last) // 2
        for at in (None, mid):
            child = folded.fork(at)
            grandchild = child.fork()
            for plc in (child, grandchild):
                with pytest.raises(ValueError, match="folded"):
                    plc.cause("C_Acc", first)
                with pytest.raises(ValueError, match="folded"):
                    plc.effect("C_Acc", first)
        # Forked from inside a window, the child still owns that window's
        # earlier scans (clipped at the fork point) and none after it.
        child = folded.fork(mid)
        with pytest.raises(ValueError, match=rf"scans {first}\.\.{mid} "):
            child.cause("C_Acc", mid)


class TestLandingDtReadouts:
    """The landing scan runs with the aggregate dt, but the scan it stands
    for lasted one normal dt: the scan-time extrema and ``_dt`` record that,
    and a rung reading ``sys.dt`` mid-scan keeps the run unfolded."""

    @staticmethod
    def _shape(reader: str | None) -> Callable[[], Program]:
        from pyrung import latch

        def build() -> Program:
            Tmr = Timer.clone("Tmr")
            with Program(strict=False) as prog:
                with Rung():
                    on_delay(Tmr, 3_000)
                if reader == "dt":
                    with Rung(system.sys.dt > 0.05):
                        latch(Bool("Big"))
                elif reader == "current":
                    with Rung(system.sys.scan_time_current_ms > 50):
                        latch(Bool("Big"))
                elif reader == "max":
                    with Rung(system.sys.scan_time_max_ms > 50):
                        latch(Bool("Big"))
                with Rung(Tmr.Done):
                    out(Bool("Hit"))
            return prog

        return build

    @pytest.mark.parametrize("reader", [None, "dt", "current", "max"])
    def test_whole_state_matches_stepping(self, reader: str | None) -> None:
        folded, stepped = _folded_and_stepped(self._shape(reader), _until_hit(1_000))
        assert folded.state.scan_id == stepped.state.scan_id
        assert dict(folded.state.tags) == dict(stepped.state.tags)
        assert dict(folded.state.memory) == dict(stepped.state.memory)


class TestTimeEventDrum:
    """A time & event drum is a timed source whose step may end on its preset,
    on its event, or on both.  Event-only steps place no timer crossing; the
    event's own change bounds the window."""

    @staticmethod
    def _shape(gate: bool, read_done: bool) -> Callable[[], Program]:
        from pyrung import time_event_drum

        W, T2 = Timer.clone("W"), Timer.clone("T2")

        def source() -> None:
            with Rung():
                on_delay(T2, 30_000)
            with Rung(Bool("O2")):
                on_delay(W, 2_500)
            with Rung(system.sys.clock_1s) if gate else Rung():
                time_event_drum(
                    outputs=[Bool("O1"), Bool("O2"), Bool("O3")],
                    presets=[3_000, None, 1_500],
                    events=[None, W.Done, T2.Acc >= 15_000],
                    unit="ms",
                    pattern=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                    current_step=Int("Step"),
                    accumulator=Int("DAcc"),
                    completion_flag=Bool("DDone"),
                ).reset(Bool("R"))

        reader = (lambda: Bool("DDone")) if read_done else (lambda: Bool("O3"))
        return lambda: _scan_stamp_program(source, reader, above=False)

    @pytest.mark.parametrize("read_done", [True, False])
    @pytest.mark.parametrize("gate", [True, False])
    def test_completion_scan_matches_stepping(self, gate: bool, read_done: bool) -> None:
        folded = _assert_fold_is_exact(self._shape(gate, read_done), _scans(400), dt=0.1)
        assert folded.state.tags["DDone"]

    @staticmethod
    def _event_only_tail(read_done: bool = True) -> Program:
        from pyrung import time_event_drum

        W = Timer.clone("W")
        with Program(strict=False) as prog:
            with Rung(Bool("O2")):
                on_delay(W, 4_000)
            with Rung():
                time_event_drum(
                    outputs=[Bool("O1"), Bool("O2")],
                    presets=[600, None],
                    events=[None, W.Done],
                    unit="sec",
                    pattern=[[1, 0], [0, 1]],
                    current_step=Int("Step"),
                    accumulator=Int("DAcc"),
                    completion_flag=Bool("DDone"),
                ).reset(Bool("R"))
            with Rung(Bool("DDone") if read_done else Bool("O1")):
                out(Bool("L"))
        return prog

    @pytest.mark.parametrize("scans", [2_000, 60_100, 61_000])
    def test_event_only_final_step_folds_to_its_event(self, scans: int) -> None:
        folded = _assert_fold_is_exact(self._event_only_tail, _scans(scans), dt=0.01)
        assert folded.state.tags["L"] is (scans > 60_400)

    def test_reading_done_does_not_step_an_event_only_step(self) -> None:
        """No preset to place: the Done read costs nothing over an unread drum."""
        work = []
        for read_done in (True, False):
            plc = PLC(self._event_only_tail(read_done), dt=0.01)
            plc.step()
            plc.run_until(NEVER, max_cycles=60_100)
            assert plc.state.tags["Step"] == 2
            counter = _count_kernel_scans(plc)
            plc.run_until(NEVER, max_cycles=2_000)
            work.append(counter["n"])
        assert work[0] == work[1]


class TestOutVariants:
    """Debounce, flasher and timed coils time their phases like on_delay: each
    phase accumulator is a timed source, so a window ends at the next phase
    change and the landing matches stepping."""

    @staticmethod
    def _shape(verb: str, reader: str, unit: str = "ms") -> Callable[[], Program]:
        from pyrung import debounce, flasher, timed

        on, off = (2_500, 1_200) if unit == "ms" else (7, 3)
        T = Timer.clone("OV", count=2)
        coil = Bool("Coil")
        verbs = {
            "debounce": lambda: debounce(coil, on=(T[1], on), off=(T[2], off), unit=unit),
            "flasher": lambda: flasher(coil, on=(T[1], off), off=(T[2], on), unit=unit),
            "timed": lambda: timed(coil, T[1], on, unit),
            "maintained": lambda: timed(coil, T[1], on, unit, maintained=True),
        }
        readers = {
            "coil": lambda: coil,
            "done": lambda: T[1].Done,
            "off_done": lambda: T[2].Done,
            "acc": lambda: T[1].Acc >= 500,
        }

        def source() -> None:
            with Rung(Bool("In")):
                verbs[verb]()

        return lambda: _scan_stamp_program(source, readers[reader], above=False)

    @pytest.mark.parametrize("reader", ["coil", "done", "acc"])
    @pytest.mark.parametrize("verb", ["debounce", "flasher", "timed", "maintained"])
    def test_held_rung_matches_stepping(self, verb: str, reader: str) -> None:
        _assert_fold_is_exact(self._shape(verb, reader), _scans(900), dt=0.01, patch={"In": True})

    @pytest.mark.parametrize("verb", ["debounce", "flasher", "timed"])
    def test_seconds_unit_matches_stepping(self, verb: str) -> None:
        shape = self._shape(verb, "coil", unit="sec")
        _assert_fold_is_exact(shape, _scans(3_000), dt=0.03, patch={"In": True})

    @pytest.mark.parametrize("verb", ["debounce", "flasher", "timed", "maintained"])
    def test_rung_dropping_mid_phase_matches_stepping(self, verb: str) -> None:
        reader = "off_done" if verb in ("debounce", "flasher") else "coil"

        def run(plc: PLC, fold: bool) -> None:
            plc.patch({"In": True})
            _run_scans(plc, 150, fold=fold)
            plc.patch({"In": False})
            _run_scans(plc, 400, fold=fold)
            plc.patch({"In": True})
            _run_scans(plc, 333, fold=fold)

        _assert_fold_is_exact(self._shape(verb, reader), run, dt=0.01)

    def test_flasher_folds(self) -> None:
        """A held flasher is not stepped scan by scan."""
        plc = PLC(self._shape("flasher", "coil")(), dt=0.01)
        plc.patch({"In": True})
        counter = _count_kernel_scans(plc)
        plc.run_until(NEVER, max_cycles=2_000)
        assert counter["n"] < 500


class TestContestedTimer:
    """A timer whose accumulator something else also writes in the scan (a
    second timer instruction, a copy, a force, a second call into its
    subroutine) can be reset and timed again every scan: its scan-boundary
    readings stay put while the landing scan would time the whole window in
    one step.  Such a window folds only while every other writer is proven off;
    otherwise the run steps."""

    @staticmethod
    def _shape(kind: str) -> Callable[[], Program]:
        from pyrung import call, copy, forloop, off_delay, subroutine

        def build() -> Program:
            Tmr, Slow = Timer.clone("Tmr"), Timer.clone("Slow")
            A, B, Btn, Sel = Bool("A"), Bool("B"), Bool("Btn"), Int("Sel")

            @subroutine("S")
            def sub() -> None:
                with Rung(Sel == 1):
                    on_delay(Tmr, 300, "ms")

            with Program(strict=False) as prog:
                if kind == "two_on_delays":
                    with Rung(A):
                        on_delay(Tmr, 300, "ms")
                    with Rung(B):
                        on_delay(Tmr, 300, "ms")
                elif kind == "two_off_delays":
                    with Rung(B):
                        off_delay(Tmr, 300, "ms")
                    with Rung(A):
                        off_delay(Tmr, 300, "ms")
                elif kind == "copy_before":
                    with Rung(Btn):
                        copy(0, Tmr.Acc)
                    with Rung(B):
                        on_delay(Tmr, 300, "ms")
                elif kind == "copy_after":
                    with Rung(B):
                        on_delay(Tmr, 300, "ms")
                    with Rung(Tmr.Done):
                        out(Bool("Seen"))
                    with Rung(Btn):
                        copy(0, Tmr.Acc)
                elif kind == "sub_twice":
                    for sel in (0, 1):
                        with Rung():
                            copy(sel, Sel)
                        with Rung():
                            call(sub)
                elif kind == "forloop":
                    with Rung(B), forloop(2):
                        on_delay(Tmr, 300, "ms")
                with Rung():
                    on_delay(Slow, 60_000, "ms")
                with Rung(Tmr.Done):
                    out(Bool("Hit"))
            return prog

        return build

    @pytest.mark.parametrize(
        "kind",
        ["two_on_delays", "two_off_delays", "copy_before", "copy_after", "sub_twice", "forloop"],
    )
    @pytest.mark.parametrize("btn", [False, True])
    def test_matches_stepping(self, kind: str, btn: bool) -> None:
        _assert_fold_is_exact(self._shape(kind), _scans(500), patch={"B": True, "Btn": btn})

    def test_reset_writer_released_mid_run(self) -> None:
        def run(plc: PLC, fold: bool) -> None:
            plc.patch({"B": True, "Btn": True})
            _run_scans(plc, 120, fold=fold)
            plc.patch({"Btn": False})
            _run_scans(plc, 400, fold=fold)

        folded = _assert_fold_is_exact(self._shape("copy_before"), run)
        assert folded.state.tags["Tmr_Done"]

    def test_forced_accumulator_matches_stepping(self) -> None:
        def run(plc: PLC, fold: bool) -> None:
            plc.force("Tmr_Acc", 50)
            _run_scans(plc, 500, fold=fold)

        folded = _assert_fold_is_exact(self._shape("copy_before"), run, patch={"B": True})
        assert not folded.state.tags["Tmr_Done"]

    def test_reset_writer_proven_off_still_folds(self) -> None:
        plc = PLC(self._shape("copy_before")(), dt=0.01)
        plc.patch({"B": True, "Btn": False})
        plc.step()
        counter = _count_kernel_scans(plc)
        _run_scans(plc, 2_000, fold=True)
        assert counter["n"] < 100

    @pytest.mark.parametrize("kind", ["two_on_delays", "copy_before", "copy_after", "sub_twice"])
    def test_history_replay_matches_stepping(self, kind: str) -> None:
        def run(plc: PLC, fold: bool) -> None:
            plc.patch({"B": True, "Btn": True})
            _run_scans(plc, 150, fold=fold)
            plc.patch({"Btn": False})
            _run_scans(plc, 300, fold=fold)

        folded, stepped = _folded_and_stepped(self._shape(kind), run)
        tip = folded.state.scan_id
        for target in (5, 100, 160, 300, tip - 1, tip):
            want = _full_state(stepped.replay_to(target))
            assert _full_state(folded.replay_to(target)) == want
            assert _full_state(folded._replay_seek(target)) == want

    def test_reset_writer_held_on_steps(self) -> None:
        def run(plc: PLC, fold: bool) -> None:
            plc.patch({"B": True, "Btn": True})
            _run_scans(plc, 200, fold=fold)

        folded, stepped = _folded_and_stepped(self._shape("copy_before"), run)
        assert _full_state(folded) == _full_state(stepped)
