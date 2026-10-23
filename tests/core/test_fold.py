"""Tests for core/fold.py — fold-aware run_until() and run_for()."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from pyrung import Bool, Counter, Int, Program, Rung, Timer, calc, count_up, on_delay, out
from pyrung.core import fall, rise, system
from pyrung.core.runner import PLC

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
    """Count-up counter incrementing every scan while Enable is True."""
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
    """run_until with fold=True skips counter plateaus."""

    def test_fold_run_until_reports_exact_visible_patches(self) -> None:
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
            max_cycles=5000,
            fold_ctx=fold_ctx,
            advances=advances,
        )

        assert advances == [("Ctr_Acc", 98)]

    def test_counter_fold_reaches_done(self) -> None:
        prog, ctr = _counter_program(100)
        from pyrung.core.runner import PLC

        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})

        result = plc.run_until(ctr.Done, max_cycles=5000)

        assert result.tags["Done"] is True

    def test_counter_fold_bit_equal_to_unfold(self) -> None:
        prog, ctr = _counter_program(50)
        from pyrung.core.runner import PLC

        plc_fold = PLC(prog, dt=0.010)
        plc_fold.patch({"Enable": True})
        plc_fold.run_until(ctr.Done, max_cycles=5000, fold=True)

        prog2, ctr2 = _counter_program(50)
        plc_nofold = PLC(prog2, dt=0.010)
        plc_nofold.patch({"Enable": True})
        plc_nofold.run_until(ctr2.Done, max_cycles=5000, fold=False)

        assert plc_fold.state.tags["Done"] == plc_nofold.state.tags["Done"]
        assert plc_fold.state.tags["Ctr_Acc"] == plc_nofold.state.tags["Ctr_Acc"]
        assert plc_fold.state.tags["Ctr_Done"] == plc_nofold.state.tags["Ctr_Done"]


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
# Inert resolved-on-read signal fold tests (DESIGN — currently xfail).
#
# A resolved-on-read signal (sys.clock_*, sys.scan_clock_toggle,
# sys.scan_counter) is invisible to the plateau guard, so today reading one
# either *caps* every fold at the signal's edge (clocks) or *disables* the
# fold outright (scan-derived).  But a rung gated by such a signal whose body
# reads only window-frozen state recomputes the *same result* at every edge —
# it is inert, and the fold should be able to skip past those edges instead of
# landing on each one.
#
# The soundness oracle stays the existing plateau guard: land once per window
# to flush a pending recompute, and only generalize the observed inertness
# across the window when the gated rung reads frozen state only.  See the
# investigation notes for the full design.  These tests assert the *target*
# behavior and xfail until it lands; the correctness halves already hold today.
# ---------------------------------------------------------------------------


def _count_kernel_scans(plc: PLC) -> dict[str, int]:
    """Wrap ``_run_single_scan`` to count kernel executions.

    Every probe scan and every ``_do_fold`` step routes through
    ``_run_single_scan`` — the single choke point — so the count is the true
    kernel work done, independent of how far ``scan_id`` (equivalent elapsed
    time) advances. Call after ``step()`` so only the run-loop work is counted.
    """
    counter = {"n": 0}
    original = plc._run_single_scan

    def wrapped(**scan_options):  # type: ignore[no-untyped-def]
        counter["n"] += 1
        return original(**scan_options)

    plc._run_single_scan = wrapped  # ty: ignore[invalid-assignment]
    return counter


class TestInertSignalFold:
    """Fold should skip edges of resolved-on-read signals whose gated rung is
    inert (recomputes a frozen result), not land on / disable for every one."""

    # ── Phase 1: soft clocks ────────────────────────────────────────────

    @staticmethod
    def _clock_heartbeat_program(preset_ms: int = 100_000) -> Program:
        """Never-completing timer plateau + a 1 s heartbeat that recomputes a
        value from frozen external inputs only (the AlarmExtent pattern).

        The timer's Done is read so the plateau has a (far) crossing for the
        fold to target once the heartbeat clock is confirmed inert."""
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

    def test_inert_clock_heartbeat_folds_past_edges(self) -> None:
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

        # Correctness (holds today): the heartbeat settles Extent to A + B.
        assert plc_fold.state.tags["Extent"] == 7
        assert plc_fold.state.tags["Extent"] == plc_nofold.state.tags["Extent"]
        # Efficiency (fails today): the clock cap currently lands on every
        # 0.5 s half-period edge (~60 over 30 s, ~2 kernel scans each). An inert
        # heartbeat should collapse the whole span into a handful of passes.
        assert counter["n"] <= 12

    def test_inert_clock_heartbeat_flushes_after_input_change(self) -> None:
        # Soundness guard (must hold today AND after): skipping inert edges may
        # not freeze a stale value.  When the frozen inputs change, the next
        # window must still land once to flush the pending recompute.
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
        """clock_1s read by BOTH an inert recompute and a self-referential
        tick.  The shared clock must stay 'hard' — the live tick still fires
        on every edge — so the soft classification is strictly per-clock."""
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
                calc(A + B, Extent)  # inert (frozen inputs)
            with Rung(rise(system.sys.clock_1s)):
                calc(Ticks + 1, Ticks)  # live (self-referential)
        return prog

    def test_clock_shared_by_live_rung_stays_bounded(self) -> None:
        # Regression guard (must hold today AND after): an over-eager soft
        # classification would skip edges the live tick needs.
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

    # ── Phase 2: inert scan_clock_toggle no longer disables fold ─────────

    @staticmethod
    def _toggle_heartbeat_program(preset_ms: int = 100_000) -> Program:
        """Timer plateau + a scan_clock_toggle heartbeat over frozen inputs.
        Reading scan_clock_toggle disables the fold today; an inert recompute
        should not."""
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
            with Rung(rise(system.sys.scan_clock_toggle)):
                calc(A + B, Extent)
        return prog

    @pytest.mark.xfail(reason="inert scan-toggle fold not yet implemented", strict=True)
    def test_inert_scan_toggle_does_not_disable_fold(self) -> None:
        prog = self._toggle_heartbeat_program()
        plc_fold = PLC(prog, dt=0.010)
        plc_fold.patch({"Enable": True, "A": 3, "B": 4})
        plc_fold.step()
        counter = _count_kernel_scans(plc_fold)
        plc_fold.run_for(5.0, fold=True)  # 500 scans

        # Correctness (holds today): scan-by-scan still settles Extent.
        assert plc_fold.state.tags["Extent"] == 7
        # Efficiency (fails today): reading scan_clock_toggle disables the fold
        # entirely → ~500 kernel scans. Inert recompute should collapse it.
        assert counter["n"] <= 12

    # ── Phase 3: scan_counter as a virtual monotonic crossing ────────────

    @staticmethod
    def _scan_counter_tick_program(preset_ms: int = 100_000, threshold: int = 250) -> Program:
        """Timer plateau + a tick gated on a single scan_counter crossing.
        scan_counter is monotonic in scan_id, so ``== threshold`` is a
        closed-form crossing the fold should land on directly."""
        Enable = Bool("Enable", external=True)
        Tmr = Timer.clone("Tmr")
        Ticks = Int("Ticks")
        with Program() as prog:
            with Rung(Enable):
                on_delay(Tmr, preset_ms, "ms")
            with Rung(system.sys.scan_counter == threshold):
                calc(Ticks + 1, Ticks)
        return prog

    @pytest.mark.xfail(reason="scan_counter virtual-crossing fold not yet implemented", strict=True)
    def test_scan_counter_crossing_folds_to_threshold(self) -> None:
        prog = self._scan_counter_tick_program(threshold=250)
        plc_fold = PLC(prog, dt=0.010)
        plc_fold.patch({"Enable": True})
        plc_fold.step()
        counter = _count_kernel_scans(plc_fold)
        plc_fold.run_for(5.0, fold=True)  # 500 scans; crosses scan_counter == 250

        prog2 = self._scan_counter_tick_program(threshold=250)
        plc_nofold = PLC(prog2, dt=0.010)
        plc_nofold.patch({"Enable": True})
        plc_nofold.step()
        plc_nofold.run_for(5.0, fold=False)

        # Correctness (holds today): the crossing fires exactly once.
        assert plc_fold.state.tags["Ticks"] == plc_nofold.state.tags["Ticks"] == 1
        # Efficiency (fails today): reading scan_counter disables the fold →
        # ~500 kernel scans. The crossing arithmetic should land near scan 250.
        assert counter["n"] <= 12


class TestComparisonsSaturated:
    """Step 1 primitive: `_comparisons_saturated` decides when a monotone
    source's contribution to a set of comparisons is frozen for the rest of the
    window (every read comparison has already made its final transition).

    This is the value-aware test the clock partition is currently blind to: an
    accumulator that has crossed every threshold a rung reads on it stops being
    'live' for that rung even though its raw value keeps changing."""

    def _saturated(self, progress, kind, cmps):
        from pyrung.core.fold import _comparisons_saturated

        return _comparisons_saturated(progress, kind, cmps)

    def test_up_gt_already_crossed_is_saturated(self) -> None:
        # Tmr.Acc at 300, rung reads `Acc > 100`: permanently true, won't re-flip.
        assert self._saturated(300.0, "up", [("gt", 100.0)]) is True

    def test_up_gt_not_yet_crossed_is_live(self) -> None:
        # Acc at 50 < 100: the comparison still flips at 100 — not frozen.
        assert self._saturated(50.0, "up", [("gt", 100.0)]) is False

    def test_up_gt_exactly_at_threshold_is_live(self) -> None:
        # At exactly 100, `> 100` is still false and flips true at 101.
        assert self._saturated(100.0, "up", [("gt", 100.0)]) is False

    def test_up_ge_at_threshold_is_saturated(self) -> None:
        assert self._saturated(100.0, "up", [("ge", 100.0)]) is True

    def test_up_lt_past_threshold_is_saturated(self) -> None:
        # `Acc < 100` with Acc at 300: permanently false going up.
        assert self._saturated(300.0, "up", [("lt", 100.0)]) is True

    def test_up_lt_below_threshold_is_live(self) -> None:
        # `Acc < 100` with Acc at 50: still true, flips false at 100.
        assert self._saturated(50.0, "up", [("lt", 100.0)]) is False

    def test_eq_is_never_saturated(self) -> None:
        # eq has a fall the progress arithmetic does not model — stay conservative.
        assert self._saturated(300.0, "up", [("eq", 100.0)]) is False

    def test_ne_is_never_saturated(self) -> None:
        assert self._saturated(300.0, "up", [("ne", 100.0)]) is False

    def test_all_must_be_saturated(self) -> None:
        # One live comparison taints the whole set.
        assert self._saturated(300.0, "up", [("gt", 100.0), ("lt", 1000.0)]) is False
        assert self._saturated(1500.0, "up", [("gt", 100.0), ("gt", 1000.0)]) is True

    def test_empty_is_vacuously_saturated(self) -> None:
        assert self._saturated(42.0, "up", []) is True

    def test_down_counter_below_threshold_is_saturated(self) -> None:
        # Count-down Acc 100→0; progress = -Acc (monotone up).  `Acc < 10`
        # becomes permanently true once Acc drops below 10.
        assert self._saturated(-5.0, "down", [("lt", 10.0)]) is True

    def test_down_counter_above_threshold_is_live(self) -> None:
        assert self._saturated(-50.0, "down", [("lt", 10.0)]) is False


class TestClockSoftBySaturation:
    """Step 2: a heartbeat clock gating a rung that reads an accumulator only
    through a comparison is statically *hard* (it reads an accumulator), but is
    rescuable — once that comparison saturates, the gated logic recomputes an
    identical result at every edge, so the clock can be promoted soft at runtime.

    `_build_fold_context` records such clocks in `ctx.sat_clocks`; the runtime
    classifier `_runtime_soft_clocks` promotes them only while every required
    comparison is saturated."""

    @staticmethod
    def _saturated_heartbeat_program(preset_ms: int = 1_000_000) -> tuple[Program, Timer]:
        """A 1 s heartbeat gating on `Tmr.Acc > 100`, with the timer ramping to
        a far preset so the comparison saturates long before completion."""
        Enable = Bool("Enable", external=True)
        Tmr = Timer.clone("Tmr")
        Beat = Bool("Beat")
        with Program() as prog:
            with Rung(Enable):
                on_delay(Tmr, preset_ms, "ms")
            with Rung(rise(system.sys.clock_1s), Tmr.Acc > 100):
                out(Beat)
        return prog, Tmr

    def test_saturatable_clock_recorded_and_not_static_soft(self) -> None:
        prog, _ = self._saturated_heartbeat_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        ctx = plc._ensure_fold_context()

        clock = system.sys.clock_1s.name
        # Statically hard (gated rung reads Tmr_Acc) — not in the plain soft set.
        assert clock not in {name for name, _hp in ctx.soft_clocks}
        # But recorded as rescuable, with its accumulator requirement attached.
        sat = {name: accs for name, _hp, accs in ctx.sat_clocks}
        assert clock in sat
        assert "Tmr_Acc" in sat[clock]

    def test_runtime_promotion_flips_at_threshold(self) -> None:
        from pyrung.core.fold import _runtime_soft_clocks

        prog, tmr = self._saturated_heartbeat_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        ctx = plc._ensure_fold_context()
        clock = system.sys.clock_1s.name

        # Before Acc crosses 100, the comparison can still flip → not promoted.
        plc.run_until(tmr.Acc >= 50, max_cycles=500, fold=False)
        assert clock not in _runtime_soft_clocks(ctx, plc.state)

        # Past 100, `Acc > 100` is permanently true → promoted soft.
        plc.run_until(tmr.Acc >= 150, max_cycles=500, fold=False)
        assert clock in _runtime_soft_clocks(ctx, plc.state)

    @staticmethod
    def _eq_heartbeat_program(preset_ms: int = 1_000_000) -> tuple[Program, Timer]:
        Enable = Bool("Enable", external=True)
        Tmr = Timer.clone("Tmr")
        Beat = Bool("Beat")
        with Program() as prog:
            with Rung(Enable):
                on_delay(Tmr, preset_ms, "ms")
            with Rung(rise(system.sys.clock_1s), Tmr.Acc == 100):
                out(Beat)
        return prog, Tmr

    def test_eq_comparison_never_promoted(self) -> None:
        from pyrung.core.fold import _runtime_soft_clocks

        prog, tmr = self._eq_heartbeat_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        ctx = plc._ensure_fold_context()
        clock = system.sys.clock_1s.name

        # Even far past the threshold, an `== 100` gate is never frozen by the
        # conservative saturation test — the clock stays bound.
        plc.run_until(tmr.Acc >= 300, max_cycles=500, fold=False)
        assert clock not in _runtime_soft_clocks(ctx, plc.state)


class TestClockSaturationFold:
    """Step 3: the fold loop honors a saturation-rescuable clock — bounding on
    it like a hard clock until its accumulator comparison saturates AND an edge
    is observed inert, then skipping the rest of the window.

    The oracle is bit-equality with scan-by-scan: the value-aware promotion may
    only ever *speed up* a run that already lands the right values."""

    @staticmethod
    def _stable_saturated_program(preset_ms: int = 1_000_000) -> Program:
        """A far-off timer plateau plus a 1 s heartbeat gated on a *saturating*
        accumulator comparison (`Tmr.Acc > 100`).  Once the timer passes 100 ms
        the gate is permanently true and the recompute (`A + B`, frozen inputs)
        is constant — the heartbeat is inert and its clock is promotable."""
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

    def test_saturated_heartbeat_folds_past_edges(self) -> None:
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

        # Correctness (fails now: the unbounded sat-clock skips the heartbeat).
        # Extent is the saturation oracle — bit-equal to scan-by-scan.  Tmr_Acc
        # folds to a *time* boundary (run_for), so it drifts up to one dt from
        # the scan-by-scan accumulation, same as test_unread_timer_folds_*.
        assert plc_fold.state.tags["Extent"] == 7
        assert plc_fold.state.tags["Extent"] == plc_nofold.state.tags["Extent"]
        assert abs(plc_fold.state.tags["Tmr_Acc"] - plc_nofold.state.tags["Tmr_Acc"]) <= 10
        # Efficiency: once saturated + observed inert, the ~60 half-edges over
        # 30 s collapse into a handful of real passes.
        assert counter["n"] <= 15

    def test_saturated_heartbeat_flushes_after_input_change(self) -> None:
        # Soundness: skipping inert edges must not freeze a stale recompute.
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
        so before saturation each edge genuinely differs.  The clock must stay
        bound until the comparison settles — no edge may be skipped early."""
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

    def test_unsaturated_sat_clock_stays_bounded(self) -> None:
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

    - the edge-detection ``_prev`` for a resolved-on-read clock surviving a big
      fold step (a clock is a pure function of the timestamp, so the next scan
      must compare against its value one *normal* dt before the landing);
    - the inert classification needing a *full period* (both a rise and a fall)
      before skipping, so a rise()-gated pulse — inert at falls, live at rises —
      is never wrongly skipped;
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
        # the big fold step into Done must leave a correct clock _prev.
        folded, stepped = self._both("timer", 17_261, 1, "clock_500ms", 0.01)
        assert folded == stepped

    def test_counter_pulse_inert_at_fall(self) -> None:
        # clock toggles every half-period; the rung pulses only on the rise, so
        # the intervening fall is inert — it must not mark the clock skippable.
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
# Frozen-rung write exclusion
# ---------------------------------------------------------------------------


class TestFrozenRungWrites:
    """Rungs whose entire input surface is unreachable from base-varying tags
    produce identical output across a plateau.  Their writes are excluded from
    the plateau visibility check so clock-gated recomputations don't break the
    fold window."""

    def test_clock_gated_rung_with_stable_inputs_is_frozen(self) -> None:
        """A rung gated by rise(clock) that reads only external inputs (never
        varying during a fold) has its writes excluded from the plateau guard."""
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.fold import _build_fold_context

        Sensor = Bool("Sensor", external=True)
        Indicator = Bool("Indicator")
        Enable = Bool("Enable", external=True)
        Tmr = Timer.clone("Tmr")

        with Program() as prog:
            with Rung(Enable):
                on_delay(Tmr, 1000, "ms")
            with Rung(rise(system.sys.clock_1s), Sensor):
                out(Indicator)

        plc = PLC(prog, dt=0.010)
        pdg = build_program_graph(prog)
        ctx = _build_fold_context(plc, pdg, prog)

        assert "Indicator" in ctx.frozen_writes

    def test_rung_reading_accumulator_is_not_frozen(self) -> None:
        """A rung that reads an accumulator (varying tag) must not be frozen."""
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.fold import _build_fold_context

        Enable = Bool("Enable", external=True)
        Tmr = Timer.clone("Tmr")
        Done = Bool("Done")

        with Program() as prog:
            with Rung(Enable):
                on_delay(Tmr, 1000, "ms")
            with Rung(Tmr.Done):
                out(Done)

        plc = PLC(prog, dt=0.010)
        pdg = build_program_graph(prog)
        ctx = _build_fold_context(plc, pdg, prog)

        assert "Done" not in ctx.frozen_writes

    def test_transitively_non_frozen_via_varying_writer(self) -> None:
        """If a tag is written by both a frozen rung and a non-frozen rung,
        it must not appear in frozen_writes."""
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.fold import _build_fold_context

        Enable = Bool("Enable", external=True)
        Tmr = Timer.clone("Tmr")
        Flag = Bool("Flag")
        Sensor = Bool("Sensor", external=True)

        with Program() as prog:
            with Rung(Enable):
                on_delay(Tmr, 1000, "ms")
            with Rung(Sensor):
                out(Flag)
            with Rung(Tmr.Done):
                out(Flag)

        plc = PLC(prog, dt=0.010)
        pdg = build_program_graph(prog)
        ctx = _build_fold_context(plc, pdg, prog)

        assert "Flag" not in ctx.frozen_writes

    def test_downstream_of_frozen_is_also_frozen(self) -> None:
        """A rung reading only frozen outputs and external inputs is itself
        frozen — transitivity through the write→read chain."""
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.fold import _build_fold_context

        Sensor = Bool("Sensor", external=True)
        Mid = Bool("Mid")
        Final = Bool("Final")
        Enable = Bool("Enable", external=True)
        Tmr = Timer.clone("Tmr")

        with Program() as prog:
            with Rung(Enable):
                on_delay(Tmr, 1000, "ms")
            with Rung(Sensor):
                out(Mid)
            with Rung(Mid):
                out(Final)

        plc = PLC(prog, dt=0.010)
        pdg = build_program_graph(prog)
        ctx = _build_fold_context(plc, pdg, prog)

        assert "Mid" in ctx.frozen_writes
        assert "Final" in ctx.frozen_writes

    def test_frozen_writes_excluded_from_plateau_guard(self) -> None:
        """End-to-end: a clock-gated frozen rung doesn't break the fold.

        The clock gates two rungs: one reads the accumulator (non-frozen,
        makes the clock hard) and one reads only external inputs (frozen).
        Without frozen-write exclusion, the non-frozen rung's reads would
        make BOTH rungs' writes break the plateau.  With it, the frozen
        rung's writes are excluded so the plateau survives for the non-frozen
        rung's identical writes (that rung is also stable in this test, but
        the clock being hard is what we're testing)."""
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.fold import _build_fold_context

        Sensor = Bool("Sensor", external=True)
        Indicator = Bool("Indicator")
        AccView = Int("AccView")
        Enable = Bool("Enable", external=True)
        Tmr = Timer.clone("Tmr")
        Done = Bool("Done")

        with Program() as prog:
            with Rung(Enable):
                on_delay(Tmr, 5000, "ms")
            with Rung(Tmr.Done):
                out(Done)
            # Clock-gated rung reading only externals → frozen
            with Rung(rise(system.sys.clock_1s), Sensor):
                out(Indicator)
            # Clock-gated rung reading the accumulator → NOT frozen
            # (makes clock_1s a hard clock)
            with Rung(rise(system.sys.clock_1s)):
                calc(Tmr.Acc, AccView)

        plc = PLC(prog, dt=0.010)
        pdg = build_program_graph(prog)
        ctx = _build_fold_context(plc, pdg, prog)

        assert "Indicator" in ctx.frozen_writes
        assert "AccView" not in ctx.frozen_writes

        plc.patch({"Enable": True, "Sensor": True})
        plc.step()
        counter = _count_kernel_scans(plc)
        plc.run_until(Tmr.Done, max_cycles=20_000, fold=True)

        assert plc.state.tags["Done"] is True
        assert plc.state.tags["Tmr_Acc"] == 5000
        # Hard clock still bounds the fold at each 0.5 s half-period edge,
        # but fewer total scans than scanning every cycle.
        assert counter["n"] < 500

    def test_target_names_not_frozen(self) -> None:
        """Tags listed as targets are never excluded, even if structurally
        frozen, so the predicate always sees their true value."""
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.fold import _build_fold_context

        Sensor = Bool("Sensor", external=True)
        Result = Bool("Result")
        Enable = Bool("Enable", external=True)
        Tmr = Timer.clone("Tmr")

        with Program() as prog:
            with Rung(Enable):
                on_delay(Tmr, 1000, "ms")
            with Rung(Sensor):
                out(Result)

        plc = PLC(prog, dt=0.010)
        pdg = build_program_graph(prog)
        ctx = _build_fold_context(plc, pdg, prog, target_names=frozenset({"Result"}))

        assert "Result" not in ctx.frozen_writes


# ---------------------------------------------------------------------------
# Frozen-rung soundness: self-feeding, scan-varying, and back-edge reads
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


class TestFrozenRungSoundness:
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
    def test_scan_varying_write_is_not_frozen(self, shape: Callable[[], Any], tag: str) -> None:
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.fold import _build_fold_context

        prog, _hit, _tmr = _gated_hit_program(shape)
        plc = PLC(prog, dt=0.010)
        ctx = _build_fold_context(plc, build_program_graph(prog), prog)

        assert tag not in ctx.frozen_writes

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
    """``~first_scan`` turns a counter on from scan 1; a timer gives the fold
    a crossing to aim for."""
    Z = Bool("Z")
    Hit = Bool("Hit")
    Tmr = Timer.clone("Tmr")
    Ctr = Counter.clone("Ctr")
    with Program(strict=False) as prog:
        with Rung():
            on_delay(Tmr, 1000)
        with Rung(~system.sys.first_scan):
            out(Z)
        with Rung(Z):
            count_up(Ctr, 10_000).reset(Hit)
        with Rung(Tmr.Done):
            out(Hit)
    return prog, Hit


class TestFirstScanWindowConstant:
    def test_first_scan_init_and_its_readers_are_frozen(self) -> None:
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.fold import _build_fold_context, _window_constant_names

        prog, _hit = _first_scan_init_program()
        plc = PLC(prog, dt=0.010)
        ctx = _build_fold_context(plc, build_program_graph(prog), prog)

        assert system.sys.first_scan.name in _window_constant_names()
        assert ctx.reads_first_scan
        assert {"Init", "Blink"} <= ctx.frozen_writes

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

    def test_frozen_first_scan_reader_skips_clock_edges(self) -> None:
        """Blink toggles on clock_1s edges.  Frozen, it no longer breaks a
        plateau on each edge, so the run takes fewer kernel scans than with
        Blink in the guard."""
        from dataclasses import replace

        def kernel_scans(freeze: bool) -> int:
            prog, hit = _first_scan_init_program()
            plc = PLC(prog, dt=0.010)
            plc.step()
            ctx = plc._ensure_fold_context(frozenset({hit.name}))
            if not freeze:
                cache = plc._fold_context_cache
                assert cache is not None
                for key, value in list(cache.items()):
                    cache[key] = replace(value, frozen_writes=frozenset())
            assert "Blink" in ctx.frozen_writes
            counter = _count_kernel_scans(plc)
            plc.run_until(hit, max_cycles=10_000)
            return counter["n"]

        assert kernel_scans(True) < kernel_scans(False)

    def test_probe_at_scan_zero_does_not_certify_a_fold(self) -> None:
        """Scan 0 reads first_scan True; scans 1.. read False.  With every tag
        already present at scan 0, the probe sees an unchanged state (Z stays
        False), but the window would turn Z on and count every scan."""
        from pyrung.core.state import SystemState

        prog, _hit = _not_first_scan_counter_program()
        seed = PLC(prog, dt=0.010)
        seed.step()
        tags = dict(seed.state.tags)
        tags.update({"Z": False, "Ctr_Acc": 0, "Ctr_CU": False, "Tmr_Acc": 0})

        def run(fold: bool) -> PLC:
            prog, hit = _not_first_scan_counter_program()
            plc = PLC(prog, dt=0.010, initial_state=SystemState().with_tags(tags))
            plc.run_until(hit, max_cycles=10_000, fold=fold)
            return plc

        folded, stepped = run(True), run(False)
        assert stepped.state.tags["Ctr_Acc"] == 99
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
    """A counter that counts every scan while the RTC second is below 30.

    Its accumulator is a fold coordinate, so a window whose scans read a
    different second than the probe did would patch the wrong per-scan delta.
    """
    Ctr = Counter.clone("Ctr")
    Tmr = Timer.clone("Tmr")
    with Program(strict=False) as prog:
        with Rung(system.rtc.second < 30):
            count_up(Ctr, 1_000_000).reset(Bool("CtrReset"))
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
    plc.run_until(lambda _s: False, max_cycles=n, fold=fold)


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
        assert folded.state.memory.get("_prev:sys.clock_1s") == stepped.state.memory.get(
            "_prev:sys.clock_1s"
        )


class TestSoftClockInertCount:
    """A soft clock is confirmed inert only by toggles that scans executed.

    A probe that reads just before a clock edge commits a state past it; that
    edge has not run.  Counting it once let a ``rise(clock_1s)`` rung, gated by a
    just-saturated timer comparison, be skipped for the rest of the ramp.
    """

    def test_unexecuted_edge_is_not_counted_inert(self) -> None:
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


def _without_time_mirrors(plc: PLC) -> None:
    from dataclasses import replace

    cache = plc._fold_context_cache
    assert cache is not None
    for key, value in list(cache.items()):
        cache[key] = replace(value, time_mirrors=None)


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
        def kernel_scans(mirrors: bool) -> int:
            prog, hit = _rtc_mirror_program()
            plc = _rtc_mirror_plc(prog)
            plc._ensure_fold_context(frozenset({hit.name}))
            if not mirrors:
                _without_time_mirrors(plc)
            counter = _count_kernel_scans(plc)
            plc.run_until(hit, max_cycles=10_000)
            return counter["n"]

        assert kernel_scans(True) < 10 < kernel_scans(False)

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


def _continued_branch_program(cont: bool) -> Program:
    """A branch whose condition reads ``X == 0``.  Continued, it evaluates on the
    previous rung's snapshot (where X is still 0), so it is live although X is 1
    at its own position."""
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
        with Rung():
            copy(1, X)
        with Rung().continued() if cont else Rung():
            with branch(X == 0):
                calc(Sec // 30, Half)
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

    def test_continued_branch_is_not_judged_at_its_own_position(self) -> None:
        from pyrung.core.fold import _time_mirrors_off

        plc = _rtc_plc(_continued_branch_program(True), dt=1.0, start=self._START)
        for _ in range(30):
            plc.step()
        tm = plc._ensure_fold_context(frozenset({"Hit"})).time_mirrors
        assert tm is not None
        assert plc.state.tags["Half"] == 1  # the branch is live
        assert not _time_mirrors_off(tm, (plc.state.tags,))

    @pytest.mark.parametrize("cont", [True, False], ids=["continued", "plain"])
    @pytest.mark.parametrize("dt", [1.0, 0.05])
    def test_continued_branch_folded_matches_stepped(self, cont: bool, dt: float) -> None:
        folded, stepped = _folded_and_stepped(
            lambda: _continued_branch_program(cont),
            _until_hit(int(70 / dt)),
            dt=dt,
            start=self._START,
        )
        assert stepped.state.tags["Seen"] is cont
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
    ``fall(G)``, which fires every scan because ``_prev`` is captured after
    the force.
    """
    from pyrung import copy, fall, latch

    Sec, Log, G, Seen = Int("Sec"), Int("Log"), Bool("G"), Bool("Seen")
    Tmr, Hit = Timer.clone("Tmr"), Bool("Hit")
    with Program(strict=False) as prog:
        with Rung():
            copy(system.rtc.second, Sec)
        if edge:
            with Rung(Tmr.Acc < 0):
                out(G)  # False mid-scan; forced True
            with Rung(fall(G)):
                copy(Sec, Log)
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
    window must have read the phase the window reads, and the edge scan runs
    as a genuine probe, so per-scan work under a clock level or edge is never
    extrapolated from the other phase."""

    @staticmethod
    def _level_gated_counter() -> Program:
        C = Counter.clone("C")
        with Program(strict=False) as prog:
            with Rung(system.sys.clock_1s):
                count_up(C, 100_000).reset(Bool("CR"))
        return prog

    @pytest.mark.parametrize("dt", [0.01, 0.03, 0.07])
    def test_level_gated_counter_counts_every_high_scan(self, dt: float) -> None:
        folded = _assert_fold_is_exact(self._level_gated_counter, _scans(int(10 / dt)), dt=dt)
        assert folded.state.tags["C_Acc"] > 0

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
        ("unit", "seconds", "max_kernel"), [("min", 360_000.0, 4), ("h", 86_400.0, 4)]
    )
    def test_sub_unit_timer_folds_long_runs_cheaply(
        self, unit: str, seconds: float, max_kernel: int
    ) -> None:
        # 36M / 8.6M scans of a timer whose step is a fraction of its unit.
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


class TestSoftClockEdgesAsRead:
    """A soft clock is confirmed inert only from edges a genuine scan read.

    At a ``dt`` of a half-period or more, one scan crosses several edges but
    an edge rung sees only the two reads; counting crossed edges confirmed a
    ``rise()`` clock inert after a single falling scan and dropped every rise.
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
        if tag == "Tmr_Acc":
            assert kernel[True] == kernel[False]
        else:
            assert kernel[True] < 20

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
            with Rung(Bool("En")):
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
    """*reader* gates ``L``; a rise of ``L`` stamps the scan counter ``SC``."""
    SC = Counter.clone("SC")
    with Program(strict=False) as prog:
        with Rung():
            count_up(SC, 2_000_000_000).reset(Bool("SCR"))
        if not above:
            source()
        with Rung(reader()):
            out(Bool("L"))
        with Rung(rise(Bool("L"))):
            from pyrung import copy

            copy(SC.Acc, Int("Stamp"))
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
            "counter": (lambda: count_up(C, 2_000).reset(Bool("CR")), lambda: C.Acc >= 50),
            "selfcalc": (lambda: calc(S + 1, S), lambda: S >= 50),
            "modwrap": (lambda: calc((M + 1) % 1_000, M), lambda: M >= 50),
        }
        body, reader = sources[kind]

        def source() -> None:
            with Rung():
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
            with Program(strict=False) as prog:
                with Rung():
                    if kind == "up":
                        count_up(Counter.clone("C"), 5_000).reset(pulse)
                    elif kind == "down":
                        count_down(Counter.clone("C"), 5_000).reset(pulse)
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
    gating it is bounded at every edge like a gated timer's, not confirmed
    inert from edges where the hidden accumulator merely paused."""

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
            if shape == "counter_preset":
                with Rung():
                    count_up(C, P).reset(Bool("CR"))
            if shape == "reset_cond":
                with Rung():
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

        Tmr, C = Timer.clone("Tmr"), Counter.clone("C")
        with Program(strict=False) as prog:
            with Rung():
                count_up(C, 100_000).reset(Bool("CR"))
            with Rung():
                on_delay(Tmr, 20_000)
            if shape == "display":
                # Nothing reads the display: the landing scan rebuilds it.
                with Rung():
                    calc(30_000 - Tmr.Acc, Int("Left"))
            else:
                # A snapshot gated by an edge never fires inside a window.
                with Rung(rise(Bool("X"))):
                    copy(C.Acc, Int("Snap"))
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
    program graph can see: a window must not leave it stale.

    The program-index cases are a documented gap (runner guide, "Folding"):
    marking every element of such a block read costs the burner most of its
    folds, since its alarm historian indexes DS with unbounded pointers.  The
    fix needs pointer bounds from their writers, or the probe scan recording
    which elements an index resolved to."""

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

    @pytest.mark.xfail(strict=True, reason="unbounded index into a landing-rebuilt display")
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
            with Rung(Bool("En")):
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
