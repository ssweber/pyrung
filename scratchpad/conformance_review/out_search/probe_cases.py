"""Proposed rewrites, run in-process against current pyrung (expect today's answers)."""
from pyrung.conformance import Group, case, run
from pyrung.core import Rung, copy, flasher, on_delay, out, debounce, search, fill, reset
from tests.conformance.memory import ClickTimer, c, t, td, dd


def prove_flasher_restarts_with_on_phase(slot):
    # O4: 300 ms phases; the rung drops at 450-550 ms, 150 ms into the off phase.
    with Rung(slot.Cmd != 0):
        on_delay(ClickTimer(305), 2000)
    with Rung(slot.Cmd != 0, td[305] >= 450, td[305] < 550):
        out(c[1832])
    with Rung(slot.Cmd != 0, ~c[1832]):
        flasher(c[1831], on=(ClickTimer(306), 300), off=(ClickTimer(307), 300))
    # While the rung is false: 1 = TD cleared, 2 = TD kept (on side held at its
    # preset, off side held mid-phase).
    with Rung(slot.Cmd != 0, td[305] >= 480, td[305] < 520, td[306] == 0):
        copy(1, slot.Result1, oneshot=True)
    with Rung(slot.Cmd != 0, td[305] >= 480, td[305] < 520, td[306] >= 300):
        copy(2, slot.Result1, oneshot=True)
    with Rung(slot.Cmd != 0, td[305] >= 480, td[305] < 520, td[307] == 0):
        copy(1, slot.Result3, oneshot=True)
    with Rung(slot.Cmd != 0, td[305] >= 480, td[305] < 520, td[307] >= 100, td[307] < 300):
        copy(2, slot.Result3, oneshot=True)
    # After the restart at 550: a fresh on phase is on at 600-650 and 750-800;
    # resuming the off phase is off at 600-650, on at 750-800; a fresh off
    # phase is off at both.
    with Rung(slot.Cmd != 0, td[305] >= 600, td[305] < 650, c[1831]):
        copy(1, slot.Result2, oneshot=True)
    with Rung(slot.Cmd != 0, td[305] >= 750, td[305] < 800, c[1831]):
        copy(1, slot.Result4, oneshot=True)
    with Rung(slot.Cmd != 0):
        copy(1, slot.Fired, oneshot=True)


def prove_out_coil_sets_timer_done(slot):
    with Rung(slot.Cmd != 0):
        debounce(c[1851], on=(ClickTimer(310), 200), off=(ClickTimer(312), 1000))
    with Rung(slot.Cmd != 0, t[310]):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, c[1851], td[310] >= 300):
        copy(1, slot.Result2, oneshot=True)
    with Rung(slot.Cmd != 0):
        copy(1, slot.Fired, oneshot=True)


def prove_continuous_cursor_outside_range(slot):
    # DD411..DD416 = {7, 1, 7, 7, 2, 7}; each result preset, then searched once.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        for i, v in enumerate([7, 1, 7, 7, 2, 7]):
            copy(v, dd[411 + i])
        copy(410, slot.Result1)
        copy(417, slot.Result2)
        copy(-5, slot.Result3)
        copy(411, slot.Result4)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        search(dd.select(411, 416) == 7, result=slot.Result1, found=c[878], continuous=True)
        search(dd.select(411, 416) == 7, result=slot.Result2, found=c[879], continuous=True)
        search(dd.select(411, 416) == 7, result=slot.Result3, found=c[880], continuous=True)
        search(dd.select(411, 416) == 7, result=slot.Result4, found=c[881], continuous=True)
        copy(1, slot.Fired)


def init_outside(slot):
    fill(0, dd.select(411, 416))
    for n in (878, 879, 880, 881):
        reset(c[n])


g = Group("out", [
    case("flasher", ladder=prove_flasher_restarts_with_on_phase,
         expect={"Result1": 0, "Result2": 0, "Result3": 0, "Result4": 0}, scans=120),
    case("o6", ladder=prove_out_coil_sets_timer_done, ready=lambda s: ~c[1851],
         keep=[t[312], td[312]], expect={"Result1": 0, "Result2": 0}, scans=50),
])
for r in run(g):
    print(r)
g2 = Group("search", [case("outside", ladder=prove_continuous_cursor_outside_range, init=init_outside,
                            expect={"Result1": 0, "Result2": 0, "Result3": 0, "Result4": 0})])
for r in run(g2):
    print(r)
