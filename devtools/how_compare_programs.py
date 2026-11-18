"""Define the programs that devtools/how_compare.py runs how() on to show a regression.

Each case is a builder that returns a dict:

    plc      the session that how() is asked from (already stepped, patched or harnessed)
    targets  what to pass to plc.how(*targets)
    goal     goal(tags: dict) -> bool, the target as plain Python, or None.  The tool uses it
             to ask whether stepping the session itself reaches the target, and when.  The
             answer does not depend on the plan.
    avoid    avoid= for how() (optional)
    kwargs   extra how() keywords (optional)

Groups (this is the brief for the tool, written after two branches regressed how() unseen):

    midcount   a timer mid-count under a held enable, with and without an installed harness,
               and with the bench link changed after the harness settled
    entry      programs whose route has an entry requirement, warmed 0..2 scans
    reach      programs that really reach: parent/successive/hazard fixtures, dwell, armed, packml
    longwait   minutes to past 32.767 s: timers, chains, off delays, harness delays, two routes
    holds      routes that need a held input (dwell, armed, timer_hold)
    routes     two routes, one of them avoided
    avoid      avoid forms: condition, callable index, callable .get, raising on a real scan
    drums      time drums
    goal       the goal is already true
    truth      what stepping says against a proof or a decline: true and false CANNOT_REACH,
               long held waits, an opaque avoid mid-coast, And and range-written slots
    composite  several targets, And(), relational targets
    rescue     programs that reach only through the last resort today; a fix to the pilot
               shows as the same answer with no last_resort on the row
    forloop    a For calling a subroutine over Int and Dint counts (declared 0..5, choices, a
               body-rung contact the body writes), and a producer in a subroutine the For calls
    system     a copy of a system tag that the runtime writes (the scan counter, an RTC field),
               compared with a value that the runtime gives it
    preset     a negative preset, written as a literal and as a tag: a counter held in
               reset, the same counter staged below its preset, an on-delay
    accs       a count a copy stages past its preset, counts compared with each other
               (directly and with a constant added), a tag its copies leave at 0 or 10
               compared with an input, an input compared with an unbounded input, and an
               input compared with a set point plus 2
    relations  facts read from the generated code: an off delay asked for Acc from power-up,
               before its enable was ever on, and a counter whose reset is latched on

Cases marked fast=False only run with --full.  The case name is its key.  The tool compares
two runs by name, so give a new case a name that no case has used, and never rename a case.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Import pyrung only after the worker has put the tree under test first on sys.path.
from pyrung import (  # noqa: E402
    PLC,
    And,
    Block,
    Bool,
    Counter,
    Dint,
    Int,
    Or,
    Program,
    TagType,
    Timer,
    call,
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
    rung,
    subroutine,
    system,
    time_drum,
)
from pyrung.core import Harness, Physical  # noqa: E402
from pyrung.core.harness import Coupling  # noqa: E402


def _flasher(name, on, off):
    """Return a flasher Physical, using the old ``Pulse`` form on a base without flat keywords."""
    try:
        return Physical(name, flasher_on=on, flasher_off=off)
    except TypeError:
        import pyrung

        pulse = vars(pyrung)["Pulse"]
        old: dict[str, Any] = {"profile": pulse(on_dwell=on, off_dwell=off)}
        return Physical(name, **old)


FS = system.sys.first_scan
FAULT = "fault.out_of_range"


@dataclass
class Case:
    name: str
    group: str
    fast: bool
    build: Callable[[], dict[str, Any]]
    wall: float | None = None  # how() wall_clock_s override
    note: str = ""


CASES: list[Case] = []


def case(name: str, group: str, *, fast: bool = True, wall: float | None = None, note: str = ""):
    def register(fn):
        if any(c.name == name for c in CASES):
            raise ValueError(f"duplicate case name {name}")
        CASES.append(Case(name, group, fast, fn, wall, note))
        return fn

    return register


def _tag_goal(tag, value=True):
    name = tag.name
    return lambda tags: tags.get(name) == value


def _add(group, name, build, *, fast=True, wall=None):
    case(name, group, fast=fast, wall=wall)(build)


# --------------------------------------------------------------------------- midcount


def _mid_program(on_s):
    En = Bool("En")
    T = Timer.clone("Tm")
    Lost = Bool("Lost")
    with Program(strict=False) as p:
        with rung(En):
            on_delay(T, on_s, "s")
        with rung(T.Done):
            latch(Lost)
    return p, En, Lost


def _bench_program(on):
    En = Bool("En")
    Fb = Bool("Fb", physical=Physical("S", on_delay=on), link="En")
    Lost = Bool("Lost")
    with Program(strict=False) as p:
        with rung(En):
            out(Bool("Seen"))
        with rung(Fb):
            latch(Lost)
    return p, En, Lost


def _alarm_program(on):
    En = Bool("En")
    Fb = Bool("Fb", physical=Physical("S", on_delay=on), link="En")
    Alarm = Bool("Alarm")
    with Program(strict=False) as p:
        with rung(En):
            out(Bool("Seen"))
        with rung(En, ~Fb):
            latch(Alarm)
    return p, En, Alarm


def mid_plain(on_s, scans, dt=0.25):
    def build():
        p, En, Lost = _mid_program(on_s)
        plc = PLC(p, dt=dt)
        plc.patch({"En": True})
        plc.run(scans)
        return dict(plc=plc, targets=(Lost,), goal=_tag_goal(Lost))

    return build


def mid_harness(on, scans, *, bench=None, dt=0.25, bench_scans=None):
    """Build a harness on-delay feedback with the enable held, and optionally change the bench
    link mid-count."""

    def build():
        p, En, Lost = _bench_program(on)
        plc = PLC(p, dt=dt)
        h = Harness(plc)
        h.install()
        plc.patch({"En": True})
        plc.run(scans)
        if bench is not None:
            h.set_overrides({"Fb": Coupling("En", "Fb", Physical("S", on_delay=bench), None)})
            if bench_scans:
                plc.run(bench_scans)
        return dict(plc=plc, targets=(Lost,), goal=_tag_goal(Lost))

    return build


def mid_alarm(declared, bench, scans, dt=0.25):
    """Build a program whose feedback settles and then falls. Hold the enable, change the
    link's on-delay, and target the alarm."""

    def build():
        p, En, Alarm = _alarm_program(declared)
        plc = PLC(p, dt=dt)
        h = Harness(plc)
        h.install()
        plc.patch({"En": True})
        plc.run(scans)
        h.set_overrides({"Fb": Coupling("En", "Fb", Physical("S", on_delay=bench), None)})
        return dict(plc=plc, targets=(Alarm,), goal=_tag_goal(Alarm))

    return build


for _s, _n in ((5, 4), (5, 12), (20, 12)):
    _add("midcount", f"mid/plain {_s}s after {_n}", mid_plain(_s, _n))
for _s, _n, _f in (
    ("5s", 4, True),
    ("5s", 12, True),
    ("20s", 4, True),
    ("40s", 4, True),
    ("40s", 100, False),
    ("60s", 8, False),
):
    _add("midcount", f"mid/harness {_s} after {_n}", mid_harness(_s, _n), fast=_f)
for _decl, _bench, _f in (
    ("5s", "10s", True),
    ("5s", "60s", True),
    ("60s", "5s", True),
    ("40s", "60s", False),
    ("60s", "40s", False),
):
    _add(
        "midcount",
        f"mid/bench {_decl}->{_bench} after 8",
        mid_harness(_decl, 8, bench=_bench),
        fast=_f,
    )
_add(
    "midcount",
    "mid/bench 5s->10s 300 scans earlier",
    mid_harness("5s", 8, bench="10s", bench_scans=300),
    fast=False,
)
_add("midcount", "mid/alarm settled 5s->10s", mid_alarm("5s", "10s", 40), fast=False)
_add("midcount", "mid/alarm settled 60s->5s", mid_alarm("60s", "5s", 40), fast=False)


def bench_dip(dip_scans, dt=0.1):
    """Settle a feedback under a bench link of on 1 s / off 500 ms, then dip the command.

    A dip shorter than the off delay keeps the feedback on. The alarm on its drop still needs
    a real drop of the enable.
    """

    def build():
        En = Bool("En")
        Fb = Bool("Fb", physical=Physical("S", on_delay="200ms", off_delay="200ms"), link="En")
        Armed, Alarm = Bool("Armed"), Bool("Alarm")
        with Program(strict=False) as p:
            with rung(En):
                out(Bool("Seen"))
            with rung(Fb):
                latch(Armed)
            with rung(Armed, ~Fb):
                latch(Alarm)
        plc = PLC(p, dt=dt)
        h = Harness(plc)
        h.install()
        plc.patch({"En": True})
        plc.run(12)
        bench = Physical("S", on_delay="1s", off_delay="500ms")
        h.set_overrides({"Fb": Coupling("En", "Fb", bench, None)})
        plc.run(3)
        if dip_scans:
            plc.patch({"En": False})
            plc.run(dip_scans)
            plc.patch({"En": True})
            plc.run(3)
        return dict(plc=plc, targets=(Alarm,), goal=_tag_goal(Alarm))

    return build


_add("midcount", "mid/bench dip 2 scans under 1s/500ms", bench_dip(2))
_add("midcount", "mid/bench drop under 1s/500ms", bench_dip(0))


# --------------------------------------------------------------------------- shared shapes


def _s1():
    x, t, w, p = Bool("HX"), Bool("HT"), Timer.clone("HW"), Int("HP")
    with Program(strict=False) as logic:
        with rung(FS):
            latch(x)
        with rung():
            on_delay(w, p)
        with rung(w.Done):
            reset(x)
        with rung(x):
            latch(t)
        with rung(FS):
            latch(x)
    return logic, t


def _s5():
    step, t, w, p = Int("HS"), Bool("HT"), Timer.clone("HW"), Int("HP")
    with Program(strict=False) as logic:
        with rung(FS):
            copy(10, step)
        with rung():
            on_delay(w, p)
        with rung(FS, w.Done):
            copy(5, step)
        with rung(step == 10, FS):
            latch(t)
        with rung(FS, step == 5):
            copy(10, step)
    return logic, t


def _rt_first():
    step, preset, w, t = Int("RtStep"), Int("RtPreset"), Timer.clone("RtW"), Bool("RtTarget")
    with Program(strict=False) as logic:
        with rung(FS):
            copy(40, step)
        with rung(step == 40):
            copy(80, step, oneshot=True)
            on_delay(w, preset)
        with rung(w.Done):
            copy(90, step, oneshot=True)
        with rung(step == 80):
            latch(t)
    return logic, t


def _two_ways():
    fast, slow = Bool("Fast", external=True), Bool("Slow", external=True)
    danger, level, target = Bool("Danger"), Int("Level"), Bool("Target")
    with Program(strict=False) as prog:
        with rung(fast):
            out(danger)
        with rung(fast):
            copy(5, level)
        with rung(Or(fast, slow)):
            out(target)
    return prog, dict(target=target, danger=danger, level=level)


def _dwell(limit=0):
    """Build a program whose stable timers need the sensor held on or off for 30 ms.

    Its watchdogs finish at 70 ms and its run timer at 250 ms. State goes from 6 to 7, or
    to 8 if a watchdog finishes first.
    """
    sensor = Bool("HoldSensor", external=True)
    stable_on, stable_off = Timer.clone("HoldStableOn"), Timer.clone("HoldStableOff")
    on_wd, off_wd, run = Timer.clone("HoldOnWD"), Timer.clone("HoldOffWD"), Timer.clone("HoldRun")
    state = Int("HoldState", default=6)
    lim = Int("HoldLimit", default=limit)
    with Program(strict=False) as prog:
        with rung(state == 6, sensor):
            on_delay(stable_on, 30, "ms")
        with rung(state == 6, ~sensor):
            on_delay(stable_off, 30, "ms")
        with rung(state == 6):
            on_delay(on_wd, 70, "ms").reset(stable_off.Done)
        with rung(state == 6):
            on_delay(off_wd, 70, "ms").reset(stable_on.Done)
        with rung(state == 6, Or(on_wd.Done, off_wd.Done)):
            copy(8, state)
        with rung(state == 6):
            on_delay(run, 250, "ms")
        with rung(state == 6, run.Done):
            copy(7, state)
        with rung(state == 6):
            copy(lim, lim)
    return prog, dict(state=state, sensor=sensor, target=state == 7)


def _armed(release_needed):
    start, sensor = Bool("Start", external=True), Bool("Sensor", external=True)
    armed_, ready, lim = Bool("Armed"), Bool("Ready"), Int("Lim")
    stable_on, stable_off = Timer.clone("StableOn"), Timer.clone("StableOff")
    on_wd, off_wd, run = Timer.clone("OnWD"), Timer.clone("OffWD"), Timer.clone("RunT")
    state = Int("St", default=6)
    gate = ready if release_needed else armed_
    with Program(strict=False) as prog:
        with rung():
            copy(5, lim)
        with rung(start):
            latch(armed_)
        with rung(armed_, ~start):
            latch(ready)
        with rung(gate, state == 6, sensor):
            on_delay(stable_on, 30, "ms")
        with rung(gate, state == 6, ~sensor):
            on_delay(stable_off, 30, "ms")
        with rung(gate, state == 6):
            on_delay(on_wd, 70, "ms").reset(stable_off.Done)
        with rung(gate, state == 6):
            on_delay(off_wd, 70, "ms").reset(stable_on.Done)
        with rung(gate, state == 6, Or(on_wd.Done, off_wd.Done)):
            copy(8, state)
        with rung(gate, state == 6):
            on_delay(run, 250, "ms")
        with rung(gate, state == 6, run.Done):
            copy(7, state)
    return prog, dict(start=start, sensor=sensor, state=state, target=state == 7)


def _timer_hold(lo=None, hi=None):
    """Build a program in which the 1 s run timer needs Speed above 3.  The program owns Lim."""
    speed, run = Int("Speed", external=True), Timer.clone("Rn")
    lim, target = Int("Lim", default=100), Bool("Target")
    with Program(strict=False) as prog:
        with rung(speed > 3):
            on_delay(run, 1000, "ms")
        with rung(run.Done):
            out(target)
        with rung():
            copy(lim, lim)
        if lo is not None:
            with rung(run.Acc >= lo):
                copy(1, lim)
            if hi is not None:
                with rung(run.Acc >= hi):
                    copy(100, lim)
    return prog, dict(speed=speed, lim=lim, target=target, run=run)


def _two_routes(minutes):
    """Build a program in which Fast reaches Target through Danger on a long timer.

    Slow reaches Target around Danger, on a timer twice as long.
    """
    fast, slow = Bool("Fast", external=True), Bool("Slow", external=True)
    danger, target = Bool("Danger"), Bool("Target")
    t1, t2 = Timer.clone("LwT1"), Timer.clone("LwT2")
    with Program(strict=False) as prog:
        with rung(fast):
            on_delay(t1, minutes, "min")
        with rung(t1.Done):
            out(danger)
        with rung(slow):
            on_delay(t2, minutes * 2, "min")
        with rung(danger):
            latch(target)
        with rung(t2.Done):
            latch(target)
    return prog, dict(target=target, danger=danger, t1=t1)


def _hold_timer(preset, unit):
    go, lamp, t1 = Bool("LwGo"), Bool("LwLamp"), Timer.clone("LwT1")
    with Program(strict=False) as prog:
        with rung(go):
            on_delay(t1, preset=preset, unit=unit)
        with rung(t1.Done):
            latch(lamp)
    return prog, dict(target=lamp, timer=t1)


def _free_timer(preset, unit):
    lamp, t1 = Bool("LwLamp"), Timer.clone("LwT1")
    with Program(strict=False) as prog:
        with rung():
            on_delay(t1, preset=preset, unit=unit)
        with rung(t1.Done):
            out(lamp)
    return prog, dict(target=lamp, timer=t1)


def _chain(first, second, unit):
    go, lamp = Bool("LwGo"), Bool("LwLamp")
    t1, t2 = Timer.clone("LwT1"), Timer.clone("LwT2")
    with Program(strict=False) as prog:
        with rung(go):
            on_delay(t1, preset=first, unit=unit)
        with rung(t1.Done):
            on_delay(t2, preset=second, unit=unit)
        with rung(t2.Done):
            latch(lamp)
    return prog, dict(target=lamp, timer=t1)


def _soak(seconds):
    start, run, done = Bool("LwStart", external=True), Bool("LwRun"), Bool("LwDone")
    t1 = Timer.clone("LwSoak")
    with Program(strict=False) as prog:
        with rung(start):
            latch(run)
        with rung(run):
            on_delay(t1, seconds, "s")
        with rung(t1.Done):
            out(done)
    return prog, dict(target=done, timer=t1)


def _off_wait(minutes):
    go, t1, idle, seen = (
        Bool("LwGo", external=True),
        Timer.clone("LwT1"),
        Bool("LwIdle"),
        Bool("LwSeen"),
    )
    with Program(strict=False) as prog:
        with rung(go):
            off_delay(t1, minutes, "min")
        with rung(t1.Done):
            latch(seen)
        with rung(seen, ~t1.Done):
            latch(idle)
    return prog, dict(target=idle, timer=t1)


def _harness_delay(on, off=None):
    """Make the program wait on a harness coupling that has a long delay."""
    physical = (
        Physical("Slow", on_delay=on, off_delay=off) if off else Physical("Slow", on_delay=on)
    )
    fb = Bool("Fb", physical=physical, link="Enable")
    enable, go, hit, t1 = (
        Bool("Enable"),
        Bool("LwGo", external=True),
        Bool("LwHit"),
        Timer.clone("LwT1"),
    )
    with Program(strict=False) as prog:
        with rung(go):
            out(enable)
        with rung(fb):
            latch(hit)
        with rung(go):
            on_delay(t1, 500, "s")
    return prog, dict(target=hit, timer=t1)


def _pulse(on_s, off_s):
    physical = _flasher("Sensor", f"{on_s}s", f"{off_s}s")
    fb = Bool("Fb", physical=physical, link="Enable")
    enable, go, hit, t1 = (
        Bool("Enable"),
        Bool("LwGo", external=True),
        Bool("LwHit"),
        Timer.clone("LwT1"),
    )
    with Program(strict=False) as prog:
        with rung(go):
            out(enable)
        with rung(fb):
            latch(hit)
        with rung(go):
            on_delay(t1, 40, "s")
    return prog, dict(target=hit, timer=t1)


def _drum(first, second, unit="s", steps=2):
    done, start = Bool("LwDrumDone"), Bool("LwStart", external=True)
    acc, step = Dint("LwAcc"), Int("LwStep")
    outs = [Bool(f"LwO{i + 1}") for i in range(steps)]
    pattern = [[1 if i == j else 0 for j in range(steps)] for i in range(steps)]
    presets = [first, second] + [second] * (steps - 2)
    with Program(strict=False) as prog:
        with rung(start):
            time_drum(
                outputs=outs,
                presets=presets,
                unit=unit,
                pattern=pattern,
                current_step=step,
                accumulator=acc,
                completion_flag=done,
            ).reset(Bool("LwReset", external=True))
    return prog, dict(target=done, acc=acc, step=step, outs=outs)


def _plain(make, target_key="target", *, dt=0.01, warm=0, harness=False, patch=None, goal=None):
    """Build a case from a (program, tags) maker, step *warm* scans and ask for tags[target_key]."""

    def build():
        prog, t = make()
        plc = PLC(prog, dt=dt)
        if harness:
            Harness(plc).install()
        if patch:
            plc.patch(patch)
        for _ in range(warm):
            plc.step()
        target = t[target_key]
        return dict(plc=plc, targets=(target,), goal=goal(t) if goal else _auto_goal(target))

    return build


_OPS = {
    "CompareEq": lambda a, b: a == b,
    "CompareNe": lambda a, b: a != b,
    "CompareLt": lambda a, b: a < b,
    "CompareLe": lambda a, b: a <= b,
    "CompareGt": lambda a, b: a > b,
    "CompareGe": lambda a, b: a >= b,
}


def _auto_goal(target):
    """Return a plain-Python goal for a Bool tag or a `tag <op> literal` condition, else None."""
    op = _OPS.get(type(target).__name__)
    if op is None:
        return _tag_goal(target) if hasattr(target, "name") else None
    name, value = target.tag.name, target.value
    if not isinstance(value, (int, float)):
        return None
    return lambda tags: (tags.get(name) is not None) and op(tags.get(name), value)


# --------------------------------------------------------------------------- avoid forms


class RaisesOnReal(Exception):
    pass


def _raising_when(pred):
    def check(s):
        if pred(s):
            raise RaisesOnReal("raised on this state")
        return False

    return check


# --------------------------------------------------------------------------- entry


def _fixtures():
    for path in (ROOT,):
        if path not in sys.path:
            sys.path.append(path)  # tests.fixtures and examples come from the tool's checkout
    from examples.learn import assignment as ex_assignment
    from examples.packml_bench import S as PACK_S
    from examples.packml_bench import StateCurrent as PACK_STATE
    from examples.packml_bench import logic as pack_logic
    from tests.fixtures import (
        pilot_bootstrap_guard_overwrite as f_guard,
    )
    from tests.fixtures import (
        pilot_bootstrap_intermediate_guard as f_intermediate,
    )
    from tests.fixtures import (
        pilot_parent_consumer_watchdog as f_parent,
    )
    from tests.fixtures import (
        pilot_progress_repeated_subroutine_landing as f_repeated,
    )
    from tests.fixtures import (
        pilot_progress_same_landing_interference as f_same,
    )
    from tests.fixtures import (
        pilot_progress_then_autonomous_hazard as f_auto,
    )
    from tests.fixtures import (
        pilot_progress_then_successive_hazards as f_succ,
    )
    from tests.fixtures import (
        pilot_scan_zero_sequence_route as f_route,
    )
    from tests.fixtures import (
        pilot_successive_delayed_hazards as f_delayed,
    )
    from tests.fixtures import (
        pilot_transient_target_restore as f_transient,
    )
    from tests.fixtures import (
        pilot_zero_net_deadline_composition as f_zero,
    )
    from tests.fixtures.pilot_alarm_presets import aborted_on_first_scan as f_first
    from tests.fixtures.pilot_alarm_presets import alarmed_at_start as f_alarm

    return dict(
        route_adjacent=(f_route.adjacent_logic, f_route.SequenceStep, 81),
        route_field=(f_route.logic, f_route.SequenceStep, 81),
        aborted=(f_first.logic, f_first.ProcessStep, f_first.AT_TARGET),
        alarmed=(f_alarm.logic, f_alarm.ProcessStep, f_alarm.COMPLETE),
        guard_overwrite=(f_guard.logic, f_guard.SequenceState, f_guard.TARGET),
        guard_intermediate=(
            f_intermediate.logic,
            f_intermediate.SequenceState,
            f_intermediate.COMPLETE,
        ),
        parent_watchdog=(f_parent.logic, f_parent.SequenceState, f_parent.ADVANCED),
        repeated_landing=(f_repeated.logic, f_repeated.SequenceState, f_repeated.COMPLETE),
        same_landing=(f_same.logic, f_same.SequenceState, f_same.COMPLETE),
        autonomous_hazard=(f_auto.logic, f_auto.SequenceState, f_auto.COMPLETE),
        successive_hazards=(f_succ.logic, f_succ.SequenceState, f_succ.COMPLETE),
        delayed_hazards=(f_delayed.logic, f_delayed.SequenceState, f_delayed.COMPLETE),
        transient_restore=(f_transient.logic, f_transient.State, f_transient.TARGET),
        zero_net=(f_zero.logic, f_zero.State, f_zero.TARGET),
        assignment=(ex_assignment.logic, ex_assignment.CycleCount, 5),
        packml=(pack_logic, PACK_STATE, PACK_S.COMPLETED.default),
        _alarm_module=f_alarm,
    )


def _fixture_case(key, *, warm, avoid=None, forced=None):
    def build():
        logic, tag, value = _fixtures()[key]
        plc = PLC(logic, dt=0.010)
        for tag_, val in (forced or {}).items():
            plc.force(tag_, val)
        for _ in range(warm):
            plc.step()
        name = tag.name
        return dict(
            plc=plc,
            targets=(tag == value,),
            goal=lambda tags: tags.get(name) == value,
            avoid=avoid() if avoid else None,
        )

    return build


def _shape_case(make, *, warm, avoid=None, dt=0.010):
    def build():
        prog, t = make()
        plc = PLC(prog, dt=dt)
        for _ in range(warm):
            plc.step()
        return dict(plc=plc, targets=(t,), goal=_tag_goal(t), avoid=avoid() if avoid else None)

    return build


ENTRY_CHECKS = (
    ("none", None, True),
    ("get system", lambda: lambda s: s.get(FAULT, False), True),
    ("idx system", lambda: lambda s: s[FAULT], True),
    ("typo", lambda: lambda s: s["Typo"], False),
    ("typo behind", lambda: lambda s: s.get(FAULT) is None and s["Typo"], False),
)

_ENTRY_FIXTURES = (
    "route_adjacent",
    "route_field",
    "aborted",
    "alarmed",
    "guard_overwrite",
    "guard_intermediate",
)
_ENTRY_SHAPES = (("s1", _s1), ("s5", _s5), ("first", _rt_first))
_REACH_FIXTURES = (
    "parent_watchdog",
    "repeated_landing",
    "same_landing",
    "autonomous_hazard",
    "successive_hazards",
    "delayed_hazards",
    "transient_restore",
    "zero_net",
    "assignment",
    "packml",
)

for _warm in (0, 1, 2):
    for _label, _avoid, _fast_check in ENTRY_CHECKS:
        for _key in _ENTRY_FIXTURES:
            _add(
                "entry",
                f"entry/{_key}/w{_warm}/{_label}",
                _fixture_case(_key, warm=_warm, avoid=_avoid),
                fast=(_warm == 1 and _fast_check),
            )
        for _key, _make in _ENTRY_SHAPES:
            _add(
                "entry",
                f"entry/{_key}/w{_warm}/{_label}",
                _shape_case(_make, warm=_warm, avoid=_avoid),
                fast=(_warm == 1 and _fast_check),
            )
        for _key in _REACH_FIXTURES:
            _add(
                "reach",
                f"reach/{_key}/w{_warm}/{_label}",
                _fixture_case(_key, warm=_warm, avoid=_avoid),
                fast=(_warm == 0 and _label in ("none", "idx system"))
                or (_warm == 1 and _label == "none"),
            )


def _alarm_pressed():
    mod = _fixtures()["_alarm_module"]
    plc = PLC(mod.logic, dt=0.010)
    plc.force(mod.Reset, True)
    plc.force(mod.AtTarget, True)
    plc.step()
    name = mod.ProcessStep.name
    return dict(
        plc=plc,
        targets=(mod.ProcessStep == mod.COMPLETE,),
        goal=lambda tags: tags.get(name) == mod.COMPLETE,
    )


_add("entry", "entry/alarmed-pressed/w1/none", _alarm_pressed)


# --------------------------------------------------------------------------- reach: holds


_add("holds", "holds/dwell", _plain(_dwell, goal=lambda t: lambda tags: tags.get("HoldState") == 7))
_add(
    "holds",
    "holds/armed release",
    _plain(lambda: _armed(True), goal=lambda t: lambda tags: tags.get("St") == 7),
)
_add(
    "holds",
    "holds/armed no release",
    _plain(lambda: _armed(False), goal=lambda t: lambda tags: tags.get("St") == 7),
)
_add(
    "holds", "holds/timer_hold", _plain(_timer_hold, warm=1, goal=lambda t: _tag_goal(t["target"]))
)
_add(
    "holds",
    "holds/timer_hold dip",
    _plain(lambda: _timer_hold(300, 700), warm=1, goal=lambda t: _tag_goal(t["target"])),
    fast=False,
)
_add(
    "holds",
    "holds/dwell warmed 1",
    _plain(_dwell, warm=1, goal=lambda t: lambda tags: tags.get("HoldState") == 7),
    fast=False,
)

# --------------------------------------------------------------------------- routes


def _routes_case(avoid_kind, minutes):
    def build():
        prog, t = _two_routes(minutes)
        plc = PLC(prog, dt=0.01)
        plc.step()
        danger = t["danger"]
        avoid = {
            None: None,
            "cond": danger,
            "idx": lambda s: s["Danger"],
            "get": lambda s: s.get("Danger", False),
            "raises": _raising_when(lambda s: s["Danger"]),
        }[avoid_kind]
        return dict(plc=plc, targets=(t["target"],), goal=_tag_goal(t["target"]), avoid=avoid)

    return build


for _kind, _f in ((None, True), ("cond", True), ("idx", False), ("get", False), ("raises", True)):
    _add("routes", f"routes/two routes 10/20 min avoid {_kind}", _routes_case(_kind, 10), fast=_f)


def _two_ways_case(avoid_kind):
    def build():
        prog, t = _two_ways()
        plc = PLC(prog, dt=0.01)
        plc.step()
        danger = t["danger"]
        avoid = {
            None: None,
            "cond": danger,
            "idx": lambda s: s["Danger"],
            "get": lambda s: s.get("Danger", False),
            "raises": _raising_when(lambda s: s["Danger"]),
        }[avoid_kind]
        return dict(plc=plc, targets=(t["target"],), goal=_tag_goal(t["target"]), avoid=avoid)

    return build


for _kind in (None, "cond", "idx", "get", "raises"):
    _add("routes", f"routes/two ways avoid {_kind}", _two_ways_case(_kind))

# --------------------------------------------------------------------------- avoid forms


def _avoid_on(make, goal_of, target_key, forms, *, warm=1):
    def make_builder(builder):
        def build():
            prog, t = make()
            plc = PLC(prog, dt=0.01)
            for _ in range(warm):
                plc.step()
            return dict(plc=plc, targets=(t[target_key],), goal=goal_of(t), avoid=builder(t))

        return build

    return make_builder


_DWELL_FORMS = (
    ("idx never", lambda t: lambda s: s["HoldState"] == 9999, True),
    ("get never", lambda t: lambda s: s.get("HoldState") == 9999, True),
    ("cond never", lambda t: t["state"] == 9999, True),
    ("idx St8", lambda t: lambda s: s["HoldState"] == 8, True),
    ("get St8", lambda t: lambda s: s.get("HoldState") == 8, True),
    ("cond St8", lambda t: t["state"] == 8, True),
    ("idx St8+sensor", lambda t: lambda s: s["HoldState"] == 8 and s["HoldSensor"], False),
    (
        "raises St8+sensor",
        lambda t: _raising_when(lambda s: s["HoldState"] == 8 and s["HoldSensor"]),
        True,
    ),
    ("raises on a real scan", lambda t: _raising_when(lambda s: s["HoldState"] == 7), True),
    (
        "hold-only raise",
        lambda t: lambda s: 1 / (s["HoldRun_Acc"] - 10) > 5 if s["HoldSensor"] else False,
        False,
    ),
)
for _label, _form, _f in _DWELL_FORMS:
    _add(
        "avoid",
        f"avoid/dwell {_label}",
        _avoid_on(_dwell, lambda t: lambda tags: tags.get("HoldState") == 7, "target", None)(_form),
        fast=_f,
    )

_TH_FORMS = (
    ("idx never", lambda t: lambda s: s["Rn_Acc"] < 0, True),
    ("get never", lambda t: lambda s: s.get("Rn_Acc", 0) < 0, True),
    ("cond never", lambda t: t["run"].Acc < 0, True),
    ("idx past 1", lambda t: lambda s: s["Rn_Acc"] > 1, True),
    ("cond past 1", lambda t: t["run"].Acc > 1, True),
    ("raises on a real scan", lambda t: _raising_when(lambda s: s["Rn_Acc"] >= 500), True),
    (
        "raises only with the hold laid on a start",
        lambda t: lambda s: 1000 / s["Rn_Acc"] > 1e9 if s["Speed"] > 3 else False,
        True,
    ),
)
for _label, _form, _f in _TH_FORMS:
    _add(
        "avoid",
        f"avoid/timer_hold {_label}",
        _avoid_on(_timer_hold, lambda t: _tag_goal(t["target"]), "target", None)(_form),
        fast=_f,
    )

_ARMED_FORMS = (
    ("idx never", lambda t: lambda s: s["St"] == 9999, True),
    ("get never", lambda t: lambda s: s.get("St") == 9999, True),
    ("raises St8+sensor", lambda t: _raising_when(lambda s: s["St"] == 8 and s["Sensor"]), True),
)
for _label, _form, _f in _ARMED_FORMS:
    _add(
        "avoid",
        f"avoid/armed {_label}",
        _avoid_on(lambda: _armed(True), lambda t: lambda tags: tags.get("St") == 7, "target", None)(
            _form
        ),
        fast=_f,
    )

# --------------------------------------------------------------------------- long waits


def _lw(
    name,
    make,
    *,
    dt=0.01,
    fast=True,
    wall=None,
    warm=1,
    goal_key="target",
    patch=None,
    harness=False,
):
    _add(
        "longwait",
        name,
        _plain(make, goal_key, dt=dt, warm=warm, patch=patch, harness=harness),
        fast=fast,
        wall=wall,
    )


_lw("longwait/soak 120s", lambda: _soak(120))
_lw("longwait/soak 30s", lambda: _soak(30))
_lw("longwait/soak 33s", lambda: _soak(33))
_lw("longwait/chain 3+2 min", lambda: _chain(3, 2, "min"))
_lw("longwait/free timer 10 min", lambda: _free_timer(10, "min"))
_lw("longwait/free timer 33s", lambda: _free_timer(33, "s"))
_lw("longwait/free timer 32767ms", lambda: _free_timer(32767, "ms"))
_lw("longwait/hold timer 4h", lambda: _hold_timer(4, "h"), fast=False, wall=200)
_lw("longwait/chain 2+2 h", lambda: _chain(2, 2, "h"), fast=False, wall=200)
_lw("longwait/off delay 5 min", lambda: _off_wait(5))
_lw("longwait/harness delay 32.767s", lambda: _harness_delay("32s767ms"), harness=True, fast=False)
_lw("longwait/harness delay 33s", lambda: _harness_delay("33s"), harness=True)
_lw("longwait/harness delay 60s", lambda: _harness_delay("60s"), harness=True, fast=False)
_lw("longwait/harness delay 10min", lambda: _harness_delay("10min"), harness=True, fast=False)
_lw("longwait/pulse 60s", lambda: _pulse(60, 60), harness=True, fast=False)
_lw("longwait/pulse 8s", lambda: _pulse(8, 8), harness=True)


def _dwell_scaled(scale):
    def make():
        sensor = Bool("HoldSensor", external=True)
        stable_on, stable_off = Timer.clone("HoldStableOn"), Timer.clone("HoldStableOff")
        on_wd, off_wd, run = (
            Timer.clone("HoldOnWD"),
            Timer.clone("HoldOffWD"),
            Timer.clone("HoldRun"),
        )
        state = Int("HoldState", default=6)
        with Program(strict=False) as prog:
            with rung(state == 6, sensor):
                on_delay(stable_on, 30 * scale, "ms")
            with rung(state == 6, ~sensor):
                on_delay(stable_off, 30 * scale, "ms")
            with rung(state == 6):
                on_delay(on_wd, 70 * scale, "ms").reset(stable_off.Done)
            with rung(state == 6):
                on_delay(off_wd, 70 * scale, "ms").reset(stable_on.Done)
            with rung(state == 6, Or(on_wd.Done, off_wd.Done)):
                copy(8, state)
            with rung(state == 6):
                on_delay(run, 250 * scale, "ms")
            with rung(state == 6, run.Done):
                copy(7, state)
        return prog, dict(state=state, target=state == 7)

    return make


_lw("longwait/dwell x10", _dwell_scaled(10), goal_key="target", fast=False)
_lw("longwait/dwell x100", _dwell_scaled(100), goal_key="target", fast=False)


def _soak_then(unit_value, unit):
    def make():
        sensor = Bool("HoldSensor", external=True)
        state = Int("HoldState", default=6)
        prep = Timer.clone("LwPrep")
        run = Timer.clone("HoldRun")
        with Program(strict=False) as prog:
            with rung():
                on_delay(prep, unit_value, unit)
            with rung(prep.Done, state == 6):
                on_delay(run, 250, "ms")
            with rung(prep.Done, state == 6, run.Done, sensor):
                copy(7, state)
        return prog, dict(state=state, target=state == 7, sensor=sensor)

    return make


_lw("longwait/soak 30 min then dwell", _soak_then(30, "min"), fast=False)
_lw("longwait/soak 10 min then run", _soak_then(10, "min"))

# --------------------------------------------------------------------------- drums


def _drum_case(first, second, unit, *, target="done", steps=2, patch=None):
    def build():
        prog, t = _drum(first, second, unit, steps)
        plc = PLC(prog, dt=0.01)
        plc.step()
        if target == "done":
            return dict(plc=plc, targets=(t["target"],), goal=_tag_goal(t["target"]))
        step_name = t["step"].name
        return dict(plc=plc, targets=(t["step"] == 2,), goal=lambda tags: tags.get(step_name) == 2)

    return build


_add("drums", "drums/20s+40s done", _drum_case(20, 40, "s"))
_add("drums", "drums/20s+40s step 2", _drum_case(20, 40, "s", target="step"))
_add("drums", "drums/5+10 min done", _drum_case(300, 600, "s"), fast=False)
_add("drums", "drums/3 steps 2s done", _drum_case(2, 3, "s", steps=3))
_add("drums", "drums/3 steps 2s step 2", _drum_case(2, 3, "s", steps=3, target="step"))


def _drum_running():
    prog, t = _drum(20, 40, "s")
    plc = PLC(prog, dt=0.01)
    plc.patch({"LwStart": True})
    plc.run(500)  # 5 s into step 1, start held
    return plc, t


def _drum_mid(target):
    def build():
        plc, t = _drum_running()
        if target == "done":
            return dict(plc=plc, targets=(t["target"],), goal=_tag_goal(t["target"]))
        step_name = t["step"].name
        return dict(plc=plc, targets=(t["step"] == 2,), goal=lambda tags: tags.get(step_name) == 2)

    return build


_add("drums", "drums/mid-step start held done", _drum_mid("done"))
_add("drums", "drums/mid-step start held step 2", _drum_mid("step"))


def _drum_zero_step(tagged, target):
    """Build a 3-step time drum, 2 s / 0 / 2 s, with a jog.

    A 0 preset ends step 2 at once, whether it is a constant or a tag
    (``drum_preset/literal_zero``, ``drum_limits/preset_from_ds``).
    """

    def build():
        done, start = Bool("LzDone"), Bool("LzStart", external=True)
        acc, step = Dint("LzAcc"), Int("LzStep")
        outs = [Bool(f"LzO{i + 1}") for i in range(3)]
        middle = Int("LzPreset", default=0) if tagged else 0
        with Program(strict=False) as prog:
            with rung(start):
                time_drum(
                    outputs=outs,
                    presets=[2, middle, 2],
                    unit="s",
                    pattern=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                    current_step=step,
                    accumulator=acc,
                    completion_flag=done,
                ).reset(Bool("LzReset", external=True)).jog(Bool("LzJog", external=True))
        plc = PLC(prog, dt=0.01)
        plc.step()
        if target == "done":
            return dict(plc=plc, targets=(done,), goal=_tag_goal(done))
        return dict(plc=plc, targets=(step == 3,), goal=lambda tags: tags.get("LzStep") == 3)

    return build


for _tagged, _label in ((False, "constant"), (True, "tag")):
    _add("drums", f"drums/{_label} 0 preset step 3", _drum_zero_step(_tagged, "step"))
    _add("drums", f"drums/{_label} 0 preset done", _drum_zero_step(_tagged, "done"))

# --------------------------------------------------------------------------- goal already true


def _goal_true_latch():
    go, lamp = Bool("Go", external=True), Bool("Lamp")
    with Program(strict=False) as prog:
        with rung(go):
            latch(lamp)
    plc = PLC(prog, dt=0.01)
    plc.patch({"Go": True})
    plc.run(3)
    return dict(plc=plc, targets=(lamp,), goal=_tag_goal(lamp))


def _goal_true_timer():
    lamp, t1 = Bool("LwLamp"), Timer.clone("LwT1")
    with Program(strict=False) as prog:
        with rung():
            on_delay(t1, 200, "ms")
        with rung(t1.Done):
            out(lamp)
    plc = PLC(prog, dt=0.01)
    plc.run(40)
    return dict(plc=plc, targets=(lamp,), goal=_tag_goal(lamp))


def _goal_true_condition():
    step, go = Int("Step"), Bool("Go", external=True)
    with Program(strict=False) as prog:
        with rung(go):
            copy(3, step)
    plc = PLC(prog, dt=0.01)
    plc.patch({"Go": True})
    plc.run(3)
    return dict(plc=plc, targets=(step == 3,), goal=lambda tags: tags.get("Step") == 3)


def _goal_true_avoided():
    b = _goal_true_latch()
    b["avoid"] = lambda s: s.get("Lamp", False)
    return b


def _goal_true_harness():
    b = mid_harness("5s", 40)()  # settled: Fb is up and Lost is already latched
    return b


_add("goal", "goal/latched", _goal_true_latch)
_add("goal", "goal/timer done", _goal_true_timer)
_add("goal", "goal/condition", _goal_true_condition)
_add("goal", "goal/latched avoid its own tag", _goal_true_avoided)
_add("goal", "goal/harness settled", _goal_true_harness)

# --------------------------------------------------------------------------- composite


def _comp_multi():
    prog, t = _two_ways()
    plc = PLC(prog, dt=0.01)
    plc.step()
    return dict(
        plc=plc,
        targets=(t["target"], t["level"] == 5),
        goal=lambda tags: bool(tags.get("Target")) and tags.get("Level") == 5,
    )


def _comp_and():
    prog, t = _two_ways()
    plc = PLC(prog, dt=0.01)
    plc.step()
    return dict(
        plc=plc,
        targets=(And(t["target"], t["level"] == 5),),
        goal=lambda tags: bool(tags.get("Target")) and tags.get("Level") == 5,
    )


def _comp_or():
    prog, t = _two_ways()
    plc = PLC(prog, dt=0.01)
    plc.step()
    return dict(
        plc=plc,
        targets=(Or(t["danger"], t["level"] == 5),),
        goal=lambda tags: bool(tags.get("Danger")) or tags.get("Level") == 5,
    )


def _comp_relational():
    prog, t = _two_ways()
    plc = PLC(prog, dt=0.01)
    plc.step()
    return dict(
        plc=plc, targets=(t["level"] >= 5,), goal=lambda tags: (tags.get("Level") or 0) >= 5
    )


def _comp_timer_acc():
    prog, t = _free_timer(10, "s")
    plc = PLC(prog, dt=0.01)
    plc.step()
    return dict(
        plc=plc,
        targets=(t["timer"].Acc >= 5000,),
        goal=lambda tags: (tags.get("LwT1_Acc") or 0) >= 5000,
    )


def _comp_dwell_and_sensor():
    prog, t = _dwell()
    plc = PLC(prog, dt=0.01)
    plc.step()
    return dict(
        plc=plc,
        targets=(And(t["state"] == 7, t["sensor"]),),
        goal=lambda tags: tags.get("HoldState") == 7 and bool(tags.get("HoldSensor")),
    )


def _comp_dwell_multi():
    prog, t = _dwell()
    plc = PLC(prog, dt=0.01)
    plc.step()
    return dict(
        plc=plc,
        targets=(t["state"] == 7, t["sensor"]),
        goal=lambda tags: tags.get("HoldState") == 7 and bool(tags.get("HoldSensor")),
    )


_add("composite", "composite/multi target", _comp_multi)
_add("composite", "composite/And", _comp_and, fast=False)
_add("composite", "composite/Or", _comp_or, fast=False)
_add("composite", "composite/relational", _comp_relational)
_add("composite", "composite/timer acc >= 5 s", _comp_timer_acc, fast=False)
_add("composite", "composite/dwell And sensor", _comp_dwell_and_sensor, fast=False)
_add("composite", "composite/dwell multi sensor", _comp_dwell_multi, fast=False)


# --------------------------------------------------------------------------- truth rows
# A held enable with a long wait: the plan is what the session does by itself, so stepping can
# say when the goal holds.  Both decline on dev today.  The rows pin that a decline stays a decline
# or turns into a correct REACHED (a REACHED plan is checked against stepping).


def _held_wait(first, second, unit, scans):
    def build():
        en, lamp = Bool("En"), Bool("Lamp")
        t1, t2 = Timer.clone("Tw1"), Timer.clone("Tw2")
        with Program(strict=False) as prog:
            with rung(en):
                on_delay(t1, first, unit)
            if second is None:
                with rung(t1.Done):
                    latch(lamp)
            else:
                with rung(t1.Done):
                    on_delay(t2, second, unit)
                with rung(t2.Done):
                    latch(lamp)
        plc = PLC(prog, dt=0.01)
        plc.patch({"En": True})
        plc.run(scans)
        return dict(plc=plc, targets=(lamp,), goal=_tag_goal(lamp))

    return build


_add("truth", "truth/held enable 120s dt .01", _held_wait(120, None, "s", 1))
_add("truth", "truth/held enable 120s dt .01 mid-count", _held_wait(120, None, "s", 3000))
_add("truth", "truth/held chain 3+2 min no harness", _held_wait(3, 2, "min", 1))


def _proof_watchdog():
    enable = Bool("En", external=True)
    feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link="En")
    hit, watch = Bool("Hit"), Timer.clone("Watch")
    with Program(strict=False) as prog:
        with rung(enable, ~feedback):
            on_delay(watch, 3000)
        with rung(watch.Done):
            latch(hit)
    plc = PLC(prog, dt=0.01)
    Harness(plc).install()
    return dict(plc=plc, targets=(hit,), goal=_tag_goal(hit))


def _proof_fill_station():
    if ROOT not in sys.path:
        sys.path.append(ROOT)
    from examples import fill_station as fs

    plc = PLC(fs.logic, dt=0.01)
    Harness(plc).install()
    return dict(plc=plc, targets=(fs.FlowAlarm,), goal=_tag_goal(fs.FlowAlarm))


def _pulse_other_contact(*, held):
    enable = Bool("En", external=True)
    feedback = Bool(
        "Fb",
        physical=_flasher("Fb", "500ms", "500ms"),
        link="En",
    )
    hit = Bool("Hit")
    with Program(strict=False) as prog:
        with rung(enable, feedback):
            latch(hit)
    plc = PLC(prog, dt=0.01)
    Harness(plc).install()
    if held:
        plc.patch({"En": True})
        plc.step()
    return dict(plc=plc, targets=(hit,), goal=_tag_goal(hit))


def _enable_moved_mid_scan():
    """Turn En off and back on within each scan.

    The rung ``~En, Fb`` reads En off, and the plant reads it on.

    An earlier how() answered CANNOT_REACH here, and that answer was wrong. Fb follows En
    through its link, and the plant never sees En off. So Fb stays on, the rung
    ``~En, Fb`` stays true, and the timer W reaches 3 s.
    """
    go, enable, hit = Bool("Go", external=True), Bool("En"), Bool("Hit")
    feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms", off_delay="100ms"), link="En")
    watch = Timer.clone("W")
    with Program(strict=False) as prog:
        with rung():
            reset(enable)
        with rung(~enable, feedback):
            on_delay(watch, 3000)
        with rung(go):
            latch(enable)
        with rung(watch.Done):
            latch(hit)
    plc = PLC(prog, dt=0.01)
    Harness(plc).install()
    plc.patch({"Go": True})
    plc.step()
    # The default budget runs out first, and base skips its proof then, hiding the false one.
    return dict(plc=plc, targets=(hit,), goal=_tag_goal(hit), kwargs=dict(max_scans=30000))


def _second_route_over_the_cap():
    """Write ``C[Idx*2-1]``, which is in no element list of a block this big.

    It lands on C1 at once.
    """
    go, enable, hit = Bool("Go", external=True), Bool("En", external=True), Bool("Hit")
    feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link="En")
    bits, index = Block("C", TagType.BOOL, 1, 2000), Int("Idx", default=1)
    watch = Timer.clone("W")
    with Program(strict=False) as prog:
        with rung(go):
            copy(1, bits[index * 2 - 1])
        with rung(enable, ~feedback):
            on_delay(watch, 3000)
        with rung(watch.Done):
            latch(bits[1])
        with rung(bits[1]):
            latch(hit)
    plc = PLC(prog, dt=0.01)
    Harness(plc).install()
    # The default budget runs out first, and base skips its proof then, hiding the false one.
    return dict(plc=plc, targets=(hit,), goal=_tag_goal(hit), kwargs=dict(max_scans=1000))


def _opaque_avoid_mid_coast(avoid_of):
    def build():
        timer, bank = Timer.clone("T"), Block("B", TagType.INT, 1, 3)
        plain, index = Int("A"), Int("Idx", external=True, default=1)
        run, hit = Bool("Run", external=True), Bool("Hit")
        with Program(strict=False) as prog:
            with rung(run):
                on_delay(timer, 2000)
            with rung(timer.Acc >= 500, timer.Acc < 600):
                copy(9, bank[1])
                copy(9, plain)
            with rung(timer.Acc >= 600):
                copy(0, bank[1])
                copy(0, plain)
            with rung(timer.Done):
                latch(hit)
        return dict(
            plc=PLC(prog, dt=0.01),
            targets=(hit,),
            goal=_tag_goal(hit),
            avoid=avoid_of(bank, plain, index),
        )

    return build


def _and_with_a_contact_off():
    go, again, x, y = Bool("Go", external=True), Bool("Ag", external=True), Bool("X"), Bool("Y")
    with Program(strict=False) as prog:
        with rung(go):
            latch(x)
        with rung(again):
            latch(y)
    return dict(
        plc=PLC(prog, dt=0.01),
        targets=(And(x, ~y),),
        goal=lambda tags: bool(tags.get("X")) and not tags.get("Y"),
    )


def _joint_on_a_slot(write_of):
    def build():
        go, done = Bool("Go", external=True), Bool("X")
        bank, index = Block("B", TagType.INT, 1, 5), Int("Idx", external=True, default=2)
        with Program(strict=False) as prog:
            with rung(go):
                write_of(bank, index)
                latch(done)
        return dict(
            plc=PLC(prog, dt=0.01),
            targets=(bank[2] == 7, done),
            goal=lambda tags: tags.get("B2") == 7 and bool(tags.get("X")),
        )

    return build


_add("truth", "truth/proof watchdog", _proof_watchdog)
_add("truth", "truth/proof fill station FlowAlarm", _proof_fill_station)
_add("truth", "truth/pulse on the other contact", lambda: _pulse_other_contact(held=False))
_add("truth", "truth/pulse on the other contact held", lambda: _pulse_other_contact(held=True))
_add("truth", "truth/enable moved mid-scan", _enable_moved_mid_scan)
_add("truth", "truth/second route over the cap", _second_route_over_the_cap)
_add(
    "truth",
    "truth/pointer avoid true mid-coast",
    _opaque_avoid_mid_coast(lambda bank, plain, index: bank[index] >= 5),
)
_add(
    "truth",
    "truth/expression avoid true mid-coast",
    _opaque_avoid_mid_coast(lambda bank, plain, index: plain + 1 >= 6),
)
_add("truth", "truth/And with a contact off", _and_with_a_contact_off)
_add(
    "truth",
    "truth/joint goal on a filled slot",
    _joint_on_a_slot(lambda bank, index: fill(7, bank.select(1, 3))),
)
_add(
    "truth",
    "truth/joint goal on a pointer slot",
    _joint_on_a_slot(lambda bank, index: copy(7, bank[index])),
)


# --------------------------------------------------------------------------- rescue
# Each reaches on dev only because the last resort tried a move the trace gave no reason for
# (tests/core/analysis/test_pilot_last_resort.py and the field-order reconnect).


def _rescue_oscillator():
    """A count the program runs for itself: only waiting reaches it."""
    osc, count, hit = Bool("Osc"), Int("Count"), Bool("Hit")
    with Program(strict=False) as prog:
        with rung(~osc):
            out(osc)
        with rung(osc):
            math(count + 1, count)
        with rung(count == 40):
            latch(hit)
    plc = PLC(prog, dt=0.01)
    plc.step()
    return dict(plc=plc, targets=(hit,), goal=_tag_goal(hit), kwargs=dict(max_scans=3000))


def _rescue_wait_on_a_bit():
    """Go is blocked until the free-running timer first finishes and drops Block."""
    timer, block, hit, go = Timer.clone("T"), Bool("Block"), Bool("Hit"), Bool("Go")
    with Program(strict=False) as prog:
        with rung(~timer.Done):
            out(block)
        with rung():
            on_delay(timer, 1, "s")
        with rung(block):
            reset(hit)
        with rung(go, ~block):
            latch(hit)
    plc = PLC(prog, dt=0.01)
    plc.step()
    return dict(plc=plc, targets=(hit,), goal=_tag_goal(hit), kwargs=dict(max_scans=3000))


def _rescue_at_the_rung():
    """Build the program of the case ``truth/enable moved mid-scan``, but leave Go unpressed.

    The plan has to press Go.
    """
    go, enable, hit = Bool("Go", external=True), Bool("En"), Bool("Hit")
    feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms", off_delay="100ms"), link="En")
    watch = Timer.clone("W")
    with Program(strict=False) as prog:
        with rung():
            reset(enable)
        with rung(~enable, feedback):
            on_delay(watch, 3000)
        with rung(go):
            latch(enable)
        with rung(watch.Done):
            latch(hit)
    plc = PLC(prog, dt=0.01)
    Harness(plc).install()
    plc.step()
    return dict(plc=plc, targets=(hit,), goal=_tag_goal(hit), kwargs=dict(max_scans=30000))


def _rescue_reconnect(cut):
    """Reconnect in field order from a retained step 98.

    The pilot holds NetworkAvailable only as an exploratory hypothesis, and stops.
    """

    def build():
        if ROOT not in sys.path:
            sys.path.append(ROOT)
        from tests.core.analysis import test_pilot_scan_zero_sequence_route_acceptance as acc
        from tests.fixtures import pilot_scan_zero_sequence_route as f_route

        logic = acc._field_order_reconnect_logic() if cut else f_route.logic
        plc = acc._retained_98(logic)
        step = f_route.SequenceStep
        return dict(
            plc=plc,
            targets=(step == 10,),
            goal=lambda tags: tags.get(step.name) == 10,
            kwargs=dict(max_scans=120),
        )

    return build


_add("rescue", "rescue/oscillator count", _rescue_oscillator)
_add("rescue", "rescue/wait on a bit a timer drops", _rescue_wait_on_a_bit)
_add("rescue", "rescue/at the rung, Go not pressed", _rescue_at_the_rung)
_add("rescue", "rescue/field-order reconnect cut", _rescue_reconnect(cut=True))
_add("rescue", "rescue/field-order reconnect full", _rescue_reconnect(cut=False))


# --------------------------------------------------------------------------- forloop
# A For calling a subroutine.  Its count != 0 decides the call for an Int count and for a Dint
# declared within 0..1,000,000 (min and max, or choices); a call under a body rung whose contact
# an iteration writes stays undecided.


def _forloop_counts():
    return {
        "dint 0..5": lambda: Dint("Dc", external=True, min=0, max=5),
        "dint choices 0,2": lambda: Dint("Dc", external=True, choices={0: "off", 2: "two"}),
        "int 0..5": lambda: Int("Dc", external=True, min=0, max=5),
        "dint 0..5 body-written contact": lambda: Dint("Dc", external=True, min=0, max=5),
    }


def _forloop_call(kind, target, avoid_alarm):
    def build():
        count = _forloop_counts()[kind]()
        btn, clr = Bool("Btn", external=True), Bool("Clr", external=True)
        y, alarm, lamp = Bool("Y"), Bool("Alarm"), Bool("Lamp")
        with Program(strict=False) as prog:
            with rung():
                with forloop(count):
                    if kind.endswith("contact"):
                        # The control's body rung reads Y, which an iteration of work latches.
                        with rung(~y):
                            call("work")
                    else:
                        call("work")
            with rung(y):
                out(lamp)
            with rung(clr):
                reset(y)
            with subroutine("work"):
                with rung(btn):
                    latch(y)
                with rung(~btn):
                    out(alarm)
        plc = PLC(prog, dt=0.01)
        plc.step()
        hit = {"Y": y, "Lamp": lamp}[target]
        return dict(
            plc=plc, targets=(hit,), goal=_tag_goal(hit), avoid=alarm if avoid_alarm else None
        )

    return build


for _kind in _forloop_counts():
    _add("forloop", f"forloop/{_kind} how(Y)", _forloop_call(_kind, "Y", False))
    _add("forloop", f"forloop/{_kind} how(Lamp)", _forloop_call(_kind, "Lamp", False))
    _add("forloop", f"forloop/{_kind} how(Y) avoid Alarm", _forloop_call(_kind, "Y", True))


def _forloop_producer_envelope(make_count):
    """Run drops when the fault subroutine, called from the For, latches Alarm with the door open.

    The two rows compare a Dint count with an Int count on the caller-guard path.  Both reach
    Done through the last resort and never call the pilot's ``_producer_caller_scope``.
    """

    def build():
        count = make_count()
        start, door = Bool("Start", external=True), Bool("Door", external=True)
        run, alarm, done = Bool("Run"), Bool("Alarm"), Bool("Done")
        timer = Timer.clone("Tr")
        with Program(strict=False) as prog:
            with rung():
                with forloop(count):
                    call("faults")
            with rung(start, ~alarm):
                latch(run)
            with rung(alarm):
                reset(run)
            with rung(run):
                on_delay(timer, 500)
            with rung(timer.Done):
                latch(done)
            with subroutine("faults"):
                with rung(run, ~door):
                    latch(alarm)
        plc = PLC(prog, dt=0.01)
        plc.step()
        return dict(plc=plc, targets=(done,), goal=_tag_goal(done), kwargs=dict(max_scans=1000))

    return build


_add(
    "forloop",
    "forloop/producer in a sub under dint 1..5",
    _forloop_producer_envelope(lambda: Dint("Dc", external=True, min=1, max=5, default=1)),
)
_add(
    "forloop",
    "forloop/producer in a sub under int 1..5",
    _forloop_producer_envelope(lambda: Int("Dc", external=True, min=1, max=5, default=1)),
)


# --------------------------------------------------------------------------- system


def _copied_system_tag(source, at_least, rtc=None):
    """Go latches Hit once a copy of *source* is at least *at_least*.

    The runtime writes *source*, so the copy can hold values that no rung writes.
    """

    def build():
        go = Bool("Go", external=True)
        copied, hit = Int("Copied"), Bool("Hit")
        with Program(strict=False) as prog:
            with rung():
                copy(source, copied)
            with rung(go, copied >= at_least):
                latch(hit)
        plc = PLC(prog, dt=0.01)
        if rtc is not None:
            plc.set_rtc(rtc)
        return dict(plc=plc, targets=(hit,), goal=_tag_goal(hit))

    return build


_add(
    "system",
    "system/copy of the scan counter reaches 50",
    _copied_system_tag(system.sys.scan_counter, 50),
)
_add(
    "system",
    "system/copy of the rtc hour reaches 13",
    _copied_system_tag(system.rtc.hour, 13, datetime(2026, 6, 15, 13, 30, 20)),
)


# --------------------------------------------------------------------------- preset


def _negative_preset_counter(literal, *, stage):
    """Build a counter with a preset of -5.  It is done at Acc 0 as soon as its reset releases.

    With *stage* a rung first copies -10 into the Acc, so five counts are needed.
    """

    def build():
        pulse, rst = Bool("Pulse", external=True), Bool("Rst", external=True)
        stage_in = Bool("Stage", external=True)
        preset = Int("Preset", default=-5)
        ctr = Counter.clone("NegCtr")
        with Program(strict=False) as prog:
            with rung(stage_in):
                copy(-10, ctr.Acc)
            with rung(pulse):
                count_up(ctr, -5 if literal else preset).reset(rst)
        plc = PLC(prog, dt=0.01)
        if stage:
            plc.patch({"Stage": True})
            plc.step()
            plc.patch({"Stage": False})
        else:
            plc.patch({"Rst": True})
        plc.step()
        return dict(plc=plc, targets=(ctr.Done,), goal=_tag_goal(ctr.Done))

    return build


def _negative_preset_on_delay(literal):
    def build():
        run = Bool("Run", external=True)
        preset = Int("Preset", default=-5)
        timer = Timer.clone("NegTon")
        with Program(strict=False) as prog:
            with rung(run):
                on_delay(timer, -5 if literal else preset, "s")
        plc = PLC(prog, dt=0.01)
        plc.step()
        return dict(plc=plc, targets=(timer.Done,), goal=_tag_goal(timer.Done))

    return build


for _kind, _literal in (("literal", True), ("tag", False)):
    _add(
        "preset",
        f"preset/counter -5 {_kind} held in reset",
        _negative_preset_counter(_literal, stage=False),
    )
    _add(
        "preset",
        f"preset/counter -5 {_kind} staged at -10",
        _negative_preset_counter(_literal, stage=True),
    )
    _add("preset", f"preset/on delay -5 s {_kind}", _negative_preset_on_delay(_literal))


# --------------------------------------------------------------------------- accs


def _accs_staged():
    """Load copies 6 into the count.  One more pulse takes it to 7, past its preset of 3."""
    load, pulse = Bool("Load", external=True), Bool("Pulse", external=True)
    count, hit = Counter.clone("Cnt"), Bool("Hit")
    with Program(strict=False) as prog:
        with rung(load):
            copy(6, count.Acc)
        with rung(pulse):
            count_up(count, 3).reset(Bool("Clear", external=True))
        with rung(count.Acc == 7):
            latch(hit)
    return dict(plc=PLC(prog, dt=0.01), targets=(hit,), goal=_tag_goal(hit))


def _accs_pair(offset):
    """Hit latches once count A is ahead of count B, which has counted twice."""

    def build():
        a, b = Counter.clone("CntA"), Counter.clone("CntB")
        hit = Bool("Hit")
        with Program(strict=False) as prog:
            with rung(Bool("PulseA", external=True)):
                count_up(a, 3).reset(Bool("ClearA", external=True))
            with rung(Bool("PulseB", external=True)):
                count_up(b, 3).reset(Bool("ClearB", external=True))
            ahead = a.Acc > b.Acc if offset is None else a.Acc == b.Acc + offset
            with rung(ahead, b.Acc >= 2):
                latch(hit)
        return dict(plc=PLC(prog, dt=0.01), targets=(hit,), goal=_tag_goal(hit))

    return build


def _accs_written_above_an_input():
    """Build a program whose copies leave Level at 0 or 10.

    Hit needs Level above the input Limit, and Limit at 5 or more.
    """
    level, limit, hit = Int("Level"), Int("Limit", external=True, min=0, max=9), Bool("Hit")
    with Program(strict=False) as prog:
        with rung(Bool("Fill", external=True)):
            copy(10, level)
        with rung(Bool("Drain", external=True)):
            copy(0, level)
        with rung(level > limit, limit >= 5):
            latch(hit)
    return dict(plc=PLC(prog, dt=0.01), targets=(hit,), goal=_tag_goal(hit))


def _accs_level_and_hmi():
    """Compare Level with 10 and with an HMI value that has no bounds, so Level has no domain."""
    level, hit = Int("Level", external=True), Bool("Hit")
    with Program(strict=False) as prog:
        with rung(level == Int("HmiLevel", external=True)):
            out(Bool("AtHmi"))
        with rung(level > 10):
            latch(hit)
    return dict(plc=PLC(prog, dt=0.01), targets=(hit,), goal=_tag_goal(hit))


_add("accs", "accs/a count a copy stages past its preset", _accs_staged)
_add("accs", "accs/a count ahead of another", _accs_pair(None))
_add("accs", "accs/a count two ahead of another", _accs_pair(2))
_add("accs", "accs/a copied level above an input", _accs_written_above_an_input)
_add("accs", "accs/an input compared with 10 and with an unbounded input", _accs_level_and_hmi)


def _accs_level_above_a_set_point():
    """No Level satisfies ``Level == Sp + 2`` while Sp is 30.

    SetSp copies 1 into Sp, and Level 3 then satisfies it.
    """
    level, setpoint, hit = (
        Int("Level", external=True, min=0, max=5),
        Int("Sp", default=30),
        Bool("Hit"),
    )
    with Program(strict=False) as prog:
        with rung(Bool("SetSp", external=True)):
            copy(1, setpoint)
        with rung(level == setpoint + 2):
            latch(hit)
    return dict(plc=PLC(prog, dt=0.01), targets=(hit,), goal=_tag_goal(hit))


_add("accs", "accs/an input equal to a set point plus 2", _accs_level_above_a_set_point)


# --------------------------------------------------------------------------- relations


def _off_delay_never_armed():
    """An off delay times only after its enable has been on once, and a fresh PLC has not.

    From power-up, Acc >= 2 needs Go on for a scan, then Go off for two seconds.  The off
    delay's profile plans that first scan, but how() stops here today ("No productive next
    action was found"), because the trace does not follow the profile's first step yet.  That
    stop is the expected answer until the trace does.
    """
    go, timer = Bool("Go", external=True), Timer.clone("Tof")
    with Program(strict=False) as prog:
        with rung(go):
            off_delay(timer, 3, "s")
    target = timer.Acc >= 2
    return dict(plc=PLC(prog, dt=0.01), targets=(target,), goal=_auto_goal(target))


def _counter_held_in_reset():
    """A counter's Done needs its reset off, and the latched Hold has it on at entry.

    Release unlatches Hold.  Then two rises of Pulse reach the preset of 2.
    """
    pulse, arm, release = (
        Bool("Pulse", external=True),
        Bool("Arm", external=True),
        Bool("Release", external=True),
    )
    hold, count = Bool("Hold"), Counter.clone("HeldCtr")
    with Program(strict=False) as prog:
        with rung(arm):
            latch(hold)
        with rung(release):
            reset(hold)
        with rung(pulse):
            count_up(count, 2).reset(hold)
    plc = PLC(prog, dt=0.01)
    plc.patch({"Arm": True})
    plc.step()
    plc.step()
    return dict(plc=plc, targets=(count.Done,), goal=_tag_goal(count.Done))


_add("relations", "relations/off delay Acc from power-up, never enabled", _off_delay_never_armed)
_add("relations", "relations/counter Done with its reset latched on", _counter_held_in_reset)
