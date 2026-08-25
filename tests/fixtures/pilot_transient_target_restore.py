"""Multi-scan target progress hidden by a same-scan rollback.

The selected input first meets an independent zero-preset hazard.  Once that
preset is corrected, ordinary program-owned steps advance over later scans.
The final autonomous step writes ``TARGET`` mid-scan, a second zero-preset
watchdog immediately writes ``INTERMEDIATE``, and a recovery subroutine then
restores that scan's ``TARGET_SOURCE`` value before scan exit.

The target write therefore belongs to later autonomous progress, not to the
selected input's exact expectation, and no checkpoint snapshot contains it.
"""

from pyrung import Bool, Int, Program, Timer, call, copy, on_delay, rung, subroutine

SOURCE = 0
ENTERED = 1
QUALIFIED = 2
TARGET_SOURCE = 3
TARGET = 4
NON_TARGET = 5
INTERMEDIATE = 8
EARLY_HAZARD = 9

State = Int("TransientRestoreState", default=SOURCE)
Advance = Bool("TransientRestoreAdvance", external=True)
EarlyPresetMs = Int("TransientRestoreEarlyPresetMs")
LaterPresetMs = Int("TransientRestoreLaterPresetMs")
EarlyWatchdog = Timer.clone("TransientRestoreEarlyWatchdog")
LaterWatchdog = Timer.clone("TransientRestoreLaterWatchdog")


with Program() as logic:
    with subroutine("TransientRestoreRollback"):
        with rung():
            copy(TARGET_SOURCE, State, oneshot=True)

    # This consumer precedes its producer so QUALIFIED -> TARGET_SOURCE -> TARGET
    # takes two autonomous scans after the corrected input transaction.
    with rung(State == TARGET_SOURCE):
        copy(TARGET, State, oneshot=True)

    # Both displacement and rollback follow the target producer in rung order,
    # so all three writes occur in the same final autonomous scan.
    with rung(State == TARGET):
        on_delay(LaterWatchdog, LaterPresetMs)

    with rung(LaterWatchdog.Done):
        copy(INTERMEDIATE, State, oneshot=True)

    # Two exact reads observe the same predecessor write; causal ambiguity is
    # about distinct sources, not the number of guard reads.
    with rung(State == INTERMEDIATE, State >= INTERMEDIATE):
        call("TransientRestoreRollback")

    with rung(State == QUALIFIED):
        copy(TARGET_SOURCE, State, oneshot=True)

    with rung(Advance, State == SOURCE):
        copy(ENTERED, State, oneshot=True)

    with rung(State == ENTERED):
        copy(QUALIFIED, State, oneshot=True)
        on_delay(EarlyWatchdog, EarlyPresetMs)

    # This independent hazard is encountered before autonomous continuation.
    with rung(EarlyWatchdog.Done):
        copy(EARLY_HAZARD, State, oneshot=True)


# A control with the same selected action, early hazard, and autonomous scan
# structure, but without the exact TARGET appearance.  The later watchdog is
# still guarded by TARGET and therefore cannot become a target-displacement
# requirement merely because the earlier action expectation exists.
with Program() as without_target_logic:
    with subroutine("TransientRestoreControlRollback"):
        with rung():
            copy(TARGET_SOURCE, State, oneshot=True)

    with rung(State == TARGET_SOURCE):
        copy(NON_TARGET, State, oneshot=True)

    with rung(State == TARGET):
        on_delay(LaterWatchdog, LaterPresetMs)

    with rung(LaterWatchdog.Done):
        copy(INTERMEDIATE, State, oneshot=True)

    with rung(State == INTERMEDIATE, State >= INTERMEDIATE):
        call("TransientRestoreControlRollback")

    with rung(State == QUALIFIED):
        copy(TARGET_SOURCE, State, oneshot=True)

    with rung(Advance, State == SOURCE):
        copy(ENTERED, State, oneshot=True)

    with rung(State == ENTERED):
        copy(QUALIFIED, State, oneshot=True)
        on_delay(EarlyWatchdog, EarlyPresetMs)

    with rung(EarlyWatchdog.Done):
        copy(EARLY_HAZARD, State, oneshot=True)
