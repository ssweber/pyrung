"""Shared structural semantics for crossing reverse results."""

from __future__ import annotations

from dataclasses import replace

from pyrung import Bool, Program, Rung, copy, out
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.trace import trace_back
from pyrung.core.analysis.reverse_semantics import (
    ReverseShape,
    normalize_reverse_result,
)
from pyrung.core.crossing import REVERSE_FALLTHROUGH, Eq, ReverseResult


def test_normalizer_distinguishes_fallthrough_contradiction_and_trivial() -> None:
    assert normalize_reverse_result(REVERSE_FALLTHROUGH).shape is ReverseShape.FALLTHROUGH
    assert normalize_reverse_result(ReverseResult()).shape is ReverseShape.CONTRADICTION
    assert (
        normalize_reverse_result(
            ReverseResult(branches=((Eq("Impossible", frozenset()),),), exact=True)
        ).shape
        is ReverseShape.CONTRADICTION
    )
    trivial = normalize_reverse_result(
        ReverseResult(
            branches=((Eq("Source", frozenset({1})),), ()),
            exact=True,
        )
    )
    assert trivial.shape is ReverseShape.TRIVIAL
    assert trivial.branches == ((),)


def test_normalizer_prunes_dead_branches_without_flattening_dnf() -> None:
    left = (Eq("LeftA", frozenset({1})), Eq("LeftB", frozenset({2})))
    right = (Eq("Right", frozenset({3})),)
    result = normalize_reverse_result(
        ReverseResult(
            branches=(
                (Eq("Dead", frozenset()),),
                left,
                right,
            ),
            exact=True,
        )
    )

    assert result.shape is ReverseShape.CONSTRAINED
    assert result.branches == (left, right)


def test_pilot_skips_writer_with_contradictory_reverse() -> None:
    source = Bool("ReverseSemanticsSource", external=True)
    target = Bool("ReverseSemanticsTarget")
    with Program() as program:
        with Rung(source):
            out(target)

    tree = trace_back(
        target.name,
        2,  # OUT can only write a Boolean value.
        {source.name: False, target.name: False},
        build_program_graph(program),
        program,
        frozenset({source.name}),
    )

    assert tree.writer_rung is None
    assert tree.ordered_actions() == []


def test_pilot_trace_preserves_selected_crossing_exactness(monkeypatch) -> None:
    from pyrung.core.analysis import crossings

    source = Bool("ReverseFidelitySource", external=True)
    target = Bool("ReverseFidelityTarget")
    with Program() as program:
        with Rung():
            copy(source, target)

    original_reverse = crossings.reverse
    for exact in (True, False):

        def reverse_with_fidelity(*args, _exact=exact, **kwargs):
            return replace(original_reverse(*args, **kwargs), exact=_exact)

        monkeypatch.setattr(crossings, "reverse", reverse_with_fidelity)
        tree = trace_back(
            target.name,
            True,
            {source.name: False, target.name: False},
            build_program_graph(program),
            program,
            frozenset({source.name}),
        )

        assert tree.writer_rung == 0
        assert tree.crossing_exact is exact
