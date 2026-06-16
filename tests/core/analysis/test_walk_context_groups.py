"""Context-aware writer candidate ordering."""

from __future__ import annotations

from pyrung import And, Bool, Int, Or, Program, Rung, calc, copy, latch, rise
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.walk import engine as walk
from pyrung.core.analysis.walk.agenda import (
    _candidate_monitors,
    _classify_disposition,
    _Disposition,
)
from pyrung.core.analysis.walk.base import _MustStay, _StepMonitors
from pyrung.core.analysis.walk.priors import _writer_candidates, _WriterCandidate
from pyrung.core.runner import PLC


def _branch_program() -> tuple[Program, str, int]:
    """Two writers can set Mode=2, but only the A writer preserves S_A."""
    Kick = Bool("Kick", external=True)
    AdvB = Bool("AdvB", external=True)
    TickA1 = Bool("TickA1", external=True)
    TickA2 = Bool("TickA2", external=True)
    StageB = Int("StageB")
    CntA1 = Int("CntA1")
    CntA2 = Int("CntA2")
    Init1 = Bool("Init1")
    Init2 = Bool("Init2")
    S_A = Int("S_A", default=1)
    Mode = Int("Mode")

    with Program() as prog:
        with Rung(rise(TickA1)):
            calc(CntA1 + 1, CntA1)
        with Rung(CntA1 >= 25):
            latch(Init1)
        with Rung(rise(TickA2)):
            calc(CntA2 + 1, CntA2)
        with Rung(CntA2 >= 25):
            latch(Init2)
        with Rung(rise(AdvB), StageB < 4):
            calc(StageB + 1, StageB)
        with Rung(StageB == 4):
            copy(0, S_A)
        with Rung(rise(Kick), Mode == 0):
            copy(1, Mode)
        with Rung(S_A == 1, Init1, Init2, Mode == 1):
            copy(2, Mode)
        with Rung(StageB == 4, Mode == 1):
            copy(2, Mode)

    return prog, Mode.name, 2


def _walk(disabled: frozenset[str]) -> tuple[list[tuple[dict[str, object], int]] | None, PLC]:
    prog, target_tag, target_value = _branch_program()
    plc = PLC(prog, dt=0.010)
    plc.step()
    work = plc.fork()
    walk._install_walk_harness(work)
    pdg = build_program_graph(work._program)
    known = work._known_tags_by_name
    ext_inputs = walk._external_bool_inputs(pdg, known)
    edge_ext = walk._edge_tags(pdg, work._program) & set(ext_inputs)
    steps = walk._walk_to_goal(
        work,
        target_tag,
        target_value,
        pdg,
        work._program,
        known,
        ext_inputs,
        edge_ext,
        32,
        nogoods=walk.NoGoodStore(),
        holds=walk.HoldStore(),
        disabled_passes=disabled,
    )
    return steps, work


def _done_candidates() -> list[_WriterCandidate]:
    prog, target_tag, target_value = _branch_program()
    plc = PLC(prog, dt=0.010)
    plc.step()
    pdg = build_program_graph(plc._program)
    _union, candidates = _writer_candidates(
        target_tag,
        target_value,
        dict(plc.state.tags),
        pdg,
        plc._program,
        known=plc._known_tags_by_name,
    )
    return candidates


def test_writer_candidates_preserve_satisfied_branch_context() -> None:
    candidates = _done_candidates()

    a_candidate = next(c for c in candidates if ("Init1", True) in c.unsatisfied)
    b_candidate = next(c for c in candidates if c.unsatisfied == (("StageB", 4),))

    assert ("S_A", 1) in a_candidate.full_conditions
    assert a_candidate.satisfied == (("S_A", 1),)
    assert set(a_candidate.unsatisfied) == {("Init1", True), ("Init2", True)}
    assert b_candidate.satisfied == ()


def test_writer_candidates_preserve_live_or_arm_context() -> None:
    StateA = Bool("StateA", default=True)
    StateB = Bool("StateB")
    Init = Bool("Init", external=True)
    Done = Bool("Done")

    with Program() as prog:
        with Rung(Or(StateA, StateB), Init):
            latch(Done)

    plc = PLC(prog, dt=0.010)
    plc.step()
    pdg = build_program_graph(prog)

    union, candidates = _writer_candidates(
        Done.name,
        True,
        dict(plc.state.tags),
        pdg,
        prog,
        known=plc._known_tags_by_name,
    )

    assert union == [(Init.name, True)]
    assert len(candidates) == 1
    assert candidates[0].satisfied == ((StateA.name, True),)
    assert candidates[0].unsatisfied == ((Init.name, True),)


def test_live_or_arm_context_does_not_guess_edge_branches() -> None:
    Pulse = Bool("Pulse", default=True)
    StateA = Bool("StateA", default=True)
    StateB = Bool("StateB")
    Init = Bool("Init", external=True)
    Done = Bool("Done")

    with Program() as prog:
        with Rung(Or(StateB, And(rise(Pulse), StateA)), Init):
            latch(Done)

    plc = PLC(prog, dt=0.010)
    plc.step()
    pdg = build_program_graph(prog)

    _union, candidates = _writer_candidates(
        Done.name,
        True,
        dict(plc.state.tags),
        pdg,
        prog,
        known=plc._known_tags_by_name,
    )

    assert candidates[0].satisfied == ()
    assert candidates[0].unsatisfied == ((Init.name, True),)


def test_writer_candidate_full_context_skips_nonpredecessor_self_conditions() -> None:
    Step = Int("Step", default=5)
    Done = Bool("Done")

    with Program() as prog:
        with Rung(Step == 5):
            copy(5, Step)
        with Rung(Step == 5):
            latch(Done)

    plc = PLC(prog, dt=0.010)
    plc.step()
    pdg = build_program_graph(prog)

    _union, candidates = _writer_candidates(
        Step.name,
        5,
        dict(plc.state.tags),
        pdg,
        prog,
        known=plc._known_tags_by_name,
    )

    assert candidates
    assert all((Step.name, 5) not in c.full_conditions for c in candidates)
    assert all((Step.name, 5) not in c.satisfied for c in candidates)


def test_candidate_monitors_preserve_selected_writer_context() -> None:
    candidate = next(c for c in _done_candidates() if ("Init1", True) in c.unsatisfied)

    monitors = _candidate_monitors(_StepMonitors(), candidate, "Mode", 2)

    assert monitors.violation({"S_A": 0, "Mode": 1}) == ("S_A", 1)
    assert monitors.violation({"S_A": 0, "Mode": 2}) is None


def test_candidate_disposition_uses_active_must_stay_context() -> None:
    guard = _MustStay(must=(("S_A", 1),), until=(("Mode", 2),))
    monitors = _StepMonitors(must_stay=(guard,))
    preferred = next(c for c in _done_candidates() if ("Init1", True) in c.unsatisfied)
    normal = next(c for c in _done_candidates() if c.unsatisfied == (("StageB", 4),))
    rejected = _WriterCandidate((), (), (("S_A", 0),), frozenset(), 0)
    deferred = _WriterCandidate((), (), (("Other", True),), frozenset({"S_A"}), 0)

    assert _classify_disposition(preferred, monitors) is _Disposition.PREFERRED
    assert _classify_disposition(normal, monitors) is _Disposition.NORMAL
    assert _classify_disposition(rejected, monitors) is _Disposition.REJECTED
    assert _classify_disposition(deferred, monitors) is _Disposition.DEFERRED


def test_context_aware_groups_choose_branch_preserving_writer() -> None:
    steps, work = _walk(frozenset())

    assert steps is not None
    assert work.state.tags["Mode"] == 2
    assert work.state.tags["S_A"] == 1
    assert work.state.tags["StageB"] < 4
    assert work.state.tags["Init1"] is True
    assert work.state.tags["Init2"] is True


def test_context_aware_ablation_restores_shortest_group_order() -> None:
    steps, work = _walk(frozenset({"context_aware_groups"}))

    assert steps is not None
    assert work.state.tags["Mode"] == 2
    assert work.state.tags["S_A"] == 0
    assert work.state.tags["StageB"] == 4
