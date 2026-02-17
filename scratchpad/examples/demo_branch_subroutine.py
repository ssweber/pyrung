# scratchpad/examples/demo_branch_subroutine.py
import os

from pyrung.core import (
    Block,
    Bool,
    Int,
    PLCRunner,
    Program,
    Rung,
    TagType,
    branch,
    call,
    copy,
    count_up,
    out,
    return_,
    rise,
    subroutine,
    all_of,
    nc,
)

Step = Int("Step")
CurStep = Int("CurStep")
DebugStep = Int("DebugStep")
StepData = Block(
    "StepData",
    TagType.INT,
    0,
    16,
    address_formatter=lambda name, addr: f"{name}[{addr}]",
)
AutoMode = Bool("AutoMode")
MainLight = Bool("MainLight")
AutoLight = Bool("AutoLight")
SubLight = Bool("SubLight")
SkippedAfterReturn = Bool("SkippedAfterReturn")
CountDone = Bool("CountDone")
CountAcc = Int("CountAcc")
ResetCount = Bool("ResetCount")

with Program(strict=False) as logic:
    # Main rung
    with Rung(Step == 0, AutoMode):

        # Call subroutine from the main rung
        call("init_sub")
        out(MainLight)

        # Branch runs only when parent rung AND AutoMode are true
        with branch(AutoMode):
            out(AutoLight)
            copy(1, Step, oneshot=True)


    # Multi-line rung with counter – tests region end-line coverage
    # The .reset() is on a separate line; its source_line won't be in
    # the instruction metadata, only rung.end_line (AST) covers it.
    with Rung(Step == 1, AutoMode):
        out(MainLight)
        count_up(CountDone, CountAcc,
                 setpoint=10) \
            .reset(ResetCount)

    # Pointer-condition playground for debug annotation formatting.
    with Rung(StepData[CurStep] == DebugStep):
        out(SkippedAfterReturn)

    # Subroutine body
    with subroutine("init_sub"):
        with Rung(Step == 0):
            out(SubLight)
            out(SubLight)
        with Rung():
            out(SubLight)

runner = PLCRunner(logic)
runner.patch(
    {
        "Step": 0,
        "CurStep": 1,
        "DebugStep": 5,
        "StepData[1]": 0,
        "AutoMode": True,
        "MainLight": False,
        "AutoLight": False,
        "SubLight": False,
        "SkippedAfterReturn": False,
        "CountDone": False,
        "CountAcc": 0,
        "ResetCount": False,
    }
)

if os.getenv("PYRUNG_DAP_ACTIVE") != "1":
    runner.step()
    print(dict(runner.current_state.tags))
