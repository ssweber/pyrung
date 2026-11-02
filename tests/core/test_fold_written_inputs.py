"""A fold input written around its reader.

A window's crossings come from the values a scan boundary shows: presets and
comparison operands.  A rung reading such a tag between two writes reads a value
no boundary shows, so the window has to stop at each value change, or the fold
lands on the wrong scan.  The same rung with a single write, on either side of
the reader, reads what the boundary shows, and keeps folding.
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
from pyrung.core.analysis.pdg import build_program_graph, tags_read_between_writes
from pyrung.core.analysis.pilot.cyclefold import cycle_fold_until
from pyrung.core.analysis.pilot.overlay import PilotRung, fork_with_pilot_rungs
from pyrung.core.fold import _build_fold_context
from pyrung.core.runner import PLC
from pyrung.core.system_points import system

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
    plc.run_until(lambda _s: False, max_cycles=scans, fold=fold)
    return plc


def _assert_exact(body: Body, scans: int = 700, **kwargs: Any) -> None:
    """Fold, and a replay seek over a stepped recording, both equal stepping."""
    folded = _run(body, True, scans, **kwargs)
    stepped = _run(body, False, scans, **kwargs)
    assert folded.state.scan_id == stepped.state.scan_id
    assert _state(folded) == _state(stepped)
    for target in (stepped.state.scan_id // 2, stepped.state.scan_id - 1):
        seek = stepped._replay_seek(target)
        assert _state(seek) == _state(stepped.replay_to(target))


def _kernel_scans(
    body: Body, scans: int, *, dt: float = 0.01, patch: dict[str, Any] | None = None
) -> int:
    plc = PLC(_program(body)(), dt=dt)
    if patch:
        plc.patch(patch)
    plc.step()
    counter = {"n": 0}
    original = plc._run_single_scan

    def wrapped(**options: Any) -> Any:
        counter["n"] += 1
        return original(**options)

    plc._run_single_scan = wrapped  # ty: ignore[invalid-assignment]
    plc.run_until(lambda _s: False, max_cycles=scans, fold=True)
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
        program = _program(body)()
        ctx = _build_fold_context(PLC(program, dt=0.01), build_program_graph(program), program)
        assert ctx.written_inputs.get(big[5].name) == frozenset({"T_Acc"})
        assert ctx.inputs_between_writes.get(big[5].name) == frozenset({"T_Acc"})

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
    """A seek folds between checkpoints with no write counts to go on."""

    BEFORE, AFTER = 19_000, 30_000  # scans 1900 and 3000 at 10 ms
    TOTAL, TARGET = 3_000, 1_950  # past the threshold, between checkpoints

    def _seek(self, body: Body, patch: dict[str, Any] | None = None) -> tuple[PLC, PLC, PLC]:
        plc = PLC(_program(body)(), dt=0.01)
        if patch:
            plc.patch(patch)
        plc.step()
        plc.run_until(lambda _s: False, max_cycles=self.TOTAL, fold=False)
        return plc, plc._replay_seek(self.TARGET), plc.replay_to(self.TARGET)

    @pytest.mark.parametrize("reader", [_timer_reader, _comparison_reader, _time_drum])
    def test_between_two_writes(self, reader: Body) -> None:
        _plc, seek, ref = self._seek(
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

        plc = PLC(_program(body)(), dt=0.01)
        plc.step()
        plc.run_until(lambda _s: False, max_cycles=5, fold=False)
        plc.patch({"Mode": True})
        plc.run_until(lambda _s: False, max_cycles=self.TOTAL, fold=False)
        assert _state(plc._replay_seek(self.TARGET)) == _state(plc.replay_to(self.TARGET))

    def test_one_write_above_still_folds(self) -> None:
        def body() -> None:
            with Rung():
                copy(self.BEFORE, P)
            _comparison_reader()

        plc, seek, ref = self._seek(body)
        assert _state(seek) == _state(ref)
        assert 0 < plc._last_replay_seek_stats["kernel_scans"] < 100


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


class TestAFoldInputStillFolds:
    """One write per scan, wherever it sits: the boundary shows what was read."""

    # Just past the 15 s presets below: the landing is what has to come out right.
    SCANS = 15_050
    DT = 0.001
    FEW = 100

    def _folds(self, body: Body) -> None:
        kernel = _kernel_scans(body, self.SCANS, dt=self.DT, patch={"P": 15_000})
        assert kernel < self.FEW, f"{kernel} kernel scans for {self.SCANS} logical"
        _assert_exact(body, self.SCANS, dt=self.DT, patch={"P": 15_000})

    def test_constant_preset(self) -> None:
        def body() -> None:
            with Rung():
                on_delay(T, 15_000)
            _hit_when_done()

        self._folds(body)

    def test_written_above(self) -> None:
        self._folds(_written_above_only(15_000))

    def test_written_below(self) -> None:
        def body() -> None:
            _timer_reader()
            with Rung():
                copy(15_000, P)

        self._folds(body)

    def test_written_in_the_same_rung(self) -> None:
        def body() -> None:
            with Rung():
                copy(15_000, P)
                on_delay(T, P)
            _hit_when_done()

        self._folds(body)

    def test_threshold_written_above(self) -> None:
        def body() -> None:
            with Rung():
                copy(15_000, P)
            with Rung():
                on_delay(T, 30_000)
            with Rung(T.Acc >= P):
                latch(Hit)

        self._folds(body)

    def test_writers_on_both_sides_with_one_idle(self) -> None:
        """The live probe counts one write, so what the graph allows did not happen."""

        def body() -> None:
            with Rung():
                copy(15_000, P)
            _timer_reader()
            with Rung(Mode):
                copy(5000, P)

        assert _kernel_scans(body, self.SCANS, dt=self.DT, patch={"P": 15_000}) < self.FEW

    def test_an_operand_no_rung_writes_is_not_an_input_here(self) -> None:
        """It reads the same at every point of a scan."""

        def body() -> None:
            _comparison_on(P)

        program = _program(body)()
        ctx = _build_fold_context(PLC(program, dt=0.01), build_program_graph(program), program)
        assert not ctx.written_inputs
        assert not ctx.inputs_between_writes


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
            plc.run_until(lambda _s: False, max_cycles=700, fold=False)

        def folded(plc: PLC) -> None:
            plc.run_until(lambda _s: False, max_cycles=700, fold=True)

        def cycle(plc: PLC) -> None:
            cycle_fold_until(plc, lambda _s: False, budget=700, kernel_budget=False)

        ref = self._held(stepped)
        assert ref.state.tags["Hit"] is True
        for advance in (folded, cycle):
            plc = self._held(advance)
            assert plc.state.scan_id == ref.state.scan_id
            assert _state(plc) == _state(ref)

    def test_the_context_names_it(self) -> None:
        def body() -> None:
            _comparison_on(P)
            with Rung():
                copy(5000, P)

        program = _program(body)()
        plc = PLC(program, dt=0.01)
        plc.step()
        held = fork_with_pilot_rungs(plc, (PilotRung("P", 200, ~Hit),))
        ctx = _build_fold_context(held, build_program_graph(program), program)
        assert ctx.inputs_between_writes == {"P": frozenset({"T_Acc"})}


class TestLiveProbeCountsTheScan:
    def test_a_writer_waking_inside_the_run_bounds_the_next_window(self) -> None:
        """One write certifies a window; the second writer turning on is a tag
        change, so the plateau breaks first and the next probe counts two."""

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

    def test_the_edges_that_move_nothing_still_fold(self) -> None:
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
            plc.run_until(lambda _s: False, max_cycles=800, fold=fold)
            return plc

        folded, stepped = run(True), run(False)
        assert stepped.state.tags["Hit"] is True
        assert _state(folded) == _state(stepped)

    def test_the_context_keeps_it_in_the_plateau_guard(self) -> None:
        program = _program(_rebuilt_on_the_clock(_comparison_reader, 3000, 20_000))()
        plc = PLC(program, dt=0.01)
        ctx = _build_fold_context(plc, build_program_graph(program), program)
        assert "P" not in ctx.frozen_writes


class TestAClockWakesAWriterTheProbeDidNotCount:
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

    def test_the_clock_is_hard(self) -> None:
        program = _program(self._body(_comparison_reader, system.sys.clock_1s, True))()
        ctx = _build_fold_context(PLC(program), build_program_graph(program), program)
        assert 0.5 in ctx.clock_half_periods
        assert "sys.clock_1s" not in dict(ctx.soft_clocks)

    def test_it_still_folds_between_the_edges(self) -> None:
        """The phase with one write folds; the phase with two steps."""
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
            plc.run_until(lambda _s: False, max_cycles=800, fold=fold)
            return plc

        folded, stepped = run(True), run(False)
        assert stepped.state.tags["Hit"] is True
        assert _state(folded) == _state(stepped)


class TestTagsReadBetweenWrites:
    """The static answer: execution order, as the rungs give it."""

    @staticmethod
    def _between(body: Body, written_before: tuple[str, ...] = ()) -> frozenset[str]:
        program = _program(body)()
        return tags_read_between_writes(program, build_program_graph(program), written_before)

    def test_above_and_below_is_not_between(self) -> None:
        def above() -> None:
            with Rung():
                copy(1, P)
            _timer_reader()

        def below() -> None:
            _timer_reader()
            with Rung():
                copy(1, P)

        assert "P" not in self._between(above)
        assert "P" not in self._between(below)

    def test_between(self) -> None:
        assert "P" in self._between(_around(_timer_reader))

    def test_something_above_the_program_counts_as_a_write(self) -> None:
        def below() -> None:
            _timer_reader()
            with Rung():
                copy(1, P)

        assert "P" in self._between(below, written_before=("P",))

    def test_a_for_body_is_seen_twice(self) -> None:
        def body() -> None:
            with Rung():
                with forloop(2):
                    copy(P, Int("Seen"))
                    copy(1, P)

        assert "P" in self._between(body)
