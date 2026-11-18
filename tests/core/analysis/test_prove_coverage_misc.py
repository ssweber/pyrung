"""Tests for the static facts that how() steers with."""

from __future__ import annotations

from pyrung.core import (
    Bool,
    Int,
    Program,
    out,
    run_function,
    rung,
)
from pyrung.core.analysis.prove import Intractable
from pyrung.core.analysis.prove.classify import _classify_dimensions


class TestAnnotatedFunctionOutput:
    """Item 16: annotated function outputs succeed verification."""

    def test_choices_annotated_function_is_verifiable(self):
        """run_function output with choices is not Intractable."""
        trigger = Bool("Trigger", external=True)
        result_tag = Int("Result", choices={0: "off", 1: "on"})

        def compute() -> dict:
            return {"result": 1}

        with Program(strict=False) as logic:
            with rung(trigger):
                run_function(compute, outs={"result": result_tag})
            with rung(result_tag == 1):
                out(Bool("Output"))

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)

    def test_min_max_annotated_function_is_verifiable(self):
        """run_function output with min/max is not Intractable."""
        trigger = Bool("Trigger", external=True)
        level = Int("Level", min=0, max=3)

        def read_level() -> dict:
            return {"level": 2}

        with Program(strict=False) as logic:
            with rung(trigger):
                run_function(read_level, outs={"level": level})
            with rung(level == 2):
                out(Bool("High"))

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)


def test_to_ascii_target_domain_covers_every_byte():
    """A TXT is a whole byte.  copy(chr(200), DS, to_ascii) writes 200, so the
    inferred domain of the target must reach 255 and not stop at 127."""
    from pyrung.core import Block, Int, TagType
    from pyrung.core.analysis.prove.classify import _domain_from_copy_convert
    from pyrung.core.copy_converters import to_ascii
    from pyrung.core.instruction import CopyInstruction

    CH = Block("CH", TagType.CHAR, 1, 2)
    domain = _domain_from_copy_convert(CopyInstruction(CH[1], Int("Code"), convert=to_ascii))
    assert domain == tuple(range(256))
