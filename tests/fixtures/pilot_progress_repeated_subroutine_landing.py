"""A target landing writer invoked from both selected and off-path call sites."""

from pyrung import Bool, Int, Program, Timer, branch, call, copy, latch, on_delay, rung, subroutine

IDLE = 0
ENTERED = 1
QUALIFIED = 2
ADVANCING = 3
AWAITING_CONFIRMATION = 4
COMPLETE = 5
HAZARD = 8

SequenceState = Int("RepeatedLandingSequenceState", default=IDLE)
StartCommand = Bool("RepeatedLandingStartCommand", external=True)
EntryReady = Bool("RepeatedLandingEntryReady", default=True)
ConfirmCommand = Bool("RepeatedLandingConfirmCommand", external=True)
PresetMs = Int("RepeatedLandingPresetMs")
Watchdog = Timer.clone("RepeatedLandingWatchdog")
InterferenceArmed = Bool("RepeatedLandingInterferenceArmed", default=True)
InterferenceLatched = Bool("RepeatedLandingInterferenceLatched")


with Program() as logic:
    with subroutine("RepeatedLandingWriter"):
        with rung():
            copy(AWAITING_CONFIRMATION, SequenceState)

    with rung(SequenceState == AWAITING_CONFIRMATION, ConfirmCommand):
        copy(COMPLETE, SequenceState, oneshot=True)

    # The selected target path reaches this first call site from ADVANCING.
    with rung(SequenceState == ADVANCING):
        call("RepeatedLandingWriter")

    with rung(SequenceState == QUALIFIED):
        copy(ADVANCING, SequenceState, oneshot=True)

    with rung(StartCommand, SequenceState == IDLE):
        copy(ENTERED, SequenceState, oneshot=True)

    with rung(SequenceState == ENTERED):
        with branch(EntryReady):
            copy(QUALIFIED, SequenceState, oneshot=True)
        on_delay(Watchdog, PresetMs)

    with rung(Watchdog.Done):
        copy(HAZARD, SequenceState, oneshot=True)

    # The same subroutine rung runs again from a distinct off-path caller after
    # the legitimate landing and owns the actual last same-value occurrence.
    with rung(SequenceState == AWAITING_CONFIRMATION, InterferenceArmed):
        call("RepeatedLandingWriter")
        latch(InterferenceLatched)
