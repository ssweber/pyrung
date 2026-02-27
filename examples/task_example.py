"""Task sequencer example using a Click-style subroutine pattern.

This demonstrates a common task pattern used in Click PLC programs:
  1. A `Task` UDT stores call/pause flags, current step, advance flag, and timers
  2. A subroutine owns the step logic and reset/stop boilerplate
  3. The main program latches `Task.Active` from `Task.Call` and calls the
     subroutine while `Task.Active == 1`
"""

import os

from pyrung import (
    Bool,
    Int,
    PLCRunner,
    Rung,
    TimeMode,
    Ts,
    calc,
    call,
    copy,
    on_delay,
    out,
    program,
    reset,
    return_early,
    subroutine,
    udt,
)


@udt()
class Task:
    Call: Int
    Pause: Int
    Active: Int
    Step: Int
    Advance: Int
    Elapsed: Int
    StepTime: Int


# Rename Step1_Active to describe your actual step (e.g., FillTank, HomeAxis).
Step1_Active = Bool("Step1_Active")
TimerDone = Bool("TimerDone")
Valve1 = Bool("Valve1")


def task_logic() -> None:
    # 1) Global and step timers run while task is active.
    #    Add other units to suit (e.g., StepTime_Min with unit=Tm).
    with Rung(Task.Active == 1):
        on_delay(TimerDone, Task.Elapsed, preset=9999, unit=Ts)
        on_delay(TimerDone, Task.StepTime, preset=9999, unit=Ts)

    # 2) Step logic (odd numbered active steps).
    with Rung(Task.Step == 1):
        out(Step1_Active)

    with Rung(Step1_Active):
        out(Valve1)

    with Rung(Step1_Active, Task.StepTime >= 5):
        copy(1, Task.Advance)

    with Rung(Task.Step == 3):
        pass

    # 3) Pause and stop/reset behavior.
    with Rung(Task.Pause == 1):
        reset(Valve1)
        return_early()

    with Rung(Task.Call == 0):
        copy(0, Task.Active)
        copy(0, Task.Step)
        copy(0, Task.Advance)
        copy(0, Task.Elapsed)
        copy(0, Task.StepTime)

        reset(Valve1)
        reset(Step1_Active)
        return_early()

    # 4) Boilerplate: odd steps are active, even steps auto-advance
    #    and reset the step timer.
    with Rung(Task.Step % 2 == 0):
        calc(Task.Step + 1, Task.Step)

    with Rung(Task.Advance == 1):
        calc(Task.Step + 1, Task.Step)
        copy(0, Task.Advance)
        copy(0, Task.StepTime)


@program
def logic():
    with Rung(Task.Call == 1):
        copy(1, Task.Active)

    with Rung(Task.Active == 1):
        call("Task_Subroutine")

    with subroutine("Task_Subroutine"):
        task_logic()


runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)

with runner.active():
    Task.Call.value = 1


def print_row(current_time: float) -> None:
    with runner.active():
        step = Task.Step.value
        timer = Task.StepTime.value
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
