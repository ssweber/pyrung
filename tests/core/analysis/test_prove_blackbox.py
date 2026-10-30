"""Black-box timers (prove/blackbox.py): Done is a choice, every counterexample replays.

The prover's kernel ticks timers by choice and the search confirms what it
finds on the concrete PLC.  These tests run that model explicitly
(``_OptConfig(abstract_timers=True)``); the exact-timing layer stays the
default until it is deleted.
"""

from __future__ import annotations

import pytest

from pyrung.circuitpy.codegen import compile_kernel
from pyrung.core import (
    PLC,
    Bool,
    Counter,
    Int,
    Program,
    Rung,
    Timer,
    copy,
    count_up,
    flasher,
    latch,
    off_delay,
    on_delay,
    out,
    rise,
)
from pyrung.core.analysis.prove import (
    Counterexample,
    Intractable,
    Proven,
    _OptConfig,
    always,
    never,
    reachable_states,
)

BLACKBOX = _OptConfig(abstract_timers=True)


def _replay(program: Program, trace) -> PLC:
    plc = PLC(program, dt=0.010)
    for step in trace:
        plc.patch(step.inputs)
        for _ in range(step.scans):
            plc.step()
    return plc


def _first_hit(program: Program, seq, tag: str) -> int | None:
    """Stepping truth: the first scan *tag* is on under (inputs, scans) holds."""
    plc = PLC(program, dt=0.010)
    n = 0
    for inputs, scans in seq:
        plc.patch(inputs)
        for _ in range(scans):
            plc.step()
            n += 1
            if plc.current_state.tags.get(tag):
                return n
    return None


def _assert_replays(program: Program, result, tag: str) -> None:
    """Two oracles: the search found it, the concrete PLC reaches it."""
    assert isinstance(result, Counterexample), result
    assert _replay(program, result.trace).current_state.tags[tag] is True


def _assert_unconfirmed(result, timer: str) -> None:
    assert isinstance(result, Intractable), result
    assert result.kind == "timing"
    assert result.reason.startswith("unconfirmed")
    assert timer in result.tags
    assert result.trace
    assert result.diverged_at is not None and 0 <= result.diverged_at < len(result.trace)


# ---------------------------------------------------------------------------
# The false-Proven probes of the S2 review (P1a, P1b, P2): never Proven
# ---------------------------------------------------------------------------


def _hand_rolled_edge():
    """P1b: an edge built from a coil and its previous-scan copy, on the Done crossing."""
    btn = Bool("BbHrBtn", external=True)
    run = Bool("BbHrRun", external=True)
    btn_prev, pulse, done_prev, bad = (
        Bool("BbHrBtnPrev"),
        Bool("BbHrPulse"),
        Bool("BbHrDonePrev"),
        Bool("BbHrBad"),
    )
    timer = Timer.clone("BbHrT")
    with Program(strict=False) as logic:
        with Rung(run):
            on_delay(timer, 500)
        with Rung(btn, ~btn_prev):
            out(pulse)
        with Rung(timer.Done, ~done_prev, pulse):
            latch(bad)
        with Rung(btn):
            out(btn_prev)
        with Rung(timer.Done):
            out(done_prev)
    return logic, bad


def _oneshot_chain():
    """P1a: a one-shot two scans upstream of the ``rise(T.Done)`` reader."""
    btn = Bool("BbOsBtn", external=True)
    run = Bool("BbOsRun", external=True)
    y2, y3, bad = Bool("BbOsY2"), Bool("BbOsY3"), Bool("BbOsBad")
    f = Int("BbOsF")
    timer = Timer.clone("BbOsT")
    with Program(strict=False) as logic:
        with Rung(y2):
            out(y3)
        with Rung(f == 1):
            out(y2)
        with Rung():
            copy(0, f)
        with Rung(btn):
            copy(1, f, oneshot=True)
        with Rung(run):
            on_delay(timer, 500)
        with Rung(y3, rise(timer.Done)):
            latch(bad)
    return logic, bad


def _lap_counter(threshold: int):
    """P2: a counter of a 30 ms self-resetting timer's laps beside a 1000 ms timer."""
    run = Bool("BbLapRun", external=True)
    lap = Timer.clone("BbLapT")
    long = Timer.clone("BbLongT")
    count = Counter.clone("BbLapC")
    bad = Bool("BbLapBad")
    with Program(strict=False) as logic:
        with Rung(run, ~lap.Done):
            on_delay(lap, 30)
        with Rung(run):
            on_delay(long, 1000)
        with Rung(lap.Done):
            count_up(count, 1000).reset(~run)
        with Rung(count.Acc == threshold, ~long.Done):
            latch(bad)
    return logic, bad


class TestReviewProbes:
    def test_hand_rolled_edge_is_found_and_replays(self):
        logic, bad = _hand_rolled_edge()
        assert _first_hit(logic, [({"BbHrRun": True}, 49), ({"BbHrBtn": True}, 3)], bad.name)
        _assert_replays(logic, never(logic, bad, _opt_config=BLACKBOX), bad.name)

    def test_oneshot_chain_is_not_proven(self):
        """Stepping hits it (Btn two scans before the crossing); the search's
        shortest trace starts the timer on the crossing scan, which no fixed
        scan time follows: unconfirmed, never Proven."""
        logic, bad = _oneshot_chain()
        assert _first_hit(logic, [({"BbOsRun": True}, 47), ({"BbOsBtn": True}, 6)], bad.name)
        result = never(logic, bad, _opt_config=BLACKBOX)
        assert not isinstance(result, Proven)
        if isinstance(result, Counterexample):
            _assert_replays(logic, result, bad.name)
        else:
            _assert_unconfirmed(result, "BbOsT_Done")

    @pytest.mark.parametrize("threshold", [10, 18, 24])
    def test_lap_count_before_the_long_timer_replays(self, threshold):
        logic, bad = _lap_counter(threshold)
        assert _first_hit(logic, [({"BbLapRun": True}, 150)], bad.name) is not None
        _assert_replays(logic, never(logic, bad, _opt_config=BLACKBOX), bad.name)

    @pytest.mark.parametrize("threshold", range(11, 27))
    def test_lap_count_boundary_is_never_proven_where_stepping_hits(self, threshold):
        logic, bad = _lap_counter(threshold)
        truth = _first_hit(logic, [({"BbLapRun": True}, 150)], bad.name)
        result = never(logic, bad, _opt_config=BLACKBOX)
        if truth is not None:
            assert not isinstance(result, Proven)
        if isinstance(result, Counterexample):
            _assert_replays(logic, result, bad.name)


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


class TestModel:
    def test_config_turns_the_exact_timing_layer_off(self):
        opt = _OptConfig(abstract_timers=True)
        assert not opt.accumulator_absorption
        assert not opt.hidden_event_jumping
        assert not opt.pending_settlement
        assert "abstract_timers" not in opt.active_optimizations

    def test_kernel_without_an_oracle_never_advances(self):
        en = Bool("BbKEn", external=True)
        timer = Timer.clone("BbKT")
        with Program(strict=False) as logic:
            with Rung(en):
                on_delay(timer, 10)
        compiled = compile_kernel(logic, blockless=True, abstract_timers=True)
        assert "_bb_tick" in compiled.source
        kernel = compiled.create_kernel()
        kernel.tags["BbKEn"] = True
        for _ in range(50):
            compiled.step_fn(kernel.tags, kernel.blocks, kernel.memory, kernel.prev, 0.010)
        assert kernel.tags["BbKT_Acc"] == 0
        assert kernel.tags["BbKT_Done"] is False
        assert "_bb_tick" not in compile_kernel(logic, blockless=True).source

    def test_on_delay_may_finish_on_its_first_enabled_scan(self):
        en = Bool("BbFirstEn", external=True)
        timer = Timer.clone("BbFirstT")
        hit = Bool("BbFirstHit")
        with Program(strict=False) as logic:
            with Rung(en):
                on_delay(timer, 500)
            with Rung(timer.Done):
                latch(hit)
        result = never(logic, hit, _opt_config=BLACKBOX)
        _assert_replays(logic, result, hit.name)

    def test_off_delay_may_drop_on_any_disabled_scan(self):
        en = Bool("BbTofEn", external=True)
        timer = Timer.clone("BbTofT")
        dropped = Bool("BbTofDropped")
        with Program(strict=False) as logic:
            with Rung(en):
                off_delay(timer, 300)
            with Rung(~en, ~timer.Done, timer.Acc > 0):
                latch(dropped)
        _assert_replays(logic, never(logic, dropped, _opt_config=BLACKBOX), dropped.name)

    def test_comparison_on_an_accumulator_lands_between_presets(self):
        en = Bool("BbCmpEn", external=True)
        timer = Timer.clone("BbCmpT")
        warn = Bool("BbCmpWarn")
        with Program(strict=False) as logic:
            with Rung(en):
                on_delay(timer, 1000)
            with Rung(timer.Acc > 300, ~timer.Done):
                latch(warn)
        _assert_replays(logic, never(logic, warn, _opt_config=BLACKBOX), warn.name)

    def test_tag_preset_is_read_not_resolved(self):
        en = Bool("BbTpEn", external=True)
        preset = Int("BbTpPreset", external=True, min=0, max=3000)
        timer = Timer.clone("BbTpT")
        hit = Bool("BbTpHit")
        with Program(strict=False) as logic:
            with Rung(en):
                on_delay(timer, preset)
            with Rung(timer.Done):
                latch(hit)
        result = never(logic, hit, _opt_config=BLACKBOX)
        assert not isinstance(result, Proven)

    def test_flasher_is_an_ordinary_black_box(self):
        """The exact layer refused Out timing internals; here they are timers."""
        en = Bool("BbFlEn", external=True)
        lamp = Bool("BbFlLamp")
        seen_off = Bool("BbFlSeenOff")
        phases = Timer.clone("BbFlT", count=2)
        with Program(strict=False) as logic:
            with Rung(en):
                flasher(lamp, on=(phases[1], 200), off=(phases[2], 200))
            with Rung(en, ~lamp):
                latch(seen_off)
        _assert_replays(logic, never(logic, seen_off, _opt_config=BLACKBOX), seen_off.name)

    def test_jumping_time_drum_flag_is_state(self):
        """The completion flag stays on while a jump re-times steps 1 and 2:
        the flag is held state, not a coil recomputed each scan."""
        from pyrung.core import time_drum

        enable, jump = Bool("BbDrEn", external=True), Bool("BbDrJump", external=True)
        done, step, y3 = Bool("BbDrDone"), Int("BbDrStep"), Bool("BbDrY3")
        with Program(strict=False) as logic:
            with Rung(enable):
                time_drum(
                    outputs=[Bool("BbDrY1"), Bool("BbDrY2"), y3],
                    presets=[200, 300, 500],
                    unit="ms",
                    pattern=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                    current_step=step,
                    accumulator=Int("BbDrAcc"),
                    completion_flag=done,
                ).reset(Bool("BbDrReset", external=True)).jump(jump, step=1)
        result = never(logic, done, step == 3, y3, _opt_config=BLACKBOX)
        assert isinstance(result, Counterexample)
        tags = _replay(logic, result.trace).current_state.tags
        assert (tags["BbDrDone"], tags["BbDrStep"], tags["BbDrY3"]) == (True, 3, True)

    def test_unread_timer_never_branches(self):
        en = Bool("BbUnEn", external=True)
        unread = Timer.clone("BbUnT")
        out_tag = Bool("BbUnY")
        with Program(strict=False) as logic:
            with Rung(en):
                on_delay(unread, 100)
            with Rung(en):
                out(out_tag)
        result = always(logic, ~out_tag, _opt_config=BLACKBOX, _debug=True)
        assert isinstance(result, Counterexample)
        model = result._debug_context.timer_model
        assert "BbUnT_Acc" not in model.branch


class TestSettled:
    def test_settled_skips_states_with_a_timer_still_running(self):
        """``settled=True``: only states where no Done choice is open."""
        en = Bool("BbStEn", external=True)
        timer = Timer.clone("BbStT")
        alarm = Bool("BbStAlarm")
        with Program(strict=False) as logic:
            with Rung(en):
                on_delay(timer, 500)
            with Rung(timer.Done):
                out(alarm)
        # Enabled and not yet done is transient: settled ignores it.
        assert isinstance(never(logic, en, ~alarm, _opt_config=BLACKBOX), Counterexample)
        assert isinstance(never(logic, en, ~alarm, settled=True, _opt_config=BLACKBOX), Proven)


class TestUnconfirmed:
    def test_exact_timing_idiom_is_unconfirmed_not_proven(self):
        """Two timers started together: the 30 ms one cannot finish after the
        1000 ms one on the PLC, but nothing about time is certain to the search."""
        en = Bool("BbRaceEn", external=True)
        short = Timer.clone("BbRaceShort")
        long = Timer.clone("BbRaceLong")
        bad = Bool("BbRaceBad")
        with Program(strict=False) as logic:
            with Rung(en):
                on_delay(short, 30)
            with Rung(en):
                on_delay(long, 1000)
            with Rung(long.Done, ~short.Done):
                latch(bad)
        result = never(logic, bad, _opt_config=BLACKBOX)
        _assert_unconfirmed(result, "BbRaceLong_Done")
        assert any("run_for" in hint for hint in result.hints)
        assert any(step.timers for step in result.trace)
        assert result.reason.isascii() and all(h.isascii() for h in result.hints)

    def test_counterexample_trace_is_concrete(self):
        en = Bool("BbConcEn", external=True)
        timer = Timer.clone("BbConcT")
        hit = Bool("BbConcHit")
        with Program(strict=False) as logic:
            with Rung(en):
                on_delay(timer, 500)
            with Rung(timer.Done):
                latch(hit)
        result = never(logic, hit, _opt_config=BLACKBOX)
        assert isinstance(result, Counterexample)
        assert not any(step.timers for step in result.trace)
        assert sum(step.scans for step in result.trace) == 50
        _assert_replays(logic, result, hit.name)


class TestCounters:
    def test_unreset_counter_key_stops_at_its_preset(self):
        """Counters count concretely; past the preset the key holds the preset."""
        pulse = Bool("BbCtPulse", external=True)
        clear = Bool("BbCtClear", external=True)
        count = Counter.clone("BbCt")
        done_seen = Bool("BbCtDone")
        with Program(strict=False) as logic:
            with Rung(rise(pulse)):
                count_up(count, 3).reset(clear)
            with Rung(count.Done):
                out(done_seen)
        result = always(logic, ~done_seen, _opt_config=BLACKBOX, _debug=True)
        _assert_replays(logic, result, done_seen.name)
        assert result._debug_context.count_clamps
        states = reachable_states(logic, project=["BbCtDone"], _opt_config=BLACKBOX)
        assert isinstance(states, frozenset)
        assert frozenset({("BbCtDone", True)}) in states


class TestKnownHoles:
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "classify: an OTE coil read by a rung above its writer is classified "
            "combinational, so a delay line Y1 -> Y2 -> Y3 leaves the key; the "
            "exact-timing layer hid it on the timer variant"
        ),
    )
    def test_delay_line_of_coils_is_state(self):
        btn = Bool("BbDlBtn", external=True)
        c = Bool("BbDlC", external=True)
        y1, y2, y3, bad = Bool("BbDlY1"), Bool("BbDlY2"), Bool("BbDlY3"), Bool("BbDlBad")
        with Program(strict=False) as logic:
            with Rung(y2):
                out(y3)
            with Rung(y1):
                out(y2)
            with Rung(rise(btn)):
                out(y1)
            with Rung(y3, rise(c)):
                latch(bad)
        seq = [({}, 1), ({"BbDlBtn": True}, 2), ({"BbDlC": True}, 1)]
        assert _first_hit(logic, seq, bad.name) is not None
        assert not isinstance(never(logic, bad, _opt_config=BLACKBOX), Proven)
