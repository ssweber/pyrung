"""Fixture: first-scan destruction that PILOT would need to anticipate.

``ProcessStep`` starts at 0.  During scan 0, the first-scan writer sets it to
10 and normal scan-order fallthrough advances it through ``AT_TARGET``.  The
same enabled rung runs a zero-preset watchdog, whose later writer wins the scan
and commits ``ABORTED`` before PILOT has taken an action.
"""

from pyrung import PLC, Int, Program, Timer, copy, on_delay, rung, system

INITIAL = 0
READY = 10
RUNNING = 40
AT_TARGET = 80
ABORTED = 90

ProcessStep = Int("FirstScanProcessStep", default=INITIAL)

# No writer, nonzero default, or comparison supplies a useful preset value.
WatchdogPresetMs = Int("FirstScanWatchdogPresetMs")
Watchdog = Timer.clone("FirstScanWatchdog")


with Program() as logic:
    with rung(system.sys.first_scan):
        copy(READY, ProcessStep)

    with rung(ProcessStep == READY):
        copy(RUNNING, ProcessStep, oneshot=True)

    with rung(ProcessStep == RUNNING):
        copy(AT_TARGET, ProcessStep, oneshot=True)
        on_delay(Watchdog, WatchdogPresetMs)

    with rung(Watchdog.Done):
        copy(ABORTED, ProcessStep, oneshot=True)


def first_scan_endpoint() -> tuple[int, int]:
    """Return the initial value and the committed endpoint of scan 0."""

    plc = PLC(logic, dt=0.010)
    initial = plc.state.tags.get(ProcessStep.name, ProcessStep.default)
    plc.step()
    return initial, plc.state.tags[ProcessStep.name]


def pilot_plan():
    """Ask PILOT for the target that scan 0 reaches only transiently."""

    return PLC(logic, dt=0.010).how(ProcessStep == AT_TARGET, max_scans=100)


if __name__ == "__main__":
    initial, endpoint = first_scan_endpoint()
    plan = pilot_plan()

    print(f"Before scan 0: {initial}")
    print(f"After scan 0: {endpoint}")
    print(f"PILOT reachable: {plan.reachable}")
    print(f"PILOT reason: {plan.reason}")

    assert initial == INITIAL
    assert endpoint == ABORTED
    assert not plan.reachable
