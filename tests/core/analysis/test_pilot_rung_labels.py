"""The rung labels that how() prints count from 1, and a subroutine rung reads ``Sub:N``."""

from __future__ import annotations

from types import SimpleNamespace

from pyrung import Bool, Program, Rung, call, latch, subroutine
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.options import _producer_label
from pyrung.core.analysis.pilot.trace_routes import enumerate_trace_choices


def _program():
    a = Bool("A", external=True)
    b = Bool("B", external=True)
    c = Bool("C", external=True)
    target = Bool("Target")
    with Program(strict=False) as program:
        with Rung(c):
            latch(target)
        with Rung(a):
            call("sub")
        with subroutine("sub"):
            with Rung(b):
                latch(target)
    return program


def test_writer_label_counts_main_rungs_from_1_and_names_subroutine_rungs() -> None:
    program = _program()
    pdg = build_program_graph(program)

    choices = enumerate_trace_choices("Target", True, {}, pdg, program)

    assert {choice.route[0] for choice in choices} == {
        "Target=True via rung 1",
        "Target=True via rung sub:1",
    }


def test_program_note_producer_label_counts_main_rungs_from_1_and_names_subroutine_rungs() -> None:
    program = _program()
    pdg = build_program_graph(program)
    nodes = {pdg.rung_nodes[index].subroutine: index for index in pdg.writers_of["Target"]}
    assert set(nodes) == {None, "sub"}

    assert _producer_label(pdg, SimpleNamespace(rung_index=nodes[None])) == "1"
    assert _producer_label(pdg, SimpleNamespace(rung_index=nodes["sub"])) == "sub:1"
