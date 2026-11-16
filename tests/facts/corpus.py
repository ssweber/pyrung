"""Small programs, each written around one PLC idiom, for the facts check.

Each builder returns a fresh program.  The idioms follow the corpus plan: integer steppers,
seal-in latches, one-shot chains, cascaded timers, gated counters, drums, pointers,
handshakes, alarm and reset, shift registers and first-scan setup.  Some programs are
copied from the prover's tests, so the shapes those tests pinned stay covered after the
tests go.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pyrung import (
    Block,
    Bool,
    Char,
    Counter,
    Dint,
    Int,
    Program,
    Real,
    Rung,
    Timer,
    Word,
    blockcopy,
    branch,
    call,
    copy,
    count_down,
    count_up,
    event_drum,
    fall,
    fill,
    latch,
    math,
    off_delay,
    on_delay,
    out,
    reset,
    return_early,
    rise,
    search,
    shift,
    subroutine,
    system,
    time_drum,
    toggle,
)
from pyrung.core import Or
from pyrung.core.tag import TagType

CORPUS: dict[str, Callable[[], Any]] = {}


def idiom(fn: Callable[[], Any]) -> Callable[[], Any]:
    CORPUS[fn.__name__] = fn
    return fn


# ---------------------------------------------------------------------------
# Seal-in latches, alarm and reset
# ---------------------------------------------------------------------------


@idiom
def seal_in_motor() -> Any:
    start = Bool("Start", external=True)
    stop = Bool("Stop", external=True)
    motor = Bool("Motor")
    with Program() as prog:
        with Rung(Or(start, motor), ~stop):
            out(motor)
    return prog


@idiom
def latch_unlatch_with_priority() -> Any:
    set_pb = Bool("SetPB", external=True)
    reset_pb = Bool("ResetPB", external=True)
    lamp = Bool("Lamp")
    with Program() as prog:
        with Rung(set_pb):
            latch(lamp)
        with Rung(reset_pb):
            reset(lamp)
    return prog


@idiom
def alarm_with_acknowledge() -> Any:
    level = Int("Level", external=True, min=0, max=100)
    ack = Bool("Ack", external=True)
    alarm = Bool("HighAlarm")
    horn = Bool("Horn")
    unacked = Bool("Unacked")
    with Program() as prog:
        with Rung(level > 80):
            latch(alarm)
        with Rung(rise(alarm)):
            latch(unacked)
        with Rung(ack, level <= 70):
            reset(alarm)
        with Rung(ack):
            reset(unacked)
        with Rung(unacked):
            out(horn)
    return prog


@idiom
def alarm_delayed_by_timer() -> Any:
    pressure = Real("Pressure", external=True, min=0.0, max=10.0)
    reset_pb = Bool("AlarmReset", external=True)
    high = Timer.clone("HighFor")
    alarm = Bool("PressureAlarm")
    with Program() as prog:
        with Rung(pressure > 7.5):
            on_delay(high, 500)
        with Rung(high.Done):
            latch(alarm)
        with Rung(reset_pb, ~high.Done):
            reset(alarm)
    return prog


# ---------------------------------------------------------------------------
# One-shots and toggles
# ---------------------------------------------------------------------------


@idiom
def one_shot_chain() -> Any:
    button = Bool("Button", external=True)
    pulse = Bool("Pulse")
    pulse_count = Int("Pulses")
    falling = Bool("Released")
    with Program() as prog:
        with Rung(rise(button)):
            out(pulse)
        with Rung(pulse, pulse_count < 9):
            math(pulse_count + 1, pulse_count)
        with Rung(fall(button)):
            out(falling)
        with Rung(pulse_count >= 9, falling):
            copy(0, pulse_count)
    return prog


@idiom
def push_on_push_off() -> Any:
    button = Bool("PushButton", external=True)
    light = Bool("Light")
    with Program() as prog:
        with Rung(rise(button)):
            toggle(light)
    return prog


# ---------------------------------------------------------------------------
# Timers
# ---------------------------------------------------------------------------


@idiom
def cascaded_timers() -> Any:
    run = Bool("Run", external=True)
    first = Timer.clone("Stage1")
    second = Timer.clone("Stage2")
    third = Timer.clone("Stage3")
    lamp = Bool("AllDone")
    with Program() as prog:
        with Rung(run):
            on_delay(first, 200)
        with Rung(first.Done):
            on_delay(second, 300)
        with Rung(second.Done):
            on_delay(third, 100)
        with Rung(third.Done):
            out(lamp)
    return prog


@idiom
def retentive_timer_with_reset() -> Any:
    run = Bool("Running", external=True)
    clear = Bool("ClearHours", external=True)
    hours = Timer.clone("RunTime")
    service = Bool("ServiceDue")
    with Program() as prog:
        with Rung(run):
            on_delay(hours, 2, unit="s").reset(clear)
        with Rung(hours.Done):
            out(service)
    return prog


@idiom
def off_delay_fan() -> Any:
    heater = Bool("Heater", external=True)
    cool = Timer.clone("CoolDown")
    fan = Bool("Fan")
    with Program() as prog:
        with Rung(heater):
            off_delay(cool, 400)
        with Rung(cool.Done):
            out(fan)
    return prog


@idiom
def self_resetting_pulse_timer() -> Any:
    enable = Bool("PulseEnable", external=True)
    tick = Timer.clone("Tick")
    ticks = Int("TickCount")
    with Program() as prog:
        with Rung(enable, ~tick.Done):
            on_delay(tick, 100)
        with Rung(tick.Done):
            math(ticks + 1, ticks)
        with Rung(ticks >= 20):
            copy(0, ticks)
    return prog


@idiom
def timer_acc_compared() -> Any:
    fill = Bool("FillValve", external=True)
    elapsed = Timer.clone("FillTime")
    half = Bool("HalfFull")
    full = Bool("Full")
    with Program() as prog:
        with Rung(fill):
            on_delay(elapsed, 800)
        with Rung(elapsed.Acc >= 400):
            out(half)
        with Rung(elapsed.Done):
            out(full)
    return prog


@idiom
def timer_with_preset_tag() -> Any:
    start = Bool("CycleStart", external=True)
    preset = Int("CyclePreset", external=True, min=50, max=300)
    dwell = Timer.clone("Dwell")
    done = Bool("DwellDone")
    with Program() as prog:
        with Rung(start):
            on_delay(dwell, preset)
        with Rung(dwell.Done):
            out(done)
    return prog


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


@idiom
def gated_counter() -> Any:
    part = Bool("PartSensor", external=True)
    gate = Bool("GateOpen", external=True)
    clear = Bool("BatchClear", external=True)
    batch = Counter.clone("Batch")
    full = Bool("BatchFull")
    with Program() as prog:
        with Rung(part, gate):
            count_up(batch, 5).reset(clear)
        with Rung(batch.Done):
            out(full)
    return prog


@idiom
def up_down_counter() -> Any:
    enter = Bool("CarIn", external=True)
    leave = Bool("CarOut", external=True)
    clear = Bool("LotClear", external=True)
    lot = Counter.clone("Lot")
    full = Bool("LotFull")
    with Program() as prog:
        with Rung(enter):
            count_up(lot, 4).down(leave).reset(clear)
        with Rung(lot.Done):
            out(full)
    return prog


@idiom
def down_counter() -> Any:
    used = Bool("Dispense", external=True)
    refill = Bool("Refill", external=True)
    stock = Counter.clone("Stock")
    empty = Bool("Empty")
    with Program() as prog:
        with Rung(used):
            count_down(stock, -3).reset(refill)
        with Rung(stock.Done):
            out(empty)
    return prog


@idiom
def counter_acc_bands() -> Any:
    pulse = Bool("Encoder", external=True)
    home = Bool("Home", external=True)
    pos = Counter.clone("Position")
    zone_a = Bool("ZoneA")
    zone_b = Bool("ZoneB")
    with Program() as prog:
        with Rung(pulse):
            count_up(pos, 12).reset(home)
        with Rung(pos.Acc >= 3, pos.Acc < 7):
            out(zone_a)
        with Rung(pos.Acc >= 7):
            out(zone_b)
    return prog


# ---------------------------------------------------------------------------
# Integer steppers
# ---------------------------------------------------------------------------


@idiom
def integer_stepper() -> Any:
    start = Bool("Go", external=True)
    at_top = Bool("AtTop", external=True)
    at_bottom = Bool("AtBottom", external=True)
    step = Int("Step")
    up = Bool("MoveUp")
    down = Bool("MoveDown")
    dwell = Timer.clone("TopDwell")
    with Program() as prog:
        with Rung(step == 0, rise(start)):
            copy(1, step)
        with Rung(step == 1):
            out(up)
        with Rung(step == 1, at_top):
            copy(2, step)
        with Rung(step == 2):
            on_delay(dwell, 150)
        with Rung(step == 2, dwell.Done):
            copy(3, step)
        with Rung(step == 3):
            out(down)
        with Rung(step == 3, at_bottom):
            copy(0, step)
    return prog


@idiom
def stepper_with_math_advance() -> Any:
    advance = Bool("Advance", external=True)
    abort = Bool("Abort", external=True)
    state = Int("State")
    lamps = [Bool(f"Lamp{i}") for i in range(4)]
    with Program(strict=False) as prog:
        with Rung(rise(advance), state < 3):
            math(state + 1, state)
        with Rung(rise(advance), state >= 3):
            copy(0, state)
        with Rung(abort):
            copy(0, state)
        for i, lamp in enumerate(lamps):
            with Rung(state == i):
                out(lamp)
    return prog


@idiom
def char_state_machine() -> Any:
    call_button = Bool("CallButton", external=True)
    state = Char("LiftState")
    ride = Timer.clone("Ride")
    with Program() as prog:
        with Rung(state == "", call_button):
            copy("m", state)
        with Rung(state == "m"):
            on_delay(ride, 120)
        with Rung(ride.Done):
            copy("a", state)
        with Rung(state == "a", ~call_button):
            copy("", state)
    return prog


# ---------------------------------------------------------------------------
# Drums and shift registers
# ---------------------------------------------------------------------------


@idiom
def event_drum_sequence() -> Any:
    run = Bool("DrumRun", external=True)
    events = [Bool(f"Event{i}", external=True) for i in range(1, 4)]
    clear = Bool("DrumClear", external=True)
    step = Int("DrumStep")
    done = Bool("DrumDone")
    valves = [Bool("ValveA"), Bool("ValveB")]
    with Program() as prog:
        with Rung(run):
            event_drum(
                outputs=valves,
                events=events,
                pattern=[[1, 0], [0, 1], [1, 1]],
                current_step=step,
                completion_flag=done,
            ).reset(clear)
    return prog


@idiom
def time_drum_sequence() -> Any:
    run = Bool("WashRun", external=True)
    clear = Bool("WashClear", external=True)
    step = Int("WashStep")
    acc = Int("WashAcc")
    done = Bool("WashDone")
    outputs = [Bool("Fill"), Bool("Agitate"), Bool("Drain")]
    with Program() as prog:
        with Rung(run):
            time_drum(
                outputs=outputs,
                presets=[100, 200, 80],
                unit="ms",
                pattern=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                current_step=step,
                accumulator=acc,
                completion_flag=done,
            ).reset(clear)
    return prog


@idiom
def shift_register_reject() -> Any:
    bad = Bool("BadPart", external=True)
    clock = Bool("ConveyorClock", external=True)
    clear = Bool("TrackClear", external=True)
    track = Block("Track", TagType.BOOL, 1, 4)
    kicker = Bool("Kicker")
    with Program() as prog:
        with Rung(bad):
            shift(track.select(1, 4)).clock(clock).reset(clear)
        with Rung(track[4]):
            out(kicker)
    return prog


@idiom
def word_history_window() -> Any:
    sample = Bool("Sample", external=True)
    reading = Int("Reading", external=True, min=0, max=50)
    log = Block("Hist", TagType.INT, 1, 3)
    high = Bool("RecentHigh")
    with Program() as prog:
        with Rung(rise(sample)):
            blockcopy(log.select(1, 2), log.select(2, 3))
            copy(reading, log[1])
        with Rung(Or(log[1] > 40, log[2] > 40, log[3] > 40)):
            out(high)
    return prog


# ---------------------------------------------------------------------------
# Pointers and indirect addressing
# ---------------------------------------------------------------------------


@idiom
def pointer_recipe_load() -> Any:
    pick = Int("RecipeNo", external=True, min=1, max=3)
    load = Bool("LoadRecipe", external=True)
    recipes = Block("Recipe", TagType.INT, 1, 3)
    setpoint = Int("Setpoint")
    hot = Bool("Hot")
    with Program() as prog:
        with Rung(system.sys.first_scan):
            copy(10, recipes[1])
            copy(20, recipes[2])
            copy(30, recipes[3])
        with Rung(rise(load)):
            copy(recipes[pick], setpoint)
        with Rung(setpoint >= 20):
            out(hot)
    return prog


@idiom
def pointer_write_cycle() -> Any:
    store = Bool("StoreValue", external=True)
    value = Int("NewValue", external=True, min=0, max=9)
    slot = Int("Slot")
    buf = Block("Buf", TagType.INT, 1, 4)
    wrapped = Bool("Wrapped")
    with Program() as prog:
        with Rung(rise(store)):
            copy(value, buf[slot + 1])
            math(slot + 1, slot)
        with Rung(slot >= 4):
            copy(0, slot)
            latch(wrapped)
    return prog


@idiom
def search_for_value() -> Any:
    find = Bool("Find", external=True)
    wanted = Int("Wanted", external=True, min=0, max=5)
    table = Block("Table", TagType.INT, 1, 4)
    where = Int("FoundAt")
    found = Bool("Found")
    with Program() as prog:
        with Rung(system.sys.first_scan):
            copy(2, table[1])
            copy(4, table[2])
            copy(1, table[3])
            copy(5, table[4])
        with Rung(rise(find)):
            search(table.select(1, 4) == wanted, result=where, found=found)
    return prog


# ---------------------------------------------------------------------------
# Handshakes
# ---------------------------------------------------------------------------


@idiom
def request_acknowledge_handshake() -> Any:
    want = Bool("NeedPart", external=True)
    ack = Bool("RobotAck", external=True)
    busy = Bool("RobotBusy", external=True)
    request = Bool("PartRequest")
    waiting = Bool("Waiting")
    timeout = Timer.clone("AckTimeout")
    fault = Bool("HandshakeFault")
    with Program() as prog:
        with Rung(want, ~waiting, ~fault):
            latch(request)
            latch(waiting)
        with Rung(request, ack):
            reset(request)
        with Rung(waiting, ~request, ~busy):
            reset(waiting)
        with Rung(request):
            on_delay(timeout, 300)
        with Rung(timeout.Done):
            latch(fault)
            reset(request)
            reset(waiting)
        with Rung(~want, fault):
            reset(fault)
    return prog


# ---------------------------------------------------------------------------
# First-scan setup and constants
# ---------------------------------------------------------------------------


@idiom
def first_scan_setup() -> Any:
    run = Bool("Enable", external=True)
    limit = Int("Limit")
    count = Int("Count")
    at_limit = Bool("AtLimit")
    with Program() as prog:
        with Rung(system.sys.first_scan):
            copy(5, limit)
        with Rung(rise(run), count < limit):
            math(count + 1, count)
        with Rung(count >= limit):
            out(at_limit)
        with Rung(~run, at_limit):
            copy(0, count)
    return prog


@idiom
def reference_constant_compare() -> Any:
    speed = Int("Speed", external=True, min=0, max=2000)
    max_speed = Int("MaxSpeed", default=1500)
    over = Bool("OverSpeed")
    with Program() as prog:
        with Rung(speed > max_speed):
            out(over)
    return prog


# ---------------------------------------------------------------------------
# Data flow: copies, math, scratch
# ---------------------------------------------------------------------------


@idiom
def scaled_copy_chain() -> Any:
    raw = Int("RawCounts", external=True, min=0, max=4000)
    eng = Int("Scaled")
    shown = Int("Display")
    high = Bool("ScaledHigh")
    with Program() as prog:
        with Rung():
            math(raw + 100, eng)
            copy(eng, shown)
        with Rung(shown > 3000):
            out(high)
    return prog


@idiom
def scratch_register() -> Any:
    a = Int("InA", external=True, min=0, max=20)
    b = Int("InB", external=True, min=0, max=20)
    tmp = Int("Tmp")
    big = Bool("SumBig")
    with Program() as prog:
        with Rung():
            math(a + b, tmp)
        with Rung(tmp > 30):
            out(big)
        with Rung():
            copy(0, tmp)
    return prog


@idiom
def dint_totalizer() -> Any:
    flow = Bool("FlowPulse", external=True)
    clear = Bool("TotalClear", external=True)
    total = Dint("Total")
    alarm = Bool("TotalHigh")
    with Program() as prog:
        with Rung(rise(flow)):
            math(total + 250, total)
        with Rung(clear):
            copy(0, total)
        with Rung(total >= 2000):
            out(alarm)
    return prog


@idiom
def word_status_bits() -> Any:
    status = Word("Status", external=True)
    ready = Bool("DriveReady")
    with Program() as prog:
        with Rung(status == 0x0037):
            out(ready)
    return prog


@idiom
def fill_block_on_reset() -> Any:
    clear = Bool("ClearAll", external=True)
    add = Bool("AddOne", external=True)
    bins = Block("Bin", TagType.INT, 1, 3)
    any_full = Bool("AnyFull")
    with Program() as prog:
        with Rung(clear):
            fill(0, bins.select(1, 3))
        with Rung(rise(add)):
            math(bins[1] + 1, bins[1])
            math(bins[2] + 2, bins[2])
        with Rung(Or(bins[1] > 4, bins[2] > 6)):
            out(any_full)
    return prog


# ---------------------------------------------------------------------------
# Subroutines and branches
# ---------------------------------------------------------------------------


@idiom
def subroutine_with_return() -> Any:
    auto = Bool("Auto", external=True)
    inhibit = Bool("Inhibit", external=True)
    count = Int("SubCount")
    pump = Bool("Pump")
    with Program() as prog:
        with Rung(auto):
            call("AutoSeq")
        with subroutine("AutoSeq"):
            with Rung(inhibit):
                return_early()
            with Rung(count < 5):
                math(count + 1, count)
            with Rung(count >= 5):
                out(pump)
                copy(0, count)
    return prog


@idiom
def branch_outputs() -> Any:
    sel = Bool("Select", external=True)
    run = Bool("RunCmd", external=True)
    left = Bool("LeftOut")
    right = Bool("RightOut")
    with Program() as prog:
        with Rung(run):
            with branch(sel):
                out(left)
            with branch(~sel):
                out(right)
    return prog


# ---------------------------------------------------------------------------
# Shapes copied from the prover's tests
# ---------------------------------------------------------------------------


@idiom
def funcdep_offset_copy() -> Any:
    """``test_prove_functional_dep_projection``: Y = X + 5 with X reset after."""
    x = Int("X", min=0, max=5)
    y = Int("Y", min=5, max=10)
    seen = Bool("Seen")
    bump = Bool("Bump", external=True)
    with Program(strict=False) as prog:
        with Rung(y > 7):
            out(seen)
        with Rung():
            math(x + 5, y)
            copy(0, x)
        with Rung(rise(bump)):
            copy(3, x)
    return prog


@idiom
def funcdep_mirror() -> Any:
    """``test_prove_functional_dep_projection``: Y is a copy of X, written in the same rung."""
    step = Int("Mode")
    shown = Int("ModeShown")
    nxt = Bool("NextMode", external=True)
    lamp = Bool("ModeLamp")
    with Program() as prog:
        with Rung(rise(nxt)):
            math(step + 1, step)
            copy(step, shown)
        with Rung(step >= 3):
            copy(0, step)
            copy(0, shown)
        with Rung(shown == 2):
            out(lamp)
    return prog


@idiom
def init_constant_flag() -> Any:
    """``test_prove_init_constant_projection``: a flag set once on the first scan."""
    mode = Int("MachineMode")
    initialized = Bool("Initialized")
    go = Bool("GoButton", external=True)
    lamp = Bool("ModeTwo")
    with Program() as prog:
        with Rung(~initialized):
            copy(2, mode)
            latch(initialized)
        with Rung(go, mode == 2):
            out(lamp)
    return prog


@idiom
def tag_compared_inputs() -> Any:
    """``test_prove_tag_compared_inputs``: an input compared with another input."""
    a = Int("Left", external=True, min=0, max=10)
    b = Int("Right", external=True, min=0, max=10)
    bigger = Bool("LeftBigger")
    equal = Bool("Equal")
    with Program() as prog:
        with Rung(a > b):
            out(bigger)
        with Rung(a == b):
            out(equal)
    return prog


@idiom
def edge_on_written_bit() -> Any:
    """``test_prove_edge_demotion``: a rise() on a bit the program writes."""
    sensor = Bool("Sensor", external=True)
    seen = Bool("SeenPart")
    count = Int("Seen")
    with Program() as prog:
        with Rung(sensor):
            out(seen)
        with Rung(rise(seen), count < 3):
            math(count + 1, count)
        with Rung(count >= 3, ~sensor):
            copy(0, count)
    return prog


@idiom
def funcdep_follows_its_source() -> Any:
    """Y = X + 5, rewritten on every rung that moves X."""
    x = Int("Pos")
    y = Int("PosOffset")
    bump = Bool("Jog", external=True)
    far = Bool("Far")
    with Program() as prog:
        with Rung(rise(bump)):
            math(x + 1, x)
            math(x + 5, y)
        with Rung(x >= 4):
            copy(0, x)
            math(x + 5, y)
        with Rung(y > 7):
            out(far)
    return prog


@idiom
def funcdep_written_before_its_source_moves() -> Any:
    """Y = X + 5 is written, then X moves in the same rung."""
    x = Int("Lead")
    y = Int("Lag")
    bump = Bool("Push", external=True)
    far = Bool("LagFar")
    with Program() as prog:
        with Rung(rise(bump)):
            math(x + 5, y)
            math(x + 1, x)
        with Rung(x >= 4):
            copy(0, x)
            copy(5, y)
        with Rung(y > 7):
            out(far)
    return prog


@idiom
def named_states_from_constants() -> Any:
    """The named-state idiom: states are constant tags, copied in and compared."""
    idle = Int("ST_IDLE", default=0)
    filling = Int("ST_FILLING", default=1)
    full = Int("ST_FULL", default=2)
    state = Int("TankState")
    start = Bool("FillStart", external=True)
    high = Bool("HighLevel", external=True)
    drain = Bool("DrainDone", external=True)
    valve = Bool("InletValve")
    with Program() as prog:
        with Rung(state == idle, start):
            copy(filling, state)
        with Rung(state == filling):
            out(valve)
        with Rung(state == filling, high):
            copy(full, state)
        with Rung(state == full, drain):
            copy(idle, state)
    return prog


@idiom
def busy_bit_cleared_each_scan() -> Any:
    """A bit set and cleared inside one scan, so it rests False between scans."""
    a = Bool("ReqA", external=True)
    b = Bool("ReqB", external=True)
    busy = Bool("Busy")
    granted_b = Bool("GrantB")
    granted_a = Bool("GrantA")
    with Program() as prog:
        with Rung(a):
            latch(busy)
            out(granted_a)
        with Rung(b, ~busy):
            out(granted_b)
        with Rung():
            reset(busy)
    return prog
