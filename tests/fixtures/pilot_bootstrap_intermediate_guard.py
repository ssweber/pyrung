"""Scan-zero recovery whose designated effect is only an intermediate state."""

from pyrung import Bool, Int, Program, copy, out, rung, system

IDLE = 0
INTERMEDIATE = 1
COMPLETE = 2
DIVERTED = 9

SequenceState = Int("BootstrapIntermediateSequenceState", default=IDLE)
PreserveIntermediate = Bool("BootstrapPreserveIntermediate", external=True)
FinishCommand = Bool("BootstrapFinishCommand", external=True)
IntermediateHandoff = Bool("BootstrapIntermediateHandoff")


with Program() as logic:
    with rung(system.sys.first_scan):
        copy(INTERMEDIATE, SequenceState)

    with rung(~PreserveIntermediate):
        copy(DIVERTED, SequenceState, oneshot=True)

    # This handoff is the intermediate producer's exact local consumer. It can
    # survive without satisfying the global target.
    with rung(SequenceState == INTERMEDIATE):
        out(IntermediateHandoff)

    with rung(FinishCommand, IntermediateHandoff):
        copy(COMPLETE, SequenceState, oneshot=True)
