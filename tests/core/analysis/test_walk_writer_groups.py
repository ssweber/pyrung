"""Per-writer prerequisite groups (writer disjunction, Open Items #10).

The probe14d failure shape on the live PackML template:
``_unsatisfied_conditions`` returns the cross-writer UNION of prereqs, so
one writer's expensive requirements (production_states R3's
``Blower__init``/``Rotate__init`` → the whole Starting SFC) conjoin with
another writer's nearly-satisfied set (R11: ``S_Resetting`` already ✓ plus
the mode call gate), and the agenda walks the union serially — burning the
global budget inside a chain the goal never needed.

The fix is ordering, not pruning: ``_unsatisfied_condition_groups`` splits
the same extraction per writer (each group one writer's own unsatisfied
conditions — a genuine alternative, since arming any single writer
produces the value), and ``_establish`` walks the smallest-unsatisfied
group first, probing the corridor between groups.  Ablating the
``writer_prereq_groups`` ordering pass restores the serial union — same
verdicts at an unbounded budget, more forks (the kind's proof obligation).

Calibrated against the two-writer program below: the grouped walk solves
at ~22 forks, the union needs ~124 (probe_writer_groups bisection).
"""

from __future__ import annotations

from pyrung import Bool, Int, Or, Program, Rung, calc, copy, latch, out, rise
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.walk import engine as walk
from pyrung.core.analysis.walk.priors import (
    _unsatisfied_condition_groups,
    _unsatisfied_conditions,
)
from pyrung.core.runner import PLC

_GROUPED_FORK_BUDGET = 60


def _two_writer_program():
    """``Mode == 2`` is produced by two writers from ``Mode == 1``: one
    gated on two counter-latched inits (25 edges each — expensive), one on
    a four-pulse stage corridor (cheap).  Mode itself steps 0 -> 1 on a
    plain pulse, so Mode governs itself (the template shape: the state
    register is the corridor) and value 2 is reachable only through the
    writer prerequisites."""
    Kick = Bool("Kick", external=True)
    AdvB = Bool("AdvB", external=True)
    TickA1 = Bool("TickA1", external=True)
    TickA2 = Bool("TickA2", external=True)
    StageB = Int("StageB")
    CntA1 = Int("CntA1")
    CntA2 = Int("CntA2")
    InitA1 = Bool("InitA1")
    InitA2 = Bool("InitA2")
    Mode = Int("Mode")
    Target = Bool("Target")

    with Program() as prog:
        with Rung(rise(TickA1)):
            calc(CntA1 + 1, CntA1)
        with Rung(CntA1 >= 25):
            latch(InitA1)
        with Rung(rise(TickA2)):
            calc(CntA2 + 1, CntA2)
        with Rung(CntA2 >= 25):
            latch(InitA2)
        with Rung(rise(AdvB), StageB < 4):
            calc(StageB + 1, StageB)
        with Rung(rise(Kick), Mode == 0):
            copy(1, Mode)
        # Writer A (expensive): both counter-latched inits.
        with Rung(InitA1, InitA2, Mode == 1):
            copy(2, Mode)
        # Writer B (cheap): the four-pulse stage corridor.
        with Rung(StageB == 4, Mode == 1):
            copy(2, Mode)
        with Rung(Mode == 2):
            out(Target)

    return prog, Target


def _walk(disabled: frozenset[str], fork_budget: int | None):
    prog, target = _two_writer_program()
    plc = PLC(prog, dt=0.010)
    work = plc.fork()
    walk._install_walk_harness(work)
    pdg = build_program_graph(work._program)
    known = work._known_tags_by_name
    ext_inputs = walk._external_bool_inputs(pdg, known)
    edge_ext = walk._edge_tags(pdg, work._program) & set(ext_inputs)
    steps = walk._walk_to_goal(
        work,
        target.name,
        True,
        pdg,
        work._program,
        known,
        ext_inputs,
        edge_ext,
        64,
        nogoods=walk.NoGoodStore(),
        holds=walk.HoldStore(),
        disabled_passes=disabled,
        fork_budget=fork_budget,
    )
    reached = bool(work.state.tags.get(target.name)) if steps is not None else False
    return steps, reached, work


def test_grouped_solves_at_default_budget() -> None:
    """The grouped order tries the nearly-satisfied writer first: the walk
    solves through the cheap stage corridor inside the calibrated budget,
    and the expensive init chains are never walked."""
    steps, reached, work = _walk(frozenset(), _GROUPED_FORK_BUDGET)
    assert steps is not None
    assert reached
    # The cheap chain alone: 4 stage pulses + the Mode kick — nowhere near
    # the 25-edge counter corridors.
    assert len(steps) <= 12
    assert not work.state.tags.get("InitA1")
    assert not work.state.tags.get("InitA2")


def test_union_ablated_exhausts_same_budget() -> None:
    """Ablating ``writer_prereq_groups`` restores the serial union, which
    walks the init chains and exhausts the same fork budget (the probe14d
    shape).  At an unbounded budget the union still solves — ordering
    advice changes effort, never verdicts."""
    steps, _reached, _work = _walk(frozenset({"writer_prereq_groups"}), _GROUPED_FORK_BUDGET)
    assert steps is None

    steps_wide, reached_wide, _work = _walk(frozenset({"writer_prereq_groups"}), None)
    assert steps_wide is not None
    assert reached_wide


def test_self_decrement_reset_is_not_candidate_for_same_target() -> None:
    """A Step==5 reset/decrement rung produces Step==4, not Step==5."""
    Advance = Bool("Advance", external=True)
    Reset = Bool("Reset", external=True)
    Step = Int("Step", default=3)
    Status = Int("Status")
    OneShot = Bool("OneShot")
    IsOdd = Int("IsOdd")

    with Program() as prog:
        with Rung():
            calc(Step % 2, IsOdd)
        with Rung(Status == 1):
            out(OneShot, oneshot=True)
        with Rung(Or(OneShot, IsOdd != 1)):
            calc(Step + 1, Step)
        with Rung(OneShot):
            copy(0, Status)
        with Rung(Step == 3, Advance):
            copy(1, Status)
        with Rung(Step == 5, Reset):
            calc(Step - 1, Step, oneshot=True)

    plc = PLC(prog)
    plc.step()
    pdg = build_program_graph(prog)

    prereqs, groups = _unsatisfied_condition_groups(
        Step.name,
        5,
        dict(plc.state.tags),
        pdg,
        prog,
        known=plc._known_tags_by_name,
    )

    assert prereqs == [(Step.name, 4)]
    assert groups == [[(Step.name, 4)]]
    assert (Reset.name, True) not in prereqs


def test_condition_groups_split_per_writer() -> None:
    """``_unsatisfied_condition_groups`` returns one group per matched
    writer; the union half stays the historical merged output."""
    prog, _target = _two_writer_program()
    plc = PLC(prog, dt=0.010)
    plc.step()
    pdg = build_program_graph(plc._program)
    snapshot = dict(plc.state.tags)

    union, groups = _unsatisfied_condition_groups(
        "Mode",
        2,
        snapshot,
        pdg,
        plc._program,
        known=plc._known_tags_by_name,
    )
    assert sorted(len(g) for g in groups) == [1, 2]
    group_sets = {frozenset(g) for g in groups}
    assert frozenset({("InitA1", True), ("InitA2", True)}) in group_sets
    assert frozenset({("StageB", 4)}) in group_sets
    # The union is exactly the groups' flattened content here (no
    # latch-break fallback, no unmatched-writer inequalities).
    assert {p for g in groups for p in g} == set(union)
    assert union == _unsatisfied_conditions(
        "Mode",
        2,
        snapshot,
        pdg,
        plc._program,
        known=plc._known_tags_by_name,
    )


def test_how_returns_cheap_chain_plan() -> None:
    """End to end: ``how(Target)`` plans through the cheap writer."""
    prog, target = _two_writer_program()
    plc = PLC(prog, dt=0.010)
    plc.step()

    path = plc.how(target)
    assert path.reachable
    assert len(path.steps) <= 12
