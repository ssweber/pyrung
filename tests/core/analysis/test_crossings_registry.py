"""Crossings Phase 2 — registry API + ReverseResult contract.

Unit-level coverage of the scaffold: the result/context types, the
register/lookup/MRO machinery, and the soundness defaults (unregistered →
FALLTHROUGH, forward → UNKNOWN, projected context carries no recorded evidence).
"""

from __future__ import annotations

from pyrung.core.analysis import crossings
from pyrung.core.analysis.crossings import (
    BaseCrossing,
    crossing_for,
    forward,
    register,
    registered_classes,
    reverse,
)
from pyrung.core.crossing import (
    REVERSE_FALLTHROUGH,
    UNKNOWN,
    CrossingContext,
    ReverseResult,
)


class _Dummy:
    """Stand-in instruction class (not a real instruction)."""


class _DummySub(_Dummy):
    """Subclass with no crossing of its own — resolves to ``_Dummy`` via MRO."""


class _DummyCrossing(BaseCrossing):
    def reverse(self, instr, target_tag, target_value, ctx):
        return ReverseResult(constraints=[("Src", frozenset({target_value}))], exact=True)


def _ctx() -> CrossingContext:
    return CrossingContext(snapshot={}, tags_by_name={})


# --- result / context contract ------------------------------------------------


def test_reverse_result_defaults() -> None:
    r = ReverseResult()
    assert r.constraints == []
    assert r.exact is False
    assert r.fallthrough is False


def test_fallthrough_singleton_shape() -> None:
    assert REVERSE_FALLTHROUGH.fallthrough is True
    assert REVERSE_FALLTHROUGH.constraints == []


def test_unsatisfiable_encoding_is_empty_frozenset() -> None:
    # [(dest, frozenset())] = "no value works" — the pinned structural blocker.
    r = ReverseResult(constraints=[("Dest", frozenset())])
    assert not r.fallthrough
    ((tag, allowed),) = r.constraints
    assert tag == "Dest"
    assert allowed == frozenset()


def test_projected_context_has_no_recorded_evidence() -> None:
    # Soundness invariant: a projected / prover-path context never carries
    # recorded evidence, so it cannot leak into seeding.
    assert _ctx().value_at_scan is None


# --- registry machinery -------------------------------------------------------


def test_unregistered_reverse_is_fallthrough() -> None:
    assert reverse(_Dummy(), "X", 1, _ctx()).fallthrough is True


def test_unregistered_forward_is_unknown() -> None:
    assert forward(_Dummy(), _ctx()) is UNKNOWN


def test_register_and_exact_lookup() -> None:
    register(_Dummy, _DummyCrossing())
    try:
        assert crossing_for(_Dummy()) is not None
        assert _Dummy in registered_classes()
        r = reverse(_Dummy(), "Dest", 7, _ctx())
        assert r.constraints == [("Src", frozenset({7}))]
        assert r.exact is True
    finally:
        crossings._REGISTRY.pop(_Dummy, None)


def test_mro_walk_resolves_subclass_to_base() -> None:
    register(_Dummy, _DummyCrossing())
    try:
        # _DummySub has no crossing of its own -> inherits _Dummy's via MRO.
        assert crossing_for(_DummySub()) is not None
        r = reverse(_DummySub(), "Dest", 3, _ctx())
        assert r.constraints == [("Src", frozenset({3}))]
    finally:
        crossings._REGISTRY.pop(_Dummy, None)


def test_base_crossing_defaults_are_sound() -> None:
    base = BaseCrossing()
    assert base.reverse(_Dummy(), "X", 1, _ctx()).fallthrough is True
    assert base.forward(_Dummy(), _ctx()) is UNKNOWN
