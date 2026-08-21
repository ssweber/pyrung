"""Neutral high-fidelity fixture for indirect sequence-step controls.

The topology models a generated controller whose process logic runs before
fault recovery and manual step management.  One state register has many
ordinary writers, reset/resume are guarded by a stop state, and next/previous
values come from indirect table reads.  The table-address scratch registers
occupy slots in that same memory block, so calculating the current address can
also overwrite table data.  With no uploaded register image, that aliasing is
real executable behavior rather than merely missing static information.
"""

from pyrung import (
    PLC,
    Block,
    Bool,
    Or,
    Program,
    TagType,
    branch,
    calc,
    call,
    copy,
    out,
    rise,
    rung,
    subroutine,
)
from pyrung.core.state import SystemState

TARGET = 100

Words = Block("IndirectRouteWords", TagType.INT, 1, 1200, retentive=True)
Words.slot(20, name="IndirectRouteStoredState")
Words.slot(21, name="IndirectRouteSequenceState")
Words.slot(24, name="IndirectRouteNextState")
Words.slot(25, name="IndirectRoutePreviousState")
Words.slot(800, name="IndirectRouteNextAddress")
Words.slot(801, name="IndirectRoutePreviousAddress")

# The controller export carries these unnamed initialized cells as its manual
# navigation tables.  They deliberately remain unnamed block slots: preserving
# those constants is part of the indirect-control topology under test.
EXPORTED_TABLE = {
    820: 25,
    825: 30,
    830: 35,
    835: 40,
    840: 45,
    845: 50,
    850: 55,
    855: 60,
    860: 65,
    865: 70,
    870: 75,
    875: 80,
    880: 85,
    885: 90,
    890: 95,
    895: 100,
    900: 105,
    905: 110,
    910: 115,
    915: 120,
    920: 125,
    925: 130,
    930: 135,
    935: 140,
    1025: 25,
    1030: 25,
    1035: 30,
    1040: 35,
    1045: 40,
    1050: 45,
    1055: 50,
    1060: 55,
    1065: 60,
    1070: 65,
    1075: 70,
    1080: 75,
    1085: 80,
    1090: 85,
    1095: 90,
    1100: 95,
}
for _address, _value in EXPORTED_TABLE.items():
    Words.slot(_address, retentive=False, default=_value)

StoredState = Words[20]
SequenceState = Words[21]
NextState = Words[24]
PreviousState = Words[25]
NextAddress = Words[800]
PreviousAddress = Words[801]

ManualModeSwitch = Bool("IndirectRouteManualModeSwitch", external=True)
AutomaticMode = Bool("IndirectRouteAutomaticMode")
ResetStep = Bool("IndirectRouteResetStep", external=True)
ResumeStep = Bool("IndirectRouteResumeStep", external=True)
NextStep = Bool("IndirectRouteNextStep", external=True)
PreviousStep = Bool("IndirectRoutePreviousStep", external=True)
Unlock = Bool("IndirectRouteUnlock", external=True)
PanelStart = Bool("IndirectRoutePanelStart", external=True)
RemoteStart = Bool("IndirectRouteRemoteStart", external=True)
CombinedStart = Bool("IndirectRouteCombinedStart")
StartWindow = Bool("IndirectRouteStartWindow")
WindowPermit = Bool("IndirectRouteWindowPermit")
CycleDone = Bool("IndirectRouteCycleDone")
# This stands in for the many process-owned prerequisites on the ordinary
# sequence writers.  It is intentionally not steerable: the fixture's manual
# route must not accidentally pass by holding one shared process input true and
# cascading through several later rungs in a single scan.
AdvancePermit = Bool("IndirectRouteAdvancePermit")
AlwaysEnabled = Bool("IndirectRouteAlwaysEnabled", default=True)
AtTarget = Bool("IndirectRouteAtTarget")


@subroutine("indirect_route_input_mapping")
def input_mapping() -> None:
    with rung(Or(PanelStart, RemoteStart)):
        out(CombinedStart)


@subroutine("indirect_route_process")
def process() -> None:
    with rung(~ManualModeSwitch):
        out(AutomaticMode)
    with rung(WindowPermit):
        out(StartWindow)

    # A representative multi-writer sequence channel.  These stages preserve
    # the chart connectivity that makes reset look like a plausible route.
    with rung(SequenceState == 145, Unlock):
        copy(25, SequenceState, oneshot=True)
    with rung(SequenceState == 25):
        with branch(rise(CombinedStart), StartWindow, ~CycleDone):
            copy(35, SequenceState)
    with rung(SequenceState == 35, AdvancePermit, AutomaticMode):
        copy(40, SequenceState, oneshot=True)
    with rung(SequenceState == 40, AdvancePermit, AutomaticMode):
        copy(55, SequenceState, oneshot=True)
    # Some exported sequences contain an intentionally empty state that
    # advances unconditionally on the scan after a manual edge lands there.
    with rung(SequenceState == 45):
        copy(50, SequenceState, oneshot=True)
    with rung(SequenceState == 55, AdvancePermit, AutomaticMode):
        copy(70, SequenceState, oneshot=True)
    with rung(SequenceState == 70, AdvancePermit, AutomaticMode):
        copy(85, SequenceState, oneshot=True)
    with rung(SequenceState == 85, AdvancePermit, AutomaticMode):
        copy(90, SequenceState, oneshot=True)
    with rung(SequenceState == 90, AdvancePermit, AutomaticMode):
        copy(95, SequenceState, oneshot=True)
    with rung(SequenceState == 95, AdvancePermit, AutomaticMode):
        copy(TARGET, SequenceState, oneshot=True)

    with rung(SequenceState == TARGET):
        out(AtTarget)


@subroutine("indirect_route_fault_recovery")
def fault_recovery() -> None:
    with rung(SequenceState == 1, ResetStep, AlwaysEnabled):
        copy(145, SequenceState, oneshot=True)
    with rung(SequenceState == 1, ResumeStep, AlwaysEnabled):
        copy(StoredState, SequenceState, oneshot=True)


@subroutine("indirect_route_manual_steps")
def manual_steps() -> None:
    with rung():
        calc(800 + SequenceState, NextAddress)
    with rung():
        copy(Words[NextAddress], NextState)
    with rung():
        calc(1000 + SequenceState, PreviousAddress)
    with rung():
        copy(Words[PreviousAddress], PreviousState)
    with rung(NextStep, ~AutomaticMode, SequenceState < 150):
        copy(NextState, SequenceState, oneshot=True)
    with rung(PreviousStep, ~AutomaticMode, SequenceState > 1):
        copy(PreviousState, SequenceState, oneshot=True)


with Program(strict=False) as logic:
    with rung():
        call(input_mapping)
    with rung():
        call(process)
    with rung():
        call(fault_recovery)
    with rung():
        call(manual_steps)


def watch_plc(
    *,
    dt: float = 0.010,
    sequence_state: int = 25,
    manual_mode: bool = True,
) -> PLC:
    """Return a post-entry boundary with an explicit controller-memory image."""

    tags = {
        SequenceState.name: sequence_state,
        ManualModeSwitch.name: manual_mode,
    }
    plc = PLC(logic, initial_state=SystemState().with_tags(tags), dt=dt)
    plc.step()
    return plc
