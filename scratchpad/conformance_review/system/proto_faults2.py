"""Prototype (scratch): fault-flag cases that separate the clearing models.

Same faults as today (Result4 stays the operand).  After the fault, three
instructions run every scan of the window, each followed by a probe coil that
snapshots the flag; nothing records (no calc) until every probe ran:
Result1 after a plain copy, Result2 after a calc of another kind,
Result3 after a successful instruction of the faulting kind.
  per-scan (pyrung today) 346/346/346   any instruction clears 0/0/0
  any calc clears         346/0/0       only its own kind      346/346/0
"""
from pyrung.conformance import Group, case, run, build_program
from pyrung.core import Or, Rung, calc, copy, fill, out
from pyrung.core.system_points import system
from tests.conformance.memory import c, dd
from tests.conformance.scans import WINDOW, count_scans, done, scans_for

FAULT_SCANS = (3, 4, 6)

def _record(slot, field, *conds):
    with Rung(slot.Cmd != 0, slot.Scratch >= 2, slot.Scratch <= WINDOW, *conds):
        calc(field * 10 + slot.Scratch, field)

def _win(slot):
    return (slot.Cmd != 0, slot.Scratch <= WINDOW)

def _fault_case(slot, flag, base, fault, other_kind, same_kind):
    """C<base>.. snapshot *flag*; DD<base+9> takes the plain copy."""
    probes = [c[base + i] for i in range(3)]
    steps = (lambda: copy(slot.Scratch, dd[base + 9]), other_kind, same_kind)
    count_scans(slot)
    with Rung(slot.Cmd != 0, Or(*(slot.Scratch == n for n in FAULT_SCANS))):
        fault()
    for step, probe in zip(steps, probes, strict=True):
        with Rung(*_win(slot)):
            step()
        with Rung(*_win(slot), flag):
            out(probe)
    for field, probe in zip((slot.Result1, slot.Result2, slot.Result3), probes, strict=True):
        _record(slot, field, probe)
    done(slot)

def prove_division_flag_clears_each_scan(slot):
    _fault_case(slot, system.fault.division_error, 901,
        fault=lambda: calc(slot.Scratch / slot.Result4, dd[901]),
        other_kind=lambda: calc(slot.Scratch + 1, dd[902]),
        same_kind=lambda: calc(100 / slot.Scratch, dd[903]))

def prove_out_of_range_flag_clears_each_scan(slot):
    _fault_case(slot, system.fault.out_of_range, 911,
        fault=lambda: calc(slot.Scratch + 32767, slot.Result4),
        other_kind=lambda: calc(100 / slot.Scratch, dd[912]),   # a calc that cannot overflow
        same_kind=lambda: calc(slot.Scratch + 1, slot.Result4))

def prove_address_flag_clears_each_scan(slot):
    def fault():
        copy(0, slot.Result4)
        copy(dd[slot.Result4], dd[921])
    def same():
        copy(922, slot.Result4)
        copy(dd[slot.Result4], dd[921])
    _fault_case(slot, system.fault.address_error, 921,
        fault=fault, other_kind=lambda: calc(slot.Scratch + 1, dd[923]), same_kind=same)

def _init(base):
    return lambda slot: fill(0, dd.select(base, base + 9))

E = {"Result1": 346, "Result2": 346, "Result3": 346}
g = Group("system", [
    case("div", ladder=prove_division_flag_clears_each_scan, init=_init(901), expect=E, scans=scans_for()),
    case("oor", ladder=prove_out_of_range_flag_clears_each_scan, init=_init(911), expect=E, scans=scans_for()),
    case("addr", ladder=prove_address_flag_clears_each_scan, init=_init(921), expect=E, scans=scans_for()),
])
if __name__ == "__main__":
    for r in run(g):
        print(r.case.id, r.actual, r.passed)
    build_program(g)   # exports through the validating CLICK exporter
    print("CLICK export OK")
