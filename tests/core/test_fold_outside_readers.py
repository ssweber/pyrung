"""What a window has to leave alone, and who may watch one from outside a rung.

A fold certifies a window with one probe scan.  The scan is a function of the tags
and the scan memory, so the probe repeats only if it left both where it found them:
every piece of memory counts unless the fold models it (``_FoldContext.held_memory``).

A monitor, a stop predicate and a ``when()`` all read the state a scan leaves, from
outside the rungs.  What they cannot watch through a window is one set
(``_FoldContext.moves_or_skipped``): reading anything in it declines the window, or
bounds it if the name is a coordinate.
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
    calc,
    call,
    copy,
    latch,
    on_delay,
    out,
    reset,
    shift,
    subroutine,
    toggle,
)
from pyrung.core.analysis.pilot.cyclefold import cycle_fold_until
from pyrung.core.fold import _extract_condition_crossings
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
# The plateau guard: every piece of scan memory counts unless the fold models it
# ---------------------------------------------------------------------------


def _one_shot_copy() -> None:
    with Rung(T2.Done):
        copy(1, Cmd, oneshot=True)
    with Rung(Cmd == 0):
        calc(N + 1, N)
    with Rung():
        copy(0, Cmd)
    with Rung():
        on_delay(T2, preset=200)
    _timer()


def _one_shot_copy_wrapping() -> None:
    with Rung(T2.Done):
        copy(1, Cmd, oneshot=True)
    with Rung(Cmd == 0):
        calc((N + 1) % 7, N)
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
            calc(N + 1, N)
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
        calc(N + 1, N)
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
        stepped.run_until(NEVER, max_cycles=1000, fold=False)
        folded = _plc(body)
        folded.run_until(NEVER, max_cycles=1000, fold=True)
        assert folded.state.scan_id == stepped.state.scan_id
        assert _state(folded) == _state(stepped)

    def test_a_count_lands_on_its_scan(self) -> None:
        stepped = _plc(_one_shot_copy)
        stepped.run_until(N >= 500, max_cycles=3000, fold=False)
        folded = _plc(_one_shot_copy)
        folded.run_until(N >= 500, max_cycles=3000, fold=True)
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
        def run(fold: bool) -> PLC:
            plc = _plc(body)
            plc.run_until(NEVER, max_cycles=20, fold=False)
            plc.patch({"G": True})
            plc.run_until(NEVER, max_cycles=1000, fold=fold)
            return plc

        stepped, folded = run(False), run(True)
        assert folded.state.scan_id == stepped.state.scan_id
        assert _state(folded) == _state(stepped)

    def test_a_timer_alone_still_folds(self) -> None:
        plc = _plc(_timer)
        counter = _count_kernel_scans(plc)
        plc.run_until(NEVER, max_cycles=700, fold=True)
        assert counter["n"] < 60

    def test_memory_nobody_has_heard_of_counts(self) -> None:
        plc = _plc(_timer)
        plc.step()
        ctx = plc._ensure_fold_context()
        state = plc.state
        assert "_frac:T_Acc" in state.memory and "_dt" in state.memory

        held = ctx.held_memory(state)
        assert "_frac:T_Acc" not in held and "_dt" not in held

        new_key = state.set(memory=state.memory.set("_next_year:1", 0))
        assert ctx.held_memory(new_key) != held, "absent and off are not the same"
        assert ctx.held_memory(new_key)["_next_year:1"] == 0

    def test_a_carry_only_a_timer_rule_moves_is_the_exception(self) -> None:
        # A second timer's carry is modeled by name, not by its prefix: one the
        # fold does not time counts.
        plc = _plc(_timer)
        ctx = plc._ensure_fold_context()
        state = plc.state.set(memory=plc.state.memory.set("_frac:Other_Acc", 0.5))
        assert ctx.held_memory(state)["_frac:Other_Acc"] == 0.5


# ---------------------------------------------------------------------------
# One set: what moves or is skipped
# ---------------------------------------------------------------------------


def _rebuilt_seconds() -> None:
    _timer()
    with Rung(A):
        calc(T.Acc // 1000, Secs)
    with Rung(T.Acc >= 7000):
        latch(Hit)


def _stale_fault_flag() -> None:
    with Rung():
        on_delay(T, preset=40000)
    with Rung():
        calc(T.Acc + 30000, Q)


def _lamp() -> None:
    with Rung(clk100):
        out(Lamp)
    _timer()


class TestTheSet:
    def test_it_holds_what_a_window_moves(self) -> None:
        def done_read() -> None:
            _lamp()
            with Rung(T.Done):
                out(Hit)
            with Rung(Hit):
                out(Y)

        ctx = _plc(done_read)._ensure_fold_context()
        moving = ctx.moves_or_skipped
        assert {T.Acc.name, Lamp.name} <= moving
        assert ctx.plateau_excluded <= moving
        assert not {T.Done.name, Hit.name} & moving

    def test_it_holds_what_a_window_leaves_stale(self) -> None:
        ctx = _plc(_rebuilt_seconds, {"A": True})._ensure_fold_context()
        assert Secs.name in ctx.landing_rebuilt
        assert Secs.name in ctx.moves_or_skipped
        assert Secs.name not in ctx.plateau_excluded

    def test_it_holds_each_time_point_nothing_bounds(self) -> None:
        moving = _plc(_timer)._ensure_fold_context().moves_or_skipped
        assert system.sys.clock_1s.name in moving
        assert system.sys.scan_counter.name in moving
        assert system.sys.scan_clock_toggle.name in moving
        assert system.rtc.second.name in moving

    def test_it_leaves_out_a_clock_that_ends_windows(self) -> None:
        def clocked() -> None:
            with Rung(clk100):
                calc(N + 1, N)
            _timer()

        moving = _plc(clocked)._ensure_fold_context().moves_or_skipped
        assert clk100.name not in moving
        assert system.sys.clock_1s.name in moving

    def test_the_rtc_field_a_rung_reads_ends_windows(self) -> None:
        def seconds() -> None:
            with Rung(system.rtc.second == 30):
                out(Lamp)
            _timer()

        moving = _plc(seconds)._ensure_fold_context().moves_or_skipped
        assert system.rtc.second.name not in moving
        assert system.rtc.minute.name in moving


def _timer_beside_an_oscillator() -> None:
    # Every scan changes F, so only a cycle jump can skip one.
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
        fold: bool,
        patch: dict[str, Any] | None = None,
        scans: int = 1000,
    ) -> tuple[list[tuple[int, Any, Any]], PLC]:
        plc = _plc(body, patch)
        fired: list[tuple[int, Any, Any]] = []
        plc.monitor(tag, lambda cur, prev: fired.append((plc.state.scan_id, prev, cur)))
        plc.run_until(NEVER, max_cycles=scans, fold=fold)
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
        folded, folded_plc = self._firings(body, tag, fold=True, patch=patch)
        stepped, stepped_plc = self._firings(body, tag, fold=False, patch=patch)
        assert folded == stepped
        assert stepped
        assert _state(folded_plc) == _state(stepped_plc)

    @pytest.mark.parametrize("tag", [F, T.Acc], ids=["oscillator", "accumulator"])
    def test_on_a_cycle_fires_as_stepped(self, tag: Any) -> None:
        def run(fold: bool) -> tuple[list[tuple[int, Any, Any]], PLC, dict[str, int]]:
            plc = _plc(_timer_beside_an_oscillator)
            fired: list[tuple[int, Any, Any]] = []
            plc.monitor(tag, lambda cur, prev: fired.append((plc.state.scan_id, prev, cur)))
            stats: dict[str, int] = {}
            if fold:
                cycle_fold_until(
                    plc, lambda _s: False, budget=600, kernel_budget=False, stats=stats
                )
            else:
                for _ in range(600):
                    plc.step()
            return fired, plc, stats

        folded, folded_plc, stats = run(True)
        stepped, stepped_plc, _ = run(False)
        assert folded == stepped
        assert stepped
        assert stats.get("macro_folds", 0) == 0
        assert _state(folded_plc) == _state(stepped_plc)

    def test_a_callback_counting_into_a_tag_counts_as_stepped(self) -> None:
        def run(fold: bool) -> PLC:
            plc = _plc(_rebuilt_seconds, {"A": True})
            plc.monitor(Secs, lambda _c, _p: plc.patch({"N": plc.state.tags.get("N", 0) + 1}))
            plc.run_until(NEVER, max_cycles=1000, fold=fold)
            return plc

        folded, stepped = run(True), run(False)
        assert folded.state.tags["N"] == stepped.state.tags["N"] == 10
        assert _state(folded) == _state(stepped)

    def test_on_what_holds_keeps_the_fold(self) -> None:
        # Only a change of Hit can fire this, and a crossing lands on it.
        def latched() -> None:
            _timer()
            with Rung(T.Acc >= 2000):
                latch(Hit)
            with Rung(Hit):
                out(Y)

        def run(fold: bool) -> tuple[list[int], int]:
            plc = _plc(latched)
            fired: list[int] = []
            plc.monitor(Hit, lambda _c, _p: fired.append(plc.state.scan_id))
            counter = _count_kernel_scans(plc)
            plc.run_until(NEVER, max_cycles=700, fold=fold)
            return fired, counter["n"]

        folded, kernel_scans = run(True)
        stepped, stepped_scans = run(False)
        assert folded == stepped
        # A monitor watches every scan: the run steps.
        assert kernel_scans == stepped_scans


def _seconds_gated() -> None:
    _timer()
    with Rung(A):
        calc(T.Acc // 1000, Secs)


def _acc_view() -> None:
    _timer()
    with Rung():
        calc(T.Acc + 5, Q)


class TestADeclaredRead:
    """``cycle_fold_until(plc, pred, predicate_reads=...)`` with no context builds one
    that guards them, and a given context that moves one traces the predicate."""

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
    @pytest.mark.parametrize("given_context", [False, True], ids=["no-context", "given-context"])
    def test_lands_as_stepped(
        self,
        body: Body,
        name: str,
        value: Any,
        patch: dict[str, Any] | None,
        given_context: bool,
    ) -> None:
        def pred(state: Any) -> bool:
            return state.tags.get(name, 0) >= value

        stepped = self._stepped(body, pred, patch, 1000)
        plc = _plc(body, patch)
        cycle_fold_until(
            plc,
            pred,
            budget=1000,
            kernel_budget=False,
            predicate_reads=frozenset({name}),
            fold_ctx=plc._ensure_fold_context() if given_context else None,
        )
        assert plc.state.scan_id == stepped.state.scan_id
        assert _state(plc) == _state(stepped)

    @pytest.mark.parametrize(
        ("body", "name", "condition", "value", "patch"),
        [
            pytest.param(_seconds_gated, "Secs", Secs >= 3, 3, {"A": True}, id="rebuilt"),
            pytest.param(_acc_view, "Q", Q >= 3005, 3005, None, id="view-of-a-source"),
        ],
    )
    @pytest.mark.parametrize("given_context", [False, True], ids=["no-context", "given-context"])
    def test_a_comparison_places_only_a_coordinate(
        self,
        body: Body,
        name: str,
        condition: Any,
        value: int,
        patch: dict[str, Any] | None,
        given_context: bool,
    ) -> None:
        # A comparison ends a window on a source's own value.  One on anything else
        # ends none, and a view's is on its source, so the read has to stand.
        def pred(state: Any) -> bool:
            return state.tags.get(name, 0) >= value

        stepped = self._stepped(body, pred, patch, 1000)
        assert stepped.state.scan_id == 300
        plc = _plc(body, patch)
        cycle_fold_until(
            plc,
            pred,
            budget=1000,
            kernel_budget=False,
            predicate_reads=frozenset({name}),
            extra_comparisons=_extract_condition_crossings(condition),
            fold_ctx=plc._ensure_fold_context() if given_context else None,
        )
        assert plc.state.scan_id == stepped.state.scan_id
        assert _state(plc) == _state(stepped)

    def test_no_context_guards_the_read_it_was_told_of(self) -> None:
        # No rung reads Lamp, so a context built blind leaves it out of the plateau
        # guard: the read would have to be traced, a scan at a time.  Told of the
        # read, the context guards Lamp and the windows stand.
        def body() -> None:
            _timer()
            with Rung(clk100, G):
                out(Lamp)

        def pred(state: Any) -> bool:
            return state.tags.get("Lamp") is True

        plc = _plc(body)
        stats: dict[str, int] = {}
        reached = cycle_fold_until(
            plc,
            pred,
            budget=1000,
            kernel_budget=False,
            predicate_reads=frozenset({"Lamp"}),
            stats=stats,
        )
        assert reached is False
        assert stats["kernel_scans"] < 100
        assert _state(plc) == _state(self._stepped(body, pred, None, 1000))

    def test_a_read_nothing_moves_keeps_the_fold(self) -> None:
        plc = _plc(_rebuilt_seconds)
        stats: dict[str, int] = {}
        reached = cycle_fold_until(
            plc,
            lambda state: state.tags.get("Hit") is True,
            budget=1000,
            kernel_budget=False,
            predicate_reads=frozenset({"Hit"}),
            stats=stats,
        )
        assert reached is True
        assert stats["kernel_scans"] < 100

    def test_a_placed_coordinate_folds_to_its_crossing(self) -> None:
        # The comparison ends the window, so its read does not bound every change.
        def pred(state: Any) -> bool:
            return state.tags.get(T.Acc.name, 0) >= 3000

        stepped = self._stepped(_timer, pred, None, 1000)
        plc = _plc(_timer)
        stats: dict[str, int] = {}
        reached = cycle_fold_until(
            plc,
            pred,
            budget=1000,
            kernel_budget=False,
            predicate_reads=frozenset({T.Acc.name}),
            extra_comparisons=_extract_condition_crossings(T.Acc >= 3000),
            stats=stats,
        )
        assert reached is True
        assert plc.state.scan_id == stepped.state.scan_id
        assert stats["kernel_scans"] < 100


def _oscillator() -> None:
    with Rung(~F):
        out(F)


def _sterile_stats(plc: PLC) -> dict[str, int]:
    stats: dict[str, int] = {}
    cycle_fold_until(
        plc,
        lambda _s: False,
        predicate_reads=frozenset({"Y"}),
        budget=200,
        kernel_budget=True,
        stats=stats,
    )
    return stats


class TestASterileCycle:
    """A coast on a cycle that can never reach its goal stops at once: what a run with
    a Harness relies on.  Only a callback the ring cannot see keeps it going."""

    def test_a_monitor_only_watches_it(self) -> None:
        plc = _plc(_oscillator)
        plc.monitor(F, lambda _new, _old: None)
        stats = _sterile_stats(plc)
        assert stats["sterile_cycle"] == 1
        assert stats["kernel_scans"] < 200

    def test_a_when_keeps_it_going(self) -> None:
        plc = _plc(_oscillator)
        plc.when(lambda _s: True).do(lambda _s: None)
        stats = _sterile_stats(plc)
        assert "sterile_cycle" not in stats
        assert stats["kernel_scans"] == 200


class TestWhenDo:
    """A ``when().do()`` runs on every scan its condition holds, on whatever it closes
    over: skipping scans would skip it, so a jump declines."""

    @staticmethod
    def _run(
        body: Body, condition: Any, patch: dict[str, Any] | None, *, fold: bool, scans: int = 1000
    ) -> tuple[list[int], PLC, dict[str, int]]:
        plc = _plc(body)
        fired: list[int] = []

        def act(_state: Any) -> None:
            fired.append(plc.state.scan_id)
            if patch:
                plc.patch(patch)

        plc.when(condition).do(act)
        stats: dict[str, int] = {}
        if fold:
            cycle_fold_until(plc, lambda _s: False, budget=scans, kernel_budget=False, stats=stats)
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
        ],
    )
    def test_fires_as_stepped_across_a_cycle(
        self, body: Body, condition: Any, patch: dict[str, Any] | None
    ) -> None:
        folded, folded_plc, stats = self._run(body, condition, patch, fold=True)
        stepped, stepped_plc, _ = self._run(body, condition, patch, fold=False)
        assert folded == stepped
        assert folded_plc.state.scan_id == stepped_plc.state.scan_id
        assert _state(folded_plc) == _state(stepped_plc)
        assert stats["macro_folds"] == 0

    def test_a_jump_returns_once_it_is_gone(self) -> None:
        plc = _plc(_timer)
        handle = plc.when(lambda _s: True).do(lambda _state: None)
        handle.remove()
        stats: dict[str, int] = {}
        cycle_fold_until(plc, lambda _s: False, budget=1000, kernel_budget=False, stats=stats)
        assert stats["macro_folds"] >= 1
