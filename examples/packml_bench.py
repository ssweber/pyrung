"""PackML-style lock benchmark focused on indirect Click block access.

This example is intentionally pointer-heavy. It mirrors the real template's
state-machine lookup patterns so ``pyrung lock`` profiles spend meaningful time
in block synchronization for indirect ``dh[]`` / ``ds[]`` access.
"""

from pyrung import (
    Bool,
    BoolBlock,
    Int,
    IntBlock,
    Or,
    rung,
    Timer,
    Word,
    calc,
    call,
    copy,
    fall,
    fill,
    latch,
    named_array,
    on_delay,
    out,
    program,
    reset,
    return_early,
    rise,
    subroutine,
)
from pyrung.click import TagMap, c, dh, ds, t, td, x

BOOL_CHOICES = {0: "False", 1: "True"}
MODE_CHOICES = {0: "Undefined", 1: "Production", 2: "Maintenance", 3: "Manual"}
CMD_CHOICES = {
    0: "Undefined",
    1: "Reset",
    2: "Start",
    3: "Stop",
    4: "Hold",
    5: "Unhold",
    6: "Suspend",
    7: "Unsuspend",
    8: "Abort",
    9: "Clear",
    10: "Complete",
}


@named_array(Int, stride=17, readonly=True)
class S:
    CLEARING = 1
    STOPPED = 2
    STARTING = 3
    IDLE = 4
    SUSPENDED = 5
    EXECUTE = 6
    STOPPING = 7
    ABORTING = 8
    ABORTED = 9
    HOLDING = 10
    HELD = 11
    UNHOLDING = 12
    SUSPENDING = 13
    UNSUSPENDING = 14
    RESETTING = 15
    COMPLETING = 16
    COMPLETED = 17


STATE_CHOICES = {
    1: "CLEARING",
    2: "STOPPED",
    3: "STARTING",
    4: "IDLE",
    5: "SUSPENDED",
    6: "EXECUTE",
    7: "STOPPING",
    8: "ABORTING",
    9: "ABORTED",
    10: "HOLDING",
    11: "HELD",
    12: "UNHOLDING",
    13: "SUSPENDING",
    14: "UNSUSPENDING",
    15: "RESETTING",
    16: "COMPLETING",
    17: "COMPLETED",
}
STATE_REQUEST_CHOICES = {0: "None", **STATE_CHOICES}


CmdChgRequest = Bool(external=True)
ModeChgRequest = Bool(external=True)
Estop = Bool(external=True)
IOModuleError = Bool(external=True)
CommFault = Bool(external=True)

ModeProduction = Bool(external=True)
ModeMaintenance = Bool(external=True)
ModeManual = Bool(external=True)

CmdReset = Bool(external=True)
CmdStart = Bool(external=True)
CmdStop = Bool(external=True)
CmdHold = Bool(external=True)
CmdUnhold = Bool(external=True)
CmdSuspend = Bool(external=True)
CmdUnsuspend = Bool(external=True)
CmdAbort = Bool(external=True)
CmdClear = Bool(external=True)
CmdComplete = Bool(external=True)

StateCurrent = Int(choices=STATE_CHOICES, lock=True)
StateRequested = Int(choices=STATE_REQUEST_CHOICES, lock=True)
CtrlCmd = Int(choices=CMD_CHOICES, external=True)
StateCompleteBool = Int(choices=BOOL_CHOICES)
UnitModeCurrent = Int(choices=MODE_CHOICES, lock=True)
CmdChgRequestBool = Int(choices=BOOL_CHOICES, external=True)
ModeChgRequestBool = Int(choices=BOOL_CHOICES, external=True)
LoopIndex = Int()
UnitModeCmd = Int(choices=MODE_CHOICES, external=True)

State_Clearing = Bool(lock=True)
State_Stopped = Bool(lock=True)
State_Starting = Bool(lock=True)
State_Idle = Bool(lock=True)
State_Suspended = Bool(lock=True)
State_Execute = Bool(lock=True)
State_Stopping = Bool(lock=True)
State_Aborting = Bool(lock=True)
State_Aborted = Bool(lock=True)
State_Holding = Bool(lock=True)
State_Held = Bool(lock=True)
State_Unholding = Bool(lock=True)
State_Suspending = Bool(lock=True)
State_Unsuspending = Bool(lock=True)
State_Resetting = Bool(lock=True)
State_Completing = Bool(lock=True)
State_Completed = Bool(lock=True)

CmdValidIdx = Int()
CmdMask = Word()
StateAllowMask = Word()
CmdValidResult = Word()
CmdValidYes = Bool()

StateMaskIdx = Int()
StateMask = Word()
DisabledStates = Word()
StateMaskResult = Word()
StateJumpIdx = Int()
StateJumpTarget = Int(choices=STATE_REQUEST_CHOICES)
StateEnableYes = Int(choices=BOOL_CHOICES)
ModeConfigIdx = Int()

AlarmCoil = BoolBlock(1, 8, retentive=True)
AlarmStatus = IntBlock(1, 8, retentive=True)
AlarmExtent = Int(lock=True, band={"ZERO": 0, "POSITIVE": ">0"})
HistorianId = Int()
HistorianBit = Bool()
HistIdIdx = Int()
HistDateIdx = Int()
HistTimeIdx = Int()
HistStatusIdx = Int()
HistorianStatusEcho = Int(lock=True)
LastHistorianId = Int(lock=True)

InitDone = Bool()
TaskStepView = Int(lock=True)

StateTimer = Timer.clone("StateTimer")
TaskTimer = Timer.clone("TaskTimer")


@named_array(Int, stride=20, always_number=True)
class TaskTemplate:
    xCall = 0
    xInit = 0
    xReset = 0
    xPause = 0
    Error = 0
    ErrorStep = 0
    Limit_Enable = 0
    Limit_Ts = 0
    ResetTmr = 0
    Trans = 0
    _CurStep = 0
    _StoredStep = 0
    _x = 0
    _init = 0
    _valstepisodd = 0


Task1 = TaskTemplate.clone("Task")


@subroutine("sm_map_val2_state")
def sm_map_val2_state():
    with rung(StateCurrent == S.CLEARING):
        out(State_Clearing)
    with rung(StateCurrent == S.STOPPED):
        out(State_Stopped)
    with rung(StateCurrent == S.STARTING):
        out(State_Starting)
    with rung(StateCurrent == S.IDLE):
        out(State_Idle)
    with rung(StateCurrent == S.SUSPENDED):
        out(State_Suspended)
    with rung(StateCurrent == S.EXECUTE):
        out(State_Execute)
    with rung(StateCurrent == S.STOPPING):
        out(State_Stopping)
    with rung(StateCurrent == S.ABORTING):
        out(State_Aborting)
    with rung(StateCurrent == S.ABORTED):
        out(State_Aborted)
    with rung(StateCurrent == S.HOLDING):
        out(State_Holding)
    with rung(StateCurrent == S.HELD):
        out(State_Held)
    with rung(StateCurrent == S.UNHOLDING):
        out(State_Unholding)
    with rung(StateCurrent == S.SUSPENDING):
        out(State_Suspending)
    with rung(StateCurrent == S.UNSUSPENDING):
        out(State_Unsuspending)
    with rung(StateCurrent == S.RESETTING):
        out(State_Resetting)
    with rung(StateCurrent == S.COMPLETING):
        out(State_Completing)
    with rung(StateCurrent == S.COMPLETED):
        out(State_Completed)


@subroutine("sm_map_cmd2_val")
def sm_map_cmd2_val():
    with rung():
        copy(0, CtrlCmd)
        copy(0, CmdChgRequestBool)

    with rung(CmdReset):
        copy(1, CtrlCmd)
    with rung(CmdStart):
        copy(2, CtrlCmd)
    with rung(CmdStop):
        copy(3, CtrlCmd)
    with rung(CmdHold):
        copy(4, CtrlCmd)
    with rung(CmdUnhold):
        copy(5, CtrlCmd)
    with rung(CmdSuspend):
        copy(6, CtrlCmd)
    with rung(CmdUnsuspend):
        copy(7, CtrlCmd)
    with rung(CmdAbort):
        copy(8, CtrlCmd)
    with rung(CmdClear):
        copy(9, CtrlCmd)
    with rung(CmdComplete):
        copy(10, CtrlCmd)

    with rung(CtrlCmd != 0):
        copy(1, CmdChgRequestBool)


@subroutine("sm_is_cmd_valid")
def sm_is_cmd_valid():
    with rung():
        calc(100 + CtrlCmd, CmdValidIdx)

    with rung():
        copy(dh[CmdValidIdx], CmdMask)

    with rung():
        copy(dh[StateCurrent], StateAllowMask)

    with rung():
        calc(CmdMask & StateAllowMask, CmdValidResult)

    with rung(CmdValidResult == 0):
        out(CmdValidYes)


@subroutine("sm_ctrl_cmd2_state_request")
def sm_ctrl_cmd2_state_request():
    with rung(CmdValidYes, CtrlCmd == 2, StateCurrent == S.IDLE):
        copy(S.STARTING, StateRequested)
        copy(0, LoopIndex)

    with rung(CmdValidYes, CtrlCmd == 1, Or(StateCurrent == S.COMPLETED, StateCurrent == S.STOPPED)):
        copy(S.RESETTING, StateRequested)
        copy(0, LoopIndex)

    with rung(CmdValidYes, CtrlCmd == 4, Or(StateCurrent == S.EXECUTE, StateCurrent == S.SUSPENDED)):
        copy(S.HOLDING, StateRequested)
        copy(0, LoopIndex)

    with rung(CmdValidYes, CtrlCmd == 5, StateCurrent == S.HELD):
        copy(S.UNHOLDING, StateRequested)
        copy(0, LoopIndex)

    with rung(CmdValidYes, CtrlCmd == 6, StateCurrent == S.EXECUTE):
        copy(S.SUSPENDING, StateRequested)
        copy(0, LoopIndex)

    with rung(CmdValidYes, CtrlCmd == 7, StateCurrent == S.SUSPENDED):
        copy(S.UNSUSPENDING, StateRequested)
        copy(0, LoopIndex)

    with rung(CmdValidYes, CtrlCmd == 10, Or(StateCurrent == S.EXECUTE, StateCurrent == S.HELD, StateCurrent == S.SUSPENDED)):
        copy(S.COMPLETING, StateRequested)
        copy(0, LoopIndex)

    with rung(CmdValidYes, CtrlCmd == 9, StateCurrent == S.ABORTED):
        copy(S.CLEARING, StateRequested)
        copy(0, LoopIndex)

    with rung(CmdValidYes, CtrlCmd == 3, Or(StateCurrent == S.IDLE, StateCurrent == S.STARTING, StateCurrent == S.EXECUTE, StateCurrent == S.COMPLETING, StateCurrent == S.COMPLETED, StateCurrent == S.RESETTING, StateCurrent == S.HOLDING, StateCurrent == S.HELD, StateCurrent == S.UNHOLDING, StateCurrent == S.SUSPENDING, StateCurrent == S.UNSUSPENDING)):
        copy(S.STOPPING, StateRequested)
        copy(0, LoopIndex)

    with rung(CmdValidYes, CtrlCmd == 8, Or(StateCurrent == S.IDLE, StateCurrent == S.STARTING, StateCurrent == S.EXECUTE, StateCurrent == S.COMPLETING, StateCurrent == S.COMPLETED, StateCurrent == S.RESETTING, StateCurrent == S.HOLDING, StateCurrent == S.HELD, StateCurrent == S.UNHOLDING, StateCurrent == S.SUSPENDING, StateCurrent == S.UNSUSPENDING, StateCurrent == S.STOPPING, StateCurrent == S.STOPPED, StateCurrent == S.CLEARING)):
        copy(S.ABORTING, StateRequested)
        copy(0, LoopIndex)


@subroutine("sm_state_complete2_request")
def sm_state_complete2_request():
    with rung(StateCurrent == S.STARTING):
        copy(S.EXECUTE, StateRequested)
        copy(0, LoopIndex)
    with rung(StateCurrent == S.COMPLETING):
        copy(S.COMPLETED, StateRequested)
        copy(0, LoopIndex)
    with rung(StateCurrent == S.RESETTING):
        copy(S.IDLE, StateRequested)
        copy(0, LoopIndex)
    with rung(StateCurrent == S.HOLDING):
        copy(S.HELD, StateRequested)
        copy(0, LoopIndex)
    with rung(StateCurrent == S.UNHOLDING):
        copy(S.EXECUTE, StateRequested)
        copy(0, LoopIndex)
    with rung(StateCurrent == S.SUSPENDING):
        copy(S.SUSPENDED, StateRequested)
        copy(0, LoopIndex)
    with rung(StateCurrent == S.UNSUSPENDING):
        copy(S.EXECUTE, StateRequested)
        copy(0, LoopIndex)
    with rung(StateCurrent == S.STOPPING):
        copy(S.STOPPED, StateRequested)
        copy(0, LoopIndex)
    with rung(StateCurrent == S.ABORTING):
        copy(S.ABORTED, StateRequested)
        copy(0, LoopIndex)
    with rung(StateCurrent == S.CLEARING):
        copy(S.STOPPED, StateRequested)
        copy(0, LoopIndex)


@subroutine("sm_copy_or_jump_state")
def sm_copy_or_jump_state():
    with rung():
        calc(LoopIndex + 1, LoopIndex)

    with rung(LoopIndex > 10):
        copy(S.ABORTED, StateRequested)

    with rung(Or(StateRequested == S.STOPPED, StateRequested == S.IDLE, StateRequested == S.EXECUTE, StateRequested == S.ABORTED)):
        copy(1, StateEnableYes)

    with rung():
        calc(300 + StateRequested, StateMaskIdx)

    with rung():
        copy(dh[StateMaskIdx], StateMask)

    with rung():
        calc(StateMask & DisabledStates, StateMaskResult)

    with rung(StateMaskResult == 0):
        copy(1, StateEnableYes)

    with rung(StateEnableYes == 1):
        copy(StateRequested, StateCurrent)
        copy(0, StateCompleteBool)
        copy(0, StateRequested)
        copy(0, StateEnableYes)
        copy(0, StateTimer.Acc)
        return_early()

    with rung():
        calc(StateRequested + 150, StateJumpIdx)

    with rung():
        copy(ds[StateJumpIdx], StateJumpTarget)

    with rung(StateJumpTarget != 0):
        copy(StateJumpTarget, StateRequested)


@subroutine("mode_change")
def mode_change():
    with rung(ModeProduction):
        copy(1, UnitModeCmd)
    with rung(ModeMaintenance):
        copy(2, UnitModeCmd)
    with rung(ModeManual):
        copy(3, UnitModeCmd)

    with rung(Or(StateCurrent == S.IDLE, StateCurrent == S.STOPPED, StateCurrent == S.ABORTED), UnitModeCmd >= 1, UnitModeCmd <= 3):
        copy(UnitModeCmd, UnitModeCurrent)
        copy(0, StateTimer.Acc)

    with rung():
        calc(200 + UnitModeCurrent, ModeConfigIdx)

    with rung():
        copy(dh[ModeConfigIdx], DisabledStates)

    with rung():
        copy(0, UnitModeCmd)
        copy(0, ModeChgRequestBool)


@subroutine("alm_historian")
def alm_historian():
    with rung(HistorianId < 1):
        copy(0, HistorianId)
        reset(HistorianBit)
        return_early()

    with rung(HistorianId > 4):
        copy(0, HistorianId)
        reset(HistorianBit)
        return_early()

    with rung():
        calc((HistorianId * 10) + 2991, HistIdIdx)
    with rung():
        calc(HistIdIdx + 5, HistDateIdx)
    with rung():
        calc(HistIdIdx + 6, HistTimeIdx)
    with rung():
        calc(HistIdIdx + 9, HistStatusIdx)

    with rung():
        copy(HistorianId, ds[HistIdIdx])
    with rung():
        copy(StateCurrent, ds[HistDateIdx])
    with rung():
        copy(Task1._CurStep, ds[HistTimeIdx])
    with rung(HistorianBit):
        copy(1, ds[HistStatusIdx])
    with rung(~HistorianBit):
        copy(0, ds[HistStatusIdx])
    with rung():
        copy(ds[HistStatusIdx], HistorianStatusEcho)
        copy(HistorianId, LastHistorianId)
        copy(0, HistorianId)
        reset(HistorianBit)


@program
def logic():
    with rung(~InitDone):
        copy(1, UnitModeCurrent)
        copy(S.STOPPED, StateCurrent)
        copy(0, StateRequested)
        copy(0, StateCompleteBool)
        copy(1, Task1.Limit_Enable)
        copy(2, Task1.Limit_Ts)

    with rung(~InitDone):
        copy(0x0000, dh[100])
        copy(0x0001, dh[101])
        copy(0x0002, dh[102])
        copy(0x0004, dh[103])
        copy(0x0008, dh[104])
        copy(0x0010, dh[105])
        copy(0x0020, dh[106])
        copy(0x0040, dh[107])
        copy(0x0080, dh[108])
        copy(0x0100, dh[109])
        copy(0x0200, dh[110])

    with rung(~InitDone):
        copy(0x037F, dh[1])
        copy(0x037E, dh[2])
        copy(0x037B, dh[3])
        copy(0x0379, dh[4])
        copy(0x0133, dh[5])
        copy(0x0153, dh[6])
        copy(0x037F, dh[7])
        copy(0x03FF, dh[8])
        copy(0x02FF, dh[9])
        copy(0x037B, dh[10])
        copy(0x016B, dh[11])
        copy(0x037B, dh[12])
        copy(0x037B, dh[13])
        copy(0x037B, dh[14])
        copy(0x037B, dh[15])
        copy(0x037B, dh[16])
        copy(0x037A, dh[17])

    with rung(~InitDone):
        copy(0x0000, dh[201])
        copy(0x0100, dh[202])
        copy(0x0224, dh[203])

    with rung(~InitDone):
        copy(0x0001, dh[301])
        copy(0x0002, dh[302])
        copy(0x0004, dh[303])
        copy(0x0008, dh[304])
        copy(0x0010, dh[305])
        copy(0x0020, dh[306])
        copy(0x0040, dh[307])
        copy(0x0080, dh[308])
        copy(0x0100, dh[309])
        copy(0x0200, dh[310])
        copy(0x0400, dh[311])
        copy(0x0800, dh[312])
        copy(0x1000, dh[313])
        copy(0x2000, dh[314])
        copy(0x4000, dh[315])
        copy(0x8000, dh[316])
        copy(0x0001, dh[317])

    with rung(~InitDone):
        copy(S.CLEARING, ds[151])
        copy(S.STOPPED, ds[152])
        copy(S.EXECUTE, ds[153])
        copy(S.IDLE, ds[154])
        copy(S.EXECUTE, ds[155])
        copy(S.EXECUTE, ds[156])
        copy(S.STOPPED, ds[157])
        copy(S.ABORTED, ds[158])
        copy(S.CLEARING, ds[159])
        copy(S.HELD, ds[160])
        copy(S.EXECUTE, ds[161])
        copy(S.SUSPENDED, ds[162])
        copy(S.EXECUTE, ds[163])
        copy(S.IDLE, ds[164])
        copy(S.COMPLETED, ds[165])
        copy(S.STOPPED, ds[166])
        copy(S.IDLE, ds[167])

    with rung(~InitDone):
        call(mode_change)

    with rung(~InitDone):
        copy(1, InitDone)

    with rung():
        on_delay(StateTimer, 1, "sec")

    with rung(ModeChgRequest):
        copy(1, ModeChgRequestBool, oneshot=True)

    with rung(ModeChgRequestBool == 1):
        call(mode_change)

    with rung():
        call(sm_map_cmd2_val)

    with rung(rise(CmdChgRequest), CtrlCmd >= 1, CtrlCmd <= 10):
        call(sm_is_cmd_valid)
        call(sm_ctrl_cmd2_state_request)

    with rung(StateCurrent == S.STARTING):
        copy(1, StateCompleteBool)
    with rung(StateCurrent == S.STOPPING):
        copy(1, StateCompleteBool)
    with rung(StateCurrent == S.ABORTING):
        copy(1, StateCompleteBool)
    with rung(StateCurrent == S.HOLDING):
        copy(1, StateCompleteBool)
    with rung(StateCurrent == S.UNHOLDING):
        copy(1, StateCompleteBool)
    with rung(StateCurrent == S.SUSPENDING):
        copy(1, StateCompleteBool)
    with rung(StateCurrent == S.UNSUSPENDING):
        copy(1, StateCompleteBool)
    with rung(StateCurrent == S.RESETTING):
        copy(1, StateCompleteBool)
    with rung(StateCurrent == S.COMPLETING):
        copy(1, StateCompleteBool)

    with rung(StateCompleteBool == 1):
        call(sm_state_complete2_request)

    with rung(StateRequested != 0):
        call(sm_copy_or_jump_state)

    with rung():
        call(sm_map_val2_state)

    with rung():
        copy(0, Task1.xCall)
        copy(0, Task1.xPause)

    with rung(StateCurrent == S.EXECUTE):
        copy(1, Task1.xCall)

    with rung(Or(StateCurrent == S.HELD, StateCurrent == S.SUSPENDED)):
        copy(1, Task1.xPause)

    with rung(Task1.xCall == 1, Task1._CurStep == 0):
        copy(1, Task1._CurStep)
        copy(0, TaskTimer.Acc)

    with rung(Task1.xCall == 1, Task1.xPause == 0):
        on_delay(TaskTimer, 1, "sec")

    with rung(Task1.xCall == 1, Task1.xPause == 0, Task1._CurStep == 1, TaskTimer.Done):
        copy(3, Task1._CurStep)
        copy(0, TaskTimer.Acc)

    with rung(Task1.xCall == 1, Task1.xPause == 0, Task1._CurStep == 3, TaskTimer.Done):
        copy(5, Task1._CurStep)
        copy(0, TaskTimer.Acc)

    with rung(Task1.xCall == 1, Task1.xPause == 0, Task1._CurStep == 5, TaskTimer.Done):
        copy(1, StateCompleteBool)
        copy(0, Task1._CurStep)
        copy(0, TaskTimer.Acc)

    with rung():
        copy(Task1._CurStep, TaskStepView)

    with rung(Estop):
        latch(AlarmCoil[1])
        copy(1, AlarmStatus[1])

    with rung(IOModuleError):
        latch(AlarmCoil[2])
        copy(1, AlarmStatus[2])

    with rung(CommFault):
        latch(AlarmCoil[3])
        copy(1, AlarmStatus[3])

    with rung(Task1.xCall == 1, TaskTimer.Acc > Task1.Limit_Ts):
        latch(AlarmCoil[4])
        copy(1, AlarmStatus[4])

    with rung(Or(rise(AlarmCoil[1]), fall(AlarmCoil[1]))):
        copy(1, HistorianId)
        out(HistorianBit)
        call(alm_historian)

    with rung(Or(rise(AlarmCoil[2]), fall(AlarmCoil[2]))):
        copy(2, HistorianId)
        out(HistorianBit)
        call(alm_historian)

    with rung(Or(rise(AlarmCoil[3]), fall(AlarmCoil[3]))):
        copy(3, HistorianId)
        out(HistorianBit)
        call(alm_historian)

    with rung(Or(rise(AlarmCoil[4]), fall(AlarmCoil[4]))):
        copy(4, HistorianId)
        out(HistorianBit)
        call(alm_historian)

    with rung():
        calc(AlarmStatus.select(1, 4).sum(), AlarmExtent)

    with rung(StateCurrent == S.CLEARING):
        fill(0, AlarmCoil.select(1, 8))
        fill(0, AlarmStatus.select(1, 8))
        copy(1, StateCompleteBool)



mapping = TagMap(
    [
        StateCurrent.map_to(ds[1]),
        StateRequested.map_to(ds[2]),
        CtrlCmd.map_to(ds[3]),
        StateCompleteBool.map_to(ds[4]),
        UnitModeCurrent.map_to(ds[5]),
        CmdChgRequestBool.map_to(ds[6]),
        LoopIndex.map_to(ds[7]),
        UnitModeCmd.map_to(ds[8]),
        ModeChgRequestBool.map_to(ds[9]),
        CmdValidIdx.map_to(ds[11]),
        StateMaskIdx.map_to(ds[12]),
        StateJumpIdx.map_to(ds[13]),
        StateJumpTarget.map_to(ds[14]),
        StateEnableYes.map_to(ds[15]),
        ModeConfigIdx.map_to(ds[16]),
        AlarmExtent.map_to(ds[209]),
        HistorianId.map_to(ds[181]),
        HistIdIdx.map_to(ds[171]),
        HistDateIdx.map_to(ds[176]),
        HistTimeIdx.map_to(ds[177]),
        HistStatusIdx.map_to(ds[180]),
        HistorianStatusEcho.map_to(ds[190]),
        LastHistorianId.map_to(ds[191]),
        TaskStepView.map_to(ds[192]),
        InitDone.map_to(c[18]),
        State_Clearing.map_to(c[1]),
        State_Stopped.map_to(c[2]),
        State_Starting.map_to(c[3]),
        State_Idle.map_to(c[4]),
        State_Suspended.map_to(c[5]),
        State_Execute.map_to(c[6]),
        State_Stopping.map_to(c[7]),
        State_Aborting.map_to(c[8]),
        State_Aborted.map_to(c[9]),
        State_Holding.map_to(c[10]),
        State_Held.map_to(c[11]),
        State_Unholding.map_to(c[12]),
        State_Suspending.map_to(c[13]),
        State_Unsuspending.map_to(c[14]),
        State_Resetting.map_to(c[15]),
        State_Completing.map_to(c[16]),
        State_Completed.map_to(c[17]),
        CmdValidYes.map_to(c[51]),
        CmdMask.map_to(dh[51]),
        StateAllowMask.map_to(dh[52]),
        CmdValidResult.map_to(dh[53]),
        StateMask.map_to(dh[61]),
        DisabledStates.map_to(dh[62]),
        StateMaskResult.map_to(dh[63]),
        AlarmCoil.map_to(c.select(101, 108)),
        AlarmStatus.map_to(ds.select(201, 208)),
        CmdChgRequest.map_to(x[1]),
        ModeChgRequest.map_to(x[2]),
        Estop.map_to(x[3]),
        IOModuleError.map_to(x[4]),
        CommFault.map_to(x[5]),
        CmdReset.map_to(x[6]),
        CmdStart.map_to(x[7]),
        CmdStop.map_to(x[8]),
        CmdHold.map_to(x[9]),
        CmdUnhold.map_to(x[10]),
        CmdSuspend.map_to(x[11]),
        CmdUnsuspend.map_to(x[12]),
        CmdAbort.map_to(x[13]),
        CmdClear.map_to(x[14]),
        CmdComplete.map_to(x[15]),
        ModeProduction.map_to(x[101]),
        ModeMaintenance.map_to(x[102]),
        ModeManual.map_to(x[103]),
        *Task1.map_to(ds.select(501, 520)),
        StateTimer.Done.map_to(t[1]),
        StateTimer.Acc.map_to(td[1]),
        TaskTimer.Done.map_to(t[2]),
        TaskTimer.Acc.map_to(td[2]),
    ],
    include_system=False,
)
