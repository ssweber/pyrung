"""Tests for the constant-table predicate oracle (``pilot/table_oracle.py``).

The oracle inverts a boolean predicate whose operands are lookups into constant
``dh``/``ds`` tables — e.g. PackML state-enablement (``stateMask[State] &
disabledMask[Mode] == 0``) and command validity (``cmdMask[Cmd] &
allowMask[State] == 0``).  It enumerates the free index registers over their
finite domains and returns the satisfying assignments.  These are the two
independent callers of the same shape in ``examples/packml_bench.py``.
"""

from __future__ import annotations

import pytest

from pyrung import PLC
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.table_oracle import solve_table_predicate


@pytest.fixture
def bench():
    """packml_bench stepped past ``~InitDone`` so the dh mask tables are filled."""
    from examples.packml_bench import logic

    plc = PLC(logic)
    for _ in range(3):
        plc.step()
    pdg = build_program_graph(plc._program)
    return plc._program, pdg, dict(plc.current_state.tags)


@pytest.fixture
def bench_trace():
    """bench plus the steerable set — for the trace-side enablement-gate tests."""
    from examples.packml_bench import logic
    from pyrung.core.analysis.pilot.trace import compute_steerable

    plc = PLC(logic)
    for _ in range(3):
        plc.step()
    program = plc._program
    pdg = build_program_graph(program)
    snap = dict(plc.current_state.tags)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, program)
    return program, pdg, snap, steerable


# --- state-enablement gate: dh[300+State] & dh[200+Mode] == 0 ----------------


def test_holding_enabled_in_production_and_maintenance(bench):
    """Holding mask dh[310]=0x0200 collides only with Manual cfg dh[203]=0x0224."""
    program, pdg, snap = bench
    sol = solve_table_predicate(
        "StateMaskResult",
        0,
        "==",
        snap,
        pdg,
        program,
        fixed={"StateRequested": 10},  # HOLDING
        domains={"UnitModeCurrent": (1, 2, 3)},
    )
    assert sol is not None
    assert sol.per_tag["UnitModeCurrent"] == [1, 2]  # Prod, Maint (not Manual)


def test_held_enabled_in_all_modes(bench):
    """Held mask dh[311]=0x0400 (bit 10) is in no mode's disabled mask."""
    program, pdg, snap = bench
    sol = solve_table_predicate(
        "StateMaskResult",
        0,
        "==",
        snap,
        pdg,
        program,
        fixed={"StateRequested": 11},  # HELD
        domains={"UnitModeCurrent": (1, 2, 3)},
    )
    assert sol is not None
    assert sol.per_tag["UnitModeCurrent"] == [1, 2, 3]


def test_enablement_matches_direct_mask_arithmetic(bench):
    """The oracle's answer must equal recomputing the mask predicate by hand."""
    program, pdg, snap = bench
    modes = (1, 2, 3)
    for state in range(1, 18):
        state_mask = snap[f"DH{300 + state}"]
        expected = [m for m in modes if state_mask & snap[f"DH{200 + m}"] == 0]
        sol = solve_table_predicate(
            "StateMaskResult",
            0,
            "==",
            snap,
            pdg,
            program,
            fixed={"StateRequested": state},
            domains={"UnitModeCurrent": modes},
        )
        assert sol is not None, f"punted on state {state}"
        assert sol.per_tag["UnitModeCurrent"] == expected, f"state {state}"


# --- command-validity gate: dh[100+Cmd] & dh[State] == 0 (second caller) ------


def test_cmd_valid_is_a_second_independent_caller(bench):
    """Same oracle, different program shape (one operand is a direct index)."""
    program, pdg, snap = bench
    states = tuple(range(1, 18))
    for cmd in (2, 4):  # Start, Hold
        cmd_mask = snap[f"DH{100 + cmd}"]
        expected = [s for s in states if cmd_mask & snap[f"DH{s}"] == 0]
        sol = solve_table_predicate(
            "CmdValidResult",
            0,
            "==",
            snap,
            pdg,
            program,
            fixed={"CtrlCmd": cmd},
            domains={"StateCurrent": states},
        )
        assert sol is not None, f"punted on cmd {cmd}"
        assert sol.per_tag["StateCurrent"] == expected, f"cmd {cmd}"


# --- soundness: never fabricate; punt when it is not a table predicate --------


def test_punts_on_multi_writer_target(bench):
    """A tag written by many rungs (not one calc) is not a table predicate."""
    program, pdg, snap = bench
    assert solve_table_predicate("StateCurrent", 0, "==", snap, pdg, program) is None


def test_punts_on_unknown_operator(bench):
    program, pdg, snap = bench
    sol = solve_table_predicate(
        "StateMaskResult",
        0,
        "in",
        snap,
        pdg,
        program,  # unsupported op
        fixed={"StateRequested": 10},
        domains={"UnitModeCurrent": (1, 2, 3)},
    )
    assert sol is None


def test_unsatisfiable_predicate_returns_empty_not_none(bench):
    """A state disabled in every available mode is a real (empty) answer, not a punt."""
    program, pdg, snap = bench
    # Restrict the mode domain to Manual only; Holding is disabled there.
    sol = solve_table_predicate(
        "StateMaskResult",
        0,
        "==",
        snap,
        pdg,
        program,
        fixed={"StateRequested": 10},  # HOLDING, disabled in Manual
        domains={"UnitModeCurrent": (3,)},
    )
    assert sol is not None
    assert sol.per_tag["UnitModeCurrent"] == []


# --- trace wiring: the enablement gate surfaces the steerable mode ------------
#
# The identity transition ``copy(StateRequested, StateCurrent)`` in
# ``sm_copy_or_jump_state`` is gated by ``StateEnableYes == 1``, whose own writer
# is gated by the constant-table predicate ``StateMask & DisabledStates == 0``.
# That predicate register is recomputed from ``StateRequested`` every scan, so
# its snapshot value is stale w.r.t. the planned transition — without the oracle
# wiring, trace reads the gate as satisfied and omits the mode change.


_HOLDING = 10  # dh[310]=0x0200 — collides with the Manual disabled mask dh[203]
_EXECUTE = 6


def _manual_execute_snapshot(snap):
    """A Manual-mode machine in EXECUTE requesting HOLDING.

    HOLDING (dh[310]=0x0200) collides with the Manual disabled mask
    (dh[203]=0x0224), so the state-enable gate is *blocked* in Manual and a mode
    change to Production/Maintenance is a genuine prerequisite.
    """
    snap = dict(snap)
    snap["UnitModeCurrent"] = 3  # Manual
    snap["StateCurrent"] = _EXECUTE
    snap["StateRequested"] = _HOLDING
    return snap


def _enable_modes(tree):
    """The ``UnitModeCurrent`` values surfaced as ``enable`` prerequisites."""
    from pyrung.core.analysis.pilot.trace import _all_nodes

    return sorted(
        {
            n.value
            for n in _all_nodes(tree)
            if n.tag == "UnitModeCurrent" and n.data_flow == "enable"
        }
    )


def _compiled(cond):
    from pyrung.core.analysis.prove import _compile_property

    pred, _, _ = _compile_property(cond)
    return pred


def test_trace_surfaces_mode_for_state_disabled_in_current_mode(bench_trace):
    """how(HOLDING) from Manual surfaces UnitModeCurrent as an enable prereq.

    The regression the commit fixes: trace used to read the stale table gate as
    satisfied and omit the mode change entirely.
    """
    from pyrung.core.analysis.pilot.trace import trace_back

    program, pdg, snap, steerable = bench_trace
    snap = _manual_execute_snapshot(snap)

    tree = trace_back("StateCurrent", _HOLDING, snap, pdg, program, steerable)
    assert _enable_modes(tree), "mode change to enable HOLDING was not surfaced"


def test_enable_arm_respects_avoid_and_via(bench_trace):
    """``avoid=``/``via=`` steer the surfaced enable arm among the *real* modes.

    HOLDING is blocked in Manual, so a mode change to Production/Maintenance is a
    genuine prerequisite.  The degenerate mode 0 (Undefined = all-zero reserved
    mask slot) is *not* surfaced: the ``UnitModeCmd != 0`` guard on
    ``copy(UnitModeCmd, UnitModeCurrent)`` proves the writer can never emit
    ``UnitModeCurrent == 0`` (producibility), so the unsteered default is the
    cheapest *real* mode and ``via=`` steers onto a specific one.
    """
    from examples.packml_bench import UnitModeCurrent
    from pyrung.core.analysis.pilot.trace import trace_back

    program, pdg, snap, steerable = bench_trace
    snap = _manual_execute_snapshot(snap)

    default = trace_back("StateCurrent", _HOLDING, snap, pdg, program, steerable)
    modes = _enable_modes(default)
    assert modes and 0 not in modes  # a real mode, never the degenerate Undefined slot

    # avoid=(UnitModeCurrent == 0) is now redundant — producibility already
    # excludes the Undefined slot — but must stay harmless (never resurrect it).
    avoided = trace_back(
        "StateCurrent",
        _HOLDING,
        snap,
        pdg,
        program,
        steerable,
        avoid_pred=_compiled(UnitModeCurrent == 0),
    )
    assert _enable_modes(avoided) and 0 not in _enable_modes(avoided)

    # via= steers onto either real mode (both drivable via the steerable UnitModeCmd).
    onto_prod = trace_back(
        "StateCurrent",
        _HOLDING,
        snap,
        pdg,
        program,
        steerable,
        via_pred=_compiled(UnitModeCurrent == 1),
    )
    assert _enable_modes(onto_prod) == [1]  # Production, steered onto

    onto_maint = trace_back(
        "StateCurrent",
        _HOLDING,
        snap,
        pdg,
        program,
        steerable,
        via_pred=_compiled(UnitModeCurrent == 2),
    )
    assert _enable_modes(onto_maint) == [2]  # Maintenance, steered onto


def test_no_mode_surfaced_when_current_mode_already_enables(bench_trace):
    """how(HOLDING) from Production surfaces no mode change.

    HOLDING is enabled in Production (dh[201]=0x0000, empty disabled mask), so
    the state-enable gate genuinely holds under the current mode — trace must not
    fabricate a mode prerequisite.
    """
    from pyrung.core.analysis.pilot.trace import trace_back

    program, pdg, snap, steerable = bench_trace
    snap = dict(snap)
    snap["UnitModeCurrent"] = 1  # Production
    snap["StateCurrent"] = _EXECUTE
    snap["StateRequested"] = _HOLDING

    tree = trace_back("StateCurrent", _HOLDING, snap, pdg, program, steerable)
    assert _enable_modes(tree) == []


# --- trace wiring, generalized trigger: non-identity transitions --------------
#
# The oracle trigger keys on the SEMANTIC shape — a guard comparison over a
# register recomputed each scan from constant-table lookups plus the
# transition's own fire-time pins — not the identity-copy silhouette.  These
# programs are the PackML bench shape with the transition writer swapped for an
# affine calc (states stored at an offset) and for a literal decode (the pin
# lives in the guard, not in data flow).  Both must surface the mode
# prerequisite the same way
# test_trace_surfaces_mode_for_state_disabled_in_current_mode does.


def _mask_gate_program(transition: str):
    """Minimal state machine whose enablement gate is a constant-table mask.

    Mirrors packml_bench's ``sm_copy_or_jump_state`` mask pipeline: ``ds[300 +
    StateRequested]`` is the state's mask bit, ``ds[200 + ModeCurrent]`` the
    mode's disabled-state mask.  HOLDING (10, bit 0x0200) collides only with the
    Manual (mode 3) config, so from Manual a mode change to 1/2 is a genuine
    enable prerequisite.  *transition* picks the writer shape:

    - ``"identity"`` — ``copy(StateRequested, StateCurrent)`` (the old trigger);
    - ``"affine"`` — ``calc(StateRequested + 100, StateCurrent)`` (states stored
      at a +100 offset; the fire-time pin is the inverted affine map);
    - ``"decode"`` — ``copy(10, StateCurrent)`` gated ``StateRequested == 10``
      (the pin is the guard's own equality conjunct).
    """
    from pyrung import Bool, Int, Program, calc, copy, out, rung
    from pyrung.click import ClickBlocks

    x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

    offset = 100 if transition == "affine" else 0
    execute = 6 + offset
    holding = 10 + offset

    CmdMode1 = Bool("CmdMode1", external=True)
    CmdMode2 = Bool("CmdMode2", external=True)
    CmdMode3 = Bool("CmdMode3", external=True)
    CmdHold = Bool("CmdHold", external=True)
    ModeCmd = Int("ModeCmd")
    ModeCurrent = Int("ModeCurrent", default=3)  # Manual
    ModeCfgIdx = Int("ModeCfgIdx")
    DisabledStates = Int("DisabledStates")
    StateRequested = Int("StateRequested", default=0)
    StateCurrent = Int("StateCurrent", default=execute)
    StateMaskIdx = Int("StateMaskIdx")
    StateMask = Int("StateMask")
    StateMaskResult = Int("StateMaskResult")
    StateEnableYes = Int("StateEnableYes")
    Output = Bool("Output")

    # Mode disabled-state masks: only Manual blocks HOLDING's bit.
    ds.slot(201, name="cfg_prod", default=0x0000)
    ds.slot(202, name="cfg_maint", default=0x0000)
    ds.slot(203, name="cfg_manual", default=0x0224)
    # State mask bits (only the addresses this machine ever indexes).
    ds.slot(300, name="mask_none", default=0x0000)
    ds.slot(310, name="mask_holding", default=0x0200)

    # strict=False: the `if transition == ...` variant selection below is test
    # scaffolding, not ladder control flow.
    with Program(strict=False) as prog:
        with rung(CmdMode1):
            copy(1, ModeCmd)
        with rung(CmdMode2):
            copy(2, ModeCmd)
        with rung(CmdMode3):
            copy(3, ModeCmd)
        with rung(ModeCmd != 0):
            copy(ModeCmd, ModeCurrent)
        with rung():
            calc(200 + ModeCurrent, ModeCfgIdx)
        with rung():
            copy(ds[ModeCfgIdx], DisabledStates)
        with rung(CmdHold, StateCurrent == execute):
            copy(10, StateRequested)
        with rung():
            calc(300 + StateRequested, StateMaskIdx)
        with rung():
            copy(ds[StateMaskIdx], StateMask)
        with rung():
            calc(StateMask & DisabledStates, StateMaskResult)
        with rung():
            copy(0, StateEnableYes)
        with rung(StateMaskResult == 0):
            copy(1, StateEnableYes)
        if transition == "identity":
            with rung(StateRequested != 0, StateEnableYes == 1):
                copy(StateRequested, StateCurrent)
                copy(0, StateRequested)
        elif transition == "affine":
            with rung(StateRequested != 0, StateEnableYes == 1):
                calc(StateRequested + 100, StateCurrent)
                copy(0, StateRequested)
        elif transition == "decode":
            with rung(StateRequested == 10, StateEnableYes == 1):
                copy(10, StateCurrent)
                copy(0, StateRequested)
        with rung(StateCurrent == holding):
            out(Output)

    return prog, holding


def _mask_gate_trace(transition: str, *, mode: int = 3):
    """``(tree, holding)`` for a trace of the HOLDING transition from *mode*."""
    from pyrung.core.analysis.pilot.trace import compute_steerable, trace_back

    prog, holding = _mask_gate_program(transition)
    plc = PLC(prog)
    for _ in range(3):
        plc.step()
    pdg = build_program_graph(prog)
    snap = dict(plc.current_state.tags)
    snap["ModeCurrent"] = mode
    snap["StateRequested"] = 10
    steerable = compute_steerable(pdg, plc._known_tags_by_name, prog)
    return trace_back("StateCurrent", holding, snap, pdg, prog, steerable), holding


def _mask_gate_modes(tree):
    from pyrung.core.analysis.pilot.trace import _all_nodes

    return sorted(
        {n.value for n in _all_nodes(tree) if n.tag == "ModeCurrent" and n.data_flow == "enable"}
    )


def test_mini_identity_transition_surfaces_mode():
    """Baseline: the mini program reproduces the bench's identity-copy wiring."""
    tree, _ = _mask_gate_trace("identity")
    modes = _mask_gate_modes(tree)
    assert modes and 3 not in modes, f"expected an enabling mode, got {modes}"


def test_affine_calc_transition_surfaces_mode():
    """A non-identity transition ``calc(StateRequested + 100, StateCurrent)``.

    Semantically identical enablement structure to the identity copy — the gate
    register is recomputed from StateRequested each scan — but the old trigger
    (identity copy-source binding) never consulted the oracle here, silently
    omitting the mode prerequisite.  The fire-time pin ``StateRequested == 10``
    is derived by inverting the affine map (110 - 100).
    """
    tree, _ = _mask_gate_trace("affine")
    modes = _mask_gate_modes(tree)
    assert modes and 3 not in modes, f"expected an enabling mode, got {modes}"


def test_decode_transition_surfaces_mode():
    """A decode transition ``copy(10, StateCurrent)`` gated ``StateRequested == 10``.

    No data-flow source at all — the fire-time pin lives in the writer's own
    guard.  The guard's equality conjuncts hold the scan the writer fires, so
    they are sound pins for the recomputed table predicate.
    """
    tree, _ = _mask_gate_trace("decode")
    modes = _mask_gate_modes(tree)
    assert modes and 3 not in modes, f"expected an enabling mode, got {modes}"


@pytest.mark.parametrize("transition", ["identity", "affine", "decode"])
def test_no_mode_surfaced_when_mini_mode_already_enables(transition):
    """From Production (empty disabled mask) no writer shape fabricates a mode."""
    tree, _ = _mask_gate_trace(transition, mode=1)
    assert _mask_gate_modes(tree) == []
