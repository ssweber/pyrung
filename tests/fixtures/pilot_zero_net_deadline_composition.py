"""Neutral multi-scan target rollback with a composable same-tag deadline."""

from pyrung import Bool, Int, Program, call, copy, out, rung, subroutine

SOURCE = 98
ENTERED = 40
QUALIFIED = 50
TARGET_SOURCE = 30
TARGET = 81
LOW = 10
THRESHOLD = 20

State = Int("ZeroNetDeadlineState", default=SOURCE)
Advance = Bool("ZeroNetDeadlineAdvance", external=True)
KeepTarget = Bool("ZeroNetDeadlineKeepTarget", external=True)
AlwaysOff = Bool("ZeroNetDeadlineAlwaysOff", readonly=True)
LinkHealthy = Bool("ZeroNetDeadlineLinkHealthy")


with Program() as logic:
    with subroutine("ZeroNetDeadlineRollback"):
        with rung(~LinkHealthy, State <= THRESHOLD):
            copy(TARGET_SOURCE, State, oneshot=True)

    # This program-owned output is deliberately not a Pilot lever.  It leaves
    # the final rollback guard's first OR alternative unavailable.
    with rung(AlwaysOff):
        out(LinkHealthy)

    # The target belongs to autonomous continuation several scans after the
    # selected input transaction.
    with rung(State == TARGET_SOURCE):
        copy(TARGET, State, oneshot=True)

    # Preventing this earlier displacement is the exact correction eventually
    # reached by composing State > THRESHOLD through its read deadline.
    with rung(~KeepTarget, State == TARGET):
        copy(LOW, State, oneshot=True)

    with rung():
        call("ZeroNetDeadlineRollback")

    # Reverse rung order makes each autonomous state occupy one scan exit.
    with rung(State == QUALIFIED):
        copy(TARGET_SOURCE, State, oneshot=True)

    with rung(State == ENTERED):
        copy(QUALIFIED, State, oneshot=True)

    with rung(Advance, State == SOURCE):
        copy(ENTERED, State, oneshot=True)
