"""Tests for the static facts that how() steers with."""

from __future__ import annotations

from pyrung.core import (
    Block,
    Bool,
    InputBlock,
    Int,
    OutputBlock,
    Program,
    Rung,
    TagType,
    copy,
    math,
    out,
)
from pyrung.core.analysis.prove import Intractable
from pyrung.core.analysis.prove.classify import _classify_dimensions

# ===================================================================
# InputBlock / TagMap input inference
# ===================================================================


class TestInputBlockNondeterministic:
    def test_input_block_tags_classified_nondeterministic(self):
        x = InputBlock("X", TagType.BOOL, 1, 4)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(x[1]):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        sd, nd, combinational, _, _, _ = result
        assert "X1" in nd
        assert nd["X1"] == (False, True)
        assert "X1" not in sd

    def test_output_block_tags_not_nondeterministic(self):
        x = InputBlock("X", TagType.BOOL, 1, 4)
        y = OutputBlock("Y", TagType.BOOL, 1, 4)

        with Program(strict=False) as logic:
            with Rung(x[1]):
                out(y[1])

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        sd, nd, combinational, _, _, _ = result
        assert "X1" in nd
        assert "Y1" not in nd
        assert "Y1" in combinational

    def test_tagmap_stamps_external_on_input_mapped_tags(self):
        from pyrung.click import ClickBlocks, TagMap

        blocks = ClickBlocks()
        x, y = blocks.x, blocks.y
        button = Bool("Button")
        motor = Bool("Motor")
        assert not button.external
        assert not motor.external

        TagMap({button: x[1], motor: y[1]}, include_system=False)

        assert button.external
        assert not motor.external

    def test_tagmap_stamped_tag_becomes_nondeterministic(self):
        from pyrung.click import ClickBlocks, TagMap

        x = ClickBlocks().x
        button = Bool("Button")
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(button):
                out(light)

        TagMap({button: x[1]}, include_system=False)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _, nd, _, _, _, _ = result
        assert "Button" in nd


class TestPointerDomainInference:
    """Pointer tags into blocks get auto-bounded from block address range."""

    def test_external_pointer_auto_bounded(self):
        """External pointer with no annotation gets domain from block bounds."""
        blk = Block("DS", TagType.INT, 1, 10)
        idx = Int("Idx", external=True)
        dest = Int("Out")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nondeterministic, *_ = result
        assert "Idx" in nondeterministic
        assert nondeterministic["Idx"] == tuple(range(0, 11))

    def test_explicit_choices_not_overridden(self):
        """Pointer with explicit choices= keeps its annotated domain."""
        blk = Block("DS", TagType.INT, 1, 10)
        idx = Int("Idx", external=True, choices={1: "first", 5: "fifth"})
        dest = Int("Out")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nondeterministic, *_ = result
        assert "Idx" in nondeterministic
        assert nondeterministic["Idx"] == (1, 5)

    def test_explicit_min_max_not_overridden(self):
        """Pointer with explicit min=/max= keeps its annotated domain."""
        blk = Block("DS", TagType.INT, 1, 50)
        idx = Int("Idx", external=True, min=1, max=5)
        dest = Int("Out")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nondeterministic, *_ = result
        assert "Idx" in nondeterministic
        assert nondeterministic["Idx"] == tuple(range(1, 6))

    def test_literal_copy_domain_not_overridden(self):
        """Pointer already inferred by literal copies keeps that tighter domain."""
        blk = Block("DS", TagType.INT, 1, 50)
        idx = Int("Idx")
        dest = Int("Out")
        sel = Bool("Sel", external=True)

        with Program(strict=False) as logic:
            with Rung(sel):
                copy(1, idx)
            with Rung(~sel):
                copy(3, idx)
            with Rung():
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nondeterministic, *_ = result
        assert "Idx" in stateful
        assert set(stateful["Idx"]) == {0, 1, 3}

    def test_wide_block_still_intractable(self):
        """Block with > 1000 addresses leaves the pointer intractable."""
        blk = Block("Big", TagType.INT, 1, 2000)
        idx = Int("Idx", external=True)
        dest = Int("Out")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert "Idx" in result.tags

    def test_internal_pointer_auto_bounded(self):
        """Internal pointer written by calc gets domain from block bounds."""
        blk = Block("DS", TagType.INT, 1, 10)
        idx = Int("Idx")
        dest = Int("Out")
        step = Bool("Step", external=True)

        with Program(strict=False) as logic:
            with Rung(step):
                math(idx + 1, idx)
            with Rung():
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, *_ = result
        assert "Idx" in stateful
        assert stateful["Idx"] == tuple(range(0, 11))

    def test_unconditioned_pointer_not_silently_dropped(self):
        """A pointer used only in instructions (no conditions) must still
        surface as intractable.  It must not be silently dropped because it has
        no comparison atoms."""
        blk = Block("DS", TagType.INT, 1, 2000)
        idx = Int("Idx", external=True)
        dest = Int("Out")
        flag = Bool("Flag", external=True)

        with Program(strict=False) as logic:
            with Rung(flag):
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert "Idx" in result.tags
        assert any("pointer" in h and "Idx" in h for h in result.hints)
