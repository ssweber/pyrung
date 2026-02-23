from pyrung.core import (
    Program, subroutine, Rung, out, on_delay, copy, calc, reset, return_, 
    call, TimeUnit, TagType, PackedStruct, Field, Bool, PLCRunner, TimeMode
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
Tmr_Dn = Bool("Tmr_Dn")
Valve1 = Bool("Valve1")

# ==============================================================================
# SUBROUTINE LOGIC
# ==============================================================================
def task_logic():
    # 1. GLOBAL & STEP TIMERS
    with Rung(task.Active == 1):
        on_delay(Tmr_Dn, task.Tmr_Th, 9999, TimeUnit.Th)
        on_delay(Tmr_Dn, task.Tmr_Tm, 9999, TimeUnit.Tm)
        on_delay(Tmr_Dn, task.Tmr_Ts, 9999, TimeUnit.Ts)
        on_delay(Tmr_Dn, task.Tmr_Tms, 9999, TimeUnit.Tms)

        on_delay(Tmr_Dn, task.Step_Th, 9999, TimeUnit.Th)
        on_delay(Tmr_Dn, task.Step_Tm, 9999, TimeUnit.Tm)
        on_delay(Tmr_Dn, task.Step_Ts, 9999, TimeUnit.Ts)
        on_delay(Tmr_Dn, task.Step_Tms, 9999, TimeUnit.Tms)

    # 2. STEP LOGIC (Odd Numbers Only)
    
    # --- Step 1 ---
    with Rung(task.CurStep == 1):
        out(Step1_Event)
        
    with Rung(Step1_Event):
        out(Valve1)

    with Rung(Step1_Event, task.Step_Ts >= 5):
        copy(1, task.Trans)

    # --- Step 3 ---
    with Rung(task.CurStep == 3):
        pass

    # 3. STOP & PAUSE RESETS
    with Rung(task.Pause == 1):
        reset(Valve1)
        return_()
        
    with Rung(task.Call == 0):
        copy(0, task.Active)
        copy(0, task.CurStep)
        copy(0, task.Trans)
        
        # Clear timers
        copy(0, task.Tmr_Th); copy(0, task.Step_Th)
        copy(0, task.Tmr_Tm); copy(0, task.Step_Tm)
        copy(0, task.Tmr_Ts); copy(0, task.Step_Ts)
        copy(0, task.Tmr_Tms); copy(0, task.Step_Tms)
        
        reset(Valve1)
        reset(Step1_Event)
        return_()

    with Rung(task.Call == 1):
        out(task.Active)

    # 4. BOTTOM BOILERPLATE
    with Rung(task.CurStep % 2 == 0):
         calc(task.CurStep + 1, task.CurStep)

    with Rung(task.Trans == 1):
        calc(task.CurStep + 1, task.CurStep)
        copy(0, task.Trans)
        
        copy(0, task.Step_Th)
        copy(0, task.Step_Tm)
        copy(0, task.Step_Ts)
        copy(0, task.Step_Tms)

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
        timer = state.tags.get('Task1_Step_Ts', 0)
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
