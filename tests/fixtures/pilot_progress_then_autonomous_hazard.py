"""Neutral pulse recovery followed by an actionless program-transaction hazard.

The start command exposes the first same-scan watchdog displacement.  Once that
is repaired, the program advances autonomously to a second source boundary.
The final autonomous Coast reaches its exact COMPLETE consumer and is then
displaced by a second watchdog later in the same scan.  Retrying that second
transaction therefore requires no user action: Compass must reread the Coast
and WorkingTheory must carry only the composed intrascan correction into it.
"""

from pyrung import Bool, Int, Program, Timer, branch, copy, on_delay, rung

IDLE = 0
ENTERED = 1
QUALIFIED = 2
ADVANCING = 3
COMPLETE = 4
FIRST_HAZARD = 8
SECOND_HAZARD = 9

SequenceState = Int("AutonomousHazardSequenceState", default=IDLE)
StartCommand = Bool("AutonomousHazardStartCommand", external=True)
EntryReady = Bool("AutonomousHazardEntryReady", default=True)
FirstPresetMs = Int("AutonomousHazardFirstPresetMs")
SecondPresetMs = Int("AutonomousHazardSecondPresetMs")
FirstWatchdog = Timer.clone("AutonomousHazardFirstWatchdog")
SecondWatchdog = Timer.clone("AutonomousHazardSecondWatchdog")


with Program() as logic:
    # Autonomous consumers precede their producers so each transition belongs
    # to a distinct physical scan.  The COMPLETE write is the second
    # transaction's exact consumer; its watchdog displacement is later below.
    with rung(SequenceState == ADVANCING):
        copy(COMPLETE, SequenceState, oneshot=True)
        on_delay(SecondWatchdog, SecondPresetMs)

    with rung(SequenceState == QUALIFIED):
        copy(ADVANCING, SequenceState, oneshot=True)

    with rung(StartCommand, SequenceState == IDLE):
        copy(ENTERED, SequenceState, oneshot=True)

    with rung(SequenceState == ENTERED):
        with branch(EntryReady):
            copy(QUALIFIED, SequenceState, oneshot=True)
        on_delay(FirstWatchdog, FirstPresetMs)

    with rung(FirstWatchdog.Done):
        copy(FIRST_HAZARD, SequenceState, oneshot=True)

    with rung(SecondWatchdog.Done):
        copy(SECOND_HAZARD, SequenceState, oneshot=True)
