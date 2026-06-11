"""Stage D3: the walker's pass registry and its ablation matrix.

Every registered pass declares a kind, and the kind is its proof obligation:

- ``ordering`` — disabling changes only effort, never verdicts.
- ``narrowing`` — must be conservative; disabling only widens the search,
  so verdicts are preserved (up to budget exhaustion — on the tiny matrix
  programs the budgets are nowhere near binding, so equality is asserted).

The matrix below is parametrized over the registry itself, so every new
pass gets its rows by construction.  Soundness is not at stake either way:
replay verification carries it; passes touch completeness only.
"""

from __future__ import annotations

import pytest

from pyrung import Bool, Program, Rung, out
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.walk import engine as walk
from pyrung.core.analysis.walk.passes import (
    WALK_PASSES,
    _WalkAdvice,
    run_walk_passes,
)
from pyrung.core.analysis.walk.priors import _steer_alphabet
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


def test_registry_names_unique_and_kinds_valid() -> None:
    names = [p.name for p in WALK_PASSES]
    assert len(names) == len(set(names))
    assert all(p.kind in ("ordering", "narrowing", "fold") for p in WALK_PASSES)
    assert all(p.description for p in WALK_PASSES)


def test_run_walk_passes_journals_every_row() -> None:
    prog, _target = _cross_guard()
    pdg = build_program_graph(prog)

    advice, journal = run_walk_passes(prog, pdg)
    assert advice.enabled == frozenset(p.name for p in WALK_PASSES)
    assert {d.pass_name for d in journal.decisions} == set(advice.enabled)
    assert all(d.outcome == "active" for d in journal.decisions)

    some = WALK_PASSES[0].name
    advice2, journal2 = run_walk_passes(prog, pdg, disabled=frozenset({some}))
    assert not advice2.has(some)
    by_name = {d.pass_name: d for d in journal2.decisions}
    assert by_name[some].outcome == "disabled"
    assert "disabled" not in {d.outcome for n, d in by_name.items() if n != some}

    with pytest.raises(ValueError, match="unknown walk pass"):
        run_walk_passes(prog, pdg, disabled=frozenset({"no_such_pass"}))


# ---------------------------------------------------------------------------
# Alphabet-level ablation properties (pure _steer_alphabet units)
# ---------------------------------------------------------------------------


def _polarity_program() -> tuple[Program, Bool]:
    """Motor needs Go AND ~Stop; Other is an out-of-cone external input."""
    Go = Bool("Go", external=True)
    Stop = Bool("Stop", external=True)
    Other = Bool("Other", external=True)
    Motor = Bool("Motor")
    Spare = Bool("Spare")

    with Program() as prog:
        with Rung(Go, ~Stop):
            out(Motor)
        with Rung(Other):
            out(Spare)

    return prog, Motor


def _alphabet(prog: Program, tag: str, disabled: frozenset[str]) -> list[tuple]:
    pdg = build_program_graph(prog)
    plc = PLC(prog, dt=0.010)
    advice, _journal = run_walk_passes(prog, pdg, disabled=disabled)
    steers = _steer_alphabet(tag, pdg, plc._known_tags_by_name, prog, True, advice=advice)
    return [(s.kind, s.input, s.value) for s in steers]


def test_cone_filter_disabled_widens() -> None:
    prog, motor = _polarity_program()
    base = _alphabet(prog, motor.name, frozenset())
    ablated = _alphabet(prog, motor.name, frozenset({"cone_filter"}))
    assert set(base) <= set(ablated)
    # The out-of-cone input becomes a candidate only when the filter is off.
    assert ("pulse", "Other", None) not in base
    assert ("pulse", "Other", None) in ablated


def test_steer_polarity_disabled_widens() -> None:
    prog, motor = _polarity_program()
    base = _alphabet(prog, motor.name, frozenset())
    ablated = _alphabet(prog, motor.name, frozenset({"steer_polarity"}))
    assert set(base) <= set(ablated)
    # Polarity narrows Go to pulse-only and Stop to low-only; ablated emits both.
    assert ("low", "Go", None) not in base
    assert ("low", "Go", None) in ablated
    assert ("pulse", "Stop", None) not in base
    assert ("pulse", "Stop", None) in ablated


def test_helpful_order_disabled_reorders_only() -> None:
    prog, motor = _polarity_program()
    base = _alphabet(prog, motor.name, frozenset())
    ablated = _alphabet(prog, motor.name, frozenset({"helpful_order"}))
    assert set(base) == set(ablated)


def _set_value_flood_program(n_noise: int = 30):
    """Target gated on a compared ND input; *n_noise* ND inputs are in-cone
    (through an internal gate bit) but never named by enabling conditions.

    The PackML shape from the burner-loop findings (§2d): program-wide cones
    put every ND input in the alphabet, and their domains multiply into
    hundreds of set-value steers paid at every explore node.
    """
    from pyrung import Int, Or, calc, rise

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


def test_set_value_relevance_disabled_widens() -> None:
    from pyrung.core.analysis.walk.base import _MAX_SET_VALUE_STEERS

    prog, target, nd_domains = _set_value_flood_program()
    pdg = build_program_graph(prog)
    plc = PLC(prog, dt=0.010)

    def alphabet(disabled: frozenset[str]) -> list[tuple]:
        advice, _journal = run_walk_passes(prog, pdg, disabled=disabled)
        steers = _steer_alphabet(
            target.name,
            pdg,
            plc._known_tags_by_name,
            prog,
            True,
            nd_domains=nd_domains,
            advice=advice,
        )
        return [(s.kind, s.input, s.value) for s in steers]

    base = alphabet(frozenset())
    ablated = alphabet(frozenset({"set_value_relevance"}))
    assert set(base) <= set(ablated)

    # Narrowing caps the flood; ablated keeps every in-cone domain value.
    base_sets = [s for s in base if s[0] == "set"]
    ablated_sets = [s for s in ablated if s[0] == "set"]
    assert len(base_sets) <= _MAX_SET_VALUE_STEERS
    assert len(ablated_sets) == sum(len(d) for d in nd_domains.values())

    # The enabling-named input keeps its full domain in the narrowed alphabet.
    for v in nd_domains["Level"]:
        assert ("set", "Level", v) in base


def test_no_advice_handle_means_all_enabled() -> None:
    prog, motor = _polarity_program()
    pdg = build_program_graph(prog)
    plc = PLC(prog, dt=0.010)
    bare = _steer_alphabet(motor.name, pdg, plc._known_tags_by_name, prog, True)
    advised = _steer_alphabet(
        motor.name, pdg, plc._known_tags_by_name, prog, True, advice=_WalkAdvice()
    )
    assert [(s.kind, s.input) for s in bare] == [(s.kind, s.input) for s in advised]


# ---------------------------------------------------------------------------
# The ablation matrix: registry x programs, asserted by kind
# ---------------------------------------------------------------------------


def _cross_guard() -> tuple[Program, Bool]:
    from tests.core.analysis.test_walk_nogood import _program as build

    return build()


def _shared_gate() -> tuple[Program, Bool]:
    from tests.core.analysis.test_walk_holds import _shared_gate_program as build

    return build()


def _seal_release() -> tuple[Program, Bool]:
    from tests.core.analysis.test_walk_holds import _seal_release_program as build

    prog, _armed, fired = build()
    return prog, fired


_MATRIX_PROGRAMS = {
    "cross_guard": _cross_guard,
    "shared_gate": _shared_gate,
    "seal_release": _seal_release,
}


def _walk(prog: Program, target: Bool, disabled: frozenset[str]) -> tuple[bool, bool]:
    """Run a single-goal walk; return (solved, target_reached_on_work)."""
    plc = PLC(prog, dt=0.010)
    work = plc.fork()
    walk._install_walk_harness(work)
    pdg = build_program_graph(work._program)
    known = work._known_tags_by_name
    ext_inputs = walk._external_bool_inputs(pdg, known)
    edge_ext = walk._edge_tags(pdg, work._program) & set(ext_inputs)
    governing, gov_value = walk._governing(target.name, True, pdg, work._program, plc=work)
    steps = walk._walk_to_goal(
        work,
        governing,
        gov_value,
        pdg,
        work._program,
        known,
        ext_inputs,
        edge_ext,
        64,
        nogoods=walk.NoGoodStore(),
        holds=walk.HoldStore(),
        disabled_passes=disabled,
    )
    solved = steps is not None
    reached = bool(work.state.tags.get(target.name)) if solved else False
    return solved, reached


@pytest.mark.parametrize("program_name", sorted(_MATRIX_PROGRAMS))
@pytest.mark.parametrize("walk_pass", WALK_PASSES, ids=lambda p: p.name)
def test_ablation_matrix(walk_pass, program_name: str) -> None:
    """Disable one pass; assert its kind's obligation.

    Ordering: same verdict (effort may differ).  Narrowing: conservative —
    disabling only widens, so the verdict is preserved; the general contract
    allows "or budget-exhausted" under wider searches, but the matrix
    programs sit far below the global budget caps, so equality is asserted.
    """
    prog, target = _MATRIX_PROGRAMS[program_name]()
    base_solved, base_reached = _walk(prog, target, frozenset())
    assert base_solved and base_reached, "matrix program must be walkable at baseline"

    solved, reached = _walk(prog, target, frozenset({walk_pass.name}))
    assert solved == base_solved, (
        f"{walk_pass.kind} pass {walk_pass.name!r} changed the verdict on {program_name}"
    )
    assert reached
