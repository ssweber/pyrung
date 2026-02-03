from clickplc_dsl import Addresses, Conditions, Actions, Td, Th, Tm, Ts, Tms, Rung

# fmt: off
# Get address references
x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, td, ctd, sd, txt = Addresses.get()

# Get condition functions
nc, rise, fall = Conditions.get()

# Get action functions
out, latch, reset, ton, tof, rton, rtof, ctu, ctd, ctud, copy, copy_block, copy_fill, copy_pack, copy_unpack, shift, search, math, math_hex, call, for_loop, next_loop, end = Actions.get()
# fmt: on


def main():
    with Rung():
        call(PLCDateTime)
    
    # Cumulative Timers
    with Rung():
        ton(t.A_ModeTimeCurrent_tmr, setpoint=0, unit=Th, elapsed_time=td.A_ModeTimeCurrent_Th)
        ton(t.A_StateTimeCurrent_tmr, setpoint=0, unit=Tm, elapsed_time=td.A_StateTimeCurrent_Tm)

    # Mode Change
    with Rung(c.C_UnitModeChgRequest):
        copy(1, ds.C_UnitModeChgRequest_ds)

    with Rung(ds.C_UnitModeChgRequest_ds == 1):
        call(ModeChange)
        out(c.S_UnitModeRequested)

    # `State Complete` State Change
    with Rung(ds.S_StateComplete_ds == 1):
        out(c.S_StateComplete)
    with Rung(c.S_StateComplete):
        call(sm_StateComplete2Request)

    # `Control Command` State Change
    with Rung():
        call(sm_MapCmd2Val)

    with Rung(c.C_CmdChgRequest):
        copy(1, ds.C_CmdChgRequest_ds, oneshot=True)

    with Rung(c.C_CmdChgRequest):
        copy(1, ds.C_CmdChgRequest_ds, True)

    with Rung(ds.C_CmdChgRequest_ds == 1, ds.C_CntrCmd >= 1, ds.C_CntrCmd <= 10):
        call(sm_IsCmdValid)
        reset(c.C_CmdChgRequest)

    with Rung(c.IsCmdValid_Yes):
        copy(0, ds.C_CmdChgRequest_ds)
        reset(c.IsCmdValid_Yes)
        call(sm_CtrlCmd2StateRequest)

    with Rung(ds.C_CtrlCmd != 0, ds.C_CmdChgRequest_ds != 0):
        copy(0, ds.C_CtrlCmd)
        copy(0, ds.C_CmdChgRequest_ds)

    # Finalize Either State Change Request

    with Rung(ds.S_StateRequested != 0):
        copy(0, ds.sm__loopindex, oneshot=True)
        out(c.S_StateChgInProcess)
        call(sm_CopyOrJumpReqState)

    with Rung():
        call(sm_MapVal2State)

    with Rung(c.S_UnitModeRequested):
        copy(0, ds.C_UnitModeChgRequest_ds)
        
    # Recipe Change
    with Rung(rise(c.C_RecipeChgRequest),any([c.S_Idle, c.S_Stopped, c.S_Aborted]),
        ds.C_UnitMode >= 1,
        ds.C_UnitMode <= 3,
    ),ds.C_SelectedRecipe >= 1, ds.C_SelectedRecipe <= 10:
        out(c.S_RecipeChgInProcess)
        
    with Rung(c.S_RecipeChgInProcess):
        call(RecipeChange)

    # AckAlarms (temporary)

    with Rung(ds.C_AckAlarms == 1):
        copy(1, ds.almhis__idx, oneshot=True)
        call(AlarmHistory)

    with Rung():
        end()

def PLCDateTime():
    with Rung(sc._1st_SCAN):
        math(sd.get("_RTC_Year(4 digits)") * 10000 + sd.get("_RTC_Month") * 100 + sd.get("_RTC_Day"), dd.A_FirstScap_YYMMDD ) # dd[19]
    with Rung(sc._1st_SCAN):
        math(sd.get("_RTC_Hour") * 10000 + sd.get("_RTC_Minute") * 100 + sd.get("_RTC_Second>"), dd.A_FirstScap_hhmmss) # dd[20]
    with Rung():
        math(sd.get("_RTC_Year(4 digits)") * 10000 + sd.get("_RTC_Month") * 100 + sd.get("_RTC_Day"), dd.A_PLCDT_YYMMDD) # dd[17]
    with Rung():
        math(sd.get("_RTC_Hour") * 10000 + sd.get("_RTC_Minute") * 100 + sd.get("_RTC_Second>"), dd.A_PLCDT_hhmmss) # dd[18]
    with Rung():
        copy(ds[19], dd.A_PLCDT_Year) # dd[11]
        copy(ds[21], dd.A_PLCDT_Month) # dd[12]
        copy(ds[22], dd.A_PLCDT_Day) # dd[13]
        copy(ds[24], dd.A_PLCDT_Hour) # dd[14]
        copy(ds[25], dd.A_PLCDT_Minute) # dd[15]
        copy(ds[26], dd.A_PLCDT_Second) # dd[16]
        
def RecipeChange():
    
    if ds.S_RecipeRequested == 1:
        copy_block(ds[2501:2550], ds[51:100])
    if ds.S_RecipeRequested == 2:
        copy_block(ds[2551:2600], ds[51:100])
    if ds.S_RecipeRequested == 3:
        copy_block(ds[2601:2650], ds[51:100])
    if ds.S_RecipeRequested == 4:
        copy_block(ds[2651:2700], ds[51:100])
    if ds.S_RecipeRequested == 5:
        copy_block(ds[2701:2750], ds[51:100])
    if ds.S_RecipeRequested == 6:
        copy_block(ds[2751:2800], ds[51:100])
    if ds.S_RecipeRequested == 7:
        copy_block(ds[2801:2850], ds[51:100])
    if ds.S_RecipeRequested == 8:
        copy_block(ds[2851:2900], ds[51:100])
    if ds.S_RecipeRequested == 9:
        copy_block(ds[2901:2950], ds[51:100])
    if ds.S_RecipeRequested == 10:
        copy_block(ds[2951:3000], ds[51:100])

def sm_ExampleAlarmRecording():
    pass
    # A_StopReason_ID    DD  DD21
    # A_StopReason_SubID DD  DD22
    # A_StopReason_StepID DD  DD23
    # A_StopReason_Value DD  DD24
    # A_StopReason_Categor DD DD25
    # A_StopReason_Date  DD  DD26
    # A_StopReason_Time  DD  DD27
    # A_StopReason_AckDate DD DD28
    # A_StopReason_AckTime DD DD29
    # DD101-DD200        A_Alm[#]_  ID,SubID,StepID,Value,Cat,Date,Time,AckDate,AckTime,None  Alarm group tags, 10 tags per group

    # Example of how to record an alarm
    #
    # First check if Stop Reason is not already set
    # with Rung(dd.A_StopReason_ID == 0):
    #     # Now copy values into Stop Reason
    #     copy(1, dd.A_StopReason_ID)
    #     copy(2, dd.A_StopReason_SubID)
    #     copy(3, dd.A_StopReason_StepID)
    #     copy(4, dd.A_StopReason_Value)
    #     copy(5, dd.A_StopReason_Categor)
    #     copy(df.now_YYMMDD, dd.A_StopReason_Date)
    #     copy(df.now_HHMMSS, dd.A_StopReason_Time)

    # Also move active alarm to the next alarm in the group
    # with Rung():
    # copy_block(dd[101:190], dd[111:200]) # move active alarms down one
    # with Rung():
    #     copy(1, dd.Alarm1_ID)
    #     copy(2, dd.Alarm1_SubID)
    #     copy(3, dd.Alarm1_StepID)
    #     copy(4, dd.Alarm1_Value)
    #     copy(5, dd.Alarm1_Categor)
    #     copy(df.now_YYMMDD, dd.Alarm1_Date)
    #     copy(df.now_HHMMSS, dd.Alarm1_Time)


def Copy2AlmHis():
    # DD501-DD1000 for AlmHist[#] records (10 values per alarm, 50 alarms in history)
    # DD101-DD200 for current Alm[#] records (10 values per alarm, 10 active alarms)

    with Rung():
        ton(t.almhis__tmr, setpoint=0, unit=Tms, elapsed_time=td.almhis__t_Tms)

    # Example Alm1_Id (then 2, 3, etc)
    with Rung():
        math(lambda: (ds.almhis__idx * 10) + 91, ds.almhis__start_idx)
    with Rung():
        copy(dd[ds.almhis__start_idx], dd.almhis__is_alm)

    # if alarm
    with Rung(dd.almhis__is_alm != 0):
        copy_block(dd[501:990], dd[511:1000])  # shift alarm history down one slot
    with Rung(dd.almhis__is_alm != 0):
        # Handle all 10 possible cases with separate if statements
        if ds.almhis__idx == 1:
            copy_block(dd[101:110], dd[501:510])
        if ds.almhis__idx == 2:
            copy_block(dd[111:120], dd[501:510])
        if ds.almhis__idx == 3:
            copy_block(dd[121:130], dd[501:510])
        if ds.almhis__idx == 4:
            copy_block(dd[131:140], dd[501:510])
        if ds.almhis__idx == 5:
            copy_block(dd[141:150], dd[501:510])
        if ds.almhis__idx == 6:
            copy_block(dd[151:160], dd[501:510])
        if ds.almhis__idx == 7:
            copy_block(dd[161:170], dd[501:510])
        if ds.almhis__idx == 8:
            copy_block(dd[171:180], dd[501:510])
        if ds.almhis__idx == 9:
            copy_block(dd[181:190], dd[501:510])
        if ds.almhis__idx == 10:
            copy_block(dd[191:200], dd[501:510])
    with Rung(dd.almhis__is_alm != 0):
        copy(dd.now_YYMMDD, dd.AlmHist1_Date)  # dd[508]
        copy(dd.now_HHMMSS, dd.AlmHist1_Time)  # dd[509]

    with Rung():
        math(lambda: ds.almhis__idx + 1, ds.almhis__idx)
        
    with Rung(ds.almhis__idx > 10):
        copy(0, ds.C_AckAlarms)

    with Rung():
        return


def ModeChange():
    with Rung(c.C_ProductionMode):
        copy(1, ds.C_UnitMode, oneshot=True)
    with Rung(c.C_MaintenanceMode):
        copy(2, ds.C_UnitMode, oneshot=True)
    with Rung(c.C_ManualMode):
        copy(3, ds.C_UnitMode, oneshot=True)
    with Rung(ds.C_UnitMode != 0):
        reset(c[1004:1006])  # c.C_ProductionMode:c.C_ManualMode

    with Rung(
        any([c.S_Idle, c.S_Stopped, c.S_Aborted]),
        ds.C_UnitMode >= 1,
        ds.C_UnitMode <= 3,
    ):
        copy(ds.C_UnitMode, ds.S_UnitModeCurrent)
        copy(0, td.A_ModeTimeCurrent_Th)

    # Get the current mode's disabled states configuration
    # Mode configs are stored in DF101-DF104
    with Rung():
        math(lambda: 200 + ds.S_UnitModeCurrent, ds.IsStateEnbl__modecfg_idx)
    with Rung():
        copy(df[ds.IsStateEnbl__modecfg_idx], dh.A_CurDisabledStates)
    with Rung():
        copy(0, ds.C_UnitModeChgRequest_ds)
        copy(0, ds.C_UnitMode)
        reset(c.C_UnitModeChgRequest)
    with Rung():
        return


# State Allowed Masks:
# 0  UNDEFINED    3FF - Allowed:
# 1  CLEARING     37F - Allowed: ABORT
# 2  STOPPED      37E - Allowed: RESET, ABORT
# 3  STARTING     37B - Allowed: STOP, ABORT
# 4  IDLE         379 - Allowed: START, STOP, ABORT
# 5  SUSPENDED    133 - Allowed: STOP, HOLD, UNSUSPEND, ABORT, COMPLETE
# 6  EXECUTE      153 - Allowed: STOP, HOLD, SUSPEND, ABORT, COMPLETE
# 7  STOPPING     37F - Allowed: ABORT
# 8  ABORTING     3FF - Allowed:
# 9  ABORTED      2FF - Allowed: CLEAR
# 10 HOLDING      37B - Allowed: STOP, ABORT
# 11 HELD         16B - Allowed: STOP, UNHOLD, ABORT, COMPLETE
# 12 UNHOLDING    37B - Allowed: STOP, ABORT
# 13 SUSPENDING   37B - Allowed: STOP, ABORT
# 14 UNSUSPENDING 37B - Allowed: STOP, ABORT
# 15 RESETTING    37B - Allowed: STOP, ABORT
# 16 COMPLETING   37B - Allowed: STOP, ABORT
# 17 COMPLETED    37A - Allowed: RESET, STOP, ABORT

# Command Bit Values:
# 1  RESET     : 001
# 2  START     : 002
# 3  STOP      : 004
# 4  HOLD      : 008
# 5  UNHOLD    : 010
# 6  SUSPEND   : 020
# 7  UNSUSPEND : 040
# 8  ABORT     : 080
# 9  CLEAR     : 100
# 10 COMPLETE  : 200

# ds = int16
# dd = int32
# dh = hex16 (for bitmask)


def sm_IsCmdValid():
    # set base to use as pointer
    with Rung():
        math(lambda: 100 + ds.C_CntrlCmd, ds.IsCmdValid__dh_base)
    with Rung():
        copy(dh[ds.IsCmdValid__dh_base], dh.IsCmdValid__cmd)
    with Rung():
        copy(dh[ds.S_StateCurrent], dh.IsCmdValid__allowed_mask)
    with Rung():
        math_hex(
            lambda: dh.IsCmdValid__cmd & dh.IsCmdValid__allowed_mask,
            dh.IsCmdValid__result,
        )

    with Rung(dh.IsCmdValid__result == "0000h"):
        out(c.IsCmdValid_Yes)

    with Rung():
        return


def sm_MapCmd2Val():
    # Map int values to coils
    with Rung(c.C_Reset):
        copy(1, ds.C_CntrlCmd, oneshot=True)
    with Rung(c.C_Start):
        copy(2, ds.C_CntrlCmd, oneshot=True)
    with Rung(c.C_Stop):
        copy(3, ds.C_CntrlCmd, oneshot=True)
    with Rung(c.C_Hold):
        copy(4, ds.C_CntrlCmd, oneshot=True)
    with Rung(c.C_Unhold):
        copy(5, ds.C_CntrlCmd, oneshot=True)
    with Rung(c.C_Suspend):
        copy(6, ds.C_CntrlCmd, oneshot=True)
    with Rung(c.C_Unsuspend):
        copy(7, ds.C_CntrlCmd, oneshot=True)
    with Rung(c.C_Abort):
        copy(8, ds.C_CntrlCmd, oneshot=True)
    with Rung(c.C_Clear):
        copy(9, ds.C_CntrlCmd, oneshot=True)
    with Rung(c.C_Complete):
        copy(10, ds.C_CntrlCmd, oneshot=True)

    # reset bits
    with Rung():
        reset(c[1007:1016])  # c.C_Reset:c.C_Complete


def sm_CtrlCmd2StateRequest():
    # THIS ORDER TO MATCH DIAGRAMIN ON PG 27 of ISA-TR88.00.02-2022

    # Start
    with Rung(ds.C_CntrlCmd == 2, c.S_Idle):
        copy(ds.sm_RefStarting, ds.S_StateRequested)

    # Reset
    with Rung(
        ds.C_CntrlCmd == 1,
        any([c.S_Completed, c.S_Stopped]),
    ):
        copy(ds.sm_RefResetting, ds.S_StateRequested)

    # Hold
    with Rung(
        ds.C_CntrlCmd == 4,
        any([c.S_Execute, c.S_Suspended]),
    ):
        copy(ds.sm_RefHolding, ds.S_StateRequested)

    # Unhold
    with Rung(ds.C_CntrlCmd == 5, c.S_Held):
        copy(ds.sm_RefUnholding, ds.S_StateRequested)

    # Suspend
    with Rung(ds.C_CntrlCmd == 6, c.S_Execute):
        copy(ds.sm_RefSuspending, ds.S_StateRequested)

    # Unsuspend
    with Rung(ds.C_CntrlCmd == 7, c.S_Suspended):
        copy(ds.sm_RefUnsuspending, ds.S_StateRequested)

    # Complete
    with Rung(
        ds.C_CntrlCmd == 10,
        any([c.S_Execute, c.S_Held, c.S_Suspended]),
    ):
        copy(ds.sm_RefCompleting, ds.S_StateRequested)

    # Clear
    with Rung(ds.C_CntrlCmd == 9, c.S_Aborted):
        copy(ds.sm_RefClearing, ds.S_StateRequested)

    # Stop
    with Rung(
        ds.C_CntrlCmd == 3,
        any(
            [
                c.S_Idle,
                c.S_Starting,
                c.S_Execute,
                c.S_Completing,
                c.S_Completed,
                c.S_Resetting,
                c.S_Holding,
                c.S_Held,
                c.S_Unholding,
                c.S_Suspending,
                c.S_Unsuspending,
            ]
        ),
    ):
        copy(ds.sm_RefStopping, ds.S_StateRequested)

    # Abort
    with Rung(
        ds.C_CntrlCmd == 8,
        any(
            [
                c.S_Idle,
                c.S_Starting,
                c.S_Execute,
                c.S_Completing,
                c.S_Completed,
                c.S_Resetting,
                c.S_Holding,
                c.S_Held,
                c.S_Unholding,
                c.S_Suspending,
                c.S_Unsuspending,
                c.S_Stopping,
                c.S_Stopped,
                c.S_Clearing,
            ]
        ),
    ):
        copy(ds.sm_RefAborting, ds.S_StateRequested)

    with Rung():
        reset(c.IsCmdValid_Yes)

    with Rung():
        return


def sm_StateComplete2Request():
    # THIS ORDER TO MATCH DIAGRAMIN ON PG 27 of ISA-TR88.00.02-2022

    # Now for StateComplete
    with Rung(c.S_Starting):
        copy(ds.sm_RefExecute, ds.S_StateRequested)

    with Rung(c.S_Completing):
        copy(ds.sm_RefCompleted, ds.S_StateRequested)

    with Rung(c.S_Resetting):
        copy(ds.sm_RefIdle, ds.S_StateRequested)

    with Rung(c.S_Holding):
        copy(ds.sm_RefHeld, ds.S_StateRequested)

    with Rung(c.S_Unholding):
        copy(ds.sm_RefExecute, ds.S_StateRequested)

    with Rung(c.S_Suspending):
        copy(ds.sm_RefSuspended, ds.S_StateRequested)

    with Rung(c.S_Unsuspending):
        copy(ds.sm_RefExecute, ds.S_StateRequested)

    with Rung(c.S_Stopping):
        copy(ds.sm_RefStopped, ds.S_StateRequested)

    with Rung(c.S_Aborting):
        copy(ds.sm_RefAborted, ds.S_StateRequested)

    with Rung(c.S_Clearing):
        copy(ds.sm_RefStopped, ds.S_StateRequested)

    with Rung():
        copy(0, ds.S_StateComplete_ds)
        reset(c.S_StateComplete)

    with Rung():
        return


def sm_CopyOrJumpReqState():
    # State has been requested
    # Now we look at currently disabled states and see if we need to jump
    with Rung():
        math(lambda: ds.sm__loopindex + 1, ds.sm__loopindex)

    with Rung(ds.sm__loopindex > 10):
        copy(9, ds.S_StateRequested)  # goto Aborted state

    # Check if the requested state is one of our hardcoded always-enabled states
    # (STOPPED, IDLE, EXECUTE, ABORTED)
    with Rung(
        ds.S_StateRequested == 2,
        ds.S_StateRequested == 4,
        ds.S_StateRequested == 6,
        ds.S_StateRequested == 9,
    ):
        copy(1, ds.IsStateEnbl_Yes)

    # Calculate the bit mask for the requested state
    # We'll store state bit masks in DH301-DH317 (where DH300 is reserved)
    with Rung():
        math(lambda: 300 + ds.S_StateRequested, ds.IsStateEnbl__mask_idx)
    with Rung():
        copy(dh[ds.IsStateEnbl__mask_idx], dh.IsStateEnbl__statemask)

    # Check if state is disabled by ANDing the state mask with mode config
    # If result is non-zero, state is disabled
    with Rung():
        math_hex(
            lambda: dh.IsStateEnbl__statemask & dh.A_CurDisabledStates,
            dh.IsStateEnbl__result,
        )

    # If result is zero, the bit is not set in mode config, meaning state is enabled
    with Rung(dh.IsStateEnbl__result == "0000h"):
        copy(1, ds.IsStateEnbl_Yes)

    # if the requested state is not disabled, set it
    with Rung(ds.IsStateEnbl_Yes == 1):
        copy(ds.S_StateRequested, ds.S_StateCurrent)
        copy(0, ds.S_StateComplete_ds)
        copy(0, ds.S_StateRequested)
        copy(0, ds.IsStateEnbl_Yes)
        copy(0, td.A_StateTimeCurrent_Tm)
        return

    # if not
    with Rung():
        math(lambda: ds.S_StateRequested + 120, ds.sm__jump_target_ds_idx)
    with Rung():
        copy(ds[ds.sm__jump_target_ds_idx], ds.sm__where2jump)
    with Rung(ds.sm__where2jump != 0):
        copy(ds.sm__where2jump, ds.S_StateRequested)
    # subroutine will be called successful jump or 10 > loops

    with Rung():
        return


def sm_MapVal2State():
    # write out outputs
    with Rung(ds.S_StateCurrent == 1):
        out(c.S_Clearing)
    with Rung(ds.S_StateCurrent == 2):
        out(c.S_Stopped)
    with Rung(ds.S_StateCurrent == 3):
        out(c.S_Starting)
    with Rung(ds.S_StateCurrent == 4):
        out(c.S_Idle)
    with Rung(ds.S_StateCurrent == 5):
        out(c.S_Suspended)
    with Rung(ds.S_StateCurrent == 6):
        out(c.S_Execute)
    with Rung(ds.S_StateCurrent == 7):
        out(c.S_Stopping)
    with Rung(ds.S_StateCurrent == 8):
        out(c.S_Aborting)
    with Rung(ds.S_StateCurrent == 9):
        out(c.S_Aborted)
    with Rung(ds.S_StateCurrent == 10):
        out(c.S_Holding)
    with Rung(ds.S_StateCurrent == 11):
        out(c.S_Held)
    with Rung(ds.S_StateCurrent == 12):
        out(c.S_Unholding)
    with Rung(ds.S_StateCurrent == 13):
        out(c.S_Suspending)
    with Rung(ds.S_StateCurrent == 14):
        out(c.S_Unsuspending)
    with Rung(ds.S_StateCurrent == 15):
        out(c.S_Resetting)
    with Rung(ds.S_StateCurrent == 16):
        out(c.S_Completing)
    with Rung(ds.S_StateCurrent == 17):
        out(c.S_Completed)
    with Rung():
        return
