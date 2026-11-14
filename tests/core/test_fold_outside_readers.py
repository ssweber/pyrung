"""Scan memory the tags do not show, and who may watch a run from outside a rung.

A scan is a function of the tags and the scan memory, so a program whose edge shows only in
memory must not be repeated as if nothing moved.  A monitor, a stop predicate and a
``when()`` read the state a scan leaves, from outside the rungs, and must see each scan they
would see stepped.  Every program here runs in the loop and stepped, and the two must agree.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from pyrung import (
    Block,
    Bool,
    Int,
    Program,
    Rung,
    TagType,
    Timer,
    call,
    copy,
    latch,
    math,
    on_delay,
    out,
    reset,
    shift,
    subroutine,
    toggle,
)
from pyrung.core.coast_run import coast_until
from pyrung.core.runner import PLC
from pyrung.core.system_points import system
from tests.core.scan_loop_counts import NEVER

pytestmark = pytest.mark.usefixtures("loop_from_first_scan")

clk100 = system.sys.clock_100ms

A, F, G, Hit, Lamp, R, Y = (
    Bool("A"),
    Bool("F"),
    Bool("G"),
    Bool("Hit"),
    Bool("Lamp"),
    Bool("R"),
    Bool("Y"),
)
Cmd, N, Q, Secs = Int("Cmd"), Int("N"), Int("Q"), Int("Secs")
T, T2 = Timer.clone("T"), Timer.clone("T2")
Bits = Block("Bits", TagType.BOOL, 1, 8)

Body = Callable[[], None]


def _build(body: Body) -> Program:
    with Program(strict=False) as prog:
        body()
    return prog


def _state(plc: PLC) -> dict[str, Any]:
    tags = {k: v for k, v in plc.state.tags.items() if not k.startswith("sys.scan")}
    memory = {f"mem:{k}": v for k, v in plc.state.memory.items() if "_frac" not in k}
    return {k: v for k, v in {**tags, **memory}.items() if v}


def _plc(body: Body, patch: dict[str, Any] | None = None) -> PLC:
    plc = PLC(_build(body), dt=0.01)
    if patch:
        plc.patch(patch)
    plc.step()
    return plc


def _count_kernel_scans(plc: PLC) -> dict[str, int]:
    counter = {"n": 0}
    original = plc._run_single_scan

    def wrapped(**options: Any) -> Any:
        counter["n"] += 1
        return original(**options)

    plc._run_single_scan = wrapped  # ty: ignore[invalid-assignment]
    return counter


def _timer() -> None:
    with Rung():
        on_delay(T, preset=7350)


# ---------------------------------------------------------------------------
# Scan memory the tags do not show
# ---------------------------------------------------------------------------


def _one_shot_copy() -> None:
    with Rung(T2.Done):
        copy(1, Cmd, oneshot=True)
    with Rung(Cmd == 0):
        math(N + 1, N)
    with Rung():
        copy(0, Cmd)
    with Rung():
        on_delay(T2, preset=200)
    _timer()


def _one_shot_copy_wrapping() -> None:
    with Rung(T2.Done):
        copy(1, Cmd, oneshot=True)
    with Rung(Cmd == 0):
        math((N + 1) % 7, N)
    with Rung():
        copy(0, Cmd)
    with Rung():
        on_delay(T2, preset=200)
    _timer()


def _transient(fire: Body) -> Body:
    """*fire* sets ``F`` on the scan after ``G`` raises ``A``; a rung below clears it."""

    def body() -> None:
        _timer()
        fire()
        with Rung(~F):
            math(N + 1, N)
        with Rung():
            reset(F)
        with Rung(G):
            out(A)

    return body


def _fire_one_shot_out() -> None:
    with Rung(A):
        out(F, oneshot=True)


def _fire_one_shot_copy() -> None:
    with Rung(A):
        copy(1, F, oneshot=True)


def _fire_toggle() -> None:
    with Rung(A):
        toggle(F)


def _fire_in_a_subroutine() -> None:
    with Rung():
        call("Fire")
    with subroutine("Fire"):
        with Rung(A):
            copy(1, F, oneshot=True)


def _shift_clock_edge() -> None:
    _timer()
    with Rung(G):
        shift(Bits.select(1, 8)).clock(A).reset(R)
    with Rung(~Bits[1]):
        math(N + 1, N)
    with Rung():
        reset(Bits.select(1, 8))
    with Rung(G):
        out(A)


class TestAProbeThatLeftMemoryBehind:
    """Each scan fires an edge the tags do not show: it restores them, so only the
    memory says the next scan is not a repeat of it."""

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(_one_shot_copy, id="one-shot-copy"),
            pytest.param(_one_shot_copy_wrapping, id="one-shot-copy-wrapping"),
        ],
    )
    def test_a_one_shot_that_rearms_on_a_timer(self, body: Body) -> None:
        stepped = _plc(body)
        stepped.run_until(NEVER, max_cycles=1000, jit=False)
        folded = _plc(body)
        folded.run_until(NEVER, max_cycles=1000, jit=True)
        assert folded.state.scan_id == stepped.state.scan_id
        assert _state(folded) == _state(stepped)

    def test_a_count_lands_on_its_scan(self) -> None:
        stepped = _plc(_one_shot_copy)
        stepped.run_until(N >= 500, max_cycles=3000, jit=False)
        folded = _plc(_one_shot_copy)
        folded.run_until(N >= 500, max_cycles=3000, jit=True)
        assert folded.state.scan_id == stepped.state.scan_id
        assert _state(folded) == _state(stepped)

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(_transient(_fire_one_shot_out), id="one-shot-out"),
            pytest.param(_transient(_fire_one_shot_copy), id="one-shot-copy"),
            pytest.param(_transient(_fire_toggle), id="toggle"),
            pytest.param(_transient(_fire_in_a_subroutine), id="one-shot-in-a-subroutine"),
            pytest.param(_shift_clock_edge, id="shift-clock"),
        ],
    )
    def test_an_edge_raised_from_below(self, body: Body) -> None:
        def run(jit: bool) -> PLC:
            plc = _plc(body)
            plc.run_until(NEVER, max_cycles=20, jit=False)
            plc.patch({"G": True})
            plc.run_until(NEVER, max_cycles=1000, jit=jit)
            return plc

        stepped, folded = run(False), run(True)
        assert folded.state.scan_id == stepped.state.scan_id
        assert _state(folded) == _state(stepped)

    def test_a_timer_alone_stays_in_the_loop(self) -> None:
        plc = _plc(_timer)
        counter = _count_kernel_scans(plc)
        plc.run_until(NEVER, max_cycles=700, jit=True)
        assert counter["n"] < 60


# ---------------------------------------------------------------------------
# Programs a reader outside the rungs may watch
# ---------------------------------------------------------------------------


def _rebuilt_seconds() -> None:
    _timer()
    with Rung(A):
        math(T.Acc // 1000, Secs)
    with Rung(T.Acc >= 7000):
        latch(Hit)


def _stale_fault_flag() -> None:
    with Rung():
        on_delay(T, preset=40000)
    with Rung():
        math(T.Acc + 30000, Q)


def _lamp() -> None:
    with Rung(clk100):
        out(Lamp)
    _timer()


def _done_read() -> None:
    _lamp()
    with Rung(T.Done):
        out(Hit)
    with Rung(Hit):
        out(Y)


def _clocked() -> None:
    with Rung(clk100):
        math(N + 1, N)
    _timer()


def _seconds() -> None:
    with Rung(system.rtc.second == 30):
        out(Lamp)
    _timer()


class TestPrograms:
    @pytest.mark.parametrize(
        ("body", "patch"),
        [
            pytest.param(_done_read, None, id="done-read"),
            pytest.param(_rebuilt_seconds, {"A": True}, id="rebuilt"),
            pytest.param(_stale_fault_flag, None, id="fault-flag"),
            pytest.param(_timer, None, id="time-points"),
            pytest.param(_clocked, None, id="clock"),
            pytest.param(_seconds, None, id="rtc-field"),
        ],
    )
    def test_each_runs_as_stepped(self, body: Body, patch: dict[str, Any] | None) -> None:
        stepped = _plc(body, patch)
        stepped.run_until(NEVER, max_cycles=1000, jit=False)
        folded = _plc(body, patch)
        folded.run_until(NEVER, max_cycles=1000, jit=True)
        assert folded.state.scan_id == stepped.state.scan_id
        assert _state(folded) == _state(stepped)


def _timer_beside_an_oscillator() -> None:
    # Every scan changes F.
    with Rung(F):
        on_delay(T, preset=9000).reset(R)
    with Rung(~F):
        out(F)


class TestAMonitor:
    @staticmethod
    def _firings(
        body: Body,
        tag: Any,
        *,
        jit: bool,
        patch: dict[str, Any] | None = None,
        scans: int = 1000,
    ) -> tuple[list[tuple[int, Any, Any]], PLC]:
        plc = _plc(body, patch)
        fired: list[tuple[int, Any, Any]] = []
        plc.monitor(tag, lambda cur, prev: fired.append((plc.state.scan_id, prev, cur)))
        plc.run_until(NEVER, max_cycles=scans, jit=jit)
        return fired, plc

    @pytest.mark.parametrize(
        ("body", "tag", "patch"),
        [
            pytest.param(_rebuilt_seconds, Secs, {"A": True}, id="rebuilt-at-the-landing"),
            pytest.param(_stale_fault_flag, system.fault.out_of_range, None, id="stale-fault-flag"),
            pytest.param(_lamp, Lamp, None, id="indicator"),
            pytest.param(_lamp, T.Acc, None, id="accumulator"),
        ],
    )
    def test_on_what_moves_fires_as_stepped(
        self, body: Body, tag: Any, patch: dict[str, Any] | None
    ) -> None:
        folded, folded_plc = self._firings(body, tag, jit=True, patch=patch)
        stepped, stepped_plc = self._firings(body, tag, jit=False, patch=patch)
        assert folded == stepped
        assert stepped
        assert _state(folded_plc) == _state(stepped_plc)

    @pytest.mark.parametrize("tag", [F, T.Acc], ids=["oscillator", "accumulator"])
    def test_on_a_cycle_fires_as_stepped(self, tag: Any) -> None:
        def run(jit: bool) -> tuple[list[tuple[int, Any, Any]], PLC, dict[str, int]]:
            plc = _plc(_timer_beside_an_oscillator)
            fired: list[tuple[int, Any, Any]] = []
            plc.monitor(tag, lambda cur, prev: fired.append((plc.state.scan_id, prev, cur)))
            stats: dict[str, int] = {}
            if jit:
                coast_until(plc, lambda _s: False, budget=600, stats=stats)
            else:
                for _ in range(600):
                    plc.step()
            return fired, plc, stats

        folded, folded_plc, _stats = run(True)
        stepped, stepped_plc, _ = run(False)
        assert folded == stepped
        assert stepped
        assert _state(folded_plc) == _state(stepped_plc)

    def test_a_callback_counting_into_a_tag_counts_as_stepped(self) -> None:
        def run(jit: bool) -> PLC:
            plc = _plc(_rebuilt_seconds, {"A": True})
            plc.monitor(Secs, lambda _c, _p: plc.patch({"N": plc.state.tags.get("N", 0) + 1}))
            plc.run_until(NEVER, max_cycles=1000, jit=jit)
            return plc

        folded, stepped = run(True), run(False)
        assert folded.state.tags["N"] == stepped.state.tags["N"] == 10
        assert _state(folded) == _state(stepped)

    def test_on_what_holds_fires_as_stepped(self) -> None:
        # Only a change of Hit can fire this.
        def latched() -> None:
            _timer()
            with Rung(T.Acc >= 2000):
                latch(Hit)
            with Rung(Hit):
                out(Y)

        def run(jit: bool) -> tuple[list[int], int]:
            plc = _plc(latched)
            fired: list[int] = []
            plc.monitor(Hit, lambda _c, _p: fired.append(plc.state.scan_id))
            counter = _count_kernel_scans(plc)
            plc.run_until(NEVER, max_cycles=700, jit=jit)
            return fired, counter["n"]

        folded, kernel_scans = run(True)
        stepped, stepped_scans = run(False)
        assert folded == stepped
        # A monitor watches every scan: the run steps.
        assert kernel_scans == stepped_scans


def _seconds_gated() -> None:
    _timer()
    with Rung(A):
        math(T.Acc // 1000, Secs)


def _acc_view() -> None:
    _timer()
    with Rung():
        math(T.Acc + 5, Q)


class TestADeclaredRead:
    """``coast_until(plc, pred, predicate_reads=...)`` lands as stepped, whatever moves the
    read."""

    @staticmethod
    def _stepped(body: Body, pred: Any, patch: dict[str, Any] | None, scans: int) -> PLC:
        plc = _plc(body, patch)
        for _ in range(scans):
            if pred(plc.state):
                break
            plc.step()
        return plc

    @pytest.mark.parametrize(
        ("body", "name", "value", "patch"),
        [
            pytest.param(_lamp, "Lamp", True, None, id="indicator"),
            pytest.param(_rebuilt_seconds, "Secs", 3, {"A": True}, id="rebuilt"),
            pytest.param(_rebuilt_seconds, "T_Acc", 3000, None, id="coordinate"),
        ],
    )
    def test_lands_as_stepped(
        self,
        body: Body,
        name: str,
        value: Any,
        patch: dict[str, Any] | None,
    ) -> None:
        def pred(state: Any) -> bool:
            return state.tags.get(name, 0) >= value

        stepped = self._stepped(body, pred, patch, 1000)
        plc = _plc(body, patch)
        coast_until(plc, pred, budget=1000, predicate_reads=frozenset({name}))
        assert plc.state.scan_id == stepped.state.scan_id
        assert _state(plc) == _state(stepped)

    @pytest.mark.parametrize(
        ("body", "name", "value", "patch"),
        [
            pytest.param(_seconds_gated, "Secs", 3, {"A": True}, id="rebuilt"),
            pytest.param(_acc_view, "Q", 3005, None, id="view-of-a-source"),
        ],
    )
    def test_a_value_derived_from_an_accumulator(
        self,
        body: Body,
        name: str,
        value: int,
        patch: dict[str, Any] | None,
    ) -> None:
        def pred(state: Any) -> bool:
            return state.tags.get(name, 0) >= value

        stepped = self._stepped(body, pred, patch, 1000)
        assert stepped.state.scan_id == 300
        plc = _plc(body, patch)
        coast_until(plc, pred, budget=1000, predicate_reads=frozenset({name}))
        assert plc.state.scan_id == stepped.state.scan_id
        assert _state(plc) == _state(stepped)

    def test_a_read_no_rung_reads_stays_in_the_loop(self) -> None:
        def body() -> None:
            _timer()
            with Rung(clk100, G):
                out(Lamp)

        def pred(state: Any) -> bool:
            return state.tags.get("Lamp") is True

        plc = _plc(body)
        stats: dict[str, int] = {}
        reached = coast_until(
            plc, pred, budget=1000, predicate_reads=frozenset({"Lamp"}), stats=stats
        )
        assert reached is False
        assert stats["kernel_scans"] < 100
        assert _state(plc) == _state(self._stepped(body, pred, None, 1000))

    def test_a_read_nothing_moves_stays_in_the_loop(self) -> None:
        plc = _plc(_rebuilt_seconds)
        stats: dict[str, int] = {}
        reached = coast_until(
            plc,
            lambda state: state.tags.get("Hit") is True,
            budget=1000,
            predicate_reads=frozenset({"Hit"}),
            stats=stats,
        )
        assert reached is True
        assert stats["kernel_scans"] < 100

    def test_a_read_of_an_accumulator_lands_on_its_crossing(self) -> None:
        def pred(state: Any) -> bool:
            return state.tags.get(T.Acc.name, 0) >= 3000

        stepped = self._stepped(_timer, pred, None, 1000)
        plc = _plc(_timer)
        stats: dict[str, int] = {}
        reached = coast_until(
            plc, pred, budget=1000, predicate_reads=frozenset({T.Acc.name}), stats=stats
        )
        assert reached is True
        assert plc.state.scan_id == stepped.state.scan_id
        assert stats["kernel_scans"] < 100


def _oscillator() -> None:
    with Rung(~F):
        out(F)


class TestWhenDo:
    """A ``when().do()`` runs on every scan its condition holds, on whatever it closes
    over."""

    @staticmethod
    def _run(
        body: Body, condition: Any, patch: dict[str, Any] | None, *, jit: bool, scans: int = 1000
    ) -> tuple[list[int], PLC, dict[str, int]]:
        plc = _plc(body)
        fired: list[int] = []

        def act(_state: Any) -> None:
            fired.append(plc.state.scan_id)
            if patch:
                plc.patch(patch)

        plc.when(condition).do(act)
        stats: dict[str, int] = {}
        if jit:
            coast_until(plc, lambda _s: False, budget=scans, stats=stats)
        else:
            for _ in range(scans):
                plc.step()
        return fired, plc, stats

    @pytest.mark.parametrize(
        ("body", "condition", "patch"),
        [
            pytest.param(_timer, T.Acc >= 2000, {"Q": 5}, id="patches-past-a-coordinate"),
            pytest.param(_timer, T.Acc >= 2000, None, id="writes-no-tag"),
            pytest.param(_lamp, Lamp, None, id="on-an-indicator"),
            pytest.param(_timer, lambda _s: True, None, id="always"),
            pytest.param(_rebuilt_seconds, Secs >= 2, {"Q": 5}, id="on-a-rebuilt-value"),
            pytest.param(_oscillator, lambda _s: True, None, id="on-an-oscillator"),
        ],
    )
    def test_fires_as_stepped(
        self, body: Body, condition: Any, patch: dict[str, Any] | None
    ) -> None:
        folded, folded_plc, _stats = self._run(body, condition, patch, jit=True)
        stepped, stepped_plc, _ = self._run(body, condition, patch, jit=False)
        assert folded == stepped
        assert folded_plc.state.scan_id == stepped_plc.state.scan_id
        assert _state(folded_plc) == _state(stepped_plc)

    def test_the_loop_returns_once_it_is_gone(self) -> None:
        plc = _plc(_timer)
        handle = plc.when(lambda _s: True).do(lambda _state: None)
        handle.remove()
        stats: dict[str, int] = {}
        coast_until(plc, lambda _s: False, budget=1000, stats=stats)
        assert stats["stretches"] >= 1
