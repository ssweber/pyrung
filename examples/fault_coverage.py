"""Fault coverage: structural + timing verification for every physical device.

Two-pass workflow using the same coupling list from the autoharness:

1. **Structural** — `prove()` checks that every feedback has a path to an
   alarm in all reachable states.  Counterexample = structural gap.
2. **Timing** — `force()` + `run_for()` checks that the alarm trips within
   the required time.  This catches timers that exist but are too slow.

Run `prove()` first — there's no point testing timing on a coupling that
never reaches an alarm.
"""

from pyrung import (
    Block,
    Bool,
    Harness,
    Int,
    Or,
    PLC,
    Physical,
    Rung,
    Timer,
    calc,
    copy,
    fall,
    latch,
    on_delay,
    program,
    rise,
)
from pyrung.core.analysis import Counterexample, Intractable, Proven, prove
from pyrung.core.tag import TagType

# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------
Cmd = Bool("Cmd", public=True)
Fb = Bool(
    "Fb",
    external=True,
    physical=Physical("MotorFb", on_delay="500ms", off_delay="200ms"),
    link="Cmd",
)

PumpCmd = Bool("PumpCmd", public=True)
PumpFb = Bool(
    "PumpFb",
    external=True,
    physical=Physical("PumpFb", on_delay="1s", off_delay="500ms"),
    link="PumpCmd",
)

FaultTimer = Timer.clone("FaultTimer")
PumpFaultTimer = Timer.clone("PumpFaultTimer")

AlarmBits = Block("AlarmBit", TagType.BOOL, 1, 2)
AlarmInts = Block("AlarmInt", TagType.INT, 1, 2)
AlarmExtent = Int("AlarmExtent", public=True)

# ---------------------------------------------------------------------------
# Logic — two devices with independent fault detection
# ---------------------------------------------------------------------------


@program
def logic():
    # Fault detection: Cmd on but Fb didn't follow within 3 s
    with Rung(Cmd, ~Fb):
        on_delay(FaultTimer, 3000)
    with Rung(FaultTimer.Done):
        latch(AlarmBits[1])

    # Pump fault detection: PumpCmd on but PumpFb didn't follow within 5 s
    with Rung(PumpCmd, ~PumpFb):
        on_delay(PumpFaultTimer, 5000)
    with Rung(PumpFaultTimer.Done):
        latch(AlarmBits[2])

    # Mirror alarm bits into Int block for summation
    with Rung(rise(AlarmBits[1])):
        copy(1, AlarmInts[1])
    with Rung(fall(AlarmBits[1])):
        copy(0, AlarmInts[1])
    with Rung(rise(AlarmBits[2])):
        copy(1, AlarmInts[2])
    with Rung(fall(AlarmBits[2])):
        copy(0, AlarmInts[2])

    # AlarmExtent: nonzero when any alarm is active
    with Rung():
        calc(AlarmInts.select(1, 2).sum(), AlarmExtent)


# ---------------------------------------------------------------------------
# Pass 1 — structural coverage with prove()
# ---------------------------------------------------------------------------
print("=== Structural coverage ===")
structural_pass: list[str] = []

plc = PLC(logic, dt=0.001)
harness = Harness(plc)
harness.install()

couplings = list(harness.couplings())
conditions = [
    Or(~plc.tags[c.en_name], plc.tags[c.fb_name], AlarmExtent != 0)
    for c in couplings
]
results = prove(logic, conditions)

for coupling, result in zip(couplings, results):
    if isinstance(result, Proven):
        print(f"  PASS  {coupling.fb_name}: alarm path exists")
        structural_pass.append(coupling.fb_name)
    elif isinstance(result, Counterexample):
        print(f"  FAIL  {coupling.fb_name}: no alarm path — structural gap")
    elif isinstance(result, Intractable):
        print(f"  SKIP  {coupling.fb_name}: intractable")

# ---------------------------------------------------------------------------
# Pass 2 — timing coverage with force
# ---------------------------------------------------------------------------
print("\n=== Timing coverage ===")

for coupling in harness.couplings():
    if coupling.fb_name not in structural_pass:
        print(f"  SKIP  {coupling.fb_name}: no structural path, skipping timing")
        continue

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
    else:
        print(f"  FAIL  {coupling.fb_name}: alarm did NOT trip in time")
