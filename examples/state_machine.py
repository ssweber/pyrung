from clickplc_dsl import Addresses, Conditions, Actions, Td, Th, Tm, Ts, Tms, sub

# fmt: off
# Get address references
x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, td, ctd, sd, txt = Addresses.get()

# Get condition functions
nc, re, fe, all, any = Conditions.get()

# Get action functions
out, set, reset, ton, tof, rton, rtof, ctu, ctd, ctud, copy, copy_block, copy_fill, copy_pack, copy_unpack, shift, search, math_decimal, math_hex, call, next_loop, end = Actions.get()
# fmt: on


def main():
    """Main program that calls the SFC subroutine when triggered."""
    with Rung(ds.S_StateComplete_ds == 1):
        out(c.S_StateComplete)
    with Rung(ds.C_CmdChgRequest_ds == 1):
        out(c.C_CmdChgRequest)
    with Rung(c.C_CmdChgRequest, ds.C_CntrCmd >= 1, ds.C_CntrCmd <= 10):
        call(sm_isCmdValid)

    with Rung(any([c.isCmdValid_Yes, c.S_StateComplete])):
        copy(0, ds.C_CmdChgRequest_ds)
        reset(c.isCmdValid_Yes)
        call(sm_setStateRequested)

    with Rung(ds.S_StateRequested != 0):
        copy(0, ds.sm__loopindex, oneshot=True)
        call(sm_copyOrJumpReqState)

    with Rung():
        call(sm_mapVal2State)

    with Rung(ds.C_UnitModeChgRequest_ds == 1):
        out(c.C_UnitModeChgRequest)
        out(c.S_UnitModeRequested)

    with Rung(c.S_UnitModeRequested):
        call(sm_ModeChange)

    with Rung(c.S_UnitModeRequested):
        copy(0, ds.C_UnitModeChgRequest_ds)

    with Rung():
        end()


@sub
def sm_ModeChange():
    with Rung(
        any([c.S_Idle, c.S_Stopped, C.S_Aborted]),
        ds.C_UnitMode >= 1,
        ds.C_UnitMode <= 3,
    ):
        copy(ds.C_UnitMode, ds.S_UnitModeCurrent)

    # Get the current mode's disabled states configuration
    # Mode configs are stored in DF101-DF104
    with Rung():
        math_decimal(lambda: 200 + ds.S_UnitModeCurrent, ds.isStateEnbl__modecfg_idx)
    with Rung():
        copy(df[ds.isStateEnbl__modecfg_idx], dh.A_CurDisabledStates)
    with Rung():
        copy(0, ds.C_UnitModeChgRequest_ds)
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


@sub
def sm_isCmdValid():
    # set base to use as pointer
    with Rung():
        math_decimal(lambda: 100 + ds.C_CntrlCmd, ds.isCmdValid__dh_base)
    with Rung():
        copy(dh[ds.isCmdValid__dh_base], dh.isCmdValid__cmd)
    with Rung():
        copy(dh[ds.S_StateCurrent], dh.isCmdValid__allowed_mask)
    with Rung():
        math_hex(
            lambda: dh.isCmdValid__cmd & dh.isCmdValid__allowed_mask,
            dh.isCmdValid__result,
        )

    with Rung(dh.isCmdValid__result == dh.CONSTANT_HEX_ZERO):
        out(c.isCmdValid_Yes)

    with Rung():
        return


@sub
def sm_setStateRequested():
    # Map int values to coils
    with Rung(ds.C_CntrlCmd == 1):
        out(c.C_Reset)
    with Rung(ds.C_CntrlCmd == 2):
        out(c.C_Start)
    with Rung(ds.C_CntrlCmd == 3):
        out(c.C_Stop)
    with Rung(ds.C_CntrlCmd == 4):
        out(c.C_Hold)
    with Rung(ds.C_CntrlCmd == 5):
        out(c.C_Unhold)
    with Rung(ds.C_CntrlCmd == 6):
        out(c.C_Suspend)
    with Rung(ds.C_CntrlCmd == 7):
        out(c.C_Unsuspend)
    with Rung(ds.C_CntrlCmd == 8):
        out(c.C_Abort)
    with Rung(ds.C_CntrlCmd == 9):
        out(c.C_Clear)
    with Rung(ds.C_CntrlCmd == 10):
        out(c.C_Complete)

    # now based on the command & current state, copy appropriate S_StateRequested
    with Rung(c.C_Start, c.S_Idle):
        copy(ds.sm_RefStarting, ds.S_StateRequested)

    with Rung(
        c.C_Reset,
        any([c.S_Completed, c.S_Stopped]),
    ):
        copy(ds.sm_RefResetting, ds.S_StateRequested)

    with Rung(
        c.C_Hold,
        any([c.S_Execute, c.S_Suspended]),
    ):
        copy(ds.sm_RefHolding, ds.S_StateRequested)

    with Rung(c.C_Unhold, c.S_Held):
        copy(ds.sm_RefUnholding, ds.S_StateRequested)

    with Rung(c.C_Suspend, c.S_Execute):
        copy(ds.sm_RefSuspending, ds.S_StateRequested)

    with Rung(c.C_Unsuspend, c.S_Suspended):
        copy(ds.sm_RefUnsuspending, ds.S_StateRequested)

    with Rung(
        c.C_Complete,
        any([c.S_Execute, c.S_Held, c.S_Suspended]),
    ):
        copy(ds.sm_RefCompleting, ds.S_StateRequested)

    with Rung(c.C_Clear, c.S_Aborted):
        copy(ds.sm_RefClearing, ds.S_StateRequested)

    with Rung(
        c.C_Stop,
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

    with Rung(
        c.C_Abort,
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

    # Now for StateComplete
    with Rung(c.S_StateComplete):
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
        copy(0, ds.C_CmdChgRequest_ds)
        reset(c.isCmdValid_Yes)

    with Rung():
        return


@sub
def sm_copyOrJumpReqState():
    # State has been requested
    # Now we look at currently disabled states and see if we need to jump
    with Rung():
        math_decimal(lambda: ds.sm__loopindex + 1, ds.sm__loopindex)

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
        copy(1, ds.isStateEnbl_Yes)

    # Calculate the bit mask for the requested state
    # We'll store state bit masks in DH301-DH317 (where DH300 is reserved)
    with Rung():
        math_decimal(lambda: 300 + ds.S_StateRequested, ds.isStateEnbl__mask_idx)
    with Rung():
        copy(dh[ds.isStateEnbl__mask_idx], dh.isStateEnbl__statemask)

    # Check if state is disabled by ANDing the state mask with mode config
    # If result is non-zero, state is disabled
    with Rung():
        math_hex(
            lambda: dh.isStateEnbl__statemask & dh.A_CurDisabledStates,
            dh.isStateEnbl__result,
        )

    # If result is zero, the bit is not set in mode config, meaning state is enabled
    with Rung(dh.isStateEnbl__result == dh.CONSTANT_HEX_ZERO):
        copy(1, ds.isStateEnbl_Yes)

    # if the requested state is not disabled, set it
    with Rung(ds.isStateEnbl_Yes == 1):
        copy(ds.S_StateRequested, ds.S_StateCurrent)
        copy(0, ds.S_StateComplete_ds)
        copy(0, ds.S_StateRequested)
        copy(0, ds.isStateEnbl_Yes)
        return

    # if not
    with Rung():
        math_decimal(lambda: ds.S_StateRequested + 120, ds.sm__jump_target_ds_idx)
    with Rung():
        copy(ds[ds.sm__jump_target_ds_idx], ds.sm__where2jump)
    with Rung(ds.sm__where2jump != 0):
        copy(ds.sm__where2jump, ds.S_StateRequested)
    # subroutine will be called successful jump or 10 > loops

    with Rung():
        return


@sub
def sm_mapVal2State():
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
