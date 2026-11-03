"""A coast under an oscillation, a read clock or a pilot hold, against stepping."""

from __future__ import annotations

from typing import Any

import pytest

from pyrung import (
    Block,
    Bool,
    Counter,
    Dint,
    Int,
    Program,
    Rung,
    TagType,
    Timer,
    calc,
    copy,
    count_up,
    latch,
    on_delay,
    out,
    time_drum,
    time_event_drum,
)
from pyrung.core import rise, system
from pyrung.core.analysis.pilot.overlay import PilotRung, fork_with_pilot_rungs
from pyrung.core.coast_run import coast_until
from pyrung.core.runner import PLC


def _active_hold_soak(soak_ms: int = 5000, wd_ms: int = 200) -> Program:
    """A long soak timer ramping while a watchdog must be kept reset.

    ``Soak`` ramps unconditionally to ``soak_ms`` and sets ``Done``.  ``WD`` is an
    RTON watchdog that counts whenever ``Osc`` is False and resets when ``Osc`` is
    True — so it only stays reset (never trips) if ``Osc`` keeps oscillating.  That
    is the active-hold soak: a long dwell whose health depends on a sub-cycle that
    must run every scan, exactly the burner rotate case in miniature.
    """
    Osc = Bool("Osc")
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
        with Rung(~Osc):
            out(Osc)
    return prog


class TestBitEqual:
    def test_budget_counts_logical_scans_with_active_holds(self) -> None:
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

        reached = coast_until(
            replay,
            lambda _state: False,
            budget=100,
            stats=stats,
        )

        assert reached is False
        assert replay.state.scan_id - start_scan == 100
        assert stats["logical_scans"] == 100
        assert stats["kernel_scans"] < 20

    def test_a_plain_soak_lands_as_stepped(self) -> None:
        Soak = Timer.clone("LayeredSoak")
        Done = Bool("LayeredDone")
        with Program() as program:
            with Rung():
                on_delay(Soak, 5000, "ms")
            with Rung(Soak.Done):
                out(Done)

        ref = PLC(program)
        ref.step()
        ref.run_until(Done, max_cycles=10_000, jit=False)
        folded = PLC(program)
        folded.step()
        stats: dict[str, int] = {}
        reached = coast_until(
            folded,
            lambda state: state.tags.get(Done.name) is True,
            budget=10_000,
            predicate_reads=frozenset((Done.name,)),
            stats=stats,
        )

        assert reached is True
        assert stats["stretched_scans"] > 0
        assert folded.state.scan_id == ref.state.scan_id
        assert folded.state.tags == ref.state.tags

    def test_a_self_calc_soak_lands_as_stepped(self) -> None:
        # A per-scan soak: a self-calc (a counter counts edges, not scans).
        Soak = Dint("LayeredReceipt")
        SoakDone = Bool("LayeredReceiptDone")
        with Program() as program:
            with Rung():
                calc(Soak + 1, Soak)
            with Rung(Soak >= 100):
                out(SoakDone)

        ref = PLC(program)
        ref.run_until(SoakDone, max_cycles=5000, jit=False)
        plc = PLC(program)
        reached = coast_until(
            plc,
            lambda state: state.tags.get(SoakDone.name) is True,
            budget=5000,
        )

        assert reached is True
        assert plc.state.scan_id == ref.state.scan_id
        assert plc.state.tags == ref.state.tags

    def test_landing_is_bit_equal_to_scan_by_scan(self) -> None:
        # Reference: pure scan-by-scan (jit=False), oscillation running.
        ref = PLC(_active_hold_soak())
        ref.step()
        ref.run_until(lambda s: s.tags.get("Done") is True, max_cycles=20000, jit=False)
        assert ref.state.tags.get("Done") is True

        # In the loop, from the same setup.
        cf = PLC(_active_hold_soak())
        cf.step()
        stats: dict[str, int] = {}
        reached = coast_until(
            cf,
            lambda s: s.tags.get("Done") is True,
            budget=20000,
            stats=stats,
        )

        assert reached is True
        # Bit-equal final state: every tag, the scan id, and the timestamp.
        assert cf.state.tags == ref.state.tags
        assert cf.state.scan_id == ref.state.scan_id
        assert cf.state.timestamp == ref.state.timestamp
        # And it got there in the loop, in a tiny fraction of the interpreted scans.
        assert stats["stretches"] >= 1
        assert stats["kernel_scans"] < ref.state.scan_id // 10
        assert stats["logical_scans"] == cf.state.scan_id - 1
        assert stats["stretched_scans"] == stats["logical_scans"] - stats["kernel_scans"]

    def test_watchdog_never_trips_in_the_loop(self) -> None:
        # The loop must NOT let the kept-reset watchdog accumulate.
        cf = PLC(_active_hold_soak(soak_ms=5000, wd_ms=200))
        cf.step()
        coast_until(cf, lambda s: s.tags.get("Done") is True, budget=20000)
        # WD stayed reset throughout (acc never approached its 200 ms preset).
        assert cf.state.tags.get("WD") in (None, False)

    def test_subtick_timer_progress_lands_as_stepped(self) -> None:
        Osc = Bool("SubtickOsc")
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
            with Rung(~Osc):
                out(Osc)

        ref = PLC(program, dt=0.010)
        ref.step()
        ref.run_until(lambda s: s.tags.get(Done.name) is True, max_cycles=1_000, jit=False)

        cf = PLC(program, dt=0.010)
        cf.step()
        stats: dict[str, int] = {}
        reached = coast_until(
            cf,
            lambda s: s.tags.get(Done.name) is True,
            budget=1_000,
            predicate_reads=frozenset((Done.name,)),
            stats=stats,
        )

        assert reached is True
        assert cf.state.tags == ref.state.tags
        assert cf.state.scan_id == ref.state.scan_id
        assert stats["stretches"] >= 1

    def test_minute_timer_roundoff_is_bit_equal(self) -> None:
        Osc = Bool("MinuteOsc")
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
            with Rung(~Osc):
                out(Osc)

        ref = PLC(program, dt=0.010)
        ref.step()
        ref.run_until(Done, max_cycles=20_000, jit=False)

        folded = PLC(program, dt=0.010)
        folded.step()
        stats: dict[str, int] = {}
        reached = coast_until(
            folded,
            lambda state: state.tags.get(Done.name) is True,
            budget=20_000,
            predicate_reads=frozenset((Done.name,)),
            stats=stats,
        )

        assert reached is True
        assert folded.state.tags == ref.state.tags
        assert folded.state.scan_id == ref.state.scan_id
        assert stats["stretches"] >= 1
        assert stats["kernel_scans"] < 500

    @pytest.mark.parametrize("kind", ["time", "time_event"])
    def test_drum_reading_another_step_than_the_boundary_shows(self, kind: str) -> None:
        """Writers above and below have the drum time step 2's 500 ms while every
        boundary shows step 3's 200 ms, which it is already past."""
        Long = Timer.clone("Long")
        Step = Int("DrumStep")
        Acc = Int("DrumAcc")
        Done = Bool("DrumDone")
        args: dict[str, Any] = {
            "outputs": [Bool("DrumO1"), Bool("DrumO2")],
            "presets": [200, 500, 200],
            "unit": "ms",
            "pattern": [[1, 0], [0, 1], [1, 1]],
            "current_step": Step,
            "accumulator": Acc,
            "completion_flag": Done,
        }
        with Program(strict=False) as program:
            with Rung():
                on_delay(Long, 3000)
            with Rung(Long.Acc < 1500):
                copy(2, Step)
            with Rung():
                if kind == "time":
                    time_drum(**args).reset(Bool("DrumReset"))
                else:
                    time_event_drum(events=[None, None, None], **args).reset(Bool("DrumReset"))
            with Rung():
                copy(3, Step)

        def done(state: Any) -> bool:
            return state.tags.get(Done.name) is True

        ref = PLC(program, dt=0.007)
        ref.step()
        ref.run_until(done, max_cycles=300, jit=False)
        folded = PLC(program, dt=0.007)
        folded.step()
        coast_until(folded, done, budget=300)
        assert folded.state.scan_id == ref.state.scan_id
        assert folded.state.tags[Acc.name] == ref.state.tags[Acc.name]
        assert folded.state.tags[Long.Acc.name] == ref.state.tags[Long.Acc.name]

    def test_subtick_time_drum_uses_its_current_step_preset(self) -> None:
        Osc = Bool("DrumOsc")
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
            with Rung(~Osc):
                out(Osc)

        def run(*, jit: bool) -> tuple[PLC, dict[str, int]]:
            plc = PLC(program, dt=0.010)
            plc.patch({Auto.name: True})
            plc.step()
            stats: dict[str, int] = {}
            if jit:
                coast_until(
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
                    jit=False,
                )
            return plc, stats

        ref, _ = run(jit=False)
        folded, stats = run(jit=True)

        assert folded.state.tags == ref.state.tags
        assert folded.state.scan_id == ref.state.scan_id
        assert folded.state.tags[Step.name] == 2
        assert stats["stretches"] >= 1


def _oscillation_fed_counter(
    preset: int, *, read_above: bool = False, with_clock: bool = False, period3: bool = False
) -> tuple[Program, Counter]:
    """A counter counting the rising edges of a free-running oscillation.

    Each rise is one count, one count a period.  A watchdog reset by the oscillation keeps
    a timer moving beside it.
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


class TestEdgeCounter:
    """A counter counts the rises of its input: under an oscillation it moves
    one count a period, and a coast lands it where stepping does."""

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
    def test_coast_matches_stepping(self, preset: int, kw: dict[str, bool]) -> None:
        prog, counter = _oscillation_fed_counter(preset, **kw)
        done = counter.Done.name
        folded = PLC(prog)
        assert coast_until(folded, lambda s: s.tags.get(done) is True, budget=50_000)
        prog, _ = _oscillation_fed_counter(preset, **kw)
        stepped = PLC(prog)
        while stepped.state.tags.get(done) is not True:
            stepped.step()

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
    (full period 1 s = 100 scans at the default dt).
    """
    Osc = Bool("Osc")
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
        with Rung(~Osc):
            out(Osc)
    return prog


class TestAcrossClocks:
    def test_bit_equal_across_a_read_system_clock(self) -> None:
        ref = PLC(_clocked_active_hold_soak())
        ref.step()
        ref.run_until(lambda s: s.tags.get("Done") is True, max_cycles=200000, jit=False)
        assert ref.state.tags.get("Done") is True

        cf = PLC(_clocked_active_hold_soak())
        cf.step()
        stats: dict[str, int] = {}
        reached = coast_until(cf, lambda s: s.tags.get("Done") is True, budget=200000, stats=stats)

        assert reached is True
        # Bit-equal across the clock — including Blink (clock phase preserved).
        assert cf.state.tags == ref.state.tags
        assert cf.state.scan_id == ref.state.scan_id
        assert cf.state.tags.get("Blink") == ref.state.tags.get("Blink")
        assert stats["stretches"] >= 1

    def test_value_scales_with_soak_length(self) -> None:
        # A 10x longer soak costs roughly the same interpreted scans.
        def run(soak_counts: int) -> int:
            cf = PLC(_clocked_active_hold_soak(soak_counts=soak_counts))
            cf.step()
            stats: dict[str, int] = {}
            coast_until(cf, lambda s: s.tags.get("Done") is True, budget=100000, stats=stats)
            assert cf.state.tags.get("Done") is True
            return stats["kernel_scans"]

        short = run(3_000)  # 3_000-scan soak
        long = run(30_000)  # 30_000-scan soak (10x)
        assert long < short * 2
        assert long < 3_000  # 10x more soak, an order of magnitude under its length


def _rtc_reader_cycle_program() -> Program:
    """A period-2 oscillation beside an unconditional reader of the RTC second
    and a rung that latches on its value: a coast must not miss a tick."""
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


class TestRtcTicks:
    @staticmethod
    def _plc() -> PLC:
        from datetime import datetime

        plc = PLC(_rtc_reader_cycle_program(), dt=0.010)
        plc.set_rtc(datetime(2026, 3, 1, 12, 59, 5, 250_000))
        plc.step()
        return plc

    def test_coast_stops_at_rtc_ticks(self) -> None:
        ref = self._plc()
        for _ in range(6_000):
            ref.step()
        folded = self._plc()
        stats: dict[str, int] = {}
        coast_until(folded, lambda _s: False, budget=6_000, stats=stats)

        assert ref.state.tags["RcSeen"] is True
        assert stats["stretches"] >= 1
        assert folded.state.scan_id == ref.state.scan_id
        assert folded.state.tags == ref.state.tags

    def test_cause_answers_a_loop_scan_as_stepping_does(self) -> None:
        """A question about a scan inside a stretch answers as stepping does."""
        stepped = self._plc()
        for _ in range(6_000):
            stepped.step()
        folded = self._plc()
        stats: dict[str, int] = {}
        coast_until(folded, lambda _s: False, budget=6_000, stats=stats)

        assert stats["stretches"] >= 1
        for scan in (3_000, 5_999):
            assert str(folded.cause("RcTmr_Acc", scan)) == str(stepped.cause("RcTmr_Acc", scan))


def _clock_mirror_cycle_program(
    *, live_stamp: bool = False, continued: bool = False, continued_live: bool = False
) -> Program:
    """A period-2 oscillation, a ``clock_1s`` contact (so a cycle spans whole
    seconds and every observed window holds an RTC tick), clock mirrors with an
    off history logger, and a 20 s timer.

    *live_stamp* adds a reader of the half-minute while ``CmStopped``;
    *continued* adds a branch on a ``continued()`` rung that reads a mirror
    behind ``X == 0``, written 1 on the rung above.  A continued rung reads
    live, so the branch is off where it stands (it was on when it read the
    earlier rung's snapshot); *continued_live* makes it ``X == 1``, on there.
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
        if continued or continued_live:
            with Rung():
                copy(0, X)
            with Rung(X == 0):
                out(Snap)
            with Rung():
                copy(1, X)
            with Rung().continued():
                with branch(X == (1 if continued_live else 0)):
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


class TestClockMirrors:
    """Clock mirrors, with their readers on and off: a coast must never leave one stale."""

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

    @pytest.mark.parametrize("budget", [1_008, 1_500])
    def test_coast_across_ticks_matches_stepping(self, budget: int) -> None:
        ref = self._stepped(_clock_mirror_cycle_program(), budget)
        folded = self._plc(_clock_mirror_cycle_program())
        stats: dict[str, int] = {}
        coast_until(
            folded,
            lambda _s: False,
            budget=budget,
            predicate_reads=frozenset(),
            stats=stats,
        )

        assert stats["stretches"] >= 1
        assert folded.state.scan_id == ref.state.scan_id
        assert folded.state.tags == ref.state.tags

    def test_history_never_serves_a_stale_mirror(self) -> None:
        ref = self._stepped(_clock_mirror_cycle_program(), 1_500)
        folded = self._plc(_clock_mirror_cycle_program())
        stats: dict[str, int] = {}
        coast_until(
            folded,
            lambda _s: False,
            budget=1_500,
            predicate_reads=frozenset(),
            stats=stats,
        )
        assert stats["stretches"] >= 1
        _assert_history_matches(folded, ref)

    @pytest.mark.parametrize(
        ("program", "patch"),
        [
            ({"live_stamp": True}, {"CmStopped": True}),
            ({"continued_live": True}, {}),
        ],
        ids=["powered_reader", "continued_branch_on_where_it_stands"],
    )
    def test_live_reader_across_ticks(
        self, program: dict[str, bool], patch: dict[str, object]
    ) -> None:
        ref = self._stepped(_clock_mirror_cycle_program(**program), 1_500, **patch)
        plc = self._plc(_clock_mirror_cycle_program(**program), **patch)
        stats: dict[str, int] = {}
        coast_until(
            plc,
            lambda _s: False,
            budget=1_500,
            predicate_reads=frozenset(),
            stats=stats,
        )

        assert plc.state.scan_id == ref.state.scan_id
        assert plc.state.tags == ref.state.tags

    @pytest.mark.parametrize("budget", [400, 1_500, 3_000])
    def test_continued_branch_off_where_it_stands(self, budget: int) -> None:
        """A continued rung reads live (the row solve), so a branch behind
        ``X == 0`` under ``copy(1, X)`` never runs, though it was on when a
        continued rung read the earlier rung's snapshot.  Its mirror reader is
        dead, and the coast equals stepping, history included."""
        ref = self._stepped(_clock_mirror_cycle_program(continued=True), budget)
        plc = self._plc(_clock_mirror_cycle_program(continued=True))
        stats: dict[str, int] = {}
        coast_until(
            plc,
            lambda _s: False,
            budget=budget,
            predicate_reads=frozenset(),
            stats=stats,
        )

        assert stats["stretches"] >= 1
        assert plc.state.scan_id == ref.state.scan_id
        assert plc.state.tags == ref.state.tags
        _assert_history_matches(plc, ref)

    @pytest.mark.parametrize("budget", [208, 400])
    def test_a_short_budget_ends_as_stepped(self, budget: int) -> None:
        plc = self._plc(_clock_mirror_cycle_program())
        stats: dict[str, int] = {}
        coast_until(
            plc,
            lambda _s: False,
            budget=budget,
            predicate_reads=frozenset(),
            stats=stats,
        )

        ref = self._plc(_clock_mirror_cycle_program())
        for _ in range(plc.state.scan_id - ref.state.scan_id):
            ref.step()
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
        coast_until(plc, stop, budget=1_500, predicate_reads=declared)
        assert ref.state.tags["CmSec"] == 55
        assert plc.state.scan_id == ref.state.scan_id
        assert plc.state.tags == ref.state.tags


class TestAcrossAClamp:
    """A coast across a timer's clamp lands on the tip where stepping does."""

    @pytest.mark.parametrize("unit", ["s", "ms"])
    def test_budget_end_matches_stepping(self, unit: str) -> None:
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
        coast_until(plc, lambda _s: False, budget=6_000, stats=stats)

        assert stats["stretches"] >= 1
        assert plc.state.scan_id == ref.state.scan_id
        assert plc.state.tags == ref.state.tags
        _assert_history_matches(plc, ref)


class TestReadsTheIntegerAccumulator:
    """A rung reads a timer's integer accumulator, not its fractional progress:
    a seconds timer at 2.07 s reads 2, so ``Acc > 2`` is still ahead of it and
    must end the stretch (the tumbler's rotate step transition went 11 scans late
    when it did not)."""

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
        coast_until(plc, lambda _s: False, budget=493, stats=stats)

        assert stats["stretches"] >= 1
        assert plc.state.scan_id == ref.state.scan_id
        assert plc.state.tags == ref.state.tags


def _coast_and_step(
    build: Any,
    *,
    budget: int = 300,
    first: dict[str, Any] | None = None,
    force: tuple[str, Any] | None = None,
    react: Any = None,
) -> tuple[PLC, PLC, dict[str, int]]:
    """The same run in a coast and stepped; a ``react`` runs every scan."""

    def make() -> PLC:
        plc = PLC(build(), dt=0.010)
        plc.patch(first or {})
        if force:
            plc.force(*force)
        plc.step()
        if react:
            plc.when(lambda _s: True).do(lambda s: react(plc, s))
        return plc

    ref = make()
    for _ in range(budget):
        ref.step()
    plc = make()
    stats: dict[str, int] = {}
    coast_until(plc, lambda _s: False, budget=budget, stats=stats)
    return plc, ref, stats


def _assert_same_tags(plc: PLC, ref: PLC) -> None:
    assert plc.state.scan_id == ref.state.scan_id
    keys = {k for k in plc.state.tags.keys() | ref.state.tags.keys() if not k.startswith("sys.")}
    assert {k: plc.state.tags.get(k) for k in keys} == {k: ref.state.tags.get(k) for k in keys}


class TestDrumStep:
    """A drum reads its preset from the step it sees, which may not be the step a scan
    boundary shows."""

    @staticmethod
    def _program(
        presets: tuple[int, int, int] = (200, 5000, 5000),
        kind: str = "time",
        above: int | None = None,
        below: int | None = None,
    ) -> Any:
        def build() -> Program:
            O1, O2, Flag, Step = Bool("O1"), Bool("O2"), Bool("Flag"), Int("Step")
            N, En, R = Int("N"), Bool("En"), Bool("R")
            args: dict[str, Any] = {
                "outputs": [O1, O2],
                "pattern": [[1, 0], [0, 1], [1, 1]],
                "presets": list(presets),
                "current_step": Step,
                "completion_flag": Flag,
                "accumulator": Int("DAcc"),
                "unit": "ms",
            }
            with Program(strict=False) as prog:
                with Rung():
                    on_delay(Timer.clone("Long"), 60_000)
                if above is not None:
                    with Rung():
                        copy(above, Step)
                with Rung(En):
                    if kind == "time":
                        time_drum(**args).reset(R)
                    else:
                        time_event_drum(events=[None, None, None], **args).reset(R)
                if below is not None:
                    with Rung():
                        copy(below, Step)
                with Rung(O2):
                    calc(N + 1, N)
                with Rung(Flag):
                    on_delay(Timer.clone("Rd"), 60_000)
            return prog

        return build

    @pytest.mark.parametrize("kind", ["time", "event"])
    def test_a_force_over_a_write_above_the_drum(self, kind: str) -> None:
        plc, ref, _stats = _coast_and_step(
            self._program(kind=kind, above=1), first={"En": True}, force=("Step", 3)
        )
        _assert_same_tags(plc, ref)
        assert ref.state.tags["N"] > 200

    @pytest.mark.parametrize("kind", ["time", "event"])
    def test_a_callback_patch_over_a_write_below_the_drum(self, kind: str) -> None:
        plc, ref, _stats = _coast_and_step(
            self._program((5000, 5000, 200), kind, below=1),
            first={"En": True},
            react=lambda plc, _s: plc.patch({"Step": 3}),
        )
        _assert_same_tags(plc, ref)
        assert ref.state.tags["Rd_Acc"] > 2000

    @pytest.mark.parametrize("presets", [(5000, 5000, 200), (200, 5000, 5000)])
    @pytest.mark.parametrize("first", [1, 3])
    @pytest.mark.parametrize("budget", [101, 150, 151, 300, 301])
    def test_a_step_swapped_every_scan(
        self, presets: tuple[int, int, int], first: int, budget: int
    ) -> None:
        def swap(plc: PLC, state: Any) -> None:
            plc.patch({"Step": 1 if state.tags.get("Step") == 3 else 3})

        plc, ref, _stats = _coast_and_step(
            self._program(presets), budget=budget, first={"En": True, "Step": first}, react=swap
        )
        _assert_same_tags(plc, ref)

    def test_a_step_nothing_writes_stays_in_the_loop(self) -> None:
        def build() -> Program:
            Tog = Bool("Tog")
            with Program(strict=False) as prog:
                with Rung(~Tog):
                    out(Tog)
                with Rung(Bool("En")):
                    time_drum(
                        outputs=[Bool("O1"), Bool("O2")],
                        pattern=[[1, 0], [0, 1]],
                        presets=[20_000, 5000],
                        current_step=Int("Step"),
                        completion_flag=Bool("Flag"),
                        accumulator=Int("DAcc"),
                        unit="ms",
                    ).reset(Bool("R"))
            return prog

        plc, ref, stats = _coast_and_step(build, budget=1500, first={"En": True})
        _assert_same_tags(plc, ref)
        assert stats["stretches"] >= 1


class TestWrittenInputs:
    """The same read, with the tag written around it: a scan boundary shows one value, and
    the reader reads another."""

    @staticmethod
    def _timer(above: int | None, below: int | None, comparison: bool = False) -> Any:
        def build() -> Program:
            Tog, P, T, Hit = Bool("Tog"), Int("P"), Timer.clone("T"), Bool("Hit")
            with Program(strict=False) as prog:
                with Rung(~Tog):
                    out(Tog)
                if above is not None:
                    with Rung():
                        copy(above, P)
                if comparison:
                    with Rung():
                        on_delay(T, 30_000)
                    with Rung(T.Acc >= P):
                        latch(Hit)
                else:
                    with Rung():
                        on_delay(T, P)
                    with Rung(T.Done):
                        latch(Hit)
                if below is not None:
                    with Rung():
                        copy(below, P)
                with Rung(Bool("Hit")):
                    calc(Int("Since") + 1, Int("Since"))
            return prog

        return build

    @pytest.mark.parametrize("comparison", [False, True], ids=["preset", "comparison"])
    def test_written_around_the_reader(self, comparison: bool) -> None:
        plc, ref, _stats = _coast_and_step(self._timer(200, 5000, comparison))
        _assert_same_tags(plc, ref)
        assert ref.state.tags["Hit"] is True

    @pytest.mark.parametrize("comparison", [False, True], ids=["preset", "comparison"])
    def test_written_on_one_side_stays_in_the_loop(self, comparison: bool) -> None:
        for above, below in ((20_000, None), (None, 20_000)):
            plc, ref, stats = _coast_and_step(
                self._timer(above, below, comparison), budget=1500, first={"P": 20_000}
            )
            _assert_same_tags(plc, ref)
            assert stats["stretches"] >= 1

    def test_a_force_over_a_write_above_the_reader(self) -> None:
        plc, ref, _stats = _coast_and_step(self._timer(200, None), force=("P", 5000))
        _assert_same_tags(plc, ref)
        assert ref.state.tags["Hit"] is True

    def test_a_callback_patch_over_a_write_below_the_reader(self) -> None:
        plc, ref, _stats = _coast_and_step(
            self._timer(None, 5000), first={"P": 200}, react=lambda plc, _s: plc.patch({"P": 200})
        )
        _assert_same_tags(plc, ref)
        assert ref.state.tags["Hit"] is True

    @pytest.mark.parametrize("first", [(200, 5000), (5000, 200)], ids=["low", "high"])
    def test_an_operand_that_alternates_through_the_period(self, first: tuple[int, int]) -> None:
        """Two writers, one per phase of the cycle: every boundary but one phase lies."""

        def build() -> Program:
            Tog, P, T, Hit = Bool("Tog"), Int("P"), Timer.clone("T"), Bool("Hit")
            with Program(strict=False) as prog:
                with Rung(~Tog):
                    out(Tog)
                with Rung(Tog):
                    copy(first[0], P)
                with Rung(~Tog):
                    copy(first[1], P)
                with Rung():
                    on_delay(T, 30_000)
                with Rung(T.Acc >= P):
                    latch(Hit)
                with Rung(Bool("Hit")):
                    calc(Int("Since") + 1, Int("Since"))
            return prog

        plc, ref, _stats = _coast_and_step(build)
        _assert_same_tags(plc, ref)
        assert ref.state.tags["Hit"] is True

    @pytest.mark.parametrize("what", ["preset", "comparison"])
    def test_a_counter_bound_written_around_its_reader(self, what: str) -> None:
        def build() -> Program:
            Tog, P, Ctr, Hit = Bool("Tog"), Int("P"), Counter.clone("Ctr"), Bool("Hit")
            with Program(strict=False) as prog:
                with Rung(~Tog):
                    out(Tog)
                with Rung():
                    copy(20, P)
                with Rung(Tog):
                    if what == "preset":
                        count_up(Ctr, P).reset(Bool("CR"))
                    else:
                        count_up(Ctr, 20_000).reset(Bool("CR"))
                if what == "preset":
                    with Rung(Ctr.Done):
                        latch(Hit)
                else:
                    with Rung(Ctr.Acc >= P):
                        latch(Hit)
                with Rung():
                    copy(5000, P)
                with Rung(Bool("Hit")):
                    calc(Int("Since") + 1, Int("Since"))
            return prog

        plc, ref, _stats = _coast_and_step(build)
        _assert_same_tags(plc, ref)
        assert ref.state.tags["Hit"] is True


class TestInputWrites:
    """A tag a rung may read between writes, written directly, through a pointer, or only
    on some scans of an oscillation."""

    BIG = Block("CwBig", TagType.INT, 1, 2000)

    @staticmethod
    def _indirect(
        above: int | None = None,
        below: int | None = None,
        writer_on: bool = False,
    ) -> Any:
        """A 2-scan cycle with a preset in a block a rung writes through a pointer."""

        def build() -> Program:
            Big = TestInputWrites.BIG
            Tog, T, Hit = Bool("Tog"), Timer.clone("T"), Bool("Hit")
            Ptr, Cmd = Int("Ptr"), Bool("Cmd")
            with Program(strict=False) as prog:
                with Rung(~Tog):
                    out(Tog)
                if above is not None:
                    with Rung():
                        copy(above, Big[5])
                with Rung():
                    on_delay(T, Big[5])
                with Rung(T.Done):
                    latch(Hit)
                if below is not None:
                    with Rung():
                        copy(below, Big[Ptr])
                with Rung(*(() if writer_on else (Cmd,))):
                    copy(9, Big[Int("Ptr2")])
            return prog

        return build

    def test_an_idle_indirect_writer_stays_in_the_loop(self) -> None:
        name = self.BIG[5].name
        plc, ref, stats = _coast_and_step(self._indirect(), budget=2500, first={name: 20_000})
        _assert_same_tags(plc, ref)
        assert ref.state.tags["Hit"] is True
        assert stats["stretches"] >= 1
        assert stats["stretched_scans"] >= 1500

    def test_an_indirect_write_of_another_slot_stays_in_the_loop(self) -> None:
        name = self.BIG[5].name
        plc, ref, stats = _coast_and_step(
            self._indirect(writer_on=True),
            budget=2500,
            first={name: 20_000, "Ptr2": 7},
        )
        _assert_same_tags(plc, ref)
        assert stats["stretches"] >= 1

    def test_an_indirect_write_around_the_reader(self) -> None:
        plc, ref, stats = _coast_and_step(
            self._indirect(above=200, below=5000), budget=300, first={"Ptr": 5}
        )
        _assert_same_tags(plc, ref)
        assert ref.state.tags["Hit"] is True

    def test_a_pointer_walking_onto_the_input(self) -> None:
        def build() -> Program:
            Big = self.BIG
            Tog, T, Hit, Ptr = Bool("Tog"), Timer.clone("T"), Bool("Hit"), Int("Ptr")
            with Program(strict=False) as prog:
                with Rung(~Tog):
                    out(Tog)
                with Rung():
                    calc(Ptr + 1, Ptr)
                with Rung():
                    on_delay(T, Big[50])
                with Rung(T.Done):
                    latch(Hit)
                with Rung(Ptr >= 1):
                    copy(20, Big[Ptr])
            return prog

        plc, ref, _stats = _coast_and_step(build, budget=1500, first={self.BIG[50].name: 20_000})
        _assert_same_tags(plc, ref)
        assert ref.state.tags["Hit"] is True

    @staticmethod
    def _gated(above: str, below: str) -> Any:
        """A cycle with writers around the reader, on the toggle's phases."""

        def build() -> Program:
            Tog, P, T, Hit = Bool("Tog"), Int("P"), Timer.clone("T"), Bool("Hit")
            gate = {"on": Tog, "off": ~Tog, "never": Bool("Never")}
            with Program(strict=False) as prog:
                with Rung(~Tog):
                    out(Tog)
                with Rung(gate[above]):
                    copy(2000, P)
                with Rung():
                    on_delay(T, P)
                with Rung(T.Done):
                    latch(Hit)
                with Rung(gate[below]):
                    copy(5000, P)
            return prog

        return build

    def test_idle_writers_around_the_reader_stay_in_the_loop(self) -> None:
        plc, ref, stats = _coast_and_step(
            self._gated("never", "never"), budget=3000, first={"P": 28_000}
        )
        _assert_same_tags(plc, ref)
        assert ref.state.tags["Hit"] is True
        assert stats["stretches"] >= 1
        assert stats["stretched_scans"] >= 2000

    @pytest.mark.parametrize(
        ("above", "below"),
        [("on", "on"), ("off", "off"), ("on", "off"), ("off", "on")],
    )
    def test_writes_in_only_some_scans_of_the_period(self, above: str, below: str) -> None:
        plc, ref, stats = _coast_and_step(self._gated(above, below), budget=700)
        _assert_same_tags(plc, ref)
        assert ref.state.tags["Hit"] is True

    def test_a_writer_woken_by_the_timer_itself(self) -> None:
        def build() -> Program:
            Tog, P, T, Hit = Bool("Tog"), Int("P"), Timer.clone("T"), Bool("Hit")
            with Program(strict=False) as prog:
                with Rung(~Tog):
                    out(Tog)
                with Rung(T.Acc >= 1000):
                    copy(200, P)
                with Rung():
                    on_delay(T, P)
                with Rung(T.Done):
                    latch(Hit)
                with Rung(T.Acc >= 1000):
                    copy(5000, P)
            return prog

        plc, ref, _stats = _coast_and_step(build, budget=2500, first={"P": 20_000})
        _assert_same_tags(plc, ref)
        assert ref.state.tags["Hit"] is True

    @staticmethod
    def _mirror_program() -> Program:
        """Clock mirrors, an indirect writer that stores one when asked, and a preset
        in the block it writes: the writer's cone reaches the mirrors."""
        Big = TestInputWrites.BIG
        Tog, T, Hit = Bool("Tog"), Timer.clone("T"), Bool("Hit")
        Sec, Blink, Cmd, Ptr = Int("CwSec"), Bool("Blink"), Bool("Cmd"), Int("Ptr")
        with Program(strict=False) as prog:
            with Rung():
                copy(system.rtc.second, Sec)
            with Rung(~Tog):
                out(Tog)
            with Rung(system.sys.clock_1s):
                out(Blink)
            with Rung():
                on_delay(T, Big[5])
            with Rung(T.Done):
                latch(Hit)
            with Rung(Cmd):
                copy(Sec, Big[Ptr])
        return prog

    @classmethod
    def _mirror_plc(cls) -> PLC:
        from datetime import datetime

        plc = PLC(cls._mirror_program(), dt=0.010)
        plc.set_rtc(datetime(2026, 3, 1, 12, 59, 50, 250_000))
        plc.patch({cls.BIG[5].name: 30_000, "Ptr": 5})
        plc.step()
        return plc

    def test_a_writer_storing_a_clock_mirror(self) -> None:
        plc = self._mirror_plc()
        ref = self._mirror_plc()
        for _ in range(1500):
            ref.step()
        stats: dict[str, int] = {}
        coast_until(
            plc,
            lambda _s: False,
            budget=1500,
            predicate_reads=frozenset(),
            stats=stats,
        )
        assert stats["stretches"] >= 1
        assert plc.state.scan_id == ref.state.scan_id
        assert plc.state.tags == ref.state.tags

    def test_the_mirror_writer_waking_after_a_stretch(self) -> None:
        plc, ref = self._mirror_plc(), self._mirror_plc()
        for leg, cmd in ((1500, False), (1500, True)):
            for runner in (plc, ref):
                runner.patch({"Cmd": cmd})
            for _ in range(leg):
                ref.step()
            coast_until(plc, lambda _s: False, budget=leg, predicate_reads=frozenset())
            assert plc.state.scan_id == ref.state.scan_id
            assert plc.state.tags == ref.state.tags
        assert ref.state.tags["Hit"] is True
