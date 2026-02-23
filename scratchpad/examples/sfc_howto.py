"""SFC template ported line-by-line to pyrung + pyclickplc soft PLC runtime."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress

from pyclickplc.server import ClickServer

from pyrung.click import ClickDataProvider, TagMap, c, ds, t, td
from pyrung.core import (
    Bool,
    Int,
    PLCRunner,
    Program,
    Rung,
    TimeMode,
    TimeUnit,
    any_of,
    call,
    copy,
    latch,
    calc,
    on_delay,
    reset,
    return_,
    subroutine,
    named_array,
)

"""
SFC (Sequential Function Chart) Implementation Template for Click PLC

IMPORTANT: STEP NUMBERING CONVENTION
- All STEP actions must use ODD numbers (1, 3, 5, 7, etc.)
- All TRANSITION logic occurs on the same odd number as its preceding step
- Even numbers are used as "reset" steps that are automatically skipped

WHY ODD STEPS?
The ODD step convention provides two critical benefits:

1. TROUBLESHOOTING SAFETY: If a sequence gets stuck at step N and you manually
   decrement CurStep by 1 (setting it to N-1), you'll enter an EVEN step number.
   Since even steps contain no logic, this provides a safe "neutral" state that
   clears timers and one-shots before proceeding to the previous step.

   Without this convention, manually decrementing a step could cause the system
   to immediately execute the previous step's actions, potentially creating
   unsafe conditions.


HOW TO USE THIS TEMPLATE:
1. Rename all instances of 'SubName' to your actual subroutine name
2. Define your step logic in SECTION 2 only
3. For each step (using ODD numbers only):
   - Create a step action rung with condition: if ds.yourName_CurStep == stepNumber
   - Create a transition rung with the same condition that sets ds.yourName_Trans to 1
4. DO NOT MODIFY the boilerplate code in SECTIONS 1 and 3

STANDARD PATTERN FOR STEP IMPLEMENTATION:
# Step X: [Description] (X must be ODD: 1, 3, 5, etc.)
if ds.yourName_CurStep == X:
    # Your step logic here
    pass

# Transition X→X+2: [Description] (Transitions advance to next ODD number)
if ds.yourName_CurStep == X:
    if [your transition condition]:
        copy(1, ds.yourName_Trans)  # This will advance to step X+2
"""

#############################################################################
# SFC IMPLEMENTATION STANDARD VARIABLE NAMING CONVENTIONS
#############################################################################
# The following variables should be defined for each SFC subroutine:
# SubName_xCall - ds     # External trigger to call the SFC
# SubName_xInit - ds     # External trigger to initialize the SFC
# SubName_xReset - ds    # External trigger to reset the SFC
# SubName_xPause - ds    # External trigger to pause the SFC
# SubName_Error - ds     # Flag indicating an error has occurred
# SubName_ErrorStep - ds # Step where the error occurred
# SubName_EnableLimit - ds # Enable time limit checking
# SubName_Limit_Ts - ds  # Time limit for steps in Ts (second) units
# SubName_ResetTmr - ds  # Reset timer flag
# SubName_Trans - ds     # Transition flag
# SubName_CurStep - ds   # Current step number
# SubName_StoredStep - ds # Previous step number
# SubName__x - ds       # Internal execution flag
# SubName__init - ds    # Internal initialization flag
# SubName__ValStepIsOdd - ds # Flag indicating if current step is odd

@named_array(Int, count=1, stride=17)
class SubNameDs:
    SubName_xCall = 0
    SubName_xInit = 0
    SubName_xReset = 0
    SubName_xPause = 0
    SubName_init = 0
    SubName_Error = 0
    SubName_ErrorStep = 0
    SubName_EnableLimit = 0
    SubName_Limit_Ts = 0
    SubName_ResetTmr = 0
    SubName_Trans = 0
    SubName_CurStep = 0
    SubName_StoredStep = 0
    SubName__x = 0
    SubName__init = 0
    SubName__ValStepIsOdd = 0
    one_time_operation_complete = 0

sub = SubNameDs[1]

SubName_x = Bool("SubName_x")
SubName_tmr = Bool("SubName_tmr")
SubName_CurStep_tmr = Bool("SubName_CurStep_tmr")
step3_timer = Bool("step3_timer")
SubName_t = Int("SubName_t")
SubName_CurStep_t = Int("SubName_CurStep_t")
step3_timer_acc = Int("step3_timer_acc")


def main() -> Program:
    """Main wrapper plus subroutine definition."""
    with Program() as logic:
        # Section 1 boilerplate in main: set internal execute flag only when call is active
        # and pause is not active.
        with Rung(sub.SubName_xCall == 1, sub.SubName_xPause != 1):
            copy(1, sub.SubName__x)

        # Call subroutine when execute bit is set.
        with Rung(sub.SubName__x == 1):
            call("SubName")

        with subroutine("SubName"):
            subRoutine1()

    return logic


def subRoutine1() -> None:
    """
    SFC Implementation Template

    This subroutine implements a Sequential Function Chart (SFC) pattern
    for the Click PLC using the pyrung DSL.

    IMPORTANT: All steps MUST use ODD numbers (1, 3, 5, 7, etc.)

    The implementation consists of four main sections:
    1. SFC INITIALIZATION BOILERPLATE
    2. STEP IMPLEMENTATION SECTION
    3. PAUSE AND SHUTDOWN IMPLEMENTATION SECTION
    4. SFC EXECUTION CONTROL BOILERPLATE

    When implementing a new SFC, only modify the STEP and Pause/Shutdown sections.
    """

    # ==============================================================================
    # SECTION 1: SFC INITIALIZATION BOILERPLATE - DO NOT MODIFY
    # ==============================================================================

    # Set active flag for current step.
    with Rung():
        # CODESYS: step.x. Variable of type Bool which is true while Step is Active.
        latch(SubName_x)

    # Shared Resets for Init and Pause.
    with Rung(any_of(sub.SubName_xInit == 1, sub.SubName_xReset == 1)):
        copy(0, sub.SubName_init)  # Reset init flag
        copy(0, SubName_t)  # Reset timer value
        copy(0, SubName_CurStep_t)  # Reset timer value
        # Set initial step to 1 (first odd number)
        copy(1, sub.SubName_CurStep)
        copy(1, sub.SubName_StoredStep)

    # Built-in timer for step timeout detection.
    with Rung(sub.SubName_xCall == 1):
        on_delay(
            SubName_tmr,
            SubName_t,
            setpoint=sub.SubName_Limit_Ts,
            time_unit=TimeUnit.Ts,
        ).reset(sub.SubName_ResetTmr == 1)

    # Reset the timer reset flag after it's been processed.
    with Rung(sub.SubName_ResetTmr == 1):
        copy(0, sub.SubName_ResetTmr)

    # Error detection - If step exceeds time limit.
    with Rung(SubName_t >= sub.SubName_Limit_Ts, sub.SubName_EnableLimit == 1):
        copy(1, sub.SubName_Error)  # Set Error flag
        copy(sub.SubName_CurStep, sub.SubName_ErrorStep)  # Store step where error occurred

    # Synchronize current and stored step.
    with Rung(sub.SubName_CurStep != sub.SubName_StoredStep):
        copy(sub.SubName_CurStep, sub.SubName_StoredStep)

    # ==============================================================================
    # SECTION 2: STEP IMPLEMENTATION SECTION - MODIFY THIS SECTION ONLY
    # ==============================================================================

    """
    EXAMPLE STEP IMPLEMENTATION WITH TROUBLESHOOTING SCENARIO:

    # Step 1: Initialization (first odd step)
    with Rung(ds.motor_CurStep == 1):
        # Initialize system
        reset(c.motor_1)
        reset(c.valve_2)

    # Transition 1→3: Auto-transition after initialization
    with Rung(ds.motor_CurStep == 1):
        # Always transition from initialization step
        copy(1, ds.motor_Trans)

    # Step 3: Motor operation step
    with Rung(ds.motor_CurStep == 3):
        # Start motor and related operations
        latch(c.motor_1)
        latch(c.valve_2)

        # TROUBLESHOOTING SCENARIO:
        # If the sequence gets stuck here and you need to go back to step 1:
        # 1. Manually set ds.motor_CurStep = 2 (even number)
        # 2. The system will detect the even step number and:
        #    - NOT execute any step logic (since even steps have no logic)
        #    - Automatically reset all timers and one-shots
        #    - Increment to step 3 in the next scan
        # This provides a safe way to manually intervene without causing
        # unexpected behavior or unsafe conditions.

    # Transition 3→5: Proceed when sensor detects position
    with Rung(ds.motor_CurStep == 3):
        if c.position_sensor == 1:
            copy(1, ds.motor_Trans)  # Will advance to step 5
    """

    # Step 1: Initialization (first odd step)
    with Rung(sub.SubName_CurStep == 1):
        # Initial step logic here.
        pass

    # Transition 1→3: Auto-transition after initialization.
    with Rung(sub.SubName_CurStep == 1):
        # Always transition from initialization step.
        copy(1, sub.SubName_Trans)  # Will advance to step 3

    # Step 3: Example step with one-shot timer (second odd step).
    with Rung(sub.SubName_CurStep == 3):
        # Start a one-shot timer.
        on_delay(step3_timer, step3_timer_acc, setpoint=2000, time_unit=TimeUnit.Tms)
        # This timer will be automatically reset if:
        # 1. We transition normally to step 5 (via step 4)
        # 2. We manually set CurStep to 2 during troubleshooting
        # Other step 3 logic here.

    # Transition 3→5: Move to next step when timer completes.
    with Rung(step3_timer):
        copy(1, sub.SubName_Trans)  # Will advance to step 5

    # Step 5: Example step with one-time operation.
    with Rung(sub.SubName_CurStep == 5):
        # This one-time operation will execute only once when entering step 5.
        copy(1, sub.one_time_operation_complete, oneshot=True)

    # Other continuous step 5 logic here.
    with Rung():
        pass

    # Add more steps and transitions as needed...
    # Remember: All step numbers must be ODD (1, 3, 5, 7, etc.)

    # ==============================================================================
    # SECTION 3: SFC Pause and Shutdown Functionality - Add Custom Conditions Below
    # ==============================================================================

    # Pause functionality.
    with Rung(sub.SubName_xPause == 1):
        # Add any actions needed for safe pausing (e.g., shutdown pumps)
        # Example: reset(c.Rotate1)
        pass

    # Add your custom pause requirements in this rung if needed.
    with Rung(sub.SubName_xPause == 1):
        copy(0, sub.SubName__x)

    # Shutdown functionality.
    with Rung(sub.SubName_xCall == 0):
        # Add any actions needed for safe shutdown (e.g., shutdown pumps)
        # Example: reset(c.Rotate1)
        pass

    # Standard shutdown procedure.
    with Rung(sub.SubName_xCall == 0):
        copy(0, sub.SubName_init)
        copy(0, sub.SubName_Error)
        copy(0, sub.SubName_ErrorStep)
        copy(0, SubName_t)  # Reset timer
        copy(0, SubName_CurStep_t)  # Reset timer
        copy(0, sub.SubName_ResetTmr)
        copy(0, sub.SubName_Trans)
        copy(0, sub.SubName_CurStep)
        copy(0, sub.SubName_StoredStep)
        copy(0, sub.SubName__x)
        copy(0, sub.SubName__init)
        copy(0, sub.SubName__ValStepIsOdd)
        copy(0, sub.SubName__x)  # Maintains explicit template intent.
        reset(SubName_x)
        return_()

    # ==============================================================================
    # SECTION 4: SFC EXECUTION CONTROL BOILERPLATE - DO NOT MODIFY
    # ==============================================================================

    # Reset handling - xReset is self-clearing.
    with Rung(sub.SubName_xReset == 1):
        copy(0, sub.SubName_xReset)

    # Check if current step is odd.
    with Rung():
        calc(sub.SubName_CurStep % 2, sub.SubName__ValStepIsOdd)

    # EVEN STEP HANDLING - SAFETY MECHANISM FOR MANUAL INTERVENTION.
    # This stays ABOVE incrementing CurStep.
    # If we detect an even step number (either from normal transition or manual intervention),
    # we immediately increment to the next odd step without executing any step logic.
    # This provides a safe "neutral zone" when manually changing step numbers.
    with Rung(sub.SubName__ValStepIsOdd != 1):
        calc(sub.SubName_CurStep + 1, sub.SubName_CurStep)
        # The brief pass through the even step number causes:
        # 1. All one-shot timers to reset
        # 2. All rising/falling edge detections to reset
        # 3. Any one-time operations to be ready for the next time

    # Step advancement when transition is triggered.
    with Rung(sub.SubName_Trans == 1):
        # Add 1 to current step to get next step (which will be even, triggering resets)
        calc(sub.SubName_CurStep + 1, sub.SubName_CurStep)
        copy(0, sub.SubName_Trans)  # Reset transition flag

    # Built-in timer for step duration tracking.
    with Rung(sub.SubName_CurStep == sub.SubName_StoredStep):
        on_delay(
            SubName_CurStep_tmr,
            SubName_CurStep_t,
            setpoint=0,
            time_unit=TimeUnit.Ts,
        )

    # Mark initialization complete after first step.
    with Rung(sub.SubName_CurStep == 2):
        copy(1, sub.SubName__init)  # It will be 2 (even)

    # Return from subroutine.
    with Rung():
        return_()


def build_mapping() -> TagMap:
    """Expose SFC tags to Click addresses for pyclickplc server."""
    return TagMap(
        [
            *SubNameDs.map_to(ds.select(3001, 3017)),
            SubName_x.map_to(c[1501]),
            SubName_tmr.map_to(t[301]),
            SubName_CurStep_tmr.map_to(t[302]),
            step3_timer.map_to(t[303]),
            SubName_t.map_to(td[301]),
            SubName_CurStep_t.map_to(td[302]),
            step3_timer_acc.map_to(td[303]),
        ]
    )


async def run_server(
    *,
    host: str,
    port: int,
    scan_period: float,
    run_seconds: float | None,
) -> None:
    logic = main()
    runner = PLCRunner(logic=logic)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=scan_period)

    mapping = build_mapping()
    provider = ClickDataProvider(runner=runner, tag_map=mapping)
    server = ClickServer(provider, host=host, port=port)

    stop_scan = asyncio.Event()

    async def scan_loop() -> None:
        while not stop_scan.is_set():
            runner.step()
            await asyncio.sleep(scan_period)

    await server.start()
    scan_task = asyncio.create_task(scan_loop())
    try:
        if run_seconds is None:
            await asyncio.Event().wait()
        else:
            await asyncio.sleep(run_seconds)
    finally:
        stop_scan.set()
        scan_task.cancel()
        with suppress(asyncio.CancelledError):
            await scan_task
        await server.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run SFC template soft PLC server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5020)
    parser.add_argument("--scan-period", type=float, default=0.01)
    parser.add_argument("--run-seconds", type=float, default=None)
    return parser.parse_args()


def entrypoint() -> None:
    args = parse_args()
    asyncio.run(
        run_server(
            host=args.host,
            port=args.port,
            scan_period=args.scan_period,
            run_seconds=args.run_seconds,
        )
    )


if __name__ == "__main__":
    entrypoint()


