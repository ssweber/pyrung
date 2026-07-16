"""PILOT gate: a latch correction's lifetime is the exposure, not the journey.

The shape (a miniature of the burner's door requirement, no burner names):

* a channel register with ``sm_MapVal2State``-style alias Bools;
* an **alarm latch** that fires in two states (``Or(InStarting, InUnholding)``)
  when the door input is absent;
* a **command writer** that pushes the channel (Hold) in a third state
  (``InExecute``) when either door input is absent — its write reaches the
  channel through an ordinary command register, not a latch;
* a **warning** rung at a fourth state (``InHeld``) that reads the same door
  input but whose write leads nowhere.

The corrective ``Door=True`` must carry a guard active in every state where a
*silenced antagonist* can fire — the alarm's own ``Or`` plus (for the joint
door+lint correction) the command writer's state — and inactive in the
warning-only state, so a later held-state door-open advance still works.

The guard is read structurally off the antagonist rungs' own conditions.  No
state literal is invented: an unreadable exposure falls back to pair-shaped
proposals (legacy landing scoping downstream).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pyrung import Bool, Int, Or, Program, Rung, copy, latch, out
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot._ops import PilotRung
from pyrung.core.analysis.pilot.corrections import correct_enablers
from pyrung.core.analysis.pilot.investigate import DeviationIncident
from pyrung.core.analysis.steerable import compute_steerable
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


def _alias_snap(**states: bool) -> dict[str, Any]:
    """A snapshot with the alias Bools set per keyword (others False)."""
    snap = {
        "InStarting": False,
        "InUnholding": False,
        "InExecute": False,
        "InHeld": False,
        "Door": False,
        "Lint": False,
    }
    snap.update(states)
    return snap


def _guard_active(rung: PilotRung, snap: dict[str, Any]) -> bool:
    return bool(rung.guard.evaluate(_SnapView(snap)))


def _door_cycle_program() -> Program:
    Door = Bool("Door", external=True)
    Lint = Bool("Lint", external=True)
    State = Int("State", default=4)
    InStarting = Bool("InStarting")
    InUnholding = Bool("InUnholding")
    InExecute = Bool("InExecute")
    InHeld = Bool("InHeld")
    Alarm = Bool("Alarm")
    LintAlarm = Bool("LintAlarm")
    Warn = Bool("Warn")
    Cmd = Int("Cmd")

    with Program() as prog:
        # sm_MapVal2State idiom: alias Bools written under channel equality.
        with Rung(State == 3):
            out(InStarting)
        with Rung(State == 12):
            out(InUnholding)
        with Rung(State == 6):
            out(InExecute)
        with Rung(State == 11):
            out(InHeld)
        # Latch antagonists: the ladder names its own contexts.
        with Rung(Or(InStarting, InUnholding), ~Door):
            latch(Alarm)
        with Rung(Or(InStarting, InUnholding), ~Lint):
            latch(LintAlarm)
        # Command antagonist: pushes the channel in a third state — silenced
        # only by the JOINT door+lint correction.
        with Rung(InExecute, Or(~Door, ~Lint)):
            copy(4, Cmd)
        # The command register drives the channel, so its downstream reaches
        # the opaque loop.
        with Rung(Cmd == 4):
            copy(10, State)
            copy(0, Cmd)
        # Bystander: reads the door in a fourth state, write leads nowhere.
        with Rung(InHeld, ~Door):
            out(Warn)
    return prog


def _door_incident() -> DeviationIncident:
    before = _alias_snap(InStarting=True)
    after = dict(before, Alarm=True, LintAlarm=True)
    return DeviationIncident(
        anchor_scan=0,
        departure_scan=None,
        end_scan=5,
        action=(("Go", True),),
        bearing=(("Alarm", False), ("LintAlarm", False)),
        before_snap=before,
        after_snap=after,
        changed_tags=("Alarm", "LintAlarm"),
        departures=(),
    )


class TestExposureGuard:
    def test_single_input_correction_covers_every_latch_state(self):
        prog = _door_cycle_program()
        plc = PLC(prog, dt=0.010)
        ctx = _make_ctx(prog, plc, opaque_loop=frozenset({"State"}))

        hyps = correct_enablers(plc, _door_incident(), ctx)
        door_only = [
            h
            for h in hyps
            if h.kind == "latch-exposure"
            and len(h.holds) == 1
            and isinstance(h.holds[0], PilotRung)
            and h.holds[0].dest == "Door"
        ]
        assert door_only, f"no guarded Door rung proposed: {[h.holds for h in hyps]}"
        rung = door_only[0].holds[0]
        assert rung.value is True

        # Active in every state where the silenced latch can arm ...
        assert _guard_active(rung, _alias_snap(InStarting=True))
        assert _guard_active(rung, _alias_snap(InUnholding=True))
        # ... and inactive where only the bystander reads the door.  The
        # command writer is NOT silenced by the door alone (the lint arm keeps
        # it fireable), so Execute stays out of the single-input guard.
        assert not _guard_active(rung, _alias_snap(InExecute=True))
        assert not _guard_active(rung, _alias_snap(InHeld=True))

    def test_joint_correction_also_covers_the_command_writer_state(self):
        prog = _door_cycle_program()
        plc = PLC(prog, dt=0.010)
        ctx = _make_ctx(prog, plc, opaque_loop=frozenset({"State"}))

        hyps = correct_enablers(plc, _door_incident(), ctx)
        joint = [h for h in hyps if len(h.holds) == 2]
        assert joint, "no joint door+lint proposal"
        rungs = joint[0].holds
        assert all(isinstance(r, PilotRung) for r in rungs), "joint correction must own its guard"
        for rung in rungs:
            # The joint assignment silences the command writer too, so the
            # guard now spans its state alongside the latch's own states.
            assert _guard_active(rung, _alias_snap(InExecute=True))
            assert _guard_active(rung, _alias_snap(InStarting=True))
            assert _guard_active(rung, _alias_snap(InUnholding=True))
            # The warning-only state still releases the input.
            assert not _guard_active(rung, _alias_snap(InHeld=True))

    def test_state_gate_downstream_of_the_silenced_rung_is_collected(self):
        """The FB shape: the lever sits behind a ReadInputs image copy; one
        silenced consumer (the latch) carries its own state gate, another (a
        stateless error producer) is gated only downstream, on the command
        writer its consequence flows through.  Both contexts must land in the
        guard — the union is the exposure."""
        FB = Bool("FB", external=True)
        iFB = Bool("iFB")
        ErrBit = Bool("ErrBit")
        State = Int("State", default=4)
        Cmd = Int("Cmd")
        InStarting = Bool("InStarting")
        InExecute = Bool("InExecute")
        FBAlarm = Bool("FBAlarm")

        with Program() as prog:
            with Rung(State == 3):
                out(InStarting)
            with Rung(State == 6):
                out(InExecute)
            # ReadInputs idiom: the program reads the image, not the lever.
            with Rung(FB):
                out(iFB)
            # Latch antagonist with its own state gate.
            with Rung(InStarting, ~iFB):
                latch(FBAlarm)
            # Stateless silenced producer ...
            with Rung(~iFB):
                out(ErrBit)
            # ... whose state gate lives one hop downstream, on the command
            # writer that pushes the channel.
            with Rung(InExecute, ErrBit):
                copy(8, Cmd)
            with Rung(Cmd == 8):
                copy(9, State)
                copy(0, Cmd)

        plc = PLC(prog, dt=0.010)
        ctx = _make_ctx(prog, plc, opaque_loop=frozenset({"State"}))
        before = {
            "InStarting": True,
            "InExecute": False,
            "FB": False,
            "iFB": False,
            "ErrBit": True,
            "State": 3,
            "Cmd": 0,
        }
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=5,
            action=(("Go", True),),
            bearing=(("FBAlarm", False),),
            before_snap=before,
            after_snap=dict(before, FBAlarm=True),
            changed_tags=("FBAlarm",),
            departures=(),
        )

        hyps = correct_enablers(plc, incident, ctx)
        fb_rungs = [
            h.holds[0]
            for h in hyps
            if h.kind == "latch-exposure"
            and len(h.holds) == 1
            and isinstance(h.holds[0], PilotRung)
            and h.holds[0].dest == "FB"
        ]
        assert fb_rungs, f"no guarded FB rung proposed: {[h.holds for h in hyps]}"
        rung = fb_rungs[0]
        view_starting = {"InStarting": True, "InExecute": False}
        view_execute = {"InStarting": False, "InExecute": True}
        view_neither = {"InStarting": False, "InExecute": False}
        assert _guard_active(rung, view_starting)
        assert _guard_active(rung, view_execute)
        assert not _guard_active(rung, view_neither)

    def test_stateless_exposure_falls_back_to_pair_proposals(self):
        """No state gate anywhere on the antagonist chain: never invent one.
        The proposal stays pair-shaped so the legacy landing scoping applies."""
        Guard = Bool("Guard", external=True)
        State = Bool("State")
        Alarm = Bool("Alarm")
        Enter = Bool("Enter", external=True)

        with Program() as prog:
            with Rung(Enter):
                out(State)
            # The latch reads only the corrected input — no channel context.
            with Rung(~Guard):
                latch(Alarm)

        plc = PLC(prog, dt=0.010)
        ctx = _make_ctx(prog, plc, opaque_loop=frozenset({"State"}))
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=5,
            action=(("Enter", True),),
            bearing=(("Alarm", False),),
            before_snap={"State": True, "Guard": False},
            after_snap={"State": True, "Guard": False, "Alarm": True},
            changed_tags=("Alarm",),
            departures=(),
        )

        hyps = correct_enablers(plc, incident, ctx)
        assert len(hyps) == 1
        assert hyps[0].holds == (("Guard", True),)
