"""Unit tests for pilot/trace.py — backward trace on toy programs."""

from __future__ import annotations

from pyrung import (
    PLC,
    Bool,
    Counter,
    Int,
    Program,
    Real,
    Timer,
    calc,
    call,
    copy,
    count_up,
    on_delay,
    out,
    rung,
    subroutine,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.trace import (
    TraceNode,
    _counter_driver_leaf,
    _env_for,
    _rank_writers,
    _resolve_inequality_target,
    _rewrite_internal_compare,
    _scan_transient_rest,
    _trace_back,
    _TraceEnv,
    compute_reference_constants,
    trace_back,
)
from pyrung.core.analysis.simplified import Atom
from pyrung.core.analysis.steerable import compute_steerable
from pyrung.core.memory_block import Block
from pyrung.core.tag import TagType


def _steerable_names(node: TraceNode) -> set[str]:
    return {t for t, _v in node.steerable_leaves()}


def _known(logic: Program) -> dict:
    plc = PLC(logic)
    return plc._known_tags_by_name


# -- Test 1: Boolean chain --------------------------------------------------


def test_bool_chain():
    """out chain: x_Enable -> y_Armed -> (+ x_Trigger) -> y_Target"""
    x_Enable = Bool("x_Enable", external=True)
    x_Trigger = Bool("x_Trigger", external=True)
    y_Armed = Bool("y_Armed")
    y_Target = Bool("y_Target")

    with Program() as logic:
        with rung(x_Enable):
            out(y_Armed)
        with rung(y_Armed, x_Trigger):
            out(y_Target)

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    tree = trace_back("y_Target", True, {}, pdg, logic, steerable)
    names = _steerable_names(tree)
    assert "x_Enable" in names
    assert "x_Trigger" in names


# -- Test 2: Copy data-flow -------------------------------------------------


def test_copy_data_flow():
    """copy(src, dest): trace finds the gate AND the copy source."""
    x_Go = Bool("x_Go", external=True)
    Requested = Int("Requested", external=True)
    Current = Int("Current")

    with Program() as logic:
        with rung(x_Go):
            copy(Requested, Current)

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    tree = trace_back("Current", 5, {}, pdg, logic, steerable)
    names = _steerable_names(tree)
    assert "x_Go" in names
    assert "Requested" in names


# -- Test 3: Calc expression -------------------------------------------------


def test_calc_affine():
    """calc(dest = src + 10): trace finds the gate and the Affine source."""
    x_Go = Bool("x_Go", external=True)
    Raw = Int("Raw", external=True)
    Scaled = Int("Scaled")

    with Program() as logic:
        with rung(x_Go):
            calc(Raw + 10, Scaled)

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    tree = trace_back("Scaled", 42, {}, pdg, logic, steerable)
    names = _steerable_names(tree)
    assert "x_Go" in names
    assert "Raw" in names


# -- Test 3b: Aggregate (sum) decomposition ---------------------------------


def test_aggregate_sum_decomposition():
    """calc(block.sum(), dest): trace decomposes to non-zero elements.

    Tracing Total=0 when the sum is 2 asks "how do I make the sum zero?"
    — each non-zero element (DS2, DS4) must be cleared.
    """
    x_Go = Bool("x_Go", external=True)
    x_Alarm = Bool("x_Alarm", external=True)
    blk = Block("DS", TagType.INT, 1, 5)
    Total = Int("Total")

    with Program() as logic:
        with rung(x_Go):
            copy(1, blk[2])
        with rung(x_Alarm):
            copy(1, blk[4])
        with rung():
            calc(blk.select(1, 5).sum(), Total)

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)
    snap = {"DS1": 0, "DS2": 1, "DS3": 0, "DS4": 1, "DS5": 0, "Total": 2}

    tree = trace_back("Total", 0, snap, pdg, logic, steerable)
    agg_children = [c for c in tree.children if c.data_flow == "aggregate"]
    assert len(agg_children) == 2
    agg_tags = {c.tag for c in agg_children}
    assert agg_tags == {"DS2", "DS4"}


# -- Test 4: Subroutine call gate -------------------------------------------


def test_subroutine_call_gate():
    """call gate: trace crosses the subroutine boundary."""
    x_Enable = Bool("x_Enable", external=True)
    x_Inner = Bool("x_Inner", external=True)
    y_Result = Bool("y_Result")

    with Program() as logic:
        with subroutine("do_work"):
            with rung(x_Inner):
                out(y_Result)

        with rung(x_Enable):
            call("do_work")

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    tree = trace_back("y_Result", True, {}, pdg, logic, steerable)
    names = _steerable_names(tree)
    assert "x_Inner" in names
    assert "x_Enable" in names


# -- Test 5: Timer done bit -------------------------------------------------


def test_timer_done():
    """Timer done: trace finds the enable condition (x_Start)."""
    x_Start = Bool("x_Start", external=True)
    timer = Timer.clone("T1")
    y_Complete = Bool("y_Complete")

    with Program() as logic:
        with rung(x_Start):
            on_delay(timer, preset=100)
        with rung(timer.Done):
            out(y_Complete)

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    tree = trace_back("y_Complete", True, {}, pdg, logic, steerable)
    # Timer.Done is written by the timer instruction — trace should find
    # x_Start as the enable condition on that writer rung
    names = _steerable_names(tree)
    assert "x_Start" in names


def _leaves(node: TraceNode) -> list[TraceNode]:
    out: list[TraceNode] = []

    def rec(n: TraceNode) -> None:
        if not n.children:
            out.append(n)
        for c in n.children:
            rec(c)

    rec(node)
    return out


def test_timer_done_running_yields_coast():
    """A *running* on-delay timer's Done bit becomes a self-advancing coast leaf.

    When the enable (rung condition) is already satisfied on the snapshot there
    is nothing to hold; reaching ``Done == True`` means letting the accumulator
    cross ``preset`` on its own — the same ``self_advancing`` accumulator coast
    the counter Done branch and the ``Acc > N`` threshold branch emit.
    """
    x_Start = Bool("x_Start", external=True)
    timer = Timer.clone("T1")

    with Program() as logic:
        with rung(x_Start):
            on_delay(timer, preset=100)

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    tree = trace_back("T1_Done", True, {"x_Start": True}, pdg, logic, steerable)
    coast = [lf for lf in _leaves(tree) if lf.self_advancing]
    assert len(coast) == 1
    assert (coast[0].tag, coast[0].value) == ("T1_Acc", 100)
    assert not coast[0].is_steerable


def test_timer_done_idle_keeps_enable_walk():
    """An idle timer's Done bit is unchanged: the enable surfaces as a steerable.

    The enable-unsatisfied arm belongs to the ordinary hold-and-coast walk — the
    coast helper returns ``None`` and today's shape (``x_Start`` steerable, no
    self-advancing leaf) is byte-identical.
    """
    x_Start = Bool("x_Start", external=True)
    timer = Timer.clone("T1")

    with Program() as logic:
        with rung(x_Start):
            on_delay(timer, preset=100)

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    tree = trace_back("T1_Done", True, {"x_Start": False}, pdg, logic, steerable)
    leaves = _leaves(tree)
    assert not any(lf.self_advancing for lf in leaves)
    assert "x_Start" in _steerable_names(tree)


def test_timer_done_running_honors_call_gate():
    """The enable includes the subroutine call gate, mirroring the ordinary walk.

    A running timer inside a subroutine coasts only when the call gate
    (``Mode == 1``) is satisfied too; with the gate unsatisfied the helper
    returns ``None`` and the ordinary walk surfaces the gate as the steerable
    blocker rather than a coast.
    """
    Mode = Int("Mode", external=True)
    Step = Bool("Step", external=True)
    timer = Timer.clone("T1")
    y_Complete = Bool("y_Complete")

    with Program() as logic:
        with subroutine("ExecSteps"):
            with rung(Step):
                on_delay(timer, preset=100)
            with rung(timer.Done):
                out(y_Complete)
        with rung(Mode == 1):
            call("ExecSteps")

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    gated = trace_back("T1_Done", True, {"Step": True, "Mode": 1}, pdg, logic, steerable)
    coast = [lf for lf in _leaves(gated) if lf.self_advancing]
    assert len(coast) == 1
    assert (coast[0].tag, coast[0].value) == ("T1_Acc", 100)

    blocked = trace_back("T1_Done", True, {"Step": True, "Mode": 2}, pdg, logic, steerable)
    assert not any(lf.self_advancing for lf in _leaves(blocked))
    assert "Mode" in _steerable_names(blocked)


# -- Test 6: Mixed (copy + guard + subroutine) ------------------------------


def test_mixed():
    """Mixed: copy + guard + subroutine + two-level trace."""
    x_Cmd = Bool("x_Cmd", external=True)
    x_Permit = Bool("x_Permit", external=True)
    Setpoint = Int("Setpoint", external=True)
    Active = Int("Active")
    y_Running = Bool("y_Running")

    with Program() as logic:
        with subroutine("activate"):
            with rung(x_Permit):
                copy(Setpoint, Active)
            with rung(Active == 50):
                out(y_Running)

        with rung(x_Cmd):
            call("activate")

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    tree = trace_back("y_Running", True, {}, pdg, logic, steerable)
    names = _steerable_names(tree)
    assert "x_Cmd" in names
    assert "x_Permit" in names
    assert "Setpoint" in names


# -- Test 7: (tag, value) visited key — same tag, different values -----------


def test_visited_key_tag_value():
    """Trace can visit the same tag at different values independently.

    StateCurrent needs to go 0→1→2 — the trace must discover both
    transitions, not stop after visiting StateCurrent once.
    """
    x_Reset = Bool("x_Reset", external=True)
    x_Start = Bool("x_Start", external=True)
    State = Int("State")
    y_Running = Bool("y_Running")

    with Program() as logic:
        # State 0 → 1 via x_Reset
        with rung(State == 0, x_Reset):
            copy(1, State)
        # State 1 → 2 via x_Start
        with rung(State == 1, x_Start):
            copy(2, State)
        # Output when State == 2
        with rung(State == 2):
            out(y_Running)

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    # Start from State=0
    tree = trace_back("y_Running", True, {"State": 0}, pdg, logic, steerable)
    names = _steerable_names(tree)
    assert "x_Reset" in names, f"expected x_Reset, got {names}"
    assert "x_Start" in names, f"expected x_Start, got {names}"


# -- Test 8: Already-satisfied returns no actions ----------------------------


def test_already_satisfied():
    """When the target is already satisfied, no steerable leaves."""
    x_Go = Bool("x_Go", external=True)
    y_Out = Bool("y_Out")

    with Program() as logic:
        with rung(x_Go):
            out(y_Out)

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    tree = trace_back("y_Out", True, {"y_Out": True}, pdg, logic, steerable)
    assert tree.satisfied
    assert tree.steerable_leaves() == []


# -- Test 9: TraceNode.ordered_actions preserves depth ordering ---------------


def test_ordered_actions_depth():
    """Deeper actions come before shallower ones (temporal prerequisite)."""
    x_Enable = Bool("x_Enable", external=True)
    x_Trigger = Bool("x_Trigger", external=True)
    y_Armed = Bool("y_Armed")
    y_Target = Bool("y_Target")

    with Program() as logic:
        with rung(x_Enable):
            out(y_Armed)
        with rung(y_Armed, x_Trigger):
            out(y_Target)

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    tree = trace_back("y_Target", True, {}, pdg, logic, steerable)
    actions = tree.ordered_actions()
    tags = [t for t, _v in actions]
    # x_Enable is deeper (it's a prerequisite of y_Armed which gates y_Target)
    # so it should come before x_Trigger
    assert tags.index("x_Enable") < tags.index("x_Trigger")


def test_subroutine_writer_selects_one_call_gate_by_wake():
    """A subroutine writer needs one caller gate, not every caller gate."""
    x_Request = Bool("x_Request", external=True)
    x_SimFirst = Bool("x_SimFirst", external=True)
    x_ModeProd = Bool("x_ModeProd", external=True)
    Mode = Int("Mode")
    Target = Bool("Target")
    Broad1 = Bool("Broad1")
    Broad2 = Bool("Broad2")
    Broad3 = Bool("Broad3")

    @subroutine("ApplyMode")
    def apply_mode():
        with rung(x_ModeProd):
            copy(1, Mode)

    with Program() as logic:
        with rung(x_Request):
            call(apply_mode)
        with rung(x_SimFirst):
            call(apply_mode)
            out(Broad1)
            out(Broad2)
            out(Broad3)
        with rung(Mode == 1):
            out(Target)

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)

    tree = trace_back("Target", True, {}, pdg, logic, steerable)
    actions = tree.ordered_actions()
    action_tags = {tag for tag, _value in actions}
    details = {action.tag: action for action in tree.ordered_action_details()}

    assert "x_ModeProd" in action_tags
    assert "x_Request" in action_tags
    assert "x_SimFirst" not in action_tags
    assert details["x_Request"].provenance
    assert details["x_Request"].provenance[0].startswith("Main:R")


def test_subroutine_writer_reuses_its_call_gate_across_trace_occurrences():
    """Repeated visits to one subroutine keep one coherent invocation route.

    Caller ranking is context-sensitive.  Once the normal caller is selected,
    even a later occurrence whose snapshot would make the simulation caller
    cheaper must reuse the normal caller.  Otherwise ``ordered_actions()`` can
    union mutually alternative call triggers into one batch.
    """
    x_Request = Bool("x_Request", external=True)
    x_SimFirst = Bool("x_SimFirst", external=True)
    x_ModeProd = Bool("x_ModeProd", external=True)
    AppliedA = Bool("AppliedA")
    AppliedB = Bool("AppliedB")

    @subroutine("ApplyModeCoherently")
    def apply_mode():
        with rung(x_ModeProd):
            out(AppliedA)
            out(AppliedB)

    with Program() as logic:
        with rung(x_Request):
            call(apply_mode)
        with rung(x_SimFirst):
            call(apply_mode)

    pdg = build_program_graph(logic)
    snapshot = dict(PLC(logic).state.tags)
    steerable = compute_steerable(pdg, _known(logic), logic)
    env = _env_for(snapshot, pdg, logic, steerable)

    first = _trace_back(env, "AppliedA", True)
    assert {tag for tag, _value in first.ordered_actions()} == {"x_ModeProd", "x_Request"}
    assert env.caller_locks

    # Model the different local context that used to make a repeated visit pick
    # the alternate caller.  The trace env deliberately owns mutable memo/lock
    # knowledge even though its structural shell is frozen.
    snapshot["x_SimFirst"] = True
    repeated = _trace_back(env, "AppliedB", True)
    repeated_actions = {tag for tag, _value in repeated.ordered_actions()}
    assert "x_Request" in repeated_actions
    assert "x_SimFirst" not in repeated_actions

    # Without the existing lock, the same later context does prefer the already
    # held simulation gate.  This proves the assertion above exercises reuse,
    # rather than two contexts that coincidentally rank callers the same way.
    fresh = _trace_back(_env_for(snapshot, pdg, logic, steerable), "AppliedB", True)
    fresh_actions = {tag for tag, _value in fresh.ordered_actions()}
    assert "x_Request" not in fresh_actions
    assert "x_SimFirst" not in fresh_actions


# -- Test 10: Indirect copy inversion (lookup table) ----------------------


def test_indirect_copy_lookup_table():
    """Trace sees through copy(block[ptr], dest) by inverting the table.

    Models the PackML jump-table pattern:
      calc(StateRequested + 10, idx)   -- compute pointer
      copy(ds[idx], JumpTarget)        -- read lookup table

    The table at ds[10+N] holds the next-state for StateRequested=N.
    Trace for JumpTarget=6 should invert the table: find which
    StateRequested values produce 6, and trace back to StateRequested.
    """
    ds = Block("DS", TagType.INT, 1, 20)
    StateRequested = Int("StateRequested")
    Idx = Int("Idx")
    JumpTarget = Int("JumpTarget")
    x_Cmd = Bool("x_Cmd", external=True)
    Output = Bool("Output")

    with Program(strict=False) as logic:
        with rung(x_Cmd):
            copy(3, StateRequested)
        with rung():
            calc(StateRequested + 10, Idx)
        with rung():
            copy(ds[Idx], JumpTarget)
        with rung(JumpTarget == 6):
            out(Output)

    plc = PLC(logic)

    # Populate the lookup table: ds[13] = 6 (StateRequested=3 → JumpTarget=6)
    plc.force("DS13", 6)
    # Other slots get different values
    plc.force("DS11", 2)
    plc.force("DS12", 4)
    plc.force("DS14", 9)
    plc.step()

    snap = dict(plc.state.tags)
    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, logic)

    tree = trace_back("JumpTarget", 6, snap, pdg, logic, steerable)

    # The trace should have followed:
    # JumpTarget=6 → ds[Idx] inversion → Idx needs value 13
    # → calc(StateRequested + 10, Idx) → StateRequested=3
    # → x_Cmd (steerable)
    assert tree.children, "trace should not dead-end at the indirect copy"
    has_lookup = any(c.data_flow == "lookup" for c in tree.children)
    assert has_lookup, "expected a 'lookup' data_flow child from indirect inversion"

    actions = tree.ordered_actions()
    action_tags = {t for t, _v in actions}
    assert "x_Cmd" in action_tags or "StateRequested" in action_tags, (
        f"expected trace to reach StateRequested or x_Cmd, got {action_tags}"
    )


# -- Test 11: Reference constants detected through functional dep chain ----


def test_reference_constants_via_func_dep_chain():
    """Reference constants are detected through the pointer chain.

    Models the PackML pattern where named constants feed into a tag
    that drives a lookup-table pointer through a calc-defined scratch:

      copy(STATE_STARTING_REF, StateRequested)   -- REF is the constant
      calc(StateRequested + 10, Idx)              -- func dep: Idx depends on StateRequested
      copy(ds[Idx], JumpTarget)                   -- Idx is the indirect pointer

    STATE_STARTING_REF is never written, used as a copy source into
    StateRequested, and StateRequested is the representative of the
    indirect-copy pointer (via the calc hop).  All three conditions hold.

    Similarly for CMD_REF feeding CtrlCmd which drives a separate
    lookup table pointer.
    """
    ds = Block("DS", TagType.INT, 1, 30)
    dh = Block("DH", TagType.INT, 1, 30)

    # State-machine REF constants (never written, initial values only)
    STATE_STARTING_REF = Int("STATE_STARTING_REF", default=3)
    STATE_IDLE_REF = Int("STATE_IDLE_REF", default=4)
    STATE_EXECUTE_REF = Int("STATE_EXECUTE_REF", default=6)

    # Command REF constant
    CMD_RESET_REF = Int("CMD_RESET_REF", default=1)

    # Pipeline tags
    StateRequested = Int("StateRequested")
    JumpIdx = Int("JumpIdx")
    JumpTarget = Int("JumpTarget")

    CtrlCmd = Int("CtrlCmd")
    CmdIdx = Int("CmdIdx")
    CmdValid = Int("CmdValid")

    x_Start = Bool("x_Start", external=True)
    x_Reset = Bool("x_Reset", external=True)

    with Program(strict=False) as logic:
        # Commands write REF values into pipeline tags
        with rung(x_Start):
            copy(STATE_STARTING_REF, StateRequested)
        with rung(x_Reset):
            copy(CMD_RESET_REF, CtrlCmd)

        # State jump table: calc pointer, indirect read
        with rung():
            calc(StateRequested + 10, JumpIdx)
        with rung():
            copy(ds[JumpIdx], JumpTarget)

        # Command validation table: calc pointer, indirect read
        with rung():
            calc(CtrlCmd + 20, CmdIdx)
        with rung():
            copy(dh[CmdIdx], CmdValid)

        # Other REF copies that DON'T go through pointer chains
        with rung():
            copy(STATE_IDLE_REF, StateRequested)
        with rung():
            copy(STATE_EXECUTE_REF, StateRequested)

    pdg = build_program_graph(logic)
    ref_consts = compute_reference_constants(pdg, logic)

    # STATE_STARTING_REF feeds StateRequested, which is the representative
    # of JumpIdx (pointer) via calc(StateRequested + 10, JumpIdx).
    assert "STATE_STARTING_REF" in ref_consts

    # STATE_IDLE_REF and STATE_EXECUTE_REF also feed StateRequested.
    assert "STATE_IDLE_REF" in ref_consts
    assert "STATE_EXECUTE_REF" in ref_consts

    # CMD_RESET_REF feeds CtrlCmd, which is the representative of
    # CmdIdx (pointer) via calc(CtrlCmd + 20, CmdIdx).
    assert "CMD_RESET_REF" in ref_consts

    # External inputs are NOT ref constants (they have no copy-source role
    # in the pointer chain, and they're meant to be steered).
    assert "x_Start" not in ref_consts
    assert "x_Reset" not in ref_consts

    # Pipeline tags that ARE written are not ref constants.
    assert "StateRequested" not in ref_consts
    assert "CtrlCmd" not in ref_consts


# -- Test 11b: table rows read only via ds[computed] are reference constants --


def test_reference_constants_via_indirect_read_slots():
    """Never-written slots read ONLY through ``ds[computed]`` are ref constants.

    A bounded pointer (``min``/``max``) makes the PDG register the reachable
    slots as readers, so ``compute_steerable`` would classify each table row as
    steerable and the skiff would waste probes on a data-only constant.  The
    indirect-read walk pulls those rows into the reference-constant set instead.
    """
    ds = Block("DS", TagType.INT, 1, 20)
    Idx = Int("Idx", min=10, max=13)  # bounded → DS10..DS13 become readers
    StateReq = Int("StateReq", min=0, max=3)
    JumpTarget = Int("JumpTarget")
    x_Cmd = Bool("x_Cmd", external=True)
    Output = Bool("Output")

    with Program(strict=False) as logic:
        with rung(x_Cmd):
            copy(3, StateReq)
        with rung():
            calc(StateReq + 10, Idx)
        with rung():
            copy(ds[Idx], JumpTarget)  # computed-index read; rows never copy sources
        with rung(JumpTarget == 6):
            out(Output)

    pdg = build_program_graph(logic)
    known = _known(logic)
    ref_consts = compute_reference_constants(pdg, logic, known)
    steerable = compute_steerable(pdg, known, logic) - ref_consts

    # The four reachable table rows are data constants, not levers.
    for slot in ("DS10", "DS11", "DS12", "DS13"):
        assert slot in ref_consts, f"{slot} should be a reference constant"
        assert slot not in steerable, f"{slot} should not be steerable"
    # The command that drives the pointer stays steerable.
    assert "x_Cmd" in steerable


def test_external_command_indexing_a_table_stays_steerable():
    """Condition 4 preserved: an external command feeding a table pointer is a lever.

    ``ToolReqCmd`` (external) is copied into ``ToolReq``, the representative of a
    lookup-table pointer via ``calc(ToolReq + 20, Idx)``.  The operator chooses
    the value, so it must remain steerable and out of the reference-constant set.
    """
    dh = Block("DH", TagType.INT, 1, 40)
    ToolReqCmd = Int("ToolReqCmd", external=True)
    ToolReq = Int("ToolReq")
    Idx = Int("Idx", min=21, max=24)
    ToolValid = Int("ToolValid")
    x_Load = Bool("x_Load", external=True)

    with Program(strict=False) as logic:
        with rung(x_Load):
            copy(ToolReqCmd, ToolReq)  # external command feeds the pointer rep
        with rung():
            calc(ToolReq + 20, Idx)
        with rung():
            copy(dh[Idx], ToolValid)

    pdg = build_program_graph(logic)
    known = _known(logic)
    ref_consts = compute_reference_constants(pdg, logic, known)
    steerable = compute_steerable(pdg, known, logic) - ref_consts

    assert "ToolReqCmd" not in ref_consts
    assert "ToolReqCmd" in steerable
    # The dh rows it indexes are still data constants.
    assert "DH21" in ref_consts


# -- Test 12: even-step counter selects the transition writer ----------------


def _curstep_engine():
    """Minimal Blower SFC step engine (blower.py R8/R15/R16/R17).

    Two ``calc(CurStep + 1, CurStep)`` writers produce ``CurStep + 1``:
    R16 is gated on parity (``valstepisodd != 1``, derived from CurStep), R17
    on a transition flag (``Trans == 1``).
    """
    x_TimerDone = Bool("x_TimerDone", external=True)
    x_FB = Bool("x_FB", external=True)
    CurStep = Int("CurStep")
    valstepisodd = Int("valstepisodd")
    Trans = Int("Trans")
    xPause = Int("xPause")

    with Program(strict=False) as logic:
        with rung(CurStep == 1, x_TimerDone, x_FB):  # transition trigger
            copy(1, Trans)
        with rung():  # parity (derived from CurStep)
            calc(CurStep % 2, valstepisodd)
        with rung(valstepisodd != 1, xPause == 0):  # even-step advance
            calc(CurStep + 1, CurStep)
        with rung(Trans == 1):  # transition advance
            calc(CurStep + 1, CurStep)

    return logic


def test_even_step_counter_selects_transition_writer():
    """``CurStep == 2`` resolves through the transition rung, not the even-step rung.

    The even-step rung can never land on ``CurStep == 2`` (its source would be
    ``CurStep == 1``, which is odd, contradicting its ``valstepisodd != 1``
    guard).  The projected oracle prerequisite-projects the affine source and
    its one-hop-derived parity, rejects the even-step rung as counterfactual,
    and selects the transition rung — surfacing ``Trans``'s trigger inputs.
    """
    logic = _curstep_engine()
    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)
    snapshot = {
        "CurStep": 0,
        "valstepisodd": 0,
        "Trans": 0,
        "xPause": 0,
        "x_TimerDone": False,
        "x_FB": False,
    }

    writers = pdg.writers_of.get("CurStep", frozenset())
    trans_rung = next(i for i, n in enumerate(pdg.rung_nodes) if "Trans" in n.condition_reads)
    even_rung = next(i for i, n in enumerate(pdg.rung_nodes) if "valstepisodd" in n.condition_reads)

    # Ranking: the transition rung outranks the (counterfactual) even-step rung.
    ranked = _rank_writers(writers, pdg, logic, "CurStep", 2, snapshot)
    assert ranked[0] == trans_rung
    assert ranked.index(trans_rung) < ranked.index(even_rung)

    # End to end: trace_back picks the transition rung and surfaces its trigger.
    tree = trace_back("CurStep", 2, snapshot, pdg, logic, steerable)
    assert tree.writer_rung == trans_rung
    names = _steerable_names(tree)
    assert "x_TimerDone" in names
    assert "x_FB" in names


# -- Test 13: one-hot pipeline tag selects the held-state writer (Fix 1) ------


def test_one_hot_pipeline_selects_held_state_writer():
    """A multi-writer one-hot pipeline tag traces through the live writer.

    ``SCB`` is written ``copy(1, SCB)`` under both ``S_Clearing`` and
    ``S_Starting``.  Holding ``S_Starting`` (one-hot peers pinned), the
    ``S_Clearing`` writer is counterfactual; the ``S_Starting`` writer is live
    and its remaining frontier is the real prerequisite ``Blower__init == 1``.
    """
    S_Starting = Bool("S_Starting")
    S_Clearing = Bool("S_Clearing")
    Blower__init = Int("Blower__init")
    SCB = Bool("SCB")

    with Program(strict=False) as logic:
        with rung(S_Clearing):  # counterfactual writer
            copy(1, SCB)
        with rung(S_Starting, Blower__init == 1):  # live writer
            copy(1, SCB)

    pdg = build_program_graph(logic)
    snapshot = {"S_Starting": True, "S_Clearing": False, "Blower__init": 0, "SCB": False}
    opaque = frozenset({"S_Starting", "S_Clearing"})

    writers = pdg.writers_of.get("SCB", frozenset())
    starting_rung = next(
        i for i, n in enumerate(pdg.rung_nodes) if "S_Starting" in n.condition_reads
    )
    clearing_rung = next(
        i for i, n in enumerate(pdg.rung_nodes) if "S_Clearing" in n.condition_reads
    )

    ranked = _rank_writers(writers, pdg, logic, "SCB", True, snapshot, opaque)
    assert ranked[0] == starting_rung
    assert ranked.index(starting_rung) < ranked.index(clearing_rung)


def test_subroutine_self_hold_not_available_without_caller_gate():
    """A body writer is not available merely because its local rung is true.

    The production body below keeps ``Mode == 1`` once the caller is already in
    production.  From ``Mode == 3`` the available tool is the mode-change writer,
    not the self-hold hidden behind ``with rung(Mode == 1): call(production)``.
    """
    Cmd = Int("Cmd", external=True)
    Req = Bool("Req", external=True)
    Mode = Int("Mode", default=3)

    @subroutine("ModeChange")
    def mode_change():
        with rung(Req, Cmd == 1):
            copy(Cmd, Mode)

    @subroutine("Production")
    def production():
        with rung():
            copy(1, Mode)

    with Program(strict=False) as logic:
        with rung(Req):
            call(mode_change)
        with rung(Mode == 1):
            call(production)

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, _known(logic), logic)
    snapshot = {"Mode": 3, "Req": False, "Cmd": 0}

    writers = pdg.writers_of.get("Mode", frozenset())
    transition = next(i for i in writers if pdg.rung_nodes[i].subroutine == "ModeChange")
    self_hold = next(i for i in writers if pdg.rung_nodes[i].subroutine == "Production")

    ranked = _rank_writers(writers, pdg, logic, "Mode", 1, snapshot, steerable=steerable)
    assert ranked[0] == transition
    assert ranked.index(transition) < ranked.index(self_hold)

    tree = trace_back("Mode", 1, snapshot, pdg, logic, steerable)
    assert tree.writer_rung == transition
    assert ("Req", True) in tree.ordered_actions()
    assert ("Cmd", 1) in tree.ordered_actions()


# -- Test 14: counter with an int advance condition resolves its driver -------


def _int_advance_counter(sel_tag):
    """``count_up`` advanced by ``Sel == 3`` — an int (non-Bool) advance read."""
    Rst = Bool("Rst", external=True)
    Cnt = Counter.clone("C")
    Done = Bool("Done")
    with Program() as logic:
        with rung(sel_tag == 3):
            count_up(Cnt, 5).reset(Rst)
        with rung(Cnt.Done):
            out(Done)
    return logic


def test_counter_int_advance_resolves_steerable_value():
    """An int advance (``Sel == 3``) resolves to the steerable value that fires it.

    The level-advance loop used to iterate only ``(True, False)``, so an int/word
    advance never matched and the driver dead-ended.  Enumerating ``Sel``'s
    declared choice domain finds ``Sel == 3``.
    """
    from pyrung.core.analysis.pilot.accumulators import resolve_profile

    Sel = Int("Sel", choices={0: "IDLE", 1: "WARM", 3: "GO"})
    logic = _int_advance_counter(Sel)
    plc = PLC(logic)
    plc.step()
    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, logic)
    env = _TraceEnv(
        snapshot=dict(plc.current_state.tags), pdg=pdg, program=logic, steerable=steerable
    )
    profile = resolve_profile("C_Done", logic).profile
    leaf = _counter_driver_leaf(env, profile, ("C_Done",))
    assert leaf is not None
    assert (leaf.tag, leaf.value) == ("Sel", 3)
    assert leaf.is_steerable and not leaf.oscillate


def test_prerequisite_action_carries_nearest_self_advancing_frontier():
    tree = TraceNode(
        tag="Done",
        value=True,
        children=[
            TraceNode(tag="Acc", value=10, self_advancing=True),
            TraceNode(tag="Enable", value=True, is_steerable=True),
        ],
    )

    (action,) = tree.ordered_action_details()
    assert action.until == Atom("Done", "eq", True)


def test_plain_action_has_no_invented_rung_lifetime():
    tree = TraceNode(tag="Enable", value=True, is_steerable=True)

    (action,) = tree.ordered_action_details()
    assert action.until is None


def test_counter_live_word_advance_punts():
    """An advance read with no complete finite domain (an unbounded word) punts."""
    from pyrung.core.analysis.pilot.accumulators import resolve_profile

    Sel = Int("Sel", external=True)  # unbounded — no choices / min-max
    logic = _int_advance_counter(Sel)
    plc = PLC(logic)
    plc.step()
    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, logic)
    env = _TraceEnv(
        snapshot=dict(plc.current_state.tags), pdg=pdg, program=logic, steerable=steerable
    )
    profile = resolve_profile("C_Done", logic).profile
    assert _counter_driver_leaf(env, profile, ("C_Done",)) is None


# -- Test 15: conjunctive compare reversal rewrites onto both source atoms -----


def test_internal_compare_conjunction_rewrites_both(monkeypatch):
    """A crossing branch of two ``Cmp``s (a conjunction) rewrites onto both atoms.

    The reversal of an internal register can yield a single conjunctive branch
    (``A > 5 ∧ B < 10``); the rewriter used to require exactly one ``Cmp`` per
    branch and dead-ended.  Both conjuncts must now surface as levers.
    """
    from pyrung.core.analysis import crossings
    from pyrung.core.crossing import Cmp, single

    A = Int("A", external=True)
    Mid = Int("Mid")
    x = Bool("x", external=True)
    with Program(strict=False) as logic:
        with rung(x):
            copy(A, Mid)  # sole writer of Mid; "B" appears only in the patched reversal

    pdg = build_program_graph(logic)
    plc = PLC(logic)
    plc.step()
    snap = dict(plc.current_state.tags)

    def _conjunctive_reverse(instr, r, target, ctx):
        return single(Cmp("A", ">", 5), Cmp("B", "<", 10))

    monkeypatch.setattr(crossings, "reverse", _conjunctive_reverse)
    rewritten = _rewrite_internal_compare(
        Atom(tag="Mid", form="gt", operand=0), frozenset({"A", "B"}), pdg, logic, snap
    )
    assert {(a.tag, a.form, a.operand) for a in rewritten} == {
        ("A", "gt", 5),
        ("B", "lt", 10),
    }


# -- Test 16: strict-inequality step is domain/epsilon-aware ------------------


def test_real_inequality_steps_epsilon_int_steps_one():
    """A Real ``gt`` fallback steps by an epsilon; an Int ``gt`` still steps ``+1``."""
    PV = Real("PV")
    IntPV = Int("IntPV")
    with Program(strict=False) as logic:
        with rung():
            copy(0, PV)
            copy(0, IntPV)
    pdg = build_program_graph(logic)
    # "Lower"/"IntLo" are tag-name operands resolved from the snapshot, not
    # declared tags — only PV/IntPV must be in pdg.tags for the type check.
    snap = {"PV": 20.0, "Lower": 15.0, "IntPV": 20, "IntLo": 15}

    real_hit = _resolve_inequality_target(
        Atom(tag="PV", form="gt", operand="Lower"), snap, None, pdg
    )
    assert real_hit is not None
    tag, val = real_hit
    assert tag == "PV"
    assert val > 15.0 and (val - 15.0) < 1.0  # an epsilon nudge, not +1

    int_hit = _resolve_inequality_target(
        Atom(tag="IntPV", form="gt", operand="IntLo"), snap, None, pdg
    )
    assert int_hit == ("IntPV", 16)


# -- Test 17: multi-scope producer rest — provable vs ambiguous ---------------


def test_multiscope_rest_provable():
    """Producers in two subroutines with a main clearer after both calls rest.

    ``flag`` is set in ``subA`` and ``subB`` (distinct scopes) and cleared by a
    self-gated main rung that runs after both calls, so it provably rests at 0.
    """
    flag = Bool("flag")
    a = Bool("a", external=True)
    b = Bool("b", external=True)
    with Program(strict=False) as logic:
        with subroutine("subA"):
            with rung(a):
                copy(1, flag)
        with subroutine("subB"):
            with rung(b):
                copy(1, flag)
        with rung():
            call("subA")
        with rung():
            call("subB")
        with rung(flag == 1):  # main clearer, after both calls, self-gated
            copy(0, flag)

    pdg = build_program_graph(logic)
    assert _scan_transient_rest("flag", pdg, logic) == (True, 0)


def test_multiscope_rest_ambiguous_punts():
    """A clearer sitting between the two producer calls cannot prove rest → punt.

    ``sB``'s producer runs after the clearer each scan, so ``flag`` may end a
    scan set — the rest is not provable and the walk must punt, not fabricate it.
    """
    flag = Bool("flag")
    a = Bool("a", external=True)
    b = Bool("b", external=True)
    with Program(strict=False) as logic:
        with subroutine("sA"):
            with rung(a):
                copy(1, flag)
        with subroutine("sB"):
            with rung(b):
                copy(1, flag)
        with rung():
            call("sA")
        with rung(flag == 1):  # clearer BETWEEN the producer calls
            copy(0, flag)
        with rung():
            call("sB")  # producer runs after the clearer

    pdg = build_program_graph(logic)
    assert _scan_transient_rest("flag", pdg, logic) == (False, None)


# -- WalkContext: the read-side seam, importable without trace -----------------


def test_trace_env_satisfies_walk_context_seam():
    """``_TraceEnv`` structurally satisfies the ``WalkContext`` read-side seam.

    The seam lives in ``types.py`` — importable *without* ``trace`` — so a future
    read-side instrument names ``WalkContext`` in its signature, lives in its own
    module, and takes this env straight in.  Locks the six world-describing fields;
    a bundle missing one is not a ``WalkContext``.
    """
    from pyrung.core.analysis.pilot.types import WalkContext

    Sel = Int("Sel", choices={0: "IDLE", 1: "WARM", 3: "GO"})
    logic = _int_advance_counter(Sel)
    plc = PLC(logic)
    plc.step()
    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, logic)
    env = _TraceEnv(
        snapshot=dict(plc.current_state.tags), pdg=pdg, program=logic, steerable=steerable
    )
    assert isinstance(env, WalkContext)
    for name in ("snapshot", "pdg", "program", "steerable", "opaque_loop", "prior"):
        assert hasattr(env, name)

    class _MissingPrior:
        snapshot: dict = {}
        pdg = None
        program = None
        steerable = frozenset()
        opaque_loop = frozenset()
        # no ``prior`` — not a WalkContext

    assert not isinstance(_MissingPrior(), WalkContext)
