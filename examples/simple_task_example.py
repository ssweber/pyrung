"""Simple task sequencer using a self-guarding subroutine pattern.

Every rung in the subroutine is guarded by ``Task.Active == 1``, so
outputs auto-reset when Active drops.  The reset rung explicitly
clears all task state (Active, Step, Advance, StepTime) when
``Task.Call`` is cleared.
"""

import os

from pyrung import (
    PLC,
    Bool,
    Int,
    Rung,
    Timer,
    branch,
    calc,
    call,
    copy,
    on_delay,
    out,
    program,
    subroutine,
    udt,
)


@udt()
class Task:
    Call: Int
    Active: Int
    Step: Int
    Advance: Int


StepTimer = Timer.clone("StepTimer")
Valve1 = Bool("Valve1")


def task_logic() -> None:
    # Step 0 means "not started" — enter step 1 when Active.
    with Rung(Task.Active == 1, Task.Step == 0):
        copy(1, Task.Step)

    # Everything under this rung auto-resets when Active drops to 0.
    with Rung(Task.Active == 1):
        on_delay(StepTimer, preset=9999, unit="Ts")

        # Step 1: open valve for 5 seconds then advance.
        with branch(Task.Step == 1):
            out(Valve1)

        with branch(Task.Step == 1, StepTimer.Acc >= 5):
            copy(1, Task.Advance)

    # Reset all task state when call is cleared.
    with Rung(Task.Call == 0):
        copy(0, Task.Active)
        copy(0, Task.Step)
        copy(0, Task.Advance)
        copy(0, StepTimer.Acc)

    # Consume advance: increment step, reset step timer.
    with Rung(Task.Advance == 1):
        calc(Task.Step + 1, Task.Step)
        copy(0, Task.Advance)
        copy(0, StepTimer.Acc)


@program
def logic():
    with Rung(Task.Call == 1):
        copy(1, Task.Active)

    # Always called — rungs self-guard with Task.Active.
    with Rung():
        call("Task_Subroutine")

    with subroutine("Task_Subroutine"):
        task_logic()


runner = PLC(logic, dt=0.010)

with runner:
    Task.Call.value = 1


def print_row(current_time: float) -> None:
    with runner:
        step = Task.Step.value
        timer = StepTimer.Acc.value
        valve = Valve1.value
    print(f"{current_time:<10.2f} | {step:<5} | {timer:<10} | {valve}")


if os.getenv("PYRUNG_DAP_ACTIVE") != "1":
    print(f"{'TIME':<10} | {'STEP':<5} | {'TIMER (s)':<10} | {'VALVE'}")
    print("-" * 50)

    runner.step()
    print_row(0.0)

    for second in range(1, 7):
        runner.run_for(1.0)
        print_row(float(second))
