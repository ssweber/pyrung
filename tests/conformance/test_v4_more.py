"""The v4 Out family, For and timers, past what ``out``, ``loop`` and ``timers`` read.

Part L (``suite.PARTS``), the groups ``out_more``, ``for_more`` and ``call_more``.  An
expectation is pyrung's reading today; a CLICK that reads otherwise prints what it read.  A
field whose reading depends on how long a scan takes (a preset of 0 or below) expects a range
over its candidates, and the harness prints what it read.  Clock windows hold for any scan up
to 50 ms, as in ``out``.

``out_more``:

- ``toggle_and_timed``: ``out`` reads a toggle's first flip and a timed coil's held rung.  A toggle
  over two edges, scan 3 reads it on, scan 5 with the rung false, scan 7 past the second edge:
  Result1 = 110, by digit.  100 if the rung's fall turns it off, 111 if the second edge is
  lost.  A timed coil of 300 ms, its rung on 0-100 ms and again 150-250 ms: Result2 at 360-440 ms
  is 0 where the second edge does nothing, 1 where it restarts the time.  Its rung back after the
  time is up, at 500-700 ms: on (ErrorCode = 1), 0 where it needs more than an edge.  TD of a held
  timed coil past its 200 ms preset, read at 500-590 ms: Result3 = 1 stopped near the preset, 2
  counting on as a debounce's does, 3 cleared.  After its rung falls, at 700-900 ms: Result4 = 1
  cleared, 2 kept.
- ``timed_maintained``: nothing has read a maintained coil.  Its rung gone at 100 ms into a 400 ms
  time: on at 200-350 ms (Result1 = 1), 0 where a maintained coil follows its rung throughout;
  off by 500 ms (Result2 = 0), 1 where it holds.  Its rung held past a 200 ms time: on at 400-590
  ms (Result3 = 1), 0 where it pulses as the plain one; rung gone at 600 ms, off by 700 ms
  (Result4 = 0), 1 where it stays.
- ``zero_and_below``: a preset of 0, scan-paced, the rung true on scans 3..5.  A debounce of 0/0:
  first on (Result1), first off past it (Result2); 3 and 6 are the rung's own scans, later ones
  mean a scan's lag.  A flasher of 0/0: scans on of 8 (Result3): a scan each way is 2, on throughout 3,
  once 1.  A timed coil of 0: scans on (Result4), 1 a one-scan pulse, 0 none.  A timed coil from a DS
  holding -5: scans on (ErrorCode), 1 as a preset of 0, 3 (the rung's) where -5 reads as unsigned,
  65531 ms.
- ``preset_mid_phase``: a preset in a DS moved mid-phase, read live or at the phase's start.  A
  flasher's on side from 1000 down to 100 ms at 300 ms: at 420-600 ms the coil is off (Result1 =
  0) where it reads live, on (1) where it took the preset at the start.  A debounce's on side
  from 200 up to 2000 ms at 150 ms: still off at 300-600 ms (Result2 = 0) live, on (1) if taken at
  the start.  A flasher's off TD through its second on phase: Result3 = 1 holds its last value, 2
  cleared.  A debounce's on TD, its rung gone at 150 ms: at 300-500 ms Result4 = 1 cleared, 2 kept.
  The reset writes 0 to the first flasher's off TD (TD240), which a false rung only freezes: what
  an off phase entered through a lowered preset does with a TD left mid-phase is not this case's
  question.  pyrung runs on from it, so a reset that froze TD240 at 700-960 ms lit the coil again
  inside 420-600 ms, about one repeat in four (the coil's phase at the reset is wall clock).

``for_more``:

- ``oneshot_and_edges``: a one-shot For of 2, its rung true on scans 3..5 and 7: runs on 3 and 7,
  Result1 = 4, Result2 = 7 (last scan).  Each scan true is 8; no re-arm is 2 and 3.  A bit rises on
  scan 3, the For's rung false on 2..4: a body's rise() fires on the scan the body comes back
  (Result3 = 5), 0 where its memory tracks in a skipped body.  A toggle in a For of 2, rung held:
  on (Result4 = 1) if it flips once, off (2) if each pass is an edge.
- ``timers_at_limits``: a TON in a body skipped from 100 to 400 ms: at 450-550 ms its Acc is under
  300 (Result1 = 1), 2 where the first scan back takes in the gap.  ``loop`` reads only that it holds
  while skipped.  A TON from a DS of -5: done at 100-200 ms (Result2 = 1), 0 where -5 reads unsigned.
  A TOF of 100 ms timed out, its DS raised to 1000 at 300 ms: Done back on at 400-600 ms
  (Result3 = 1), 0 where a timed-out TOF stays out.  A TD written -100, its TON true from 50 ms:
  below 0 at 60-130 ms (Result4 = 1), 2 where it starts at or above 0.  A counter from a DS of
  -5, no count: done at 100-200 ms (ErrorCode = 1), 0 where -5 reads unsigned.

- ``clock_edges_in_a_body``: ``system/clock_bits_toggle_at_their_rate`` reads a clock's rise in a
  subroutine; in a For body nothing has.  rise(SC5) above a For of 2 against the same in its body,
  over 500 ms: Result1 = 1 same count, 2 double (each pass an edge), 0 else.  The same for SC3, the
  scan clock (Result2).  The plain count of SC5 is 3..7 (Result3 = 1), so the compare is not 0 = 0.

``call_more``, called twice a scan (``Group(calls=2)``; ``Scratch`` flips on each call):

- ``called_twice``: a TON on both calls against an RTON on the second: Result1 = 1 for ahead by
  half again (a timer takes the scan's time per execution, as in a For), 2 for level (it takes it
  once a scan).  A counter's input held across calls: 1 (Result2), more if each scan or call were
  an edge.  Input on the second call only: counts (Result3 = 1), 0 where it is compared at scan
  end, which sees it high each time.  A time drum on both calls, Result4 as Result1.

The cases own what ``memory.OWNED`` lists under these groups.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import (
    Or,
    Rung,
    calc,
    copy,
    count_up,
    debounce,
    fill,
    flasher,
    forloop,
    latch,
    off_delay,
    on_delay,
    out,
    reset,
    rise,
    system,
    time_drum,
    timed,
    toggle,
)
from tests.conformance.memory import ClickCounter, ClickTimer, c, ctd, dd, ds, t, td
from tests.conformance.probe_kit import between, once
from tests.conformance.scans import count_scans, done, scans_for


def _clock(slot, n, preset=5000):
    with Rung(slot.Cmd != 0):
        on_delay(ClickTimer(n), preset)
    return td[n]


def _at(slot, clock, low, high):
    return (*once(slot), clock >= low, clock < high)


def _end(slot, clock, ms):
    with Rung(*once(slot), clock >= ms):
        copy(1, slot.Fired)


def prove_toggle_and_timed(slot):
    count_scans(slot)
    coil, rung = c[1702], c[1703]
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 2, slot.Scratch == 3, slot.Scratch >= 6)):
        out(rung)
    with Rung(rung):
        toggle(coil)
    with Rung(slot.Cmd != 0, slot.Scratch == 3, coil):
        calc(slot.Result1 + 100, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == 5, coil):
        calc(slot.Result1 + 10, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == 7, coil):
        calc(slot.Result1 + 1, slot.Result1)
    clock = _clock(slot, 226)
    pulse_rung, pulse, held = c[1704], c[1705], c[1706]
    with Rung(slot.Cmd != 0, Or(clock < 100, clock >= 500)):
        out(pulse_rung)
    with Rung(slot.Cmd != 0, clock >= 150, clock < 250):
        out(c[1717])
    with Rung(Or(pulse_rung, c[1717]), clock < 700):
        timed(pulse, ClickTimer(227), 300)
    with Rung(*_at(slot, clock, 360, 440), pulse):
        copy(1, slot.Result2)
    with Rung(*_at(slot, clock, 500, 700), pulse):
        copy(1, slot.ErrorCode)
    with Rung(slot.Cmd != 0, clock < 600):
        timed(held, ClickTimer(228), 200)
    with Rung(*_at(slot, clock, 500, 590), td[228] >= 200, td[228] < 300):
        copy(1, slot.Result3)
    with Rung(*_at(slot, clock, 500, 590), td[228] >= 300):
        copy(2, slot.Result3)
    with Rung(*_at(slot, clock, 500, 590), td[228] == 0):
        copy(3, slot.Result3)
    with Rung(*_at(slot, clock, 700, 900), td[228] == 0):
        copy(1, slot.Result4)
    with Rung(*_at(slot, clock, 700, 900), td[228] >= 200):
        copy(2, slot.Result4)
    _end(slot, clock, 950)


def init_toggle_and_timed(slot):
    reset(c[1702])


def prove_timed_maintained(slot):
    clock = _clock(slot, 229)
    dropped, held = c[1707], c[1708]
    with Rung(slot.Cmd != 0, clock < 100):
        timed(dropped, ClickTimer(230), 400, maintained=True)
    with Rung(slot.Cmd != 0, clock < 600):
        timed(held, ClickTimer(231), 200, maintained=True)
    with Rung(*_at(slot, clock, 200, 350), dropped):
        copy(1, slot.Result1)
    with Rung(*_at(slot, clock, 500, 590), dropped):
        copy(1, slot.Result2)
    with Rung(*_at(slot, clock, 400, 590), held):
        copy(1, slot.Result3)
    with Rung(*_at(slot, clock, 700, 900), held):
        copy(1, slot.Result4)
    _end(slot, clock, 950)


def prove_zero_and_below(slot):
    rung, deb, fla, pulse, neg = c[1709], c[1710], c[1712], c[1713], c[1714]
    window = (slot.Cmd != 0, slot.Scratch >= 1, slot.Scratch <= 8)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(-5, ds[3926])
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 5):
        out(rung)
    with Rung(rung):
        debounce(deb, on=(ClickTimer(232), 0), off=(ClickTimer(233), 0))
    with Rung(rung):
        flasher(fla, on=(ClickTimer(234), 0), off=(ClickTimer(235), 0))
    with Rung(rung):
        timed(pulse, ClickTimer(236), 0)
    with Rung(rung):
        timed(neg, ClickTimer(237), ds[3926])
    with Rung(*window, deb, slot.Result1 == 0):
        copy(slot.Scratch, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 8, ~deb, slot.Result2 == 0):
        copy(slot.Scratch, slot.Result2)
    with Rung(*window, fla):
        calc(slot.Result3 + 1, slot.Result3)
    with Rung(*window, pulse):
        calc(slot.Result4 + 1, slot.Result4)
    with Rung(*window, neg):
        calc(slot.ErrorCode + 1, slot.ErrorCode)
    done(slot)


def init_zero_and_below(slot):
    copy(0, ds[3926])


def prove_preset_mid_phase(slot):
    clock = _clock(slot, 238)
    fla, deb, fla2, deb2 = c[1715], c[1716], c[1718], c[1719]
    with Rung(slot.Cmd != 0, clock < 150):
        copy(1000, ds[3927])
        copy(200, ds[3928])
    with Rung(slot.Cmd != 0, clock >= 150):
        copy(2000, ds[3928])
    with Rung(slot.Cmd != 0, clock >= 300):
        copy(100, ds[3927])
    with Rung(slot.Cmd != 0):
        flasher(fla, on=(ClickTimer(239), ds[3927]), off=(ClickTimer(240), 1000))
    with Rung(slot.Cmd != 0):
        debounce(deb, on=(ClickTimer(241), ds[3928]), off=(ClickTimer(242), 1000))
    with Rung(slot.Cmd != 0):
        flasher(fla2, on=(ClickTimer(225), 100), off=(ClickTimer(113), 100))
    with Rung(slot.Cmd != 0, clock < 150):
        debounce(deb2, on=(ClickTimer(114), 2000), off=(ClickTimer(115), 2000))
    with Rung(*_at(slot, clock, 420, 600), fla):
        copy(1, slot.Result1)
    with Rung(*_at(slot, clock, 300, 600), deb):
        copy(1, slot.Result2)
    # The coil on at 250 ms or later is the second on phase: the first ended by 150.
    with Rung(*once(slot), clock >= 250, fla2, td[113] >= 100):
        copy(1, slot.Result3)
    with Rung(*once(slot), clock >= 250, fla2, td[113] == 0):
        copy(2, slot.Result3)
    with Rung(*_at(slot, clock, 300, 500), td[114] == 0):
        copy(1, slot.Result4)
    with Rung(*_at(slot, clock, 300, 500), td[114] > 0):
        copy(2, slot.Result4)
    _end(slot, clock, 650)


def init_preset_mid_phase(slot):
    fill(0, ds.select(3927, 3928))
    # A false rung freezes the flasher's TDs.  An on phase cut short by its lowered
    # preset goes straight to the off phase, and pyrung runs that from the frozen TD.
    copy(0, td[240])


group = Group(
    "out_more",
    [
        case(
            "A toggle holds and flips on its next edge (Result1 = 110); a timed coil's second edge "
            "does not restart it (Result2 = 0), a later one gives a new pulse (ErrorCode = 1); its "
            "TD stops near the preset (Result3 = 1), clearing with the rung (Result4 = 1)",
            ladder=prove_toggle_and_timed,
            init=init_toggle_and_timed,
            scans=200,
            expect={"Result1": 110, "Result2": 0, "Result3": 1, "Result4": 1, "ErrorCode": 1},
            probe="toggle's next edge; a timed coil's restart, new pulse and TD",
        ),
        case(
            "A maintained timed coil runs its time out with the rung gone: on at 200-350 ms "
            "(Result1 = 1), off by 500 ms (Result2 = 0); past its time it follows a held rung, on "
            "(Result3 = 1), then off when it falls (Result4 = 0)",
            ladder=prove_timed_maintained,
            scans=200,
            expect={"Result1": 1, "Result2": 0, "Result3": 1, "Result4": 0},
            probe="a maintained timed coil, read by nothing yet",
        ),
        case(
            "Out coils with a preset of 0 and below: a debounce follows its rung (first on, Result1 "
            "= 3; first off past it, Result2 = 6); scans on of a flasher (Result3), a timed coil "
            "(Result4), a timed coil from a DS of -5 (ErrorCode)",
            ladder=prove_zero_and_below,
            init=init_zero_and_below,
            scans=scans_for(),
            keep=[t[233], td[233], t[234], td[234], t[235], td[235]],
            expect={
                "Result1": between(3, 4),
                "Result2": between(6, 7),
                "Result3": between(0, 3),
                "Result4": between(0, 3),
                "ErrorCode": between(0, 3),
            },
            probe="an Out coil with a preset of 0, or below 0 from a DS",
        ),
        case(
            "An Out coil's DS preset reads live: a flasher's lowered at 300 ms ends the on phase "
            "(Result1 = 0), a debounce's raised at 150 ms holds it off (Result2 = 0); a flasher's "
            "off TD holds into the next on phase (Result3 = 1), a debounce's on TD clears (Result4 = 1)",
            ladder=prove_preset_mid_phase,
            init=init_preset_mid_phase,
            ready=lambda slot: ~c[1716],
            scans=200,
            keep=[
                t[239],
                td[239],
                t[242],
                td[242],
                t[225],
                td[225],
                t[113],
                td[113],
                t[115],
                td[115],
            ],
            expect={"Result1": 0, "Result2": 0, "Result3": 1, "Result4": 1},
            probe="an Out coil's DS preset, live or as its phase began; each side's TD",
        ),
    ],
)


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
            calc(slot.Result1 + 1, slot.Result1)
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
    clock = _clock(slot, 244)
    with Rung(slot.Cmd != 0, clock < 50):
        copy(-5, ds[3929])
        copy(100, ds[3930])
        out(c[1726])
    with Rung(slot.Cmd != 0, clock >= 300):
        copy(1000, ds[3930])
    # The For's rung is true with Cmd at 0, so the reset reaches the TON.
    with Rung(Or(slot.Cmd == 0, clock < 100, clock >= 400)):
        with forloop(1):
            with Rung(slot.Cmd != 0):
                on_delay(ClickTimer(245), 30000)
    with Rung(*_at(slot, clock, 450, 550), td[245] < 300):
        copy(1, slot.Result1)
    with Rung(*_at(slot, clock, 450, 550), td[245] >= 300):
        copy(2, slot.Result1)
    with Rung(slot.Cmd != 0, clock >= 50):
        on_delay(ClickTimer(246), ds[3929])
    with Rung(*_at(slot, clock, 100, 200), t[246]):
        copy(1, slot.Result2)
    with Rung(c[1726]):
        off_delay(ClickTimer(247), ds[3930])
    with Rung(*_at(slot, clock, 400, 600), t[247]):
        copy(1, slot.Result3)
    # Below its TON, once the rung is true: a false rung clears the TD.
    with Rung(slot.Cmd != 0, clock >= 50):
        on_delay(ClickTimer(248), 30000)
    with Rung(slot.Cmd != 0, clock >= 50, ~c[1728]):
        copy(-100, td[248])
        latch(c[1728])
    with Rung(*_at(slot, clock, 60, 130), td[248] < 0):
        copy(1, slot.Result4)
    with Rung(*_at(slot, clock, 60, 130), td[248] >= 0):
        copy(2, slot.Result4)
    with Rung(c[1727]):
        count_up(ClickCounter(149), ds[3929]).reset(slot.Cmd == 0)
    with Rung(*_at(slot, clock, 100, 200), ClickCounter(149).Done):
        copy(1, slot.ErrorCode)
    _end(slot, clock, 650)


def prove_clock_edges_in_a_body(slot):
    clock = _clock(slot, 122)
    live = (slot.Cmd != 0, clock < 500)
    for edge_clock, plain, double, inner in (
        (system.sys.clock_100ms, dd[78], dd[82], dd[79]),
        (system.sys.scan_clock_toggle, dd[80], dd[83], dd[81]),
    ):
        with Rung(rise(edge_clock), *live):
            calc(plain + 1, plain)
            calc(double + 2, double)
        # The For's rung is always true, so the reset reaches the body's edge.
        with Rung():
            with forloop(2):
                with Rung(rise(edge_clock), *live):
                    calc(inner + 1, inner)
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


for_group = Group(
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
            "Timer limits: a TON in a body skipped 300 ms does not take in the gap (Result1 = 1); "
            "a DS preset of -5 is done (Result2 = 1); a TOF's raised preset brings Done back "
            "(Result3 = 1); a TD written -100 stays below 0 (Result4 = 1); a counter's DS "
            "preset of -5 is done with no count (ErrorCode = 1)",
            ladder=prove_timers_at_limits,
            init=init_timers_at_limits,
            scans=200,
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


call_group = Group(
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


def test_out_more():
    assert_all_passed(run(group))


@pytest.mark.parametrize("scans", range(200, 310, 10))
def test_preset_mid_phase_holds_wherever_the_last_run_stopped(scans):
    """One flasher cycle (1100 ms) in 100 ms steps: where the reset cuts the off phase."""
    mid_phase = next(c for c in group.cases if c.id == "preset_mid_phase")
    assert_all_passed(run(Group("out_more", [replace(mid_phase, scans=scans)])))


def test_for_more():
    assert_all_passed(run(for_group))


def test_call_more():
    assert_all_passed(run(call_group))
