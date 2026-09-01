"""Stable shared APIs used by prover, PDG consumers, and validation."""

from __future__ import annotations

from pyrung import Bool, Int, Rung, calc, call, copy, return_early, subroutine
from pyrung.core import Program
from pyrung.core.analysis import (
    CallSite,
    build_program_graph,
    closed_value_domains,
    effective_reach_chains,
    extract_affine_expression,
    extract_forward_affine,
    scope_reach_chains,
)


def test_affine_api_covers_expression_and_write_instruction() -> None:
    source = Int("AffineSource")
    target = Int("AffineTarget")
    with Program() as program:
        with Rung():
            calc(10 - source, target)

    instruction = program.rungs[0]._instructions[0]

    assert extract_affine_expression(instruction.expression) == ("AffineSource", -1, 10)
    assert extract_forward_affine(instruction) == ("AffineSource", -1, 10)


def test_closed_domains_combine_declarations_and_complete_producers() -> None:
    declared = Int("Declared", min=1, max=3, external=True)
    produced = Int("Produced")
    with Program() as program:
        with Rung():
            copy(2, produced)
        with Rung(declared == 1):
            copy(4, produced)

    domains = closed_value_domains(program, build_program_graph(program))

    assert domains["Declared"] == (1, 2, 3)
    assert domains["Produced"] == (0, 2, 4)


def test_pdg_call_sites_drive_return_aware_reach_chains() -> None:
    enter = Bool("Enter", external=True)
    stop = Bool("Stop", external=True)
    with Program(strict=False) as program:
        with Rung(enter):
            call("outer")
        with subroutine("outer"):
            with Rung(stop):
                return_early()
            with Rung():
                call("inner")
        with subroutine("inner"):
            with Rung():
                copy(1, Int("Result"))

    graph = build_program_graph(program)
    sites = graph.call_sites()
    inner_node = graph.rung_node(
        scope="subroutine",
        subroutine="inner",
        rung_index=0,
    )

    assert all(isinstance(site, CallSite) for site in sites)
    assert [(site.caller, site.callee) for site in sites] == [
        (None, "outer"),
        ("outer", "inner"),
    ]
    assert graph.rung_nodes[sites[1].node_index].subroutine == "outer"
    assert inner_node is not None
    chains = effective_reach_chains(
        program,
        graph,
        inner_node,
        scope_chains=scope_reach_chains(program, graph),
    )
    assert len(chains) == 1
    assert len(chains[0].conditions) == 1
    assert not chains[0].return_stable
    assert len(chains[0].return_guards) == 1
