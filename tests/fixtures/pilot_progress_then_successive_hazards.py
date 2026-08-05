"""Neutral multi-scan progress followed by two repairable hazards.

The rung order gives the selected start an exact same-scan consumer, then puts
the autonomous state consumers above their producers.  After the first
watchdog is repaired, the program owns two multi-scan transitions and then
genuinely stops for a fresh confirmation input.
That confirmation reaches the target writer, but a second zero-preset
watchdog produces the final regressive landing later in the same scan.

This differs from the compact successive-hazard fixture: the two requirements
belong to distinct source actions separated by legitimate autonomous program
progress.  Recovery must preserve the first correction, reorient at the new
input boundary, and link the later displacement to the confirmation action.
"""

from pyrung import Bool, Int, Program, Timer, branch, copy, on_delay, rung

IDLE = 0
ENTERED = 1
QUALIFIED = 2
ADVANCING = 3
AWAITING_CONFIRMATION = 4
COMPLETE = 5
FIRST_HAZARD = 8
SECOND_HAZARD = 9

SequenceState = Int("ProgressHazardSequenceState", default=IDLE)
StartCommand = Bool("ProgressHazardStartCommand", external=True)
EntryReady = Bool("ProgressHazardEntryReady", default=True)
ConfirmCommand = Bool("ProgressHazardConfirmCommand", external=True)
FirstPresetMs = Int("ProgressHazardFirstPresetMs")
SecondPresetMs = Int("ProgressHazardSecondPresetMs")
FirstWatchdog = Timer.clone("ProgressHazardFirstWatchdog")
SecondWatchdog = Timer.clone("ProgressHazardSecondWatchdog")


with Program() as logic:
    # Autonomous consumers precede their producers so each later transition
    # is observable in a distinct scan.  The final input is intentionally not
    # needed until the program has completed both transitions below it.
    with rung(SequenceState == AWAITING_CONFIRMATION, ConfirmCommand):
        copy(COMPLETE, SequenceState, oneshot=True)
        on_delay(SecondWatchdog, SecondPresetMs)

    with rung(SequenceState == ADVANCING):
        copy(AWAITING_CONFIRMATION, SequenceState, oneshot=True)

    with rung(SequenceState == QUALIFIED):
        copy(ADVANCING, SequenceState, oneshot=True)

    with rung(StartCommand, SequenceState == IDLE):
        copy(ENTERED, SequenceState, oneshot=True)

    with rung(SequenceState == ENTERED):
        with branch(EntryReady):
            copy(QUALIFIED, SequenceState, oneshot=True)
        on_delay(FirstWatchdog, FirstPresetMs)

    # These writers are deliberately later than all legitimate state motion.
    # With a zero preset they win the scan in which their watchdog completes.
    with rung(FirstWatchdog.Done):
        copy(FIRST_HAZARD, SequenceState, oneshot=True)

    with rung(SecondWatchdog.Done):
        copy(SECOND_HAZARD, SequenceState, oneshot=True)
