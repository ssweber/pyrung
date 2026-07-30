"""Crossings — CalcCrossing (affine equality inversion)."""

from __future__ import annotations

from pyrung import Dint, Int, Real
from pyrung.core.analysis.crossings.calc import CalcCrossing
from pyrung.core.crossing import UNKNOWN, Affine, Aggregate, Cmp, CrossingContext, Eq, eq_target
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


def test_real_multiply_inverts_float_target() -> None:
    src, dest = Real("Src"), Real("Dest")
    r = _CALC.reverse(
        CalcInstruction(src * 2.5, dest),
        None,
        eq_target("Dest", 7.5),
        _ctx(src, dest),
    )
    assert _only(r) == (Eq("Src", frozenset({3.0})),)
    assert r.exact is False


def test_mul_non_integer_preimage_falls_through() -> None:
    src, dest = Int("Src"), Int("Dest")
    r = _CALC.reverse(CalcInstruction(src * 3, dest), None, eq_target("Dest", 7), _ctx(src, dest))
    assert r.fallthrough


def test_real_zero_scale_still_falls_through() -> None:
    src, dest = Real("Src"), Real("Dest")
    r = _CALC.reverse(
        CalcInstruction(src * 0.0, dest),
        None,
        eq_target("Dest", 0.0),
        _ctx(src, dest),
    )
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


def test_ne_target_falls_through() -> None:
    # An equality/inequality-by-value target (==/!=) is not an ordering reverse.
    src, dest = Int("Src"), Int("Dest")
    ne_target = Cmp("Dest", "!=", 10)
    assert _CALC.reverse(
        CalcInstruction(src + 5, dest), None, ne_target, _ctx(src, dest)
    ).fallthrough


# --- reverse() — inequality (Cmp) targets -------------------------------------


def _ctx_snap(snapshot, *tags) -> CrossingContext:
    return CrossingContext(snapshot=snapshot, tags_by_name={t.name: t for t in tags})


def test_cmp_affine_add_shifts_bound() -> None:
    # dest = src + 10; dest > 60  ⟹  src > 50.
    src, dest = Int("Src"), Int("Dest")
    r = _CALC.reverse(CalcInstruction(src + 10, dest), None, Cmp("Dest", ">", 60), _ctx(src, dest))
    assert _only(r) == (Cmp("Src", ">", 50),)
    assert r.exact is False


def test_cmp_affine_sub_shifts_bound() -> None:
    # dest = src - 5; dest <= 10  ⟹  src <= 15.
    src, dest = Int("Src"), Int("Dest")
    r = _CALC.reverse(CalcInstruction(src - 5, dest), None, Cmp("Dest", "<=", 10), _ctx(src, dest))
    assert _only(r) == (Cmp("Src", "<=", 15),)


def test_cmp_affine_negate_flips_operator() -> None:
    # dest = 100 - src; dest > 40  ⟹  src < 60 (scale -1 flips > to <).
    src, dest = Int("Src"), Int("Dest")
    r = _CALC.reverse(CalcInstruction(100 - src, dest), None, Cmp("Dest", ">", 40), _ctx(src, dest))
    assert _only(r) == (Cmp("Src", "<", 60),)


def test_cmp_multiply_falls_through() -> None:
    # Multiply is non-bijective under wrap -> defer the inequality.
    src, dest = Int("Src"), Int("Dest")
    r = _CALC.reverse(CalcInstruction(src * 3, dest), None, Cmp("Dest", ">", 9), _ctx(src, dest))
    assert r.fallthrough


def test_cmp_tag_bound_falls_through() -> None:
    # A tag-valued bound (dest > Threshold) is not yet reversed here.
    src, dest = Int("Src"), Int("Dest")
    cmp_target = Cmp("Dest", ">", "Threshold", bound_is_tag=True)
    r = _CALC.reverse(CalcInstruction(src + 5, dest), None, cmp_target, _ctx(src, dest))
    assert r.fallthrough


def test_cmp_two_tag_add_freezes_partner_dnf() -> None:
    # dest = A + B; dest > 8, snapshot A=2,B=3  ⟹  (A > 5) ∨ (B > 6).
    a, b, dest = Int("A"), Int("B"), Int("Dest")
    r = _CALC.reverse(
        CalcInstruction(a + b, dest),
        None,
        Cmp("Dest", ">", 8),
        _ctx_snap({"A": 2, "B": 3}, a, b, dest),
    )
    assert r.branches == ((Cmp("A", ">", 5),), (Cmp("B", ">", 6),))
    assert r.exact is False


def test_cmp_two_tag_sub_flips_partner_branch() -> None:
    # dest = A - B; dest > 3, snapshot A=4,B=1
    #   left:  A > 3 + B_now = 4
    #   right: B < A_now - 3 = 1  (the -B term flips > to <)
    a, b, dest = Int("A"), Int("B"), Int("Dest")
    r = _CALC.reverse(
        CalcInstruction(a - b, dest),
        None,
        Cmp("Dest", ">", 3),
        _ctx_snap({"A": 4, "B": 1}, a, b, dest),
    )
    assert r.branches == ((Cmp("A", ">", 4),), (Cmp("B", "<", 1),))


def test_cmp_two_tag_no_snapshot_falls_through() -> None:
    # No snapshot value for the partner -> nothing to freeze against -> defer.
    a, b, dest = Int("A"), Int("B"), Int("Dest")
    r = _CALC.reverse(CalcInstruction(a + b, dest), None, Cmp("Dest", ">", 8), _ctx(a, b, dest))
    assert r.fallthrough


def test_cmp_sum_expr_falls_through() -> None:
    blk = Block("DS", TagType.INT, 1, 5)
    dest = Int("Total")
    instr = CalcInstruction(blk.select(1, 3).sum(), dest)
    r = _CALC.reverse(instr, None, Cmp("Total", ">", 0), _ctx(dest))
    assert r.fallthrough


# --- forward() — general affine classification --------------------------------


def test_forward_tag_plus_literal() -> None:
    src, dest = Int("Src"), Int("Dest")
    assert _CALC.forward(CalcInstruction(src + 10, dest), CrossingContext()) == Affine(
        source="Src", scale=1, offset=10
    )


def test_forward_literal_plus_tag() -> None:
    src, dest = Int("Src"), Int("Dest")
    assert _CALC.forward(CalcInstruction(10 + src, dest), CrossingContext()) == Affine(
        source="Src", scale=1, offset=10
    )


def test_forward_tag_minus_literal() -> None:
    src, dest = Int("Src"), Int("Dest")
    assert _CALC.forward(CalcInstruction(src - 5, dest), CrossingContext()) == Affine(
        source="Src", scale=1, offset=-5
    )


def test_forward_literal_minus_tag() -> None:
    src, dest = Int("Src"), Int("Dest")
    assert _CALC.forward(CalcInstruction(100 - src, dest), CrossingContext()) == Affine(
        source="Src", scale=-1, offset=100
    )


def test_forward_tag_mul_literal() -> None:
    src, dest = Int("Src"), Int("Dest")
    assert _CALC.forward(CalcInstruction(src * 3, dest), CrossingContext()) == Affine(
        source="Src", scale=3, offset=0
    )


def test_forward_literal_mul_tag() -> None:
    src, dest = Int("Src"), Int("Dest")
    assert _CALC.forward(CalcInstruction(3 * src, dest), CrossingContext()) == Affine(
        source="Src", scale=3, offset=0
    )


def test_forward_unary_plus() -> None:
    src, dest = Int("Src"), Int("Dest")
    assert _CALC.forward(CalcInstruction(+src, dest), CrossingContext()) == Affine(
        source="Src", scale=1, offset=0
    )


def test_forward_unary_negate() -> None:
    src, dest = Int("Src"), Int("Dest")
    assert _CALC.forward(CalcInstruction(-src, dest), CrossingContext()) == Affine(
        source="Src", scale=-1, offset=0
    )


def test_forward_multi_tag_returns_unknown() -> None:
    a, b, dest = Int("A"), Int("B"), Int("Dest")
    assert _CALC.forward(CalcInstruction(a + b, dest), CrossingContext()) is UNKNOWN


def test_forward_self_referential_still_works() -> None:
    acc = Int("Acc")
    assert _CALC.forward(CalcInstruction(acc + 1, acc), CrossingContext()) == Affine(
        source="Acc", scale=1, offset=1
    )


def test_forward_sum_returns_aggregate() -> None:
    blk = Block("DS", TagType.INT, 1, 5)
    dest = Int("Total")
    result = _CALC.forward(CalcInstruction(blk.select(1, 3).sum(), dest), CrossingContext())
    assert isinstance(result, Aggregate)
    assert result.operation == "sum"
    assert result.tags == ("DS1", "DS2", "DS3")
