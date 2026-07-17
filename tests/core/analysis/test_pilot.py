"""Integration tests for PILOT loop (pilot_how / pilot_drive).

Tests are organized in three sections:
1. Core PILOT tests (direct pilot_how/pilot_drive calls)
2. engine="pilot" parity tests (same programs as test_walk_how_e2e)
3. Real-pattern parity tests (same programs as test_walk_real_patterns)
"""

from __future__ import annotations

from pyrung import (
    PLC,
    And,
    Block,
    Bool,
    Int,
    Or,
    Physical,
    Program,
    Rung,
    TagType,
    Timer,
    calc,
    call,
    copy,
    latch,
    on_delay,
    out,
    reset,
    return_early,
    rise,
    rung,
    subroutine,
)
from pyrung.core.analysis.pilot import pilot_drive, pilot_events, pilot_how


def _replay(prog: Program, path) -> PLC:
    """Replay a path on a fresh PLC and return it."""
    return path.replay()


# ===================================================================
# Section 1: Core PILOT tests
# ===================================================================


def _auto_complete_command_program() -> tuple[Program, dict[str, object]]:
    """Small command-register program with both user and program-owned writers."""
    # True PackML numbering (ISA-TR88.00.02) for CtrlCommand and CurrentState.
    IDLE = 4
    EXECUTE = 6
    HELD = 11
    COMPLETED = 17
    START_CMD = 2
    HOLD_CMD = 4
    UNHOLD_CMD = 5
    COMPLETE_CMD = 10

    C_Start = Bool("AutoCmd_C_Start", external=True)
    ResumeAck = Bool("AutoCmd_ResumeAck", external=True)
    C_Complete = Bool("AutoCmd_C_Complete", external=True)
    Cmd = Int(
        "AutoCmd_Cmd",
        choices={
            0: "Undefined",
            1: "Reset",
            START_CMD: "Start",
            3: "Stop",
            HOLD_CMD: "Hold",
            UNHOLD_CMD: "Unhold",
            6: "Suspend",
            7: "Unsuspend",
            8: "Abort",
            9: "Clear",
            COMPLETE_CMD: "Complete",
        },
    )
    CmdReq = Int("AutoCmd_CmdReq")
    State = Int(
        "AutoCmd_State",
        default=IDLE,
        choices={
            0: "Undefined",
            1: "Clearing",
            2: "Stopped",
            3: "Starting",
            IDLE: "Idle",
            5: "Suspended",
            EXECUTE: "Execute",
            7: "Stopping",
            8: "Aborting",
            9: "Aborted",
            10: "Holding",
            HELD: "Held",
            12: "Unholding",
            13: "Suspending",
            14: "Unsuspending",
            15: "Resetting",
            16: "Completing",
            COMPLETED: "Completed",
        },
    )
    StateRequested = Int("AutoCmd_StateRequested")
    Phase = Int("AutoCmd_Phase")
    CmdStartRef = Int("AutoCmd_CmdStartRef", readonly=True, default=START_CMD)
    CmdHoldRef = Int("AutoCmd_CmdHoldRef", readonly=True, default=HOLD_CMD)
    CmdUnholdRef = Int("AutoCmd_CmdUnholdRef", readonly=True, default=UNHOLD_CMD)
    CmdCompleteRef = Int("AutoCmd_CmdCompleteRef", readonly=True, default=COMPLETE_CMD)
    HoldTmr = Timer.clone("AutoCmd_HoldTmr")
    CompleteTmr = Timer.clone("AutoCmd_CompleteTmr")

    with Program() as logic:
        with rung(C_Start):
            copy(CmdStartRef, Cmd)
            copy(1, CmdReq)

        with rung(C_Complete):
            copy(CmdCompleteRef, Cmd)
            copy(1, CmdReq)

        with rung(State == EXECUTE, Phase == 0):
            on_delay(HoldTmr, 30, "ms")

        with rung(rise(HoldTmr.Done)):
            copy(CmdHoldRef, Cmd)
            copy(1, CmdReq)

        with rung(State == HELD, ResumeAck):
            copy(CmdUnholdRef, Cmd)
            copy(1, CmdReq)
            copy(1, Phase)

        with rung(State == EXECUTE, Phase == 1):
            on_delay(CompleteTmr, 30, "ms")

        with rung(rise(CompleteTmr.Done)):
            copy(CmdCompleteRef, Cmd)
            copy(1, CmdReq)

        with rung(CmdReq == 1, Cmd == START_CMD, State == IDLE):
            copy(EXECUTE, StateRequested)

        with rung(CmdReq == 1, Cmd == HOLD_CMD, State == EXECUTE):
            copy(HELD, StateRequested)

        with rung(CmdReq == 1, Cmd == UNHOLD_CMD, State == HELD):
            copy(EXECUTE, StateRequested)

        with rung(CmdReq == 1, Cmd == COMPLETE_CMD, State == EXECUTE):
            copy(COMPLETED, StateRequested)

        with rung(StateRequested != 0):
            copy(StateRequested, State)
            copy(0, StateRequested)
            copy(0, Cmd)
            copy(0, CmdReq)

    return logic, {
        "C_Start": C_Start,
        "ResumeAck": ResumeAck,
        "C_Complete": C_Complete,
        "Cmd": Cmd,
        "State": State,
        "Held": HELD,
        "Execute": EXECUTE,
        "Completed": COMPLETED,
    }


def test_auto_complete_command_premise() -> None:
    """A start plus resume ack can reach Completed without pressing Complete."""
    logic, tags = _auto_complete_command_program()
    plc = PLC(logic, dt=0.010)

    plc.patch({tags["C_Start"].name: True})
    plc.step()
    plc.patch({tags["C_Start"].name: False})
    plc.run(cycles=8)
    assert plc.state.tags[tags["State"].name] == tags["Held"]

    plc.patch({tags["ResumeAck"].name: True})
    plc.step()
    plc.patch({tags["ResumeAck"].name: False})
    plc.run(cycles=8)

    assert plc.state.tags[tags["State"].name] == tags["Completed"]
    assert plc.state.tags[tags["C_Complete"].name] is False


def test_trace_surfaces_resume_ack_when_execute_is_needed_from_held() -> None:
    """Hint for the detour failure: Held -> Execute is ResumeAck, not Start."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.trace import trace_back
    from pyrung.core.analysis.steerable import compute_steerable

    logic, tags = _auto_complete_command_program()
    plc = PLC(logic, dt=0.010)

    plc.patch({tags["C_Start"].name: True})
    plc.step()
    plc.patch({tags["C_Start"].name: False})
    plc.run(cycles=8)
    assert plc.state.tags[tags["State"].name] == tags["Held"]

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, logic)
    tree = trace_back(
        tags["State"].name,
        tags["Execute"],
        dict(plc.state.tags),
        pdg,
        logic,
        steerable,
    )

    actions = tree.ordered_actions()
    assert (tags["ResumeAck"].name, True) in actions, actions
    assert (tags["C_Start"].name, True) not in actions, actions


def test_pilot_reaches_completed_through_program_owned_command_detour() -> None:
    """PILOT should follow the program-owned command detour, not press Complete.

    The intended route is ``C_Start`` -> Execute, let the program request Held,
    press ``ResumeAck`` so the program requests Unhold, then let the program issue
    Complete.  Today the steerable ``C_Complete`` writer wins ``Cmd == Complete``
    and the walk breaks before ever expanding the timer-owned producer, so the
    route is invisible once ``C_Complete`` is avoided; and even a visible route
    needs the loop to accept the program's Execute -> Held self-advance as
    en-route rather than reverting it as a regression.
    """
    logic, tags = _auto_complete_command_program()
    plc = PLC(logic, dt=0.010)

    path = plc.how(tags["State"] == tags["Completed"], avoid=tags["C_Complete"], max_scans=300)

    assert path.reachable
    assert path.changes.get(tags["C_Start"].name) is True
    assert path.changes.get(tags["ResumeAck"].name) is True
    assert path.changes.get(tags["C_Complete"].name) is not True
    assert path.replay().state.tags[tags["State"].name] == tags["Completed"]


def test_simple_latch():
    x_Go = Bool("x_Go", external=True)
    y_Out = Bool("y_Out")

    with Program() as logic:
        with rung(x_Go):
            out(y_Out)

    plc = PLC(logic)
    path = pilot_how(plc, y_Out)

    assert path.reachable
    assert path.total_changes >= 1
    assert "x_Go" in path.changes


def test_pilot_events_stream_candidate_decisions():
    x_Go = Bool("x_Go", external=True)
    y_Out = Bool("y_Out")

    with Program() as logic:
        with rung(x_Go):
            out(y_Out)

    plc = PLC(logic)
    events = list(pilot_events(plc, y_Out))
    kinds = [event.kind for event in events]

    assert "started" in kinds
    assert "iteration" in kinds
    assert "candidates_built" in kinds
    assert "candidate_try" in kinds
    assert "candidate_accepted" in kinds
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True
    accepted = next(event for event in events if event.kind == "candidate_accepted")
    assert accepted.data["changes"]["total"]
    assert accepted.data["accepted_because"]["target_reached"] is True


def test_bool_output_routes_report_and_redirect():
    """Burner latches via ``Or(ProdMode, MaintMode)`` — both internal coils, so
    there are two material routes.  how() never reports ambiguous: it takes a
    deterministic default (the first arm, ProdMode) and records it on
    ``Path.route``, naming the route not taken; ``avoid=``/``via=`` redirect onto
    the maintenance route."""
    ProdCmd = Bool("ProdCmd", external=True)
    MaintCmd = Bool("MaintCmd", external=True)
    Mode = Int("Mode")
    ProdMode = Bool("ProdMode")
    MaintMode = Bool("MaintMode")
    Burner = Bool("Burner")

    with Program() as logic:
        with rung(ProdCmd):
            copy(1, Mode)
        with rung(MaintCmd):
            copy(2, Mode)
        with rung(Mode == 1):
            out(ProdMode)
        with rung(Mode == 2):
            out(MaintMode)
        with rung(Or(ProdMode, MaintMode)):
            out(Burner)

    plc = PLC(logic)

    # Default route: reachable, names ProdMode, surfaces MaintMode for redirect.
    default = plc.how(Burner)
    assert default.reachable
    assert default.route is not None
    assert not default.route.dominant
    assert "ProdMode" in default.route.label
    alternatives = default.route.pivots[0].alternatives
    assert any("MaintMode" in alt.label for alt in alternatives)
    assert default.changes.get("ProdCmd") is True

    # via= redirects onto the maintenance route.
    via = plc.how(Burner, via=MaintMode)
    assert via.reachable
    assert via.route is not None and "MaintMode" in via.route.label
    assert via.changes.get("MaintCmd") is True
    assert via.changes.get("ProdCmd") is not True

    # avoid= steers off the production route to the same place.
    avoided = plc.how(Burner, avoid=ProdMode)
    assert avoided.reachable
    assert avoided.route is not None and "MaintMode" in avoided.route.label
    assert avoided.changes.get("ProdCmd") is not True


def test_word_target_routes_report_and_redirect():
    """A word target (``State == 5``) gets the same route report + redirect as a
    Bool: ``copy(5, State)`` is gated ``Or(ProdMode, MaintMode)`` — two internal
    coils, two material routes.  how() takes the default (ProdMode) and records
    the MaintMode route; ``via=``/``avoid=`` redirect onto it.  Mirrors
    ``test_bool_output_routes_report_and_redirect`` with a value target."""
    ProdCmd = Bool("ProdCmd", external=True)
    MaintCmd = Bool("MaintCmd", external=True)
    ProdMode = Bool("ProdMode")
    MaintMode = Bool("MaintMode")
    State = Int("State")

    with Program() as logic:
        with rung(ProdCmd):
            out(ProdMode)
        with rung(MaintCmd):
            out(MaintMode)
        with rung(Or(ProdMode, MaintMode)):
            copy(5, State)

    plc = PLC(logic)

    default = plc.how(State == 5)
    assert default.reachable
    assert default.route is not None
    assert not default.route.dominant
    assert "ProdMode" in default.route.label
    alternatives = default.route.pivots[0].alternatives
    assert any("MaintMode" in alt.label for alt in alternatives)
    assert default.changes.get("ProdCmd") is True

    via = plc.how(State == 5, via=MaintMode)
    assert via.reachable
    assert via.route is not None and "MaintMode" in via.route.label
    assert via.changes.get("MaintCmd") is True
    assert via.changes.get("ProdCmd") is not True

    avoided = plc.how(State == 5, avoid=ProdMode)
    assert avoided.reachable
    assert avoided.route is not None and "MaintMode" in avoided.route.label
    assert avoided.changes.get("ProdCmd") is not True


def test_bool_false_target_routes_report_and_redirect():
    """A ``Bool == False`` target routes too: a latched ``Running`` has two reset
    writers (``StopA``, ``StopB``), so clearing it has two routes.  how() takes the
    default (StopA) and records StopB; ``via=``/``avoid=`` redirect onto it.  The
    target starts True (so it is not already satisfied) — otherwise there is
    nothing to route."""
    StartCmd = Bool("StartCmd", external=True)
    StopA = Bool("StopA", external=True)
    StopB = Bool("StopB", external=True)
    Running = Bool("Running")

    with Program() as logic:
        with rung(StartCmd):
            latch(Running)
        with rung(StopA):
            reset(Running)
        with rung(StopB):
            reset(Running)

    plc = PLC(logic)
    plc.force("StartCmd", True)
    plc.step()
    plc.force("StartCmd", False)
    plc.step()
    assert plc.state.tags["Running"] is True

    default = plc.how(Running == False)  # noqa: E712
    assert default.reachable
    assert default.route is not None
    assert not default.route.dominant
    assert "StopA" in default.route.label
    alternatives = default.route.pivots[0].alternatives
    assert any("StopB" in alt.label for alt in alternatives)
    assert default.changes.get("StopA") is True

    via = plc.how(Running == False, via=StopB)  # noqa: E712
    assert via.reachable
    assert via.route is not None and "StopB" in via.route.label
    assert via.changes.get("StopB") is True
    assert via.changes.get("StopA") is not True

    avoided = plc.how(Running == False, avoid=StopA)  # noqa: E712
    assert avoided.reachable
    assert avoided.route is not None and "StopB" in avoided.route.label
    assert avoided.changes.get("StopA") is not True


def test_multi_target_avoid_via_now_supported():
    """``avoid=``/``via=`` combined with a multi-target ``how()`` used to raise;
    now the same route predicate constrains every target's route selection.

    ``Burner`` latches via ``Or(ProdMode, MaintMode)`` (two routes); ``Aux`` is
    independent.  ``how(Burner, Aux, via=MaintMode)`` reaches both and steers
    Burner onto the maintenance route (``MaintCmd``, not ``ProdCmd``); ``avoid=
    ProdMode`` reaches the same place (the avoid gate also vetoes resting with
    ProdMode set)."""
    ProdCmd = Bool("ProdCmd", external=True)
    MaintCmd = Bool("MaintCmd", external=True)
    ProdMode = Bool("ProdMode")
    MaintMode = Bool("MaintMode")
    Burner = Bool("Burner")
    AuxCmd = Bool("AuxCmd", external=True)
    Aux = Bool("Aux")

    with Program() as logic:
        with rung(ProdCmd):
            out(ProdMode)
        with rung(MaintCmd):
            out(MaintMode)
        with rung(Or(ProdMode, MaintMode)):
            out(Burner)
        with rung(AuxCmd):
            out(Aux)

    via = PLC(logic).how(Burner, Aux, via=MaintMode)
    assert via.reachable
    assert via.changes.get("MaintCmd") is True
    assert via.changes.get("ProdCmd") is not True
    assert via.changes.get("AuxCmd") is True

    avoided = PLC(logic).how(Burner, Aux, avoid=ProdMode)
    assert avoided.reachable
    assert avoided.changes.get("MaintCmd") is True
    assert avoided.changes.get("ProdCmd") is not True
    assert avoided.changes.get("AuxCmd") is True


def test_equality_gated_coil_single_equality_unchanged():
    """The single-equality mode flag still aliases to its channel register.

    ``out(ManualMode)`` under ``rung(Mode == 3)`` means ``ManualMode=True`` is
    equivalent to ``Mode=3``.  Generalizing the recognizer to value sets must not
    move this case: the alias is now the singleton set ``{3}`` (same conflict
    behavior), and a non-``True`` request never aliases."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.trace import _equality_gated_coil

    Mode = Int("Mode")
    ManualMode = Bool("ManualMode")
    with Program() as prog:
        with rung(Mode == 3):
            out(ManualMode)
    pdg = build_program_graph(prog)

    assert _equality_gated_coil("ManualMode", True, pdg, prog) == ("Mode", frozenset({3}))
    assert _equality_gated_coil("ManualMode", False, pdg, prog) is None


def test_equality_gated_coil_or_of_two_equalities_is_set_valued():
    """A flag gated by an OR of two equalities aliases to the value SET.

    ``out(HiLo)`` under ``rung(Or(Mode == 3, Mode == 5))`` means ``HiLo=True``
    implies ``Mode in {3, 5}`` — the Or-widens branch of the lattice."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.trace import _equality_gated_coil

    Mode = Int("Mode")
    HiLo = Bool("HiLo")
    with Program() as prog:
        with rung(Or(Mode == 3, Mode == 5)):
            out(HiLo)
    pdg = build_program_graph(prog)

    assert _equality_gated_coil("HiLo", True, pdg, prog) == ("Mode", frozenset({3, 5}))


def test_equality_gated_coil_two_writers_agree_union_set():
    """Two ``out`` writers gating the same register union their value sets.

    ``HiLo`` is driven ``True`` from two rungs (``Mode == 3`` and ``Mode == 5``);
    ``HiLo=True`` implies ``Mode`` is in the union ``{3, 5}``."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.trace import _equality_gated_coil

    Mode = Int("Mode")
    Flag = Bool("Flag")
    with Program(strict=False) as prog:
        with rung(Mode == 3):
            out(Flag)
        with rung(Mode == 5):
            out(Flag)
    pdg = build_program_graph(prog)

    assert _equality_gated_coil("Flag", True, pdg, prog) == ("Mode", frozenset({3, 5}))


def test_equality_gated_coil_inequality_returns_none():
    """An inequality-only gate implies no finite channel-value set — no alias.

    Honesty boundary: never fabricate a channel constraint the guard does not
    actually pin."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.trace import _equality_gated_coil

    Mode = Int("Mode")
    Flag = Bool("Flag")
    with Program() as prog:
        with rung(Mode >= 3):
            out(Flag)
    pdg = build_program_graph(prog)

    assert _equality_gated_coil("Flag", True, pdg, prog) is None


def test_equality_gated_coil_writers_disagree_returns_none():
    """Writers that gate *different* registers cannot alias to one channel set."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.trace import _equality_gated_coil

    Mode = Int("Mode")
    Other = Int("Other")
    Flag = Bool("Flag")
    with Program(strict=False) as prog:
        with rung(Mode == 3):
            out(Flag)
        with rung(Other == 2):
            out(Flag)
    pdg = build_program_graph(prog)

    assert _equality_gated_coil("Flag", True, pdg, prog) is None


def test_route_conflict_set_alias_intersection_semantics():
    """A set-valued flag alias clashes only when the needed value is outside the set.

    ``HiLo=True`` implies ``Mode in {3, 5}``.  Beside a sibling pin on ``Mode``:
    ``Mode=1`` is disjoint from ``{3, 5}`` → conflict; ``Mode=3`` intersects it →
    no conflict.  This is the set-intersection test :func:`_route_conflicts`
    performs (empty intersection = contradiction), replacing the old
    two-distinct-scalar-values test."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.trace import TraceNode, _route_conflicts

    Mode = Int("Mode")
    HiLo = Bool("HiLo")
    with Program() as prog:
        with rung(Or(Mode == 3, Mode == 5)):
            out(HiLo)
    pdg = build_program_graph(prog)

    # Needed value (Mode=1) falls outside the flag's implied set {3, 5}.
    clashing = TraceNode("Target", True, children=[TraceNode("HiLo", True), TraceNode("Mode", 1)])
    assert {conflict.tag for conflict in _route_conflicts(clashing, pdg, prog)} == {"Mode"}

    # Needed value (Mode=3) is inside the set — the flag is satisfiable alongside it.
    compatible = TraceNode("Target", True, children=[TraceNode("HiLo", True), TraceNode("Mode", 3)])
    assert not _route_conflicts(compatible, pdg, prog)


def test_route_conflicts_distinguish_value_pairs_on_the_same_tag():
    """Common sequencing noise must not erase a route-specific mode clash.

    Both routes contain the same apparent ``Mode 0 ↔ 1`` sequencing conflict.
    The Manual route additionally requires ``ManualMode=True`` (an alias for
    ``Mode=3``) beside ``Mode=1``.  Tag-only intersection called every Mode
    conflict shared and erased the Manual-only contradiction; structured
    witnesses subtract only the genuinely identical ``0 ↔ 1`` pair.
    """
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.trace import TraceNode, _route_conflicts

    Mode = Int("WitnessMode")
    ManualMode = Bool("WitnessManualMode")
    with Program() as prog:
        with rung(Mode == 3):
            out(ManualMode)
    pdg = build_program_graph(prog)

    common = [TraceNode("WitnessMode", 0), TraceNode("WitnessMode", 1)]
    production = TraceNode("Target", True, children=common)
    manual = TraceNode(
        "Target",
        True,
        children=[*common, TraceNode("WitnessManualMode", True)],
    )

    production_conflicts = _route_conflicts(production, pdg, prog)
    manual_conflicts = _route_conflicts(manual, pdg, prog)
    shared = production_conflicts & manual_conflicts
    manual_only = manual_conflicts - shared

    assert len(shared) == 1
    # Mode=3 conflicts independently with both common pins (0 and 1); neither
    # witness is erased merely because the common conflict uses the same tag.
    assert len(manual_only) == 2
    assert {conflict.tag for conflict in manual_only} == {"WitnessMode"}


def test_or_arm_over_inputs_collapses():
    """An ``Or`` over directly-steerable inputs is not a surfaced choice.

    The latch is gated ``Start ∧ Or(ModeA, ModeB)``; both arms are inputs PILOT
    can assert, so it resolves without an explicit ``choice=`` (contrast
    ``test_bool_output_ambiguous_requires_choice``, whose arms are coils)."""
    Start = Bool("Start", external=True)
    ModeA = Bool("ModeA", external=True)
    ModeB = Bool("ModeB", external=True)
    Run = Bool("Run")

    with Program() as logic:
        with rung(Start, Or(ModeA, ModeB)):
            latch(Run)

    path = pilot_how(PLC(logic), Run)
    assert path.reachable
    assert path.route is None  # collapsed onto the steerable arm — no surfaced fork
    asserted = {tag for tag, val in path.changes.items() if val is True}
    assert "Start" in asserted
    assert asserted & {"ModeA", "ModeB"}  # at least one arm satisfied


def test_or_mixed_steerable_conjunction_arm_collapses():
    """An OR with a *fully-steerable conjunction* arm collapses onto it.

    Mirrors ``examples.click_conveyor``'s DiverterCmd:
    ``Gate ∧ Or(And(State==2, Flag), And(Manual, Btn))``.  The manual arm is all
    inputs, so PILOT takes it without a ``choice=``; the auto arm needs an
    internal ``State``/``Flag`` commitment (a real choice, available via
    ``choice=``) and is not the default.  ``State``/``Flag`` are given writers so
    they are genuinely internal (not free inputs) — the scorer then prefers the
    all-steerable manual arm.  Contrast
    ``test_bool_output_ambiguous_requires_choice``: there *no* arm is steerable,
    so the choice must stay surfaced."""
    Gate = Bool("Gate", external=True)
    Manual = Bool("Manual", external=True)
    Btn = Bool("Btn", external=True)
    Detect = Bool("Detect", external=True)
    SetFlag = Bool("SetFlag", external=True)
    State = Int("State")
    Flag = Bool("Flag")
    Cmd = Bool("Cmd")

    with Program() as logic:
        with rung(Detect):
            copy(2, State)
        with rung(SetFlag):
            latch(Flag)
        with rung(Gate, Or(And(State == 2, Flag), And(Manual, Btn))):
            out(Cmd)

    path = pilot_how(PLC(logic), Cmd)
    assert path.reachable
    assert path.route is None  # collapsed onto the steerable arm — no surfaced fork
    final = path.changes
    assert final.get("Gate") is True
    assert final.get("Manual") is True
    assert final.get("Btn") is True
    assert "Flag" not in final and "State" not in final  # internal arm untouched
    assert _replay(logic, path).state.tags["Cmd"] is True


def test_or_steerable_threshold_arm_collapses():
    """A threshold over a *steerable* analog input is a directly-driveable arm.

    ``Or(And(State==2, Flag), Size > 100)`` collapses onto the threshold arm —
    the trace's inequality levers drive ``Size`` past the cutoff — rather than
    surfacing it as a choice against the internal auto arm."""
    Size = Int("Size", external=True)
    State = Int("State")
    Flag = Bool("Flag")
    Cmd = Bool("Cmd")

    with Program() as logic:
        with rung(Or(And(State == 2, Flag), Size > 100)):
            out(Cmd)

    path = pilot_how(PLC(logic), Cmd)
    assert path.reachable
    assert path.route is None  # collapsed onto the steerable arm — no surfaced fork
    assert _replay(logic, path).state.tags["Cmd"] is True


def test_high_wake_lever_deprioritized_not_dropped():
    """A needed lever with a large wake is tried last, never dropped.

    ``x_Master`` gates a subroutine that writes ``Mode`` plus two dozen broad
    tags, so its downstream write cone dwarfs the median of the tight gates and
    lands over ``wake_cap``.  The old hard filter removed it from the candidate
    list outright, which made ``Target`` (needs ``Mode==1``) silently
    unreachable.  Wake is now an *ordering* effect only: the master
    enable is split off the batch-facing ``trace_actions`` (so it can't poison a
    widening/co-pulse batch) but is still a candidate — sorted to the tail so it
    is tried after every tighter lever, never excluded."""
    x_G1 = Bool("x_G1", external=True)
    x_G2 = Bool("x_G2", external=True)
    x_G3 = Bool("x_G3", external=True)
    x_G4 = Bool("x_G4", external=True)
    x_Master = Bool("x_Master", external=True)
    Mode = Int("Mode")
    Target = Bool("Target")
    broad = [Bool(f"Broad{i}") for i in range(24)]

    @subroutine("ApplyMaster", strict=False)
    def apply_master():
        with rung(x_Master):
            copy(1, Mode)
            for b in broad:
                out(b)

    with Program() as logic:
        with rung():
            call(apply_master)
        with rung(Mode == 1, x_G1, x_G2, x_G3, x_G4):
            out(Target)

    built: list = []
    path = pilot_how(
        PLC(logic),
        Target,
        on_event=lambda ev: built.append(ev) if ev.kind == "candidates_built" else None,
    )

    # First iteration batches all five levers; the high-wake one exceeds the cap.
    first = built[0].data
    assert first["wake_cap"] == 20
    cand_tags = [c["tag"] for c in first["candidates"]]
    # Present (not dropped) and tried last of all — deprioritized, never excluded.
    assert "x_Master" in cand_tags
    assert cand_tags[-1] == "x_Master"
    assert first["candidates"][-1]["wake"] > first["wake_cap"]
    # Split off the batch-facing trace_actions so it can't poison a batch trial.
    assert ("x_Master", True) not in first["trace_actions"]

    # And the target is still reachable — the whole point of not dropping it.
    assert path.reachable
    assert path.changes.get("x_Master") is True
    assert _replay(logic, path).state.tags["Target"] is True


def test_preserve_holds_latch_against_active_reset():
    """Trace surfaces an active opposite-value writer's guard as a preserve hold.

    ``Run`` is latched, but ``reset(Run)`` fires while ``~Healthy`` (NC interlock
    unhealthy by default).  Establishing the latch is not enough — the value is
    clobbered the same scan unless ``Healthy`` is held, which trace must surface
    as a prerequisite of the latch *persisting*."""
    Start = Bool("Start", external=True)
    Healthy = Bool("Healthy", external=True)  # NC interlock: unhealthy (False) at rest
    Run = Bool("Run")

    with Program() as logic:
        with rung(Start):
            latch(Run)
        with rung(~Healthy):
            reset(Run)

    path = pilot_how(PLC(logic), Run)
    assert path.reachable
    final = path.changes
    assert final.get("Start") is True  # establish
    assert final.get("Healthy") is True  # preserve — suppress the active reset

    replay = _replay(logic, path)
    assert replay.state.tags["Run"] is True


def test_two_step_sequential():
    x_A = Bool("x_A", external=True)
    x_B = Bool("x_B", external=True)
    y_Mid = Bool("y_Mid")
    y_Out = Bool("y_Out")

    with Program() as logic:
        with rung(x_A):
            out(y_Mid)
        with rung(y_Mid, x_B):
            out(y_Out)

    plc = PLC(logic)
    path = pilot_how(plc, y_Out)

    assert path.reachable
    assert path.total_changes >= 2


def test_already_satisfied():
    x_Go = Bool("x_Go", external=True)
    y_Out = Bool("y_Out")

    with Program() as logic:
        with rung(x_Go):
            out(y_Out)

    plc = PLC(logic)
    plc.force("x_Go", True)
    plc.step()

    path = pilot_how(plc, y_Out)
    assert path.reachable
    assert path.total_changes == 0


def test_state_machine():
    x_Reset = Bool("x_Reset", external=True)
    x_Start = Bool("x_Start", external=True)
    State = Int("State")
    y_Running = Bool("y_Running")

    with Program() as logic:
        with rung(State == 0, x_Reset):
            copy(1, State)
        with rung(State == 1, x_Start):
            copy(2, State)
        with rung(State == 2):
            out(y_Running)

    plc = PLC(logic)
    path = pilot_how(plc, y_Running)

    assert path.reachable
    assert path.total_changes >= 2


def test_subroutine_call():
    x_Enable = Bool("x_Enable", external=True)
    x_Action = Bool("x_Action", external=True)
    y_Done = Bool("y_Done")

    with Program() as logic:
        with subroutine("work"):
            with rung(x_Action):
                out(y_Done)
        with rung(x_Enable):
            call("work")

    plc = PLC(logic)
    path = pilot_how(plc, y_Done)

    assert path.reachable
    assert "x_Enable" in path.changes
    assert "x_Action" in path.changes


def test_timer_wait():
    x_Start = Bool("x_Start", external=True)
    tmr = Timer.clone("T1")
    y_Complete = Bool("y_Complete")

    with Program() as logic:
        with rung(x_Start):
            on_delay(tmr, preset=10)
        with rung(tmr.Done):
            out(y_Complete)

    plc = PLC(logic)
    path = pilot_how(plc, y_Complete, max_scans=3000)
    assert path.reachable


def test_pilot_drive_live():
    x_Go = Bool("x_Go", external=True)
    y_Out = Bool("y_Out")

    with Program() as logic:
        with rung(x_Go):
            out(y_Out)

    plc = PLC(logic)
    assert plc.state.tags.get("y_Out") is not True

    path = pilot_drive(plc, y_Out)
    assert path.reachable
    assert plc.state.tags.get("y_Out") is True


# ===================================================================
# Section 2: engine="pilot" parity (from test_walk_how_e2e programs)
# ===================================================================


def _simple_latch_prog():
    Start = Bool("Start", external=True)
    Running = Bool("Running")
    with Program() as prog:
        with Rung(Start):
            latch(Running)
    return prog, Start, Running


def _two_step_latch():
    Start = Bool("Start", external=True)
    Confirm = Bool("Confirm", external=True)
    Ready = Bool("Ready")
    Done = Bool("Done")
    with Program() as prog:
        with Rung(Start):
            latch(Ready)
        with Rung(Ready, Confirm):
            latch(Done)
    return prog, Start, Confirm, Ready, Done


def _three_step_program():
    CmdA = Bool("CmdA", external=True)
    CmdB = Bool("CmdB", external=True)
    CmdC = Bool("CmdC", external=True)
    A = Bool("A")
    B = Bool("B")
    C = Bool("C")
    with Program() as prog:
        with Rung(CmdA):
            latch(A)
        with Rung(A, CmdB):
            latch(B)
        with Rung(B, CmdC):
            latch(C)
    return prog, CmdA, CmdB, CmdC, A, B, C


def test_engine_simple_latch():
    prog, Start, Running = _simple_latch_prog()
    plc = PLC(prog, dt=0.010)
    path = plc.how(Running)
    assert path.reachable
    assert path.total_changes > 0


def test_engine_two_step():
    prog, Start, Confirm, Ready, Done = _two_step_latch()
    plc = PLC(prog, dt=0.010)
    path = plc.how(Done)
    assert path.reachable
    assert path.total_changes > 0


def test_engine_three_step():
    prog, CmdA, CmdB, CmdC, A, B, C = _three_step_program()
    plc = PLC(prog, dt=0.010)
    path = plc.how(C)
    assert path.reachable


def test_engine_replay_validates():
    """Every returned path must replay correctly."""
    prog, Start, Confirm, Ready, Done = _two_step_latch()
    plc = PLC(prog, dt=0.010)
    path = plc.how(Done)
    assert path.reachable

    replay = _replay(prog, path)
    assert replay.state.tags["Done"] is True


def test_engine_already_satisfied():
    prog, Start, Running = _simple_latch_prog()
    plc = PLC(prog, dt=0.010)
    plc.patch({"Start": True})
    plc.step()
    assert plc.state.tags["Running"] is True
    path = plc.how(Running)
    assert path.reachable
    assert path.total_changes == 0


def test_engine_from_stepped_state():
    prog, Start, Confirm, Ready, Done = _two_step_latch()
    plc = PLC(prog, dt=0.010)
    plc.patch({"Start": True})
    plc.step()
    assert plc.state.tags["Ready"] is True
    path = plc.how(Done)
    assert path.reachable
    assert path.total_changes > 0


def test_engine_independent_and_dependent():
    """C depends on A (through cone), B is independent."""
    CmdA = Bool("CmdA", external=True)
    CmdB = Bool("CmdB", external=True)
    CmdC = Bool("CmdC", external=True)
    A = Bool("A")
    B = Bool("B")
    C = Bool("C")
    with Program() as prog:
        with Rung(CmdA):
            latch(A)
        with Rung(CmdB):
            latch(B)
        with Rung(A, CmdC):
            latch(C)
    plc = PLC(prog, dt=0.010)
    path = plc.how(C)
    assert path.reachable

    replay = _replay(prog, path)
    assert replay.state.tags["C"] is True


# ===================================================================
# Section 3: Real-pattern parity (from test_walk_real_patterns)
# ===================================================================


def _cmd_protocol_program():
    CmdReset = Bool("CmdReset", external=True)
    CmdStart = Bool("CmdStart", external=True)

    CtrlCmd = Int("CtrlCmd")
    CmdRequest = Int("CmdRequest")
    StateCurrent = Int("StateCurrent")
    StateRequested = Int("StateRequested")
    Output = Bool("Output")

    with Program() as prog:
        with Rung(CmdReset):
            copy(1, CtrlCmd)
            copy(1, CmdRequest)
        with Rung(CmdStart):
            copy(2, CtrlCmd)
            copy(1, CmdRequest)

        with Rung(CmdRequest == 1, CtrlCmd == 1, StateCurrent == 0):
            copy(1, StateRequested)
        with Rung(CmdRequest == 1, CtrlCmd == 2, StateCurrent == 1):
            copy(2, StateRequested)

        with Rung(StateRequested != 0):
            copy(StateRequested, StateCurrent)
            copy(0, StateRequested)
            copy(0, CmdRequest)
            copy(0, CtrlCmd)

        with Rung(StateCurrent == 2):
            out(Output)

    return prog, Output


def test_cmd_protocol():
    """Int command-value protocol: two-step Reset+Start sequence."""
    prog, Output = _cmd_protocol_program()
    plc = PLC(prog)
    path = plc.how(Output)
    assert path.reachable


def _return_early_program():
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


def test_return_early():
    """return_early() flow gating: Enable must be True."""
    prog, Output = _return_early_program()
    plc = PLC(prog)
    path = plc.how(Output)
    assert path.reachable


def _step_sequencer_program():
    Advance = Bool("Advance", external=True)
    CurStep = Int("CurStep", default=1)
    Trans = Int("Trans")
    ValIsOdd = Int("ValIsOdd")
    Output = Bool("Output")

    with Program() as prog:
        with Rung(CurStep == 1, Advance):
            copy(1, Trans)
        with Rung(CurStep == 3):
            out(Output)
        with Rung():
            calc(CurStep % 2, ValIsOdd)
        with Rung(ValIsOdd != 1):
            calc(CurStep + 1, CurStep)
        with Rung(Trans == 1):
            calc(CurStep + 1, CurStep)
            copy(0, Trans)

    return prog, Output


def test_step_sequencer():
    """Odd/even step sequencer with auto-advance."""
    prog, Output = _step_sequencer_program()
    plc = PLC(prog)
    path = plc.how(Output)
    assert path.reachable


def _deep_call_program():
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
        with rung(SfcCall == 0):
            return_early()
        with rung(CurStep == 1):
            on_delay(Tmr, 100, "ms")
        with rung(CurStep == 1, Tmr.Done):
            copy(1, Trans)
        with rung(CurStep == 3, Confirm):
            out(Output)
        with rung():
            calc(CurStep % 2, ValIsOdd)
        with rung(ValIsOdd != 1):
            calc(CurStep + 1, CurStep)
        with rung(Trans == 1):
            calc(CurStep + 1, CurStep)
            copy(0, Trans)

    with Program() as prog:
        with Rung(CmdProd):
            copy(1, Mode)

        with Rung(CmdReset, StateCurrent == 0):
            copy(1, StateRequested)
        with Rung(CmdStart, StateCurrent == 1):
            copy(2, StateRequested)
        with Rung(StateRequested != 0):
            copy(StateRequested, StateCurrent)
            copy(0, StateRequested)

        with Rung(Mode == 1):
            call(production)

        with Rung(SfcCall == 1):
            call(my_sfc)

    return prog, Output


def test_deep_call():
    """Six-level prerequisite chain across three subroutine scopes."""
    prog, Output = _deep_call_program()
    plc = PLC(prog, dt=0.010)
    path = plc.how(Output, max_scans=6000)
    assert path.reachable


# ===================================================================
# Section 4: Writer ranking and gate-movement acceptance
# ===================================================================


def test_writer_ranking_literal_preferred():
    """trace_back prefers Literal writers over generic Affine copies.

    StateCurrent has both a specific Literal writer (copy(6, StateCurrent))
    and a generic Affine writer (copy(StateRequested, StateCurrent)).
    When the Affine writer sorts first by rung index, writer ranking must
    still prefer the Literal — otherwise the trace dead-ends at an
    unreachable intermediate value.
    """
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.trace import trace_back
    from pyrung.core.analysis.steerable import compute_steerable

    CmdClear = Bool("CmdClear", external=True)
    CmdStart = Bool("CmdStart", external=True)
    StateCurrent = Int("StateCurrent", default=9)
    StateRequested = Int("StateRequested")
    Output = Bool("Output")

    @subroutine("apply_state")
    def apply_state():
        with rung(StateRequested != 0):
            copy(StateRequested, StateCurrent)
            copy(0, StateRequested)

    with Program() as prog:
        with rung(CmdClear, StateCurrent == 9):
            copy(1, StateRequested)
        with rung(CmdStart, StateCurrent == 4):
            copy(3, StateRequested)
        with rung():
            call(apply_state)
        with rung(StateRequested == 1):
            copy(2, StateCurrent)
            copy(0, StateRequested)
        with rung(StateRequested == 3):
            copy(6, StateCurrent)
            copy(0, StateRequested)
        with rung(StateCurrent == 6):
            out(Output)

    plc = PLC(prog)
    plc.step()
    snap = dict(plc.state.tags)
    pdg = build_program_graph(prog)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, prog)

    tree = trace_back("StateCurrent", 6, snap, pdg, prog, steerable)

    # The Literal writer (copy(6, StateCurrent) when StateRequested==3)
    # should be selected — NOT the Affine writer (copy(StateRequested, StateCurrent)).
    # With the wrong writer, the trace dead-ends at StateRequested=6 (no producer).
    # With the right writer, it traces to StateRequested==3 → CmdStart.
    actions = tree.ordered_actions()
    assert len(actions) > 0, "trace should find steerable actions, not dead-end"
    action_tags = {t for t, _v in actions}
    assert "CmdStart" in action_tags or "CmdClear" in action_tags


def test_packml_state_sequence():
    """PackML-like state machine: Aborted(9) -> Stopped(2) -> Idle(4) -> Execute(6).

    Each transition requires a command valid only from the current state.
    Transient states auto-complete to the next resting state.
    Tests sequential state discovery through writer ranking and
    gate-movement acceptance.
    """
    CmdClear = Bool("CmdClear", external=True)
    CmdReset = Bool("CmdReset", external=True)
    CmdStart = Bool("CmdStart", external=True)
    StateCurrent = Int("StateCurrent", default=9)
    StateRequested = Int("StateRequested")
    Output = Bool("Output")

    with Program() as prog:
        with rung(CmdClear, StateCurrent == 9):
            copy(1, StateRequested)
        with rung(CmdReset, StateCurrent == 2):
            copy(15, StateRequested)
        with rung(CmdStart, StateCurrent == 4):
            copy(3, StateRequested)

        with rung(StateRequested == 1):
            copy(2, StateCurrent)
            copy(0, StateRequested)
        with rung(StateRequested == 15):
            copy(4, StateCurrent)
            copy(0, StateRequested)
        with rung(StateRequested == 3):
            copy(6, StateCurrent)
            copy(0, StateRequested)

        with rung(StateRequested != 0):
            copy(StateRequested, StateCurrent)
            copy(0, StateRequested)

        with rung(StateCurrent == 6):
            out(Output)

    plc = PLC(prog)
    path = pilot_how(plc, Output, max_scans=3000)
    assert path.reachable
    assert path.total_changes >= 3


def test_gate_movement_acceptance():
    """PILOT accepts NEUTRAL actions that move watched gate tags.

    Five-step state machine: 0 -> 1 -> 2 -> 3 -> 4 -> 5.
    Each step requires a command valid only from the current state.
    Even if distance doesn't improve on a step, gate-tag movement
    (State changing) should be accepted so PILOT can re-trace from
    the new state and discover the next command.
    """
    Cmd0 = Bool("Cmd0", external=True)
    Cmd1 = Bool("Cmd1", external=True)
    Cmd2 = Bool("Cmd2", external=True)
    Cmd3 = Bool("Cmd3", external=True)
    Cmd4 = Bool("Cmd4", external=True)
    State = Int("State", default=0)
    Output = Bool("Output")

    with Program() as prog:
        with rung(State == 0, Cmd0):
            copy(1, State)
        with rung(State == 1, Cmd1):
            copy(2, State)
        with rung(State == 2, Cmd2):
            copy(3, State)
        with rung(State == 3, Cmd3):
            copy(4, State)
        with rung(State == 4, Cmd4):
            copy(5, State)
        with rung(State == 5):
            out(Output)

    plc = PLC(prog)
    path = pilot_how(plc, Output, max_scans=3000)
    assert path.reachable
    assert path.total_changes >= 5


# ===================================================================
# Section 5: Harness integration
# ===================================================================


def test_harness_feedback_excluded_from_steerable():
    """Feedback tags with Physical+link are driven by the Harness, not PILOT.

    Program: x_Go enables o_Motor, x_MotorFB (linked to o_Motor) gates y_Done.
    PILOT must NOT steer x_MotorFB directly — the Harness synthesizes it
    after o_Motor goes True.
    """
    x_Go = Bool("x_Go", external=True)
    o_Motor = Bool("o_Motor")
    x_MotorFB = Bool(
        "x_MotorFB",
        external=True,
        physical=Physical("MotorFb", on_delay="0ms", off_delay="0ms"),
        link="o_Motor",
    )
    y_Done = Bool("y_Done")

    with Program() as logic:
        with rung(x_Go):
            out(o_Motor)
        with rung(o_Motor, x_MotorFB):
            out(y_Done)

    plc = PLC(logic, dt=0.010)
    path = pilot_how(plc, y_Done)

    assert path.reachable
    assert "x_MotorFB" not in path.changes, "PILOT should not steer x_MotorFB — Harness owns it"


# ===================================================================
# Section 6: Influence mapping (Layer 6)
# ===================================================================


def test_compass_bfs_shortest_path():
    """BFS finds the shortest action sequence through a transition table."""
    from pyrung.core.analysis.pilot.compass import Compass, CompassObservation

    inf = Compass()
    tag = "State"
    action_a = ("Cmd", 1)
    action_b = ("Cmd", 2)
    action_c = ("Cmd", 3)
    action_d = ("Recipe", 7)
    inf, changed = inf.apply(
        (
            CompassObservation("edge", tag, action_a, 0, 1),
            CompassObservation("edge", tag, action_b, 1, 2),
            CompassObservation("edge", tag, action_c, 2, 3),
            CompassObservation("edge", tag, action_d, 0, 3),
        )
    )
    assert changed

    path = inf.find_path(tag, 0, 3)
    assert path == [action_d], f"BFS should find direct path, got {path}"

    path_long = inf.find_path(tag, 0, 2)
    assert path_long == [action_a, action_b], f"Should find 2-step path, got {path_long}"

    assert inf.find_path(tag, 0, 99) is None


def test_compass_paths_include_wait_transitions():
    """WAIT is a transition cause, but not a candidate action."""
    from pyrung.core.analysis.pilot.compass import WAIT, Compass, CompassObservation

    inf = Compass()
    tag = "State"
    action_a = ("Cmd", "clear")
    action_b = ("Cmd", "start")
    action_bad = ("Cmd", "abort")
    inf, _ = inf.apply(
        (
            CompassObservation("edge", tag, action_a, 9, 1),
            CompassObservation("edge", tag, WAIT, 1, 2),
            CompassObservation("edge", tag, action_b, 2, 6),
            CompassObservation("edge", tag, action_bad, 1, 9),
        )
    )

    assert inf.find_path(tag, 9, 6) == [action_a, WAIT, action_b]
    assert inf.find_path(tag, 1, 6) == [WAIT, action_b]
    assert inf.off_path_actions(tag, 1, 6) == {action_bad}


def test_unprobed_actions_sorts_mixed_flat_and_composite_causes():
    """unprobed_actions must not crash when the action set mixes a flat
    ``Action`` ``(tag, value)`` with a skiff-learned composite pair-probe
    cause ``((tag, value), (tag, value))`` — sorting the raw tuples compares
    element 0 of a flat action (a ``str``) against element 0 of a composite
    (a ``tuple``), which used to raise ``TypeError`` (crash observed live
    during skiff pair-probing at a Held state).
    """
    from pyrung.core.analysis.pilot.compass import Compass, CompassObservation

    inf = Compass()
    tag = "State"
    from_val = 0

    flat_probed = ("Cmd", 1)
    composite_probed = (("C_Start", True), ("InterlockAck", True))
    inf, _ = inf.apply(
        (
            CompassObservation("edge", tag, flat_probed, from_val, 1),
            CompassObservation("edge", tag, composite_probed, from_val, 2),
        )
    )

    flat_unprobed = ("Cmd", 2)
    composite_unprobed = (("C_Start", True), ("InterlockAck", False))

    available = {flat_probed, composite_probed, flat_unprobed, composite_unprobed}

    result = inf.unprobed_actions(tag, from_val, available)

    # No exception, and the already-probed causes (flat or composite) are
    # excluded regardless of shape.
    assert flat_probed not in result
    assert composite_probed not in result

    # Deterministic order: "C_Start" < "Cmd" lexicographically, so the
    # composite cause sorts before the flat one under the canonicalized key.
    assert result == [composite_unprobed, flat_unprobed]

    # Repeating the call is byte-for-byte the same — no ordering flakiness.
    assert inf.unprobed_actions(tag, from_val, available) == result


# upstream_candidates unit tests removed — function deleted with BFS search cut.


def test_detect_opaque_pipeline():
    """detect_opaque_pipelines finds indirect-copy targets and their steerable inputs."""
    from pyrung.click import ClickBlocks
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.charts import detect_opaque_pipelines
    from pyrung.core.analysis.steerable import compute_steerable

    x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

    CmdA = Bool("CmdA", external=True)
    CmdB = Bool("CmdB", external=True)
    CmdC = Bool("CmdC", external=True)
    CmdReg = Int("CmdReg")
    Pointer = Int("Pointer")
    Scratch = Int("Scratch")
    State = Int("State", default=0)
    Output = Bool("Output")

    ds.slot(10, name="val_A", default=10)
    ds.slot(11, name="val_B", default=20)
    ds.slot(12, name="val_C", default=30)

    @subroutine("ApplyState")
    def apply_state():
        with rung():
            calc(CmdReg + 10, Pointer)
        with rung():
            copy(ds[Pointer], Scratch)
        with rung(Scratch != 0):
            copy(Scratch, State)
            copy(0, CmdReg)

    with Program() as prog:
        with rung(CmdA):
            copy(0, CmdReg)
        with rung(CmdB):
            copy(1, CmdReg)
        with rung(CmdC):
            copy(2, CmdReg)
        with rung(CmdReg != 0):
            call(apply_state)
        with rung(State == 30):
            out(Output)

    pdg = build_program_graph(prog)
    plc = PLC(prog)
    plc.step()
    steerable = compute_steerable(pdg, plc._known_tags_by_name, prog)

    slices = detect_opaque_pipelines(pdg, prog, steerable)
    assert len(slices) >= 1, "Should detect the indirect copy pipeline"

    all_action_tags = frozenset().union(*(s.action_tags for s in slices))
    assert {"CmdA", "CmdB", "CmdC"} <= all_action_tags, (
        f"Should find steerable action tags, got {all_action_tags}"
    )


def test_single_calc_source_multi_tag_constant_base():
    """_single_calc_source hops through calc(CmdReg + Base, Pointer) when Base is constant."""
    from pyrung.click import ClickBlocks
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.trace import _single_calc_source

    x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

    Base = Int("Base", default=10)
    CmdReg = Int("CmdReg")
    Pointer = Int("Pointer")

    @subroutine("ApplyMode")
    def apply_mode():
        with rung():
            calc(CmdReg + Base, Pointer)

    with Program() as prog:
        with rung(Bool("Go", external=True)):
            copy(1, CmdReg)
        with rung():
            call(apply_mode)

    pdg = build_program_graph(prog)
    result = _single_calc_source("Pointer", pdg, prog)
    assert result is not None, (
        "_single_calc_source should hop through calc(CmdReg + Base) when Base has no writers"
    )
    expr, src_tag = result
    assert src_tag == "CmdReg", f"Should identify CmdReg as the mutable source, got {src_tag}"


def test_single_calc_source_rejects_two_mutable_tags():
    """_single_calc_source rejects calc(A + B, Pointer) when both A and B are mutable."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.trace import _single_calc_source

    A = Int("A")
    B = Int("B")
    Pointer = Int("Pointer")

    with Program() as prog:
        with rung(Bool("Go", external=True)):
            copy(1, A)
            copy(5, B)
        with rung():
            calc(A + B, Pointer)

    pdg = build_program_graph(prog)
    result = _single_calc_source("Pointer", pdg, prog)
    assert result is None, (
        "_single_calc_source should reject when both tags in the expression are mutable"
    )


def test_invert_indirect_multi_tag_pointer():
    """_invert_indirect follows calc(CmdReg + Base, Pointer) to CmdReg when Base is constant."""
    from pyrung.click import ClickBlocks
    from pyrung.core.analysis.pdg import build_program_graph, resolve_rung
    from pyrung.core.analysis.pilot.trace import _invert_indirect

    x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

    Base = Int("Base", default=10)
    CmdReg = Int("CmdReg")
    Pointer = Int("Pointer")
    Scratch = Int("Scratch")

    ds.slot(10, name="mode_0", default=0)
    ds.slot(11, name="mode_a", default=1)
    ds.slot(12, name="mode_prod", default=3)

    @subroutine("ApplyMode")
    def apply_mode():
        with rung():
            calc(CmdReg + Base, Pointer)
        with rung():
            copy(ds[Pointer], Scratch)

    with Program() as prog:
        with rung(Bool("Go", external=True)):
            copy(2, CmdReg)
        with rung():
            call(apply_mode)

    pdg = build_program_graph(prog)
    plc = PLC(prog)
    plc.step()
    snapshot = dict(plc.state.items())

    # Find the rung that writes Scratch via copy(ds[Pointer], Scratch)
    writer_ids = pdg.writers_of.get("Scratch", frozenset())
    ro = None
    for wi in sorted(writer_ids):
        ro = resolve_rung(prog, pdg.rung_nodes[wi])
        if ro is not None:
            break
    assert ro is not None

    result = _invert_indirect(ro, "Scratch", 3, snapshot, pdg, prog)
    assert result is not None, (
        "_invert_indirect should invert through calc(CmdReg + Base) to find CmdReg values"
    )
    idx_tag, vals = result
    assert idx_tag == "CmdReg", f"Should resolve to CmdReg, got {idx_tag}"
    assert 2 in vals, f"CmdReg=2 should produce ds[12]=3, got {vals}"


def test_canonical_index_source_hops_constant_base_calc():
    """evidence._canonical_index_source hops calc(CmdReg + Base, Pointer) to CmdReg.

    Regression for the divergent ``_single_calc_source``: evidence now shares the
    trace (constant-tolerant) definition, so it hops through a calc that names a
    constant (Base) beside the single mutable source (CmdReg), and binds Base to
    its default so the address evaluator resolves without a live snapshot.
    """
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.evidence import _canonical_index_source

    Base = Int("Base", default=10)
    CmdReg = Int("CmdReg")
    Pointer = Int("Pointer")

    @subroutine("ApplyMode")
    def apply_mode():
        with rung():
            calc(CmdReg + Base, Pointer)

    with Program() as prog:
        with rung(Bool("Go", external=True)):
            copy(2, CmdReg)
        with rung():
            call(apply_mode)

    pdg = build_program_graph(prog)
    tag, eval_addr = _canonical_index_source("Pointer", lambda v: int(v), pdg, prog, None)
    assert tag == "CmdReg", f"should hop through constant-base calc to CmdReg, got {tag}"
    # CmdReg=2 with the constant Base bound to its default (10) → address 12.
    assert int(eval_addr(2)) == 12, f"eval_addr should bind Base default (10), got {eval_addr(2)}"


def test_expand_routes_punts_on_aggregate_writer():
    """expand_routes drops an aggregate writer (sum over a block) as a route source.

    An aggregate produces a runtime sum with no static destination value, so route
    expansion (value navigation) has nothing to seed a compass edge with.  The
    honest punt: no route, no crash.
    """
    from pyrung.click import ClickBlocks
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.evidence import expand_routes

    x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

    Total = Int("Total")

    with Program() as prog:
        with rung(Bool("Go", external=True)):
            calc(ds.select(1, 5).sum(), Total)

    pdg = build_program_graph(prog)
    routes = expand_routes("Total", pdg, prog, frozenset({"Go"}), frozenset())
    assert routes == [], f"aggregate writer should yield no value-nav route, got {routes}"


def test_l6_probe_with_trace_context():
    """L6 probes include trace-known inputs as context for opaque pipelines.

    The mode pipeline uses a two-tag pointer calc (CmdReg + Base), so the
    backward trace cannot invert the IndirectRef — CmdProd is only
    discoverable through L6 convergent steer detection. The command rungs
    require both CmdProd AND rise(Enable). Enable is in the trace's
    ordered_actions (needed for the output rung), but probing CmdProd
    alone never triggers rise(Enable). The L6 probe must apply trace
    context to discover the Mode transition.
    """
    from pyrung.click import ClickBlocks

    x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

    Enable = Bool("Enable", external=True)
    CmdA = Bool("CmdA", external=True)
    CmdProd = Bool("CmdProd", external=True)
    Base = Int("Base", default=10)
    CmdReg = Int("CmdReg")
    Pointer = Int("Pointer")
    Scratch = Int("Scratch")
    Mode = Int("Mode", default=0)
    Output = Bool("Output")

    ds.slot(10, name="mode_0", default=0)
    ds.slot(11, name="mode_a", default=1)
    ds.slot(12, name="mode_prod", default=3)

    @subroutine("ApplyMode")
    def apply_mode():
        with rung():
            calc(CmdReg + Base, Pointer)
        with rung():
            copy(ds[Pointer], Scratch)
        with rung(Scratch != 0):
            copy(Scratch, Mode)
            copy(0, CmdReg)
            copy(0, Scratch)

    with Program() as prog:
        with rung(rise(Enable), CmdA):
            copy(1, CmdReg)
        with rung(rise(Enable), CmdProd):
            copy(2, CmdReg)
        with rung():
            call(apply_mode)
        with rung(Enable, Mode == 3):
            out(Output)

    plc = PLC(prog)
    path = pilot_how(plc, Output, max_scans=3000)
    assert path.reachable, (
        f"L6 should discover Mode transition with trace context: {getattr(path, 'reason', '')}"
    )


def test_influence_driven_opaque_state_machine():
    """PILOT reaches a target through an opaque pipeline via influence mapping.

    State is written through an indirect copy (ds[pointer] -> Scratch -> State).
    The backward trace dead-ends at Scratch (opaque writer). Influence mapping
    detects the pipeline upfront, probes command buttons systematically, and
    BFS finds the path.
    """
    from pyrung.click import ClickBlocks

    x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

    CmdA = Bool("CmdA", external=True)
    CmdB = Bool("CmdB", external=True)
    CmdC = Bool("CmdC", external=True)
    CmdReg = Int("CmdReg")
    Pointer = Int("Pointer")
    Scratch = Int("Scratch")
    State = Int("State", default=0)
    Output = Bool("Output")

    # ds[10]=1, ds[11]=2, ds[12]=3
    ds.slot(10, name="jump_0", default=1)
    ds.slot(11, name="jump_1", default=2)
    ds.slot(12, name="jump_2", default=3)

    @subroutine("ApplyState")
    def apply_state():
        with rung():
            calc(CmdReg + 10, Pointer)
        with rung():
            copy(ds[Pointer], Scratch)
        with rung(Scratch != 0):
            copy(Scratch, State)
            copy(0, CmdReg)
            copy(0, Scratch)

    with Program() as prog:
        with rung(CmdA):
            copy(1, CmdReg)
        with rung(CmdB):
            copy(2, CmdReg)
        with rung(CmdC):
            copy(3, CmdReg)
        with rung():
            call(apply_state)
        with rung(State == 3):
            out(Output)

    plc = PLC(prog)
    path = pilot_how(plc, Output, max_scans=3000)
    assert path.reachable, (
        f"Should reach Output via influence mapping: {getattr(path, 'reason', '')}"
    )


# ===================================================================
# Section 7: Static route expansion (evidence module)
# ===================================================================


def _packml_program():
    """PackML-like program with Literal+Affine writers for StateCurrent."""
    CmdClear = Bool("CmdClear", external=True)
    CmdReset = Bool("CmdReset", external=True)
    CmdStart = Bool("CmdStart", external=True)
    StateCurrent = Int("StateCurrent", default=9)
    StateRequested = Int("StateRequested")
    Output = Bool("Output")

    with Program() as prog:
        with rung(CmdClear, StateCurrent == 9):
            copy(1, StateRequested)
        with rung(CmdReset, StateCurrent == 2):
            copy(15, StateRequested)
        with rung(CmdStart, StateCurrent == 4):
            copy(3, StateRequested)

        with rung(StateRequested == 1):
            copy(2, StateCurrent)
            copy(0, StateRequested)
        with rung(StateRequested == 15):
            copy(4, StateCurrent)
            copy(0, StateRequested)
        with rung(StateRequested == 3):
            copy(6, StateCurrent)
            copy(0, StateRequested)

        with rung(StateRequested != 0):
            copy(StateRequested, StateCurrent)
            copy(0, StateRequested)

        with rung(StateCurrent == 6):
            out(Output)

    return prog, Output


def test_expand_routes_packml_state_machine():
    """Static route expansion finds all state transitions with correct destinations."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.evidence import expand_routes
    from pyrung.core.analysis.steerable import compute_steerable

    prog, _Output = _packml_program()
    plc = PLC(prog)
    plc.step()
    pdg = build_program_graph(prog)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, prog)

    routes = expand_routes("StateCurrent", pdg, prog, steerable, frozenset())

    # Pipeline routes via StateRequested
    pipeline = [r for r in routes if r.request_tag == "StateRequested"]
    assert len(pipeline) >= 3, f"Expected >=3 pipeline routes, got {len(pipeline)}"

    # Build source→dest map from pipeline routes
    route_map: dict[int, int] = {}
    for r in pipeline:
        for tag, value in r.source_constraints:
            if tag == "StateCurrent":
                route_map[value] = r.destination_value

    assert route_map.get(9) == 2, f"Clear: 9→2, got {route_map}"
    assert route_map.get(2) == 4, f"Reset: 2→4, got {route_map}"
    assert route_map.get(4) == 6, f"Start: 4→6, got {route_map}"

    # Every pipeline route should have steerable action tags
    for r in pipeline:
        assert r.action_tags, f"Route should have action tags: {r.source_constraints}"


def test_expand_routes_indirect_jump_table_pipeline():
    """Indirect copy routes lift pointer scratch back to the request tag."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.evidence import (
        expand_pipeline_need,
        expand_routes,
        infer_pipeline_roles,
    )
    from pyrung.core.analysis.steerable import compute_steerable

    CmdStart = Bool("CmdStart", external=True)
    StateComplete = Bool("StateComplete", external=True)
    StateCurrent = Int("StateCurrent", default=4)
    StateRequested = Int("StateRequested")
    StateEnabled = Bool("StateEnabled")
    StateStarting = Bool("StateStarting")
    JumpIdx = Int("JumpIdx")
    JumpTable = Block("JumpTable", TagType.INT, 100, 110)
    JumpTable.slot(103, default=6)
    Output = Bool("Output")

    with Program() as prog:
        with rung(StateCurrent == 4):
            out(StateStarting)
        with rung(CmdStart, StateCurrent == 4):
            copy(3, StateRequested)
        with rung(StateComplete, StateStarting):
            copy(3, StateRequested)
        with rung(StateRequested != 0):
            out(StateEnabled)
        with rung(StateEnabled):
            calc(StateRequested + 100, JumpIdx)
            copy(JumpTable[JumpIdx], StateCurrent)
            copy(0, StateRequested)
        with rung(StateCurrent == 6):
            out(Output)

    plc = PLC(prog)
    plc.step()
    pdg = build_program_graph(prog)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, prog)

    routes = expand_routes("StateCurrent", pdg, prog, steerable, frozenset())

    pipeline = [r for r in routes if r.request_tag == "StateRequested"]
    assert pipeline
    start = next(
        r
        for r in pipeline
        if ("StateCurrent", 4) in r.source_constraints and ("CmdStart", True) in r.enablers
    )
    assert start.request_value == 3
    assert start.destination_value == 6
    assert start.action_tags == frozenset({"CmdStart"})

    complete = next(
        r
        for r in pipeline
        if ("StateCurrent", 4) in r.source_constraints and ("StateComplete", True) in r.enablers
    )
    assert ("StateStarting", True) not in complete.enablers
    assert complete.destination_value == 6

    roles = infer_pipeline_roles("StateCurrent", pdg, prog, steerable, frozenset())
    assert roles.request_tags == frozenset({"StateRequested"})
    assert roles.guard_internal_tags == frozenset({"StateEnabled"})
    assert "StateEnabled" in roles.trace_internal_tags
    assert "StateRequested" not in roles.trace_internal_tags

    expansions = expand_pipeline_need("StateRequested", 3, (roles,), tuple(routes))
    assert len(expansions) == 1
    expansion = expansions[0]
    assert expansion.role == roles
    assert any(
        ("StateCurrent", 4) in route.source_constraints
        and ("StateComplete", True) in route.enablers
        for route in expansion.routes
    )


def test_skiff_scan_suppresses_non_participants():
    """Skiff scans run full scans while pinning unrelated side effects."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.evidence import infer_pipeline_roles, roles_for_needed_tag
    from pyrung.core.analysis.pilot.skiff import run_skiff_scan
    from pyrung.core.analysis.steerable import compute_steerable

    CmdStart = Bool("CmdStart", external=True)
    StateCurrent = Int("StateCurrent", default=4)
    StateRequested = Int("StateRequested")
    StateEnabled = Bool("StateEnabled")
    SideEffect = Int("SideEffect")
    JumpIdx = Int("JumpIdx")
    JumpTable = Block("JumpTable", TagType.INT, 100, 110)
    JumpTable.slot(103, default=6)

    with Program() as prog:
        with rung(CmdStart, StateCurrent == 4):
            copy(3, StateRequested)
            copy(99, SideEffect)
        with rung(StateRequested != 0):
            out(StateEnabled)
        with rung(StateEnabled):
            calc(StateRequested + 100, JumpIdx)
            copy(JumpTable[JumpIdx], StateCurrent)
            copy(0, StateRequested)

    plc = PLC(prog)
    plc.step()
    pdg = build_program_graph(prog)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, prog)
    role = infer_pipeline_roles("StateCurrent", pdg, prog, steerable, frozenset())
    assert roles_for_needed_tag("StateRequested", (role,)) == (role,)

    result = run_skiff_scan(
        plc,
        role,
        pdg,
        actions=(("CmdStart", True),),
        extra_tags=frozenset({"JumpIdx"}),
        scans=1,
    )

    assert result.after["StateCurrent"] == 6
    assert result.after["SideEffect"] == 0
    assert ("StateCurrent", 4, 6) in result.participating_changes
    assert not result.suppressed_changes


def test_static_routes_remain_in_catalog_not_learned_knowledge():
    """Static paths are queryable without copying them into learned entries."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.charts import (
        _best_static_path,
        build_static_transition_graphs,
        detect_opaque_loop,
    )
    from pyrung.core.analysis.pilot.compass import Compass, NavigationCatalog
    from pyrung.core.analysis.pilot.evidence import infer_pipeline_roles
    from pyrung.core.analysis.steerable import compute_steerable

    prog, _Output = _packml_program()
    plc = PLC(prog)
    plc.step()
    pdg = build_program_graph(prog)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, prog)

    opaque = detect_opaque_loop(pdg, prog)
    role = infer_pipeline_roles("StateCurrent", pdg, prog, steerable, opaque)
    graphs = build_static_transition_graphs(
        (role,),
        pdg,
        prog,
        steerable,
        opaque,
        None,
    )
    compass = Compass(NavigationCatalog(graphs=graphs))
    assert len(compass.knowledge.entries) == 0

    path = _best_static_path(
        "StateCurrent",
        6,
        {"StateCurrent": 9},
        compass.catalog.graphs,
        edge_allowed=lambda _edge: True,
    )
    assert path is not None, "static reader should find path 9→6"
    assert len(path.edges) == 3, f"path should be 3 hops, got {len(path.edges)}"


def test_expand_routes_direct_writer():
    """Direct Literal writers produce routes without a request tag."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.evidence import expand_routes
    from pyrung.core.analysis.steerable import compute_steerable

    CmdA = Bool("CmdA", external=True)
    CmdB = Bool("CmdB", external=True)
    State = Int("State", default=0)
    Output = Bool("Output")

    with Program() as prog:
        with rung(State == 0, CmdA):
            copy(1, State)
        with rung(State == 1, CmdB):
            copy(2, State)
        with rung(State == 2):
            out(Output)

    plc = PLC(prog)
    plc.step()
    pdg = build_program_graph(prog)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, prog)

    routes = expand_routes("State", pdg, prog, steerable, frozenset())

    assert len(routes) >= 2
    # All direct — no request pipeline
    for r in routes:
        assert r.request_tag is None
        assert r.destination_value is not None

    route_map = {}
    for r in routes:
        for tag, value in r.source_constraints:
            if tag == "State":
                route_map[value] = r.destination_value
    assert route_map.get(0) == 1
    assert route_map.get(1) == 2


def test_expand_routes_subroutine_call_site_gates():
    """Routes through subroutine writers include call-site gate conditions."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.evidence import expand_routes
    from pyrung.core.analysis.steerable import compute_steerable

    Enable = Bool("Enable", external=True)
    Cmd = Bool("Cmd", external=True)
    State = Int("State", default=0)
    Output = Bool("Output")

    @subroutine("doWork")
    def do_work():
        with rung(State == 0, Cmd):
            copy(1, State)

    with Program() as prog:
        with rung(Enable):
            call(do_work)
        with rung(State == 1):
            out(Output)

    plc = PLC(prog)
    plc.step()
    pdg = build_program_graph(prog)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, prog)

    routes = expand_routes("State", pdg, prog, steerable, frozenset())

    assert len(routes) >= 1
    route = routes[0]
    assert route.writer_subroutine == "doWork"
    # Call site gate should include Enable
    gate_tags = {tag for tag, _val in route.call_site_gates}
    assert "Enable" in gate_tags, f"Expected Enable in call_site_gates, got {route.call_site_gates}"
