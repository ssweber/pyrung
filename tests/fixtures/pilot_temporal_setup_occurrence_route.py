"""Neutral reset/gate sequence whose setup is visible only inside one scan.

The ordering mirrors the late field transaction without its domain names:
reset first writes the productive value, one gate state displaces it to the
network fault, and the corrective gate state exposes a second displacement.
Restoring that gate at the retained source changes no endpoint tag; its value
is nevertheless read by the exact short-circuit guard before fresh Compass
steering retries reset.
"""

from pyrung import Bool, Int, Program, copy, rung

SOURCE = 92
PRODUCTIVE = 10
TARGET = 81
FIRST_FAULT = 94

SequenceState = Int("OccurrenceRouteSequenceState", default=SOURCE)
ResetCommand = Bool("OccurrenceRouteResetCommand", external=True)
GateAvailable = Bool("OccurrenceRouteGateAvailable", external=True, default=True)


with Program() as logic:
    # Reverse route order makes PRODUCTIVE an adjacent-scan handoff.
    with rung(SequenceState == PRODUCTIVE):
        copy(TARGET, SequenceState, oneshot=True)

    with rung(ResetCommand):
        copy(PRODUCTIVE, SequenceState, oneshot=True)

    # The first reset attempt reaches PRODUCTIVE and is displaced here.
    with rung(GateAvailable, SequenceState == PRODUCTIVE):
        copy(FIRST_FAULT, SequenceState, oneshot=True)

    # Reset + gate-false prevents the first fault but exposes this later one.
    # Gate-true then satisfies this guard's exact complement by short circuit:
    # SequenceState is not read, and no endpoint tag needs to change.
    with rung(~GateAvailable, SequenceState == PRODUCTIVE):
        copy(SOURCE, SequenceState, oneshot=True)
