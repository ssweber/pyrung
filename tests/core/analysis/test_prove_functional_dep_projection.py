"""Tests for the functional dependency projection pass."""

from __future__ import annotations

from pyrung.core import (
    Bool,
    Int,
    Program,
    Rung,
    calc,
    copy,
    out,
    return_early,
    subroutine,
)
from pyrung.core.analysis.prove import Proven, always
from pyrung.core.analysis.prove.passes import (
    _pass_build_graph,
    _pass_classify_dimensions,
    _pass_detect_functional_dependencies,
    _pass_elide_scan_local_state,
    _PassContext,
)
from pyrung.core.program import call


def _run_through_funcdep(program: Program) -> _PassContext:
    ctx = _PassContext(
        program=program,
        scope=None,
        project=None,
        extra_exprs=None,
        dt=0.01,
        compiled=None,
    )
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
        with Rung(y > 7):
            out(seen)
        with Rung():
            calc(x + 5, y)
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
        with Rung(y > 1):
            out(seen)
        with Rung():
            copy(x, y)
            copy(0, x)

    ctx = _run_through_funcdep(logic)
    assert ctx.stateful_dims is not None

    assert "Y" not in ctx.stateful_dims
    assert ctx._elided_tags is not None
    assert ctx._elided_tags.get("Y") == "functional_dep"


def test_mixed_writers_kept() -> None:
    """Tag written by calc in one rung and literal in another stays stateful."""
    x = Int("X", min=0, max=3)
    y = Int("Y", min=0, max=8)
    cond = Bool("Cond")
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(y > 5):
            out(seen)
        with Rung():
            calc(x + 5, y)
            copy(0, x)
        with Rung(cond):
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
        with Rung(y > 5):
            out(seen)
        with Rung(cond):
            calc(x + 5, y)
        with Rung(~cond):
            calc(x + 3, y)
        with Rung():
            copy(0, x)

    ctx = _run_through_funcdep(logic)
    assert ctx.stateful_dims is not None

    assert "Y" in ctx.stateful_dims


def test_self_reference_not_projected_as_funcdep() -> None:
    """Self-referencing calc(x+1, x) should never be projected as a functional dep."""
    x = Int("X", min=0, max=10)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(x > 5):
            out(seen)
        with Rung():
            calc(x + 1, x)

    ctx = _run_through_funcdep(logic)
    assert ctx._elided_tags is None or ctx._elided_tags.get("X") != "functional_dep"


def test_co_writing_violation_kept() -> None:
    """X written in rung A and B, Y=X+5 only in rung A -> Y kept."""
    x = Int("X", min=0, max=5)
    y = Int("Y", min=5, max=10)
    cond = Bool("Cond")
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(y > 7):
            out(seen)
        with Rung():
            calc(x + 5, y)
            copy(0, x)
        with Rung(cond):
            copy(1, x)

    ctx = _run_through_funcdep(logic)
    assert ctx.stateful_dims is not None

    assert "Y" in ctx.stateful_dims


def test_sequential_unconditional_same_sub_projected() -> None:
    """Calc chain in same subroutine, sequential, unconditional -> projected.

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
        with Rung(y > 30):
            out(seen)
        with Rung(x > 20):
            out(seen)
        with Rung(trigger):
            copy(2, src)
        with Rung():
            call("my_sub")
        with subroutine("my_sub"):
            with Rung():
                calc(src * 10, x)
            with Rung():
                calc(x + 5, y)

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
        with Rung(y > 7):
            out(seen)
        with Rung():
            call("my_sub")
        with subroutine("my_sub"):
            with Rung():
                copy(3, x)
            with Rung(cond):
                return_early()
            with Rung():
                calc(x + 5, y)

    ctx = _run_through_funcdep(logic)
    assert ctx.stateful_dims is not None
    assert "Y" in ctx.stateful_dims


def test_cross_scope_not_projected() -> None:
    """X in subroutine, Y in main -> not projected by sequential check."""
    x = Int("X", min=0, max=5)
    y = Int("Y", min=5, max=10)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(y > 7):
            out(seen)
        with Rung():
            call("my_sub")
        with Rung():
            calc(x + 5, y)
        with subroutine("my_sub"):
            with Rung():
                copy(3, x)

    ctx = _run_through_funcdep(logic)
    assert ctx.stateful_dims is not None
    assert "Y" in ctx.stateful_dims


def test_prove_soundness_with_functional_dep() -> None:
    """Prove a property that references both X and Y with Y projected."""
    x = Int("X", min=0, max=3)
    y = Int("Y", min=0, max=8)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(y > 7):
            out(seen)
        with Rung():
            calc(x + 5, y)
            copy(0, x)

    result = always(logic, y <= 10)
    assert isinstance(result, Proven)
