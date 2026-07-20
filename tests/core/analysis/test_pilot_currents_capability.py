"""CI gates for the program-owned-current capability pieces.

Covers the read-side building blocks that make a program-owned command producer
visible beside the avoided operator button (see ``currents.py``):

* **const-fold** (piece 1) — a copy from a program constant (never-written,
  non-lever) folds to its declared default, so a value-by-literal producer search
  sees the program-owned ``copy(CmdCompleteRef, C_CtrlCmd)`` producer; punts when
  the source has any program writer or is a steerable/external lever;
* **sibling producer families** (piece 2) — the writers issuing one command value
  group into a family; a steerable exemplar is sufficient but not necessary (the
  completion-pipeline shape has program-only producers);
* **regression legibility** (piece 6) — the ``trend_regression`` console line
  prints the channel transition being reverted, so a destructive Abort (``6->8``)
  is distinguishable from a program-intended detour (``6->11``);
* **tide-gated edge** (piece 3, born STRICT-XFAIL) — a program-owned producer via a
  const-Ref copy, guarded by an internal step/timer chain that needs one external
  nudge, with the operator button for the same value avoided.  The recipe advance
  is NOT an operator ack at a recognized state (so ``currents`` returns None), and
  the trace dead-ends on the opaque-loop state register — so today the pilot never
  surfaces the producer's step-chain prerequisites.  Flips when channel-punt
  expansion surfaces them into the trace tree.
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
from pyrung.core.crossing import Affine

from .test_pilot_table_detour import _packml_table_detour_program


def _walkctx(logic, plc):
    """A drive-layer :class:`WorldView` (steerable has ref-constants subtracted)."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.charts import detect_opaque_loop
    from pyrung.core.analysis.pilot.currents import WorldView
    from pyrung.core.analysis.pilot.trace import compute_reference_constants
    from pyrung.core.analysis.steerable import compute_steerable

    pdg = build_program_graph(logic)
    known = plc._known_tags_by_name
    ref = compute_reference_constants(pdg, logic, known)
    steerable = compute_steerable(pdg, known, logic) - ref
    opaque_loop = detect_opaque_loop(pdg, logic)
    return WorldView(dict(plc.state.tags), pdg, logic, steerable, opaque_loop, None)


# --------------------------------------------------------------------------- #
# piece 1 — const-fold
# --------------------------------------------------------------------------- #


def test_const_fold_folds_program_constant_source() -> None:
    """An identity copy from a never-written, non-lever constant folds to its
    default, so the program-owned producer's written value is statically known."""
    from pyrung.core.analysis.pilot.currents import (
        fold_const_copy_source,
        is_program_constant,
    )

    logic, _tags = _packml_table_detour_program()
    plc = PLC(logic, dt=0.010)
    plc.step()
    ctx = _walkctx(logic, plc)

    assert is_program_constant("PackTbl_CmdCompleteRef", ctx) is True
    # copy(CmdCompleteRef, Cmd) -> Affine(source=CmdCompleteRef); folds to 10.
    folded = fold_const_copy_source(Affine(source="PackTbl_CmdCompleteRef"), ctx)
    assert folded is not None
    assert folded.value == 10


def test_const_fold_punts_on_program_written_source() -> None:
    """Fail-closed: a source with any program writer is not a constant — no fold."""
    from pyrung.core.analysis.pilot.currents import (
        fold_const_copy_source,
        is_program_constant,
    )

    logic, _tags = _packml_table_detour_program()
    plc = PLC(logic, dt=0.010)
    plc.step()
    ctx = _walkctx(logic, plc)

    # StateRequested is program-written -> not a constant.
    assert is_program_constant("PackTbl_StateRequested", ctx) is False
    assert fold_const_copy_source(Affine(source="PackTbl_StateRequested"), ctx) is None


def test_const_fold_punts_on_steerable_source() -> None:
    """A steerable/external operator word is a lever, not a constant — no fold
    (its value can change, so folding to the snapshot value would be unsound)."""
    from pyrung.core.analysis.pilot.currents import (
        fold_const_copy_source,
        is_program_constant,
    )

    logic, _tags = _packml_table_detour_program()
    plc = PLC(logic, dt=0.010)
    plc.step()
    ctx = _walkctx(logic, plc)

    assert is_program_constant("PackTbl_C_Start", ctx) is False
    assert fold_const_copy_source(Affine(source="PackTbl_C_Start"), ctx) is None


# --------------------------------------------------------------------------- #
# piece 2 — sibling producer families
# --------------------------------------------------------------------------- #


def test_sibling_family_includes_program_and_operator() -> None:
    """The family for the terminal command value carries BOTH the program-owned
    producer (a const-Ref copy under a timer done) and the operator button — the
    const-fold is what makes the program producer join the value's family."""
    from pyrung.core.analysis.pilot.currents import sibling_producer_family

    logic, _tags = _packml_table_detour_program()
    plc = PLC(logic, dt=0.010)
    plc.step()
    ctx = _walkctx(logic, plc)

    fam = sibling_producer_family(ctx, "PackTbl_Cmd", 10)
    assert fam is not None
    kinds = {p.kind for p in fam.producers}
    assert "program" in kinds  # the CompleteTmr.Done producer
    assert "operator" in kinds  # the C_Complete button
    assert fam.has_steerable_exemplar is True
    assert fam.program_owned  # at least one program-owned producer surfaced


def test_sibling_family_without_steerable_exemplar() -> None:
    """A steerable exemplar is SUFFICIENT but not NECESSARY: the Hold command value
    is issued only by the program's own dwell timer (no operator Hold button), so
    the family is found with ``has_steerable_exemplar=False``."""
    from pyrung.core.analysis.pilot.currents import sibling_producer_family

    logic, _tags = _packml_table_detour_program()
    plc = PLC(logic, dt=0.010)
    plc.step()
    ctx = _walkctx(logic, plc)

    fam = sibling_producer_family(ctx, "PackTbl_Cmd", 4)  # HOLD_CMD
    assert fam is not None
    assert fam.has_steerable_exemplar is False
    assert all(p.kind == "program" for p in fam.producers)


def test_sibling_family_none_for_unproduced_value() -> None:
    """No writer produces the value -> None (never fabricate a family)."""
    from pyrung.core.analysis.pilot.currents import sibling_producer_family

    logic, _tags = _packml_table_detour_program()
    plc = PLC(logic, dt=0.010)
    plc.step()
    ctx = _walkctx(logic, plc)

    assert sibling_producer_family(ctx, "PackTbl_Cmd", 999) is None


# --------------------------------------------------------------------------- #
# piece 6 — regression legibility
# --------------------------------------------------------------------------- #


def test_regression_console_prints_channel_transition() -> None:
    """The reverted channel edge is printed, separating a destructive Abort from a
    program-intended detour in the transcript."""
    from pyrung.core.analysis.pilot.types import PilotEvent
    from pyrung.dap.console import _format_pilot_progress

    event = PilotEvent(
        "trend_regression",
        815,
        {
            "channel_transitions": (("S_StateCurrent", 6, 8),),
            "investigation": {
                "confirmed_detail": ({"holds": (("A_Alm16_Status", 1),)},),
            },
        },
    )
    line = _format_pilot_progress(event)
    assert line is not None
    assert "channel S_StateCurrent 6->8" in line
    assert "A_Alm16_Status=1" in line  # the cause still rides the same line


def test_regression_console_no_transition_is_unchanged() -> None:
    """Byte-identical to the prior behavior when no channel moved."""
    from pyrung.core.analysis.pilot.types import PilotEvent
    from pyrung.dap.console import _format_pilot_progress

    event = PilotEvent(
        "trend_regression",
        815,
        {"channel_transitions": (), "investigation": {}},
    )
    assert _format_pilot_progress(event) == "  regression: reverted to checkpoint"


# --------------------------------------------------------------------------- #
# piece 3 — tide-gated edge (born STRICT-XFAIL, flipped by channel-punt expansion)
# --------------------------------------------------------------------------- #


def _tide_gated_program() -> tuple[Program, dict[str, object]]:
    """A PackML machine whose terminal command is program-owned via a const-Ref
    copy, gated by an INTERNAL step/timer chain that needs one external nudge.

    Unlike the sibling ``_packml_table_detour_program`` (whose detour turns on an
    operator ack legal *at a recognized state*, which ``currents`` surfaces), here
    the recipe advance is a step chain: Execute dwells a timer, the step needs one
    external ``DoorSensor`` rise to advance, and only at the final step does the
    program self-issue Complete.  There is NO operator ack at any state the current
    reader recognizes, so recognition is silent and the trace dead-ends on the
    opaque-loop state register — the tide-gated edge the capability must open.
    """
    IDLE, EXECUTE, COMPLETING, COMPLETED = 4, 6, 16, 17
    START_CMD, COMPLETE_CMD = 2, 10

    STATE_CHOICES = {
        0: "Undefined", 1: "Clearing", 2: "Stopped", 3: "Starting", 4: "Idle",
        5: "Suspended", 6: "Execute", 7: "Stopping", 8: "Aborting", 9: "Aborted",
        16: "Completing", 17: "Completed",
    }  # fmt: skip
    CMD_CHOICES = {0: "Undefined", 2: "Start", 10: "Complete"}

    C_Start = Bool("TG_C_Start", external=True)
    DoorSensor = Bool("TG_DoorSensor", external=True)  # the one external nudge
    C_Complete = Bool("TG_C_Complete", external=True)  # avoided operator button

    Cmd = Int("TG_Cmd", choices=CMD_CHOICES)
    CmdReq = Int("TG_CmdReq")
    State = Int("TG_State", default=IDLE, choices=STATE_CHOICES)
    StateRequested = Int("TG_StateRequested")
    Step = Int("TG_Step")

    CmdStartRef = Int("TG_CmdStartRef", readonly=True, default=START_CMD)
    CmdCompleteRef = Int("TG_CmdCompleteRef", readonly=True, default=COMPLETE_CMD)

    EnblYes = Int("TG_EnblYes")
    JumpIdx = Int("TG_JumpIdx")
    JumpTarget = Int("TG_JumpTarget")
    JT = Block("TG_JT", TagType.INT, 150, 200)
    for s in range(1, 18):
        JT.slot(150 + s, default=s)

    DwellTmr = Timer.clone("TG_DwellTmr")
    FinishTmr = Timer.clone("TG_FinishTmr")

    with Program(strict=False) as logic:
        # operator + program-owned command producers (both via a const Ref)
        with rung(C_Start):
            copy(CmdStartRef, Cmd)
            copy(1, CmdReq)
        with rung(C_Complete):  # the avoided operator button
            copy(CmdCompleteRef, Cmd)
            copy(1, CmdReq)

        # recipe step chain (self-driving except one external nudge) ------------
        # Step 0 -> 1: a dwell timer completes on its own in Execute.
        with rung(State == EXECUTE, Step == 0):
            on_delay(DwellTmr, 100, "ms")
        with rung(rise(DwellTmr.Done)):
            copy(1, Step)
        # Step 1 -> 2: needs the external DoorSensor rise (the tide gate).
        with rung(State == EXECUTE, Step == 1, rise(DoorSensor)):
            copy(2, Step)
        # Step 2: a second dwell, then the program self-issues Complete.
        with rung(State == EXECUTE, Step == 2):
            on_delay(FinishTmr, 100, "ms")
        with rung(rise(FinishTmr.Done)):
            copy(CmdCompleteRef, Cmd)  # PROGRAM-OWNED Complete via const Ref
            copy(1, CmdReq)

        # command -> state request (gated by state) -----------------------------
        with rung(CmdReq == 1, Cmd == START_CMD, State == IDLE):
            copy(EXECUTE, StateRequested)
        with rung(CmdReq == 1, Cmd == COMPLETE_CMD, State == EXECUTE):
            copy(COMPLETING, StateRequested)
        with rung(State == COMPLETING):
            copy(COMPLETED, StateRequested)

        # enable + indirect jump-table hop (arms the opaque loop) ---------------
        with rung():
            copy(1, EnblYes)
        with rung():
            calc(StateRequested + 150, JumpIdx)
        with rung():
            copy(JT[JumpIdx], JumpTarget)
        with rung(StateRequested != 0, EnblYes == 1):
            copy(StateRequested, State)
            copy(0, StateRequested)
            copy(0, Cmd)
            copy(0, CmdReq)

    tags: dict[str, object] = {
        "C_Start": C_Start,
        "DoorSensor": DoorSensor,
        "C_Complete": C_Complete,
        "State": State,
        "Execute": EXECUTE,
        "Completed": COMPLETED,
    }
    return logic, tags


def test_tide_gated_premise() -> None:
    """Hand-drive proves the constructive route exists without pressing Complete:
    Start, wait the dwell, pulse DoorSensor, wait the finish dwell — reaches Completed."""
    logic, tags = _tide_gated_program()
    plc = PLC(logic, dt=0.010)

    plc.patch({tags["C_Start"].name: True})
    plc.step()
    plc.patch({tags["C_Start"].name: False})
    plc.run(cycles=30)
    assert plc.state.tags[tags["State"].name] == tags["Execute"]

    plc.patch({tags["DoorSensor"].name: True})
    plc.step()
    plc.patch({tags["DoorSensor"].name: False})
    plc.run(cycles=40)

    assert plc.state.tags[tags["State"].name] == tags["Completed"]
    assert plc.state.tags[tags["C_Complete"].name] is False


def test_tide_gated_edge_reaches_via_program_current() -> None:
    """PILOT reaches Completed by opening the tide-gated program-owned edge —
    pressing the one external nudge (DoorSensor) at the right step and riding the
    program's self-issued Complete — without pressing the avoided C_Complete."""
    logic, tags = _tide_gated_program()
    plc = PLC(logic, dt=0.010)

    path = plc.how(tags["State"] == tags["Completed"], avoid=tags["C_Complete"], max_scans=400)

    assert path.reachable
    assert path.changes.get(tags["DoorSensor"].name) is True
    assert path.changes.get(tags["C_Complete"].name) is not True
    assert path.replay().state.tags[tags["State"].name] == tags["Completed"]
