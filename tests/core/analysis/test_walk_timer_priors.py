from __future__ import annotations

from pyrung import Bool, Program, Rung, Timer, on_delay
from pyrung.core.analysis.causal.projected import projected_cause
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.walk.priors import _unsatisfied_conditions, _writer_candidates
from pyrung.core.runner import PLC


def _ton_program() -> tuple[Program, Bool, Timer]:
    enable = Bool("Enable", external=True)
    timer = Timer.clone("Delay")
    with Program() as prog:
        with Rung(enable):
            on_delay(timer, 100, "ms")
    return prog, enable, timer


def test_on_delay_done_true_static_prereq_uses_enable_polarity() -> None:
    prog, enable, timer = _ton_program()
    plc = PLC(prog, dt=0.010)
    plc.step()
    pdg = build_program_graph(prog)

    union, candidates = _writer_candidates(
        timer.Done.name,
        True,
        dict(plc.state.tags),
        pdg,
        prog,
        known=plc._known_tags_by_name,
    )

    assert union == [(enable.name, True)]
    assert len(candidates) == 1
    assert candidates[0].unsatisfied == ((enable.name, True),)


def test_on_delay_done_true_does_not_break_live_enable() -> None:
    prog, enable, timer = _ton_program()
    plc = PLC(prog, dt=0.010)
    plc.patch({enable.name: True})
    plc.step()
    assert plc.state.tags[timer.Done.name] is False

    pdg = build_program_graph(prog)

    assert _unsatisfied_conditions(
        timer.Done.name,
        True,
        dict(plc.state.tags),
        pdg,
        prog,
        known=plc._known_tags_by_name,
    ) == []


def test_projected_cause_on_delay_done_true_names_enable() -> None:
    prog, enable, timer = _ton_program()
    plc = PLC(prog, dt=0.010)
    plc.step()
    pdg = build_program_graph(prog)

    chain = projected_cause(
        plc._logic,
        plc.history,
        timer.Done.name,
        True,
        pdg,
        timelines=plc._rung_firing_timelines,
        program=prog,
        structural=True,
    )

    assert chain.mode == "projected"
    assert [(t.tag_name, t.to_value) for t in chain.conjunctive_roots] == [
        (enable.name, True)
    ]
