from clickplc_dsl import Addresses, Conditions, Actions, Td, Th, Tm, Ts, Tms, Rung

# fmt: off
# Get address references
x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, td, ctd, sd, txt = Addresses.get()

# Get condition functions
nc, re, fe = Conditions.get()

# Get action functions
out, latch, reset, ton, tof, rton, rtof, ctu, ctd, ctud, copy, copy_block, copy_fill, copy_pack, copy_unpack, shift, search, math, math_hex, call, for_loop, next_loop, end = Actions.get()
# fmt: on

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


def main():
    """Main program that calls the SFC subroutine when triggered."""
    with Rung(ds.SubName_xCall == 1, ds.SubName_xPause != 1):
        copy(1, ds.SubName__x)
    with Rung(ds.SubName__x == 1):
        call(SubName)

    with Rung():
        end()


def subRoutine1():
    """
    SFC Implementation Template

    This subroutine implements a Sequential Function Chart (SFC) pattern
    for the Click PLC using the clickplc_dsl library.

    IMPORTANT: All steps MUST use ODD numbers (1, 3, 5, 7, etc.)

    The implementation consists of three main sections:
    1. SFC INITIALIZATION BOILERPLATE
    2. STEP IMPLEMENTATION SECTION
    3. PAUSE AND SHUTDOWN IMPLEMENTATION SECTION
    3. SFC EXECUTION CONTROL BOILERPLATE

    When implementing a new SFC, only modify the STEP, Pause and Shutdown IMPLEMENTATION SECTIONS.
    """

    # ==============================================================================#
    # SECTION 1: SFC INITIALIZATION BOILERPLATE - DO NOT MODIFY
    # ==============================================================================#

    # Set active flag for current step
    with Rung():
        # CODESYS: step.x. Variable of type Bool which is true while Step is Active
        # ClickPlc. c bit
        latch(c.SubName_x)

    # Shared Resets for Init and Pause
    with Rung(any([ds.SubName_xInit == 1, ds.SubName_xReset == 1])):
        copy(0, ds.SubName_init)  # Reset init flag
        copy(0, td.SubName_t)  # Reset timer value
        copy(0, td.SubName_CurStep_t)  # Reset timer value
        # Set initial step to 1 (first odd number)
        copy_fill(1, ds[SubName_CurStep:SubName_StoredStep])

    # Built-in timer for step timeout detection
    with Rung(ds.SubName_xCall == 1):
        rton(
            t.SubName_tmr,
            setpoint=ds.SubName_Limit_Ts,
            unit=Ts,
            reset=lambda: ds.SubName_ResetTmr == 1,
        )

    # Reset the timer reset flag after it's been processed
    with Rung(ds.SubName_ResetTmr == 1):
        copy(0, ds.SubName_ResetTmr)

    # Error detection - If step exceeds time limit
    with Rung(td.SubName_t >= ds.SubName_Limit_Ts, ds.SubName_EnableLimit == 1):
        # Set Error flag
        copy(1, ds.SubName_Error)
        # Store the step where the error occurred
        copy(ds.SubName_CurStep, ds.SubName_ErrorStep)

    # Synchronize current and stored step
    with Rung(ds.SubName_CurStep != ds.SubName_StoredStep):
        copy(ds.SubName_CurStep, ds.SubName_StoredStep)

    # ==============================================================================#
    # SECTION 2: STEP IMPLEMENTATION SECTION - MODIFY THIS SECTION ONLY
    # ==============================================================================#

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
    with Rung(ds.SubName_CurStep == 1):
        # Initial step logic here
        pass

    # Transition 1→3: Auto-transition after initialization
    with Rung(ds.SubName_CurStep == 1):
        # Always transition from initialization step
        copy(1, ds.SubName_Trans)  # Will advance to step 3

    # Step 3: Example step with one-shot timer (second odd step)
    with Rung(ds.SubName_CurStep == 3):
        # Start a one-shot timer
        ton(t.step3_timer, 2000, Tms)

        # This timer will be automatically reset if:
        # 1. We transition normally to step 5 (via step 4)
        # 2. We manually set CurStep to 2 during troubleshooting

        # Other step 3 logic here
        pass

    # Transition 3→5: Move to next step when timer completes
    with Rung(t.step3_timer):
        copy(1, ds.SubName_Trans)  # Will advance to step 5

    # Step 5: Example step with one-time operation
    with Rung(ds.SubName_CurStep == 5):
        # This one-time operation will execute only once when entering step 5
        copy(1, ds.one_time_operation_complete, oneshot=True)

    # Other continuous step 5 logic here
    with Rung():
        pass

    # Add more steps and transitions as needed...
    # Remember: All step numbers must be ODD (1, 3, 5, 7, etc.)

    # ==============================================================================
    # SECTION 3: SFC Pause and Shutdown Functionality - Add Custom Conditions Below
    # ==============================================================================

    # Pause functionality
    with Rung(ds.SubName_xPause == 1):
        # Add any actions needed for safe pausing (e.g., shutdown pumps)
        # Example: reset(c.Rotate1)
        pass

    with Rung(
        ds.SubName_xPause == 1,
        # Add your custom pause requirements here
        # Example: nc(c.Rotate1Sensor),
    ):
        copy(0, ds.SubName__x)

    # Shutdown functionality
    with Rung(ds.SubName_xCall == 0):
        # Add any actions needed for safe shutdown (e.g., shutdown pumps)
        # Example: reset(c.Rotate1)
        pass

    # Standard shutdown procedure
    with Rung(
        ds.SubName_xCall == 0,
        # Add your custom shutdown requirements here
        # Example: nc(c.Rotate1Sensor),
    ):
        copy(0, ds[SubName_init:SubName_ErrorStep])
        copy(0, td.SubName_t)  # Reset timer
        copy(0, td.SubName_CurStep_t)  # Reset timer
        copy_fill(0, ds[SubName__ResetTmr:SubName__ValStepIsOdd])  # which sets ds.SubName__x to 0
        copy(0, ds.SubName__x)
        reset(c.SubName_x)
        return

    # ==============================================================================#
    # SECTION 4: SFC EXECUTION CONTROL BOILERPLATE - DO NOT MODIFY
    # ==============================================================================#

    # Reset handling - xReset is self-clearing
    with Rung(ds.SubName_xReset == 1):
        copy(0, ds.SubName_xReset)

    # Check if current step is odd
    with Rung():
        math("df.SubName_CurStep MOD 2", ds.SubName__ValStepIsOdd)

    # EVEN STEP HANDLING - SAFETY MECHANISM FOR MANUAL INTERVENTION
    # This stays ABOVE incrementing CurStep
    # If we detect an even step number (either from normal transition or manual intervention),
    # we immediately increment to the next odd step without executing any step logic.
    # This provides a safe "neutral zone" when manually changing step numbers.
    with Rung(ds.SubName__ValStepIsOdd != 1):  # If step is even
        math("<ds.SubName_CurStep> + 1", ds.SubName_CurStep)  # Make it odd
        # The brief pass through the even step number causes:
        # 1. All one-shot timers to reset
        # 2. All rising/falling edge detections to reset
        # 3. Any one-time operations to be ready for the next time

    # Step advancement when transition is triggered
    with Rung(ds.SubName_Trans == 1):
        # Add 1 to current step to get next step (which will be even, triggering resets)
        math("ds.SubName_CurStep + 1", ds.SubName_CurStep)
        copy(0, ds.SubName_Trans)  # Reset transition flag

    # Built-in timer for step duration tracking
    with Rung(ds.SubName_CurStep == ds.SubName_StoredStep):
        ton(t.SubName_CurStep_tmr, setpoint=0, unit=Ts)

    # Mark initialization complete after first step
    with Rung(ds.SubName_CurStep == 2):  # It will be 2 (even)
        copy(1, ds.SubName__init)

    # Return from subroutine
    with Rung():
        return
