"""Reference-constant goal flood (Open Items #11).

The probe16 failure shape on the live PackML template: the ``sm__STATE*REF``
bank — registers with **no program writers** (their values are declared
initial data) read as ``copy`` sources by the state machine — is classified
nondeterministic input, so regression happily offers to rewrite one whenever
a copy destination or comparison needs a value the constant doesn't hold.
Each such goal "solves" in one set-value action, is operator-meaningless
(the mutation breaks the very state map that made its writer eligible), and
a whole bank of them floods the walk ahead of the writer the program
actually drives.

The fix is ordering, not pruning (``ref_constant_order``):
``_reference_constants`` collects the never-written copy-source registers
once per walk; ``_establish`` sorts writer groups that would mutate one
behind every other alternative, and ``_recover`` walks cause()-named ref
goals last, probing the corridor before the deferred tail.  Ablating the
pass restores cause()/writer order — same verdicts at an unbounded budget,
more forks and a goalpost-moving plan (the kind's proof obligation).

Calibrated against the bank program below: the ordered walk solves at ~110
forks with a clean plan; ablated needs ~1214 and returns a 35-action plan
that rewrites all fourteen constants (probe_refflood bisection).
"""

from __future__ import annotations

from pyrung import Bool, Int, Program, Rung, calc, copy, out, rise
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.walk import engine as walk
from pyrung.core.analysis.walk.agenda import _ref_constants_last
from pyrung.core.analysis.walk.priors import _reference_constants
from pyrung.core.runner import PLC

_ORDERED_FORK_BUDGET = 220
_N_REFS = 14


def _ref_bank_program():
    """``Req == 5`` is produced by fifteen copy-from-register writers: a
    fourteen-strong bank of never-written reference constants (state maps
    ``Cur == Ref_i`` keep their gates satisfied from cold) and, on the last
    rung, the one writer the program drives — its source already holds 5,
    gated on a four-pulse stage corridor.  Every decoy group is a one-item
    "mutate the constant" prereq, so without the ordering pass the walk
    rewrites the whole bank (each mutation self-defeating: it breaks its
    own state map) before it ever tries the real writer."""
    Go = Bool("Go", external=True)
    Arm = Bool("Arm", external=True)
    Cur = Int("Cur", default=1)
    refs = [Int(f"Ref{i:02d}", default=1) for i in range(_N_REFS)]
    ref_real = Int("RefReal", default=5)
    sts = [Bool(f"St{i:02d}") for i in range(_N_REFS)]
    st_real = Bool("StReal")
    ModeOk = Bool("ModeOk")
    Stage = Int("Stage")
    Req = Int("Req")
    Target = Bool("Target")

    with Program(strict=False) as prog:
        for i in range(_N_REFS):
            with Rung(Cur == refs[i]):
                out(sts[i])
        with Rung(Cur == 1):
            out(st_real)
        with Rung(rise(Arm), Stage < 4):
            calc(Stage + 1, Stage)
        with Rung(Stage == 4):
            out(ModeOk)
        for i in range(_N_REFS):
            with Rung(sts[i], rise(Go)):
                copy(refs[i], Req)
        with Rung(st_real, ModeOk, rise(Go)):
            copy(ref_real, Req)
        with Rung(Req == 5):
            out(Target)

    return prog, Target


def _walk(disabled: frozenset[str], fork_budget: int | None):
    prog, target = _ref_bank_program()
    plc = PLC(prog, dt=0.010)
    work = plc.fork()
    walk._install_walk_harness(work)
    pdg = build_program_graph(work._program)
    known = work._known_tags_by_name
    ext_inputs = walk._external_bool_inputs(pdg, known)
    edge_ext = walk._edge_tags(pdg, work._program) & set(ext_inputs)
    nd_domains: dict[str, tuple[int, ...]] = {
        f"Ref{i:02d}": (0, 1, 4, 5, 6) for i in range(_N_REFS)
    }
    nd_domains["RefReal"] = (0, 1, 4, 5, 6)
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
    return steps, reached, work


def _ref_writes(steps) -> set[str]:
    return {name for action, _scans in steps for name in action if name.startswith("Ref")}


def test_ordered_solves_at_default_budget() -> None:
    """With reference constants deferred, the walk goes straight through
    the real writer — the stage corridor plus the Go pulse — inside the
    calibrated budget, and no constant is ever rewritten."""
    steps, reached, work = _walk(frozenset(), _ORDERED_FORK_BUDGET)
    assert steps is not None
    assert reached
    assert len(steps) <= 12
    assert _ref_writes(steps) == set()
    for i in range(_N_REFS):
        assert work.state.tags.get(f"Ref{i:02d}") == 1


def test_ablated_floods_same_budget() -> None:
    """Ablating the ordering passes restores writer order: the walk rewrites
    the constant bank group by group and exhausts the same fork budget.  At
    an unbounded budget it still solves — ordering advice changes effort,
    never verdicts — but the plan it returns is the goalpost-moving one,
    mutating reference constants on its way."""
    disabled = frozenset({"ref_constant_order", "context_aware_groups"})
    steps, _reached, _work = _walk(disabled, _ORDERED_FORK_BUDGET)
    assert steps is None

    steps_wide, reached_wide, _work = _walk(disabled, None)
    assert steps_wide is not None
    assert reached_wide
    assert _ref_writes(steps_wide)  # the detours are in the plan


def test_reference_constants_detection() -> None:
    """``_reference_constants`` collects exactly the never-written
    copy-source registers: the bank and the real source qualify; the
    zero-writer comparison operand (``Cur``), program-written registers,
    and plain external Bools stay out."""
    prog, _target = _ref_bank_program()
    plc = PLC(prog, dt=0.010)
    pdg = build_program_graph(plc._program)

    refs = _reference_constants(pdg, plc._program)
    assert refs == frozenset({f"Ref{i:02d}" for i in range(_N_REFS)} | {"RefReal"})
    assert "Cur" not in refs  # read only in conditions — an ordinary setpoint
    assert "Req" not in refs  # program-written
    assert "Go" not in refs and "Arm" not in refs


def test_ref_constants_last_partition() -> None:
    """The recovery-goal partition is stable and returns the deferral
    index; an empty ref set is the identity (the ablation rides on it)."""
    goals = [("A", 1), ("R1", 2), ("B", True), ("R2", 3)]
    refs = frozenset({"R1", "R2"})

    ordered, at = _ref_constants_last(goals, refs)
    assert ordered == [("A", 1), ("B", True), ("R1", 2), ("R2", 3)]
    assert at == 2

    same, at_all = _ref_constants_last(goals, frozenset())
    assert same == goals
    assert at_all == len(goals)

    tail_only, at_zero = _ref_constants_last([("R1", 2)], refs)
    assert tail_only == [("R1", 2)]
    assert at_zero == 0
