"""Out coil variants: the ``out`` group (subroutine ``Conf_Out``).

Each coil is a C bit and each phase a raw Timer pair, ``T<n>`` with ``TD<n>``,
which is what CLICK's Out dialog names (its TD) and claims (the whole timer
number).  The cases own C1801..C1900 (``BITS_C``) and T301..T400 / TD301..TD400
(``TIMERS``), which no other group uses.  A case that needs its input to drop
or bounce at a known time drives it from a clock timer of its own.

The expectations are pyrung's answers, unconfirmed on a CLICK: the open probes
O1-O6 in ``scratchpad/click-v4/RESEARCH.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

from pyrung.click import ClickBlocks
from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, Tag, copy, debounce, flasher, on_delay, out, reset, timed, toggle

_blocks = ClickBlocks()
c, t, td = _blocks.c, _blocks.t, _blocks.td

#: Raw C bits and timer numbers the out cases own.
BITS_C = range(1801, 1901)
TIMERS = range(301, 401)


@dataclass(frozen=True)
class _ClickTimer:
    """Timer ``n`` as CLICK addresses it: its done bit T<n> and accumulator TD<n>."""

    n: int

    @property
    def Done(self) -> Tag:
        return t[self.n]

    @property
    def Acc(self) -> Tag:
        return td[self.n]


def _timer(n: int) -> _ClickTimer:
    return _ClickTimer(n)


def prove_toggle_flips_once_per_edge(slot):
    # O1: the rung goes true and stays true; the bit flips once.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        reset(c[1801])
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
        debounce(c[1811], on=(_timer(301), 100), off=(_timer(311), 1000))
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
        on_delay(_timer(302), 2000)
    with Rung(slot.Cmd != 0, td[302] >= 200, td[302] < 300):
        out(c[1822])
    with Rung(slot.Cmd != 0, ~c[1822]):
        debounce(c[1821], on=(_timer(303), 300), off=(_timer(304), 1000))
    with Rung(slot.Cmd != 0, td[302] >= 450, td[302] < 550, c[1821]):
        copy(1, slot.Result2, oneshot=True)
    with Rung(slot.Cmd != 0, c[1821]):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0):
        copy(1, slot.Fired, oneshot=True)


def prove_flasher_restarts_with_on_phase(slot):
    # O4: 300 ms phases; the rung drops at 450-550 ms (mid off phase).  Both TDs
    # clear, and the restart at 550 ms begins with a full on phase.
    with Rung(slot.Cmd != 0):
        on_delay(_timer(305), 2000)
    with Rung(slot.Cmd != 0, td[305] >= 450, td[305] < 550):
        out(c[1832])
    with Rung(slot.Cmd != 0, ~c[1832]):
        flasher(c[1831], on=(_timer(306), 300), off=(_timer(307), 300))
    with Rung(slot.Cmd != 0, td[305] >= 480, td[305] < 520, td[306] == 0, td[307] == 0):
        copy(1, slot.Result1, oneshot=True)
    with Rung(slot.Cmd != 0, td[305] >= 750, td[305] < 800, c[1831]):
        copy(1, slot.Result2, oneshot=True)
    with Rung(slot.Cmd != 0):
        copy(1, slot.Fired, oneshot=True)


def prove_timed_needs_fresh_edge(slot):
    # O5: a 200 ms pulse with the rung held; the coil does not come back on.
    with Rung(slot.Cmd != 0):
        on_delay(_timer(308), 2000)
    with Rung(slot.Cmd != 0):
        timed(c[1841], _timer(309), 200)
    with Rung(slot.Cmd != 0, c[1841]):
        copy(1, slot.Result1, oneshot=True)
    with Rung(slot.Cmd != 0, td[308] >= 400, c[1841]):
        copy(1, slot.Result2, oneshot=True)
    with Rung(slot.Cmd != 0):
        copy(1, slot.Fired, oneshot=True)


def prove_out_coil_sets_timer_done(slot):
    # O6: does the coil write its T<n>?  pyrung sets it when the phase is done.
    with Rung(slot.Cmd != 0):
        debounce(c[1851], on=(_timer(310), 200), off=(_timer(312), 1000))
    with Rung(slot.Cmd != 0, t[310]):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0):
        copy(1, slot.Fired, oneshot=True)


group = Group(
    "out",
    [
        case(
            "A toggle coil flips once when its rung goes true and holds while it stays "
            "true (Result1 = 1 for on)",
            ladder=prove_toggle_flips_once_per_edge,
            expect={"Result1": 1},
            scans=20,
        ),
        case(
            "A debounce coil's on-side TD keeps counting past its preset while the input "
            "stays on, as a TON's does (Result2 = 1 once it passes 300 ms on a 100 ms "
            "preset)",
            ladder=prove_debounce_td_keeps_counting,
            expect={"Result1": 1, "Result2": 1},
            scans=50,
        ),
        case(
            "A bounce restarts a debounce coil's on-side timing: on for 200 ms, off for "
            "100 ms, then on with a 300 ms on side, the coil is still off at 450-550 ms "
            "(Result2 = 0) and on later (Result1 = 1)",
            ladder=prove_debounce_bounce_restarts_on_side,
            expect={"Result1": 1, "Result2": 0},
            scans=80,
        ),
        case(
            "A flasher's rung going false clears both TDs (Result1 = 1), and the restart "
            "begins with a full on phase (Result2 = 1)",
            ladder=prove_flasher_restarts_with_on_phase,
            expect={"Result1": 1, "Result2": 1},
            scans=90,
        ),
        case(
            "A timed coil (not maintained) pulses once with its rung held: on (Result1 = "
            "1), then off for good until the rung goes false (Result2 = 0)",
            ladder=prove_timed_needs_fresh_edge,
            expect={"Result1": 1, "Result2": 0},
            scans=50,
        ),
        case(
            "An Out coil sets its Timer's done bit T<n> when its phase is done (Result1 = "
            "1); pyrung's answer, the open probe O6",
            ladder=prove_out_coil_sets_timer_done,
            expect={"Result1": 1},
            scans=40,
        ),
    ],
)


def test_out():
    assert_all_passed(run(group))
