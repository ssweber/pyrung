"""A call inside a For body: the ``loop_call`` group (main-level, no ``Conf_LoopCall``).

CLICK has no nested subroutine calls, so a call has to start in the main
program.  This group runs its case rungs in main (``Group(in_main=True)``) and
each case defines the helper subroutine it calls (``Conf_LoopCall...``).  It is
part C: whether CLICK takes a Call between For and Next is open, and a
refusal at paste or transfer then costs this group's helpers and main rungs.  The
cases own no raw memory.

CLICK's error C010D counts the loops of called subroutines toward the depth of
3, which suggests a call inside a loop is allowed, but no case has ever run
one.  Candidate answers per case:

- ``call_inside_loop`` (probe): ``forloop(3)`` whose body calls a helper that
  adds 1 to Result1.  Once per iteration reads Result1 = 3 (pyrung, adopted).
  If CLICK refuses the Call between For and Next, the project does not load:
  that is the answer.  Once per scan, or ignored, would read 1 or 0.
- ``body_contact_after_call`` (probe): the body rung is ``Scratch == 0`` and
  its Call runs a helper that adds 1 to Result1 and sets Scratch.  A body-rung
  contact evaluated on every iteration (pyrung, adopted; CHANGELOG: "contacts
  evaluated on every iteration") skips iterations 2 and 3: Result1 = 1.  A
  contact evaluated once when the loop starts calls on all three: 3.
- ``loop_in_called_subroutine`` (probe): two nested loops of 2 in main, the
  Call in the inner one, and a helper with a loop of 2 that adds 1 to
  Result1: depth 3 counting the helper's loop, 2 * 2 * 2 = 8.  CLICK takes
  it and reads 8 (pyrung, adopted).  If it refuses (C010D at 3), the rule
  counts more than the research found; a fourth level is not probed here, as
  pyrung's exporter refuses it (``CLK_FORLOOP_NESTING_DEPTH``).
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, calc, call, copy, forloop, subroutine


def _fire(slot) -> None:
    with Rung(slot.Cmd != 0):
        copy(1, slot.Fired, oneshot=True)


def prove_call_inside_loop(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(3):
            call("Conf_LoopCall_Inc")
    _fire(slot)
    with subroutine("Conf_LoopCall_Inc"):
        with Rung():
            calc(slot.Result1 + 1, slot.Result1)


def prove_body_contact_after_call(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(3):
            with Rung(slot.Scratch == 0):
                call("Conf_LoopCall_Gate")
    _fire(slot)
    with subroutine("Conf_LoopCall_Gate"):
        with Rung():
            calc(slot.Result1 + 1, slot.Result1)
            copy(1, slot.Scratch)


def prove_loop_in_called_subroutine(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(2):
            with forloop(2):
                call("Conf_LoopCall_Deep")
    _fire(slot)
    with subroutine("Conf_LoopCall_Deep"):
        with Rung():
            with forloop(2):
                calc(slot.Result1 + 1, slot.Result1)


group = Group(
    "loop_call",
    [
        case(
            "A Call between For and Next runs its subroutine once per iteration: "
            "a loop of 3 that calls a subroutine adding 1 to Result1 reads 3",
            ladder=prove_call_inside_loop,
            expect={"Result1": 3},
            probe="Call inside a For body, once per iteration",
        ),
        case(
            "A body rung's contact is evaluated on every iteration, around a Call: "
            "the rung is Scratch == 0 and the called subroutine adds 1 to Result1 "
            "and sets Scratch, so only iteration 1 calls (Result1 = 1)",
            ladder=prove_body_contact_after_call,
            expect={"Result1": 1},
            probe="body-rung contacts per iteration around a call",
        ),
        case(
            "A loop in a called subroutine counts toward the depth of 3: two nested "
            "loops of 2 calling a subroutine with a loop of 2 run its body 8 times",
            ladder=prove_loop_in_called_subroutine,
            expect={"Result1": 8},
            probe="loop in a called subroutine at depth 3",
        ),
    ],
    in_main=True,
)


def test_loop_call():
    assert_all_passed(run(group))
