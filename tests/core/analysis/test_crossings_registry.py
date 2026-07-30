"""Crossings — registry API + Constraint/ReverseResult contract.

Unit-level coverage of the scaffold: the constraint algebra, the DNF result, the
register/lookup/MRO machinery, and the soundness defaults (unregistered →
FALLTHROUGH, forward → UNKNOWN, projected context carries no recorded evidence).
"""

from __future__ import annotations

from pyrung.core.analysis import crossings
from pyrung.core.analysis.crossings import (
    BaseCrossing,
    crossing_for,
    forward,
    propose,
    register,
    registered_classes,
    reverse,
)
from pyrung.core.crossing import (
    REVERSE_FALLTHROUGH,
    UNKNOWN,
    Constraint,
    CrossingContext,
    Eq,
    Literal,
    ReverseResult,
    eq_target,
    satisfied,
    single,
    unsatisfiable,
)


class _Dummy:
    """Stand-in instruction class (not a real instruction)."""


class _DummySub(_Dummy):
    """Subclass with no crossing of its own — resolves to ``_Dummy`` via MRO."""


class _DummyCrossing(BaseCrossing):
    def reverse(self, instr, rung, target, ctx):
        (value,) = target.values  # target is Eq(tag, {value})
        return single(Eq("Src", frozenset({value})), exact=True)


class _TargetAwareCrossing(BaseCrossing):
    def forward(self, instr, target_tag, ctx):
        return Literal(target_tag)


def _ctx() -> CrossingContext:
    return CrossingContext(snapshot={}, tags_by_name={})


# --- constraint algebra -------------------------------------------------------


def test_constraint_subtypes_are_frozen_values() -> None:
    a = Eq("X", frozenset({1}))
    b = Eq("X", frozenset({1}))
    assert a == b and hash(a) == hash(b)  # frozen, hashable, value-equal
    assert isinstance(a, Constraint)


# --- result contract ----------------------------------------------------------


def test_reverse_result_defaults() -> None:
    r = ReverseResult()
    assert r.branches == ()
    assert r.exact is False
    assert r.fallthrough is False


def test_single_branch_shape() -> None:
    r = single(Eq("Src", frozenset({7})), exact=True)
    assert r.branches == ((Eq("Src", frozenset({7})),),)
    assert r.exact is True
    assert r.fallthrough is False


def test_satisfied_is_one_empty_branch() -> None:
    r = satisfied()
    assert r.branches == ((),)  # trivially-true: no input constraint needed
    assert r.exact is True


def test_fallthrough_singleton_shape() -> None:
    assert REVERSE_FALLTHROUGH.fallthrough is True
    assert REVERSE_FALLTHROUGH.branches == ()


def test_unsatisfiable_encoding_is_empty_eq() -> None:
    # Eq(dest, frozenset()) = "no value works" — the pinned structural blocker.
    r = unsatisfiable("Dest")
    ((constraint,),) = r.branches
    assert constraint == Eq("Dest", frozenset())
    assert not r.fallthrough


# --- registry machinery -------------------------------------------------------


def test_unregistered_reverse_is_fallthrough() -> None:
    assert reverse(_Dummy(), None, eq_target("X", 1), _ctx()).fallthrough is True


def test_unregistered_forward_is_unknown() -> None:
    assert forward(_Dummy(), "Dest", _ctx()) is UNKNOWN


def test_unregistered_proposal_is_empty_and_not_a_reverse_claim() -> None:
    proposal = propose(_Dummy(), None, eq_target("X", 1), _ctx())
    assert proposal.empty
    assert proposal.branches == ()
    assert proposal.verify_required is False


def test_register_and_exact_lookup() -> None:
    register(_Dummy, _DummyCrossing())
    try:
        assert crossing_for(_Dummy()) is not None
        assert _Dummy in registered_classes()
        r = reverse(_Dummy(), None, eq_target("Dest", 7), _ctx())
        assert r.branches == ((Eq("Src", frozenset({7})),),)
        assert r.exact is True
    finally:
        crossings._REGISTRY.pop(_Dummy, None)


def test_mro_walk_resolves_subclass_to_base() -> None:
    register(_Dummy, _DummyCrossing())
    try:
        # _DummySub has no crossing of its own -> inherits _Dummy's via MRO.
        assert crossing_for(_DummySub()) is not None
        r = reverse(_DummySub(), None, eq_target("Dest", 3), _ctx())
        assert r.branches == ((Eq("Src", frozenset({3})),),)
    finally:
        crossings._REGISTRY.pop(_Dummy, None)


def test_base_crossing_defaults_are_sound() -> None:
    base = BaseCrossing()
    assert base.reverse(_Dummy(), None, eq_target("X", 1), _ctx()).fallthrough is True
    assert base.forward(_Dummy(), "X", _ctx()) is UNKNOWN
    assert base.propose(_Dummy(), None, eq_target("X", 1), _ctx()).empty


def test_target_aware_forward_preserves_destination_identity() -> None:
    register(_Dummy, _TargetAwareCrossing())
    try:
        assert forward(_Dummy(), "Dest2", _ctx()) == Literal("Dest2")
    finally:
        crossings._REGISTRY.pop(_Dummy, None)
