"""Prover coverage for range aggregates (sum / max / min / median / mean) inside math()."""

from __future__ import annotations

from pyrung.core import Block, Int, TagType
from pyrung.core.analysis.prove.classify import _expr_reads_tag
from pyrung.core.expression import sqrt


class TestExprReadsTag:
    def test_sees_range_members(self):
        """The self-feed check must see members read through sum and aggregates."""
        ds = Block("DS", TagType.INT, 1, 4)
        assert _expr_reads_tag(ds.select(1, 3).sum() + 1, "DS2")
        assert _expr_reads_tag(ds.select(1, 3).max(), "DS3")
        assert not _expr_reads_tag(ds.select(1, 3).median(), "DS4")

    def test_sees_math_function_operands(self):
        x = Int("X")
        assert _expr_reads_tag(sqrt(x + 1), "X")
