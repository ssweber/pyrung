"""Crossings — CalcCrossing (affine equality inversion)."""

from __future__ import annotations

from pyrung import Dint, Int
from pyrung.core.analysis.crossings.calc import CalcCrossing
from pyrung.core.crossing import Cmp, CrossingContext, Eq, eq_target
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.memory_block import Block
from pyrung.core.tag import TagType

_CALC = CalcCrossing()


def _ctx(*tags) -> CrossingContext:
    return CrossingContext(tags_by_name={t.name: t for t in tags})


def _only(result):
    """The single conjunctive branch of a one-branch result."""
    (branch,) = result.branches
    return branch


def test_affine_add_inverts_to_preimage_exactly() -> None:
    src, dest = Int("Src"), Int("Dest")
    r = _CALC.reverse(CalcInstruction(src + 5, dest), None, eq_target("Dest", 42), _ctx(src, dest))
    assert _only(r) == (Eq("Src", frozenset({37})),)
    assert r.exact is True  # same-width add is a bijection on the wrap ring
    assert r.fallthrough is False


def test_affine_add_wrap_corrects_at_boundary() -> None:
    # dest = src + 5 wraps: dest == -32766 means src == 32765 (32765+5 wraps), not -32771.
    src, dest = Int("Src"), Int("Dest")
    r = _CALC.reverse(
        CalcInstruction(src + 5, dest), None, eq_target("Dest", -32766), _ctx(src, dest)
    )
    assert _only(r) == (Eq("Src", frozenset({32765})),)
    assert r.exact is True


def test_affine_negate_inverts_exactly() -> None:
    src, dest = Int("Src"), Int("Dest")
    r = _CALC.reverse(CalcInstruction(-src, dest), None, eq_target("Dest", 7), _ctx(src, dest))
    assert _only(r) == (Eq("Src", frozenset({-7})),)
    assert r.exact is True


def test_affine_mismatched_width_stays_candidate() -> None:
    # DINT src wrapped into an INT dest admits other preimages -> exact=False.
    src, dest = Dint("Src"), Int("Dest")
    r = _CALC.reverse(CalcInstruction(src + 5, dest), None, eq_target("Dest", 42), _ctx(src, dest))
    assert _only(r) == (Eq("Src", frozenset({37})),)
    assert r.exact is False


def test_affine_mul_exact_division_is_candidate() -> None:
    # Multiply is not bijective under wrap -> the divisible preimage is a candidate.
    src, dest = Int("Src"), Int("Dest")
    r = _CALC.reverse(CalcInstruction(src * 3, dest), None, eq_target("Dest", 9), _ctx(src, dest))
    assert _only(r) == (Eq("Src", frozenset({3})),)
    assert r.exact is False


def test_mul_non_integer_preimage_falls_through() -> None:
    src, dest = Int("Src"), Int("Dest")
    r = _CALC.reverse(CalcInstruction(src * 3, dest), None, eq_target("Dest", 7), _ctx(src, dest))
    assert r.fallthrough


def test_sum_expr_falls_through() -> None:
    blk = Block("DS", TagType.INT, 1, 5)
    dest = Int("Total")
    instr = CalcInstruction(blk.select(1, 3).sum(), dest)
    assert _CALC.reverse(instr, None, eq_target("Total", 1), _ctx(dest)).fallthrough is True


def test_multi_tag_expr_falls_through() -> None:
    a, b, dest = Int("A"), Int("B"), Int("Dest")
    r = _CALC.reverse(CalcInstruction(a + b, dest), None, eq_target("Dest", 5), _ctx(a, b, dest))
    assert r.fallthrough


def test_non_numeric_target_falls_through() -> None:
    src, dest = Int("Src"), Int("Dest")
    r = _CALC.reverse(CalcInstruction(src + 5, dest), None, eq_target("Dest", "x"), _ctx(src, dest))
    assert r.fallthrough


def test_non_eq_target_falls_through() -> None:
    src, dest = Int("Src"), Int("Dest")
    cmp_target = Cmp("Dest", ">=", 10)
    assert _CALC.reverse(
        CalcInstruction(src + 5, dest), None, cmp_target, _ctx(src, dest)
    ).fallthrough
