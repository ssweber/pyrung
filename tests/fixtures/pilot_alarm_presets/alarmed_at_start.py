"""Fixture: zero preset, same-scan alarm, and reset recovery.

The process starts alarmed.  ``Reset`` is a usable alarm-recovery command: its
one-shot copies the process to ``RUNNING``, where ``AtTarget`` completes it in
the same scan.  A watchdog also executes in that scan.  Its dynamic preset has
no writer, comparison, or nonzero default, so zero is the only value the
program gives PILOT to discover.  At zero the watchdog finishes immediately
and its later writer restores ``ALARMED``.

After that failed attempt, changing the preset is not enough: the continuously
held ``Reset`` condition has already spent the one-shot that enters
``RUNNING``.  Recovery must correct the preset *and* release/reassert ``Reset``.
"""

from pyrung import (
    PLC,
    Bool,
    Int,
    Program,
    Timer,
    branch,
    copy,
    on_delay,
    rung,
)

RUNNING = 40
COMPLETE = 80
ALARMED = 91
SAFE_WATCHDOG_PRESET_MS = 20

ProcessStep = Int("ProcessStep", default=ALARMED)
Reset = Bool("Reset", external=True)
AtTarget = Bool("AtTarget", external=True)

# Intentionally no writer, nonzero default, or comparison that supplies a
# useful domain value.  The timer reads this tag, so it is a steerable input,
# but the program only exposes its default value of zero.
WatchdogPresetMs = Int("WatchdogPresetMs")
Watchdog = Timer.clone("Watchdog")


with Program() as logic:
    # Reset is a genuine route out of alarm, but its copy fires only once while
    # this alarm-gated condition remains continuously powered.
    with rung(Reset, ProcessStep >= ALARMED):
        copy(RUNNING, ProcessStep, oneshot=True)

    with rung(ProcessStep == RUNNING):
        with branch(AtTarget):
            copy(COMPLETE, ProcessStep, oneshot=True)

        # The parent rung was enabled at its start, so this still executes
        # after the branch above changes ProcessStep to COMPLETE.
        on_delay(Watchdog, WatchdogPresetMs)

    # This later writer wins the scan when the zero-preset watchdog completes.
    with rung(Watchdog.Done):
        copy(ALARMED, ProcessStep, oneshot=True)


def pilot_plan():
    """Ask PILOT to reach COMPLETE from the initial alarmed state."""

    return PLC(logic, dt=0.010).how(ProcessStep == COMPLETE, max_scans=100)


def composite_reset() -> int:
    """Show that preset + Reset + AtTarget is a usable clean recovery."""

    plc = PLC(logic, dt=0.010)
    plc.force(WatchdogPresetMs, SAFE_WATCHDOG_PRESET_MS)
    plc.force(Reset, True)
    plc.force(AtTarget, True)
    plc.step()
    return plc.state.tags[ProcessStep.name]


def manual_recovery() -> tuple[int, ...]:
    """Demonstrate the missing composite temporal correction."""

    plc = PLC(logic, dt=0.010)
    plc.force(Reset, True)
    plc.force(AtTarget, True)

    plc.step()
    alarm_after_zero_preset = plc.state.tags[ProcessStep.name]

    # Correcting the preset alone cannot refire the spent Reset one-shot.
    plc.force(WatchdogPresetMs, SAFE_WATCHDOG_PRESET_MS)
    plc.step()
    alarm_after_preset_only = plc.state.tags[ProcessStep.name]

    # Release and reassert Reset to rearm the alarm-recovery route.
    plc.force(Reset, False)
    plc.step()
    alarm_after_reset_release = plc.state.tags[ProcessStep.name]

    plc.force(Reset, True)
    plc.step()
    complete_after_reset_reassert = plc.state.tags[ProcessStep.name]

    return (
        alarm_after_zero_preset,
        alarm_after_preset_only,
        alarm_after_reset_release,
        complete_after_reset_reassert,
    )


if __name__ == "__main__":
    plan = pilot_plan()
    clean_recovery = composite_reset()
    recovery = manual_recovery()

    print(f"PILOT reachable: {plan.reachable}")
    print(f"PILOT reason: {plan.reason}")
    print(f"Composite reset step: {clean_recovery}")
    print(f"Manual recovery steps: {recovery}")

    assert not plan.reachable
    assert clean_recovery == COMPLETE
    assert recovery == (ALARMED, ALARMED, ALARMED, COMPLETE)
