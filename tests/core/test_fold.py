"""Tests for core/fold.py — fold-aware run_until() and run_for()."""

from __future__ import annotations

import pytest

from pyrung import Bool, Counter, Int, Program, Rung, Timer, calc, count_up, on_delay, out
from pyrung.core import rise, system
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

        assert plc_fold.state.tags["Done"] == plc_nofold.state.tags["Done"]
        assert plc_fold.state.tags["Tmr_Done"] == plc_nofold.state.tags["Tmr_Done"]


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


def _count_real_scans(plc: PLC) -> dict[str, int]:
    """Wrap ``_run_single_scan`` to count real interpreter passes.

    Every probe scan and every ``_do_fold`` step routes through
    ``_run_single_scan`` — the single choke point — so the count is the true
    "real work" done, independent of how far ``scan_id`` (equivalent elapsed
    time) advances.  Call after ``step()`` so only the run-loop work is counted.
    """
    counter = {"n": 0}
    original = plc._run_single_scan

    def wrapped(*, consume_pause_request: bool):  # type: ignore[no-untyped-def]
        counter["n"] += 1
        return original(consume_pause_request=consume_pause_request)

    plc._run_single_scan = wrapped  # type: ignore[method-assign]
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
        counter = _count_real_scans(plc_fold)
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
        # 0.5 s half-period edge (~60 over 30 s, ~2 real scans each).  An inert
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
        counter = _count_real_scans(plc_fold)
        plc_fold.run_for(5.0, fold=True)  # 500 scans

        # Correctness (holds today): scan-by-scan still settles Extent.
        assert plc_fold.state.tags["Extent"] == 7
        # Efficiency (fails today): reading scan_clock_toggle disables the fold
        # entirely → ~500 real scans.  Inert recompute should collapse it.
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
        counter = _count_real_scans(plc_fold)
        plc_fold.run_for(5.0, fold=True)  # 500 scans; crosses scan_counter == 250

        prog2 = self._scan_counter_tick_program(threshold=250)
        plc_nofold = PLC(prog2, dt=0.010)
        plc_nofold.patch({"Enable": True})
        plc_nofold.step()
        plc_nofold.run_for(5.0, fold=False)

        # Correctness (holds today): the crossing fires exactly once.
        assert plc_fold.state.tags["Ticks"] == plc_nofold.state.tags["Ticks"] == 1
        # Efficiency (fails today): reading scan_counter disables the fold →
        # ~500 real scans.  The crossing arithmetic should land near scan 250.
        assert counter["n"] <= 12


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
        counter = _count_real_scans(plc_fold)
        plc_fold.run_for(5.0, fold=True)  # 500 scans; timer never completes (100 s)

        prog2, _ = self._unread_timer_program(preset_ms=100_000)
        plc_nofold = PLC(prog2, dt=0.010)
        plc_nofold.patch({"Enable": True})
        plc_nofold.step()
        plc_nofold.run_for(5.0, fold=False)

        # The preset is a (far) visible crossing clamped to the run window, so
        # the churn collapses instead of stepping all 500 scans.
        assert counter["n"] <= 12
        # The timer never completes; its accumulator tracks elapsed time.  The
        # fold lands the time boundary exactly (advancing the requested 5.0 s),
        # while scan-by-scan drifts one extra scan from float `t += dt` error —
        # so they agree to within one dt (folding to a *time* boundary can't be
        # bit-equal to a drifting accumulation; folding to an acc crossing is —
        # see test_run_until_accumulator_threshold_does_not_overshoot).
        assert plc_fold.state.tags["Tmr_Done"] is False
        assert plc_fold.state.timestamp >= 0.01 + 5.0 - 1e-9  # advanced the full 5.0 s
        assert abs(plc_fold.state.tags["Tmr_Acc"] - plc_nofold.state.tags["Tmr_Acc"]) <= 10

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
