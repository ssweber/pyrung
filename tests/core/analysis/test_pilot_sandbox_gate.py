"""Boundary gate for the sandbox instrument (``pilot/sandbox.py``).

Trace and let-run have the burner Starting→Execute acceptance test; this is the
skiff's equivalent. The program below is the live-word twin of the constant-table
mask gate the oracle solves statically (``test_table_oracle.py``): identical
``stateMask & disabledMask == 0`` shape, except the disabled-mask word is
**rewritten at runtime** from an external word (``copy(CfgWord, DisabledMask)``
under a load command). Per ``pilot/CLAUDE.md`` this is exactly the case every
static instrument must punt on — not steerable, not constant-table-backed, no
finite domain — and the documented escalation is the sandbox.

Three facts pin the gate:

1. The target is genuinely reachable — driving the inputs by hand reaches it
   (the failure below is a capability gap, never an unreachable target).
2. The static read punts — ``solve_table_predicate`` returns ``None`` for the
   live operand. If this assertion ever fails, the static layer learned to read
   live words and the gate must be re-examined (it is no longer the skiff case).
3. ``how()`` cannot reach it today, and fails *honestly* (named reason, never a
   fabricated edge). The strict xfail flips the day the skiff is wired into the
   drive loop — at which point remove the xfail and update ``pilot/CLAUDE.md``.
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
        # The constant half: per-state mask from a declared-constant table.
        with rung():
            calc(300 + StateRequested, StateMaskIdx)
        with rung():
            copy(ds[StateMaskIdx], StateMask)
        with rung():
            calc(StateMask & DisabledMask, MaskResult)
        with rung(CmdStart, StateCurrent == 1):
            copy(6, StateRequested)
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "sandbox skiff not wired into the drive loop — this is its boundary "
        "gate (pilot/CLAUDE.md, escalation rule step 4). When it flips: remove "
        "the xfail, assert the plan's shape, and update CLAUDE.md."
    ),
)
def test_skiff_boundary_gate_live_mask_guard():
    """how() through a genuinely-live enablement guard — requires the skiff."""
    prog, output = _live_mask_program()
    plc = PLC(prog)
    path = pilot_how(plc, output, max_scans=600)
    assert path.reachable, f"skiff gate: {path.reason}"


def test_unreachable_today_fails_honestly():
    """Until the skiff is wired, the miss must carry a named reason — never silent."""
    prog, output = _live_mask_program()
    plc = PLC(prog)
    path = pilot_how(plc, output, max_scans=600)
    if path.reachable:
        pytest.skip("skiff gate passed — retire this test alongside the xfail flip")
    assert path.reason, "unreachable target must always name a reason"
