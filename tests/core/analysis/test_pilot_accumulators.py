"""Tests for PILOT's accumulator resolver and the generalized done-boundary
hypothesis generator (``_done_boundary_hypotheses``).

The resolver maps an ejecting consumer tag — a Done bit or an ``Acc`` register —
back to its owning instruction's profile; the generator turns that into the
corrective hold (oscillate, or stop-holding-the-advance).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pyrung import (
    Bool,
    Counter,
    Program,
    Rung,
    Timer,
    count_up,
    on_delay,
    out,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.accumulators import (
    eject_target,
    resolve_profile,
    scans_to_eject,
)
from pyrung.core.analysis.pilot.investigate import (
    DeviationIncident,
    _done_boundary_hypotheses,
)
from pyrung.core.analysis.pilot.trace import compute_steerable
from pyrung.core.runner import PLC


def _make_ctx(prog: Program, plc: PLC, **overrides: Any) -> SimpleNamespace:
    pdg = build_program_graph(prog)
    steerable = frozenset(compute_steerable(pdg, plc._known_tags_by_name, prog))
    ns: dict[str, Any] = {
        "pdg": pdg,
        "program": prog,
        "steerable": steerable,
        "opaque_loop": frozenset(),
        "pipeline_internal_tags": frozenset(),
        "choice": None,
        "compass": SimpleNamespace(action_tags=frozenset()),
    }
    ns.update(overrides)
    return SimpleNamespace(**ns)


def _plain_timer_program() -> Program:
    run = Bool("run", external=True)
    T = Timer.clone("T")
    Out = Bool("Out")
    with Program() as prog:
        with Rung(run):
            on_delay(T, 50, "ms")  # plain TON, no reset
        with Rung(T.Done):
            out(Out)
    return prog


class TestResolveProfile:
    def test_resolves_via_done_bit(self):
        prog = _plain_timer_program()
        match = resolve_profile("T_Done", prog)
        assert match is not None
        assert match.via_done is True
        assert match.profile.kind == "on_delay"

    def test_resolves_via_accumulator(self):
        prog = _plain_timer_program()
        match = resolve_profile("T_Acc", prog)
        assert match is not None
        assert match.via_done is False
        assert match.profile.accumulator.name == "T_Acc"

    def test_unknown_tag_returns_none(self):
        assert resolve_profile("nope", _plain_timer_program()) is None

    def test_scans_to_eject_analytic(self):
        prog = _plain_timer_program()
        plc = PLC(prog, dt=0.010)
        plc.step()
        match = resolve_profile("T_Done", prog)
        assert match is not None
        # 50 ms / (10 units per 10 ms scan) = 5 scans to Done from acc=0.
        assert eject_target(match, plc) == 50
        assert scans_to_eject(match, plc) == 5


class TestDoneBoundaryHypotheses:
    def test_plain_held_enable_yields_cannot_hold(self):
        # A plain TON ejects PILOT when `run` is held True long enough; the
        # corrective hypothesis is to stop holding `run`.
        prog = _plain_timer_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"run": True})
        for _ in range(8):
            plc.step()
        assert plc.state.tags["T_Done"] is True

        ctx = _make_ctx(prog, plc)
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=plc.state.scan_id,
            action=(("run", True),),
            bearing=(("Out", False),),
            before_snap={"run": True},
            after_snap=dict(plc.state.tags),
            changed_tags=("T_Done", "Out"),
            departures=(),
        )

        hyps = _done_boundary_hypotheses(plc, incident, ctx)
        done_boundary = [h for h in hyps if h.kind == "done-boundary"]
        assert len(done_boundary) == 1
        ((tag, value),) = done_boundary[0].holds
        assert tag == "run"
        assert value is False  # stop holding the advance input

    def test_counter_preset_yields_cannot_hold(self):
        pulse = Bool("pulse", external=True)
        rst = Bool("rst", external=True)
        Out = Bool("Out")
        with Program() as prog:
            with Rung(pulse):
                count_up(Counter[1], preset=3).reset(rst)
            with Rung(Counter[1].Done):
                out(Out)

        plc = PLC(prog, dt=0.010)
        plc.patch({"pulse": True, "rst": False})
        for _ in range(5):
            plc.step()
        assert plc.state.tags["Counter_Done"] is True

        ctx = _make_ctx(prog, plc)
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=plc.state.scan_id,
            action=(("pulse", True),),
            bearing=(("Out", False),),
            before_snap={"pulse": True},
            after_snap=dict(plc.state.tags),
            changed_tags=("Counter_Done", "Counter_Acc", "Out"),
            departures=(),
        )

        hyps = _done_boundary_hypotheses(plc, incident, ctx)
        done_boundary = [h for h in hyps if h.kind == "done-boundary"]
        assert len(done_boundary) == 1
        ((tag, value),) = done_boundary[0].holds
        assert tag == "pulse"
        assert value is False
