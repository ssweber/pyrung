"""A clock read through a tag the plateau guard might not see move.

A rung reading a clock ends the window at each of its edges, unless the clock only
drives an *indicator*: an ``out()`` bit on rungs of plain contacts, whose readers
all sit below its writers and write nothing but indicators.  The guard does not
watch an indicator and each landing rebuilds it.  Anything else a clock-driven tag
reaches keeps the clock a bound: a second clock on a rung, a comparison, a latch, a
copy, a subroutine gated by it.  Blink indicators are common and cheap, so they stay in the loop.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from pyrung import (
    Block,
    Bool,
    Counter,
    Int,
    Or,
    Program,
    Rung,
    TagType,
    Timer,
    branch,
    calc,
    call,
    copy,
    count_up,
    forloop,
    latch,
    on_delay,
    out,
    reset,
    return_early,
    rise,
    run_function,
    subroutine,
)
from pyrung.core.runner import PLC
from pyrung.core.system_points import system
from tests.core.scan_loop_counts import NEVER, ScansOutsideLoop

pytestmark = pytest.mark.usefixtures("loop_from_first_scan")

clk100 = system.sys.clock_100ms
clk1s = system.sys.clock_1s

F, G, H, Lamp, Hit, X, Mode, Clear = (
    Bool("F"),
    Bool("G"),
    Bool("H"),
    Bool("Lamp"),
    Bool("Hit"),
    Bool("X"),
    Bool("Mode"),
    Bool("Clear"),
)
N, Th, V, W, Z, Ptr, Since = (
    Int("N"),
    Int("Th"),
    Int("V"),
    Int("W"),
    Int("Z"),
    Int("Ptr"),
    Int("Since"),
)
T, T2 = Timer.clone("T"), Timer.clone("T2")
C = Counter.clone("C")
Bank = Block("Bank", TagType.INT, 1, 8)

Body = Callable[[], None]


def _program(body: Body) -> Callable[[], Program]:
    def build() -> Program:
        with Program(strict=False) as prog:
            body()
            # Counts the scans since the hit: a latch alone hides a late landing.
            with Rung(Hit):
                calc(Since + 1, Since)
        return prog

    return build


def _state(plc: PLC) -> dict[str, Any]:
    tags = {k: v for k, v in plc.state.tags.items() if not k.startswith("sys.scan")}
    memory = {f"mem:{k}": v for k, v in plc.state.memory.items() if "_frac" not in k}
    return {k: v for k, v in {**tags, **memory}.items() if v}


def _plc(body: Body, patch: dict[str, Any] | None) -> PLC:
    plc = PLC(_program(body)(), dt=0.01)
    if patch:
        plc.patch(patch)
    plc.step()
    return plc


def _assert_exact(body: Body, scans: int = 1200, patch: dict[str, Any] | None = None) -> None:
    """The loop and a replay equal stepping, at the end and at scans a replay reaches."""
    stepped = _plc(body, patch)
    expected: dict[int, dict[str, Any]] = {}
    for _ in range(scans):
        stepped.step()
        expected[stepped.state.scan_id] = _state(stepped)

    folded = _plc(body, patch)
    folded.run_until(NEVER, max_cycles=scans, fold=True)
    assert folded.state.scan_id == stepped.state.scan_id
    assert _state(folded) == expected[folded.state.scan_id]

    for target in (stepped.state.scan_id // 2, (stepped.state.scan_id * 3) // 4):
        seek = stepped.replay_to(target)
        assert _state(seek) == expected[target]


def _kernel_scans(body: Body, scans: int = 1200) -> int:
    plc = _plc(body, None)
    counter = ScansOutsideLoop(plc)
    plc.run_until(NEVER, max_cycles=scans, fold=True)
    return counter["n"]


def _assert_lands_as_stepped(body: Body, *conditions: Any) -> None:
    """``run_until`` stops on one scan with one state, folded or stepped, for each."""
    for condition in conditions:
        folded, stepped = _plc(body, None), _plc(body, None)
        folded.run_until(condition, max_cycles=2000, fold=True)
        stepped.run_until(condition, max_cycles=2000, fold=False)
        assert folded.state.scan_id == stepped.state.scan_id
        assert _state(folded) == _state(stepped)


def _timer() -> None:
    with Rung():
        on_delay(T, 30_000)


def _blink() -> None:
    with Rung(clk100):
        out(F)


def _blinks() -> None:
    _blink()
    with Rung(clk1s):
        out(G)


def _crossing() -> None:
    _blink()
    _timer()
    with Rung(F, T.Acc >= 205):
        latch(Hit)


def _crossing_above_a_write() -> None:
    _blink()
    _timer()
    with Rung(F, T.Acc >= Th):
        latch(Hit)
    with Rung():
        copy(205, Th)


def _two_blinks(gated: Body) -> Body:
    def body() -> None:
        _blinks()
        with Rung(F, G):
            gated()
        _timer()

    return body


def _threshold_below_two_blinks() -> None:
    _blinks()
    with Rung(F, G):
        copy(200, Th)
    _timer()
    with Rung(T.Acc >= Th):
        latch(Hit)


def _chain_of_two() -> None:
    _blink()
    with Rung(F):
        out(H)
    _timer()
    with Rung(H, T.Acc >= 205):
        latch(Hit)


def _pulse_and_a_blink() -> None:
    with Rung(rise(clk100)):
        out(H)
    with Rung(clk1s):
        out(G)
    with Rung(H, G):
        calc(N + 1, N)


def _chain_of_two_clocks() -> None:
    _blinks()
    with Rung(F, G):
        out(H)
    with Rung(H):
        out(Lamp)
    with Rung(Lamp):
        calc(N + 1, N)
    _timer()


def _done_mirror() -> None:
    _blinks()
    with Rung():
        on_delay(T, 207)
    with Rung(T.Done):
        out(H)
    with Rung(F, G, H):
        latch(Hit)


def _copied_blink() -> None:
    _blink()
    with Rung():
        copy(F, V)
    with Rung(V == 1, T.Acc >= 205):
        latch(Hit)
    _timer()


def _stepped_value() -> None:
    _blink()
    with Rung(F):
        copy(1, V)
    with Rung(~F):
        copy(0, V)
    with Rung(V == 1, T.Acc >= 205):
        latch(Hit)
    _timer()


def _clock_on_the_call() -> None:
    with Rung(clk100):
        call("s")
    _timer()
    with subroutine("s"):
        with Rung(T.Acc >= 205):
            latch(Hit)


def _blink_on_the_call() -> None:
    _blink()
    with Rung(F):
        call("s")
    _timer()
    with subroutine("s"):
        with Rung(T.Acc >= 205):
            latch(Hit)


def _second_blink_in_the_sub() -> None:
    _blinks()
    with Rung(F):
        call("s")
    _timer()
    with subroutine("s"):
        with Rung(G):
            latch(Hit)


def _blink_written_in_a_sub() -> None:
    with Rung():
        call("s")
    with Rung(clk1s):
        out(G)
    _timer()
    with Rung(F, G):
        calc(N + 1, N)
    with subroutine("s"):
        with Rung(clk100):
            out(F)


def _sampled_by_a_blink_on_the_call() -> None:
    # H only updates while F runs the subroutine: it depends on the order of edges.
    _blinks()
    with Rung(F):
        call("s")
    _timer()
    with Rung(H):
        calc(N + 1, N)
    with subroutine("s"):
        with Rung(G):
            out(H)


def _blink_return_early() -> None:
    _blinks()
    with Rung():
        call("s")
    _timer()
    with subroutine("s"):
        with Rung(F):
            return_early()
        with Rung(G):
            calc(N + 1, N)


def _branch_comparison() -> None:
    _blink()
    _timer()
    with Rung(F):
        with branch(T.Acc >= 205):
            latch(Hit)


def _branch_second_blink() -> None:
    _blinks()
    with Rung(F):
        with branch(G):
            latch(Hit)
    _timer()


def _blink_written_in_a_branch() -> None:
    with Rung():
        with branch(clk100):
            out(F)
    _timer()
    with Rung(F, T.Acc >= 205):
        latch(Hit)


def _loop_body_second_blink() -> None:
    _blinks()
    with Rung(F):
        with forloop(2):
            with Rung(G):
                out(H)
    with Rung(H):
        calc(N + 1, N)
    _timer()


def _two_clocks_latch_and_reset(reader: bool) -> Body:
    def body() -> None:
        with Rung(clk100):
            latch(X)
        with Rung(clk1s):
            reset(X)
        if reader:
            with Rung(X):
                calc(N + 1, N)
        _timer()

    return body


def _two_blinks_latch_and_reset() -> None:
    _blinks()
    with Rung(F):
        latch(X)
    with Rung(G):
        reset(X)
    with Rung(X):
        calc(N + 1, N)
    _timer()


def _two_clocks_copied() -> None:
    with Rung(clk100):
        copy(1, V)
    with Rung(clk1s):
        copy(0, V)
    with Rung(V == 1):
        out(H)
    with Rung(H):
        calc(N + 1, N)
    _timer()


def _standing_value_in_three_steps() -> None:
    # Settles only over several phases, with no reader to show it.
    with Rung(clk100):
        copy(7, V)
    with Rung(~clk100):
        copy(V, W)
    with Rung(clk100):
        copy(W, Z)
    _timer()


def _between_out_comparison() -> None:
    _blink()
    _timer()
    with Rung(F, T.Acc >= 205):
        latch(Hit)
    with Rung(Mode):
        out(F)


def _between_out_second_clock(gated: Body) -> Body:
    def body() -> None:
        _blink()
        with Rung(F, clk1s):
            gated()
        with Rung(Mode):
            out(F)
        _timer()

    return body


def _between_copy_comparison() -> None:
    with Rung(clk100):
        copy(1, V)
    _timer()
    with Rung(V == 1, T.Acc >= 205):
        latch(Hit)
    with Rung():
        copy(0, V)


def _between_into_a_rebuilt_bit() -> None:
    with Rung(clk100):
        copy(1, V)
    with Rung(V == 1):
        out(H)
    with Rung():
        copy(0, V)
    _timer()
    with Rung(H, T.Acc >= 205):
        latch(Hit)


def _between_indirect_write() -> None:
    with Rung(clk100):
        copy(1, Bank[Ptr])
    _timer()
    with Rung(Bank[3] == 1, T.Acc >= 205):
        latch(Hit)
    with Rung():
        copy(0, Bank[3])


def _count() -> None:
    count_up(C, 30_000).reset(Clear)


def _retentive() -> None:
    on_delay(T2, 30_000).reset(Clear)


class TestAClockThroughATag:
    @pytest.mark.parametrize(
        "body,patch",
        [
            pytest.param(_crossing, None, id="comparison-beside-a-blink"),
            pytest.param(_crossing_above_a_write, None, id="comparison-above-its-operand-write"),
            pytest.param(_two_blinks(lambda: latch(Hit)), None, id="two-blinks-latch"),
            pytest.param(_two_blinks(lambda: calc(N + 1, N)), None, id="two-blinks-calc"),
            pytest.param(_two_blinks(_count), None, id="two-blinks-count"),
            pytest.param(_two_blinks(_retentive), None, id="two-blinks-retentive"),
            pytest.param(
                _threshold_below_two_blinks, {"Th": 5000}, id="two-blinks-move-a-threshold"
            ),
            pytest.param(_chain_of_two, None, id="chain-to-a-comparison"),
            pytest.param(_chain_of_two_clocks, None, id="chain-from-two-blinks"),
            pytest.param(_pulse_and_a_blink, None, id="rise-of-a-clock-and-a-blink"),
            pytest.param(_done_mirror, None, id="done-mirror"),
            pytest.param(_copied_blink, None, id="copied-blink"),
            pytest.param(_stepped_value, None, id="value-stepped-by-a-blink"),
        ],
    )
    def test_on_the_rung(self, body: Body, patch: dict[str, Any] | None) -> None:
        _assert_exact(body, patch=patch)

    def test_with_one_blink_forced(self) -> None:
        def body() -> None:
            _two_blinks(lambda: calc(N + 1, N))()

        def run(fold: bool) -> PLC:
            plc = _plc(body, None)
            plc.force(F, True)
            plc.run_until(NEVER, max_cycles=1200, fold=fold)
            return plc

        assert _state(run(fold=True)) == _state(run(fold=False))

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(_clock_on_the_call, id="clock-on-the-call"),
            pytest.param(_blink_on_the_call, id="blink-on-the-call"),
            pytest.param(_second_blink_in_the_sub, id="second-blink-in-the-sub"),
            pytest.param(_blink_written_in_a_sub, id="blink-written-in-a-sub"),
            pytest.param(_sampled_by_a_blink_on_the_call, id="sampled-by-a-blink-on-the-call"),
            pytest.param(_blink_return_early, id="blink-return-early"),
            pytest.param(_branch_comparison, id="branch-comparison"),
            pytest.param(_branch_second_blink, id="branch-second-blink"),
            pytest.param(_blink_written_in_a_branch, id="blink-written-in-a-branch"),
            pytest.param(_loop_body_second_blink, id="loop-body-second-blink"),
        ],
    )
    def test_through_a_subroutine_or_a_branch(self, body: Body) -> None:
        _assert_exact(body)

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(_two_clocks_latch_and_reset(True), id="latch-and-reset"),
            pytest.param(_two_clocks_latch_and_reset(False), id="latch-and-reset-unread"),
            pytest.param(_two_blinks_latch_and_reset, id="blinks-latch-and-reset"),
            pytest.param(_two_clocks_copied, id="two-copies"),
            pytest.param(_standing_value_in_three_steps, id="three-steps"),
        ],
    )
    def test_a_value_left_standing(self, body: Body) -> None:
        _assert_exact(body)

    @pytest.mark.parametrize(
        "body,patch",
        [
            pytest.param(_between_out_comparison, None, id="out-comparison"),
            pytest.param(_between_out_second_clock(_retentive), None, id="out-retentive"),
            pytest.param(_between_out_second_clock(lambda: calc(N + 1, N)), None, id="out-calc"),
            pytest.param(_between_copy_comparison, None, id="copy-comparison"),
            pytest.param(_between_into_a_rebuilt_bit, None, id="into-a-rebuilt-bit"),
            pytest.param(_between_indirect_write, {"Ptr": 3}, id="indirect-write"),
        ],
    )
    def test_read_between_two_writes(self, body: Body, patch: dict[str, Any] | None) -> None:
        _assert_exact(body, patch=patch)


class TestTheWindowEndsAtTheEdge:
    """Not an indicator: the clock bounds the window, so a stop lands on its scan."""

    def test_a_comparison_beside_a_blink(self) -> None:
        _assert_lands_as_stepped(_crossing, Hit, F, ~F)
        _assert_exact(_crossing, 40)

    def test_two_blinks_on_a_rung(self) -> None:
        body = _two_blinks(lambda: calc(N + 1, N))
        _assert_lands_as_stepped(body, N >= 1, N >= 7, N >= 30)

    def test_a_standing_value(self) -> None:
        body = _two_clocks_latch_and_reset(False)
        _assert_lands_as_stepped(body, X, ~X)
        for scans in (4, 5, 6, 49, 50, 51):
            _assert_exact(body, scans)

    def test_a_rebuilt_bit_that_reaches_a_comparison(self) -> None:
        _assert_lands_as_stepped(_chain_of_two, H, ~H, Hit)

    def test_a_clock_gating_the_call(self) -> None:
        _assert_lands_as_stepped(_sampled_by_a_blink_on_the_call, H, ~H, N >= 5)


def _indicator() -> None:
    _blink()
    _timer()


def _indicator_and_lamp() -> None:
    _blink()
    with Rung(F):
        out(Lamp)
    _timer()


def _indicator_on_a_stored_gate() -> None:
    _blink()
    with Rung(F, Mode):
        out(Lamp)
    _timer()


def _indicator_of_two_blinks() -> None:
    _blinks()
    with Rung(F, G):
        out(Lamp)
    _timer()


def _indicator_chain_of_two_blinks() -> None:
    _blinks()
    with Rung(F, G):
        out(H)
    with Rung(H):
        out(Lamp)
    _timer()


def _indicator_latched_once() -> None:
    _blink()
    with Rung(F):
        latch(H)
    _timer()


def _indicator_in_a_subroutine() -> None:
    with Rung():
        call("s")
    _timer()
    with Rung(F):
        out(Lamp)
    with subroutine("s"):
        with Rung(clk100):
            out(F)


def _indicator_written_around() -> None:
    _blink()
    with Rung(F):
        out(Lamp)
    with Rung(Mode):
        out(F)
    _timer()


def _indicator_of_a_rebuilt_value() -> None:
    with Rung():
        copy(0, V)
    with Rung(clk100):
        copy(1, V)
    with Rung(clk1s):
        copy(2, V)
    with Rung(V == 1):
        out(Lamp)
    _timer()


class TestAnIndicatorStaysInTheLoop:
    """A clock-driven bit that gates nothing else moving: the common case, and exact."""

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(_indicator, id="blink"),
            pytest.param(_indicator_and_lamp, id="lamp"),
            pytest.param(_indicator_on_a_stored_gate, id="stored-gate"),
            pytest.param(_indicator_of_two_blinks, id="two-blinks"),
            pytest.param(_indicator_chain_of_two_blinks, id="chain-of-two-blinks"),
            pytest.param(_indicator_in_a_subroutine, id="in-a-subroutine"),
        ],
    )
    def test_runs_exactly(self, body: Body) -> None:
        # A real scan at each edge of the 100 ms clock: 1 scan in 5.
        assert _kernel_scans(body) <= 1200 // 4
        _assert_exact(body)

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(_indicator, id="blink"),
            pytest.param(_indicator_and_lamp, id="lamp"),
            pytest.param(_indicator_in_a_subroutine, id="in-a-subroutine"),
        ],
    )
    @pytest.mark.parametrize("scans", [3, 4, 5, 6, 7, 53, 54, 55, 56])
    def test_every_landing_rebuilds_it(self, body: Body, scans: int) -> None:
        _assert_exact(body, scans)

    def test_into_a_range_of_bits(self) -> None:
        assert _kernel_scans(_blink_into_a_range) <= 1200 // 4
        _assert_exact(_blink_into_a_range)


def _one_shot_blink() -> None:
    with Rung(clk100):
        out(F, oneshot=True)
    with Rung(F):
        out(Lamp)
    _timer()


Bits = Block("Bits", TagType.BOOL, 1, 4)


def _blink_read_through_a_pointer() -> None:
    with Rung(clk100):
        out(Bits[2])
    _timer()
    with Rung(Bits[Ptr] == 1, T.Acc >= 205):
        latch(Hit)


def _blink_into_a_range() -> None:
    with Rung(clk100):
        out(Bits.select(1, 2))
    with Rung(Bits[2]):
        out(Lamp)
    _timer()


def _indicator_read_above_its_writer() -> None:
    # The reader sees last scan's blink: one scan late.
    with Rung(F):
        out(Lamp)
    _blink()
    _timer()


def _indicator_feeds_latch() -> None:
    A, B = Bool("A"), Bool("B")
    with Rung(clk100):
        out(A)
    with Rung(A):
        out(B)
    with Rung(B):
        latch(Hit)
    _timer()


def _blink_in_an_uncalled_subroutine() -> None:
    # A writer no main rung reaches has no position to sit above its reader.
    with subroutine("Never"):
        with Rung(clk100):
            out(F)
    with Rung(F):
        out(Lamp)
    _timer()


def _seal_in_on_a_clock() -> None:
    # The same rung reads and writes it, so it is no function of its scan.
    with Rung(Or(clk100, F)):
        out(F)
    with Rung(F):
        out(Lamp)
    _timer()


def _branch_lamp_above_its_writer() -> None:
    # H reads the F of the scan before.
    with Rung(Mode):
        with branch(F):
            out(H)
        with branch(clk100):
            out(F)
    with Rung(H):
        out(Lamp)
    _timer()


def _function_over_a_gated_blink(calls: list[int]) -> Body:
    # The function moves the bit every scan; nothing but the plateau guard can see it.
    def alternate() -> dict[str, bool]:
        calls.append(1)
        return {"v": len(calls) % 2 == 0}

    def body() -> None:
        _timer()
        with Rung(G):
            call("Blink")
        with subroutine("Blink"):
            with Rung(clk1s):
                out(F)
        with Rung():
            run_function(alternate, outs={"v": F})
        with Rung(F):
            out(Lamp)

    return body


def _clock_over_a_counting_branch() -> None:
    _timer()
    with Rung(clk100):
        out(F)
        with branch(G):
            calc(N + 1, N)


def _blink_in_a_branch_read_below() -> None:
    _timer()
    with Rung(G):
        with branch(clk100):
            out(F)
    with Rung(F):
        out(Lamp)


def _branch_reads_a_sibling_coil() -> None:
    _timer()
    with Rung(G):
        with branch(clk100):
            out(F)
        with branch(F):
            out(H)
    with Rung(H):
        calc(N + 1, N)


def _branch_above_its_writer() -> None:
    _timer()
    with Rung(G):
        with branch(F):
            out(H)
        with branch(clk100):
            out(F)
    with Rung(H):
        calc(N + 1, N)


def _edge_in_a_branch_under_a_clock() -> None:
    _timer()
    with Rung(clk100):
        out(F)
        with branch(rise(G)):
            out(H)


def _one_shot_in_a_branch() -> None:
    _timer()
    with Rung(clk100):
        with branch(G):
            out(F, oneshot=True)
    with Rung(F):
        calc(N + 1, N)


def _contact_on_an_accumulator() -> None:
    _timer()
    with Rung(T.Acc, clk100):
        out(F)
    with Rung(F):
        out(Lamp)


def _contact_on_a_wrapping_value() -> None:
    _timer()
    with Rung():
        calc((V + 1) % 7, V)
    with Rung(V, clk100):
        out(F)
    with Rung(F):
        out(Lamp)


def _contact_on_a_rebuilt_view() -> None:
    _timer()
    with Rung():
        calc(T.Acc // 100, V)
    with Rung(V, clk100):
        out(F)
    with Rung(F):
        out(Lamp)


def _contact_on_first_scan() -> None:
    _timer()
    with Rung(system.sys.first_scan, clk100):
        out(F)
    with Rung(F):
        calc(N + 1, N)


def _contact_on_a_fault_flag() -> None:
    _timer()
    with Rung():
        calc(T.Acc + 30000, V)
    with Rung(system.fault.out_of_range, clk100):
        out(F)
    with Rung(F):
        calc(N + 1, N)


def _contact_on_a_comparison_result() -> None:
    _timer()
    with Rung(clk100, X):
        out(F)
    with Rung(F):
        out(Lamp)
    with Rung(T.Acc >= 2000):
        out(X)


class TestWhatIsNotAnIndicator:
    """Each stays in the loop less than an indicator does, or not at all, and stays exact."""

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(_indicator_latched_once, id="latched-once"),
            pytest.param(_indicator_written_around, id="written-around"),
            pytest.param(_indicator_of_a_rebuilt_value, id="rebuilt-value"),
            pytest.param(_one_shot_blink, id="one-shot"),
            pytest.param(_indicator_read_above_its_writer, id="read-above-its-writer"),
            pytest.param(_indicator_feeds_latch, id="feeds-a-latch"),
            pytest.param(_blink_in_an_uncalled_subroutine, id="uncalled-writer"),
            pytest.param(_seal_in_on_a_clock, id="seal-in"),
        ],
    )
    def test_stays_exact_with_the_clock_as_a_bound(self, body: Body) -> None:
        _assert_exact(body)

    def test_a_branch_read_above_its_writer(self) -> None:
        _assert_exact(_branch_lamp_above_its_writer, patch={"Mode": True})

    def test_a_second_writer_stays_in_the_guard(self) -> None:
        # Its moves are not a function of the clock, and a probe scan shows them.
        def run(fold: bool) -> tuple[int, dict[str, Any]]:
            calls: list[int] = []
            plc = _plc(_function_over_a_gated_blink(calls), None)
            # A monitor on what holds changes nothing here, and tells --fold-check to
            # leave this run alone: a stepped twin would call the same function.
            plc.monitor(G, lambda _cur, _prev: None)
            plc.run_until(NEVER, max_cycles=700, fold=fold)
            return len(calls), _state(plc)

        assert run(True) == run(False)

    @pytest.mark.parametrize("ptr", [1, 2])
    def test_read_through_a_pointer(self, ptr: int) -> None:
        _assert_exact(_blink_read_through_a_pointer, patch={"Ptr": ptr})

    def test_a_latch_on_a_chain_lands_on_its_scan(self) -> None:
        _assert_lands_as_stepped(_indicator_feeds_latch, Hit)


class TestAPlainRung:
    """Contacts over coils are a function of their scan, whatever the contacts read."""

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(_clock_over_a_counting_branch, id="clock-over-a-counting-branch"),
            pytest.param(_blink_in_a_branch_read_below, id="blink-in-a-branch"),
            pytest.param(_branch_reads_a_sibling_coil, id="reads-a-sibling-coil"),
            pytest.param(_branch_above_its_writer, id="above-its-writer"),
            pytest.param(_edge_in_a_branch_under_a_clock, id="edge-under-a-clock"),
            pytest.param(_one_shot_in_a_branch, id="one-shot"),
        ],
    )
    def test_in_a_branch(self, body: Body) -> None:
        _assert_exact(body, patch={"G": True})
        _assert_exact(body)

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(_contact_on_an_accumulator, id="accumulator"),
            pytest.param(_contact_on_a_wrapping_value, id="wrapping-value"),
            pytest.param(_contact_on_a_rebuilt_view, id="rebuilt-view"),
            pytest.param(_contact_on_first_scan, id="first-scan"),
            pytest.param(_contact_on_a_fault_flag, id="fault-flag"),
            pytest.param(_contact_on_a_comparison_result, id="comparison-result"),
        ],
    )
    def test_with_a_contact_on_what_moves(self, body: Body) -> None:
        _assert_exact(body)


def _compare(threshold: int, clock: Any) -> Body:
    def body() -> None:
        with Rung(clock):
            out(F)
        _timer()
        with Rung(F, T.Acc >= threshold):
            latch(Hit)

    return body


def _one_shot_window(threshold: int, clock: Any) -> Body:
    # True for only a few edges: a window over them misses it for good.
    def body() -> None:
        with Rung(clock):
            out(F)
        _timer()
        with Rung(F, T.Acc >= threshold, T.Acc < threshold + 120):
            latch(Hit)

    return body


def _two_gates_on_a_comparison(threshold: int) -> Body:
    def body() -> None:
        _blinks()
        _timer()
        with Rung(F, G, T.Acc >= threshold):
            latch(Hit)

    return body


def _two_clocks_on_a_rung() -> None:
    with Rung(clk100, clk1s):
        calc(N + 1, N)
    _timer()


def _constant_then_blink() -> None:
    with Rung(clk100):
        copy(7, V)
    with Rung(clk1s):
        out(F)
    with Rung(F, V == 7):
        calc(N + 1, N)


def _seed_282_shape(preset: int) -> Body:
    # From the fuzzer: a chain off a blink into a call and a latch that holds itself.
    def body() -> None:
        B0, B1, B3, B4, M1 = Bool("B0"), Bool("B1"), Bool("B3"), Bool("B4"), Bool("M1")
        with Rung():
            on_delay(T, preset)
        with Rung(clk100):
            out(B0)
        with Rung(B0):
            out(B1)
        with Rung(B1):
            call("s")
        with Rung(B3, B1):
            latch(B1)
        with subroutine("s"):
            with Rung(B1, M1, T.Done):
                reset(B4)
            with Rung(B1, B1):
                latch(B3)

    return body


class TestBlinkRows:
    """The rows a frozen blink bit got wrong: a window held it still past its edges."""

    @pytest.mark.parametrize("clock", [clk100, clk1s], ids=["100ms", "1s"])
    @pytest.mark.parametrize("threshold", [105, 205, 242, 1201, 3940, 6951])
    def test_a_blink_and_a_comparison(self, clock: Any, threshold: int) -> None:
        _assert_exact(_compare(threshold, clock), 800)

    @pytest.mark.parametrize("clock", [clk100, clk1s], ids=["100ms", "1s"])
    @pytest.mark.parametrize("threshold", [105, 379, 1201, 3940, 6951])
    def test_a_blink_and_a_one_shot_window(self, clock: Any, threshold: int) -> None:
        _assert_exact(_one_shot_window(threshold, clock), 800)

    @pytest.mark.parametrize("threshold", [105, 242, 1201, 3940, 6951])
    def test_two_blinks_and_a_comparison(self, threshold: int) -> None:
        _assert_exact(_two_gates_on_a_comparison(threshold), 800)

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(_two_blinks(lambda: calc(N + 1, N)), id="two-blinks-calc"),
            pytest.param(_two_blinks(lambda: latch(Hit)), id="two-blinks-latch"),
            pytest.param(_two_clocks_on_a_rung, id="two-clocks-on-a-rung"),
            pytest.param(_constant_then_blink, id="constant-then-blink"),
            pytest.param(_indicator_feeds_latch, id="indicator-feeds-latch"),
        ],
    )
    def test_the_rest(self, body: Body) -> None:
        _assert_exact(body, 800)

    @pytest.mark.parametrize("preset", [300, 1000, 30_000])
    @pytest.mark.parametrize("patch", [None, {"M1": True}], ids=["m1-off", "m1-on"])
    def test_seed_282_shape(self, preset: int, patch: dict[str, Any] | None) -> None:
        _assert_exact(_seed_282_shape(preset), 900, patch=patch)
