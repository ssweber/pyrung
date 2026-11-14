"""Prototype (scratch): pin SD9 and SC3 on the first scan (SC2).

pyrung: SD9 = 0, SC3 off.  'SD9 starts at 1': Result1 = 1, Result3 = 0.
'SC3 starts on': Result1 = 0, Result3 = 1.  Result2 = 1 shows the capture ran.
"""
from pyrung.conformance import Group, case, run, build_program
from pyrung.core import Rung, copy
from pyrung.core.system_points import system
from tests.conformance.memory import dd

def prove_first_scan_counter_and_clock(slot):
    first = system.sys.first_scan
    with Rung(first):                       # outside the Cmd gate: once per STOP->RUN
        copy(system.sys.scan_counter, dd[981])
        copy(1, dd[982])
        copy(0, dd[983])
    with Rung(first, system.sys.scan_clock_toggle):
        copy(1, dd[983])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(dd[981], slot.Result1)
        copy(dd[982], slot.Result2)
        copy(dd[983], slot.Result3)
        copy(1, slot.Fired)

g = Group("system", [case("first", ladder=prove_first_scan_counter_and_clock,
    expect={"Result1": 0, "Result2": 1, "Result3": 0})])
if __name__ == "__main__":
    for r in run(g):
        print(r.case.id, r.actual, r.passed, getattr(r, "reason", None))
    build_program(g); print("CLICK export OK")
