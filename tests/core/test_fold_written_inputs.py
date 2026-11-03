"""A preset or comparison operand written around its reader.

A rung reading such a tag between two writes reads a value no scan boundary shows.  The same
rung with a single write, on either side of the reader, reads what the boundary shows.  Every
program here runs in the loop and stepped, and the landing and a replay must equal stepping.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest

from pyrung import (
    Block,
    Bool,
    Dint,
    Int,
    Program,
    Rung,
    TagType,
    Timer,
    branch,
    calc,
    call,
    copy,
    forloop,
    latch,
    off_delay,
    on_delay,
    out,
    subroutine,
    time_drum,
    time_event_drum,
)
from pyrung.core.analysis.pilot.overlay import PilotRung, fork_with_pilot_rungs
from pyrung.core.coast_run import coast_until
from pyrung.core.runner import PLC
from pyrung.core.system_points import system
from tests.core.scan_loop_counts import NEVER, ScansOutsideLoop

pytestmark = pytest.mark.usefixtures("loop_from_first_scan")

P = Int("P")
Hit = Bool("Hit")
Mode = Bool("Mode")
T = Timer.clone("T")
Ptr = Int("Ptr")
Bank = Block("Bank", TagType.INT, 1, 8)
Step, DAcc, Flag, O1, O2 = Int("Step"), Dint("DAcc"), Bool("Flag"), Bool("O1"), Bool("O2")

Body = Callable[[], None]


def _program(body: Body) -> Callable[[], Program]:
    def build() -> Program:
        with Program(strict=False) as prog:
            body()
            # Counts the scans since the hit: a latch alone hides a late landing.
            with Rung(Hit):
                calc(Int("Since") + 1, Int("Since"))
        return prog

    return build


def _state(plc: PLC) -> dict[str, Any]:
    """Every tag and memory slot off its default: a block's untouched slots come and go."""
    tags = {k: v for k, v in plc.state.tags.items() if not k.startswith("sys.scan")}
    memory = {f"mem:{k}": v for k, v in plc.state.memory.items() if "_frac" not in k}
    return {k: v for k, v in {**tags, **memory}.items() if v}


def _run(
    body: Body,
    fold: bool,
    scans: int,
    *,
    dt: float = 0.01,
    patch: dict[str, Any] | None = None,
    force: tuple[str, Any] | None = None,
    then: dict[str, Any] | None = None,
) -> PLC:
    plc = PLC(_program(body)(), dt=dt)
    if patch:
        plc.patch(patch)
    if force:
        plc.force(*force)
    plc.step()
    if then:
        plc.patch(then)
    plc.run_until(NEVER, max_cycles=scans, fold=fold)
    return plc


def _assert_exact(body: Body, scans: int = 700, **kwargs: Any) -> None:
    """The loop, and a replay over a stepped recording, both equal stepping."""
    folded = _run(body, True, scans, **kwargs)
    stepped = _run(body, False, scans, **kwargs)
    assert folded.state.scan_id == stepped.state.scan_id
    assert _state(folded) == _state(stepped)
    for target in (stepped.state.scan_id // 2, stepped.state.scan_id - 1):
        assert _state(stepped.replay_to(target)) == _state(_run(body, False, target - 1, **kwargs))


def _kernel_scans(
    body: Body, scans: int, *, dt: float = 0.01, patch: dict[str, Any] | None = None
) -> int:
    plc = PLC(_program(body)(), dt=dt)
    if patch:
        plc.patch(patch)
    plc.step()
    counter = ScansOutsideLoop(plc)
    plc.run_until(NEVER, max_cycles=scans, fold=True)
    assert plc.state.scan_id >= scans
    return counter["n"]


def _hit_when_done() -> None:
    with Rung(T.Done):
        latch(Hit)


def _around(reader: Body, before: int = 200, after: int = 5000) -> Body:
    def body() -> None:
        with Rung():
            copy(before, P)
        reader()
        with Rung():
            copy(after, P)

    return body


def _timer_reader() -> None:
    with Rung():
        on_delay(T, P)
    _hit_when_done()


def _off_delay_reader() -> None:
    with Rung(Mode):
        off_delay(T, P)
    with Rung(~T.Done):
        latch(Hit)


def _comparison_reader() -> None:
    with Rung():
        on_delay(T, 30_000)
    with Rung(T.Acc >= P):
        latch(Hit)


def _inverted_comparison_reader() -> None:
    with Rung():
        on_delay(T, 30_000)
    with Rung(P <= T.Acc):
        latch(Hit)


def _time_drum(preset: Any = P) -> None:
    with Rung(Bool("En")):
        time_drum(
            outputs=[O1, O2],
            pattern=[[1, 0], [0, 1]],
            presets=[preset, 5000],
            unit="ms",
            current_step=Step,
            accumulator=DAcc,
            completion_flag=Flag,
        ).reset(Bool("R"))
    with Rung(O2):
        calc(Int("N") + 1, Int("N"))


def _time_event_drum() -> None:
    with Rung(Bool("En")):
        time_event_drum(
            outputs=[O1, O2],
            pattern=[[1, 0], [0, 1]],
            presets=[P, 5000],
            events=[None, None],
            unit="ms",
            current_step=Step,
            accumulator=DAcc,
            completion_flag=Flag,
        ).reset(Bool("R"))
    with Rung(O2):
        calc(Int("N") + 1, Int("N"))


class TestWrittenAroundItsReader:
    @pytest.mark.parametrize(
        "reader",
        [_timer_reader, _comparison_reader, _inverted_comparison_reader],
    )
    def test_reader_between_two_writes(self, reader: Body) -> None:
        _assert_exact(_around(reader))

    def test_off_delay_preset(self) -> None:
        _assert_exact(_around(_off_delay_reader), patch={"Mode": True}, then={"Mode": False})

    @pytest.mark.parametrize("drum", [_time_drum, _time_event_drum])
    def test_drum_tag_preset(self, drum: Body) -> None:
        _assert_exact(_around(drum), patch={"En": True})

    def test_two_writes_to_the_same_value_are_exact_too(self) -> None:
        _assert_exact(_around(_timer_reader, 200, 200))

    def test_indirect_write_to_the_operand(self) -> None:
        """The second write goes through a pointer the graph resolves to a bank."""

        def body() -> None:
            with Rung():
                copy(200, Bank[2])
            with Rung():
                on_delay(T, Bank[2])
            _hit_when_done()
            with Rung():
                copy(5000, Bank[Ptr])

        _assert_exact(body, patch={"Ptr": 2})

    def test_a_comparison_through_a_pointer(self) -> None:
        def body() -> None:
            with Rung():
                copy(200, Bank[2])
            _comparison_on(Bank[Ptr])
            with Rung():
                copy(5000, Bank[2])

        _assert_exact(body, patch={"Ptr": 2})

    def test_pointer_written_around_a_comparison(self) -> None:
        def body() -> None:
            with Rung():
                copy(1, Ptr)
            _comparison_on(Bank[Ptr])
            with Rung():
                copy(2, Ptr)

        _assert_exact(body, patch={"Bank1": 200, "Bank2": 5000})

    def test_over_cap_indirect_write_has_no_position(self) -> None:
        big = Block("Big", TagType.INT, 1, 2000)

        def body() -> None:
            with Rung():
                copy(200, big[5])
            with Rung():
                on_delay(T, big[5])
            _hit_when_done()
            with Rung():
                copy(5000, big[Ptr])

        _assert_exact(body, patch={"Ptr": 5})

    def test_writes_inside_a_subroutine(self) -> None:
        @subroutine("Around")
        def around() -> None:
            with Rung():
                copy(200, P)
            with Rung():
                on_delay(T, P)
            _hit_when_done()
            with Rung():
                copy(5000, P)

        def body() -> None:
            with Rung():
                call(around)

        _assert_exact(body)

    def test_a_call_each_side_of_the_reader(self) -> None:
        @subroutine("WriteP")
        def write() -> None:
            with Rung():
                copy(Int("V"), P)

        def body() -> None:
            with Rung():
                copy(200, Int("V"))
                call(write)
            _timer_reader()
            with Rung():
                copy(5000, Int("V"))
                call(write)

        _assert_exact(body)

    def test_a_subroutine_that_reads_called_between_two_writes(self) -> None:
        @subroutine("Reader")
        def reader() -> None:
            _timer_reader()

        def body() -> None:
            with Rung():
                copy(200, P)
                call(reader)
                copy(5000, P)

        _assert_exact(body)

    def test_within_one_rung(self) -> None:
        def body() -> None:
            with Rung():
                copy(200, P)
                on_delay(T, P)
                copy(5000, P)
            _hit_when_done()

        _assert_exact(body)

    def test_branch_reads_between_the_trunk_writes(self) -> None:
        def body() -> None:
            with Rung():
                on_delay(T, 30_000)
            with Rung():
                copy(200, P)
                with branch(T.Acc >= P):
                    latch(Hit)
                copy(5000, P)

        _assert_exact(body)

    def test_a_for_body_reads_what_its_last_iteration_wrote(self) -> None:
        """The second iteration reads the first's write, which no boundary shows."""

        @subroutine("Compare")
        def compare() -> None:
            with Rung(T.Acc >= P):
                latch(Hit)

        def body() -> None:
            with Rung():
                on_delay(T, 30_000)
            with Rung():
                with forloop(2) as loop:
                    call(compare)
                    calc(loop.idx * 4800 + 200, P)

        _assert_exact(body, patch={"P": 5000})

    def test_a_call_inside_a_for_body(self) -> None:
        @subroutine("ReadThenWrite")
        def read_then_write() -> None:
            with Rung(T.Acc >= P):
                latch(Hit)
            with Rung():
                copy(5000, P)

        def body() -> None:
            with Rung():
                on_delay(T, 30_000)
            with Rung():
                copy(200, P)
            with Rung():
                with forloop(2):
                    call(read_then_write)

        _assert_exact(body)

    def test_a_force_hides_the_write_above_the_reader(self) -> None:
        _assert_exact(_written_above_only(200), force=("P", 5000))

    def test_a_force_hides_a_comparison_operand_too(self) -> None:
        def body() -> None:
            with Rung():
                copy(200, P)
            _comparison_reader()

        _assert_exact(body, force=("P", 5000))


class TestReplaySeek:
    """A seek between checkpoints lands where stepping does."""

    BEFORE, AFTER = 19_000, 30_000  # scans 1900 and 3000 at 10 ms
    TOTAL, TARGET = 3_000, 1_950  # past the threshold, between checkpoints

    def _seek(self, body: Body, patch: dict[str, Any] | None = None) -> tuple[PLC, PLC]:
        def run(scans: int) -> PLC:
            plc = PLC(_program(body)(), dt=0.01)
            if patch:
                plc.patch(patch)
            plc.step()
            plc.run_until(NEVER, max_cycles=scans, fold=False)
            return plc

        return run(self.TOTAL).replay_to(self.TARGET), run(self.TARGET - 1)

    @pytest.mark.parametrize("reader", [_timer_reader, _comparison_reader, _time_drum])
    def test_between_two_writes(self, reader: Body) -> None:
        seek, ref = self._seek(
            _around(reader, self.BEFORE, self.AFTER), patch={"En": True, "P": self.BEFORE}
        )
        assert _state(seek) == _state(ref)

    def test_a_write_that_only_starts_midway(self) -> None:
        def body() -> None:
            with Rung():
                copy(self.BEFORE, P)
            _comparison_reader()
            with Rung(Mode):
                copy(self.AFTER, P)

        def run(scans: int) -> PLC:
            plc = PLC(_program(body)(), dt=0.01)
            plc.step()
            plc.run_until(NEVER, max_cycles=5, fold=False)
            plc.patch({"Mode": True})
            plc.run_until(NEVER, max_cycles=scans, fold=False)
            return plc

        seek = run(self.TOTAL).replay_to(self.TARGET)
        assert _state(seek) == _state(run(self.TARGET - 6))

    def test_one_write_above(self) -> None:
        def body() -> None:
            with Rung():
                copy(self.BEFORE, P)
            _comparison_reader()

        seek, ref = self._seek(body)
        assert _state(seek) == _state(ref)


def _comparison_on(operand: Any) -> None:
    with Rung():
        on_delay(T, 30_000)
    with Rung(T.Acc >= operand):
        latch(Hit)


def _written_above_only(value: int) -> Body:
    def body() -> None:
        with Rung():
            copy(value, P)
        _timer_reader()

    return body


class TestAnInputWrittenOnceStaysInTheLoop:
    """One write per scan, wherever it sits: the boundary shows what was read."""

    # Just past the 15 s presets below: what happens at the preset has to come out right.
    SCANS = 15_050
    DT = 0.001
    FEW = 100

    def _in_the_loop(self, body: Body) -> None:
        kernel = _kernel_scans(body, self.SCANS, dt=self.DT, patch={"P": 15_000})
        assert kernel < self.FEW, f"{kernel} kernel scans for {self.SCANS} logical"
        _assert_exact(body, self.SCANS, dt=self.DT, patch={"P": 15_000})

    def test_constant_preset(self) -> None:
        def body() -> None:
            with Rung():
                on_delay(T, 15_000)
            _hit_when_done()

        self._in_the_loop(body)

    def test_written_above(self) -> None:
        self._in_the_loop(_written_above_only(15_000))

    def test_written_below(self) -> None:
        def body() -> None:
            _timer_reader()
            with Rung():
                copy(15_000, P)

        self._in_the_loop(body)

    def test_written_in_the_same_rung(self) -> None:
        def body() -> None:
            with Rung():
                copy(15_000, P)
                on_delay(T, P)
            _hit_when_done()

        self._in_the_loop(body)

    def test_threshold_written_above(self) -> None:
        def body() -> None:
            with Rung():
                copy(15_000, P)
            with Rung():
                on_delay(T, 30_000)
            with Rung(T.Acc >= P):
                latch(Hit)

        self._in_the_loop(body)

    def test_writers_on_both_sides_with_one_idle(self) -> None:
        def body() -> None:
            with Rung():
                copy(15_000, P)
            _timer_reader()
            with Rung(Mode):
                copy(5000, P)

        assert _kernel_scans(body, self.SCANS, dt=self.DT, patch={"P": 15_000}) < self.FEW

    def test_an_operand_no_rung_writes(self) -> None:
        """It reads the same at every point of a scan."""

        def body() -> None:
            _comparison_on(P)

        _assert_exact(body, patch={"P": 200})


class TestHoldAboveTheProgram:
    """A pilot rung runs before the program, so a hold is a write above every rung."""

    @staticmethod
    def _held(advance: Callable[[PLC], None]) -> PLC:
        def body() -> None:
            _comparison_on(P)
            with Rung():
                copy(5000, P)

        plc = PLC(_program(body)(), dt=0.01)
        plc.patch({"P": 5000})
        plc.step()
        held = fork_with_pilot_rungs(plc, (PilotRung("P", 200, ~Hit),))
        advance(held)
        return held

    def test_a_held_operand_with_a_writer_below_the_reader(self) -> None:
        def stepped(plc: PLC) -> None:
            plc.run_until(NEVER, max_cycles=700, fold=False)

        def folded(plc: PLC) -> None:
            plc.run_until(NEVER, max_cycles=700, fold=True)

        def coast(plc: PLC) -> None:
            coast_until(plc, lambda _s: False, budget=700)

        ref = self._held(stepped)
        assert ref.state.tags["Hit"] is True
        for advance in (folded, coast):
            plc = self._held(advance)
            assert plc.state.scan_id == ref.state.scan_id
            assert _state(plc) == _state(ref)


class TestAWriterThatWakes:
    def test_a_writer_waking_inside_the_run(self) -> None:
        def body() -> None:
            with Rung():
                copy(200, P)
            _timer_reader()
            with Rung(Mode):
                copy(5000, P)
            with Rung(T.Acc >= 100):
                out(Bool("Late"))

        for start in (False, True):
            _assert_exact(body, patch={"Mode": start})

    def test_a_guarded_write_that_stays_the_same_value(self) -> None:
        def body() -> None:
            with Rung(T.Acc >= 300):
                copy(5000, P)
            _timer_reader()
            with Rung(T.Acc < 500):
                copy(5000, P)

        _assert_exact(body, patch={"P": 5000})


def _rebuilt_on_the_clock(reader: Body, on: int, off: int) -> Body:
    def body() -> None:
        with Rung(system.sys.clock_1s):
            copy(on, P)
        with Rung(~system.sys.clock_1s):
            copy(off, P)
        reader()

    return body


class TestRebuiltOnTheClock:
    """Every scan rebuilds the input the same way, but a clock edge changes the value."""

    @pytest.mark.parametrize("reader", [_timer_reader, _comparison_reader])
    @pytest.mark.parametrize("on,off", [(3000, 20_000), (20_000, 3000)], ids=["on", "off"])
    def test_a_clock_edge_moves_the_input(self, reader: Body, on: int, off: int) -> None:
        body = _rebuilt_on_the_clock(reader, on, off)
        _assert_exact(body, 1000, patch={"P": 20_000})

    def test_the_edges_that_move_nothing_stay_in_the_loop(self) -> None:
        body = _rebuilt_on_the_clock(_comparison_reader, 20_000, 20_000)
        assert _kernel_scans(body, 1000, patch={"P": 20_000}) < 100
        _assert_exact(body, 1000, patch={"P": 20_000})

    @pytest.mark.parametrize("shape", ["ticks", "copied"])
    def test_a_real_time_clock_field_in_the_input(self, shape: str) -> None:
        def body() -> None:
            with Rung():
                if shape == "ticks":
                    calc(system.rtc.second * 500 + 3000, P)
                else:
                    copy(system.rtc.second, P)
            with Rung():
                on_delay(T, 30_000)
            with Rung(T.Acc >= P * (1 if shape == "ticks" else 500)):
                latch(Hit)

        def run(fold: bool) -> PLC:
            plc = PLC(_program(body)(), dt=0.01)
            plc.set_rtc(datetime(2026, 1, 1))
            plc.patch({"P": 20_000})
            plc.step()
            plc.run_until(NEVER, max_cycles=800, fold=fold)
            return plc

        folded, stepped = run(True), run(False)
        assert stepped.state.tags["Hit"] is True
        assert _state(folded) == _state(stepped)

    def test_a_mid_run_edge_shows_in_the_input(self) -> None:
        """The value moves only at the edge: a stop on either side shows it."""
        body = _rebuilt_on_the_clock(_comparison_reader, 3000, 20_000)
        for scans in (49, 50, 51, 99, 100, 101):
            _assert_exact(body, scans, patch={"P": 20_000})


class TestAClockWakesAWriter:
    """Every boundary shows the writer below; the gated writer above only shows mid-scan."""

    GATES = [system.sys.clock_1s, ~system.sys.clock_1s]

    @staticmethod
    def _body(reader: Body, gate: Any, chained: bool = False) -> Body:
        def body() -> None:
            with Rung(gate):
                out(Flag)
            with Rung(Flag if chained else gate):
                copy(9000, P)
            reader()
            with Rung():
                copy(30_000, P)

        return body

    @pytest.mark.parametrize("reader", [_timer_reader, _comparison_reader])
    @pytest.mark.parametrize("gate", GATES, ids=["high", "low"])
    @pytest.mark.parametrize("chained", [False, True], ids=["direct", "through_a_bit"])
    def test_the_gated_value_is_read(self, reader: Body, gate: Any, chained: bool) -> None:
        body = self._body(reader, gate, chained)
        _assert_exact(body, 1300, patch={"P": 30_000})

    def test_it_stays_in_the_loop_between_the_edges(self) -> None:
        body = self._body(_comparison_reader, system.sys.clock_1s)
        scans = 8000
        kernel = _kernel_scans(body, scans, dt=0.001, patch={"P": 30_000})
        assert kernel < scans * 3 // 4, f"{kernel} kernel scans for {scans} logical"

    def test_a_real_time_clock_copy_behind_the_writer(self) -> None:
        def body() -> None:
            with Rung():
                copy(system.rtc.second, Int("Sec"))
            with Rung(Int("Sec") == 5):
                copy(3000, P)
            _comparison_reader()
            with Rung():
                copy(30_000, P)

        def run(fold: bool) -> PLC:
            plc = PLC(_program(body)(), dt=0.01)
            plc.set_rtc(datetime(2026, 1, 1))
            plc.patch({"P": 30_000})
            plc.step()
            plc.run_until(NEVER, max_cycles=800, fold=fold)
            return plc

        folded, stepped = run(True), run(False)
        assert stepped.state.tags["Hit"] is True
        assert _state(folded) == _state(stepped)
