"""Boundary gate for the native pipeline crossing (``pilot/causal.py``).

The cause-chain walkers (``chase_cause_roots`` / ``chase_chain_tags``) once
dead-ended at a PackML jump table: ``S_StateCurrent`` is written by an indirect
copy gated by a freshly-computed constant-table enable flag, while the requester
(``S_StateRequested``) is a *held* enabler at the transfer scan — so the shallow
recorded walk stopped short of the watchdog that requested the state.  A
route-inversion *compass bridge* used to cross that hop.

The deep ``cause()`` walk crosses it natively: it chases the held enable-flag /
request enabler to its establishing transition and continues the recorded walk
from there, reaching the latched alarm and the starved watchdog without any
route inversion.  These gates pin that native crossing — the bridge is gone.

Gate discipline (pilot/CLAUDE.md §Testing changes):

* ``test_watchdog_starve_ejects`` — hand-driveable ground truth: a starved
  watchdog latches the alarm which requests the state, bumping StateCurrent 6->8.
* ``test_recorded_walk_crosses_pipeline_natively`` — the capability: the deep
  recorded walk reaches the requester chain (held request register, latched
  alarm, watchdog Done) with no bridge.  Permanent; trips if the deep walk ever
  loses the ability to cross the pipeline hop on its own.
* ``test_ranking_prefers_watchdog_natively`` — the ranking seam: causal primacy
  puts the watchdog first because the deep chain places the watchdog Done inside
  the channel chain, so the same-scan collateral no longer ties it on chain
  membership and wins on temporal proximity (the luck the crossing removes).
"""

from __future__ import annotations

from typing import Any

from pyrung import (
    PLC,
    Bool,
    Int,
    Program,
    Rung,
    Timer,
    calc,
    copy,
    latch,
    on_delay,
    rung,
)
from pyrung.core.analysis.pilot.causal import chase_chain_tags
from pyrung.core.analysis.pilot.investigate import (
    InvestigationHypothesis,
    _rank_hypotheses,
)
from pyrung.core.analysis.pilot.types import BearingDeparture, DeviationIncident

# ---------------------------------------------------------------------------
# The gate program — a PackML jump-table pipeline whose ABORT request comes
# from a starved complement-reset watchdog (not a steerable command).
# ---------------------------------------------------------------------------


def _pipeline_watchdog_program() -> tuple[Program, Any]:
    from pyrung.click import ClickBlocks

    x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

    Sensor = Bool("Sensor", external=True)
    CmdAbort = Bool("CmdAbort", external=True)
    StateRequested = Int("StateRequested", default=0)
    StateCurrent = Int("StateCurrent", default=1)
    StateJumpIdx = Int("StateJumpIdx")
    StateEnableYes = Int("StateEnableYes")
    Step = Int("Step")
    Output = Bool("Output")
    OffWD = Timer.clone("OffWD")
    OnWD = Timer.clone("OnWD")
    Alm = Bool("Alm")

    # Identity jump table: ds[150 + n] = n.
    ds.slot(156, name="jump_execute", default=6)
    ds.slot(158, name="jump_abort", default=8)

    with Program(strict=False) as prog:
        # Complement-reset watchdogs — starve unless Sensor oscillates in EXECUTE.
        with Rung(StateCurrent == 6):
            on_delay(OffWD, 40, "ms").reset(Sensor)
        with Rung(StateCurrent == 6):
            on_delay(OnWD, 40, "ms").reset(~Sensor)
        # Watchdog starves -> alarm latches.
        with Rung(OffWD.Done):
            latch(Alm)
        with Rung(OnWD.Done):
            latch(Alm)
        # Alarm handling requests ABORT (writes the REQUEST register, not the
        # destination — this is what made the shallow walk dead-end).
        with Rung(Alm):
            copy(8, StateRequested)
        # Operator abort command (steerable) — same request register.  Present so
        # the compass graph carries a command edge (as any PackML program does);
        # never pressed in the watchdog-starve scenario.
        with Rung(CmdAbort):
            copy(8, StateRequested)
        # The opaque pipeline transition rung: gated by the freshly-computed
        # enable flag (a constant-table predicate), copies the requested state
        # through the jump table, and clears the request in the same scan.  The
        # enable flag is computed AFTER this rung, so it lags one scan:
        # StateRequested is a HELD enabler at the transfer scan (not a trigger) —
        # exactly what dead-ended the shallow walk short of the alarm.
        with rung(StateEnableYes == 1):
            copy(ds[StateJumpIdx], StateCurrent)
            copy(0, StateRequested)
            copy(0, StateEnableYes)
        with rung():
            calc(150 + StateRequested, StateJumpIdx)
        with rung(StateRequested != 0):
            copy(1, StateEnableYes)
        # Same-scan collateral: the state-8 shared-init resets progress at
        # exactly the ejection scan (ties the watchdog on temporal proximity).
        with Rung(StateCurrent == 8):
            copy(0, Step)
        # Progress advances in EXECUTE.
        with Rung(StateCurrent == 6, Step < 2):
            copy(Step + 1, Step)
        # Target.
        with Rung(StateCurrent == 6, Step >= 2):
            latch(Output)

    return prog, Output


def _boot(prog: Program) -> PLC:
    plc = PLC(prog, dt=0.010)
    plc.patch({"StateCurrent": 6})
    plc.step()
    return plc


def _drive_to_eject(plc: PLC, limit: int = 30) -> int:
    """Hold Sensor steady so a watchdog starves; return the ejection scan."""
    for _ in range(limit):
        plc.force("Sensor", True)
        plc.step()
        if plc.state.tags.get("StateCurrent") == 8:
            break
    return plc.state.scan_id


# ---------------------------------------------------------------------------
# Ground truth + the native crossing
# ---------------------------------------------------------------------------


def test_watchdog_starve_ejects():
    """The plant can do it: a steady sensor starves a watchdog, the alarm latches
    and requests the state, and StateCurrent is bumped 6 -> 8 (ABORT)."""
    prog, _output = _pipeline_watchdog_program()
    plc = _boot(prog)
    eject = _drive_to_eject(plc)
    assert plc.state.tags["StateCurrent"] == 8
    assert plc.state.tags["Alm"] is True
    # StateRequested==8 is recorded one scan before the transfer (held enabler).
    assert plc.history.at(eject - 1).tags["StateRequested"] == 8
    assert plc.history.at(eject).tags["StateRequested"] == 0


def test_recorded_walk_crosses_pipeline_natively():
    """The deep recorded walk crosses the jump-table hop with no bridge: it
    chases the held enable-flag / request enabler to the scan the alarm set it,
    and continues to the latched alarm and the watchdog Done that starved.

    If this ever starts failing, the deep walk lost the ability to cross the
    pipeline on its own and both this gate and the pilot's reliance on the native
    crossing need rework."""
    prog, _output = _pipeline_watchdog_program()
    plc = _boot(prog)
    eject = _drive_to_eject(plc)

    tags = chase_chain_tags(plc, "StateCurrent", scan=eject)
    # The requester chain is recovered: the held request register, the latched
    # alarm, and the watchdog Done that starved.
    assert "StateRequested" in tags
    assert "Alm" in tags
    assert "OnWD_Done" in tags


# ---------------------------------------------------------------------------
# The ranking seam — causal primacy is exact from the native crossing
# ---------------------------------------------------------------------------


def _incident(anchor: int, eject: int) -> DeviationIncident:
    return DeviationIncident(
        anchor_scan=anchor,
        departure_scan=eject,
        end_scan=eject,
        action=(),
        bearing=(("Output", True),),
        before_snap={},
        after_snap={},
        changed_tags=("StateCurrent", "Step"),
        departures=(
            BearingDeparture("StateCurrent", 8, eject),
            BearingDeparture("Step", 0, eject),
        ),
        channel_tag="StateCurrent",
    )


def test_ranking_prefers_watchdog_natively():
    """The watchdog hypothesis ranks first because the deep chain places the
    watchdog Done inside the channel chain — no bridge.  The same-scan collateral
    (state-8 shared-init resetting Step) is a downstream *effect* of StateCurrent,
    not a cause, so it never enters StateCurrent's backward chain and loses on
    chain membership."""
    prog, _output = _pipeline_watchdog_program()
    plc = _boot(prog)
    anchor = 1
    eject = _drive_to_eject(plc)

    watchdog = InvestigationHypothesis(
        kind="liveness",
        holds=(("Sensor", True),),
        sources=("OnWD_Done", "Sensor"),
        detail="watchdog starved",
    )
    collateral = InvestigationHypothesis(
        kind="precise-cause",
        holds=(("Step", 0),),
        sources=("Step",),
        detail="Step reset at ejection",
    )
    incident = _incident(anchor, eject)

    # ctx is only consulted as the (now ignored) bridge= argument, so causal
    # primacy is decided entirely by the native deep chain.
    ranked = _rank_hypotheses(plc, [collateral, watchdog], incident, None)
    assert ranked[0] is watchdog
