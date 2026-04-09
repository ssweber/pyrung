from pyrung.core import (
    Program, subroutine, Rung, out, on_delay, copy, calc, reset, return_early,
    call, TimeUnit, TagType, PackedStruct, Field, Bool, PLCRunner, TimeMode, Timer
)
from pyrung.click import c, ds, t, td

# ==============================================================================
# DATA STRUCTURES & TAGS
# ==============================================================================
TaskDs = PackedStruct(
    "Task",
    TagType.INT,
    count=1,
    # All Fields here are INTs because the Struct is INT
    Call=Field(default=0),
    Pause=Field(default=0),
    Active=Field(default=0),
    CurStep=Field(default=0),
    Trans=Field(default=0),
    
    # Global Task Timer Accumulators
    Tmr_Th=Field(default=0),
    Tmr_Tm=Field(default=0),
    Tmr_Ts=Field(default=0),
    Tmr_Tms=Field(default=0),
    
    # Current Step Timer Accumulators
    Step_Th=Field(default=0),
    Step_Tm=Field(default=0),
    Step_Ts=Field(default=0),
    Step_Tms=Field(default=0),
)
task = TaskDs[1]

# Internal Flags (These are Bools, so they don't need == 1)
Step1_Event = Bool("Step1_Event")
Valve1 = Bool("Valve1")

# Timers — one per unit for global and step timing
GlobalTmrTh = Timer.named(1, "GlobalTmrTh")
GlobalTmrTm = Timer.named(2, "GlobalTmrTm")
GlobalTmrTs = Timer.named(3, "GlobalTmrTs")
GlobalTmrTms = Timer.named(4, "GlobalTmrTms")
StepTmrTh = Timer.named(5, "StepTmrTh")
StepTmrTm = Timer.named(6, "StepTmrTm")
StepTmrTs = Timer.named(7, "StepTmrTs")
StepTmrTms = Timer.named(8, "StepTmrTms")

# ==============================================================================
# SUBROUTINE LOGIC
# ==============================================================================
def task_logic():
    # 1. GLOBAL & STEP TIMERS
    with Rung(task.Active == 1):
        on_delay(GlobalTmrTh, preset=9999, unit=TimeUnit.Th)
        on_delay(GlobalTmrTm, preset=9999, unit=TimeUnit.Tm)
        on_delay(GlobalTmrTs, preset=9999, unit=TimeUnit.Ts)
        on_delay(GlobalTmrTms, preset=9999, unit=TimeUnit.Tms)

        on_delay(StepTmrTh, preset=9999, unit=TimeUnit.Th)
        on_delay(StepTmrTm, preset=9999, unit=TimeUnit.Tm)
        on_delay(StepTmrTs, preset=9999, unit=TimeUnit.Ts)
        on_delay(StepTmrTms, preset=9999, unit=TimeUnit.Tms)

    # 2. STEP LOGIC (Odd Numbers Only)
    
    # --- Step 1 ---
    with Rung(task.CurStep == 1):
        out(Step1_Event)
        
    with Rung(Step1_Event):
        out(Valve1)

    with Rung(Step1_Event, StepTmrTs.acc >= 5):
        copy(1, task.Trans)

    # --- Step 3 ---
    with Rung(task.CurStep == 3):
        pass

    # 3. STOP & PAUSE RESETS
    with Rung(task.Pause == 1):
        reset(Valve1)
        return_early()
        
    with Rung(task.Call == 0):
        copy(0, task.Active)
        copy(0, task.CurStep)
        copy(0, task.Trans)
        
        # Clear timers
        copy(0, GlobalTmrTh.acc); copy(0, StepTmrTh.acc)
        copy(0, GlobalTmrTm.acc); copy(0, StepTmrTm.acc)
        copy(0, GlobalTmrTs.acc); copy(0, StepTmrTs.acc)
        copy(0, GlobalTmrTms.acc); copy(0, StepTmrTms.acc)
        
        reset(Valve1)
        reset(Step1_Event)
        return_early()

    with Rung(task.Call == 1):
        out(task.Active)

    # 4. BOTTOM BOILERPLATE
    with Rung(task.CurStep % 2 == 0):
         calc(task.CurStep + 1, task.CurStep)

    with Rung(task.Trans == 1):
        calc(task.CurStep + 1, task.CurStep)
        copy(0, task.Trans)

        copy(0, StepTmrTh.acc)
        copy(0, StepTmrTm.acc)
        copy(0, StepTmrTs.acc)
        copy(0, StepTmrTms.acc)

# ==============================================================================
# MAIN PROGRAM
# ==============================================================================
def main() -> Program:
    with Program() as logic:
        # FIXED: Added '== 1' because task.Call is an INT
        with Rung(task.Call == 1):
            call("Task_Subroutine")

        with subroutine("Task_Subroutine"):
            task_logic()

    return logic

# ==============================================================================
# SYNCHRONOUS SIMULATION
# ==============================================================================
def run_simulation_formatted():
    # 1. Setup Logic and Runner
    logic = main()
    runner = PLCRunner(logic=logic)
    
    # Configure 10ms fixed scan time [1]
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.01)

    # 2. Set Initial Condition (Turn the Task ON)
    runner.patch(tags={task.Call: 1})

    # 3. Print Header
    print(f"{'TIME':<10} | {'STEP':<5} | {'TIMER (s)':<10} | {'VALVE'}")
    print("-" * 50)

    # 4. Define a helper to print the current row
    def print_row(current_time):
        state = runner.current_state
        
        # Extract values using the tag names from the state dictionary
        step = state.tags.get('Task1_CurStep', 0)
        timer = state.tags.get('StepTmrTs_acc', 0)
        valve = state.tags.get('Valve1', False)
        
        # Format: Time with 2 decimals, Step/Timer left aligned
        print(f"{current_time:<10.2f} | {step:<5} | {timer:<10} | {valve}")

    # 5. Print Initial State (Time 0.00)
    print_row(0.0)

    # 6. Loop 6 times (Advancing 1.0 second each time)
    for i in range(1, 7):
        _ = runner.run_for(1.0)
        print_row(float(i))

if __name__ == "__main__":
    run_simulation_formatted()
