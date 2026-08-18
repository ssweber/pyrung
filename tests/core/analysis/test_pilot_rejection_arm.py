"""The rejection arm of ``tide_tables.guard_verdict``, wired into trace.

``trace._trace_back`` consults the tide tables when it admits a writer for a needed
``(tag, value)``: with the writer's own fire-time pins fixed
(``_transition_fire_pins``), a ``GUARD_DEAD`` verdict means the writer can never
fire producing the value, so it is skipped exactly as a False ``_can_produce``
would — a provably-dead writer never burns a drive-loop trial.

Soundness is non-negotiable and one-directional:

- reject ONLY on a definite ``GUARD_DEAD``; ``SAT``/``PUNT`` retain the writer;
- the fixed pins are the writer's OWN (never borrowed);
- a ``DEAD`` proof is trusted only over *complete* free-tag domains — the prover's
  ``nd_domains`` or a Bool type — so without a domain prior the arm punts.

A ``PUNT`` on a frontier the walk cannot otherwise drive marks the ``TraceNode``
with ``live_guard=True`` for the sandbox skiff to consume.
"""

from __future__ import annotations

from pyrung import PLC, Bool, Int, Program, calc, copy, rise, rung
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.tide_tables import GUARD_DEAD, GUARD_SAT
from pyrung.core.analysis.pilot.trace import (
    DomainPrior,
    TraceNode,
    _writer_guard_verdict,
    resolve_rung,
    trace_back,
)
from pyrung.core.analysis.pilot.writer_selection import _reverse_writer
from pyrung.core.analysis.simplified import _sp_to_expr
from pyrung.core.analysis.steerable import compute_steerable


def _known(logic: Program) -> dict:
    return PLC(logic)._known_tags_by_name


def _steer(logic: Program) -> frozenset[str]:
    return compute_steerable(build_program_graph(logic), _known(logic), logic)


def _leaf_tags(node: TraceNode) -> set[str]:
    return {t for t, _v in node.steerable_leaves()}


def _leaf_pairs(node: TraceNode) -> set[tuple[str, object]]:
    return set(node.steerable_leaves())


# --- Test 1: a guard that contradicts the writer's own fire pin ---------------
#
# ``calc(Cmd + 5, State)`` producing ``State == 7`` forces ``Cmd == 2`` (the calc
# fire pin), while the same rung is gated ``Cmd == 1``.  The two can never hold at
# once, so the writer is provably dead. The selected writer's reverse receipt
# carries that fire pin to the oracle, which skips the impossible writer.


def _calc_contradiction_program():
    Cmd = Int("Cmd", external=True)
    Go = Bool("Go", external=True)
    State = Int("State")
    with Program() as logic:
        with rung(Cmd == 1):  # rung 0 — dead: fire pin Cmd==2 vs guard Cmd==1
            calc(Cmd + 5, State)
        with rung(Go):  # rung 1 — viable: copy(Cmd, State) forces Cmd==7
            copy(Cmd, State)
    return logic


def test_calc_fire_pin_contradiction_verdict_is_dead():
    """The oracle reports the calc-gated writer's guard provably unsatisfiable."""
    logic = _calc_contradiction_program()
    pdg = build_program_graph(logic)
    steer = _steer(logic)
    snap = {"Cmd": 0, "Go": False, "State": 0}

    from pyrung.core.analysis.pilot.trace import _env_for

    env = _env_for(snap, pdg, logic, steer)
    ro = resolve_rung(logic, pdg.rung_nodes[0])
    guard = _sp_to_expr(ro.sp_tree())
    reverse_result = _reverse_writer(ro, "State", 7, snap, pdg)
    assert _writer_guard_verdict(env, 0, "State", 7, reverse_result, guard) == GUARD_DEAD


def test_calc_fire_pin_contradiction_writer_skipped():
    """Trace skips the dead calc writer and picks the viable copy writer."""
    logic = _calc_contradiction_program()
    pdg = build_program_graph(logic)
    tree = trace_back("State", 7, {"Cmd": 0, "Go": False, "State": 0}, pdg, logic, _steer(logic))

    assert tree.writer_rung == 1  # the viable copy writer, not the dead calc one
    assert _leaf_pairs(tree) == {("Go", True), ("Cmd", 7)}


# --- Test 2: a guard that enumerates to definitely-False over declared domains -
#
# Two writers of ``State``.  The first is gated ``And(Cmd == 2, Mode == 9)`` while
# ``Mode``'s complete domain is ``{1, 2, 3}`` (the prover's ``nd_domains``): no
# assignment satisfies it, so it is provably dead.  The second, gated
# ``And(Go, Mode == 1)``, is viable.  Ranking puts the dead writer first, so the
# rejection arm is what lets the viable one through.


def _choices_dead_program():
    Cmd = Int("Cmd", external=True)
    Mode = Int("Mode", external=True)
    Go = Bool("Go", external=True)
    State = Int("State")
    with Program() as logic:
        with rung(Cmd == 2, Mode == 9):  # rung 0 — dead over Mode in {1,2,3}
            copy(Cmd, State)
        with rung(Go, Mode == 1):  # rung 1 — viable
            copy(Cmd, State)
    return logic


def test_choices_dead_writer_skipped_with_domain_prior():
    """With ``Mode``'s complete domain known, the dead writer is skipped."""
    logic = _choices_dead_program()
    pdg = build_program_graph(logic)
    snap = {"Cmd": 0, "Mode": 0, "Go": False, "State": 0}
    prior = DomainPrior(nd_domains={"Mode": (1, 2, 3), "Cmd": (2,)})

    tree = trace_back("State", 2, snap, pdg, logic, _steer(logic), prior=prior)

    assert tree.writer_rung == 1  # viable writer chosen despite ranking dead first
    # The bogus ``Mode == 9`` leaf never surfaces; the viable arm's demands do.
    assert ("Mode", 9) not in _leaf_pairs(tree)
    assert _leaf_pairs(tree) == {("Go", True), ("Mode", 1), ("Cmd", 2)}


def test_choices_dead_writer_kept_without_domain_prior():
    """Soundness gate: with no complete domain for ``Mode`` the arm PUNTS.

    ``Mode``'s value space is not knowable to be complete (no ``nd_domains``), so a
    ``DEAD`` proof would be unsound — the arm admits the first writer and surfaces
    its ``Mode == 9`` frontier. This pins the
    "reject only over complete domains" rule.
    """
    logic = _choices_dead_program()
    pdg = build_program_graph(logic)
    snap = {"Cmd": 0, "Mode": 0, "Go": False, "State": 0}

    tree = trace_back("State", 2, snap, pdg, logic, _steer(logic))  # no prior

    assert tree.writer_rung == 0  # unchanged from pre-rejection-arm behavior
    assert ("Mode", 9) in _leaf_pairs(tree)


# --- Test 3: an undecidable / genuinely-live guard punts (behavior unchanged) --
#
# ``St`` is written under ``Mask == 0`` where ``Mask = calc(Live & 0x40)`` is a
# bitwise function of a runtime-loaded word — un-invertible and with no finite
# domain.  The oracle can neither prove the guard satisfiable nor dead, so it
# PUNTS: the writer is admitted, and because the ``Mask`` gate
# is a genuine dead-end (nothing steerable), the frontier is flagged ``live_guard``.


def _live_guard_program():
    Trig = Bool("Trig", external=True)
    Cfg = Int("Cfg", external=True)
    Load = Bool("Load", external=True)
    Live = Int("Live", default=0x40)
    Mask = Int("Mask")
    St = Int("St")
    with Program() as logic:
        with rung(rise(Load)):
            copy(Cfg, Live)  # Live is rewritten at runtime — no finite domain
        with rung():
            calc(Live & 0x40, Mask)  # bitwise: un-invertible, genuinely live
        with rung(Trig, Mask == 0):
            copy(7, St)
    return logic


def test_live_guard_admits_writer_and_flags_frontier():
    """An undecidable guard admits the writer and sets ``live_guard``."""
    logic = _live_guard_program()
    pdg = build_program_graph(logic)
    snap = {"Trig": False, "Cfg": 0, "Load": False, "Live": 0x40, "Mask": 0x40, "St": 0}

    tree = trace_back("St", 7, snap, pdg, logic, _steer(logic))

    # The writer is admitted and its steerable trigger surfaces.
    assert tree.writer_rung == 2
    assert ("Trig", True) in _leaf_pairs(tree)

    # The frontier gated by the unreadable ``Mask == 0`` guard carries the signal.
    flagged = [(n.tag, n.value) for n in tree.iter_nodes() if n.live_guard]
    assert flagged == [("St", 7)]


def test_readable_guard_not_flagged_live():
    """A guard whose operand merely lacks an ``nd_domains`` entry but resolves to a
    steerable input is NOT flagged live — the flag means *unreadable*, not
    *un-enumerable*.  Here ``Live == 3`` traces through ``copy(Cfg, Live)`` to the
    steerable ``Cfg``, so the frontier is fully steerable and carries no signal."""
    Trig = Bool("Trig", external=True)
    Cfg = Int("Cfg", external=True)
    Load = Bool("Load", external=True)
    Live = Int("Live", default=5)
    St = Int("St")
    with Program() as logic:
        with rung(Load):
            copy(Cfg, Live)
        with rung(Trig, Live == 3):
            copy(7, St)
    pdg = build_program_graph(logic)
    snap = {"Trig": False, "Cfg": 0, "Load": False, "Live": 5, "St": 0}

    tree = trace_back("St", 7, snap, pdg, logic, _steer(logic))

    assert tree.writer_rung == 1
    assert ("Cfg", 3) in _leaf_pairs(tree)  # the guard was read, not punted-dead
    assert [n for n in tree.iter_nodes() if n.live_guard] == []


# --- Test 4: SAT verdict — a satisfiable guard is admitted normally -----------


def test_satisfiable_guard_verdict_is_sat():
    """A writer whose guard holds for some in-domain assignment is SAT, admitted."""
    logic = _choices_dead_program()
    pdg = build_program_graph(logic)
    snap = {"Cmd": 0, "Mode": 0, "Go": False, "State": 0}
    prior = DomainPrior(nd_domains={"Mode": (1, 2, 3), "Cmd": (2,)})

    from pyrung.core.analysis.pilot.trace import _env_for

    env = _env_for(snap, pdg, logic, steerable=_steer(logic), prior=prior)
    ro = resolve_rung(logic, pdg.rung_nodes[1])  # the viable Mode==1 writer
    guard = _sp_to_expr(ro.sp_tree())
    reverse_result = _reverse_writer(ro, "State", 2, snap, pdg)
    assert _writer_guard_verdict(env, 1, "State", 2, reverse_result, guard) == GUARD_SAT
