"""Instruction memory in the prover's state key.

The memory keys come from the program's site table, not from one trial scan:
a one-shot the first scan does not reach (in a subroutine called later) is
still a dimension.  An unwritten key reads as its site default, so "never
written" and "holds the default" are one state.
"""

from __future__ import annotations

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Int,
    Program,
    Rung,
    Timer,
    call,
    copy,
    latch,
    on_delay,
    out,
    reset,
    rise,
    subroutine,
)
from pyrung.core.analysis.prove import Counterexample, TraceStep, always, never
from pyrung.core.analysis.prove.kernel import _extract_state_key


def _replay_trace(program: Program, trace: list[TraceStep]) -> PLC:
    plc = PLC(program, dt=0.010)
    for step in trace:
        plc.patch(step.inputs)
        for _ in range(step.scans):
            plc.step()
    return plc


def _assert_trace_replays(program: Program, result: Counterexample, tag_name: str) -> None:
    plc = _replay_trace(program, result.trace)
    assert plc.current_state.tags[tag_name] is True


def _oneshot_in_late_subroutine() -> tuple[Program, Bool]:
    """A one-shot in a subroutine that the first scan does not call.

    ``Bad`` latches when the subroutine is re-entered with ``T`` held and the
    one-shot does not fire: it fired before ``G`` dropped, and its memory kept
    it from firing again.  Reaching that needs the state ``G off, T on, P off,
    W on`` with the one-shot memory set; the same tags with the memory clear
    (``T`` already on when the program starts, ``G`` off) fire instead.  With
    the memory out of the state key the two merge and ``Bad`` looks
    unreachable.
    """
    G = Bool("G", external=True)
    T = Bool("T", external=True)
    P = Bool("P")
    W = Bool("W")
    Bad = Bool("Bad")
    with Program(strict=False) as logic:
        with Rung(G):
            call("sub")
        with Rung(~G, T):
            latch(W)
        with Rung(~T):
            reset(W)
        with Rung(P):
            reset(W)
        with Rung(G, T, ~P, W):
            latch(Bad)
        with subroutine("sub"):
            with Rung(T):
                out(P, oneshot=True)
    return logic, Bad


class TestMemoryKeysFromSiteTable:
    def test_concrete_run_reaches_bad(self):
        logic, _bad = _oneshot_in_late_subroutine()
        plc = PLC(logic, dt=0.010)
        for g, t in [(True, True), (True, True), (False, True), (True, True)]:
            plc.patch({"G": g, "T": t})
            plc.step()
        assert plc.current_state.tags["Bad"] is True

    def test_oneshot_in_uncalled_subroutine_is_a_dimension(self):
        """Regression: the trial scan never wrote the one-shot key (Proven before)."""
        logic, Bad = _oneshot_in_late_subroutine()
        result = always(logic, ~Bad, max_states=10_000, depth_budget=20)
        assert isinstance(result, Counterexample)
        _assert_trace_replays(logic, result, "Bad")

    def test_oneshot_in_uncalled_subroutine_unoptimized(self):
        logic, Bad = _oneshot_in_late_subroutine()
        result = always(logic, ~Bad, max_states=10_000, depth_budget=20, _skip_optimizations=True)
        assert isinstance(result, Counterexample)
        _assert_trace_replays(logic, result, "Bad")


class _Kernel:
    def __init__(self, memory: dict) -> None:
        self.tags: dict = {}
        self.memory = memory
        self.prev: dict = {}


class TestMemoryDefaults:
    def test_unwritten_key_equals_its_default(self):
        names = ("_drum_event_ready:i1", "_oneshot:i2")
        defaults = (True, False)

        def key(memory: dict) -> tuple:
            return _extract_state_key(
                _Kernel(memory),  # type: ignore[arg-type]
                (),
                (),
                names,
                memory_key_defaults=defaults,
            )

        assert key({}) == key({"_drum_event_ready:i1": True, "_oneshot:i2": False})
        # The drum's event latch defaults on: an off value is a different state.
        assert key({}) != key({"_drum_event_ready:i1": False})
        assert key({}) != key({"_oneshot:i2": True})


# --- an edge on the scan a hidden timer crossing lands on ---------------------


def _edge_on_crossing(site: str) -> tuple[Program, Bool]:
    """``Bad`` needs ``Btn`` to rise on the very scan ``T`` finishes.

    A hidden-event jump skips the timer's run and tries input changes only
    before it and after the crossing scan; each edge-sensitive site here reads
    ``Btn`` rising on the crossing scan differently from one scan later.
    """
    Btn = Bool("Btn", external=True)
    Run = Bool("Run", external=True)
    Bad = Bool("Bad")
    T = Timer.clone("EdgeT")
    with Program(strict=False) as logic:
        with Rung(Run):
            on_delay(T, 50)
        if site == "oneshot":
            Flag = Int("Flag")
            with Rung(Btn):
                copy(1, Flag, oneshot=True)
            with Rung(rise(T.Done), Flag == 1):
                latch(Bad)
            with Rung():
                copy(0, Flag)
        else:
            X = Bool("X")
            with Rung(Btn, Run):
                out(X)
            with Rung(rise(T.Done), rise(X)):
                latch(Bad)
    return logic, Bad


@pytest.mark.parametrize("site", ["oneshot", "rise_of_coil"])
def test_edge_on_a_hidden_crossing_scan_is_explored(site: str) -> None:
    logic, bad = _edge_on_crossing(site)
    result = never(logic, bad)
    assert isinstance(result, Counterexample)
    _assert_trace_replays(logic, result, bad.name)
