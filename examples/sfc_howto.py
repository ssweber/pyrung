from clickplc_dsl import Addresses, Conditions, Actions, Td, Th, Tm, Ts, Tms, sub

# fmt: off
# Get address references
x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, td, ctd, sd, txt = Addresses.get()

# Get condition functions
nc, re, fe, all, any = Conditions.get()

# Get action functions
out, set, reset, ton, tof, rton, rtof, ctu, ctd, ctud, copy, copy_block, copy_fill, copy_pack, copy_unpack, shift, search, math_decimal, math_hex, call, next_loop, end = Actions.get()
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
1. Rename all instances of 'subName' to your actual subroutine name
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
# subName_xCall - ds     # External trigger to call the SFC
# subName_xInit - ds     # External trigger to initialize the SFC
# subName_xReset - ds    # External trigger to reset the SFC
# subName_xPause - ds    # External trigger to pause the SFC
# subName_Error - ds     # Flag indicating an error has occurred
# subName_ErrorStep - ds # Step where the error occurred
# subName_EnableLimit - ds # Enable time limit checking
# subName_Limit_Ts - ds  # Time limit for steps in Ts (second) units
# subName_ResetTmr - ds  # Reset timer flag
# subName_Trans - ds     # Transition flag
# subName_CurStep - ds   # Current step number
# subName_StoredStep - ds # Previous step number
# subName__x - ds       # Internal execution flag
# subName__init - ds    # Internal initialization flag
# subName__ValStepIsOdd - ds # Flag indicating if current step is odd


def main():
    """Main program that calls the SFC subroutine when triggered."""
    with Rung(ds.subName_xCall == 1, ds.subName_xPause != 1):
        copy(1, ds.subName__x)
    with Rung(ds.subName__x == 1):
        call(subName)

    with Rung():
        end()


@sub
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
        set(c.subName_x)

    # Shared Resets for Init and Pause
    with Rung(any([ds.subName_xInit == 1, ds.subName_xReset == 1])):
        copy(0, ds.subName_init)  # Reset init flag
        copy(0, td.subName_t)  # Reset timer value
        copy(0, td.subName_CurStep_t)  # Reset timer value
        # Set initial step to 1 (first odd number)
        copy_fill(1, ds[subName_CurStep:subName_StoredStep])

    # Built-in timer for step timeout detection
    with Rung(ds.subName_xCall == 1):
        rton(
            t.subName_tmr,
            setpoint=ds.subName_Limit_Ts,
            unit=Ts,
            reset=lambda: ds.subName_ResetTmr == 1,
        )

    # Reset the timer reset flag after it's been processed
    with Rung(ds.subName_ResetTmr == 1):
        copy(0, ds.subName_ResetTmr)

    # Error detection - If step exceeds time limit
    with Rung(td.subName_t >= ds.subName_Limit_Ts, ds.subName_EnableLimit == 1):
        # Set Error flag
        copy(1, ds.subName_Error)
        # Store the step where the error occurred
        copy(ds.subName_CurStep, ds.subName_ErrorStep)

    # Synchronize current and stored step
    with Rung(ds.subName_CurStep != ds.subName_StoredStep):
        copy(ds.subName_CurStep, ds.subName_StoredStep)

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
        set(c.motor_1)
        set(c.valve_2)
        
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
    with Rung(ds.subName_CurStep == 1):
        # Initial step logic here
        pass

    # Transition 1→3: Auto-transition after initialization
    with Rung(ds.subName_CurStep == 1):
        # Always transition from initialization step
        copy(1, ds.subName_Trans)  # Will advance to step 3

    # Step 3: Example step with one-shot timer (second odd step)
    with Rung(ds.subName_CurStep == 3):
        # Start a one-shot timer
        ton(t.step3_timer, 2000, Tms)

        # This timer will be automatically reset if:
        # 1. We transition normally to step 5 (via step 4)
        # 2. We manually set CurStep to 2 during troubleshooting

        # Other step 3 logic here
        pass

    # Transition 3→5: Move to next step when timer completes
    with Rung(t.step3_timer):
        copy(1, ds.subName_Trans)  # Will advance to step 5

    # Step 5: Example step with one-time operation
    with Rung(ds.subName_CurStep == 5):
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
    with Rung(ds.subName_xPause == 1):
        # Add any actions needed for safe pausing (e.g., shutdown pumps)
        # Example: reset(c.Rotate1)
        pass

    with Rung(
        ds.subName_xPause == 1,
        # Add your custom pause requirements here
        # Example: nc(c.Rotate1Sensor),
    ):
        copy(0, ds.subName__x)

    # Shutdown functionality
    with Rung(ds.subName_xCall == 0):
        # Add any actions needed for safe shutdown (e.g., shutdown pumps)
        # Example: reset(c.Rotate1)
        pass

    # Standard shutdown procedure
    with Rung(
        ds.subName_xCall == 0,
        # Add your custom shutdown requirements here
        # Example: nc(c.Rotate1Sensor),
    ):
        copy(0, ds[subName_init:subName_ErrorStep])
        copy(0, td.subName_t)  # Reset timer
        copy(0, td.subName_CurStep_t)  # Reset timer
        copy_fill(
            0, ds[subName__ResetTmr:subName__ValStepIsOdd]
        )  # which sets ds.subName__x to 0
        copy(0, ds.subName__x)
        reset(c.subName_x)
        return

    # ==============================================================================#
    # SECTION 4: SFC EXECUTION CONTROL BOILERPLATE - DO NOT MODIFY
    # ==============================================================================#

    # Reset handling - xReset is self-clearing
    with Rung(ds.subName_xReset == 1):
        copy(0, ds.subName_xReset)

    # Check if current step is odd
    with Rung():
        math_decimal("df.subName_CurStep MOD 2", ds.subName__ValStepIsOdd)

    # EVEN STEP HANDLING - SAFETY MECHANISM FOR MANUAL INTERVENTION
    # This stays ABOVE incrementing CurStep
    # If we detect an even step number (either from normal transition or manual intervention),
    # we immediately increment to the next odd step without executing any step logic.
    # This provides a safe "neutral zone" when manually changing step numbers.
    with Rung(ds.subName__ValStepIsOdd != 1):  # If step is even
        math_decimal("<ds.subName_CurStep> + 1", ds.subName_CurStep)  # Make it odd
        # The brief pass through the even step number causes:
        # 1. All one-shot timers to reset
        # 2. All rising/falling edge detections to reset
        # 3. Any one-time operations to be ready for the next time

    # Step advancement when transition is triggered
    with Rung(ds.subName_Trans == 1):
        # Add 1 to current step to get next step (which will be even, triggering resets)
        math_decimal("ds.subName_CurStep + 1", ds.subName_CurStep)
        copy(0, ds.subName_Trans)  # Reset transition flag

    # Built-in timer for step duration tracking
    with Rung(ds.subName_CurStep == ds.subName_StoredStep):
        ton(t.subName_CurStep_tmr, setpoint=0, unit=Ts)

    # Mark initialization complete after first step
    with Rung(ds.subName_CurStep == 2):  # It will be 2 (even)
        copy(1, ds.subName__init)

    # Return from subroutine
    with Rung():
        return
