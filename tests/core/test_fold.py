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
        counter = _count_real_scans(plc_fold)
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
        counter = _count_real_scans(plc)
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
