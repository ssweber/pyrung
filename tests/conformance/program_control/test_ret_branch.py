"""Return on a branch: the ``ret_branch`` group (subroutine ``Conf_RetBranch``).

A probe group in part C: a CLICK that refuses the ladder (at paste or at
transfer) costs this group's subroutine and call rung.
It owns no raw memory.

- ``branch_return_stops_subroutine`` (probe): a subroutine rung whose main
  output copies 1 into Scratch and whose branch, under a contact, carries a
  Return.  The rung above writes Result1 = 1 and ``Fired``; the rung below the
  return writes Result2 = 1 (the marker).  A Return on a branch that is taken
  ends the subroutine there: Result1 = 1, Result2 = 0 (pyrung today, and the
  answer adopted).  If CLICK accepts the Return but ignores it on a branch,
  the marker is written: Result1 = 1, Result2 = 1.  If CLICK refuses the
  ladder at paste or transfer, that is the answer: the project does not
  load, nothing runs, and the case is not run against it.

The Return skips the rest of the subroutine, so the group holds this one case
(any other case's rungs after it would not run) and the return is gated on
``Cmd != 0`` so the case's reset rung, above it, still runs while idle.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, branch, copy, return_early
from pyrung.core.program import Program


def prove_branch_return_stops_subroutine(slot):
    with Rung(slot.Cmd != 0):
        copy(1, slot.Result1)
        copy(1, slot.Fired)
    with Rung(slot.Cmd != 0):
        copy(1, slot.Scratch)
        with branch(slot.Cmd != 0):
            # The footprint check builds a case outside any subroutine, where
            # return_early() refuses; it needs the tags a case touches only.
            program = Program._current()
            if program is not None and program._current_subroutine is not None:
                return_early()
            else:
                copy(0, slot.Scratch)
    with Rung(slot.Cmd != 0):
        copy(1, slot.Result2)


group = Group(
    "ret_branch",
    [
        case(
            "A Return on a branch of a subroutine rung, powered, ends the subroutine "
            "there: the rung above it runs (Result1) and the rung below it does not "
            "(Result2 stays 0)",
            ladder=prove_branch_return_stops_subroutine,
            expect={"Result1": 1, "Result2": 0},
            probe="Return on a branch of a subroutine rung",
        ),
    ],
)


def test_ret_branch():
    assert_all_passed(run(group))
