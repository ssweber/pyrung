"""PILOT gate: a program departure remains ordinary provisional piloting.

The shape (a miniature of the real burner's HoldForShine handshake):

* the Execute-era recipe dwell needs the door **closed** (``i_Door`` gates the
  phase timer), so the pilot earns a steady ``Door=True`` hold as a coast
  prerequisite;
* at recipe step 103 the program issues its **own** Hold — a channel departure
  that classifies as a stopover with a clean return route;
* the Execute-scoped door rung yields in HELD, so Boolean baseline opens the
  door and advances the recipe from 103 to 105;
* the clean route supplies ``C_Unhold``. Trying it with the door open enters
  Unholding, latches ``DoorAlarm``, and regresses to Aborted;
* ordinary investigation from the provisional HELD checkpoint learns the
  corrective closed-door rung, reverts locally, and retries the same route;
* with the door closed through Unholding, the detour rejoins Execute and works.

The gate proves there is no second detour controller: normal candidate,
VERIFY, regression, investigation, PilotRung, checkpoint, and retry mechanics
remain active inside the provisional corridor.

The fixture reuses the armed opaque-loop / constant-mask-table skeleton of
``test_pilot_table_detour.py`` (the plain-copy sibling never arms the compass
value graph the detour classifier reads) plus the discrete stepper shape of
``test_pilot_gauge._step_chain_program`` (the detour needs a gauge component
or classification fails closed to regression).
"""

from __future__ import annotations

from pyrung import (
    PLC,
    Block,
    Bool,
    Int,
    Program,
    TagType,
    Timer,
    calc,
    copy,
    latch,
    on_delay,
    out,
    reset,
    rung,
)

_DWELL_MS = 100


def _door_cycle_program() -> tuple[Program, dict[str, object]]:
    IDLE, EXECUTE, ABORTED, HOLDING, HELD, UNHOLDING, RESETTING, COMPLETING, COMPLETED = (
        4,
        6,
        9,
        10,
        11,
        12,
        15,
        16,
        17,
    )
    START_CMD, HOLD_CMD, UNHOLD_CMD, COMPLETE_CMD = 2, 4, 5, 10

    STATE_CHOICES = {
        0: "Undefined", 1: "Clearing", 2: "Stopped", 3: "Starting", 4: "Idle",
        5: "Suspended", 6: "Execute", 7: "Stopping", 8: "Aborting", 9: "Aborted",
        10: "Holding", 11: "Held", 12: "Unholding", 13: "Suspending",
        14: "Unsuspending", 15: "Resetting", 16: "Completing", 17: "Completed",
    }  # fmt: skip
    CMD_CHOICES = {
        0: "Undefined", 1: "Reset", 2: "Start", 3: "Stop", 4: "Hold", 5: "Unhold",
        6: "Suspend", 7: "Unsuspend", 8: "Abort", 9: "Clear", 10: "Complete",
    }  # fmt: skip

    # Operator surface: the door (a physical input), the start button, the
    # Unhold button, and the command button PILOT must avoid.
    Door = Bool("HRel_DoorClosed", external=True)
    C_Start = Bool("HRel_C_Start", external=True)
    C_Unhold = Bool("HRel_C_Unhold", external=True)
    C_Complete = Bool("HRel_C_Complete", external=True)
    DoorAlarm = Bool("HRel_DoorAlarm")

    i_Door = Bool("HRel_iDoorClosed")  # the input image (the ReadInputs idiom)

    Cmd = Int("HRel_Cmd", choices=CMD_CHOICES)
    CmdReq = Int("HRel_CmdReq")
    State = Int("HRel_State", default=IDLE, choices=STATE_CHOICES)
    StateRequested = Int("HRel_StateRequested")
    Mode = Int("HRel_Mode", default=1, choices={1: "Production", 2: "Maintenance", 3: "Manual"})

    CmdStartRef = Int("HRel_CmdStartRef", readonly=True, default=START_CMD)
    CmdHoldRef = Int("HRel_CmdHoldRef", readonly=True, default=HOLD_CMD)
    CmdUnholdRef = Int("HRel_CmdUnholdRef", readonly=True, default=UNHOLD_CMD)
    CmdCompleteRef = Int("HRel_CmdCompleteRef", readonly=True, default=COMPLETE_CMD)

    # The recipe coordinate — a discrete stepper (credential family B): +2
    # affine advances armed by step-derived flags (self-limiting provenance).
    # ``choices`` bounds the backward trace's affine descent (need 103 while
    # the snapshot sits at 105 would otherwise walk 101, 99, 97, … forever).
    Step = Int(
        "HRel_Step",
        default=101,
        choices={101: "Phase1", 103: "HoldPoint", 105: "DoorCycled", 107: "Phase2Done"},
    )
    At101 = Bool("HRel_At101")
    At103 = Bool("HRel_At103")
    At105 = Bool("HRel_At105")
    At107 = Bool("HRel_At107")
    Trans = Bool("HRel_Trans")

    PhaseTmr = Timer.clone("HRel_PhaseTmr")
    ShineTmr = Timer.clone("HRel_ShineTmr")

    MaskIdx = Int("HRel_MaskIdx")
    StateMask = Int("HRel_StateMask")
    CfgIdx = Int("HRel_CfgIdx")
    DisabledStates = Int("HRel_DisabledStates")
    MaskResult = Int("HRel_MaskResult")
    EnblYes = Int("HRel_EnblYes")
    JumpIdx = Int("HRel_JumpIdx")
    JumpTarget = Int("HRel_JumpTarget")

    # Constant tables (see test_pilot_table_detour.py): per-state mask bits at
    # MT[301..317], cfg disabled-state masks at MT[201..203] (nothing disabled),
    # identity jump table JT[151..167].
    MT = Block("HRel_MT", TagType.INT, 200, 340)
    JT = Block("HRel_JT", TagType.INT, 150, 340)
    MT.slot(201, default=0x0000)
    MT.slot(202, default=0x0000)
    MT.slot(203, default=0x0000)
    for s, bit in {
        1: 0x0001, 2: 0x0002, 3: 0x0004, 4: 0x0008, 5: 0x0010, 6: 0x0020,
        7: 0x0040, 8: 0x0080, 9: 0x0100, 10: 0x0200, 11: 0x0400, 12: 0x0800,
        13: 0x1000, 14: 0x2000, 15: 0x4000, 16: 0x8000, 17: 0x0001,
    }.items():  # fmt: skip
        MT.slot(300 + s, default=bit)
    for s in range(1, 18):
        JT.slot(150 + s, default=s)

    with Program(strict=False) as logic:
        # Input image — the out-coil hop the release check must read through
        # (a held Door pins i_Door at the hold's polarity every scan).
        with rung(Door):
            out(i_Door)

        # Step-derived flags (the stepper's self-limiting provenance).
        with rung(Step == 101):
            out(At101)
        with rung(Step == 103):
            out(At103)
        with rung(Step == 105):
            out(At105)
        with rung(Step == 107):
            out(At107)

        # Operator command producers.
        with rung(C_Start):
            copy(CmdStartRef, Cmd)
            copy(1, CmdReq)
        with rung(C_Complete):
            copy(CmdCompleteRef, Cmd)
            copy(1, CmdReq)
        with rung(C_Unhold):
            copy(CmdUnholdRef, Cmd)
            copy(1, CmdReq)

        # Recipe phase 1: the Execute dwell needs the door CLOSED.
        with rung(State == EXECUTE, At101, i_Door):
            on_delay(PhaseTmr, _DWELL_MS, "ms")
        with rung(At101, PhaseTmr.Done):
            latch(Trans)

        # At step 103 the program holds itself (the burner's R17).
        with rung(State == EXECUTE, At103):
            copy(CmdHoldRef, Cmd)
            copy(1, CmdReq)

        # The HELD-era door cycle: the advance needs the door OPEN (R18).
        with rung(State == HELD, At103, ~i_Door):
            latch(Trans)

        # Recipe phase 2 back in Execute needs the door CLOSED again.
        with rung(State == EXECUTE, At105, i_Door):
            on_delay(ShineTmr, _DWELL_MS, "ms")
        with rung(At105, ShineTmr.Done):
            latch(Trans)

        # At step 107 the program completes on its own.
        with rung(State == EXECUTE, At107):
            copy(CmdCompleteRef, Cmd)
            copy(1, CmdReq)

        # The stepper: one +2 affine writer, transition-pulse armed.
        with rung(Trans):
            calc(Step + 2, Step)
            reset(Trans)

        # Real recipe steppers have an establishment/reset writer.  Besides
        # being the credential eraser, it bounds reverse value walking when a
        # stale step-derived Trans writer is considered after the live step has
        # already advanced.
        with rung(State == RESETTING):
            copy(101, Step)

        # Command -> state request, gated by the current state.
        with rung(CmdReq == 1, Cmd == START_CMD, State == IDLE):
            copy(EXECUTE, StateRequested)
        with rung(CmdReq == 1, Cmd == HOLD_CMD, State == EXECUTE):
            copy(HOLDING, StateRequested)
        with rung(CmdReq == 1, Cmd == UNHOLD_CMD, State == HELD):
            copy(UNHOLDING, StateRequested)
        with rung(CmdReq == 1, Cmd == COMPLETE_CMD, State == EXECUTE):
            copy(COMPLETING, StateRequested)

        # Auto (state-complete) transitions.  The Completed request carries the
        # recipe conjunct (state-complete gated on the recipe being done) — it
        # is also what lets the backward trace cross from the channel side
        # (cut at the second channel value by the opaque-loop budget) into the
        # stepper chain where the door needs live.
        with rung(State == HOLDING):
            copy(HELD, StateRequested)
        with rung(State == UNHOLDING):
            copy(EXECUTE, StateRequested)
        # Executable safety truth: entering Unholding open latches an alarm,
        # which wins over the normal rejoin and throws the route to Aborted.
        with rung(State == UNHOLDING, ~i_Door):
            latch(DoorAlarm)
        with rung(DoorAlarm):
            copy(ABORTED, StateRequested)
        with rung(State == COMPLETING, At107):
            copy(COMPLETED, StateRequested)

        # Enable: constant-table mask predicate (everything enabled).
        with rung():
            calc(300 + StateRequested, MaskIdx)
        with rung():
            copy(MT[MaskIdx], StateMask)
        with rung():
            calc(200 + Mode, CfgIdx)
        with rung():
            copy(MT[CfgIdx], DisabledStates)
        with rung():
            calc(StateMask & DisabledStates, MaskResult)
        with rung():
            copy(0, EnblYes)
        with rung(MaskResult == 0):
            copy(1, EnblYes)

        # Indirect jump-table hop (arms detect_opaque_loop).
        with rung():
            calc(StateRequested + 150, JumpIdx)
        with rung():
            copy(JT[JumpIdx], JumpTarget)

        # Apply (enabled) or redirect through the jump chain.
        with rung(StateRequested != 0, EnblYes == 1):
            copy(StateRequested, State)
            copy(0, StateRequested)
            copy(0, Cmd)
            copy(0, CmdReq)
        with rung(StateRequested != 0, EnblYes != 1, JumpTarget != 0):
            copy(JumpTarget, StateRequested)

    tags: dict[str, object] = {
        "Door": Door,
        "i_Door": i_Door,
        "C_Start": C_Start,
        "C_Unhold": C_Unhold,
        "C_Complete": C_Complete,
        "DoorAlarm": DoorAlarm,
        "State": State,
        "Step": Step,
        "Idle": IDLE,
        "Execute": EXECUTE,
        "Aborted": ABORTED,
        "Held": HELD,
        "Completed": COMPLETED,
    }
    return logic, tags


def _pulse(plc: PLC, tag, value=True, release=False) -> None:
    plc.patch({tag.name: value})
    plc.step()
    if release:
        plc.patch({tag.name: not value})


def test_door_cycle_premise() -> None:
    """Hand-drive: close door, start, program holds itself at 103; open door
    (103 -> 105), ack (program unholds), re-close door, dwell (105 -> 107),
    program completes — Completed without pressing C_Complete."""
    logic, tags = _door_cycle_program()
    plc = PLC(logic, dt=0.010)
    state, step = tags["State"].name, tags["Step"].name

    plc.patch({tags["Door"].name: True})
    plc.step()
    _pulse(plc, tags["C_Start"], release=True)
    plc.run(cycles=30)
    assert plc.state.tags[state] == tags["Held"], plc.state.tags[state]
    assert plc.state.tags[step] == 103

    # The door cycle: open advances the recipe at HELD.
    plc.patch({tags["Door"].name: False})
    plc.run(cycles=3)
    assert plc.state.tags[step] == 105

    # Unhold while open is unsafe and latches the alarm.
    _pulse(plc, tags["C_Unhold"], release=True)
    plc.run(cycles=3)
    assert plc.state.tags[state] == tags["Aborted"]
    assert plc.state.tags[tags["DoorAlarm"].name] is True

    # A fresh safe run closes before Unhold.
    logic, tags = _door_cycle_program()
    plc = PLC(logic, dt=0.010)
    state, step = tags["State"].name, tags["Step"].name
    plc.patch({tags["State"].name: tags["Held"], tags["Step"].name: 105})
    plc.patch({tags["Door"].name: True})
    plc.step()
    _pulse(plc, tags["C_Unhold"], release=True)
    plc.run(cycles=3)
    assert plc.state.tags[state] == tags["Execute"]
    assert plc.state.tags[tags["DoorAlarm"].name] is False

    # Door re-closed, phase-2 dwell, program-issued Complete.
    plc.patch({tags["Door"].name: True})
    plc.run(cycles=30)
    assert plc.state.tags[state] == tags["Completed"]
    assert plc.state.tags[step] == 107
    assert plc.state.tags[tags["C_Complete"].name] is False


def test_stepper_is_a_gauge_component() -> None:
    """Detour classification fails closed without a gauge; the fixture's
    recipe stepper must classify (family B, self-limiting +2 advances)."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.gauge import build_gauge
    from pyrung.core.analysis.pilot.pilot import _build_pilot_context
    from pyrung.core.analysis.pilot.trace import compute_clear_only, compute_steerable

    logic, tags = _door_cycle_program()
    plc = PLC(logic, dt=0.010)
    plc.step()
    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, logic)
    clear_only = compute_clear_only(pdg, plc._known_tags_by_name, logic)
    _nd, cfg, _evidence, _sem = _build_pilot_context(logic, dict(plc.state.tags))
    assert cfg is not None
    gauge = build_gauge(
        pdg,
        logic,
        tags["State"].name,
        cfg,
        steerable=steerable,
        clear_only=clear_only,
        edge_tags=frozenset(),
        pipeline_internal_tags=frozenset(),
        channel_tags=frozenset({tags["State"].name}),
        harness=None,
    )
    by_tag = {c.tag: c for c in gauge.components}
    assert tags["Step"].name in by_tag, [c.tag for c in gauge.components]
    # The prover threshold-absorbs the ``Step == …`` flag comparisons, so the
    # register classifies as family A (ordinal); without that absorption it
    # would classify as family B (stepper). Either carries the detour.
    assert by_tag[tags["Step"].name].kind in ("ordinal", "stepper")
    assert by_tag[tags["Step"].name].direction == 1


def test_provisional_departure_keeps_the_ordinary_pilot_loop_active() -> None:
    """PILOT reaches Completed through the HELD door cycle.

    The route first falsifies open-door Unholding, ordinary investigation
    learns the correction, and the detour works at Execute rejoin—without
    pressing the avoided ``C_Complete``."""
    from pyrung.core.analysis.pilot.pilot import pilot_events
    from pyrung.core.runner import _compile_avoid

    logic, tags = _door_cycle_program()
    plc = PLC(logic, dt=0.010)
    # Focus the gate on the corridor departure.  Starting from Idle lets the
    # candidate settlement fast-forward the phase timer and land directly in
    # HELD, so no Execute checkpoint, earned Door=True hold, or detour loan ever
    # exists.  At Execute the first zoom earns that hold before the program's
    # own Hold ejects the coast.
    plc.patch({tags["Door"].name: True, tags["State"].name: tags["Execute"]})
    plc.step()

    kinds: list[str] = []
    finished = None
    for ev in pilot_events(
        plc,
        tags["State"] == tags["Completed"],
        avoid_pred=_compile_avoid(tags["C_Complete"]),
        max_scans=3000,
    ):
        kinds.append(ev.kind)
        if ev.kind == "finished":
            finished = ev.data
            break

    assert finished is not None and finished["reached"], (finished or {}).get("reason")
    assert "provisional_started" in kinds, kinds
    assert "provisional_promoted" in kinds, kinds
    final = finished["work"].state.tags
    assert final[tags["State"].name] == tags["Completed"]
    assert final[tags["C_Complete"].name] is not True
    assert final[tags["DoorAlarm"].name] is False
