"""PILOT scopes latch corrections to the recorded conductive context."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pyrung import Bool, Int, Or, Program, Rung, latch, out
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.corrections import correct_enablers
from pyrung.core.analysis.pilot.overlay import PilotRung
from pyrung.core.analysis.pilot.types import DeviationIncident
from pyrung.core.analysis.steerable import compute_steerable
from pyrung.core.condition import CompareEq
from pyrung.core.runner import PLC


class _SnapView:
    def __init__(self, snap: dict[str, Any]):
        self._snap = snap

    def get_tag(self, name: str, default: Any = None) -> Any:
        return self._snap.get(name, default)


def _make_ctx(prog: Program, plc: PLC, **overrides: Any) -> SimpleNamespace:
    pdg = build_program_graph(prog)
    steerable = frozenset(compute_steerable(pdg, plc._known_tags_by_name, prog))
    ns: dict[str, Any] = {
        "pdg": pdg,
        "program": prog,
        "steerable": steerable,
        "opaque_loop": frozenset(),
        "pipeline_internal_tags": frozenset(),
        "route": None,
        "compass": SimpleNamespace(action_tags=frozenset()),
    }
    ns.update(overrides)
    return SimpleNamespace(**ns)


def _guard_active(rung: PilotRung, snap: dict[str, Any]) -> bool:
    return bool(rung.guard.evaluate(_SnapView(snap)))


class TestExposureGuard:
    def test_uses_actual_condition_from_recorded_alias_writer(self) -> None:
        """Only the state that conducted this incident scopes the correction."""
        Door = Bool("Door", external=True)
        Lint = Bool("Lint", external=True)
        State = Int("State", default=4)
        InStarting = Bool("InStarting")
        InUnholding = Bool("InUnholding")
        Alarm = Bool("Alarm")
        LintAlarm = Bool("LintAlarm")

        with Program() as prog:
            with Rung(State == 3):
                out(InStarting)
            with Rung(State == 12):
                out(InUnholding)
            with Rung(Or(InStarting, InUnholding), ~Door):
                latch(Alarm)
            with Rung(Or(InStarting, InUnholding), ~Lint):
                latch(LintAlarm)

        plc = PLC(prog, dt=0.010)
        ctx = _make_ctx(prog, plc, opaque_loop=frozenset({"State"}))
        before = dict(plc.state.tags)
        anchor = plc.state.scan_id

        plc.patch({"State": 3})
        plc.step()
        incident = DeviationIncident(
            anchor_scan=anchor,
            departure_scan=plc.state.scan_id,
            end_scan=plc.state.scan_id,
            action=(("State", 3),),
            bearing=(("State", 3),),
            before_snap=before,
            after_snap=dict(plc.state.tags),
            changed_tags=("State", "Alarm", "LintAlarm"),
            departures=(),
            channel_tag="State",
        )

        hyps = correct_enablers(plc, incident, ctx)
        joint = next(h for h in hyps if len(h.holds) == 2)
        assert all(isinstance(proposed, PilotRung) for proposed in joint.holds)
        for proposed in joint.holds:
            # This is the original mapping rung condition, not a sampled
            # equality and not a union of every state where the alarm could
            # theoretically fire.
            assert isinstance(proposed.guard, CompareEq)
            assert proposed.guard.tag.name == "State"
            assert proposed.guard.value == 3
            assert _guard_active(proposed, {"State": 3})
            assert not _guard_active(proposed, {"State": 6})
            assert not _guard_active(proposed, {"State": 12})

    def test_no_recorded_channel_condition_keeps_pair_proposal(self) -> None:
        """Without causal channel evidence, Pilot does not invent a scope."""
        Guard = Bool("Guard", external=True)
        Alarm = Bool("Alarm")

        with Program() as prog:
            with Rung(~Guard):
                latch(Alarm)

        plc = PLC(prog, dt=0.010)
        ctx = _make_ctx(prog, plc)
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=1,
            action=(),
            bearing=(("Alarm", False),),
            before_snap={"Guard": False, "Alarm": False},
            after_snap={"Guard": False, "Alarm": True},
            changed_tags=("Alarm",),
            departures=(),
        )

        hyps = correct_enablers(plc, incident, ctx)
        assert len(hyps) == 1
        assert hyps[0].holds == (("Guard", True),)
