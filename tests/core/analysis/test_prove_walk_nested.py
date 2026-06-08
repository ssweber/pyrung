"""Nested-corridor reproducer for the corridor walker (``plan_walk``).

A distilled version of the real CLICK ``how(y_BurnerLoop)`` case that spikes the
BFS to 20+ GB: the target sits behind three coupled, timer-gated state machines
that must all be driven to specific phases at once.

  PackML stack     : Mode->Production, State->EXECUTE   (command pulses + a dwell)
  Production steps  : rests at 'Dry', dwells (HeatDelay) + temp<band -> calls Heat
  Heat task        : step sequencer rests at 3, dwells (HeatTimer) -> o_Burner
  output map       : o_Burner -> y_Burner   (Production mode only)

It keeps the nesting + per-layer timer dwell + analog gate that make the problem
hard, and strips the alarms / warnings / historian / other modes & tasks that
only add breadth.

Current state of the art: the walker **bails** on this (no single steer advances
the innermost governing tag from the start; the prerequisite layers must be
solved first — the nested / prerequisite-corridor case), so ``plan_walk``
returns ``None`` and ``how()`` falls back to BFS.  ``test_walker_bails_today`` is
the tripwire: when prerequisite-corridor support lands it will start returning a
``Path`` and that test must be flipped to assert success.
"""

from __future__ import annotations

from pyrung import (
    Bool,
    Int,
    Timer,
    call,
    copy,
    on_delay,
    out,
    program,
    return_early,
    rung,
    subroutine,
)
from pyrung.core.analysis.simplified import Atom
from pyrung.core.runner import PLC

# --- Layer 1: PackML-lite mode / state ---
CmdMode = Bool("CmdMode", external=True)  # request Production mode
CmdStart = Bool("CmdStart", external=True)  # request start (IDLE->STARTING->EXECUTE)
InitDone = Bool("InitDone")
Mode = Int("Mode")  # 0=off, 1=Production
State = Int("State")  # 1=IDLE, 2=STARTING, 3=EXECUTE
StateTimer = Timer.clone("StateTimer")

# --- Layer 2: production step sequencer ---
ProdStep = Int("ProdStep")  # 0=idle, 1=Dry (rests here, dwells, calls Heat)
HeatDelay = Timer.clone("HeatDelay")
Temp = Int("Temp", external=True)  # analog dryer temp (held input)
LowBand = Int("LowBand")
HeatCall = Bool("HeatCall")

# --- Layer 3: heat task ---
HeatStep = Int("HeatStep")  # 0=idle, 1=warmup (rests, dwells), 3=burner
HeatTimer = Timer.clone("HeatTimer")
o_Burner = Bool("o_Burner")

# --- output ---
y_Burner = Bool("y_Burner", lock=True)  # TARGET


@subroutine("burner_prod_steps")
def _prod_steps() -> None:
    with rung(State != 3):  # not executing -> clear the sequence
        copy(0, ProdStep)
        copy(0, HeatDelay.Acc)
        return_early()
    with rung(ProdStep == 0):  # enter at the Dry step
        copy(1, ProdStep)
    with rung():  # Dry-step dwell timer
        on_delay(HeatDelay, 10, "sec")
    with rung(ProdStep == 1, HeatDelay.Done, Temp < LowBand):  # dwell + cool -> call Heat
        copy(1, HeatCall)


@subroutine("burner_heat_task")
def _heat_task() -> None:
    with rung(HeatCall == 0):  # not called -> clear the task
        copy(0, HeatStep)
        copy(0, HeatTimer.Acc)
        return_early()
    with rung(HeatStep == 0):  # enter at warmup
        copy(1, HeatStep)
    with rung():  # warmup dwell timer
        on_delay(HeatTimer, 5, "sec")
    with rung(HeatStep == 1, HeatTimer.Done):  # warmup elapsed -> burner step
        copy(3, HeatStep)
    with rung(HeatStep == 3, Temp < LowBand):  # burner step drives the loop
        out(o_Burner)


@program
def _burner_logic() -> None:
    with rung(~InitDone):
        copy(1, State)  # IDLE
        copy(100, LowBand)
        copy(1, InitDone)

    with rung(CmdMode):
        copy(1, Mode)

    with rung():  # state timer free-runs (the STARTING dwell)
        on_delay(StateTimer, 1, "sec")

    with rung(Mode == 1, State == 1, CmdStart):  # IDLE + Production + Start -> STARTING
        copy(2, State)
        copy(0, StateTimer.Acc)

    with rung(State == 2, StateTimer.Done):  # STARTING dwell done -> EXECUTE
        copy(3, State)

    with rung(State == 3):  # EXECUTE -> run the production step sequencer
        call(_prod_steps)

    with rung(HeatCall == 1):  # production called the Heat task
        call(_heat_task)

    with rung(Mode == 1, o_Burner):  # set_outputs (Production): o_Burner -> y_Burner
        out(y_Burner)


def test_forward_reachable() -> None:
    """The distilled program is valid: holding the inputs drives y_Burner true
    through all three nested dwells (1 s state + 10 s heat-delay + 5 s heat)."""
    plc = PLC(_burner_logic, dt=0.010)
    plc.patch({"CmdMode": True})
    plc.step()
    plc.patch({"CmdStart": True})
    for _ in range(2000):  # comfortably past 1 s + 10 s + 5 s at dt=0.01
        plc.step()

    assert plc.state.tags["State"] == 3  # EXECUTE
    assert plc.state.tags["HeatStep"] == 3  # burner step
    assert plc.state.tags["y_Burner"] is True


def test_walker_reaches_nested_target() -> None:
    """The corridor walker solves the 3-layer nested corridor via multi-tag
    factoring: prerequisite discovery drives Mode, State, ProdStep, HeatCall,
    and HeatStep in sequence, chaining time-folds through three timer dwells."""
    from pyrung.core.analysis.prove.walk import plan_walk

    plc = PLC(_burner_logic, dt=0.010)
    snapshot = dict(plc._state.tags)
    goal = Atom(tag="y_Burner", form="xic", operand=True)

    path = plan_walk(plc, snapshot, goal, 20)
    assert path is not None
    assert path.reachable
    assert path.total_scans > 0
