"""Boundary gates for the sandbox instrument (``pilot/sandbox.py``).

Trace and let-run have the burner Starting→Execute acceptance test; these are
the skiff's equivalents.  Both programs share the constant-table mask shape the
oracle solves statically (``test_table_oracle.py``) — ``stateMask &
disabledMask == 0`` — except the disabled-mask word is **live**: rewritten at
runtime, so every static instrument punts and the documented escalation is the
skiff (isolated fork-pin-step probes feeding ``Compass.record``; the verify
pipeline confirms every learned edge live).

Two tiers:

- **Command-selected** (``_command_mask_program``, PASSING — the wired skiff's
  acceptance test): the mask is selected among constant-table rows by Bool
  commands.  Two conditional writers, so the oracle's single-writer operand
  model punts, but a pair probe (config select + start command) observably
  flips the governing register in isolation.
- **Free-word** (``_live_mask_program``, strict xfail — the next tier): the
  mask is copied from an unconstrained external word.  No sound probe values
  exist and the unblock is a *sequence* (set word, pulse load, then command),
  so it needs value synthesis / establish staging, not more probing.

Both gates keep the honesty pins: hand-driveable ground truth (a capability
gap, never an unreachable target), a ``solve_table_predicate`` punt (genuinely
live — if that assertion fails, the static layer got smarter and the gate
needs rework), and named-reason failures.
"""

from __future__ import annotations

import pytest

from pyrung import PLC, Bool, Int, Program, calc, copy, out, rise, rung
from pyrung.core.analysis.pilot import pilot_how


def _live_mask_program():
    """State machine whose enablement gate mixes a constant table with a live word.

    ``DisabledMask`` rests at 0x0040 (EXECUTE disabled). Unblocking requires a
    coordinated runtime config load: ``CfgWord`` nonzero with bit 6 clear, pulsed
    in via ``CfgLoad``. The ``DisabledMask != 0`` guard ("config must be valid")
    keeps the trivial resting pulse (``CfgWord=0``) from unblocking by accident.
    """
    from pyrung.click import ClickBlocks

    x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

    CmdStart = Bool("CmdStart", external=True)
    CfgLoad = Bool("CfgLoad", external=True)
    CfgWord = Int("CfgWord", external=True)
    DisabledMask = Int("DisabledMask", default=0x0040)
    StateMaskIdx = Int("StateMaskIdx")
    StateMask = Int("StateMask")
    MaskResult = Int("MaskResult")
    StateRequested = Int("StateRequested", default=0)
    StateCurrent = Int("StateCurrent", default=1)
    Output = Bool("Output")

    ds.slot(300, name="mask_none", default=0x0000)
    ds.slot(301, name="mask_stopped", default=0x0000)
    ds.slot(306, name="mask_execute", default=0x0040)

    with Program() as prog:
        # The live half: DisabledMask is rewritten at runtime from an external
        # word. No constant table backs it; its domain is unknowable statically.
        with rung(rise(CfgLoad)):
            copy(CfgWord, DisabledMask)
        # Request BEFORE the mask chain: the enablement predicate must be
        # computed for the state being requested, or the transition rung
        # consumes a stale row and fires regardless of the mask.
        with rung(CmdStart, StateCurrent == 1):
            copy(6, StateRequested)
        # The constant half: per-state mask from a declared-constant table.
        with rung():
            calc(300 + StateRequested, StateMaskIdx)
        with rung():
            copy(ds[StateMaskIdx], StateMask)
        with rung():
            calc(StateMask & DisabledMask, MaskResult)
        with rung(StateRequested != 0, MaskResult == 0, DisabledMask != 0):
            copy(StateRequested, StateCurrent)
            copy(0, StateRequested)
        with rung(StateCurrent == 6):
            out(Output)

    return prog, Output


def test_live_mask_target_is_hand_driveable():
    """Ground truth: the plant can do it — load a permissive config, then start."""
    prog, _output = _live_mask_program()
    plc = PLC(prog)
    plc.step()

    plc.patch({"CfgWord": 0x0001, "CfgLoad": True})
    plc.step()
    plc.patch({"CfgLoad": False})
    plc.step()
    assert plc.state.tags["DisabledMask"] == 0x0001

    plc.patch({"CmdStart": True})
    for _ in range(3):
        plc.step()
    assert plc.state.tags["StateCurrent"] == 6
    assert plc.state.tags["Output"] is True


def test_static_read_punts_on_live_mask_operand():
    """The oracle must return None — the mask operand is genuinely live.

    If this starts failing, the static layer learned to resolve the live word;
    the program is then no longer the skiff case and this gate needs rework.
    """
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.table_oracle import solve_table_predicate

    prog, _output = _live_mask_program()
    plc = PLC(prog)
    plc.step()
    pdg = build_program_graph(prog)
    snap = dict(plc.state.tags)

    sol = solve_table_predicate(
        "MaskResult",
        0,
        "==",
        snap,
        pdg,
        prog,
        fixed={"StateRequested": 6},
    )
    assert sol is None, "live DisabledMask operand must punt, not fabricate"


def _command_mask_program():
    """The skiff-winnable tier: the live mask is selected among constant-table
    rows by Bool commands.

    ``DisabledMask`` has TWO conditional table-copy writers, so every static
    read punts (the oracle's operand model needs a single writer; the
    producible-domain chain needs a sole write) — but a command probe
    observably flips it, which is exactly what fork-pin-step learns. The
    unblock is a coordinated ``CfgProd`` (permissive row, bit 6 clear, nonzero)
    beside the ``CmdStart`` the readable half of the tree already knows.
    """
    from pyrung.click import ClickBlocks

    x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

    CmdStart = Bool("CmdStart", external=True)
    CfgProd = Bool("CfgProd", external=True)
    CfgMaint = Bool("CfgMaint", external=True)
    DisabledMask = Int("DisabledMask", default=0x0040)
    StateMaskIdx = Int("StateMaskIdx")
    StateMask = Int("StateMask")
    MaskResult = Int("MaskResult")
    StateRequested = Int("StateRequested", default=0)
    StateCurrent = Int("StateCurrent", default=1)
    Output = Bool("Output")

    ds.slot(201, name="cfg_prod_row", default=0x0001)
    ds.slot(202, name="cfg_maint_row", default=0x0264)
    ds.slot(300, name="cm_mask_none", default=0x0000)
    ds.slot(301, name="cm_mask_stopped", default=0x0000)
    ds.slot(306, name="cm_mask_execute", default=0x0040)

    with Program() as prog:
        with rung(rise(CfgProd)):
            copy(ds[201], DisabledMask)
        with rung(rise(CfgMaint)):
            copy(ds[202], DisabledMask)
        # Request BEFORE the mask chain — see _live_mask_program.
        with rung(CmdStart, StateCurrent == 1):
            copy(6, StateRequested)
        with rung():
            calc(300 + StateRequested, StateMaskIdx)
        with rung():
            copy(ds[StateMaskIdx], StateMask)
        with rung():
            calc(StateMask & DisabledMask, MaskResult)
        with rung(StateRequested != 0, MaskResult == 0, DisabledMask != 0):
            copy(StateRequested, StateCurrent)
            copy(0, StateRequested)
        with rung(StateCurrent == 6):
            out(Output)

    return prog, Output


def test_command_mask_target_is_hand_driveable():
    """Ground truth for the command-selected tier: select the permissive row, start."""
    prog, _output = _command_mask_program()
    plc = PLC(prog)
    plc.step()

    plc.patch({"CfgProd": True})
    plc.step()
    plc.patch({"CfgProd": False})
    plc.step()
    assert plc.state.tags["DisabledMask"] == 0x0001

    plc.patch({"CmdStart": True})
    for _ in range(3):
        plc.step()
    assert plc.state.tags["StateCurrent"] == 6
    assert plc.state.tags["Output"] is True


def test_skiff_gate_command_selected_mask():
    """THE skiff acceptance flip: how() through a command-selected live mask.

    Statically unreadable (two-writer mask — every oracle path punts), but the
    skiff's isolated probes learn which command flips it, the compass carries
    the bearing, and the live verify pipeline confirms the edge. This is the
    boundary gate the sandbox instrument was kept dark for.
    """
    prog, output = _command_mask_program()
    plc = PLC(prog)
    path = pilot_how(plc, output, max_scans=600)
    assert path.reachable, f"skiff gate: {path.reason}"
    replay = path.replay()
    assert replay.state.tags["Output"] is True


@pytest.mark.xfail(
    strict=True,
    reason=(
        "the free-word tier: the live mask is copied from an unconstrained "
        "external word, so the skiff has no sound probe values — unblocking "
        "needs value synthesis (e.g. a bitwise-complement proposal for &==0 "
        "guards). The skiff itself is wired; this gate marks the next tier."
    ),
)
def test_skiff_boundary_gate_live_mask_guard():
    """how() through a free-word enablement guard — needs value synthesis."""
    prog, output = _live_mask_program()
    plc = PLC(prog)
    path = pilot_how(plc, output, max_scans=600)
    assert path.reachable, f"skiff gate: {path.reason}"


def test_unreachable_today_fails_honestly():
    """The free-word tier's miss must carry a named reason — never silent."""
    prog, output = _live_mask_program()
    plc = PLC(prog)
    path = pilot_how(plc, output, max_scans=600)
    if path.reachable:
        pytest.skip("free-word tier solved — retire this test alongside the xfail flip")
    assert path.reason, "unreachable target must always name a reason"


def test_compass_contradict_falsifies_seeded_edge():
    """Live no-change evidence removes a learned edge; the probe mark stays."""
    from pyrung.core.analysis.pilot.compass import Compass

    compass = Compass()
    compass.record("State", ("Cmd", True), 1, 6)
    assert compass.find_path("State", 1, 6) == [("Cmd", True)]

    assert compass.contradict("State", ("Cmd", True), 1) is True
    assert compass.find_path("State", 1, 6) is None
    assert compass.unprobed_actions("State", 1, {("Cmd", True)}) == []
    # Idempotent: no entry left, probe mark preserved.
    assert compass.contradict("State", ("Cmd", True), 1) is False


def test_is_composite_action_shapes():
    """Single action pairs vs skiff-learned joint causes."""
    from pyrung.core.analysis.pilot.compass import is_composite_action

    assert not is_composite_action(("Cmd", True))
    assert not is_composite_action(("Cmd", 3))
    assert is_composite_action((("CfgProd", True), ("CmdStart", True)))
    assert not is_composite_action(())
    assert not is_composite_action("Cmd")
