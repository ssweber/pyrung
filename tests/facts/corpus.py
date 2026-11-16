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
    Harness,
    Int,
    Physical,
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
    forloop,
    latch,
    math,
    off_delay,
    on_delay,
    out,
    pack_words,
    receive,
    reset,
    return_early,
    rise,
    search,
    shift,
    subroutine,
    system,
    time_drum,
    to_text,
    to_value,
    toggle,
)
from pyrung.core import PLC, Or
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
            math(x + 5, y)
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


@idiom
def hmi_command_word_cleared_by_ladder() -> Any:
    """An HMI writes a command number, and the ladder clears it once taken."""
    command = Int("HmiCommand", external=True)
    running = Bool("ConveyorRun")
    with Program() as prog:
        with Rung(command == 1):
            latch(running)
            copy(0, command)
        with Rung(command == 2):
            reset(running)
            copy(0, command)
    return prog


@idiom
def hmi_setpoint_clamped_by_ladder() -> Any:
    """An HMI writes a setpoint, and the ladder pulls it back into range."""
    setpoint = Int("SpeedSetpoint", external=True)
    fast = Bool("FastMode")
    with Program() as prog:
        with Rung(setpoint > 100):
            copy(100, setpoint)
        with Rung(setpoint < 0):
            copy(0, setpoint)
        with Rung(setpoint >= 60):
            out(fast)
    return prog


@idiom
def hmi_request_taken_when_nonzero() -> Any:
    """Any nonzero request starts a job, and the ladder clears the request."""
    request = Int("JobRequest", external=True)
    job = Int("JobNumber")
    busy = Bool("JobBusy")
    done = Bool("JobDone", external=True)
    with Program() as prog:
        with Rung(request != 0, ~busy):
            copy(request, job)
            latch(busy)
            copy(0, request)
        with Rung(busy, done):
            reset(busy)
    return prog


# ---------------------------------------------------------------------------
# Inputs the outside sets, harness feedback, and stores that convert
# ---------------------------------------------------------------------------

#: Idioms that need a harness on each PLC: name -> the function that installs it.
PREPARE: dict[str, Callable[[PLC], None]] = {}


def _harness(plc: PLC) -> None:
    Harness(plc).install()


@idiom
def valve_with_limit_switch_watchdog() -> Any:
    """A valve's limit switch follows it through the harness, and a watchdog trips on a stall.

    The watchdog's 100 ms is shorter than the switch's 200 ms delay, so it trips on every opening.
    """
    command = Bool("OpenCmd", external=True)
    valve = Bool("Valve")
    opened = Bool("ValveOpen", physical=Physical("ValveOpen", on_delay="200ms"), link="Valve")
    stall = Timer.clone("Stall")
    fault = Bool("ValveFault")
    clear = Bool("FaultClear", external=True)
    with Program() as prog:
        with Rung(command):
            out(valve)
        with Rung(valve, ~opened):
            on_delay(stall, 100)
        with Rung(stall.Done):
            latch(fault)
        with Rung(clear):
            reset(fault)
    return prog


PREPARE["valve_with_limit_switch_watchdog"] = _harness


@idiom
def field_value_copied_and_compared() -> Any:
    """A level that answers a fill valve in the field is copied, and only the copy is compared.

    The level has ``link=`` and no ``physical=``, so no harness drives it.
    """
    fill = Bool("FillValve")
    level = Int("FieldLevel", link="FillValve")
    held = Int("SampledLevel")
    with Program() as prog:
        with Rung(Bool("FillCmd", external=True)):
            out(fill)
        with Rung(Bool("TakeSample", external=True)):
            copy(level, held)
        with Rung(held > 50):
            out(Bool("SampleHigh"))
    return prog


@idiom
def field_value_summed_and_compared() -> Any:
    """A field level plus an operator's bias is stored on a sample, and the sum is compared.

    The facts trace a comparison back through a copy, but not through a sum of two tags.
    """
    level = Int("SumLevel", link="SumFill")
    bias = Int("Bias", external=True, min=0, max=10)
    total = Int("BiasedLevel")
    with Program() as prog:
        with Rung(Bool("SumFillCmd", external=True)):
            out(Bool("SumFill"))
        with Rung(Bool("TakeSum", external=True)):
            math(level + bias, total)
        with Rung(total > 50):
            out(Bool("BiasedHigh"))
    return prog


@idiom
def field_count_the_ladder_clears() -> Any:
    """A count the field sets on a linked counter, which the ladder clears on a reset button."""
    count = Int("FieldCount", link="CountEnable")
    with Program() as prog:
        with Rung(Bool("CountCmd", external=True)):
            out(Bool("CountEnable"))
        with Rung(Bool("ClearCount", external=True)):
            copy(0, count)
        with Rung(count > 10):
            out(Bool("CountHigh"))
    return prog


@idiom
def loop_adds_three_times() -> Any:
    """A forloop adds 1 to a total three times on each scan Go is on, and a rung clears it."""
    total = Int("LoopTotal")
    with Program() as prog:
        with Rung(Bool("LoopGo", external=True)):
            with forloop(3):
                math(total + 1, total)
        with Rung(total > 5):
            copy(0, total)
    return prog


def _receive_from_peer(dest: Any) -> None:
    # A string target does no I/O, so in a run no peer answers.  The run sets the payload
    # as a peer would.
    receive(
        target="peer",
        remote_start="DS501",
        dest=dest,
        receiving=Bool("Receiving"),
        success=Bool("ReceiveOk"),
        error=Bool("ReceiveError"),
        exception_response=Int("ReceiveCode"),
    )


@idiom
def received_value_the_ladder_clears() -> Any:
    """A value a peer sends through receive(), which the ladder clears once it reads 1."""
    incoming = Int("Remote")
    with Program() as prog:
        with Rung(Bool("Poll", external=True)):
            _receive_from_peer(incoming)
        with Rung(incoming == 1):
            copy(0, incoming)
        with Rung(incoming > 5):
            out(Bool("RemoteHigh"))
    return prog


@idiom
def received_count_the_ladder_adds_to() -> Any:
    """Registers a peer sends, one of which the ladder also counts up.

    The peer can lower the count, so the count is no up counter.
    """
    regs = Block("Regs", TagType.INT, 501, 510)
    with Program() as prog:
        with Rung(Bool("PollRegs", external=True)):
            _receive_from_peer(regs.select(501, 510))
        with Rung(Bool("Tick", external=True)):
            math(regs[503] + 1, regs[503])
        with Rung(regs[503] > 100):
            out(Bool("CountHigh2"))
    return prog


@idiom
def real_input_between_whole_numbers() -> Any:
    """A Real input with whole-number bounds holds the values between them too."""
    pressure = Real("LinePressure", external=True, min=0.0, max=10.0)
    high = Bool("PressureHigh")
    with Program() as prog:
        with Rung(pressure > 5):
            out(high)
    return prog


@idiom
def math_result_wraps() -> Any:
    """A math() into an Int wraps what a copy() would clamp."""
    go = Bool("Load", external=True)
    total = Dint("BigTotal")
    low = Int("LowWord")
    negative = Bool("LowNegative")
    with Program() as prog:
        with Rung(go):
            copy(40000, total)
        with Rung():
            math(total, low)
        with Rung(low < 0):
            out(negative)
    return prog


@idiom
def math_literal_wraps() -> Any:
    """A math() of a constant past an Int's range stores it wrapped."""
    go = Bool("LoadConst", external=True)
    low = Int("WrappedConst")
    small = Bool("ConstSmall")
    with Program() as prog:
        with Rung(go):
            math(70000, low)
        with Rung(low < 5000, low > 0):
            out(small)
    return prog


@idiom
def hmi_value_copied_after_ladder_default() -> Any:
    """An HMI writes a setpoint the ladder also defaults, and a copy of it is compared."""
    setpoint = Int("HmiSetpoint", external=True)
    working = Int("WorkingSetpoint")
    with Program() as prog:
        with Rung(Bool("LoadDefault", external=True)):
            copy(5, setpoint)
        with Rung(Bool("Apply", external=True)):
            copy(setpoint, working)
        with Rung(working > 50):
            out(Bool("HighSetpoint"))
    return prog


@idiom
def compared_input_copied_and_compared() -> Any:
    """An input compared with one literal is copied, and the copy is compared with another."""
    raw = Int("RawLevel", external=True)
    level = Int("HeldLevel")
    with Program() as prog:
        with Rung(raw > 10):
            out(Bool("RawAbove"))
        with Rung(Bool("Sample", external=True)):
            copy(raw, level)
        with Rung(level > 50):
            out(Bool("LevelHigh"))
    return prog


# ---------------------------------------------------------------------------
# Writes a rung makes only for some values
# ---------------------------------------------------------------------------


@idiom
def short_text_keeps_the_characters_after_it() -> Any:
    """A short to_text() leaves a character another rung wrote, and a rung reads it."""
    msg = Block("ShortMsg", TagType.CHAR, 1, 10)
    value = Int("ShortValue", external=True, choices={1: "one", 12345: "five digits"})
    seen = Int("ShortSeen")
    with Program() as prog:
        with Rung(Bool("ShortMark", external=True)):
            copy("4", msg[5])
        with Rung():
            call("ShortText")
        with subroutine("ShortText"):
            with Rung():
                copy(value, msg[1], convert=to_text())
            with Rung(msg[5] != "4"):
                copy(7, seen)
            with Rung():
                copy(seen, Int("ShortShown"))
    return prog


@idiom
def pointer_write_keeps_the_other_element() -> Any:
    """A copy through a pointer leaves the element it does not point at, and a rung reads it."""
    regs = Block("PtrReg", TagType.INT, 1, 4)
    pointer = Int("PtrAt", external=True, choices={1: "first", 2: "second"})
    seen = Int("PtrSeen")
    with Program() as prog:
        with Rung(Bool("PtrMark", external=True)):
            copy(4, regs[1])
        with Rung():
            call("PtrWrite")
        with subroutine("PtrWrite"):
            with Rung():
                copy(9, regs[pointer])
            with Rung(regs[1] != 4):
                copy(7, seen)
            with Rung():
                copy(seen, Int("PtrShown"))
    return prog


@idiom
def pointer_copy_of_a_source_is_no_dependency() -> Any:
    """A copy through a pointer follows its source only at the element it points at."""
    regs = Block("DepReg", TagType.INT, 1, 4)
    pointer = Int("DepAt", external=True, choices={1: "first", 2: "second"})
    source = Int("DepSource")
    with Program() as prog:
        with Rung(regs[1] == 2):
            out(Bool("DepSeen"))
        with Rung(Bool("DepLoad", external=True)):
            copy(Int("DepInput", external=True, min=0, max=3), source)
        with Rung():
            copy(source, regs[pointer])
    return prog


# ---------------------------------------------------------------------------
# Reals that hold NaN or Inf
# ---------------------------------------------------------------------------


@idiom
def math_overflow_then_difference() -> Any:
    """A math() past the Real range stores Inf, and Inf minus Inf is NaN.

    Inf compares like a large number, but a later formula tells it apart.  A NaN is
    neither >= 0 nor < 0, and a copy of it into an Int stores 0.
    """
    big = Real("BigReal")
    over = Real("OverReal")
    diff = Real("DiffReal")
    with Program() as prog:
        with Rung(Bool("LoadBig", external=True)):
            copy(3.0e38, big)
        with Rung(Bool("ClearBig", external=True)):
            copy(1.0, big)
        with Rung(Bool("Scale", external=True)):
            math(big * 10, over)
        with Rung(over > 1.0e38):
            out(Bool("OverHigh"))
        with Rung(Bool("Subtract", external=True)):
            math(over - over, diff)
        with Rung(diff >= 0):
            out(Bool("DiffNotBelow"))
        with Rung(diff < 0):
            out(Bool("DiffBelow"))
        with Rung(Bool("ShowDiff", external=True)):
            copy(diff, Int("DiffShown"))
    return prog


@idiom
def nan_copied_and_compared_with_itself() -> Any:
    """A division by a tiny Real stores Inf, and 0 to a negative power is Inf in a formula.

    Inf minus Inf is NaN, a copy of a NaN is a NaN, and a NaN is not equal to itself.
    """
    divisor = Real("Divisor", external=True, choices={0.0: "zero", 1.0e-39: "tiny", 2.0: "two"})
    inverse = Real("Inverse")
    gap = Real("InverseGap")
    kept = Real("KeptGap")
    with Program() as prog:
        with Rung(Bool("Invert", external=True)):
            math(1.0 / divisor, inverse)
        with Rung(inverse > 1.0):
            out(Bool("InverseAboveOne"))
        with Rung(Bool("Cancel", external=True)):
            math(divisor**-1 - divisor**-1, gap)
        with Rung(Bool("KeepGap", external=True)):
            copy(gap, kept)
        with Rung(kept == kept):
            out(Bool("KeptIsANumber"))
        with Rung(Bool("ShowInverse", external=True)):
            copy(inverse, Int("InverseShown"))
    return prog


@idiom
def words_packed_into_a_real() -> Any:
    """Two words packed into a Real can be the bits of a NaN or an Inf."""
    words = Block("PackedWord", TagType.INT, 1, 2)
    high = Int("HighWord", external=True, choices={0: "zero", 0x7F80: "Inf", 0x7FC0: "NaN"})
    packed = Real("PackedReal")
    with Program() as prog:
        with Rung(Bool("LoadWords", external=True)):
            copy(high, words[2])
        with Rung(Bool("PackWords", external=True)):
            pack_words(words.select(1, 2), packed)
        with Rung(packed >= 0):
            out(Bool("PackedNotBelow"))
        with Rung(packed < 0):
            out(Bool("PackedBelow"))
        with Rung(Bool("ShowPacked", external=True)):
            copy(packed, Int("PackedShown"))
    return prog


# ---------------------------------------------------------------------------
# Writes through a pointer into a block of more than 1000 elements
# ---------------------------------------------------------------------------


@idiom
def pointer_write_into_a_big_real_block() -> Any:
    """A write through a pointer with no declared bounds, into a block of more than 1000
    elements, lists no targets.  It can store into the element a rung reads.

    The write stores Inf here, and Inf minus Inf is NaN.  A copy of the element into an
    Int stores 100 too.
    """
    far = Block("FarReal", TagType.REAL, 1, 2000)
    at = Int("FarRealAt", external=True)
    base = Real("FarRealBase")
    over = Real("FarRealOver")
    shown = Int("FarRealShown")
    with Program() as prog:
        with Rung(at == 5):
            out(Bool("FarRealAtFive"))
        with Rung(Bool("FarRealLoad", external=True)):
            copy(3.0e38, base)
        with Rung(Bool("FarRealTen", external=True)):
            copy(10.0, base)
        with Rung(Bool("FarRealScale", external=True)):
            math(base * 10, over)
        with Rung(Bool("FarRealWrite", external=True)):
            copy(over, far[at])
        with Rung(far[5] - far[5] == 0.0):
            out(Bool("FarRealIsNumber"))
        with Rung(Bool("FarRealShow", external=True)):
            copy(far[5], shown)
        with Rung(shown > 50):
            out(Bool("FarRealAbove"))
        with Rung(shown < 200):
            out(Bool("FarRealBelow"))
    return prog


@idiom
def pointer_write_into_a_big_int_block() -> Any:
    """The Int form, and the element has a write of its own too."""
    far = Block("FarInt", TagType.INT, 1, 2000)
    at = Int("FarIntAt", external=True)
    source = Int("FarIntSource", external=True, choices={0: "zero", 100: "hundred"})
    shown = Int("FarIntShown")
    with Program() as prog:
        with Rung(at == 5):
            out(Bool("FarIntAtFive"))
        with Rung(Bool("FarIntSet", external=True)):
            copy(3, far[5])
        with Rung(Bool("FarIntWrite", external=True)):
            copy(source, far[at])
        with Rung(far[5] > 50):
            out(Bool("FarIntHigh"))
        with Rung(Bool("FarIntShow", external=True)):
            copy(far[5], shown)
        with Rung(shown > 50):
            out(Bool("FarIntShownHigh"))
    return prog


@idiom
def index_sum_write_into_a_big_int_block() -> Any:
    """An index that adds two tags lists no targets past 1000 elements, bounds or not."""
    far = Block("SumInt", TagType.INT, 1, 2000)
    at = Int("SumIntAt", external=True, min=0, max=10)
    offset = Int("SumIntOffset", external=True, min=0, max=10)
    source = Int("SumIntSource", external=True, choices={0: "zero", 100: "hundred"})
    with Program() as prog:
        with Rung(Bool("SumIntWrite", external=True)):
            copy(source, far[at + offset])
        with Rung(far[5] > 50):
            out(Bool("SumIntHigh"))
    return prog


def _acknowledge_write(prefix: str, read_at: int) -> Any:
    """A write the tumbler makes: ``math(Cmd * 10 + 2999, At)`` under ``Cmd`` 1..100, then
    ``copy(source, far[At])``.  So the write reaches 3009..3999 and nothing else."""
    far = Block(f"{prefix}Int", TagType.INT, 1, 4500)
    cmd = Int(f"{prefix}Cmd", external=True)
    at = Int(f"{prefix}At")
    source = Int(f"{prefix}Source", external=True, choices={0: "zero", 100: "hundred"})
    shown = Int(f"{prefix}Shown")
    with Program() as prog:
        with Rung(Bool(f"{prefix}Set", external=True)):
            copy(3, far[read_at])
        with Rung(cmd >= 1, cmd <= 100):
            math(cmd * 10 + 2999, at)
            copy(source, far[at])
        with Rung(Bool(f"{prefix}Show", external=True)):
            copy(far[read_at], shown)
        with Rung(shown > 50):
            out(Bool(f"{prefix}ShownHigh"))
    return prog


@idiom
def guarded_pointer_write_reaches_the_element_read() -> Any:
    """``Cmd`` 1 makes the write store into element 3009, so its copy may hold 100."""
    return _acknowledge_write("AckIn", 3009)


@idiom
def guarded_pointer_write_leaves_the_element_read() -> Any:
    """The write never reaches element 5, so element 5 keeps its domain (0, 3)."""
    return _acknowledge_write("AckOut", 5)


# ---------------------------------------------------------------------------
# Reads an instruction's own state decides
# ---------------------------------------------------------------------------


@idiom
def drum_preset_written_below_the_drum() -> Any:
    """A time drum reads its step's preset tag on each scan the step times.

    The rungs below the drum write the preset every scan, so the drum reads the value of the
    scan before.
    """
    slow = Bool("SlowMix", external=True)
    preset = Int("MixTime")
    with Program() as prog:
        with Rung(Bool("MixRun", external=True)):
            time_drum(
                outputs=[Bool("MixA"), Bool("MixB")],
                presets=[preset, 50],
                unit="ms",
                pattern=[[1, 0], [0, 1]],
                current_step=Int("MixStep"),
                accumulator=Int("MixAcc"),
                completion_flag=Bool("MixDone"),
            ).reset(Bool("MixClear", external=True))
        with Rung(slow):
            copy(300, preset)
        with Rung(~slow):
            copy(100, preset)
    return prog


@idiom
def drum_preset_set_in_its_subroutine() -> Any:
    """A time drum in a subroutine reads a preset tag that its rungs write only now and then."""
    preset = Int("RinseTime")
    with Program() as prog:
        with Rung():
            call("Rinse")
        with subroutine("Rinse"):
            with Rung(Bool("RinseLong", external=True)):
                copy(300, preset)
            with Rung(Bool("RinseShort", external=True)):
                copy(100, preset)
            with Rung(Bool("RinseRun", external=True)):
                time_drum(
                    outputs=[Bool("RinseA"), Bool("RinseB")],
                    presets=[preset, 50],
                    unit="ms",
                    pattern=[[1, 0], [0, 1]],
                    current_step=Int("RinseStep"),
                    accumulator=Int("RinseAcc"),
                    completion_flag=Bool("RinseDone"),
                ).reset(Bool("RinseClear", external=True))
    return prog


@idiom
def drum_event_written_below_the_drum() -> Any:
    """An event drum reads only its current step's event, and a rung below the drum writes it."""
    ready = Bool("FeedReady")
    with Program() as prog:
        with Rung(Bool("FeedRun", external=True)):
            event_drum(
                outputs=[Bool("FeedA"), Bool("FeedB")],
                events=[Bool("FeedStart", external=True), ready],
                pattern=[[1, 0], [0, 1]],
                current_step=Int("FeedStep"),
                completion_flag=Bool("FeedDone"),
            ).reset(Bool("FeedClear", external=True))
        with Rung(Bool("FeedSensor", external=True)):
            out(ready)
    return prog


@idiom
def off_delay_preset_written_below_the_timer() -> Any:
    """An off-delay timer reads its preset tag only while it times, after its rung goes false."""
    long = Bool("LongPurge", external=True)
    preset = Int("PurgeTime")
    purge = Timer.clone("Purge")
    with Program() as prog:
        with Rung(Bool("Burner", external=True)):
            off_delay(purge, preset)
        with Rung(purge.Done):
            out(Bool("Blower"))
        with Rung(long):
            copy(300, preset)
        with Rung(~long):
            copy(100, preset)
    return prog


@idiom
def search_stops_before_the_element_written_below() -> Any:
    """A search reads no element past its first match, so whether it reads Bin2 turns on Bin1."""
    bins = Block("Bin", TagType.INT, 1, 2)
    wanted = Int("BinWant", external=True, min=0, max=3)
    full = Bool("BinFull", external=True)
    with Program() as prog:
        with Rung(Bool("BinLook", external=True)):
            search(bins.select(1, 2) <= wanted, result=Int("BinAt"), found=Bool("BinHit"))
        with Rung(Bool("BinStock", external=True)):
            copy(5, bins[1])
        with Rung(full):
            copy(1, bins[2])
        with Rung(~full):
            copy(3, bins[2])
    return prog


@idiom
def continuous_search_resumes_after_its_result() -> Any:
    """A continuous search reads its last result to start after it."""
    lanes = Block("Lane", TagType.INT, 1, 3)
    at = Int("LaneAt")
    with Program() as prog:
        with Rung():
            search(lanes.select(1, 3) == 5, result=at, found=Bool("LaneHit"), continuous=True)
        with Rung(at == 2):
            out(Bool("LaneTwo"))
        with Rung(Bool("LaneStock1", external=True)):
            copy(5, lanes[1])
        with Rung(Bool("LaneStock2", external=True)):
            copy(5, lanes[2])
        with Rung(Bool("LaneRestart", external=True)):
            copy(0, at)
    return prog


# ---------------------------------------------------------------------------
# Writes an instruction may skip
# ---------------------------------------------------------------------------


@idiom
def text_copy_skipped_on_a_bad_digit() -> Any:
    """A copy from text to a number writes nothing when the text is not a number."""
    key = Char("KeyChar", external=True, choices={"7": "seven", "x": "bad"})
    level = Int("KeyLevel")
    with Program() as prog:
        with Rung():
            copy(key, level, convert=to_value)
        with Rung(level > 5):
            latch(Bool("KeyHigh"))
        with Rung(Bool("KeyClear", external=True)):
            reset(Bool("KeyHigh"))
        with Rung(Bool("KeyHigh")):
            out(Bool("KeyLamp"))
    return prog
