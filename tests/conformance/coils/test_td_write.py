"""A TD written by the ladder into a running Out coil variant: the ``td_write`` group (part
``follow_extra``).

A CLICK's debounce and flasher keep their state in their TDs: they never set T (probe O6) and
their TDs freeze while the rung is false.  A TON counts on from a TD the ladder writes
(``timers/acc_clamps_at_32767``).  Does a debounce, which counts its sides in its TDs, or a
flasher, whose coil reads its phase from its TDs alone in pyrung, move with a TD written while it
runs?  A clock (a TON from ``Cmd``, its TD in ms) times each write, once the side is timing and
not on its first scan.

``debounce_on_td_write``: a debounce of 500 ms on, 50 ms off, its rung on.  At 100 ms the ladder
writes its on TD to 500, the preset, once.

- Result1, the clock when the coil first reads on.  Near 100 (pyrung, a scan or two past the
  write): the write counted, the side is done at once.  Near 500 or past it: the coil waits for
  its own time, the write is ignored or lost.  0 for never.
- Result2, the on TD at 150 ms.  Near 550: the write was counted on from.  Near 150: the TD is
  shown the write but the side keeps its own count.  500: clamped to the preset.  Near 50: cleared.
- Result3, the on TD at 700 ms.  About 1100 (pyrung): counted on from 500 and still running with
  the rung.  About 700: the write left no mark.  500: held at the preset.

``flasher_on_td_write``: a flasher of 500 ms on, 500 ms off, its rung on.  At 100 ms the ladder
writes its on TD to 600, past the on preset, once.

- Result1, the clock when the coil first reads off (from 100 ms).  Near 100 (pyrung): the on phase
  ended at once.  Near 500: the coil runs its own time.  0 for never.
- Result2, the off TD at 150 ms.  Near 50 (pyrung): the off phase began from 0 at the write.  0:
  it has not begun.  Else: something else.
- Result3, the on TD at 150 ms.  600 (pyrung): the written value stands through the off phase.
  Near 150: the TD ignored the write.  0: cleared.
- ErrorCode, the coil at 700 ms: 1 on, 2 off.  pyrung: on (off phase 100..600, on again at 600).
  A flasher that ignores the write: off (off phase 500..1000).

``flasher_off_td_write``: a flasher of 200 ms on, 500 ms off, its rung on.  Its off phase is
200..700 ms.  At 350 ms the ladder writes its off TD to 600, past the off preset, once.

- Result1, the clock when the coil first reads on again (from 350 ms, 0 for never by 1000 ms).
  Near 350: the off phase ended at once and the on phase began.  Near 700: the coil runs its own
  time.  Near 860 (pyrung): the write ended the off phase, but the on TD, held past its preset
  of 200 through the off phase, ends the on phase on the spot and a new off phase begins from 0.
- Result2, the off TD at 420 ms.  600: the written value stands.  Near 60 (pyrung): the off phase
  began again from 0.  Near 220: the TD ignored the write, or took it as its own count.  0:
  cleared.
- Result3, the on TD at 420 ms.  210 (pyrung): held from the on phase.  Near 60: the on phase
  began from 0 at the write.  0: cleared.

pyrung's readings are pinned in ``test_pyrung_follows_a_written_td``, not in ``expect``: every
field is a ``between`` range, so no run confirms a case.  The windows hold for any scan up to 40
ms.  The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, debounce, flasher
from tests.conformance.memory import ClickTimer, c, td
from tests.conformance.probe_kit import between, end_at, ms_clock, once

TD_MAX = 32767


def prove_debounce_on_td_write(slot: Any) -> None:
    coil, on_td = c[261], td[150]
    clock = ms_clock(slot, 152)
    with Rung(slot.Cmd != 0):
        debounce(coil, on=(ClickTimer(150), 500), off=(ClickTimer(151), 50))
    with Rung(slot.Cmd != 0, clock >= 100):
        copy(500, on_td, oneshot=True)
    with Rung(*once(slot), coil):
        copy(clock, slot.Result1, oneshot=True)
    with Rung(*once(slot), clock >= 150):
        copy(on_td, slot.Result2, oneshot=True)
    with Rung(*once(slot), clock >= 700):
        copy(on_td, slot.Result3, oneshot=True)
    end_at(slot, clock, 700)


def prove_flasher_on_td_write(slot: Any) -> None:
    coil, on_td, off_td = c[262], td[153], td[154]
    clock = ms_clock(slot, 155)
    with Rung(slot.Cmd != 0):
        flasher(coil, on=(ClickTimer(153), 500), off=(ClickTimer(154), 500))
    with Rung(slot.Cmd != 0, clock >= 100):
        copy(600, on_td, oneshot=True)
    with Rung(*once(slot), clock >= 100, ~coil):
        copy(clock, slot.Result1, oneshot=True)
    with Rung(*once(slot), clock >= 150):
        copy(off_td, slot.Result2, oneshot=True)
        copy(on_td, slot.Result3, oneshot=True)
    with Rung(*once(slot), clock >= 700, coil):
        copy(1, slot.ErrorCode)
    with Rung(*once(slot), clock >= 700, ~coil):
        copy(2, slot.ErrorCode)
    end_at(slot, clock, 700)


def prove_flasher_off_td_write(slot: Any) -> None:
    coil, on_td, off_td = c[263], td[156], td[157]
    clock = ms_clock(slot, 158)
    with Rung(slot.Cmd != 0):
        flasher(coil, on=(ClickTimer(156), 200), off=(ClickTimer(157), 500))
    with Rung(slot.Cmd != 0, clock >= 350):
        copy(600, off_td, oneshot=True)
    with Rung(*once(slot), clock >= 350, coil):
        copy(clock, slot.Result1, oneshot=True)
    with Rung(*once(slot), clock >= 420):
        copy(off_td, slot.Result2, oneshot=True)
        copy(on_td, slot.Result3, oneshot=True)
    end_at(slot, clock, 1000)


group = Group(
    "td_write",
    [
        case(
            "A debounce whose on TD the ladder writes to its preset (500) at 100 ms while the "
            "rung is on: the clock when the coil first reads on (Result1, near 100 where the "
            "write counts, near 500 where the side keeps its own time) and the on TD at "
            "150 ms and 700 ms (Result2, Result3)",
            ladder=prove_debounce_on_td_write,
            ready=lambda slot: ~c[261],
            # The TDs of a debounce are kept after its rung falls; the next rise starts the on
            # side from 0.
            keep=(td[150], td[151]),
            expect={
                "Result1": between(0, TD_MAX),
                "Result2": between(0, TD_MAX),
                "Result3": between(0, TD_MAX),
            },
            scans=100,
            fires_within=1.5,
            probe="a TD written into a running debounce's on side: counted or not (5)",
        ),
        case(
            "A flasher whose on TD the ladder writes past its preset (600 of 500) at 100 ms "
            "during the on phase: the clock when the coil first reads off (Result1, near 100 "
            "where the write counts, near 500 where the flasher runs its own time), the off TD "
            "and the on TD at 150 ms (Result2, Result3) and the coil at 700 ms "
            "(ErrorCode = 1 on, 2 off)",
            ladder=prove_flasher_on_td_write,
            ready=lambda slot: ~c[262],
            keep=(td[153], td[154]),
            expect={
                "Result1": between(0, TD_MAX),
                "Result2": between(0, TD_MAX),
                "Result3": between(0, TD_MAX),
                "ErrorCode": between(1, 2),
            },
            scans=100,
            fires_within=1.5,
            probe="a TD written into a flasher's on phase: the off phase at once or not (5)",
        ),
        case(
            "A flasher whose off TD the ladder writes past its preset (600 of 500) at 350 ms "
            "during the off phase: the clock when the coil first reads on again (Result1, near "
            "350 where the on phase begins, near 700 where the flasher runs its own time) and "
            "the off TD and the on TD at 420 ms (Result2, Result3)",
            ladder=prove_flasher_off_td_write,
            ready=lambda slot: ~c[263],
            keep=(td[156], td[157]),
            expect={
                "Result1": between(0, TD_MAX),
                "Result2": between(0, TD_MAX),
                "Result3": between(0, TD_MAX),
            },
            scans=130,
            fires_within=2.0,
            probe="a TD written into a flasher's off phase: the on phase at once or not (5)",
        ),
    ],
)


def test_td_write():
    assert_all_passed(run(group))


def test_pyrung_follows_a_written_td():
    """What pyrung reads today (every ``expect`` is a ``between``, so this is the pin)."""
    read = {r.case.id: r.actual for r in run(group)}
    assert 100 <= read["debounce_on_td_write"]["Result1"] <= 140
    assert 540 <= read["debounce_on_td_write"]["Result2"] <= 600
    assert 1050 <= read["debounce_on_td_write"]["Result3"] <= 1150
    flash = read["flasher_on_td_write"]
    assert 100 <= flash["Result1"] <= 140
    assert 20 <= flash["Result2"] <= 80
    assert flash["Result3"] == 600
    assert flash["ErrorCode"] == 1
    off = read["flasher_off_td_write"]
    # The on TD, held past its preset, ends the on phase at once: a new off phase from 0.
    assert 840 <= off["Result1"] <= 900
    assert 30 <= off["Result2"] <= 90
    assert 200 <= off["Result3"] <= 220
