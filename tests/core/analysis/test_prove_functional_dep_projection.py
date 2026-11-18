"""Tests for the functional dependency projection pass."""

from __future__ import annotations

from pyrung.core import (
    Bool,
    Int,
    Program,
    copy,
    math,
    out,
    return_early,
    rung,
    subroutine,
)
from pyrung.core.analysis.prove.passes import (
    _pass_build_graph,
    _pass_classify_dimensions,
    _pass_detect_functional_dependencies,
    _pass_elide_scan_local_state,
    _PassContext,
)
from pyrung.core.program import call


def _run_through_funcdep(program: Program) -> _PassContext:
    ctx = _PassContext(program=program, dt=0.01)
    _pass_build_graph(ctx)
    _pass_classify_dimensions(ctx)
    _pass_elide_scan_local_state(ctx)
    _pass_detect_functional_dependencies(ctx)
    return ctx


def test_simple_constant_offset_projected() -> None:
    """Y = X + 5, both cross-scan stateful -> Y projected."""
    x = Int("X", min=0, max=5)
    y = Int("Y", min=5, max=10)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with rung(y > 7):
            out(seen)
        with rung():
            math(x + 5, y)
            copy(0, x)

    ctx = _run_through_funcdep(logic)
    assert ctx.stateful_dims is not None

    assert "Y" not in ctx.stateful_dims
    assert ctx._elided_tags is not None
    assert ctx._elided_tags.get("Y") == "functional_dep"
    assert "X" in ctx.stateful_dims


def test_zero_offset_copy_projected() -> None:
    """Y = copy(X), both cross-scan stateful -> Y projected."""
    x = Int("X", min=0, max=3)
    y = Int("Y", min=0, max=3)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with rung(y > 1):
            out(seen)
        with rung():
            copy(x, y)
            copy(0, x)

    ctx = _run_through_funcdep(logic)
    assert ctx.stateful_dims is not None

    assert "Y" not in ctx.stateful_dims
    assert ctx._elided_tags is not None
    assert ctx._elided_tags.get("Y") == "functional_dep"


def test_mixed_writers_kept() -> None:
    """Tag written by a Math in one rung and literal in another stays stateful."""
    x = Int("X", min=0, max=3)
    y = Int("Y", min=0, max=8)
    cond = Bool("Cond")
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with rung(y > 5):
            out(seen)
        with rung():
            math(x + 5, y)
            copy(0, x)
        with rung(cond):
            copy(0, y)

    ctx = _run_through_funcdep(logic)
    assert ctx.stateful_dims is not None

    assert "Y" in ctx.stateful_dims


def test_different_offsets_kept() -> None:
    x = Int("X", min=0, max=3)
    y = Int("Y", min=0, max=8)
    cond = Bool("Cond")
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with rung(y > 5):
            out(seen)
        with rung(cond):
            math(x + 5, y)
        with rung(~cond):
            math(x + 3, y)
        with rung():
            copy(0, x)

    ctx = _run_through_funcdep(logic)
    assert ctx.stateful_dims is not None

    assert "Y" in ctx.stateful_dims


def test_self_reference_not_projected_as_funcdep() -> None:
    """Self-referencing math(x+1, x) should never be projected as a functional dep."""
    x = Int("X", min=0, max=10)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with rung(x > 5):
            out(seen)
        with rung():
            math(x + 1, x)

    ctx = _run_through_funcdep(logic)
    assert ctx._elided_tags is None or ctx._elided_tags.get("X") != "functional_dep"


def test_co_writing_violation_kept() -> None:
    """X written in rung A and B, Y=X+5 only in rung A -> Y kept."""
    x = Int("X", min=0, max=5)
    y = Int("Y", min=5, max=10)
    cond = Bool("Cond")
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with rung(y > 7):
            out(seen)
        with rung():
            math(x + 5, y)
            copy(0, x)
        with rung(cond):
            copy(1, x)

    ctx = _run_through_funcdep(logic)
    assert ctx.stateful_dims is not None

    assert "Y" in ctx.stateful_dims


def test_sequential_unconditional_same_sub_projected() -> None:
    """Math chain in same subroutine, sequential, unconditional -> projected.

    Mirrors the historian pattern: X is computed from a nondeterministic
    source via a non-linear expression, then Y = X + offset follows
    unconditionally in the same subroutine with no return_early between.
    X survives sliced elision because it is condition-read in a main rung.
    """
    src = Int("Src", min=1, max=4)
    x = Int("X", min=0, max=50)
    y = Int("Y", min=0, max=55)
    trigger = Bool("Trigger", external=True)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with rung(y > 30):
            out(seen)
        with rung(x > 20):
            out(seen)
        with rung(trigger):
            copy(2, src)
        with rung():
            call("my_sub")
        with subroutine("my_sub"):
            with rung():
                math(src * 10, x)
            with rung():
                math(x + 5, y)

    ctx = _run_through_funcdep(logic)
    assert ctx.stateful_dims is not None
    assert "X" in ctx.stateful_dims
    assert "Y" not in ctx.stateful_dims
    assert ctx._elided_tags is not None
    assert ctx._elided_tags.get("Y") == "functional_dep"


def test_return_early_between_source_and_dep_kept() -> None:
    """return_early() between X writer and Y writer blocks projection."""
    x = Int("X", min=0, max=5)
    y = Int("Y", min=5, max=10)
    cond = Bool("Cond")
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with rung(y > 7):
            out(seen)
        with rung():
            call("my_sub")
        with subroutine("my_sub"):
            with rung():
                copy(3, x)
            with rung(cond):
                return_early()
            with rung():
                math(x + 5, y)

    ctx = _run_through_funcdep(logic)
    assert ctx.stateful_dims is not None
    assert "Y" in ctx.stateful_dims


def test_cross_scope_not_projected() -> None:
    """X in subroutine, Y in main -> not projected by sequential check."""
    x = Int("X", min=0, max=5)
    y = Int("Y", min=5, max=10)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with rung(y > 7):
            out(seen)
        with rung():
            call("my_sub")
        with rung():
            math(x + 5, y)
        with subroutine("my_sub"):
            with rung():
                copy(3, x)

    ctx = _run_through_funcdep(logic)
    assert ctx.stateful_dims is not None
    assert "Y" in ctx.stateful_dims


def _historian_with_loop(count: object, *, oneshot: bool = False) -> Program:
    """The historian chain above, with Y's math() inside a forloop."""
    from pyrung.core import forloop

    src = Int("Src", min=1, max=4)
    x = Int("X", min=0, max=50)
    y = Int("Y", min=0, max=55)
    trigger = Bool("Trigger", external=True)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with rung(y > 30):
            out(seen)
        with rung(x > 20):
            out(seen)
        with rung(trigger):
            copy(2, src)
        with rung():
            call("my_sub")
        with subroutine("my_sub"):
            with rung():
                math(src * 10, x)
            with rung():
                with forloop(count, oneshot=oneshot):
                    math(x + 5, y)
    return logic


def test_sequential_literal_count_loop_projected() -> None:
    """forloop(2) on a contactless rung runs every scan: Y is still projected."""
    ctx = _run_through_funcdep(_historian_with_loop(2))
    assert ctx.stateful_dims is not None
    assert "Y" not in ctx.stateful_dims
    assert ctx._elided_tags is not None
    assert ctx._elided_tags.get("Y") == "functional_dep"


def test_sequential_tag_count_loop_kept() -> None:
    """forloop(N) runs no iteration when N == 0, so Y can hold: kept."""
    count = Int("N", external=True, min=0, max=2)
    ctx = _run_through_funcdep(_historian_with_loop(count))
    assert ctx.stateful_dims is not None
    assert "Y" in ctx.stateful_dims


def test_sequential_oneshot_loop_kept() -> None:
    """forloop(2, oneshot=True) runs on the first scan only, so Y holds: kept."""
    ctx = _run_through_funcdep(_historian_with_loop(2, oneshot=True))
    assert ctx.stateful_dims is not None
    assert "Y" in ctx.stateful_dims
