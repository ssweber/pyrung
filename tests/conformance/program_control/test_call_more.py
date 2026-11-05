"""A subroutine called twice a scan: a timer, a counter and a drum on each call.

An expectation is pyrung's reading today; a CLICK that reads otherwise prints what it read.  A
field whose reading depends on how long a scan takes (a preset of 0 or below) expects a range
over its candidates, and the harness prints what it read.  Clock windows hold for any scan up
to 50 ms, as in ``out``.

``call_more``, called twice a scan (``Group(calls=2)``; ``Scratch`` flips on each call):

- ``called_twice``: a TON on both calls against an RTON on the second: Result1 = 1 for ahead by
  half again (a timer takes the scan's time per execution, as in a For), 2 for level (it takes it
  once a scan).  A counter's input held across calls: 1 (Result2), more if each scan or call were
  an edge.  Input on the second call only: counts (Result3 = 1), 0 where it is compared at scan
  end, which sees it high each time.  A time drum on both calls, Result4 as Result1.

The cases own what ``memory.OWNED`` lists under this group.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import (
    Rung,
    calc,
    copy,
    count_up,
    on_delay,
    out,
    time_drum,
)
from tests.conformance.memory import ClickCounter, ClickTimer, c, ctd, ds, td
from tests.conformance.probe_kit import once


def prove_called_twice(slot):
    # Scratch flips on every call: 1 on the first call of a scan, 0 on the second.
    second = slot.Scratch == 0
    with Rung(slot.Cmd != 0):
        calc(1 - slot.Scratch, slot.Scratch)
    with Rung(slot.Cmd != 0):
        on_delay(ClickTimer(249), 30000)
    with Rung(slot.Cmd != 0, second):
        on_delay(ClickTimer(250), 30000).reset(slot.Cmd == 0)
    held, late, a, b, flag = c[1732], c[1733], c[1734], c[1735], c[1736]
    with Rung(slot.Cmd != 0):
        out(held)
    with Rung(slot.Cmd != 0, second):
        out(late)
    with Rung(held):
        count_up(ClickCounter(148), 30000).reset(slot.Cmd == 0)
    with Rung(late):
        count_up(ClickCounter(150), 30000).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0):
        time_drum(
            outputs=[a, b],
            presets=[30000, 30000],
            unit="ms",
            pattern=[[1, 0], [0, 1]],
            current_step=ds[4016],
            accumulator=td[243],
            completion_flag=flag,
        ).reset(slot.Cmd == 0)
    ready = (*once(slot), second, td[250] >= 200)
    with Rung(*ready):
        calc(td[249] - td[250] * 3 / 2, slot.ErrorCode)
    with Rung(*ready, slot.ErrorCode > 0):
        copy(1, slot.Result1)
    with Rung(*ready, slot.ErrorCode <= 0):
        copy(2, slot.Result1)
    with Rung(*ready):
        copy(ctd[148], slot.Result2)
        calc(td[243] - td[250] * 3 / 2, slot.ErrorCode)
    with Rung(*ready, ctd[150] > 1):
        copy(1, slot.Result3)
    with Rung(*ready, slot.ErrorCode > 0):
        copy(1, slot.Result4)
    with Rung(*ready, slot.ErrorCode <= 0):
        copy(2, slot.Result4)
    with Rung(*ready):
        copy(0, slot.ErrorCode)
        copy(1, slot.Fired)


group = Group(
    "call_more",
    [
        case(
            "In a subroutine called twice a scan a TON takes the scan's time per call (Result1 = 1), "
            "a counter's held input counts once (Result2 = 1), an input on the second call only "
            "counts (Result3 = 1), and a time drum takes the time per call (Result4 = 1)",
            ladder=prove_called_twice,
            scans=200,
            expect={"Result1": 1, "Result2": 1, "Result3": 1, "Result4": 1},
            probe="a timer, counter and drum per call or per scan",
        ),
    ],
    calls=2,
)


def test_call_more():
    assert_all_passed(run(group))
