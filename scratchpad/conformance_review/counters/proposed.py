"""Proposed edge-model rewrites (scratch; expectations are the CLICK edge model)."""
from pyrung.conformance import Group, case, run
from pyrung.core import Or, Rung, calc, copy, count_down, count_up
from tests.conformance.memory import ClickCounter
from tests.conformance.scans import count_scans, done, scans_for

def on(slot, *scans):
    return Or(*(slot.Scratch == s for s in scans))

def prove_survives_rung_false(slot):
    counter = ClickCounter(11)
    count_scans(slot)
    with Rung(slot.Cmd != 0, on(slot, 1, 3, 5, 7)):
        count_up(counter, 3).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, slot.Scratch == 6):
        copy(counter.Acc, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == 6, counter.Done):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result3)
    done(slot)

W = 16
def prove_preset_raised_resumes(slot):
    counter = ClickCounter(21)
    count_scans(slot, W)
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(3, slot.Result4)
    with Rung(slot.Cmd != 0, slot.Scratch == 9):
        copy(6, slot.Result4)
    with Rung(slot.Cmd != 0, on(slot, *range(1, W, 2))):
        count_up(counter, slot.Result4).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, slot.Scratch <= W, counter.Done):
        calc(slot.Result1 + 1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch >= 9, slot.Scratch <= W, counter.Done, slot.Result2 == 0):
        copy(slot.Scratch, slot.Result2)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result3)
    done(slot, W)

def prove_preset_lowered_while_idle(slot):
    counter = ClickCounter(31)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(10, slot.Result4)
    with Rung(slot.Cmd != 0, slot.Scratch == 7):
        copy(2, slot.Result4)
    with Rung(slot.Cmd != 0, on(slot, 1, 3, 5)):
        count_up(counter, slot.Result4).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, slot.Scratch <= 8, counter.Done, slot.Result1 == 0):
        copy(slot.Scratch, slot.Result1)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result2)
    done(slot)

def prove_reset_wins_over_count(slot):
    counter = ClickCounter(41)
    count_scans(slot)
    with Rung(slot.Cmd != 0, on(slot, 1, 3, 5, 7)):
        count_up(counter, 2).reset(Or(slot.Cmd == 0, slot.Scratch == 3))
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        copy(counter.Acc, slot.Result1)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch <= 8, counter.Done):
        calc(slot.Result3 + 1, slot.Result3)
    done(slot)

def prove_count_down_negative(slot):
    counter = ClickCounter(61)
    count_scans(slot)
    with Rung(slot.Cmd != 0, on(slot, 2, 4, 6)):
        count_down(counter, 3).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch <= 8, counter.Done, slot.Result2 == 0):
        copy(slot.Scratch, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        copy(counter.Acc, slot.Result3)
    done(slot)

def prove_up_down_same_scan(slot):
    counter = ClickCounter(71)
    count_scans(slot)
    with Rung(slot.Cmd != 0, on(slot, 1, 3, 5, 7)):
        count_up(counter, 2).down(on(slot, 5, 7)).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, slot.Scratch == 5):
        copy(counter.Acc, slot.Result1)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch <= 8, counter.Done):
        calc(slot.Result3 + 1, slot.Result3)
    done(slot)

DMAX, DMIN = 2147483647, -2147483648
def prove_acc_clamps_at_dint_range(slot):
    up, down = ClickCounter(81), ClickCounter(82)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(DMAX - 2, up.Acc)
        copy(DMIN + 3, down.Acc)
    with Rung(slot.Cmd != 0, on(slot, 1, 3, 5, 7)):
        count_up(up, 100).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, on(slot, 1, 3, 5, 7)):
        count_down(down, 100).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, slot.Scratch <= 8, up.Acc == DMAX):
        calc(slot.Result1 + 1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch <= 8, up.Acc == DMAX, slot.Result2 == 0):
        copy(slot.Scratch, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch <= 8, down.Acc == DMIN):
        calc(slot.Result3 + 1, slot.Result3)
    with Rung(slot.Cmd != 0, slot.Scratch <= 8, down.Acc == DMIN, slot.Result4 == 0):
        copy(slot.Scratch, slot.Result4)
    done(slot)

def prove_held_input_not_recounted(slot):
    counter = ClickCounter(91)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch <= 8):
        count_up(counter, 100).reset(Or(slot.Cmd == 0, slot.Scratch == 4))
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        copy(counter.Acc, slot.Result1)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result2)
    done(slot)

def prove_count_down_done_at_preset(slot):
    counter = ClickCounter(92)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(5, counter.Acc)
    with Rung(slot.Cmd != 0, on(slot, 2, 4, 6, 8)):
        count_down(counter, 3).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, slot.Scratch <= 8, counter.Done, slot.Result2 == 0):
        copy(slot.Scratch, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        copy(counter.Acc, slot.Result3)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result1)
    done(slot)

S = scans_for()
g = Group("counters", [
    case("survives", ladder=prove_survives_rung_false, expect={"Result1": 3, "Result2": 1, "Result3": 4}, scans=S),
    case("raised", ladder=prove_preset_raised_resumes, expect={"Result1": 10, "Result2": 11, "Result3": 8, "Result4": 6}, scans=scans_for(W)),
    case("lowered", ladder=prove_preset_lowered_while_idle, expect={"Result1": 7, "Result2": 3, "Result4": 2}, scans=S),
    case("resetwins", ladder=prove_reset_wins_over_count, expect={"Result1": 0, "Result2": 2, "Result3": 2}, scans=S),
    case("cdneg", ladder=prove_count_down_negative, expect={"Result1": -3, "Result2": 1, "Result3": -1}, scans=S),
    case("updown", ladder=prove_up_down_same_scan, expect={"Result1": 2, "Result2": 2, "Result3": 6}, scans=S),
    case("clamp", ladder=prove_acc_clamps_at_dint_range, expect={"Result1": 6, "Result2": 3, "Result3": 4, "Result4": 5}, scans=S),
    case("held", ladder=prove_held_input_not_recounted, expect={"Result1": 1, "Result2": 0}, scans=S),
    case("cddone", ladder=prove_count_down_done_at_preset, expect={"Result1": 1, "Result2": 4, "Result3": 4}, scans=S),
])
for r in run(g):
    print(r.passed, r.case.sentence if hasattr(r.case,'sentence') else r.case, r.actual, getattr(r,'note',''))
