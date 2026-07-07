"""Boundary gate for the compass bridge (``pilot/causal.py``).

The cause-chain walkers (``chase_cause_roots`` / ``chase_chain_tags``) dead-end at
a PackML jump table: ``S_StateCurrent`` is written by an indirect copy gated by a
freshly-computed constant-table enable flag, while the requester
(``S_StateRequested``) is a *held* enabler at the transfer scan — added by name
but never recursed — so the recorded walk stops short of the watchdog that
requested the state.  The bridge crosses that hop by route inversion: it consults
``ctx.compass.graphs`` for the requesters of the observed destination transition,
confirms which route fired against recorded history, and resumes the walk from
that route's guard tags.

Gate discipline (pilot/CLAUDE.md §Boundary gates):

* ``test_watchdog_starve_ejects`` — hand-driveable ground truth: a starved
  watchdog latches the alarm which requests the state, bumping StateCurrent 6->8.
* ``test_recorded_walk_dead_ends_without_bridge`` — the **punt pin**: the plain
  recorded walk does NOT reach the watchdog.  Permanent; trips if the recorded
  walker ever gets smart enough to cross the pipeline on its own.
* ``test_bridge_reaches_watchdog`` — the capability: WITH the bridge the chain
  reaches the watchdog Done bit.
* ``TestBridgePipelineHop`` — unit tests of ``_bridge_pipeline_hop`` over
  hand-fed routes (confirmed / wrong-value-punts / no-routes-punts).
* ``test_ranking_prefers_watchdog_with_bridge`` — the ranking seam: causal
  primacy puts the watchdog first only once the bridge reaches it; without it the
  same-scan collateral wins on temporal proximity (luck).
"""

from __future__ import annotations

from types import SimpleNamespace
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
from pyrung.core.analysis.causal.models import Transition
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.causal import (
    _Bridge,
    _bridge_pipeline_hop,
    chase_chain_tags,
)
from pyrung.core.analysis.pilot.compass import Compass, build_compass_graphs
from pyrung.core.analysis.pilot.evidence import (
    TransitionRoute,
    infer_pipeline_roles,
)
from pyrung.core.analysis.pilot.investigate import (
    InvestigationHypothesis,
    _rank_hypotheses,
)
from pyrung.core.analysis.pilot.trace import compute_steerable
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
        # destination — this is what makes the recorded walk dead-end).
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
        # exactly what dead-ends the recorded walk short of the alarm.
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


def _real_bridge(prog: Program, plc: PLC) -> SimpleNamespace:
    """A duck-typed bridge carrying the fixture's real compass graphs."""
    pdg = build_program_graph(prog)
    steer = frozenset(compute_steerable(pdg, plc._known_tags_by_name, prog))
    role = infer_pipeline_roles("StateCurrent", pdg, prog, steer, frozenset())
    graphs = build_compass_graphs((role,), pdg, prog, steer, frozenset(), None)
    compass = Compass()
    compass.set_graphs(graphs)
    return SimpleNamespace(compass=compass)


# ---------------------------------------------------------------------------
# Ground truth + the dead-end pin + the capability
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


def test_recorded_walk_dead_ends_without_bridge():
    """Punt pin (permanent): the plain recorded walk cannot cross the jump-table
    hop, so the watchdog Done never enters the chain.  If this ever starts
    failing, the recorded walker learned to cross the pipeline on its own and this
    gate — and the bridge's reason for existing — needs rework."""
    prog, _output = _pipeline_watchdog_program()
    plc = _boot(prog)
    eject = _drive_to_eject(plc)

    tags = chase_chain_tags(plc, "StateCurrent", scan=eject)
    assert "OnWD_Done" not in tags
    assert "OffWD_Done" not in tags
    # It genuinely dead-ends at the held pipeline enable flag.
    assert "StateEnableYes" in tags


def test_bridge_reaches_watchdog():
    """WITH the bridge the chain crosses the hop and reaches the watchdog."""
    prog, _output = _pipeline_watchdog_program()
    plc = _boot(prog)
    eject = _drive_to_eject(plc)

    bridge = _real_bridge(prog, plc)
    tags = chase_chain_tags(plc, "StateCurrent", scan=eject, bridge=bridge)
    # The requester chain is recovered: the held request register, the latched
    # alarm, and the watchdog Done that starved.
    assert "StateRequested" in tags
    assert "Alm" in tags
    assert "OnWD_Done" in tags


# ---------------------------------------------------------------------------
# Unit tests of _bridge_pipeline_hop over hand-fed routes
# ---------------------------------------------------------------------------


def _route(request_value: Any, enablers: tuple[tuple[str, Any], ...]) -> TransitionRoute:
    return TransitionRoute(
        destination_tag="StateCurrent",
        destination_value=8,
        request_tag="StateRequested",
        request_value=request_value,
        source_constraints=(),
        enablers=enablers,
        action_tags=frozenset(),
        writer_node=0,
        writer_subroutine=None,
        call_site_gates=(),
    )


def _bridge_from_routes(*routes: TransitionRoute) -> _Bridge:
    graph = SimpleNamespace(routes=tuple(routes))
    return _Bridge(SimpleNamespace(compass=SimpleNamespace(graphs=(graph,))))


class TestBridgePipelineHop:
    def _driven(self) -> tuple[PLC, int]:
        prog, _output = _pipeline_watchdog_program()
        plc = _boot(prog)
        eject = _drive_to_eject(plc)
        return plc, eject

    def test_confirmed_route_bridges(self):
        plc, eject = self._driven()
        effect = Transition("StateCurrent", eject, 6, 8)
        bridge = _bridge_from_routes(_route(8, (("Alm", True),)))
        resumes = _bridge_pipeline_hop(plc, effect, bridge)
        tags = {t for t, _v, _s in resumes}
        assert "StateRequested" in tags
        assert "Alm" in tags
        # The request resume points at the scan its value was recorded (held one
        # scan before the transfer), not the transfer scan.
        req = next((s for t, _v, s in resumes if t == "StateRequested"), None)
        assert req == eject - 1

    def test_wrong_request_value_punts(self):
        plc, eject = self._driven()
        effect = Transition("StateCurrent", eject, 6, 8)
        # No scan ever recorded StateRequested == 99: the route did not fire.
        bridge = _bridge_from_routes(_route(99, (("Alm", True),)))
        assert _bridge_pipeline_hop(plc, effect, bridge) == []

    def test_no_routes_punts(self):
        plc, eject = self._driven()
        effect = Transition("StateCurrent", eject, 6, 8)
        bridge = _bridge_from_routes()
        assert _bridge_pipeline_hop(plc, effect, bridge) == []

    def test_direct_route_skipped(self):
        plc, eject = self._driven()
        effect = Transition("StateCurrent", eject, 6, 8)
        direct = TransitionRoute(
            destination_tag="StateCurrent",
            destination_value=8,
            request_tag=None,
            request_value=None,
            source_constraints=(),
            enablers=(("CmdAbort", True),),
            action_tags=frozenset({"CmdAbort"}),
            writer_node=0,
            writer_subroutine=None,
            call_site_gates=(),
        )
        assert _bridge_pipeline_hop(plc, effect, _bridge_from_routes(direct)) == []


# ---------------------------------------------------------------------------
# The ranking seam — causal primacy is exact only with the bridge
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
        governing_tag="StateCurrent",
    )


def test_ranking_prefers_watchdog_with_bridge():
    """The watchdog hypothesis ranks first only once the bridge places the
    watchdog Done inside the governing chain.  Without the bridge the same-scan
    collateral (state-8 shared-init resetting Step) ties it on chain membership
    and wins on temporal proximity — the luck the bridge removes."""
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

    ctx_with = _real_bridge(prog, plc)
    ranked_with = _rank_hypotheses(plc, [collateral, watchdog], incident, ctx_with)
    assert ranked_with[0] is watchdog

    # Without the bridge, the governing chain dead-ends short of the watchdog, so
    # OnWD_Done is not in `primal` and the collateral wins on proximity.
    empty = Compass()
    empty.set_graphs(())
    ctx_without = SimpleNamespace(compass=empty)
    ranked_without = _rank_hypotheses(plc, [collateral, watchdog], incident, ctx_without)
    assert ranked_without[0] is collateral
