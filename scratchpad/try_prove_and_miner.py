"""Try out prove() and session miner on click_conveyor_annotated."""
from __future__ import annotations

import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import sys
from pathlib import Path
from time import perf_counter

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pyrung import (
    And,
    Bool,
    Counter,
    Field,
    Harness,
    Int,
    Or,
    Physical,
    PLC,
    Rung,
    Timer,
    branch,
    comment,
    copy,
    count_up,
    latch,
    named_array,
    on_delay,
    out,
    program,
    reset,
    rise,
    udt,
)
from pyrung.click import TagMap, c, ct, ctd, ds, t, td, x, y
from typing import Any, cast

# ── Re-declare tags (same as click_conveyor_annotated) ──

@udt()
class ConveyorIO:
    Motor: Bool = Field(public=True)
    MotorFb: Bool = Field(
        external=True,
        physical=Physical("MotorFb", on_delay="500ms", off_delay="200ms"),
        link="Motor",
    )
    StatusLight: Bool = Field(public=True)
    Diverter: Bool = Field(public=True)
    DiverterFb: Bool = Field(
        external=True,
        physical=Physical("DiverterFb", on_delay="100ms", off_delay="100ms"),
        link="Diverter",
    )


ConveyorIO = cast(Any, ConveyorIO)
conv = ConveyorIO.clone("Conv")


@udt()
class SizeAnalog:
    Reading: Int = Field(min=0, max=4095, uom="counts")
    Threshold: Int = Field(default=100, public=True, min=0, max=4095, uom="counts")


SizeAnalog = cast(Any, SizeAnalog)
Size = SizeAnalog.clone("Size")


StartBtn = Bool("StartBtn", public=True)
StopBtn = Bool("StopBtn", public=True)
EstopOK = Bool("EstopOK", public=True, external=True)
Auto = Bool("Auto", public=True)
Manual = Bool("Manual", public=True)
EntrySensor = Bool("EntrySensor")
DiverterBtn = Bool("DiverterBtn", public=True)
BinASensor = Bool("BinASensor")
BinBSensor = Bool("BinBSensor")

Running = Bool("Running", public=True)
IsLarge = Bool("IsLarge")
CountReset = Bool("CountReset", public=True)


@named_array(Int, stride=4, readonly=True)
class SortState:
    IDLE = 0
    DETECTING = 1
    SORTING = 2
    RESETTING = 3


SortState = cast(Any, SortState)
State = Int("State", choices=SortState, public=True, default=SortState.IDLE)

DetTimer = Timer.clone("DetTimer")
HoldTimer = Timer.clone("HoldTimer")

BinACounter = Counter.clone("BinACounter", public=True)
BinBCounter = Counter.clone("BinBCounter", public=True)


@program
def logic():
    comment("Start/stop — NC stop button resets when pressed or wire broken")
    with Rung(StartBtn, Or(Auto, Manual)):
        latch(Running)
    with Rung(~StopBtn):
        reset(Running)
    with Rung(~EstopOK):
        reset(Running)

    comment("Motor output — EstopOK gates all outputs")
    with Rung(EstopOK):
        with branch(Running):
            out(conv.Motor)
        with branch(Running):
            out(conv.StatusLight)

    comment("Sort state machine — IDLE to DETECTING: box arrives")
    with Rung(State == SortState.IDLE, rise(EntrySensor)):
        copy(SortState.DETECTING, State)

    comment("DETECTING: read size for 0.5 seconds")
    with Rung(State == SortState.DETECTING):
        on_delay(DetTimer, 500)
    with Rung(State == SortState.DETECTING, Size.Reading > Size.Threshold):
        latch(IsLarge)
    with Rung(DetTimer.Done):
        copy(SortState.SORTING, State)

    comment("SORTING: hold diverter for 2 seconds")
    with Rung(State == SortState.SORTING):
        on_delay(HoldTimer, 2000)
    with Rung(HoldTimer.Done):
        copy(SortState.RESETTING, State)

    comment("RESETTING: clean up and return to idle")
    with Rung(State == SortState.RESETTING):
        reset(IsLarge)
        copy(SortState.IDLE, State)

    comment("Diverter output — auto sort OR manual button, gated by EstopOK")
    with Rung(
        EstopOK,
        Or(
            And(State == SortState.SORTING, IsLarge, Auto),
            And(Manual, DiverterBtn),
        ),
    ):
        out(conv.Diverter)

    comment("Bin counters")
    with Rung(rise(BinASensor)):
        count_up(BinACounter, preset=9999).reset(CountReset)
    with Rung(rise(BinBSensor)):
        count_up(BinBCounter, preset=9999).reset(CountReset)


# =====================================================================
# Part 1: Exhaustive verification — prove()
# =====================================================================
def main() -> None:
    from pyrung.core.analysis.prove import Intractable, program_hash, prove, reachable_states
    from pyrung.dap.capture import CaptureEntry
    from pyrung.dap.miner import mine_candidates

    print("=" * 60)
    print("EXHAUSTIVE VERIFICATION: prove()")
    print("=" * 60)

    properties = [
        ("Property 1: Motor → EstopOK (motor cannot run without estop)", Or(~conv.Motor, EstopOK)),
        (
            "Property 2: Diverter → EstopOK (diverter cannot activate without estop)",
            Or(~conv.Diverter, EstopOK),
        ),
        (
            "Property 3: Motor ↔ StatusLight (light tracks motor)",
            Or(And(conv.Motor, conv.StatusLight), And(~conv.Motor, ~conv.StatusLight)),
        ),
        ("Property 4: ~StopBtn → ~Running (stop kills running)", Or(StopBtn, ~Running)),
        ("Property 5: ~EstopOK → ~Running (estop kills running)", Or(EstopOK, ~Running)),
        (
            "Property 6: State ∈ {IDLE, DETECTING, SORTING, RESETTING}",
            Or(
                State == SortState.IDLE,
                State == SortState.DETECTING,
                State == SortState.SORTING,
                State == SortState.RESETTING,
            ),
        ),
    ]

    prove_started = perf_counter()
    results = prove(logic, [prop for _label, prop in properties])
    prove_elapsed = perf_counter() - prove_started
    for i, ((label, _prop), result) in enumerate(zip(properties, results, strict=True), start=1):
        print(f"\n--- {label}")
        print(f"    Result: {result}")
    print(f"\nprove() batch elapsed: {prove_elapsed:.3f}s")

    print("\n" + "=" * 60)
    print("REACHABLE STATE SPACE")
    print("=" * 60)

    reachable_started = perf_counter()
    states = reachable_states(logic)
    reachable_elapsed = perf_counter() - reachable_started
    if isinstance(states, Intractable):
        print(f"\nIntractable: {states}")
    else:
        print(f"\nTotal reachable states (public projection): {len(states)}")
        for i, s in enumerate(sorted(states, key=lambda s: sorted(s))[:10]):
            print(f"  State {i}: {dict(sorted(s))}")
        if len(states) > 10:
            print(f"  ... and {len(states) - 10} more")
    print(f"reachable_states() elapsed: {reachable_elapsed:.3f}s")

    print("\n" + "=" * 60)
    print("PROGRAM HASH & LOCK FILE")
    print("=" * 60)

    h = program_hash(logic)
    print(f"\nProgram hash: {h}")

    print("\n" + "=" * 60)
    print("SESSION RECORDING & MINER")
    print("=" * 60)

    runner = PLC(logic, dt=0.010)
    harness = Harness(runner)
    harness.install()

    with runner:
        StopBtn.value = True
        EstopOK.value = True
        Auto.value = True
        Size.Threshold.value = 100

    runner.step()

    entries: list[CaptureEntry] = []
    start_scan = runner.current_state.scan_id

    with runner:
        StartBtn.value = True
    runner.step()
    entries.append(CaptureEntry("patch StartBtn true", runner.current_state.scan_id, 0.0))

    with runner:
        StartBtn.value = False
    runner.step()
    entries.append(CaptureEntry("patch StartBtn false", runner.current_state.scan_id, 0.0))

    for i in range(60):
        runner.step()
        entries.append(CaptureEntry(f"step {i}", runner.current_state.scan_id, 0.0))

    runner.force(EntrySensor, True)
    runner.force(Size.Reading, 150)
    runner.step()
    entries.append(CaptureEntry("force EntrySensor + Size.Reading", runner.current_state.scan_id, 0.0))

    for i in range(200):
        runner.step()
        entries.append(CaptureEntry(f"detect step {i}", runner.current_state.scan_id, 0.0))

    runner.unforce(EntrySensor)
    runner.unforce(Size.Reading)

    for i in range(300):
        runner.step()
        entries.append(CaptureEntry(f"sort step {i}", runner.current_state.scan_id, 0.0))

    print("\nMining candidates from recorded session...")
    candidates = mine_candidates("conveyor_test", entries, runner, start_scan_id=start_scan)

    print(f"\nFound {len(candidates)} candidate invariants:\n")
    for c_item in candidates:
        print(f"  [{c_item.id}] ({c_item.kind})")
        print(f"       {c_item.description}")
        print(
            f"       observed={c_item.observation_count}  violations={c_item.violation_count}  delay={c_item.observed_delay_scans} scans"
        )
        if c_item.physics_floor_scans is not None:
            print(f"       physics floor: {c_item.physics_floor_scans} scans")
        print()

    print("Done!")


if __name__ == "__main__":
    main()
