"""Structural patterns from real Click PLC programs for corridor walker testing.

Each test extracts one pattern from the APC_PackTag_SFC template that the
corridor walker hasn't been tested against.  Programs are minimalized: same
structure, fewer tags, no alarm/historian/HMI noise.

Patterns:
1. Int command-value protocol (multi-step state machine transitions)
2. return_early() subroutine flow gating
3. Independent SFC rendezvous (two subsystems must both complete)
4. Odd/even step sequencer (CurStep%2 auto-advance)
5. Deep conditional subroutine call chain (mode -> state -> SFC -> step -> output)
"""

from __future__ import annotations

from pyrung import (
    Bool,
    Int,
    Program,
    Rung,
    Timer,
    calc,
    call,
    copy,
    on_delay,
    out,
    reset,
    return_early,
    rung,
    subroutine,
)
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Pattern 1: Int command-value protocol
# ---------------------------------------------------------------------------


def _cmd_protocol_program() -> tuple[Program, Bool]:
    """Multi-step command protocol: Bool commands -> Int state transitions.

    Stopped(0) -> CmdReset pulse -> Idle(1) -> CmdStart pulse -> Execute(2).
    Output gated by StateCurrent==2.  Mimics the PackML
    sm_map_cmd2_val -> sm_ctrl_cmd2_state_request pipeline: Bool external
    inputs set an Int command register; a validation gate checks the command
    against the current state; a valid command sets StateRequested; a
    finalizer copies StateRequested into StateCurrent.

    The walker must discover two sequential steer values for the same Int
    mechanism, where the second is only valid after the first has settled.
    """
    CmdReset = Bool("CmdReset", external=True)
    CmdStart = Bool("CmdStart", external=True)

    CtrlCmd = Int("CtrlCmd")
    CmdRequest = Int("CmdRequest")
    StateCurrent = Int("StateCurrent")
    StateRequested = Int("StateRequested")
    Output = Bool("Output")

    with Program() as prog:
        # Map Bool commands to Int values (sm_map_cmd2_val)
        with Rung(CmdReset):
            copy(1, CtrlCmd)
            copy(1, CmdRequest)
        with Rung(CmdStart):
            copy(2, CtrlCmd)
            copy(1, CmdRequest)

        # Validate and map to state request (sm_ctrl_cmd2_state_request)
        with Rung(CmdRequest == 1, CtrlCmd == 1, StateCurrent == 0):
            copy(1, StateRequested)
        with Rung(CmdRequest == 1, CtrlCmd == 2, StateCurrent == 1):
            copy(2, StateRequested)

        # Apply state change (sm_copy_or_jump_state)
        with Rung(StateRequested != 0):
            copy(StateRequested, StateCurrent)
            copy(0, StateRequested)
            copy(0, CmdRequest)
            copy(0, CtrlCmd)

        with Rung(StateCurrent == 2):
            out(Output)

    return prog, Output


def test_cmd_protocol_premise() -> None:
    """Premise: manual command sequence reaches Execute."""
    prog, _Output = _cmd_protocol_program()
    plc = PLC(prog)

    plc.patch({"CmdReset": True})
    plc.step()
    plc.patch({"CmdReset": False})
    plc.step()
    assert plc.state.tags["StateCurrent"] == 1

    plc.patch({"CmdStart": True})
    plc.step()
    plc.patch({"CmdStart": False})
    plc.step()
    assert plc.state.tags["StateCurrent"] == 2
    assert plc.state.tags["Output"] is True


def test_cmd_protocol_walker() -> None:
    """Walker must discover the two-step Reset+Start command sequence."""
    prog, Output = _cmd_protocol_program()
    plc = PLC(prog)
    path = plc.how(Output)
    assert path.reachable


# ---------------------------------------------------------------------------
# Pattern 2: return_early() subroutine flow gating
# ---------------------------------------------------------------------------


def _return_early_program() -> tuple[Program, Bool]:
    """Subroutine with return_early() blocking downstream output.

    When Enable is False, the shutdown section does return_early(), skipping
    the output rung entirely.  The output writer has no explicit condition --
    it is *flow-gated* by return_early(), not condition-gated.  The walker
    must understand that Enable is in the output's cone despite not appearing
    in the output rung's condition.
    """
    Enable = Bool("Enable", external=True)
    Output = Bool("Output")

    @subroutine("ReturnEarlySFC")
    def my_sfc():
        with rung(~Enable):
            return_early()
        with rung():
            out(Output)

    with Program() as prog:
        with Rung():
            call(my_sfc)

    return prog, Output


def test_return_early_premise() -> None:
    """Premise: Enable=True bypasses return_early and reaches Output."""
    prog, _Output = _return_early_program()
    plc = PLC(prog)

    plc.step()
    assert plc.state.tags["Output"] is not True

    plc.patch({"Enable": True})
    plc.step()
    assert plc.state.tags["Output"] is True


def test_return_early_walker() -> None:
    """Walker must understand return_early() flow gating."""
    prog, Output = _return_early_program()
    plc = PLC(prog)
    path = plc.how(Output)
    assert path.reachable


# ---------------------------------------------------------------------------
# Pattern 3: Independent SFC rendezvous
# ---------------------------------------------------------------------------


def _rendezvous_program() -> tuple[Program, Bool]:
    """Two independent SFCs must both complete before Output.

    SfcA needs EnableA held for 200 ms (timer); SfcB needs EnableB held for
    300 ms.  Releasing either enable resets that SFC's init flag (main-level
    reset rung).  The walker must hold both enables simultaneously for at
    least 300 ms.

    The pulse steer prefix releases all currently-high external inputs
    before pulsing the target, so steering EnableB clobbers the held EnableA.
    Serial prerequisite walking fails: after walking InitA (EnableA held),
    the EnableB steer releases EnableA, which fires reset(InitA).

    This is the Tier 1 (force-and-sum) pattern: two independent subsystems
    that need simultaneous input holds, connected only by an And gate on
    their internal completion flags.
    """
    EnableA = Bool("EnableA", external=True)
    EnableB = Bool("EnableB", external=True)
    InitA = Bool("InitA")
    InitB = Bool("InitB")
    TmrA = Timer.clone("TmrA")
    TmrB = Timer.clone("TmrB")
    Output = Bool("Output")

    @subroutine("RendezvousSfcA")
    def sfc_a():
        with rung():
            on_delay(TmrA, 200, "ms")
        with rung(TmrA.Done):
            out(InitA)

    @subroutine("RendezvousSfcB")
    def sfc_b():
        with rung():
            on_delay(TmrB, 300, "ms")
        with rung(TmrB.Done):
            out(InitB)

    with Program() as prog:
        with Rung(EnableA):
            call(sfc_a)
        with Rung(~EnableA):
            reset(InitA)
        with Rung(EnableB):
            call(sfc_b)
        with Rung(~EnableB):
            reset(InitB)
        with Rung(InitA, InitB):
            out(Output)

    return prog, Output


def test_rendezvous_premise() -> None:
    """Premise: both enables held simultaneously reaches Output."""
    prog, _Output = _rendezvous_program()
    plc = PLC(prog, dt=0.010)

    plc.patch({"EnableA": True, "EnableB": True})
    for _ in range(35):
        plc.step()
    assert plc.state.tags["InitA"] is True
    assert plc.state.tags["InitB"] is True
    assert plc.state.tags["Output"] is True


def test_rendezvous_serial_clobber() -> None:
    """Demonstrate: enabling A then B serially drops A's init."""
    prog, _Output = _rendezvous_program()
    plc = PLC(prog, dt=0.010)

    plc.patch({"EnableA": True})
    for _ in range(25):
        plc.step()
    assert plc.state.tags["InitA"] is True

    # Switching to EnableB releases EnableA -> reset(InitA)
    plc.patch({"EnableA": False, "EnableB": True})
    plc.step()
    assert plc.state.tags["InitA"] is not True


def test_rendezvous_walker() -> None:
    """Walker holds both enables simultaneously via Tier 1 force-and-sum."""
    prog, Output = _rendezvous_program()
    plc = PLC(prog, dt=0.010)
    path = plc.how(Output)
    assert path.reachable


# ---------------------------------------------------------------------------
# Pattern 4: Odd/even step sequencer
# ---------------------------------------------------------------------------


def _step_sequencer_program() -> tuple[Program, Bool]:
    """SFC step sequencer with odd/even auto-advance.

    CurStep increments on Trans=1; even steps auto-advance immediately.
    Stable values are odd: 1, 3, 5.  Transition from step 1 to step 3
    requires: Advance pulse -> Trans=1 -> CurStep=2 -> even skip -> CurStep=3
    (two scans after the pulse).

    Mimics the Click SFC boilerplate: every step handler sits on an odd
    CurStep value; the even-step auto-advance provides a neutral zone for
    manual troubleshooting (decrement CurStep by 1 -> land on even -> skip
    to next odd without executing step logic).
    """
    Advance = Bool("Advance", external=True)
    CurStep = Int("CurStep", default=1)
    Trans = Int("Trans")
    ValIsOdd = Int("ValIsOdd")
    Output = Bool("Output")

    with Program() as prog:
        # Step 1: transition when Advance
        with Rung(CurStep == 1, Advance):
            copy(1, Trans)

        # Step 3: output
        with Rung(CurStep == 3):
            out(Output)

        # Even step auto-advance
        with Rung():
            calc(CurStep % 2, ValIsOdd)
        with Rung(ValIsOdd != 1):
            calc(CurStep + 1, CurStep)

        # Trans-driven step advancement
        with Rung(Trans == 1):
            calc(CurStep + 1, CurStep)
            copy(0, Trans)

    return prog, Output


def test_step_sequencer_premise() -> None:
    """Premise: Advance pulse reaches step 3 and Output."""
    prog, _Output = _step_sequencer_program()
    plc = PLC(prog)

    assert plc.state.tags["CurStep"] == 1

    plc.patch({"Advance": True})
    plc.step()  # Trans=1, CurStep -> 2
    plc.patch({"Advance": False})
    plc.step()  # Even skip: CurStep -> 3
    plc.step()  # out(Output) fires at CurStep==3
    assert plc.state.tags["CurStep"] == 3
    assert plc.state.tags["Output"] is True


def test_step_sequencer_walker() -> None:
    """Walker must handle CurStep self-increment + even-skip pattern."""
    prog, Output = _step_sequencer_program()
    plc = PLC(prog)
    path = plc.how(Output)
    assert path.reachable


# ---------------------------------------------------------------------------
# Pattern 5: Deep conditional subroutine call chain
# ---------------------------------------------------------------------------


def _deep_call_program() -> tuple[Program, Bool]:
    """Six-level prerequisite chain through conditional subroutine calls.

    CmdProd -> Mode=1 -> production sub called -> state machine driven to
    Execute (CmdReset + CmdStart) -> SfcCall=1 -> SFC called -> timer ->
    CurStep walks to 3 -> Confirm -> Output.

    Prerequisite depth: Mode (1) -> StateCurrent (2-3) -> SfcCall (4) ->
    CurStep (5) -> Confirm (5).  Three distinct governing tags (Mode,
    StateCurrent, CurStep) across three subroutine scopes, plus the
    return_early flow gate in the SFC shutdown section.
    """
    CmdProd = Bool("CmdProd", external=True)
    CmdReset = Bool("CmdReset", external=True)
    CmdStart = Bool("CmdStart", external=True)
    Confirm = Bool("Confirm", external=True)

    Mode = Int("Mode")
    StateCurrent = Int("StateCurrent")
    StateRequested = Int("StateRequested")

    SfcCall = Int("SfcCall")
    CurStep = Int("CurStep", default=1)
    Trans = Int("Trans")
    ValIsOdd = Int("ValIsOdd")
    Tmr = Timer.clone("Tmr")

    Output = Bool("Output")

    @subroutine("DeepProduction")
    def production():
        with rung(StateCurrent == 2):
            copy(1, SfcCall)
        with rung(StateCurrent != 2):
            copy(0, SfcCall)

    @subroutine("DeepSFC")
    def my_sfc():
        # Shutdown: exit if not enabled
        with rung(SfcCall == 0):
            return_early()
        # Step 1: timer wait
        with rung(CurStep == 1):
            on_delay(Tmr, 100, "ms")
        with rung(CurStep == 1, Tmr.Done):
            copy(1, Trans)
        # Step 3: output gate
        with rung(CurStep == 3, Confirm):
            out(Output)
        # Even skip
        with rung():
            calc(CurStep % 2, ValIsOdd)
        with rung(ValIsOdd != 1):
            calc(CurStep + 1, CurStep)
        # Trans advance
        with rung(Trans == 1):
            calc(CurStep + 1, CurStep)
            copy(0, Trans)

    with Program() as prog:
        # Mode selection
        with Rung(CmdProd):
            copy(1, Mode)

        # Command protocol (simplified)
        with Rung(CmdReset, StateCurrent == 0):
            copy(1, StateRequested)
        with Rung(CmdStart, StateCurrent == 1):
            copy(2, StateRequested)
        with Rung(StateRequested != 0):
            copy(StateRequested, StateCurrent)
            copy(0, StateRequested)

        # Production mode -> subroutines
        with Rung(Mode == 1):
            call(production)

        # SFC call
        with Rung(SfcCall == 1):
            call(my_sfc)

    return prog, Output


def test_deep_call_premise() -> None:
    """Premise: full manual sequence reaches Output."""
    prog, _Output = _deep_call_program()
    plc = PLC(prog, dt=0.010)

    # 1. Production mode
    plc.patch({"CmdProd": True})
    plc.step()
    plc.patch({"CmdProd": False})
    assert plc.state.tags["Mode"] == 1

    # 2. Reset: Stopped -> Idle
    plc.patch({"CmdReset": True})
    plc.step()
    plc.patch({"CmdReset": False})
    assert plc.state.tags["StateCurrent"] == 1

    # 3. Start: Idle -> Execute
    plc.patch({"CmdStart": True})
    plc.step()
    plc.patch({"CmdStart": False})
    assert plc.state.tags["StateCurrent"] == 2
    assert plc.state.tags["SfcCall"] == 1

    # 4. Timer + step advance (~12 scans at 10 ms/scan for 100 ms timer)
    for _ in range(15):
        plc.step()
    assert plc.state.tags["CurStep"] == 3

    # 5. Confirm -> Output
    plc.patch({"Confirm": True})
    plc.step()
    assert plc.state.tags["Output"] is True


def test_deep_call_walker() -> None:
    """Walker must traverse 6 prerequisite levels across 3 subroutine scopes."""
    prog, Output = _deep_call_program()
    plc = PLC(prog, dt=0.010)
    path = plc.how(Output)
    assert path.reachable
