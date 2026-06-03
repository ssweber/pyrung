"""Tests for the init constant projection pass (Patterns A, B, and C)."""

from __future__ import annotations

from pyrung.core import (
    Bool,
    InputBlock,
    Int,
    Program,
    Rung,
    copy,
    latch,
    out,
    reset,
    rise,
)
from pyrung.core.analysis.prove import Proven, always
from pyrung.core.analysis.prove.passes import (
    _pass_build_graph,
    _pass_classify_dimensions,
    _pass_detect_functional_dependencies,
    _pass_detect_init_constants,
    _pass_elide_scan_local_state,
    _PassContext,
)
from pyrung.core.system_points import system
from pyrung.core.tag import TagType


def _run_through_initconst(program: Program) -> _PassContext:
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
    _pass_detect_init_constants(ctx)
    return ctx


# --- Pattern A: self-latching Bool guard ---


def test_simple_init_pattern_projected() -> None:
    init_done = Bool("InitDone")
    cfg = Int("Cfg", min=0, max=10)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(cfg > 3):
            out(seen)
        with Rung(~init_done):
            copy(5, cfg)
            latch(init_done)

    ctx = _run_through_initconst(logic)
    assert ctx.stateful_dims is not None

    assert "Cfg" not in ctx.stateful_dims
    assert ctx._elided_tags is not None
    assert ctx._elided_tags.get("Cfg") == "init_constant"
    assert "InitDone" in ctx.stateful_dims


def test_copy_true_latch_still_valid() -> None:
    init_done = Bool("InitDone")
    cfg = Int("Cfg", min=0, max=10)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(cfg > 3):
            out(seen)
        with Rung(~init_done):
            copy(5, cfg)
            copy(True, init_done)

    ctx = _run_through_initconst(logic)
    assert ctx.stateful_dims is not None

    assert "Cfg" not in ctx.stateful_dims
    assert "InitDone" in ctx.stateful_dims


def test_additional_writer_outside_init_rung_kept() -> None:
    init_done = Bool("InitDone")
    cfg = Int("Cfg", min=0, max=10)
    trigger = Bool("Trigger")
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(cfg > 3):
            out(seen)
        with Rung(~init_done):
            copy(5, cfg)
            latch(init_done)
        with Rung(trigger):
            copy(3, cfg)

    ctx = _run_through_initconst(logic)
    assert ctx.stateful_dims is not None

    assert "Cfg" in ctx.stateful_dims


def test_non_literal_init_write_kept() -> None:
    init_done = Bool("InitDone")
    src = Int("Src", min=0, max=10)
    cfg = Int("Cfg", min=0, max=10)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(cfg > 3):
            out(seen)
        with Rung(~init_done):
            copy(src, cfg)
            latch(init_done)

    ctx = _run_through_initconst(logic)
    assert ctx.stateful_dims is not None

    assert "Cfg" in ctx.stateful_dims


def test_latch_not_monotonic_nothing_projected() -> None:
    init_done = Bool("InitDone")
    cfg = Int("Cfg", min=0, max=10)
    clear = Bool("Clear")
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(cfg > 3):
            out(seen)
        with Rung(~init_done):
            copy(5, cfg)
            latch(init_done)
        with Rung(clear):
            reset(init_done)

    ctx = _run_through_initconst(logic)
    assert ctx.stateful_dims is not None

    assert "Cfg" in ctx.stateful_dims


def test_multiple_independent_latches() -> None:
    init_a = Bool("InitA")
    init_b = Bool("InitB")
    cfg_a = Int("CfgA", min=0, max=10)
    cfg_b = Int("CfgB", min=0, max=10)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(cfg_a > 3):
            out(seen)
        with Rung(cfg_b > 5):
            out(seen)
        with Rung(~init_a):
            copy(5, cfg_a)
            latch(init_a)
        with Rung(~init_b):
            copy(7, cfg_b)
            latch(init_b)

    ctx = _run_through_initconst(logic)
    assert ctx.stateful_dims is not None

    assert "CfgA" not in ctx.stateful_dims
    assert "CfgB" not in ctx.stateful_dims
    assert "InitA" in ctx.stateful_dims
    assert "InitB" in ctx.stateful_dims


def test_edge_source_under_init_kept() -> None:
    init_done = Bool("InitDone")
    cfg = Bool("Cfg")
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(~init_done):
            copy(True, cfg)
            latch(init_done)
        with Rung(rise(cfg)):
            out(seen)

    ctx = _run_through_initconst(logic)

    if "Cfg" in (ctx._elided_tags or {}):
        assert ctx._elided_tags["Cfg"] != "init_constant"


def test_multiple_tags_in_separate_init_rungs() -> None:
    """DH tags written in different rungs all guarded by ~InitDone."""
    init_done = Bool("InitDone")
    dh1 = Int("DH1", min=0, max=100)
    dh2 = Int("DH2", min=0, max=100)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(dh1 > 5):
            out(seen)
        with Rung(dh2 > 10):
            out(seen)
        with Rung(~init_done):
            copy(10, dh1)
        with Rung(~init_done):
            copy(20, dh2)
        with Rung(~init_done):
            latch(init_done)

    ctx = _run_through_initconst(logic)
    assert ctx.stateful_dims is not None

    assert "DH1" not in ctx.stateful_dims
    assert "DH2" not in ctx.stateful_dims
    assert "InitDone" in ctx.stateful_dims


# --- Pattern B: co-latching nondeterministic guard ---


def test_colatch_two_tags_under_nd_guard() -> None:
    """Two tags under same ND Bool guard, same rung, literal writes -> 1 projected."""
    inp = InputBlock("Inp", TagType.BOOL, 1, 1)
    f = inp[1]
    x = Int("X", min=0, max=10)
    y = Int("Y", min=0, max=10)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(x > 3):
            out(seen)
        with Rung(y > 5):
            out(seen)
        with Rung(f):
            copy(5, x)
            copy(7, y)

    ctx = _run_through_initconst(logic)
    assert ctx.stateful_dims is not None

    projected_count = sum(
        1
        for name in ("X", "Y")
        if name not in ctx.stateful_dims
        and ctx._elided_tags is not None
        and ctx._elided_tags.get(name) == "init_constant_colatch"
    )
    assert projected_count >= 1


def test_colatch_different_rungs_separate_groups() -> None:
    """Tags under same guard but different rungs -> separate groups."""
    inp = InputBlock("Inp", TagType.BOOL, 1, 1)
    f = inp[1]
    x = Int("X", min=0, max=10)
    y = Int("Y", min=0, max=10)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(x > 3):
            out(seen)
        with Rung(y > 5):
            out(seen)
        with Rung(f):
            copy(5, x)
        with Rung(f):
            copy(7, y)

    ctx = _run_through_initconst(logic)
    assert ctx.stateful_dims is not None

    assert "X" in ctx.stateful_dims
    assert "Y" in ctx.stateful_dims


def test_colatch_single_tag_no_projection() -> None:
    inp = InputBlock("Inp", TagType.BOOL, 1, 1)
    f = inp[1]
    x = Int("X", min=0, max=10)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(x > 3):
            out(seen)
        with Rung(f):
            copy(5, x)

    ctx = _run_through_initconst(logic)
    assert ctx.stateful_dims is not None

    assert "X" in ctx.stateful_dims


def test_prove_soundness_with_init_constant() -> None:
    """Prove a property that references an init-constant tag."""
    init_done = Bool("InitDone")
    cfg = Int("Cfg", min=0, max=100)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(cfg > 50):
            out(seen)
        with Rung(~init_done):
            copy(42, cfg)
            latch(init_done)

    result = always(logic, cfg <= 42)
    assert isinstance(result, Proven)


def test_first_scan_pattern_soundness() -> None:
    """Prove soundness with co-latching first_scan-style pattern."""
    inp = InputBlock("Inp", TagType.BOOL, 1, 1)
    f = inp[1]
    x = Int("X", min=0, max=10)
    y = Int("Y", min=0, max=10)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(x > 3):
            out(seen)
        with Rung(y > 5):
            out(seen)
        with Rung(f):
            copy(5, x)
            copy(7, y)

    result = always(logic, x <= 5)
    assert isinstance(result, Proven)


# --- Pattern C: system.sys.first_scan guard ---


def test_first_scan_system_guard_projects_peers() -> None:
    """Multiple tags under sys.first_scan, one representative kept."""
    cfg_a = Int("CfgA", min=0, max=10)
    cfg_b = Int("CfgB", min=0, max=10)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(cfg_a > 3):
            out(seen)
        with Rung(cfg_b > 5):
            out(seen)
        with Rung(system.sys.first_scan):
            copy(5, cfg_a)
            copy(7, cfg_b)

    ctx = _run_through_initconst(logic)
    assert ctx.stateful_dims is not None

    projected_count = sum(
        1
        for name in ("CfgA", "CfgB")
        if name not in ctx.stateful_dims
        and ctx._elided_tags is not None
        and ctx._elided_tags.get(name) == "init_constant_first_scan"
    )
    assert projected_count >= 1

    kept_count = sum(1 for name in ("CfgA", "CfgB") if name in ctx.stateful_dims)
    assert kept_count == 1


def test_first_scan_single_tag_not_projected() -> None:
    """A single tag under sys.first_scan has no peer -> kept."""
    cfg = Int("Cfg", min=0, max=10)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(cfg > 3):
            out(seen)
        with Rung(system.sys.first_scan):
            copy(5, cfg)

    ctx = _run_through_initconst(logic)
    assert ctx.stateful_dims is not None
    assert "Cfg" in ctx.stateful_dims


def test_first_scan_across_rungs() -> None:
    """Tags in separate sys.first_scan rungs still form one group."""
    cfg_a = Int("CfgA", min=0, max=10)
    cfg_b = Int("CfgB", min=0, max=10)
    cfg_c = Int("CfgC", min=0, max=10)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(cfg_a > 3):
            out(seen)
        with Rung(cfg_b > 5):
            out(seen)
        with Rung(cfg_c > 7):
            out(seen)
        with Rung(system.sys.first_scan):
            copy(5, cfg_a)
        with Rung(system.sys.first_scan):
            copy(7, cfg_b)
        with Rung(system.sys.first_scan):
            copy(9, cfg_c)

    ctx = _run_through_initconst(logic)
    assert ctx.stateful_dims is not None

    kept = [n for n in ("CfgA", "CfgB", "CfgC") if n in ctx.stateful_dims]
    projected = [
        n
        for n in ("CfgA", "CfgB", "CfgC")
        if ctx._elided_tags is not None and ctx._elided_tags.get(n) == "init_constant_first_scan"
    ]
    assert len(kept) == 1
    assert len(projected) == 2


def test_first_scan_representative_needs_nondefault() -> None:
    """Representative must have literal != default; all-default group stays."""
    cfg_a = Int("CfgA", min=0, max=10)
    cfg_b = Int("CfgB", min=0, max=10)
    seen = Bool("Seen")

    with Program(strict=False) as logic:
        with Rung(cfg_a > 3):
            out(seen)
        with Rung(cfg_b > 5):
            out(seen)
        with Rung(system.sys.first_scan):
            copy(0, cfg_a)
            copy(0, cfg_b)

    ctx = _run_through_initconst(logic)
    assert ctx.stateful_dims is not None
    assert "CfgA" in ctx.stateful_dims
    assert "CfgB" in ctx.stateful_dims
