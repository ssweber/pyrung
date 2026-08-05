"""Neutral producer/parent-consumer/branch/watchdog recovery fixture.

The command writes an intermediate state.  The next parent rung directly
reads that state, while its child branch reads a separate enable and advances
the state.  Because rung enablement is fixed at entry, the watchdog below the
branch still executes after the branch advances the state.  Its zero preset
therefore completes in the same scan and a later alarm writer wins.

This shape pins two independent contracts:

* the intermediate write is consumed by the parent rung, not by the child
  branch which reads only ``AdvanceEnable``; and
* after that handoff succeeds, the later watchdog consequence must link back
  to the committed expectation and retry only this local transaction with a
  corrected preset.
"""

from pyrung import Bool, Int, Program, Timer, branch, copy, on_delay, rung

IDLE = 0
INTERMEDIATE = 1
ADVANCED = 2
ALARMED = 9

SequenceState = Int("NeutralSequenceState", default=IDLE)
StartCommand = Bool("NeutralStartCommand", external=True)
# Already true at the source so the first selected command exercises the
# producer -> parent -> branch transaction directly.  Recovery must not need a
# separate prerequisite attempt to discover the watchdog consequence.
AdvanceEnable = Bool("NeutralAdvanceEnable", default=True, external=True)
WatchdogPresetMs = Int("NeutralWatchdogPresetMs")
Watchdog = Timer.clone("NeutralWatchdog")


with Program() as logic:
    with rung(StartCommand, SequenceState == IDLE):
        copy(INTERMEDIATE, SequenceState, oneshot=True)

    with rung(SequenceState == INTERMEDIATE):
        with branch(AdvanceEnable):
            copy(ADVANCED, SequenceState, oneshot=True)

        # The parent was enabled by its entry read of INTERMEDIATE.  This
        # executes even though the preceding child branch wrote ADVANCED.
        on_delay(Watchdog, WatchdogPresetMs)

    with rung(Watchdog.Done):
        copy(ALARMED, SequenceState, oneshot=True)
