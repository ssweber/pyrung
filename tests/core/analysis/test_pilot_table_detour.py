"""PILOT gate: a PackML command/state fixture that ARMS the opaque-loop / table
surface (unlike the sibling ``_auto_complete_command_program`` in ``test_pilot.py``,
which uses PLAIN copies and never arms it).

Why a second fixture.  The sibling program models the same PackML command detour
(Start -> Execute, a program-owned Hold -> Held, an operator ack -> Unhold, a
program-owned Complete -> Completed) but writes ``State`` with a plain
``copy(StateRequested, State)``.  With no indirect-copy source, ``statics.detect_
opaque_loop`` / ``detect_opaque_pipelines`` (``pilot/statics.py``) return empty, so
the compass value-graph, the constant-table mask oracle, and the state-consistent
pinning machinery all stay dormant.  This fixture reproduces the real tumbler's
shape that *does* arm them:

  * a constant **jump table** ``JT[150 + StateRequested]`` read by an INDIRECT copy
    (``copy(JT[JumpIdx], JumpTarget)``) — the opaque hop that puts ``State`` in
    ``detect_opaque_loop``.  ``State`` itself is written by a plain
    ``copy(StateRequested, State)`` so it stays copy-coupled *stepping* (the prover's
    ``_compute_stepping_tags`` only forwards stepping through a NAMED copy source — an
    ``IndirectRef`` source would drop ``State`` from the stepping set and collapse the
    whole compass; see "DSL/pilot limitations" below);
  * a **constant-table mask enable** ``StateMask[300+StateRequested] &
    DisabledStates[200+Mode] == 0`` gating the transition (the ``table_oracle`` shape);
  * a **free, undeclared, externally-writable neighbor word** ``PackTbl_A_Alm100`` at
    ``MT[300]`` — one offset below the state-mask slots ``MT[301..317]`` — read into an
    alarm interlock that gates ONLY the Completing(16) enable.  It rests at 0 (so the
    machine completes by hand), but the pilot cannot domain a free word, reproducing
    the real project's mis-attribution surface (``how`` there declines naming the
    ``A_Alm*_Status`` neighbor of the ``dh[300+state]`` mask table).

Naming is generic PackML only (ISA-TR88.00.02 numbering) — no process-specific names.

What the pilot actually does (OBSERVED, see the xfail docstring for the transcript):
the pilot engages the compass, drives ``C_Start`` -> Execute, follows the program's
Hold self-advance to Holding(10)/Held(11), then stalls on the Hold->ack->Unhold->
self-issued-Complete handshake and declines at the skiff free-word exit, NAMING the
undeclared mask-table neighbor (``PackTbl_A_Alm100``) — the same mis-attribution the
real machine produces (see the xfail docstring).
"""

from __future__ import annotations

import pytest

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
    on_delay,
    rise,
    rung,
)

# Dwell for the program-owned Hold/Complete timers.  100 ms (= 10 scans at dt=0.010)
# is short enough that a skiff probe window covers a whole dwell, so the loop reaches
# the skiff's second lap (the ``State`` pair-probe that learns a composite cause) and
# lands on the free-word stuck exit — the decline that NAMES the undeclared mask-table
# neighbor, mirroring the real machine's mis-attribution.  A longer dwell (200 ms)
# budget-exhausts on the avoided command first and never reaches that exit.
_DWELL_MS = 100


def _packml_table_detour_program() -> tuple[Program, dict[str, object]]:
    """PackML command/state machine whose state register rides an indirect
    jump-table hop and whose Completing enable reads a free neighbor word."""
    IDLE, EXECUTE, HOLDING, HELD, COMPLETING, COMPLETED = 4, 6, 10, 11, 16, 17
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
    MODE_CHOICES = {1: "Production", 2: "Maintenance", 3: "Manual"}

    # Operator buttons (steerable) + the command register PILOT must avoid pressing.
    C_Start = Bool("PackTbl_C_Start", external=True)
    InterlockAck = Bool("PackTbl_InterlockAck", external=True)
    C_Complete = Bool("PackTbl_C_Complete", external=True)

    Cmd = Int("PackTbl_Cmd", choices=CMD_CHOICES)
    CmdReq = Int("PackTbl_CmdReq")
    State = Int("PackTbl_State", default=IDLE, choices=STATE_CHOICES)
    StateRequested = Int("PackTbl_StateRequested")
    Phase = Int("PackTbl_Phase")
    RecipeStep = Int("PackTbl_RecipeStep")
    Mode = Int("PackTbl_Mode", default=1, choices=MODE_CHOICES)  # Production

    CmdStartRef = Int("PackTbl_CmdStartRef", readonly=True, default=START_CMD)
    CmdHoldRef = Int("PackTbl_CmdHoldRef", readonly=True, default=HOLD_CMD)
    CmdUnholdRef = Int("PackTbl_CmdUnholdRef", readonly=True, default=UNHOLD_CMD)
    CmdCompleteRef = Int("PackTbl_CmdCompleteRef", readonly=True, default=COMPLETE_CMD)

    MaskIdx = Int("PackTbl_MaskIdx")
    StateMask = Int("PackTbl_StateMask")
    CfgIdx = Int("PackTbl_CfgIdx")
    DisabledStates = Int("PackTbl_DisabledStates")
    MaskResult = Int("PackTbl_MaskResult")
    AlmStatus = Int("PackTbl_AlmStatus")
    EnblYes = Int("PackTbl_EnblYes")
    JumpIdx = Int("PackTbl_JumpIdx")
    JumpTarget = Int("PackTbl_JumpTarget")

    HoldTmr = Timer.clone("PackTbl_HoldTmr")
    CompleteTmr = Timer.clone("PackTbl_CompleteTmr")

    # Constant table region MT[200..340]:
    #   cfg disabled-state masks   MT[201..203] (all 0 here → nothing disabled)
    #   per-state mask bits        MT[301..317]
    #   FREE NEIGHBOR WORD         MT[300] (external, undeclared) — the mis-attribution
    MT = Block("PackTbl_MT", TagType.INT, 200, 340)
    # Jump table JT[151..167]: identity, JT[150 + state] = state.
    JT = Block("PackTbl_JT", TagType.INT, 150, 340)

    MT.slot(201, default=0x0000)
    MT.slot(202, default=0x0000)
    MT.slot(203, default=0x0000)
    for s, bit in {
        1: 0x0001, 2: 0x0002, 3: 0x0004, 4: 0x0008, 5: 0x0010, 6: 0x0020,
        7: 0x0040, 8: 0x0080, 9: 0x0100, 10: 0x0200, 11: 0x0400, 12: 0x0800,
        13: 0x1000, 14: 0x2000, 15: 0x4000, 16: 0x8000, 17: 0x0001,
    }.items():  # fmt: skip
        MT.slot(300 + s, default=bit)
    MT.slot(300, name="PackTbl_A_Alm100", external=True, default=0)
    for s in range(1, 18):
        JT.slot(150 + s, default=s)

    with Program(strict=False) as logic:
        # --- operator + program-owned command producers ---
        with rung(C_Start):
            copy(CmdStartRef, Cmd)
            copy(1, CmdReq)
        with rung(C_Complete):
            copy(CmdCompleteRef, Cmd)
            copy(1, CmdReq)
        # A dwell in Execute makes the program issue Hold on its own.
        with rung(State == EXECUTE, Phase == 0):
            on_delay(HoldTmr, _DWELL_MS, "ms")
        with rung(rise(HoldTmr.Done)):
            copy(CmdHoldRef, Cmd)
            copy(1, CmdReq)
        # An operator ack while Held advances the recipe step and issues Unhold.
        with rung(State == HELD, InterlockAck):
            copy(CmdUnholdRef, Cmd)
            copy(1, CmdReq)
            copy(1, Phase)
            calc(RecipeStep + 1, RecipeStep)
        # A second dwell in Execute makes the program issue Complete on its own.
        with rung(State == EXECUTE, Phase == 1):
            on_delay(CompleteTmr, _DWELL_MS, "ms")
        with rung(rise(CompleteTmr.Done)):
            copy(CmdCompleteRef, Cmd)
            copy(1, CmdReq)

        # --- command -> state request (gated by the current state) ---
        with rung(CmdReq == 1, Cmd == START_CMD, State == IDLE):
            copy(EXECUTE, StateRequested)
        with rung(CmdReq == 1, Cmd == HOLD_CMD, State == EXECUTE):
            copy(HOLDING, StateRequested)
        with rung(CmdReq == 1, Cmd == UNHOLD_CMD, State == HELD):
            copy(EXECUTE, StateRequested)
        with rung(CmdReq == 1, Cmd == COMPLETE_CMD, State == EXECUTE):
            copy(COMPLETING, StateRequested)

        # --- auto (state-complete) transitions ---
        with rung(State == HOLDING):
            copy(HELD, StateRequested)
        with rung(State == COMPLETING):
            copy(COMPLETED, StateRequested)

        # --- enable: constant-table mask predicate (all enabled in Production) ---
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
        # Free-word alarm interlock — gates ONLY the Completing(16) enable read.
        # AlmStatus mirrors MT[300], the external undeclared neighbor of the mask
        # table.  It rests at 0 (so the transition fires by hand), but the pilot
        # cannot soundly domain a free word: this is the mis-attribution surface.
        with rung():
            copy(MT[300], AlmStatus)
        with rung(StateRequested == COMPLETING, AlmStatus != 0):
            copy(0, EnblYes)

        # --- indirect jump-table hop (arms detect_opaque_loop) ---
        # JumpTarget = JT[150 + StateRequested] via an INDIRECT copy.  State rides
        # the feedback loop through it (State <- StateRequested <- JumpTarget), so
        # detect_opaque_loop contains State even though State's own writer below is
        # a plain (stepping-preserving) copy.
        with rung():
            calc(StateRequested + 150, JumpIdx)
        with rung():
            copy(JT[JumpIdx], JumpTarget)

        # --- apply (enabled) or redirect through the jump chain (not enabled) ---
        with rung(StateRequested != 0, EnblYes == 1):
            copy(StateRequested, State)
            copy(0, StateRequested)
            copy(0, Cmd)
            copy(0, CmdReq)
        with rung(StateRequested != 0, EnblYes != 1, JumpTarget != 0):
            copy(JumpTarget, StateRequested)

    tags: dict[str, object] = {
        "C_Start": C_Start,
        "InterlockAck": InterlockAck,
        "C_Complete": C_Complete,
        "State": State,
        "StateRequested": StateRequested,
        "Idle": IDLE,
        "Execute": EXECUTE,
        "Holding": HOLDING,
        "Held": HELD,
        "Completing": COMPLETING,
        "Completed": COMPLETED,
        "AlmWord": "PackTbl_A_Alm100",
    }
    return logic, tags


def test_table_detour_premise() -> None:
    """Hand-drive: Start, wait for the program's Hold, ack, wait — reaches
    Completed(17) without ever pressing C_Complete (the program issues Complete)."""
    logic, tags = _packml_table_detour_program()
    plc = PLC(logic, dt=0.010)

    plc.patch({tags["C_Start"].name: True})
    plc.step()
    plc.patch({tags["C_Start"].name: False})
    plc.run(cycles=30)
    assert plc.state.tags[tags["State"].name] == tags["Held"]

    plc.patch({tags["InterlockAck"].name: True})
    plc.step()
    plc.patch({tags["InterlockAck"].name: False})
    plc.run(cycles=30)

    assert plc.state.tags[tags["State"].name] == tags["Completed"]
    assert plc.state.tags[tags["C_Complete"].name] is False


def test_table_detour_arms_opaque_table_surface() -> None:
    """The whole point of this fixture: it ARMS the opaque-loop / table machinery
    the sibling plain-copy fixture leaves dormant.

    ``detect_opaque_loop`` must contain the state register (the indirect jump-table
    hop feeds back into it), the prover must still classify ``State`` as *stepping*
    (so the compass value-graph engages), and ``infer_pipeline_roles`` must see the
    StateRequested -> State transition pipeline.
    """
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.evidence import infer_pipeline_roles
    from pyrung.core.analysis.pilot.pilot import _build_pilot_context
    from pyrung.core.analysis.pilot.statics import detect_opaque_loop
    from pyrung.core.analysis.pilot.trace import compute_steerable

    logic, tags = _packml_table_detour_program()
    plc = PLC(logic, dt=0.010)
    pdg = build_program_graph(logic)
    state_name = tags["State"].name

    # (1) The indirect jump-table copy puts the state register in the opaque loop.
    opaque_loop = detect_opaque_loop(pdg, logic)
    assert state_name in opaque_loop, sorted(opaque_loop)

    # (2) State stays copy-coupled *stepping* (plain copy source), so the compass
    #     value-graph is built for it rather than the loop dead-ending immediately.
    _nd, _key, evidence, _sem = _build_pilot_context(logic, dict(plc.state.tags))
    assert evidence is not None
    assert evidence.is_stepping(state_name)

    # (3) The StateRequested -> State transition pipeline is visible.
    steerable = compute_steerable(pdg, plc._known_tags_by_name, logic)
    role = infer_pipeline_roles(state_name, pdg, logic, steerable, opaque_loop, evidence)
    assert role.governing_tag == state_name
    assert tags["StateRequested"].name in role.request_tags


@pytest.mark.xfail(
    reason="pilot: cannot drive the Hold->ack->Unhold->self-issued-Complete detour "
    "while avoiding C_Complete; it reaches Held then declines at the skiff free-word "
    "exit, mis-attributing the enable to the undeclared mask-table neighbor "
    "'PackTbl_A_Alm100'"
)
def test_pilot_table_detour_declines_avoiding_complete() -> None:
    """PILOT should follow the program-owned command detour, not press Complete.

    OBSERVED (``pilot_events``, this fixture at the 100 ms dwell): the pilot engages
    the compass, drives ``C_Start`` -> Execute(6), follows the program's own Hold
    self-advance to Holding(10)/Held(11), then stalls on the
    Hold->ack->Unhold->self-issued-Complete handshake and reaches the skiff free-word
    stuck exit, NAMING the neighbor exactly as the real project mis-attributes::

        pilot: unreachable - frontier PackTbl_State=17 is gated by free word
          'PackTbl_A_Alm100' (external, no declared domain); the skiff has no sound
          probe values for it. Declare choices= (or min=/max=) on PackTbl_A_Alm100 ...

    (``Plan.skiff_decline`` carries the same text; ``Plan.avoid_names`` names
    ``PackTbl_C_Complete``.)  The alarm word is a red herring — the premise test
    reaches Completed by hand without ever touching it.  This mirrors the real
    tumbler's honest gap (Phase K in ``pilot/CLAUDE.md``): reaching Completed is a
    *drive* problem — survive a multi-stage SFC progression through a deliberate
    EXECUTE->HELD->EXECUTE detour to a self-issued terminal command — not the
    free-word suppression project.  At a LONGER dwell (200 ms, beyond the skiff's
    probe window) the pilot instead budget-exhausts on the avoided command
    (``budget exhausted: avoid excludes PackTbl_C_Complete``) before reaching that
    exit.  Either way the target is unreachable-by-pilot, so this stays xfail.
    """
    logic, tags = _packml_table_detour_program()
    plc = PLC(logic, dt=0.010)

    path = plc.how(tags["State"] == tags["Completed"], avoid=tags["C_Complete"], max_scans=300)

    assert path.reachable
    assert path.changes.get(tags["C_Start"].name) is True
    assert path.changes.get(tags["InterlockAck"].name) is True
    assert path.changes.get(tags["C_Complete"].name) is not True
    assert path.replay().state.tags[tags["State"].name] == tags["Completed"]
