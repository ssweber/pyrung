"""Tests for PILOT's advance ownership and done-boundary corrections.

The resolver maps an ejecting consumer tag — a Done bit or an ``Acc`` register —
back to its owning instruction's profile; the generator turns that into the
corrective hold (oscillate, or stop-holding-the-advance).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pyrung import (
    Block,
    Bool,
    Counter,
    Int,
    Program,
    Rung,
    TagType,
    Timer,
    count_up,
    event_drum,
    on_delay,
    out,
    shift,
    time_drum,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.advance import build_advance_index
from pyrung.core.analysis.pilot.corrections import correct_enablers
from pyrung.core.analysis.pilot.investigate import DeviationIncident
from pyrung.core.analysis.steerable import compute_steerable
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


class TestAdvanceIndex:
    def test_resolves_via_done_bit(self):
        prog = _plain_timer_program()
        owner = build_advance_index(prog).resolve("T_Done")
        assert owner is not None
        assert owner.profile.done is not None
        assert owner.profile.done.name == "T_Done"

    def test_resolves_via_accumulator(self):
        prog = _plain_timer_program()
        owner = build_advance_index(prog).resolve("T_Acc")
        assert owner is not None
        assert owner.profile.accumulator is not None
        assert owner.profile.accumulator.name == "T_Acc"

    def test_unknown_tag_returns_none(self):
        assert build_advance_index(_plain_timer_program()).resolve("nope") is None

    def test_conflicting_owners_are_rejected_loudly(self):
        run_a = Bool("run_a", external=True)
        run_b = Bool("run_b", external=True)
        timer = Timer.clone("Shared")
        with Program(strict=False) as prog:
            with Rung(run_a):
                on_delay(timer, 10, "ms")
            with Rung(run_b):
                on_delay(timer, 20, "ms")

        index = build_advance_index(prog)

        assert index.resolve(timer.Acc.name) is None
        assert len(index.conflict(timer.Acc.name)) == 2
        assert "ambiguous" in (index.conflict_message(timer.Acc.name) or "")


class TestSequencerAdvance:
    def test_event_drum_path_replays_across_event_boundaries(self):
        enable = Bool("EventAuto", external=True)
        reset = Bool("EventReset", external=True)
        events = [Bool(f"Event{i}", external=True) for i in range(1, 4)]
        step = Int("EventStep")
        done = Bool("EventDone")
        output = Bool("EventOutput")
        with Program() as prog:
            with Rung(enable):
                event_drum(
                    outputs=[output],
                    events=events,
                    pattern=[[1], [0], [1]],
                    current_step=step,
                    completion_flag=done,
                ).reset(reset)

        path = PLC(prog).how(step == 3, max_scans=400)

        assert path.reachable, path.reason
        assert path.replay().state.tags[step.name] == 3

    def test_time_drum_path_replays_across_time_boundaries(self):
        enable = Bool("TimeAuto", external=True)
        reset = Bool("TimeReset", external=True)
        step = Int("TimeStep")
        acc = Int("TimeAcc")
        done = Bool("TimeDone")
        output = Bool("TimeOutput")
        with Program() as prog:
            with Rung(enable):
                time_drum(
                    outputs=[output],
                    presets=[20, 20, 20],
                    unit="ms",
                    pattern=[[1], [0], [1]],
                    current_step=step,
                    accumulator=acc,
                    completion_flag=done,
                ).reset(reset)

        path = PLC(prog, dt=0.010).how(step == 3, max_scans=400)

        assert path.reachable, path.reason
        assert path.replay().state.tags[step.name] == 3

    def test_shift_path_replays_across_clock_boundaries(self):
        data = Bool("ShiftData", external=True)
        clock = Bool("ShiftClock", external=True)
        reset = Bool("ShiftReset", external=True)
        bits = Block("PilotShift", TagType.BOOL, 1, 3)
        with Program() as prog:
            with Rung(data):
                shift(bits.select(1, 3)).clock(clock).reset(reset)

        path = PLC(prog).how(bits[3], max_scans=400)

        assert path.reachable, path.reason
        assert path.replay().state.tags[bits[3].name] is True


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

        hyps = correct_enablers(plc, incident, ctx)
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

        hyps = correct_enablers(plc, incident, ctx)
        done_boundary = [h for h in hyps if h.kind == "done-boundary"]
        assert len(done_boundary) == 1
        ((tag, value),) = done_boundary[0].holds
        assert tag == "pulse"
        assert value is False
