"""Instruction memory in PILOT's world key.

The memory keys come from the program's site table, not from one trial scan:
a one-shot the first scan does not reach (in a subroutine called later) is
still a dimension.  An unwritten key reads as its site default, so "never
written" and "holds the default" are one state.
"""

from __future__ import annotations

from pyrung.core import (
    PLC,
    Bool,
    Program,
    call,
    latch,
    out,
    reset,
    rung,
    subroutine,
)


def _oneshot_in_late_subroutine() -> tuple[Program, Bool]:
    """A one-shot in a subroutine that the first scan does not call.

    ``Bad`` latches when the subroutine is re-entered with ``T`` held and the
    one-shot does not fire: it fired before ``G`` dropped, and its memory kept
    it from firing again.  Reaching that needs the state ``G off, T on, P off,
    W on`` with the one-shot memory set; the same tags with the memory clear
    (``T`` already on when the program starts, ``G`` off) fire instead.  With
    the memory out of the world key the two merge.
    """
    G = Bool("G", external=True)
    T = Bool("T", external=True)
    P = Bool("P")
    W = Bool("W")
    Bad = Bool("Bad")
    with Program(strict=False) as logic:
        with rung(G):
            call("sub")
        with rung(~G, T):
            latch(W)
        with rung(~T):
            reset(W)
        with rung(P):
            reset(W)
        with rung(G, T, ~P, W):
            latch(Bad)
        with subroutine("sub"):
            with rung(T):
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
