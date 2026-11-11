"""For and timers, past what ``loop`` and ``timers`` read.

An expectation is pyrung's reading today; a CLICK that reads otherwise prints what it read.  A
field whose reading depends on how long a scan takes (a preset of 0 or below) expects a range
over its candidates, and the harness prints what it read.  Clock windows hold for any scan up
to 50 ms, as in ``out``.

``for_more``:

- ``oneshot_and_edges``: a one-shot For of 2, its rung true on scans 3..5 and 7: runs on 3 and 7,
  Result1 = 4, Result2 = 7 (last scan).  Each scan true is 8; no re-arm is 2 and 3.  A bit rises on
  scan 3, the For's rung false on 2..4: a body's rise() fires on the scan the body comes back
  (Result3 = 5), 0 where its memory tracks in a skipped body.  A toggle in a For of 2, rung held:
  on (Result4 = 1) if it flips once, off (2) if each pass is an edge.
- ``timers_at_limits``: steps that ``Scratch`` holds, and timers that judge themselves.  Each
  step's rungs come last, in reverse order, so each new step starts on the next scan.

  - Step 0 runs a TON in a For body until its Acc reads 100.  Step 1 skips the body until the
    case's clock (a TON from ``Cmd``) reads 400 ms.  On the first scan of step 2, Result1 is 1 if
    the TON's Acc is under 300, 2 if the first scan back took in the gap.  ``loop`` reads only
    that it holds while skipped.
  - A TON from a DS of -5, on from ``Cmd``: Result2 is 1 if it is done on the first scan of step
    2, 0 where -5 reads unsigned.
  - A counter from a DS of -5, with no count: ErrorCode is 1 if it is done on the first scan of
    step 2, 0 where -5 reads unsigned.
  - A TOF of 100 ms, its rung on for step 0.  Once it has timed out and its TD reads 200, its DS
    goes up to 1000.  Result3 is 1 if Done is back on once the TD reads 300, 0 where a timed-out
    TOF stays out.  ``Fired`` goes up then, in step 3.
  - A TD written -100 on the first scan, below its TON on from ``Cmd``.  On the scan after the
    write, Result4 is 1 if it is below 0, 2 if it is 0 or above.

- ``clock_edges_in_a_body``: ``system/clock_bits_toggle_at_their_rate`` reads a clock's rise in a
  subroutine; in a For body nothing has.  rise(SC5) above a For of 2 against the same in its body,
  over 500 ms: Result1 = 1 same count, 2 double (each pass an edge), 0 else.  The same for SC3, the
  scan clock (Result2).  The plain count of SC5 is 3..7 (Result3 = 1), so the compare is not 0 = 0.

The cases own what ``memory.OWNED`` lists under this group.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.conformance import _runner as runner_module
from pyrung.core import (
    Or,
    Rung,
    copy,
    count_up,
    fill,
    forloop,
    latch,
    math,
    off_delay,
    on_delay,
    out,
    reset,
    rise,
    system,
    toggle,
)
from tests.conformance.memory import ClickCounter, ClickTimer, c, dd, ds, t, td
from tests.conformance.probe_kit import ms_clock, once
from tests.conformance.scans import count_scans, done, scans_for


def prove_oneshot_and_edges(slot):
    count_scans(slot)
    rung, skip, bit, toggled = c[1722], c[1723], c[1724], c[1725]
    with Rung(
        slot.Cmd != 0,
        Or(slot.Scratch == 3, slot.Scratch == 4, slot.Scratch == 5, slot.Scratch == 7),
    ):
        out(rung)
    with Rung(rung):
        with forloop(2, oneshot=True):
            math(slot.Result1 + 1, slot.Result1)
            copy(slot.Scratch, slot.Result2)
    # The bit rises on scan 3, the For's rung false on scans 2..4.  The rung is true
    # with Cmd at 0, so the reset reaches the body's edge.
    with Rung(slot.Cmd != 0, slot.Scratch >= 2, slot.Scratch <= 4):
        out(skip)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3):
        out(bit)
    with Rung(~skip):
        with forloop(1):
            with Rung(rise(bit), slot.Cmd != 0):
                copy(slot.Scratch, slot.Result3)
    # Likewise a toggle's rung: true from Cmd, in a For whose rung is always true.
    with Rung():
        with forloop(2):
            with Rung(slot.Cmd != 0):
                toggle(toggled)
    with Rung(slot.Cmd != 0, slot.Scratch == 3, toggled):
        copy(1, slot.Result4)
    with Rung(slot.Cmd != 0, slot.Scratch == 3, ~toggled):
        copy(2, slot.Result4)
    done(slot)


def init_oneshot_and_edges(slot):
    reset(c[1725])


def prove_timers_at_limits(slot):
    def step(n):
        return (slot.Cmd != 0, slot.Scratch == n)

    clock = ms_clock(slot, 244)
    body_td, tof = td[245], ClickTimer(247)
    counter = ClickCounter(149)
    # The reset writes 0 to the presets, so this rung stages them on the first scan only.
    with Rung(slot.Cmd != 0, ds[3930] == 0):
        copy(-5, ds[3929])
        copy(100, ds[3930])
    with Rung(*step(0)):
        out(c[1726])
    # The For's rung is true with Cmd at 0, so the reset reaches the TON.
    with Rung(Or(slot.Cmd == 0, slot.Scratch != 1)):
        with forloop(1):
            with Rung(slot.Cmd != 0):
                on_delay(ClickTimer(245), 30000)
    with Rung(slot.Cmd != 0):
        on_delay(ClickTimer(246), ds[3929])
    with Rung(c[1726]):
        off_delay(tof, ds[3930])
    with Rung(slot.Cmd != 0, tof.Acc >= 200, ~tof.Done):
        copy(1000, ds[3930])
    with Rung(*once(slot), tof.Acc >= 300, ds[3930] == 1000, tof.Done):
        copy(1, slot.Result3)
    # Below its TON, once the rung is true: a false rung clears the TD.
    with Rung(slot.Cmd != 0):
        on_delay(ClickTimer(248), 30000)
    # Above the write, so these rungs first see C1728 on the scan after it.
    with Rung(*once(slot), c[1728], slot.Result4 == 0, td[248] < 0):
        copy(1, slot.Result4)
    with Rung(*once(slot), c[1728], slot.Result4 == 0, td[248] >= 0):
        copy(2, slot.Result4)
    with Rung(slot.Cmd != 0, ~c[1728]):
        copy(-100, td[248])
        latch(c[1728])
    with Rung(c[1727]):
        count_up(counter, ds[3929]).reset(slot.Cmd == 0)
    with Rung(*step(3), tof.Acc >= 300):
        copy(1, slot.Fired)
    with Rung(*step(2), body_td < 300):
        copy(1, slot.Result1)
    with Rung(*step(2), body_td >= 300):
        copy(2, slot.Result1)
    with Rung(*step(2), t[246]):
        copy(1, slot.Result2)
    with Rung(*step(2), counter.Done):
        copy(1, slot.ErrorCode)
    with Rung(*step(2)):
        copy(3, slot.Scratch)
    with Rung(*step(1), clock >= 400):
        copy(2, slot.Scratch)
    with Rung(*step(0), body_td >= 100):
        copy(1, slot.Scratch)


def prove_clock_edges_in_a_body(slot):
    clock = ms_clock(slot, 122)
    live = (slot.Cmd != 0, clock < 500)
    for edge_clock, plain, double, inner in (
        (system.sys.clock_100ms, dd[78], dd[82], dd[79]),
        (system.sys.scan_clock_toggle, dd[80], dd[83], dd[81]),
    ):
        with Rung(rise(edge_clock), *live):
            math(plain + 1, plain)
            math(double + 2, double)
        # The For's rung is always true, so the reset reaches the body's edge.
        with Rung():
            with forloop(2):
                with Rung(rise(edge_clock), *live):
                    math(inner + 1, inner)
    ready = (*once(slot), clock >= 550)
    with Rung(*ready, dd[79] == dd[78]):
        copy(1, slot.Result1)
    with Rung(*ready, dd[79] == dd[82]):
        copy(2, slot.Result1)
    with Rung(*ready, dd[81] == dd[80]):
        copy(1, slot.Result2)
    with Rung(*ready, dd[81] == dd[83]):
        copy(2, slot.Result2)
    with Rung(*ready, dd[78] >= 3, dd[78] <= 7):
        copy(1, slot.Result3)
    with Rung(*ready):
        copy(1, slot.Fired)


def init_clock_edges_in_a_body(slot):
    fill(0, dd.select(78, 83))


def init_timers_at_limits(slot):
    fill(0, ds.select(3929, 3930))
    copy(0, td[248])
    reset(c[1728])


group = Group(
    "for_more",
    [
        case(
            "A one-shot For of 2 runs on its rung's edges (Result1 = 4, last on scan 7, Result2); a "
            "body's rise() fires when the body comes back on a bit that rose while it was skipped "
            "(Result3 = 5); a toggle in a For of 2 flips once (Result4 = 1)",
            ladder=prove_oneshot_and_edges,
            init=init_oneshot_and_edges,
            scans=scans_for(),
            expect={"Result1": 4, "Result2": 7, "Result3": 5, "Result4": 1},
            probe="a one-shot For; edge memory in a skipped body; a toggle's edge in a For",
        ),
        case(
            "Timer limits: a TON in a body skipped from an Acc of 100 to 400 ms does not take "
            "in the gap (Result1 = 1); a DS preset of -5 is done (Result2 = 1); a TOF's raised "
            "preset brings Done back (Result3 = 1); a TD written -100 is below 0 on the next scan "
            "(Result4 = 1); a counter's DS preset of -5 is done with no count (ErrorCode = 1)",
            ladder=prove_timers_at_limits,
            init=init_timers_at_limits,
            scans=80,
            keep=[t[247], td[247]],
            expect={"Result1": 1, "Result2": 1, "Result3": 1, "Result4": 1, "ErrorCode": 1},
            probe="a timer across a skipped body; presets and an Acc below 0",
        ),
        case(
            "A clock's rise in a For of 2 fires as often as outside it: SC5 (Result1 = 1), SC3 "
            "(Result2 = 1); SC5 rose 3..7 times in 500 ms (Result3 = 1)",
            ladder=prove_clock_edges_in_a_body,
            init=init_clock_edges_in_a_body,
            scans=200,
            expect={"Result1": 1, "Result2": 1, "Result3": 1},
            probe="a clock's edge in a For body, read by hardware only outside one",
        ),
    ],
)


def test_for_more():
    assert_all_passed(run(group))


@pytest.mark.parametrize("dt", [0.0004, 0.001, 0.010, 0.025, 0.040])
def test_timers_at_limits_at_any_scan(monkeypatch, dt):
    """The CLICK scans in about 1 ms or less, the soft PLC in 10..40 ms."""
    monkeypatch.setattr(runner_module, "DT", dt)
    limits = next(c for c in group.cases if c.id == "timers_at_limits")
    assert_all_passed(run(Group("for_more", [replace(limits, scans=int(0.8 / dt))])))
