"""Drums: the ``drum`` group (subroutine ``Conf_Drum``).

A drum's outputs, events and completion flag are C bits and its accumulator a
TD register, and no slot field is either, so each case uses raw registers it
owns: C1901..C2000 and TD401..TD500 (``memory.OWNED``).  The current step is a slot field (DS).  Each drum's reset input is
``Cmd == 0``, so the reset puts it back on step 1 with its time cleared.

The expectations are pyrung's answers, unconfirmed on a CLICK.  They are the
open probes D1 (does a Time & Event step's elapsed time keep counting past its
duration while it waits for the event?) and D3 (is the event a level: does an
event already on when the time is up end the step?).
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, latch, reset, time_event_drum
from tests.conformance.memory import c, td


def prove_te_acc_counts_while_waiting(slot):
    # Step 1 is up after 300 ms but its event (C1903) never comes.
    with Rung(slot.Cmd != 0):
        time_event_drum(
            outputs=[c[1901], c[1902]],
            presets=[300, None],
            events=[c[1903], c[1904]],
            unit="ms",
            pattern=[[1, 0], [0, 1]],
            current_step=slot.Result1,
            accumulator=td[401],
            completion_flag=c[1905],
        ).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, slot.Result1 == 1, td[401] >= 500):
        copy(1, slot.Result2, oneshot=True)
    with Rung(slot.Cmd != 0):
        copy(1, slot.Fired, oneshot=True)


def prove_te_level_event_already_on(slot):
    # The event is on before step 1's time is up, and stays on through the
    # event-only step 2.
    with Rung(slot.Cmd != 0):
        latch(c[1913])
    with Rung(slot.Cmd != 0):
        time_event_drum(
            outputs=[c[1911], c[1912]],
            presets=[300, None],
            events=[c[1913], c[1913]],
            unit="ms",
            pattern=[[1, 0], [0, 1]],
            current_step=slot.Result1,
            accumulator=td[411],
            completion_flag=c[1915],
        ).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, c[1915]):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0):
        copy(1, slot.Fired, oneshot=True)


def init_te_level_event_already_on(slot):
    reset(c[1913])


group = Group(
    "drum",
    [
        case(
            "A Time & Event step whose time is up keeps timing while it waits for its "
            "event: the step stays 1 and its elapsed time passes 500 ms on a 300 ms "
            "duration (Result2 = 1)",
            ladder=prove_te_acc_counts_while_waiting,
            expect={"Result1": 1, "Result2": 1},
            scans=80,
        ),
        case(
            "A Time & Event event is a level: one already on ends step 1 when its 300 ms "
            "is up, and, still on, ends the event-only last step, so the drum completes "
            "(Result2 = 1) on step 2",
            ladder=prove_te_level_event_already_on,
            init=init_te_level_event_already_on,
            expect={"Result1": 2, "Result2": 1},
            scans=60,
        ),
    ],
)


def test_drum():
    assert_all_passed(run(group))
