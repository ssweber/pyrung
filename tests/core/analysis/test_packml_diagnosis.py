"""Regression tests for classification, taken from the PackML diagnostic bench.

An external tag that the program also writes counts as an input only while some read can
see a value that the program did not write.
"""

from __future__ import annotations

from pyrung.core import (
    Bool,
    Int,
    Program,
    Rung,
    copy,
    latch,
)
from pyrung.core.analysis.prove import Intractable
from pyrung.core.analysis.prove.classify import _classify_dimensions

# ===================================================================
# Test 2: Classification of external+written tags (Theory 1)
# ===================================================================


class TestExternalWrittenClassification:
    def test_unconditionally_written_external_without_readers_is_combinational(self):
        """External+written tag with no readers is correctly combinational."""
        val = Int("Val", choices={0: "A", 1: "B", 2: "C"}, external=True)
        enable = Bool("Enable", external=True)

        with Program(strict=False) as logic:
            with Rung():
                copy(0, val)
            with Rung(enable):
                copy(1, val)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful_dims, nondeterministic_dims, combinational_tags, *_ = result
        assert "Val" in combinational_tags
        assert "Val" not in nondeterministic_dims

    def test_written_external_with_reader_is_stateful(self):
        """External+written tag WITH a downstream reader should be stateful."""
        cmd = Int("Cmd", choices={0: "None", 1: "A", 2: "B"}, external=True)
        btn_a = Bool("BtnA", external=True)
        btn_b = Bool("BtnB", external=True)
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung():
                copy(0, cmd)
            with Rung(btn_a):
                copy(1, cmd)
            with Rung(btn_b):
                copy(2, cmd)
            with Rung(cmd == 1):
                latch(output)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful_dims, nondeterministic_dims, *_ = result
        assert "Cmd" in stateful_dims, (
            f"Cmd should be stateful when it has downstream readers. "
            f"stateful={list(stateful_dims)}, nd={list(nondeterministic_dims)}"
        )
