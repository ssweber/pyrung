"""Autonomous target-path progress with an off-path same-value writer.

The final rung writes the same channel value as the legitimate autonomous
producer, but it is not the producer selected on the target backward trace and
also latches an unrelated side effect.  A folded-repair proof must join the
actual write occurrence to the selected writer rather than borrowing the
legitimate producer's ProgramStep reading.
"""

from pyrung import Bool, Int, Program, Timer, branch, copy, latch, on_delay, rung

IDLE = 0
ENTERED = 1
QUALIFIED = 2
ADVANCING = 3
AWAITING_CONFIRMATION = 4
COMPLETE = 5
HAZARD = 8

SequenceState = Int("SameLandingSequenceState", default=IDLE)
StartCommand = Bool("SameLandingStartCommand", external=True)
EntryReady = Bool("SameLandingEntryReady", default=True)
ConfirmCommand = Bool("SameLandingConfirmCommand", external=True)
PresetMs = Int("SameLandingPresetMs")
Watchdog = Timer.clone("SameLandingWatchdog")
InterferenceArmed = Bool("SameLandingInterferenceArmed", default=True)
InterferenceLatched = Bool("SameLandingInterferenceLatched")


with Program() as logic:
    with rung(SequenceState == AWAITING_CONFIRMATION, ConfirmCommand):
        copy(COMPLETE, SequenceState, oneshot=True)

    with rung(SequenceState == ADVANCING):
        copy(AWAITING_CONFIRMATION, SequenceState, oneshot=True)

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

    # This runs after the legitimate ADVANCING -> AWAITING_CONFIRMATION writer.
    # The no-op channel write is still an exact occurrence and its side effect
    # makes borrowing the target writer's proof observably unsound.
    with rung(SequenceState == AWAITING_CONFIRMATION, InterferenceArmed):
        copy(AWAITING_CONFIRMATION, SequenceState)
        latch(InterferenceLatched)
