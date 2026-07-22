"""Tests for the limit-cycle classifier (pilot-authorized macro-skip)."""

from __future__ import annotations

import pytest

from pyrung import Bool, Counter, Int, Program, Rung, Timer, count_up, on_delay, out, time_drum
from pyrung.core import system
from pyrung.core.analysis.pilot.cyclefold import _Cycle, cycle_fold_until, detect_cycle
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
        # And it got there by folding, in a tiny fraction of the real scans.
        assert stats["folds"] >= 1
        assert stats["real_scans"] < ref.state.scan_id // 10
        assert stats["logical_scans"] == cf.state.scan_id - 1
        assert stats["kernel_scans"] == stats["real_scans"]
        assert stats["macro_folds"] == stats["folds"]
        assert stats["skipped_scans"] == stats["logical_scans"] - stats["kernel_scans"]
        assert stats["saved_kernel_scans"] == stats["skipped_scans"]
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
        assert stats["folds"] >= 1

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
        assert stats["folds"] >= 1


def _clocked_active_hold_soak(soak_counts: int = 3000) -> Program:
    """An active-hold soak whose period is forced wide by a read system clock.

    The soak is a count-up counter (``+1``/scan) so it can reach long dwells
    without the 32 767 timer-accumulator clamp.  ``Blink`` follows ``clock_1s``
    (full period 1 s = 100 scans at the default dt), so folding must align the
    period to the clock (LCM with the period-2 oscillation = 100): the observed
    window spans the clock's full cycle and every jump preserves its phase — the
    runtime form of the soft-clock partition.
    """
    Osc = Bool("Osc", external=True)
    Ctr = Counter.clone("Ctr")
    NeverReset = Bool("NeverReset", external=True)
    WD = Timer.clone("WD")
    Done = Bool("Done")
    Blink = Bool("Blink")

    with Program() as prog:
        with Rung():
            count_up(Ctr, soak_counts).reset(NeverReset)
        with Rung():
            on_delay(WD, 200, "ms").reset(Osc)
        with Rung(system.sys.clock_1s):
            out(Blink)
        with Rung(Ctr.Done):
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
        assert stats["folds"] >= 1

    def test_value_scales_with_soak_length(self) -> None:
        # Observation cost is ~fixed (period-bound); the fold absorbs the rest, so
        # a 10x longer soak costs roughly the same real scans.
        def run(soak_counts: int) -> int:
            cf = PLC(_clocked_active_hold_soak(soak_counts=soak_counts))
            cf.step()
            _install_oscillator(cf, "Osc")
            stats: dict[str, int] = {}
            cycle_fold_until(cf, lambda s: s.tags.get("Done") is True, budget=100000, stats=stats)
            assert cf.state.tags.get("Done") is True
            return stats["real_scans"]

        short = run(3_000)  # 3_000-scan soak
        long = run(30_000)  # 30_000-scan soak (10x)
        # Real scans stay observation-bound: a 10x longer soak is nearly free.
        assert long < short * 2
        assert long < 3_000  # 10x more soak, an order of magnitude under its length
