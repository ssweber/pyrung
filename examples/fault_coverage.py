"""Fault coverage: a timing check for every physical device.

The autoharness lists every device coupling.  For each one, `force()` and
`run_for()` break the feedback and check that the alarm trips in time.  This
catches a fault timer that is missing or too slow.
"""

from pyrung import (
    PLC,
    Block,
    Bool,
    Harness,
    Int,
    Physical,
    rung,
    Timer,
    math,
    copy,
    fall,
    latch,
    on_delay,
    program,
    rise,
    TagType,
)

# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------
Cmd = Bool(public=True)
Fb = Bool(
    external=True,
    physical=Physical("MotorFb", on_delay="500ms", off_delay="200ms"),
    link="Cmd",
)

PumpCmd = Bool(public=True)
PumpFb = Bool(
    external=True,
    physical=Physical("PumpFb", on_delay="1s", off_delay="500ms"),
    link="PumpCmd",
)

FaultTimer = Timer.clone("FaultTimer")
PumpFaultTimer = Timer.clone("PumpFaultTimer")

AlarmBits = Block("AlarmBit", TagType.BOOL, 1, 2)
AlarmInts = Block("AlarmInt", TagType.INT, 1, 2)
AlarmExtent = Int(public=True)

# ---------------------------------------------------------------------------
# Logic — two devices with independent fault detection
# ---------------------------------------------------------------------------


@program
def logic():
    # Fault detection: Cmd on but Fb didn't follow within 3 s
    with rung(Cmd, ~Fb):
        on_delay(FaultTimer, 3000)
    with rung(FaultTimer.Done):
        latch(AlarmBits[1])

    # Pump fault detection: PumpCmd on but PumpFb didn't follow within 5 s
    with rung(PumpCmd, ~PumpFb):
        on_delay(PumpFaultTimer, 5000)
    with rung(PumpFaultTimer.Done):
        latch(AlarmBits[2])

    # Mirror alarm bits into Int block for summation
    with rung(rise(AlarmBits[1])):
        copy(1, AlarmInts[1])
    with rung(fall(AlarmBits[1])):
        copy(0, AlarmInts[1])
    with rung(rise(AlarmBits[2])):
        copy(1, AlarmInts[2])
    with rung(fall(AlarmBits[2])):
        copy(0, AlarmInts[2])

    # AlarmExtent: nonzero when any alarm is active
    with rung():
        math(AlarmInts.select(1, 2).sum(), AlarmExtent)


# ---------------------------------------------------------------------------
# Timing coverage with force
# ---------------------------------------------------------------------------
print("=== Timing coverage ===")
tripped_couplings: list[str] = []

plc = PLC(logic, dt=0.001)
harness = Harness(plc)
harness.install()

for coupling in harness.couplings():
    plc2 = PLC(logic, dt=0.001)
    h2 = Harness(plc2)
    h2.install()

    # Drive the enable on, let harness deliver feedback
    plc2.force(coupling.en_name, True)
    plc2.run_for(1.5)

    # Break the feedback — fault timer should trip
    plc2.force(coupling.fb_name, False)
    plc2.run_for(6.0)

    with plc2:
        tripped = AlarmExtent.value != 0
    if tripped:
        print(f"  PASS  {coupling.fb_name}: alarm tripped within time budget")
        tripped_couplings.append(coupling.fb_name)
    else:
        print(f"  FAIL  {coupling.fb_name}: alarm did NOT trip in time")
