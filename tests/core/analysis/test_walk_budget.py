"""Walk budget honesty: per-steer enforcement, the wall-clock knob, and the
set-value-flood reproducer from the burner-loop findings (§2d).

The PackML failure shape: program-wide cones put every non-Bool ND input in
the steer alphabet, their pipeline domains multiply into hundreds of
set-value steers, and the explore pays |alphabet| forks at every node — a
bounded fork budget dies inside the first goal's establish.  Two fixes are
pinned here:

- ``set_value_relevance`` (narrowing pass): enabling-named ND inputs keep
  their full domains, the rest fill a bounded remainder — the same walk
  solves within a fork budget that exhausts with the pass ablated.
- Budget checks reach inside the explore loop (per steer trial, not just
  agenda yield boundaries), which is also what makes ``wall_budget_s`` an
  honest knob instead of a suggestion.
"""

from __future__ import annotations

from pyrung import Bool, Int, Or, Program, Rung, calc, out, rise
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.simplified import Atom
from pyrung.core.analysis.walk import engine as walk
from pyrung.core.analysis.walk.engine import plan_walk
from pyrung.core.runner import PLC

# Calibrated against the flood program below: the narrowed walk solves at
# 131 forks, the ablated walk needs 635 (probe_budget_calib bisection).
_FLOOD_FORK_BUDGET = 250


def _set_value_flood_program(n_noise: int = 30):
    """Target needs a 3-step Mode corridor plus a compared ND input.

    The *n_noise* ND inputs are in the governing cone (through the internal
    ``NoiseGate`` bit) but never named by any enabling condition — their
    only effect is flooding the alphabet with set-value steers, 5 domain
    values each.
    """
    Level = Int("Level", external=True)
    noise = [Int(f"Noise{i:02d}", external=True) for i in range(n_noise)]
    Go = Bool("Go", external=True)
    CmdNext = Bool("CmdNext", external=True)
    NoiseGate = Bool("NoiseGate")
    Mode = Int("Mode")
    Target = Bool("Target")

    noise_any = Or(*[n > 50 for n in noise])

    with Program() as prog:
        with Rung(noise_any):
            out(NoiseGate)
        with Rung(rise(CmdNext), Mode < 3, Or(Go, NoiseGate)):
            calc(Mode + 1, Mode)
        with Rung(Mode == 3, Level >= 10):
            out(Target)

    nd_domains: dict[str, tuple] = {"Level": (0, 9, 10, 11)}
    for n in noise:
        nd_domains[n.name] = (0, 49, 50, 51, 100)
    return prog, Target, nd_domains


def _flood_walk(disabled: frozenset[str], fork_budget: int | None):
    prog, target, nd_domains = _set_value_flood_program()
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
        nd_domains=nd_domains,
        nogoods=walk.NoGoodStore(),
        holds=walk.HoldStore(),
        disabled_passes=disabled,
        fork_budget=fork_budget,
    )
    reached = bool(work.state.tags.get(target.name)) if steps is not None else False
    return steps, reached


def test_set_value_flood_solves_within_fork_budget() -> None:
    """With the relevance pass, the flood program solves well inside the
    bounded budget (the (d) reproducer's fixed direction)."""
    steps, reached = _flood_walk(frozenset(), _FLOOD_FORK_BUDGET)
    assert steps is not None
    assert reached


def test_set_value_flood_ablated_exhausts_same_budget() -> None:
    """Ablating ``set_value_relevance`` re-creates the findings-§2d shape:
    the full set-value flood exhausts the same fork budget before the
    corridor completes.  (At an unbounded budget the ablated walk still
    solves — narrowing is conservative; this row pins the effort gap.)"""
    steps, _reached = _flood_walk(frozenset({"set_value_relevance"}), _FLOOD_FORK_BUDGET)
    assert steps is None

    steps_wide, reached_wide = _flood_walk(frozenset({"set_value_relevance"}), None)
    assert steps_wide is not None
    assert reached_wide


def test_walk_to_goal_wall_budget_zero_is_exhausted() -> None:
    """A zero wall-clock budget refuses immediately — honest exhaustion."""
    steps, _ = _flood_walk(frozenset(), None)
    assert steps is not None, "premise: walkable without a wall cap"

    prog, target, nd_domains = _set_value_flood_program()
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
        nd_domains=nd_domains,
        nogoods=walk.NoGoodStore(),
        holds=walk.HoldStore(),
        wall_budget_s=0.0,
    )
    assert steps is None


def _two_step_program():
    """Ready latches on Arm; Done needs Ready AND Fire — a small walkable
    program for the plan_walk-level knob assertions."""
    from pyrung import latch

    Arm = Bool("Arm", external=True)
    Fire = Bool("Fire", external=True)
    Ready = Bool("Ready")
    Done = Bool("Done")

    with Program() as prog:
        with Rung(Arm):
            latch(Ready)
        with Rung(Ready, Fire):
            out(Done)

    return prog, Done


def test_plan_walk_wall_budget_returns_honest_exhaustion() -> None:
    prog, done = _two_step_program()
    plc = PLC(prog, dt=0.010)
    goal = Atom(tag=done.name, form="xic", operand=True)

    baseline = plan_walk(plc, dict(plc._state.tags), goal, 20)
    assert baseline is not None
    assert baseline.reachable, "premise: walkable without a wall cap"

    capped = plan_walk(plc, dict(plc._state.tags), goal, 20, wall_budget_s=0.0)
    assert capped is not None
    assert not capped.reachable
    assert "budget exhausted" in (capped.reason or "")
    assert "wall-clock" in (capped.reason or "")
    assert capped.diagnosis is not None
    assert capped.diagnosis.verdict == "not-found"


def test_how_walk_seconds_knob() -> None:
    """The public ``how(walk_seconds=)`` knob reaches the walk budget."""
    prog, done = _two_step_program()
    plc = PLC(prog, dt=0.010)
    plc.step()

    path = plc.how(done, walk_seconds=0.0)
    assert not path.reachable
    assert "wall-clock" in (path.reason or "")
