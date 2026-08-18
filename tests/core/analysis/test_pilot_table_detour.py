"""PILOT gate: a PackML command/state fixture that ARMS the opaque-loop / table
surface (unlike the sibling ``_auto_complete_command_program`` in ``test_pilot.py``,
which uses PLAIN copies and never arms it).

Why a second fixture.  The sibling program models the same PackML command detour
(Start -> Execute, a program-owned Hold -> Held, an operator ack -> Unhold, a
program-owned Complete -> Completed) but writes ``State`` with a plain
``copy(StateRequested, State)``. With no indirect-copy source, ``pipeline_graph.detect_
opaque_loop`` / ``detect_opaque_pipelines`` (``pilot/pipeline_graph.py``) return empty, so
the compass value-graph, the constant-table mask tide tables, and the
state-consistent pinning machinery all stay dormant.  This fixture reproduces the
real tumbler's shape that *does* arm them:

  * a constant **jump table** ``JT[150 + StateRequested]`` read by an INDIRECT copy
    (``copy(JT[JumpIdx], JumpTarget)``) — the opaque hop that puts ``State`` in
    ``detect_opaque_loop``.  ``State`` itself is written by a plain
    ``copy(StateRequested, State)`` so it stays copy-coupled *stepping* (the prover's
    ``_compute_stepping_tags`` only forwards stepping through a NAMED copy source — an
    ``IndirectRef`` source would drop ``State`` from the stepping set and collapse the
    whole compass; see "DSL/pilot limitations" below);
  * a **constant-table mask enable** ``StateMask[300+StateRequested] &
    DisabledStates[200+Mode] == 0`` gating the transition (the ``tide_tables`` shape);
  * a **free, undeclared, externally-writable neighbor word** ``PackTbl_A_Alm100`` at
    ``MT[300]`` — one offset below the state-mask slots ``MT[301..317]`` — read into an
    alarm interlock that gates ONLY the Completing(16) enable.  It rests at 0 (so the
    machine completes by hand), but the pilot cannot domain a free word, reproducing
    the real project's mis-attribution surface (``how`` there declines naming the
    ``A_Alm*_Status`` neighbor of the ``dh[300+state]`` mask table).

Naming is generic PackML only (ISA-TR88.00.02 numbering) — no process-specific names.

What the pilot actually does (OBSERVED): the pilot engages the compass, drives
``C_Start`` -> Execute(6), follows the program's own Hold self-advance to
Holding(10)/Held(11), then — recognizing the program-owned command detour — presses
the one operator action legal while HELD (``InterlockAck``) and coasts the
program's own Unhold -> Phase-advance -> self-issued Complete all the way to
Completed(17), **never pressing the avoided ``C_Complete``**.  The undeclared
mask-table neighbor (``PackTbl_A_Alm100``) rests at 0 the whole run, so the enable
is satisfied naturally.

This is the **program-awaited action capability** (``pilot/awaited_actions.py``):
when the trace dead-ends on the opaque-loop state register and
the compass route is the avoided command, the pilot recognizes the one operator
push the program is dwelling on at the current ``(state, step)`` and surfaces it as
a fallback bearing — the program executes the detour, the pilot supplies the single
handshake action.
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
    on_delay,
    rise,
    rung,
)

# Dwell for the program-owned Hold/Complete timers.  100 ms (= 10 scans at dt=0.010)
# is short enough that a skiff probe window covers a whole dwell, so the loop reaches
# the skiff's second lap (the ``State`` pair-probe that learns a composite cause) and
# lands on a stuck exit because the undeclared mask-table word cannot be probed
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


def _current_ctx(logic, plc):
    """Build the pdg / steerable / opaque_loop / evidence the recognizer consumes."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.drive_setup import build_prover_context
    from pyrung.core.analysis.pilot.pipeline_graph import detect_opaque_loop
    from pyrung.core.analysis.steerable import compute_steerable

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, logic)
    opaque_loop = detect_opaque_loop(pdg, logic)
    prover = build_prover_context(logic, dict(plc.state.tags))
    return pdg, steerable, opaque_loop, prover.evidence


def _drive_to(plc, tags, state_value):
    plc.patch({tags["C_Start"].name: True})
    plc.step()
    plc.patch({tags["C_Start"].name: False})
    plc.run(cycles=30)
    assert plc.state.tags[tags["State"].name] == state_value, plc.state.tags[tags["State"].name]


def test_awaited_action_recognizes_ack_while_held() -> None:
    """The recognizer surfaces the ONE operator action the program is dwelling on
    at HELD — ``InterlockAck`` — a legal, non-avoided, state-moving push."""
    from pyrung.core.analysis.pilot.awaited_actions import awaited_actions
    from pyrung.core.analysis.pilot.evidence import infer_pipeline_roles
    from pyrung.core.analysis.pilot.types import WorldView

    logic, tags = _packml_table_detour_program()
    plc = PLC(logic, dt=0.010)
    _drive_to(plc, tags, tags["Held"])

    pdg, steerable, opaque_loop, evidence = _current_ctx(logic, plc)
    role = infer_pipeline_roles(tags["State"].name, pdg, logic, steerable, opaque_loop, evidence)
    world = WorldView(dict(plc.state.tags), pdg, logic, steerable, opaque_loop, None)

    readings = awaited_actions(world, tags["State"].name, (role,))
    action = next(
        reading for reading in readings if reading.action != (tags["C_Complete"].name, True)
    )
    assert action is not None
    assert action.action == (tags["InterlockAck"].name, True)
    assert action.from_state == tags["Held"]
    # It moves the state off HELD (toward Execute) — the detour's back-leg.
    assert action.to_state == tags["Execute"]


def test_awaited_action_policy_defers_a_command_with_an_automatic_sibling() -> None:
    """Compass must coast/read a program-owned command, not press its twin."""
    from types import SimpleNamespace

    from pyrung.core.analysis.pilot.evidence import infer_pipeline_roles
    from pyrung.core.analysis.pilot.navigation_contracts import TargetSpec
    from pyrung.core.analysis.pilot.options import _awaited_action_bearing

    logic, tags = _packml_table_detour_program()
    plc = PLC(logic, dt=0.010)
    _drive_to(plc, tags, tags["Held"])
    plc.patch({tags["InterlockAck"].name: True})
    plc.step()
    plc.patch({tags["InterlockAck"].name: False})
    plc.run(cycles=2)
    assert plc.state.tags[tags["State"].name] == tags["Execute"]

    pdg, steerable, opaque_loop, evidence = _current_ctx(logic, plc)
    role = infer_pipeline_roles(
        tags["State"].name,
        pdg,
        logic,
        steerable,
        opaque_loop,
        evidence,
    )
    ctx = SimpleNamespace(
        target=TargetSpec(tags["State"].name, tags["Completed"]),
        opaque_loop=opaque_loop,
        pdg=pdg,
        program=logic,
        steerable=steerable,
        domain_prior=None,
        pipeline_roles=(role,),
        blocked_actions=frozenset(),
        avoid_pred=None,
    )

    # Complete is structurally pressable here, but the program's timer owns an
    # automatic producer for that same command value.
    assert (
        _awaited_action_bearing(
            SimpleNamespace(snap=dict(plc.state.tags)),
            ctx,
        )
        is None
    )


def test_awaited_action_reader_returns_structural_execute_readings() -> None:
    """The reader reports structure without deciding whether PILOT should wait."""
    from pyrung.core.analysis.pilot.awaited_actions import awaited_actions
    from pyrung.core.analysis.pilot.evidence import infer_pipeline_roles
    from pyrung.core.analysis.pilot.types import WorldView

    logic, tags = _packml_table_detour_program()
    plc = PLC(logic, dt=0.010)
    _drive_to(plc, tags, tags["Held"])
    # Ack once -> program drives HELD -> Execute; land at Execute before Complete.
    plc.patch({tags["InterlockAck"].name: True})
    plc.step()
    plc.patch({tags["InterlockAck"].name: False})
    plc.run(cycles=2)
    assert plc.state.tags[tags["State"].name] == tags["Execute"]

    pdg, steerable, opaque_loop, evidence = _current_ctx(logic, plc)
    role = infer_pipeline_roles(tags["State"].name, pdg, logic, steerable, opaque_loop, evidence)
    world = WorldView(dict(plc.state.tags), pdg, logic, steerable, opaque_loop, None)

    readings = awaited_actions(world, tags["State"].name, (role,))
    assert any(reading.action == (tags["C_Complete"].name, True) for reading in readings)


def test_awaited_action_reader_does_not_apply_avoid_policy() -> None:
    """Avoid filtering belongs to Compass, not the structural reader."""
    from pyrung.core.analysis.pilot.awaited_actions import awaited_actions
    from pyrung.core.analysis.pilot.evidence import infer_pipeline_roles
    from pyrung.core.analysis.pilot.types import WorldView

    logic, tags = _packml_table_detour_program()
    plc = PLC(logic, dt=0.010)
    _drive_to(plc, tags, tags["Held"])

    pdg, steerable, opaque_loop, evidence = _current_ctx(logic, plc)
    role = infer_pipeline_roles(tags["State"].name, pdg, logic, steerable, opaque_loop, evidence)
    world = WorldView(dict(plc.state.tags), pdg, logic, steerable, opaque_loop, None)

    readings = awaited_actions(world, tags["State"].name, (role,))
    assert any(reading.action == (tags["InterlockAck"].name, True) for reading in readings)


def test_awaited_action_surfaces_ack_as_candidate() -> None:
    """Wait-over-steer ordering: at HELD the pilot's candidate list carries the
    avoided C_Complete route AND the awaited-action-prescribed InterlockAck; the avoided
    command is rejected and the program-awaited action is accepted."""
    from pyrung.core.analysis.pilot.api import pilot_events
    from pyrung.core.runner import _compile_avoid

    logic, tags = _packml_table_detour_program()
    plc = PLC(logic, dt=0.010)

    held_candidates = None
    cur_state = None
    for ev in pilot_events(
        plc,
        tags["State"] == tags["Completed"],
        avoid_pred=_compile_avoid(tags["C_Complete"]),
        max_scans=300,
    ):
        if ev.kind == "iteration":
            cur_state = ev.data.get("snapshot", {}).get(tags["State"].name)
        elif ev.kind == "candidates_built" and cur_state == tags["Held"]:
            held_candidates = ev.data["candidates"]

    assert held_candidates is not None, "no candidates_built at HELD"
    by_tag = {c["tag"]: c for c in held_candidates}
    assert tags["InterlockAck"].name in by_tag
    ack = by_tag[tags["InterlockAck"].name]
    assert ack["awaited_action_prescribed"] is True
    assert ack["awaited_action_note"]  # the recognition rationale is recorded


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
    from pyrung.core.analysis.pilot.drive_setup import build_prover_context
    from pyrung.core.analysis.pilot.evidence import infer_pipeline_roles
    from pyrung.core.analysis.pilot.pipeline_graph import detect_opaque_loop
    from pyrung.core.analysis.steerable import compute_steerable

    logic, tags = _packml_table_detour_program()
    plc = PLC(logic, dt=0.010)
    pdg = build_program_graph(logic)
    state_name = tags["State"].name

    # (1) The indirect jump-table copy puts the state register in the opaque loop.
    opaque_loop = detect_opaque_loop(pdg, logic)
    assert state_name in opaque_loop, sorted(opaque_loop)

    # (2) State stays copy-coupled *stepping* (plain copy source), so the compass
    #     value-graph is built for it rather than the loop dead-ending immediately.
    prover = build_prover_context(logic, dict(plc.state.tags))
    assert prover.evidence is not None
    assert prover.evidence.is_stepping(state_name)

    # (3) The StateRequested -> State transition pipeline is visible.
    steerable = compute_steerable(pdg, plc._known_tags_by_name, logic)
    role = infer_pipeline_roles(
        state_name,
        pdg,
        logic,
        steerable,
        opaque_loop,
        prover.evidence,
    )
    assert role.channel_tag == state_name
    assert tags["StateRequested"].name in role.request_tags


def test_completion_edges_record_program_owned_command_bearings() -> None:
    """Part 1 of the wait-edge arc: each completion edge records its route's
    charted gate pair — the wait's bearing, verbatim.

    The Holding(→10) route's writer is gated on ``Cmd == HOLD_CMD``, the
    Completing(→16) route's on ``Cmd == COMPLETE_CMD``; those recorded pairs
    are the completion.  That the program's own ``rise(HoldTmr.Done)`` /
    ``rise(CompleteTmr.Done)`` producers issue them is Part 2's discovery —
    the sibling trace reads it, record time invents nothing.
    """
    from pyrung.core.analysis.pilot.drive_setup import infer_opaque_pipeline_roles
    from pyrung.core.analysis.pilot.pipeline_graph import build_static_transition_graphs

    HOLD_CMD, COMPLETE_CMD = 4, 10

    logic, tags = _packml_table_detour_program()
    plc = PLC(logic, dt=0.010)
    plc.step()
    pdg, steerable, opaque_loop, evidence = _current_ctx(logic, plc)
    roles = infer_opaque_pipeline_roles(pdg, logic, steerable, opaque_loop, evidence)
    graphs = build_static_transition_graphs(roles, pdg, logic, steerable, opaque_loop, evidence)

    completions = {
        edge.to_value: edge.completion
        for graph in graphs
        if graph.role.channel_tag == tags["State"].name
        for edge in graph.edges
        if edge.action is None and edge.completion
    }
    assert completions[tags["Holding"]] == (("PackTbl_Cmd", HOLD_CMD),)
    assert completions[tags["Completing"]] == (("PackTbl_Cmd", COMPLETE_CMD),)


def test_pilot_table_detour_reaches_completed_avoiding_complete() -> None:
    """PILOT follows the program-owned command detour, never pressing Complete.

    OBSERVED (``pilot_events``, this fixture at the 100 ms dwell): the pilot drives
    ``C_Start`` -> Execute(6), follows the program's own Hold self-advance to
    Holding(10)/Held(11), and there — the compass route is the avoided
    ``C_Complete`` and the backward trace dead-ends on the opaque-loop state
    register — the **program-awaited action** recognizer
    (``pilot/awaited_actions.py``)
    surfaces the one operator action legal while HELD::

        candidates: [C_Complete (route, avoided -> rejected),
                     InterlockAck (awaited_action_prescribed)]

    Pressing ``InterlockAck`` sets ``Phase = 1`` and issues the program's Unhold;
    the settle-coast then rides the program's own Unhold -> Execute(6) ->
    self-issued Complete -> Completing(16) -> Completed(17), reaching the target
    without ever pressing ``C_Complete``.  The undeclared mask-table neighbor
    ``PackTbl_A_Alm100`` rests at 0 throughout, so the enable is satisfied
    naturally. Reaching Completed is therefore a drive problem, not a free-word
    suppression problem.

    ``C_Start`` records ``False`` in ``path.changes`` (not ``True``): it is a
    momentary start command, released by the convergence-command pulse when the
    later ``InterlockAck`` fires — exactly as the hand route un-patches it (see the
    premise test).  Its presence in ``changes`` records that the pilot pressed it;
    the replay reaching Completed proves the drive ran through it (Idle -> Execute
    is reachable only via Start).
    """
    logic, tags = _packml_table_detour_program()
    plc = PLC(logic, dt=0.010)

    path = plc.how(tags["State"] == tags["Completed"], avoid=tags["C_Complete"], max_scans=300)

    assert path.reachable
    # The pilot pressed Start to enter the recipe (a momentary command later
    # released by the convergence pulse, so its net value is False).
    assert tags["C_Start"].name in path.changes
    assert path.changes.get(tags["InterlockAck"].name) is True
    assert path.changes.get(tags["C_Complete"].name) is not True
    assert path.replay().state.tags[tags["State"].name] == tags["Completed"]
