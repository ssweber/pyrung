"""Tests for the limit-cycle classifier (pilot-authorized macro-skip)."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

import pytest

from pyrung import (
    Bool,
    Counter,
    Dint,
    Int,
    Program,
    Rung,
    Timer,
    calc,
    count_up,
    latch,
    on_delay,
    out,
    time_drum,
)
from pyrung.core import rise, system
from pyrung.core.analysis.pilot.cyclefold import _Cycle, cycle_fold_until, detect_cycle
from pyrung.core.analysis.pilot.overlay import PilotRung, fork_with_pilot_rungs
from pyrung.core.runner import PLC


def _osc_ramp_snaps(n: int) -> list[dict[str, object]]:
    """A period-2 oscillation + a +1/scan accumulator + a constant tag."""
    return [{"osc": i % 2, "acc": i, "k": 5} for i in range(n)]


class TestDetectCycle:
    def test_period2_oscillation_with_monotone_acc(self) -> None:
        cyc = detect_cycle(_osc_ramp_snaps(10), monotone_allowed=frozenset({"acc"}))
        assert cyc is not None
        assert cyc.period == 2
        # +1/scan over a 2-scan period is +2/period; osc/k are boundary-stable.
        assert cyc.monotone == {"acc": 2.0}
        assert "osc" not in cyc.monotone
        assert "k" not in cyc.monotone

    def test_pure_plateau_is_period1_no_monotone(self) -> None:
        snaps = [{"a": 1, "b": True} for _ in range(5)]
        cyc = detect_cycle(snaps)
        assert cyc == _Cycle(period=1, monotone={})

    def test_pure_ramp_is_period1_monotone(self) -> None:
        snaps = [{"acc": i} for i in range(5)]
        cyc = detect_cycle(snaps)
        assert cyc is not None
        assert cyc.period == 1
        assert cyc.monotone == {"acc": 1.0}

    def test_multi_monotone_under_one_cycle(self) -> None:
        # Two ramps at different rates, plus a period-3 cycle.
        snaps = [{"cyc": i % 3, "fast": 2 * i, "slow": i} for i in range(12)]
        cyc = detect_cycle(snaps, monotone_allowed=frozenset({"fast", "slow"}))
        assert cyc is not None
        assert cyc.period == 3
        assert cyc.monotone == {"fast": 6.0, "slow": 3.0}

    def test_modular_tag_not_certified_forces_true_period(self) -> None:
        # `cyc` = i % 3 is locally linear over 3 samples.  With it NOT certified
        # monotone, the detector must not extrapolate it as a ramp at P=1; it
        # finds the true period 3 where cyc is boundary-stable.
        snaps = [{"cyc": i % 3, "acc": i} for i in range(12)]
        cyc = detect_cycle(snaps, monotone_allowed=frozenset({"acc"}))
        assert cyc is not None
        assert cyc.period == 3
        assert cyc.monotone == {"acc": 3.0}
        # Unsafe (None) mode would wrongly accept P=1 treating cyc as +1/scan.
        loose = detect_cycle(snaps)
        assert loose is not None and loose.period == 1 and "cyc" in loose.monotone

    def test_chaotic_returns_none(self) -> None:
        # A non-repeating, non-monotone bool sequence: no clean cycle.
        bits = [0, 1, 1, 0, 1, 0, 0, 1, 1, 1, 0, 1, 0, 0]
        snaps = [{"x": b} for b in bits]
        assert detect_cycle(snaps, max_period=6) is None

    def test_too_short_returns_none(self) -> None:
        assert detect_cycle(_osc_ramp_snaps(2)) is None  # need > 2*P snaps

    def test_nonconstant_delta_rejected(self) -> None:
        # Quadratic growth — delta is not constant, so no clean monotone cycle.
        snaps = [{"q": i * i} for i in range(6)]
        assert detect_cycle(snaps) is None

    def test_certified_timer_quantum_canonicalizes_subtraction_noise(self) -> None:
        # The newest anchor is one ULP high.  Exact subtraction produces
        # unequal deltas even though both are the same one-quantum timer step.
        snaps = [{"acc": 3.0}, {"acc": 4.0}, {"acc": math.nextafter(5.0, math.inf)}]
        assert (
            detect_cycle(
                snaps,
                monotone_allowed=frozenset({"acc"}),
                max_period=1,
            )
            is None
        )

        cycle = detect_cycle(
            snaps,
            monotone_allowed=frozenset({"acc"}),
            monotone_quanta={"acc": 1.0},
            max_period=1,
        )

        assert cycle == _Cycle(period=1, monotone={"acc": 1.0})

    def test_timer_quantum_does_not_admit_real_delta_mismatch(self) -> None:
        snaps = [{"acc": 3.0}, {"acc": 4.0}, {"acc": 5.0001}]
        assert (
            detect_cycle(
                snaps,
                monotone_allowed=frozenset({"acc"}),
                monotone_quanta={"acc": 1.0},
                max_period=1,
            )
            is None
        )

    def test_timer_quantum_accounts_for_accumulated_fractional_carry_drift(self) -> None:
        # Real minute-timer anchors from the tumbler, 100 scans apart.  Their
        # difference includes 100 rounds through the hidden float carry.
        snaps = [{"acc": 0.0} for _ in range(201)]
        snaps[0] = {"acc": 0.20166666666666969}
        snaps[100] = {"acc": 0.2183333333333373}
        snaps[200] = {"acc": 0.2350000000000049}
        cycle = detect_cycle(
            snaps,
            monotone_allowed=frozenset({"acc"}),
            monotone_quanta={"acc": 0.010 / 60.0},
            period_multiple_of=100,
            max_period=100,
        )

        assert cycle is not None
        assert cycle.period == 100
        assert cycle.monotone["acc"] == pytest.approx(1.0 / 60.0)

    def test_smallest_period_wins(self) -> None:
        # A period-2 cycle would also "fit" period 4; detector must return 2.
        snaps = [{"osc": i % 2, "acc": i} for i in range(12)]
        cyc = detect_cycle(snaps)
        assert cyc is not None
        assert cyc.period == 2

    def test_fixed_significant_surface_matches_prefiltered_snapshots(self) -> None:
        noise = (0, 1, 1, 0, 1, 0, 0, 1, 1, 1)
        full = [{"osc": i % 2, "acc": i, "ignored": noise[i]} for i in range(len(noise))]
        filtered = [{"osc": snap["osc"], "acc": snap["acc"]} for snap in full]

        expected = detect_cycle(
            filtered,
            monotone_allowed=frozenset({"acc"}),
        )
        retained = detect_cycle(
            full,
            monotone_allowed=frozenset({"acc"}),
            significant_keys=frozenset({"osc", "acc"}),
        )

        assert retained == expected == _Cycle(period=2, monotone={"acc": 2.0})


# ---------------------------------------------------------------------------
# Bit-equal coast: cycle-fold landing == scan-by-scan landing
# ---------------------------------------------------------------------------


def _active_hold_soak(soak_ms: int = 5000, wd_ms: int = 200) -> Program:
    """A long soak timer ramping while a watchdog must be kept reset.

    ``Soak`` ramps unconditionally to ``soak_ms`` and sets ``Done``.  ``WD`` is an
    RTON watchdog that counts whenever ``Osc`` is False and resets when ``Osc`` is
    True — so it only stays reset (never trips) if ``Osc`` keeps oscillating.  That
    is the active-hold soak: a long dwell whose health depends on a sub-cycle that
    must run every scan, exactly the burner rotate case in miniature.
    """
    Osc = Bool("Osc", external=True)
    Soak = Timer.clone("Soak")
    WD = Timer.clone("WD")
    Done = Bool("Done")

    with Program() as prog:
        with Rung():
            on_delay(Soak, soak_ms, "ms")
        with Rung():
            on_delay(WD, wd_ms, "ms").reset(Osc)
        with Rung(Soak.Done):
            out(Done)
    return prog


def _install_oscillator(plc: PLC, tag: str):
    """Flip *tag* every scan via a runner-native reactive patch (period-2)."""

    def _act(s) -> None:  # noqa: ANN001
        plc.patch({tag: not s.tags.get(tag, False)})

    return plc.when(lambda s: True).do(_act)


class TestCycleFoldBitEqual:
    @staticmethod
    def _assert_fallback_stats(
        plc: PLC,
        *,
        fold_context_updates: dict[str, object],
    ) -> None:
        fold_ctx = plc._ensure_fold_context()
        assert fold_ctx is not None
        stats: dict[str, int] = {}

        reached = cycle_fold_until(
            plc,
            lambda _state: False,
            budget=4,
            fold_ctx=replace(fold_ctx, **fold_context_updates),
            stats=stats,
        )

        assert reached is False
        assert stats["logical_scans"] == 4
        assert stats["kernel_scans"] <= stats["logical_scans"]
        assert stats["macro_folds"] >= 0

    def test_scan_derived_fallback_exposes_runner_stats(self) -> None:
        plc = PLC(_active_hold_soak())
        self._assert_fallback_stats(
            plc,
            fold_context_updates={"scan_derived_names": frozenset({"scan_counter"})},
        )

    def test_clock_lcm_fallback_forwards_exact_visible_patches(self) -> None:
        # A per-scan soak: a self-calc (a counter counts edges, not scans).
        Soak = Dint("FallbackReceipt")
        SoakDone = Bool("FallbackReceiptDone")
        with Program() as program:
            with Rung():
                calc(Soak + 1, Soak)
            with Rung(Soak >= 100):
                out(SoakDone)

        plc = PLC(program)
        base_fold_ctx = plc._ensure_fold_context()
        assert base_fold_ctx is not None
        fold_ctx = replace(
            base_fold_ctx,
            clock_half_periods=(20.485,),
        )
        advances: list[tuple[str, object]] = []

        reached = cycle_fold_until(
            plc,
            lambda state: state.tags.get(SoakDone.name) is True,
            budget=5000,
            fold_ctx=fold_ctx,
            advances=advances,
        )

        assert reached is True
        assert advances == [(Soak.name, 98)]

    def test_off_grid_clock_fallback_exposes_runner_stats(self) -> None:
        plc = PLC(_active_hold_soak())
        self._assert_fallback_stats(
            plc,
            # dt=0.01, so this clock's 2.5-scan full period cannot align.
            fold_context_updates={"clock_half_periods": (0.0125,)},
        )

    def test_pathological_clock_lcm_fallback_exposes_runner_stats(self) -> None:
        plc = PLC(_active_hold_soak())
        self._assert_fallback_stats(
            plc,
            # dt=0.01 gives a 4097-scan full period, beyond the cycle bound.
            fold_context_updates={"clock_half_periods": (20.485,)},
        )

    def test_recorded_window_can_budget_logical_scans_with_active_holds(self) -> None:
        Held = Bool("LogicalBudgetHeld", external=True)
        Mirror = Bool("LogicalBudgetMirror")
        Soak = Timer.clone("LogicalBudgetSoak")
        with Program() as program:
            with Rung(Held):
                out(Mirror)
            with Rung():
                on_delay(Soak, 50_000, "ms")

        replay = PLC(program)
        replay.step()
        replay = fork_with_pilot_rungs(replay, (PilotRung(Held.name, True, ~Soak.Done),))
        start_scan = replay.state.scan_id
        stats: dict[str, int] = {}

        reached = cycle_fold_until(
            replay,
            lambda _state: False,
            budget=100,
            kernel_budget=False,
            stats=stats,
        )

        assert reached is False
        assert replay.state.scan_id - start_scan == 100
        assert stats["logical_scans"] == 100
        assert stats["kernel_scans"] < 20
        assert stats["ordinary_folds"] >= 1
        assert (
            stats["ordinary_folded_scans"] + stats["cycle_folded_scans"] + stats["residual_scans"]
            == stats["logical_scans"]
        )
        assert stats["ordinary_folded_scans"] > 0
        assert stats["cycle_folded_scans"] == 0

    def test_ordinary_plateau_fold_is_layered_ahead_of_cycle_detection(self) -> None:
        Soak = Timer.clone("LayeredSoak")
        Done = Bool("LayeredDone")
        with Program() as program:
            with Rung():
                on_delay(Soak, 5000, "ms")
            with Rung(Soak.Done):
                out(Done)

        folded = PLC(program)
        folded.step()
        stats: dict[str, int] = {}
        reached = cycle_fold_until(
            folded,
            lambda state: state.tags.get(Done.name) is True,
            budget=10_000,
            predicate_reads=frozenset((Done.name,)),
            stats=stats,
        )

        assert reached is True
        assert stats["ordinary_folds"] >= 1
        assert stats["cycle_folds"] == 0
        assert stats["skipped_scans"] > 0
        assert stats["ordinary_folded_scans"] == stats["skipped_scans"]
        assert stats["cycle_folded_scans"] == 0
        assert stats["residual_scans"] == stats["kernel_scans"]

    def test_layered_ordinary_fold_forwards_exact_visible_patches(self) -> None:
        # A per-scan soak: a self-calc (a counter counts edges, not scans).
        Soak = Dint("LayeredReceipt")
        SoakDone = Bool("LayeredReceiptDone")
        with Program() as program:
            with Rung():
                calc(Soak + 1, Soak)
            with Rung(Soak >= 100):
                out(SoakDone)

        plc = PLC(program)
        advances: list[tuple[str, object]] = []
        reached = cycle_fold_until(
            plc,
            lambda state: state.tags.get(SoakDone.name) is True,
            budget=5000,
            advances=advances,
        )

        assert reached is True
        assert advances == [(Soak.name, 98)]

    def test_landing_is_bit_equal_to_scan_by_scan(self) -> None:
        # Reference: pure scan-by-scan (fold=False), oscillation running.
        ref = PLC(_active_hold_soak())
        ref.step()
        _install_oscillator(ref, "Osc")
        ref.run_until(lambda s: s.tags.get("Done") is True, max_cycles=20000, fold=False)
        assert ref.state.tags.get("Done") is True

        # Cycle-fold: same setup, folds the soak.
        cf = PLC(_active_hold_soak())
        cf.step()
        _install_oscillator(cf, "Osc")
        stats: dict[str, int] = {}
        advances: list[tuple[str, int | float]] = []
        reached = cycle_fold_until(
            cf,
            lambda s: s.tags.get("Done") is True,
            budget=20000,
            stats=stats,
            advances=advances,
        )

        assert reached is True
        # Bit-equal final state: every tag, the scan id, and the timestamp.
        assert cf.state.tags == ref.state.tags
        assert cf.state.scan_id == ref.state.scan_id
        assert cf.state.timestamp == pytest.approx(ref.state.timestamp)
        # And it got there by folding, in a tiny fraction of the kernel scans.
        assert stats["macro_folds"] >= 1
        assert stats["kernel_scans"] < ref.state.scan_id // 10
        assert stats["logical_scans"] == cf.state.scan_id - 1
        assert stats["skipped_scans"] == stats["logical_scans"] - stats["kernel_scans"]
        assert stats["saved_kernel_scans"] == stats["skipped_scans"]
        assert (
            stats["ordinary_folded_scans"] + stats["cycle_folded_scans"] + stats["residual_scans"]
            == stats["logical_scans"]
        )
        assert advances
        assert any(tag == "Soak_Acc" and value > 0 for tag, value in advances)

    def test_watchdog_never_trips_under_fold(self) -> None:
        # The whole point: folding must NOT let the kept-reset watchdog accumulate.
        cf = PLC(_active_hold_soak(soak_ms=5000, wd_ms=200))
        cf.step()
        _install_oscillator(cf, "Osc")
        cycle_fold_until(cf, lambda s: s.tags.get("Done") is True, budget=20000)
        # WD stayed reset throughout (acc never approached its 200 ms preset).
        assert cf.state.tags.get("WD") in (None, False)

    def test_subtick_timer_progress_is_not_misread_as_a_sterile_cycle(self) -> None:
        Osc = Bool("SubtickOsc", external=True)
        Soak = Timer.clone("SubtickSoak")
        Done = Bool("SubtickDone")
        Mirror = Bool("SubtickMirror")
        with Program() as program:
            with Rung(Osc):
                out(Mirror)
            with Rung():
                on_delay(Soak, 2, "s")
            with Rung(Soak.Done):
                out(Done)

        ref = PLC(program, dt=0.010)
        ref.step()
        _install_oscillator(ref, Osc.name)
        ref.run_until(lambda s: s.tags.get(Done.name) is True, max_cycles=1_000, fold=False)

        cf = PLC(program, dt=0.010)
        cf.step()
        _install_oscillator(cf, Osc.name)
        stats: dict[str, int] = {}
        reached = cycle_fold_until(
            cf,
            lambda s: s.tags.get(Done.name) is True,
            budget=1_000,
            predicate_reads=frozenset((Done.name,)),
            stats=stats,
        )

        assert reached is True
        assert cf.state.tags == ref.state.tags
        assert cf.state.scan_id == ref.state.scan_id
        assert stats.get("sterile_cycle", 0) == 0
        assert stats["macro_folds"] >= 1

    def test_minute_timer_roundoff_still_folds_bit_equal(self) -> None:
        Osc = Bool("MinuteOsc", external=True)
        Soak = Timer.clone("MinuteSoak")
        Done = Bool("MinuteDone")
        Mirror = Bool("MinuteMirror")
        with Program() as program:
            with Rung(Osc):
                out(Mirror)
            with Rung():
                on_delay(Soak, 2, "min")
            with Rung(Soak.Done):
                out(Done)

        ref = PLC(program, dt=0.010)
        ref.step()
        _install_oscillator(ref, Osc.name)
        ref.run_until(Done, max_cycles=20_000, fold=False)

        folded = PLC(program, dt=0.010)
        folded.step()
        _install_oscillator(folded, Osc.name)
        stats: dict[str, int] = {}
        reached = cycle_fold_until(
            folded,
            lambda state: state.tags.get(Done.name) is True,
            budget=20_000,
            predicate_reads=frozenset((Done.name,)),
            stats=stats,
        )

        assert reached is True
        assert folded.state.tags == ref.state.tags
        assert folded.state.scan_id == ref.state.scan_id
        assert stats["macro_folds"] >= 1
        assert stats["kernel_scans"] < 500

    def test_subtick_time_drum_uses_its_current_step_preset(self) -> None:
        Osc = Bool("DrumOsc", external=True)
        Auto = Bool("DrumAuto", external=True)
        Reset = Bool("DrumReset", external=True)
        Step = Int("DrumStep")
        Acc = Int("DrumAcc")
        Done = Bool("DrumDone")
        Output = Bool("DrumOutput")
        Mirror = Bool("DrumMirror")
        with Program() as program:
            with Rung(Osc):
                out(Mirror)
            with Rung(Auto):
                time_drum(
                    outputs=[Output],
                    presets=[1, 2],
                    unit="s",
                    pattern=[[1], [0]],
                    current_step=Step,
                    accumulator=Acc,
                    completion_flag=Done,
                ).reset(Reset)

        def run(*, fold: bool) -> tuple[PLC, dict[str, int]]:
            plc = PLC(program, dt=0.010)
            plc.patch({Auto.name: True})
            plc.step()
            _install_oscillator(plc, Osc.name)
            stats: dict[str, int] = {}
            if fold:
                cycle_fold_until(
                    plc,
                    lambda state: state.tags.get(Done.name) is True,
                    budget=1_000,
                    predicate_reads=frozenset((Done.name,)),
                    stats=stats,
                )
            else:
                plc.run_until(
                    lambda state: state.tags.get(Done.name) is True,
                    max_cycles=1_000,
                    fold=False,
                )
            return plc, stats

        ref, _ = run(fold=False)
        folded, stats = run(fold=True)

        assert folded.state.tags == ref.state.tags
        assert folded.state.scan_id == ref.state.scan_id
        assert folded.state.tags[Step.name] == 2
        assert stats.get("sterile_cycle", 0) == 0
        assert stats["macro_folds"] >= 1


def _oscillation_fed_counter(
    preset: int, *, read_above: bool = False, with_clock: bool = False, period3: bool = False
) -> tuple[Program, Counter]:
    """A counter counting the rising edges of a free-running oscillation.

    Each rise is one count, so the counter is a monotone coordinate of the
    oscillation's cycle (one count a period) and the cycle fold jumps it,
    where the ordinary fold declines a counter that moves on its probe scan.
    A watchdog reset by the oscillation keeps a timer in the window.
    """
    Osc, A = Bool("Osc"), Bool("A")
    C = Counter.clone("CfC")
    WD = Timer.clone("CfWD")
    with Program(strict=False) as prog:
        if read_above:
            with Rung(C.Acc >= preset - 3):
                latch(Bool("Hit"))
        if period3:
            # On one scan in three.
            with Rung(~Osc, ~A):
                out(A)
            with Rung(A):
                out(Osc)
        else:
            with Rung(~Osc):
                out(Osc)
        with Rung(Osc):
            count_up(C, preset).reset(Bool("CfRst", external=True))
        with Rung():
            on_delay(WD, 200, "ms").reset(Osc)
        if with_clock:
            with Rung(system.sys.clock_1s):
                out(Bool("Blink"))
    return prog, C


class TestCycleFoldEdgeCounter:
    """A counter counts the rises of its input: under an oscillation it moves
    one count a period, and a cycle jump lands it where stepping does."""

    @pytest.mark.parametrize(
        ("preset", "kw"),
        [
            (2000, {}),
            (2001, {}),
            (2000, {"read_above": True}),
            (2000, {"with_clock": True}),
            (1500, {"period3": True}),
        ],
    )
    def test_cycle_jump_matches_stepping(self, preset: int, kw: dict[str, bool]) -> None:
        prog, counter = _oscillation_fed_counter(preset, **kw)
        done = counter.Done.name
        folded = PLC(prog)
        advances: list[tuple[str, object]] = []
        assert cycle_fold_until(
            folded, lambda s: s.tags.get(done) is True, budget=50_000, advances=advances
        )
        prog, _ = _oscillation_fed_counter(preset, **kw)
        stepped = PLC(prog)
        while stepped.state.tags.get(done) is not True:
            stepped.step()

        assert any(name == counter.Acc.name for name, _value in advances)
        assert folded.state.scan_id == stepped.state.scan_id
        assert dict(folded.state.tags) == dict(stepped.state.tags)
        memory = {k: v for k, v in folded.state.memory.items() if not k.startswith("_frac")}
        assert memory == {
            k: v for k, v in stepped.state.memory.items() if not k.startswith("_frac")
        }


def _clocked_active_hold_soak(soak_counts: int = 3000) -> Program:
    """An active-hold soak whose period is forced wide by a read system clock.

    The soak is a self-calc (``+1``/scan) so it can reach long dwells
    without the 32 767 timer-accumulator clamp.  ``Blink`` follows ``clock_1s``
    (full period 1 s = 100 scans at the default dt), so folding must align the
    period to the clock (LCM with the period-2 oscillation = 100): the observed
    window spans the clock's full cycle and every jump preserves its phase — the
    runtime form of the soft-clock partition.
    """
    Osc = Bool("Osc", external=True)
    Ctr = Dint("Ctr")
    WD = Timer.clone("WD")
    Done = Bool("Done")
    Blink = Bool("Blink")

    with Program() as prog:
        with Rung():
            calc(Ctr + 1, Ctr)
        with Rung():
            on_delay(WD, 200, "ms").reset(Osc)
        with Rung(system.sys.clock_1s):
            out(Blink)
        with Rung(Ctr >= soak_counts):
            out(Done)
    return prog


class TestCycleFoldAcrossClocks:
    def test_bit_equal_across_a_read_system_clock(self) -> None:
        ref = PLC(_clocked_active_hold_soak())
        ref.step()
        _install_oscillator(ref, "Osc")
        ref.run_until(lambda s: s.tags.get("Done") is True, max_cycles=200000, fold=False)
        assert ref.state.tags.get("Done") is True

        cf = PLC(_clocked_active_hold_soak())
        cf.step()
        _install_oscillator(cf, "Osc")
        stats: dict[str, int] = {}
        reached = cycle_fold_until(
            cf, lambda s: s.tags.get("Done") is True, budget=200000, stats=stats
        )

        assert reached is True
        # Bit-equal across the clock — including Blink (clock phase preserved).
        assert cf.state.tags == ref.state.tags
        assert cf.state.scan_id == ref.state.scan_id
        assert cf.state.tags.get("Blink") == ref.state.tags.get("Blink")
        assert stats["macro_folds"] >= 1

    def test_value_scales_with_soak_length(self) -> None:
        # Observation cost is ~fixed (period-bound); the fold absorbs the rest, so
        # a 10x longer soak costs roughly the same kernel scans.
        def run(soak_counts: int) -> int:
            cf = PLC(_clocked_active_hold_soak(soak_counts=soak_counts))
            cf.step()
            _install_oscillator(cf, "Osc")
            stats: dict[str, int] = {}
            cycle_fold_until(cf, lambda s: s.tags.get("Done") is True, budget=100000, stats=stats)
            assert cf.state.tags.get("Done") is True
            return stats["kernel_scans"]

        short = run(3_000)  # 3_000-scan soak
        long = run(30_000)  # 30_000-scan soak (10x)
        # Kernel scans stay observation-bound: a 10x longer soak is nearly free.
        assert long < short * 2
        assert long < 3_000  # 10x more soak, an order of magnitude under its length


def _rtc_reader_cycle_program() -> Program:
    """A period-2 oscillation beside an unconditional reader of the RTC second
    and a rung that latches on its value: a cycle jump must not skip a tick."""
    from pyrung import calc, copy, latch

    Sec = Int("RcSec")
    Half = Int("RcHalf")
    Seen = Bool("RcSeen")
    T = Bool("RcT")
    Tmr = Timer.clone("RcTmr")
    with Program(strict=False) as prog:
        with Rung():
            copy(system.rtc.second, Sec)
        with Rung(~T):
            out(T)
        with Rung():
            calc(Sec // 30, Half)
        with Rung(Half == 1):
            latch(Seen)
        with Rung():
            on_delay(Tmr, 300_000)
    return prog


class TestCycleFoldRtcTicks:
    @staticmethod
    def _plc() -> PLC:
        from datetime import datetime

        plc = PLC(_rtc_reader_cycle_program(), dt=0.010)
        plc.set_rtc(datetime(2026, 3, 1, 12, 59, 5, 250_000))
        plc.step()
        return plc

    def test_cycle_jump_stops_at_rtc_ticks(self) -> None:
        ref = self._plc()
        for _ in range(6_000):
            ref.step()
        folded = self._plc()
        stats: dict[str, int] = {}
        cycle_fold_until(folded, lambda _s: False, budget=6_000, kernel_budget=False, stats=stats)

        assert ref.state.tags["RcSeen"] is True
        assert stats["cycle_folds"] >= 1
        assert folded.state.scan_id == ref.state.scan_id
        assert folded.state.tags == ref.state.tags

    def test_cause_refuses_a_jumped_scan(self) -> None:
        """A cycle jump's skipped scans never ran: recorded cause() refuses them."""
        folded = self._plc()
        stats: dict[str, int] = {}
        cycle_fold_until(folded, lambda _s: False, budget=6_000, kernel_budget=False, stats=stats)

        assert stats["cycle_folds"] >= 1
        assert folded._fold_windows
        first, last = folded._fold_windows[0]
        assert first < last
        with pytest.raises(ValueError, match="folded"):
            folded.cause("RcTmr_Acc", first)


def _clock_mirror_cycle_program(*, live_stamp: bool = False, continued: bool = False) -> Program:
    """A period-2 oscillation, a ``clock_1s`` contact (so a cycle spans whole
    seconds and every observed window holds an RTC tick), clock mirrors with an
    off history logger, and a 20 s timer.

    *live_stamp* adds a reader of the half-minute while ``CmStopped``;
    *continued* adds a branch on a ``continued()`` rung that reads a mirror and
    is live on the earlier rung's snapshot although off at its own position.
    """
    from pyrung import Or, branch, calc, copy

    T = Bool("CmT")
    Blink = Bool("CmBlink")
    Sec = Int("CmSec")
    HHMM = Int("CmHHMM")
    Alarm = Bool("CmAlarm")
    Hist1, Hist2 = Int("CmHist1"), Int("CmHist2")
    Stopped = Bool("CmStopped")
    StopSec = Int("CmStopSec")
    X, Snap, Half = Int("CmX"), Bool("CmSnap"), Int("CmHalf")
    Hit = Bool("CmHit")
    Tmr = Timer.clone("CmTmr")
    with Program(strict=False) as prog:
        with Rung():
            copy(system.rtc.second, Sec)
        with Rung():
            calc(system.rtc.hour * 100 + system.rtc.minute, HHMM)
        with Rung(~T):
            out(T)
        with Rung(system.sys.clock_1s):
            out(Blink)
        with Rung(rise(Alarm)):
            copy(Hist1, Hist2)
            copy(Sec, Hist1)
        if live_stamp:
            with Rung(Or(Stopped, Alarm)):
                calc(Sec // 30, StopSec)
            with Rung(StopSec > 0):
                out(Bool("CmStopSecHigh"))
        if continued:
            with Rung():
                copy(0, X)
            with Rung(X == 0):
                out(Snap)
            with Rung():
                copy(1, X)
            with Rung().continued():
                with branch(X == 0):
                    calc(Sec // 30, Half)
            with Rung(Half == 1):
                out(Bool("CmHalfOn"))
        with Rung():
            on_delay(Tmr, 20, "s")
        with Rung(Tmr.Done):
            out(Hit)
    return prog


def _assert_history_matches(folded: PLC, ref: PLC) -> None:
    """Every scan the folded run's history serves equals the stepped run's."""
    served = sorted(sid for sid in folded._recent_state_cache if sid <= folded.state.scan_id)
    assert served
    for sid in served:
        want = {k: v for k, v in ref.history.at(sid).tags.items() if not k.startswith("sys.scan")}
        got = {k: v for k, v in folded.history.at(sid).tags.items() if not k.startswith("sys.scan")}
        assert got == want, sid


class TestCycleFoldClockMirrors:
    """Clock mirrors are ignored by the cycle observer, and a jump may cross RTC
    ticks while every reader is off.  The jump leaves them stale, so a real scan
    always follows it."""

    @staticmethod
    def _plc(prog: Program, **patch: object) -> PLC:
        from datetime import datetime

        plc = PLC(prog, dt=0.010)
        plc.set_rtc(datetime(2026, 3, 1, 12, 59, 50, 250_000))
        if patch:
            plc.patch(patch)
        plc.step()
        return plc

    def _stepped(self, prog: Program, scans: int, **patch: object) -> PLC:
        ref = self._plc(prog, **patch)
        for _ in range(scans):
            ref.step()
        return ref

    # The first jump comes at scan 209 (208 scans used): 1_008 leaves exactly
    # eight whole periods, so the jump would otherwise end the coast.
    @pytest.mark.parametrize("budget", [1_008, 1_500])
    def test_cycle_jump_across_ticks_matches_stepping(self, budget: int) -> None:
        ref = self._stepped(_clock_mirror_cycle_program(), budget)
        folded = self._plc(_clock_mirror_cycle_program())
        assert folded._ensure_fold_context().time_mirrors is not None
        stats: dict[str, int] = {}
        cycle_fold_until(
            folded,
            lambda _s: False,
            budget=budget,
            kernel_budget=False,
            predicate_reads=frozenset(),
            stats=stats,
        )

        assert stats["cycle_folds"] >= 1
        assert folded.state.scan_id == ref.state.scan_id
        assert folded.state.tags == ref.state.tags

    def test_history_never_serves_a_stale_mirror(self) -> None:
        """The jumped tip still holds the pre-jump clock copies, so history
        replays that scan instead of serving it."""
        ref = self._stepped(_clock_mirror_cycle_program(), 1_500)
        folded = self._plc(_clock_mirror_cycle_program())
        stats: dict[str, int] = {}
        cycle_fold_until(
            folded,
            lambda _s: False,
            budget=1_500,
            kernel_budget=False,
            predicate_reads=frozenset(),
            stats=stats,
        )
        assert stats["cycle_folds"] >= 1
        _assert_history_matches(folded, ref)

    def test_without_mirrors_the_cycle_never_repeats(self) -> None:
        plc = self._plc(_clock_mirror_cycle_program())
        fold_ctx = replace(plc._ensure_fold_context(), time_mirrors=None)
        stats: dict[str, int] = {}
        cycle_fold_until(
            plc,
            lambda _s: False,
            budget=1_500,
            kernel_budget=False,
            fold_ctx=fold_ctx,
            predicate_reads=frozenset(),
            stats=stats,
        )
        assert stats["cycle_folds"] == 0

    @pytest.mark.parametrize(
        ("program", "patch"),
        [
            ({"live_stamp": True}, {"CmStopped": True}),
            ({"continued": True}, {}),
        ],
        ids=["powered_reader", "continued_branch"],
    )
    def test_live_reader_blocks_the_jump_across_ticks(
        self, program: dict[str, bool], patch: dict[str, object]
    ) -> None:
        ref = self._stepped(_clock_mirror_cycle_program(**program), 1_500, **patch)
        plc = self._plc(_clock_mirror_cycle_program(**program), **patch)
        stats: dict[str, int] = {}
        cycle_fold_until(
            plc,
            lambda _s: False,
            budget=1_500,
            kernel_budget=False,
            predicate_reads=frozenset(),
            stats=stats,
        )

        assert stats.get("cycle_folds", 0) == 0
        assert stats.get("rtc_readers_on", 0) >= 1
        assert plc.state.scan_id == ref.state.scan_id
        assert plc.state.tags == ref.state.tags

    # 208 kernel scans spends the budget on the scan that certifies the first
    # cycle; a jump taken there would end the coast on stale mirrors.
    @pytest.mark.parametrize("budget", [208, 400])
    def test_kernel_budget_never_ends_on_a_jump(self, budget: int) -> None:
        plc = self._plc(_clock_mirror_cycle_program())
        stats: dict[str, int] = {}
        cycle_fold_until(
            plc,
            lambda _s: False,
            budget=budget,
            kernel_budget=True,
            predicate_reads=frozenset(),
            stats=stats,
        )

        ref = self._plc(_clock_mirror_cycle_program())
        for _ in range(plc.state.scan_id - ref.state.scan_id):
            ref.step()
        assert stats["cycle_folds"] == (0 if budget == 208 else 1)
        assert plc.state.tags == ref.state.tags

    @pytest.mark.parametrize("declared", [None, frozenset({"CmSec"})], ids=["undeclared", "copy"])
    def test_predicate_that_may_read_a_copy_keeps_ticks_hard(
        self, declared: frozenset[str] | None
    ) -> None:
        """A predicate reading a clock copy must see every second it shows."""

        def stop(s: Any) -> bool:
            return s.tags.get("CmSec") == 55

        ref = self._plc(_clock_mirror_cycle_program())
        for _ in range(1_500):
            ref.step()
            if stop(ref.state):
                break
        plc = self._plc(_clock_mirror_cycle_program())
        cycle_fold_until(plc, stop, budget=1_500, kernel_budget=False, predicate_reads=declared)
        assert ref.state.tags["CmSec"] == 55
        assert plc.state.scan_id == ref.state.scan_id
        assert plc.state.tags == ref.state.tags


class TestCycleJumpPatchesLand:
    """A cycle jump lands its accumulator edits on the tip, clamped where the
    timer clamps, and a logical budget never ends on the jump itself."""

    @pytest.mark.parametrize("unit", ["s", "ms"])
    def test_budget_end_keeps_a_real_scan_after_the_jump(self, unit: str) -> None:
        def build() -> Program:
            T, Hit = Bool("PjT"), Bool("PjHit")
            Tmr = Timer.clone("PjTmr")
            with Program(strict=False) as prog:
                with Rung(~T):
                    out(T)
                with Rung():
                    on_delay(Tmr, 300 if unit == "s" else 300_000, unit)
                with Rung(Tmr.Done):
                    out(Hit)
            return prog

        ref = PLC(build(), dt=0.010)
        ref.step()
        for _ in range(6_000):
            ref.step()
        plc = PLC(build(), dt=0.010)
        plc.step()
        stats: dict[str, int] = {}
        cycle_fold_until(plc, lambda _s: False, budget=6_000, kernel_budget=False, stats=stats)

        assert stats["cycle_folds"] >= 1
        assert plc.state.scan_id == ref.state.scan_id
        assert plc.state.tags == ref.state.tags
        _assert_history_matches(plc, ref)


class TestCycleJumpReadsTheIntegerAccumulator:
    """A rung reads a timer's integer accumulator, not its fractional progress:
    a seconds timer at 2.07 s reads 2, so ``Acc > 2`` is still ahead of it and
    must bound the jump (the tumbler's rotate step transition went 11 scans
    late when it did not)."""

    @pytest.mark.parametrize(("form", "k"), [("gt", 2), ("ge", 3), ("eq", 3)])
    def test_comparison_on_a_seconds_timer_flips_where_stepping_does(
        self, form: str, k: int
    ) -> None:
        def build() -> Program:
            T, Hit, N = Bool("IaT"), Bool("IaHit"), Int("IaN")
            Tmr = Timer.clone("IaTmr")
            cond = {"gt": Tmr.Acc > k, "ge": Tmr.Acc >= k, "eq": Tmr.Acc == k}[form]
            with Program(strict=False) as prog:
                with Rung(~T):
                    out(T)
                with Rung():
                    on_delay(Tmr, 60, "s")
                with Rung(cond):
                    calc(N + 1, N)
                with Rung(cond):
                    out(Hit)
            return prog

        ref = PLC(build(), dt=0.010)
        ref.step()
        for _ in range(500):
            ref.step()
        plc = PLC(build(), dt=0.010)
        plc.step()
        # Start the coast part-way into a second, as the tumbler's did.
        for _ in range(7):
            plc.step()
        stats: dict[str, int] = {}
        cycle_fold_until(plc, lambda _s: False, budget=493, kernel_budget=False, stats=stats)

        assert stats["cycle_folds"] >= 1
        assert plc.state.scan_id == ref.state.scan_id
        assert plc.state.tags == ref.state.tags
