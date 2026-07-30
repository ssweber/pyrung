"""PILOT gate: absence-root correctives — the sail-trap shape, in miniature.

A fault whose true cause **never moved** is invisible to the shallow chase:
``Sail`` (external, resting False) drives an intermediate error register,
which feeds an alarm delay, a latch, and a laundering calc before the state
machine aborts Execute(6) -> Aborting(8) — the recorded chain of the abort
dead-ends at things that *did* move.  The deep cause walk names ``Sail``
as a never-moved external root, and investigation must turn that into the
confirmed corrective hold ``Sail=True``.

The fixture also plants ``Suspend`` — a response-side gate on the abort rung
itself (``~Suspend``).  Holding it True also keeps the channel in place for
any bounded replay, so it is indistinguishable from the true fix by replay
alone: the gate pins that the deeper terminal (the cause) outranks the
shallower one (the response suppressor).

Born with the absence-root mechanism (corrections.py::_absence_root_
correctives); the live check is the burner drive
``how S_StateCurrent==17 avoid C_Complete`` (machine-local,
scratchpad/burner/repro_sail17.py).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pyrung import Bool, Int, Program, Real, Rung, Timer, calc, copy, latch, on_delay, out
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.coast import CoastTriggerEvent
from pyrung.core.analysis.pilot.corrections import _absence_root_correctives
from pyrung.core.analysis.pilot.investigate import (
    ReplayIncident,
    ReplayStep,
    build_deviation_incident,
    build_replay_fn,
    incident_regression_witness,
    investigate_deviation,
)
from pyrung.core.analysis.pilot.navigation_contracts import TargetSpec
from pyrung.core.analysis.steerable import compute_steerable
from pyrung.core.runner import PLC


def _sail_trap_program() -> Program:
    Sail = Bool("Sail", external=True)  # the never-moved permissive (rests False)
    Suspend = Bool("Suspend", external=True)  # response-side gate on the abort rung
    Tmr = Timer.clone("SailTmr")
    HeatErr = Bool("HeatErr")  # buffered intermediate
    AlmTrig = Bool("AlmTrig")
    AlmExtent = Int("AlmExtent")
    Phase = Int("Phase")

    with Program() as prog:
        with Rung(Phase == 0):
            copy(6, Phase)  # boot straight into Execute
        with Rung(Phase == 6, ~Sail):
            out(HeatErr)  # intermediate error register
        with Rung(HeatErr):
            on_delay(Tmr, 100, "ms")  # alarm delay (10 scans at 10 ms)
        with Rung(Tmr.Done):
            latch(AlmTrig)  # latched alarm
        with Rung(Phase >= 0):
            calc(AlmTrig * 1, AlmExtent)  # laundering hop (block-sum stand-in)
        with Rung(AlmExtent >= 1, ~Suspend):
            copy(8, Phase)  # abort Execute -> Aborting

    return prog


def _drive_to_abort(
    program_factory: Any = None,
) -> tuple[PLC, PLC, int, dict[str, Any]]:
    """Run from cold to the 6->8 abort; return (work, checkpoint, anchor, before)."""
    plc = PLC(prog := (program_factory or _sail_trap_program)(), dt=0.010)
    plc.step()  # Phase 0 -> 6
    assert plc.state.tags["Phase"] == 6
    cp = plc.fork()
    anchor = plc.state.scan_id
    before = dict(plc.state.tags)
    for _ in range(60):
        plc.step()
        if plc.state.tags["Phase"] == 8:
            break
    assert plc.state.tags["Phase"] == 8, "fixture must abort on its own"
    plc._sail_trap_prog = prog  # keep the Program reachable for callers
    return plc, cp, anchor, before


def _ctx(prog: Program, plc: PLC, **overrides: Any) -> SimpleNamespace:
    pdg = build_program_graph(prog)
    steerable = frozenset(compute_steerable(pdg, plc._known_tags_by_name, prog))
    ns: dict[str, Any] = {
        "pdg": pdg,
        "program": prog,
        "steerable": steerable,
        "resting": {tag: False for tag in steerable if isinstance(plc.state.tags.get(tag), bool)},
        "edge_tags": set(),
        "opaque_loop": frozenset(),
        "pipeline_internal_tags": frozenset(),
        "route": None,
        "compass": SimpleNamespace(action_tags=frozenset()),
        "pipeline_roles": (),
        "target": TargetSpec("Phase", 17),
        "domain_prior": None,
        "clear_only": frozenset(),
    }
    ns.update(overrides)
    return SimpleNamespace(**ns)


def _incident(plc: PLC, anchor: int, before: dict[str, Any]):
    return build_deviation_incident(
        anchor_scan=anchor,
        end_scan=plc.state.scan_id,
        action=(),
        bearing=(("Phase", 6),),
        before_snap=before,
        after_snap=dict(plc.state.tags),
        timeline=(
            CoastTriggerEvent(
                "pen",
                "pen",
                plc.state.scan_id,
                (("Phase", 6, plc.state.tags["Phase"]),),
            ),
        ),
        channel_tag="Phase",
    )


class TestAbsenceRootGeneration:
    """_absence_root_correctives names the never-moved permissive."""

    def test_sail_named_deepest_first(self) -> None:
        plc, _cp, anchor, before = _drive_to_abort()
        prog = plc._sail_trap_prog
        incident = _incident(plc, anchor, before)
        ctx = _ctx(prog, plc)

        hyps, primal = _absence_root_correctives(plc, incident, ctx)
        holds = [h.holds for h in hyps]
        assert (("Sail", True),) in holds
        assert "Sail" in primal
        # Suspend (response-side, shallower terminal) may also surface, but
        # the deeper fault-generation terminal must come first.
        assert holds[0] == (("Sail", True),)
        assert all(h.kind == "absence-root" for h in hyps)

    def test_pilot_touched_roots_excluded(self) -> None:
        plc, _cp, anchor, before = _drive_to_abort()
        prog = plc._sail_trap_prog
        incident = _incident(plc, anchor, before)
        ctx = _ctx(prog, plc)

        hyps, primal = _absence_root_correctives(plc, incident, ctx, exclude=frozenset({"Sail"}))
        assert (("Sail", True),) not in [h.holds for h in hyps]
        assert "Sail" not in primal


def _cold_heater_program() -> Program:
    """The sail trap with an analog permissive: ``Temp`` (Real, external,
    resting 0.0) supports the fault through an ordered comparison against a
    program-written threshold — the burner's Execute-wait shape in miniature.
    A Bool flip does not exist for ``Temp``; the corrective must cross the
    comparison's boundary instead (``Temp > Setpoint``)."""
    Temp = Real("Temp", external=True)  # the never-moved analog (rests 0.0)
    Suspend = Bool("Suspend", external=True)  # response-side gate on the abort rung
    Tmr = Timer.clone("HeatTmr")
    HeatErr = Bool("HeatErr")
    AlmTrig = Bool("AlmTrig")
    AlmExtent = Int("AlmExtent")
    Phase = Int("Phase")
    Setpoint = Int("Setpoint")

    with Program() as prog:
        with Rung(Phase == 0):
            copy(130, Setpoint)  # computed threshold, resolved from the snapshot
            copy(6, Phase)  # boot straight into Execute
        with Rung(Phase == 6, Temp <= Setpoint):
            out(HeatErr)  # cold heater -> intermediate error register
        with Rung(HeatErr):
            on_delay(Tmr, 100, "ms")  # alarm delay (10 scans at 10 ms)
        with Rung(Tmr.Done):
            latch(AlmTrig)  # latched alarm
        with Rung(Phase >= 0):
            calc(AlmTrig * 1, AlmExtent)  # laundering hop (block-sum stand-in)
        with Rung(AlmExtent >= 1, ~Suspend):
            copy(8, Phase)  # abort Execute -> Aborting

    return prog


class TestAnalogAbsenceRoot:
    """A wide-word root becomes a boundary-crossing hold, not a silent skip."""

    def test_temp_boundary_hold_generated(self) -> None:
        plc, _cp, anchor, before = _drive_to_abort(_cold_heater_program)
        prog = plc._sail_trap_prog
        incident = _incident(plc, anchor, before)
        ctx = _ctx(prog, plc)

        hyps, primal = _absence_root_correctives(plc, incident, ctx)
        temp_holds = [h for h in hyps if h.holds[0][0] == "Temp"]
        assert temp_holds, "the analog root must produce a corrective, not a skip"
        assert "Temp" in primal
        _tag, value = temp_holds[0].holds[0]
        # Crossing Temp <= Setpoint means strictly past the 130 threshold.
        assert value > 130
        assert "Temp > Setpoint" in temp_holds[0].detail

    def test_analog_root_confirmed_by_replay(self) -> None:
        plc, cp, anchor, before = _drive_to_abort(_cold_heater_program)
        prog = plc._sail_trap_prog
        incident = _incident(plc, anchor, before)
        ctx = _ctx(prog, plc)
        witness = incident_regression_witness(plc, incident)
        assert witness is not None
        assert (
            witness.cause[0].rung.subroutine,
            witness.cause[0].rung.rung_index,
            witness.cause[0].tag,
        ) == (None, 5, "Phase")
        assert len(witness.cause) > 1

        # A recorded let-run step: the coast holds Phase and re-arms toward the
        # channel target, bounded by its own span (the abort window).
        steps = [ReplayStep(inputs=(), scans=incident.end_scan - anchor, kind="letrun")]
        replay = build_replay_fn(
            cp,
            99,
            {},
            steps,
            ctx=ctx,
            incident=ReplayIncident(
                channel_tag="Phase",
                channel_target=6,
                terminal_role_tags=("Phase",),
                watch_roles=("Phase",),
                regression_witness=witness,
            ),
        )

        result = investigate_deviation(plc, incident, ctx, replay)
        assert result.correction is not None
        temp_holds = [
            (rung.dest, rung.value) for rung in result.correction.pilot_rungs if rung.dest == "Temp"
        ]
        assert temp_holds, "the boundary hold must survive its replay"
        assert temp_holds[0][1] > 130
        assert not any(
            rung.dest == "Suspend" and rung.value is True for rung in result.correction.pilot_rungs
        )


class TestAbsenceRootConfirmation:
    """investigate_deviation confirms the stuck permissive, not the suppressor."""

    def test_confirms_sail_over_suspend(self) -> None:
        plc, cp, anchor, before = _drive_to_abort()
        prog = plc._sail_trap_prog
        incident = _incident(plc, anchor, before)
        ctx = _ctx(prog, plc)
        witness = incident_regression_witness(plc, incident)
        assert witness is not None
        assert (
            witness.cause[0].rung.subroutine,
            witness.cause[0].rung.rung_index,
            witness.cause[0].tag,
        ) == (None, 5, "Phase")
        assert len(witness.cause) > 1

        # A recorded let-run step: the coast holds Phase and re-arms toward the
        # channel target, bounded by its own span (the abort window).
        steps = [ReplayStep(inputs=(), scans=incident.end_scan - anchor, kind="letrun")]
        replay = build_replay_fn(
            cp,
            99,
            {},
            steps,
            ctx=ctx,
            incident=ReplayIncident(
                channel_tag="Phase",
                channel_target=6,
                terminal_role_tags=("Phase",),
                watch_roles=("Phase",),
                regression_witness=witness,
            ),
        )

        raw = replay((("Sail", True),))
        assert raw.accepted, raw
        result = investigate_deviation(plc, incident, ctx, replay)
        assert result.correction is not None
        assert any(
            rung.dest == "Sail" and rung.value is True for rung in result.correction.pilot_rungs
        )
        assert not any(
            rung.dest == "Suspend" and rung.value is True for rung in result.correction.pilot_rungs
        )
        confirmed_kinds = {h.kind for h in result.confirmed}
        assert "absence-root" in confirmed_kinds
