"""One local transaction exposing two ordered, independently repairable hazards."""

from pyrung import Bool, Int, Program, Timer, copy, on_delay, rung

IDLE = 0
COMPLETE = 1
FIRST_HAZARD = 8
SECOND_HAZARD = 9

SequenceState = Int("SuccessiveHazardSequenceState", default=IDLE)
CompleteCommand = Bool("SuccessiveHazardCompleteCommand", external=True)
FirstPresetMs = Int("SuccessiveHazardFirstPresetMs")
SecondPresetMs = Int("SuccessiveHazardSecondPresetMs")
FirstWatchdog = Timer.clone("SuccessiveHazardFirstWatchdog")
SecondWatchdog = Timer.clone("SuccessiveHazardSecondWatchdog")


with Program() as logic:
    with rung(CompleteCommand, SequenceState == IDLE):
        copy(COMPLETE, SequenceState, oneshot=True)

    # The first zero-preset watchdog diverts the transaction before the second
    # watchdog is enabled. Once its exact requirement is repaired, the same
    # local transaction proceeds far enough to expose the second hazard.
    with rung(SequenceState == COMPLETE):
        on_delay(FirstWatchdog, FirstPresetMs)

    with rung(FirstWatchdog.Done):
        copy(FIRST_HAZARD, SequenceState, oneshot=True)

    with rung(SequenceState == COMPLETE):
        on_delay(SecondWatchdog, SecondPresetMs)

    with rung(SecondWatchdog.Done):
        copy(SECOND_HAZARD, SequenceState, oneshot=True)
