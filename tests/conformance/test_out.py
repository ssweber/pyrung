"""Out coil variants: the ``out`` group (subroutine ``Conf_Out``).

Each coil is a C bit and each phase a raw Timer pair, ``T<n>`` with ``TD<n>``,
which is what CLICK's Out dialog names (its TD) and claims (the whole timer
number).  The cases own C1801..C1900 and timers T301..T400 (``memory.OWNED``).
A case that needs its input to drop or bounce at a known time drives it from a
clock timer of its own.

Every coil's rung is gated on ``Cmd``, so the reset drops each coil's input.
A debounce coil then turns off only when its off side times out, so those
cases are ``ready`` once the coil is off, and ``keep`` the off side's timer:
its TD keeps counting while the input stays off, from power-up on, so it never
matches a fresh PLC's.

The cases are the probes O1-O6 of ``scratchpad/click-v4/RESEARCH.md``.  Hardware
round 1 (2026-10-29) confirmed O1, O2, O3 and O5 and answered O6: an Out coil
never sets its Timer's done bit T<n>; the coil's phases come from the TDs
alone, so a contact on that T<n> is always off.  pyrung sets it
(``pending="OUT"``).  O4 is still open:

- ``flasher_restarts_with_on_phase`` (probe): 300 ms phases, the rung false
  from 450 to 550 ms, 150 ms into the off phase.  Round 1 read "not both TDs
  0" and the coil on somewhere in 600-800 ms.  The case reads, while the rung
  is false, the on-side TD (Result1: 1 = cleared, 2 = held at or past its
  preset) and the off-side TD (Result3: 1 = cleared, 2 = kept mid-phase), and
  whether the off-side TD stands still (ErrorCode: 1 = frozen or cleared,
  2 = still counting); after the restart, the coil at 600-650 ms (Result2)
  and at 750-800 ms (Result4).  Adopted, the flasher freezes and resumes:
  2, 2, 1, then off (0) and on (1), the off phase's last 150 ms then a new on
  phase.  pyrung clears both TDs and restarts with an on phase: 1, 1, 1, 1, 1.
  A CLICK keeps both TDs, frozen, yet restarts with an on phase: 2, 1, 2, 1, 1
  (hardware round 2, 2026-10-30).
  A fresh off phase reads Result2 = 0, Result4 = 0.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, debounce, flasher, on_delay, out, reset, timed, toggle
from tests.conformance.memory import ClickTimer, c, t, td


def prove_toggle_flips_once_per_edge(slot):
    # O1: the rung goes true and stays true; the bit flips once.
    with Rung(slot.Cmd != 0):
        toggle(c[1801])
    with Rung(slot.Cmd != 0, c[1801]):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, ~c[1801]):
        copy(2, slot.Result1)
    with Rung(slot.Cmd != 0):
        copy(1, slot.Fired, oneshot=True)


def prove_debounce_td_keeps_counting(slot):
    # O2: the on side fires at 100 ms; does its TD carry on past the preset?
    with Rung(slot.Cmd != 0):
        debounce(c[1811], on=(ClickTimer(301), 100), off=(ClickTimer(311), 1000))
    with Rung(slot.Cmd != 0, c[1811]):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, c[1811], td[301] >= 300):
        copy(1, slot.Result2, oneshot=True)
    with Rung(slot.Cmd != 0):
        copy(1, slot.Fired, oneshot=True)


def prove_debounce_bounce_restarts_on_side(slot):
    # O3: the input is on 0-200 ms, off 200-300 ms, then on.  With a 300 ms on
    # side that restarts on the bounce the coil comes on near 600 ms, not near
    # 400 ms (the off side, 1000 ms, never fires).
    with Rung(slot.Cmd != 0):
        on_delay(ClickTimer(302), 2000)
    with Rung(slot.Cmd != 0, td[302] >= 200, td[302] < 300):
        out(c[1822])
    with Rung(slot.Cmd != 0, ~c[1822]):
        debounce(c[1821], on=(ClickTimer(303), 300), off=(ClickTimer(304), 1000))
    with Rung(slot.Cmd != 0, td[302] >= 450, td[302] < 550, c[1821]):
        copy(1, slot.Result2, oneshot=True)
    with Rung(slot.Cmd != 0, c[1821]):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0):
        copy(1, slot.Fired, oneshot=True)


def prove_flasher_restarts_with_on_phase(slot):
    # O4: 300 ms phases; the rung drops at 450-550 ms, 150 ms into the off
    # phase.  Each check's window keeps clear of the phase edges and is wide
    # enough that one long wall-clock scan doesn't step over it.
    with Rung(slot.Cmd != 0):
        on_delay(ClickTimer(305), 2000)
    with Rung(slot.Cmd != 0, td[305] >= 450, td[305] < 550):
        out(c[1832])
    with Rung(slot.Cmd != 0, ~c[1832]):
        flasher(c[1831], on=(ClickTimer(306), 300), off=(ClickTimer(307), 300))
    # While the rung is false: 1 = TD cleared, 2 = TD kept.
    with Rung(slot.Cmd != 0, td[305] >= 480, td[305] < 520, td[306] == 0):
        copy(1, slot.Result1, oneshot=True)
    with Rung(slot.Cmd != 0, td[305] >= 480, td[305] < 520, td[306] >= 300):
        copy(2, slot.Result1, oneshot=True)
    with Rung(slot.Cmd != 0, td[305] >= 480, td[305] < 520, td[307] == 0):
        copy(1, slot.Result3, oneshot=True)
    with Rung(slot.Cmd != 0, td[305] >= 480, td[305] < 520, td[307] >= 100, td[307] < 300):
        copy(2, slot.Result3, oneshot=True)
    # Does the off-side TD stand still while the rung is false?
    with Rung(slot.Cmd != 0, td[305] >= 460, td[305] < 480):
        copy(td[307], slot.Scratch, oneshot=True)
    with Rung(slot.Cmd != 0, td[305] >= 520, td[305] < 540, td[307] == slot.Scratch):
        copy(1, slot.ErrorCode, oneshot=True)
    with Rung(slot.Cmd != 0, td[305] >= 520, td[305] < 540, td[307] != slot.Scratch):
        copy(2, slot.ErrorCode, oneshot=True)
    # After the restart at 550: a fresh on phase is on at 600-650 and 750-800;
    # resuming the off phase is off at 600-650, on at 750-800; a fresh off
    # phase is off at both.
    with Rung(slot.Cmd != 0, td[305] >= 600, td[305] < 650, c[1831]):
        copy(1, slot.Result2, oneshot=True)
    with Rung(slot.Cmd != 0, td[305] >= 750, td[305] < 800, c[1831]):
        copy(1, slot.Result4, oneshot=True)
    with Rung(slot.Cmd != 0, td[305] >= 850):
        copy(1, slot.Fired, oneshot=True)


def prove_timed_needs_fresh_edge(slot):
    # O5: a 200 ms pulse with the rung held; the coil does not come back on.
    with Rung(slot.Cmd != 0):
        on_delay(ClickTimer(308), 2000)
    with Rung(slot.Cmd != 0):
        timed(c[1841], ClickTimer(309), 200)
    with Rung(slot.Cmd != 0, c[1841]):
        copy(1, slot.Result1, oneshot=True)
    with Rung(slot.Cmd != 0, td[308] >= 400, c[1841]):
        copy(1, slot.Result2, oneshot=True)
    with Rung(slot.Cmd != 0):
        copy(1, slot.Fired, oneshot=True)


def prove_out_coil_sets_timer_done(slot):
    # O6: does the coil write its T<n>?  Result2 shows the on phase finished.
    with Rung(slot.Cmd != 0):
        debounce(c[1851], on=(ClickTimer(310), 200), off=(ClickTimer(312), 1000))
    with Rung(slot.Cmd != 0, t[310]):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, c[1851], td[310] >= 300):
        copy(1, slot.Result2, oneshot=True)
    with Rung(slot.Cmd != 0):
        copy(1, slot.Fired, oneshot=True)


def init_toggle_flips_once_per_edge(slot):
    reset(c[1801])


group = Group(
    "out",
    [
        case(
            "A toggle coil flips once when its rung goes true and holds while it stays "
            "true (Result1 = 1 for on)",
            ladder=prove_toggle_flips_once_per_edge,
            init=init_toggle_flips_once_per_edge,
            expect={"Result1": 1},
            scans=20,
        ),
        case(
            "A debounce coil's on-side TD keeps counting past its preset while the input "
            "stays on, as a TON's does (Result2 = 1 once it passes 300 ms on a 100 ms "
            "preset)",
            ladder=prove_debounce_td_keeps_counting,
            ready=lambda slot: ~c[1811],
            keep=[t[311], td[311]],
            expect={"Result1": 1, "Result2": 1},
            scans=50,
        ),
        case(
            "A bounce restarts a debounce coil's on-side timing: on for 200 ms, off for "
            "100 ms, then on with a 300 ms on side, the coil is still off at 450-550 ms "
            "(Result2 = 0) and on later (Result1 = 1)",
            ladder=prove_debounce_bounce_restarts_on_side,
            ready=lambda slot: ~c[1821],
            keep=[t[304], td[304]],
            expect={"Result1": 1, "Result2": 0},
            scans=80,
        ),
        case(
            "A flasher whose rung goes false freezes, then restarts with an on phase: 150 ms "
            "into its off phase, its on-side TD is kept (Result1 = 2) and its off-side TD "
            "kept (Result3 = 2) and standing still (ErrorCode = 1); back on, it is on at "
            "600-650 ms (Result2 = 1) and at 750-800 ms (Result4 = 1)",
            ladder=prove_flasher_restarts_with_on_phase,
            expect={"Result1": 2, "Result2": 1, "Result3": 2, "Result4": 1, "ErrorCode": 1},
            scans=100,
            pending="OUT",
            probe="flasher with its rung false: TDs cleared, kept or frozen; restart phase",
        ),
        case(
            "A timed coil (not maintained) pulses once with its rung held: on (Result1 = "
            "1), then off for good until the rung goes false (Result2 = 0)",
            ladder=prove_timed_needs_fresh_edge,
            expect={"Result1": 1, "Result2": 0},
            scans=50,
        ),
        case(
            "An Out coil never sets its Timer's done bit T<n>: a debounce coil whose on "
            "phase is done (Result2 = 1) leaves T<n> off (Result1 = 0)",
            ladder=prove_out_coil_sets_timer_done,
            ready=lambda slot: ~c[1851],
            keep=[t[312], td[312]],
            expect={"Result1": 0, "Result2": 1},
            scans=40,
            pending="OUT",
        ),
    ],
)


def test_out():
    assert_all_passed(run(group))
