# scratchpad/examples/demo_branch_subroutine.py
from pyrung.core import (
    Bool,
    Int,
    PLCRunner,
    Program,
    Rung,
    branch,
    call,
    copy,
    out,
    return_,
    subroutine,
)

Step = Int("Step")
AutoMode = Bool("AutoMode")
MainLight = Bool("MainLight")
AutoLight = Bool("AutoLight")
SubLight = Bool("SubLight")
SkippedAfterReturn = Bool("SkippedAfterReturn")

with Program(strict=False) as logic:
    # Main rung
    with Rung(Step == 0):
        out(MainLight)

        # Branch runs only when parent rung AND AutoMode are true
        with branch(AutoMode):
            out(AutoLight)
            copy(1, Step, oneshot=True)

        # Call subroutine from the main rung
        call("init_sub")

    # Subroutine body
    with subroutine("init_sub"):
        with Rung():
            out(SubLight)
            return_()  # exits subroutine early
            out(SkippedAfterReturn)

runner = PLCRunner(logic)
runner.patch(
    {
        "Step": 0,
        "AutoMode": True,
        "MainLight": False,
        "AutoLight": False,
        "SubLight": False,
        "SkippedAfterReturn": False,
    }
)

runner.step()
print(dict(runner.current_state.tags))
