"""CMP_EQ_ON_COMPUTED_REAL: == / != against a REAL the ladder computes."""

from __future__ import annotations

from pyrung.core import Bool, Int, Program, Real, Rung, calc, copy
from pyrung.core.validation.report import validate

CODE = "CMP_EQ_ON_COMPUTED_REAL"


def _hits(prog: Program):
    return [f for f in validate(prog, select={"CMP"}) if f.code == CODE]


def _prog(build) -> Program:
    en = Bool("En", external=True)
    out = Bool("Out", external=True)
    with Program(strict=False) as prog:
        build(en, out)
    return prog


def test_calc_written_real_eq_is_flagged():
    scaled = Real("Scaled")
    raw = Real("Raw", external=True)

    def build(en, out):
        with Rung(en):
            calc(raw * 0.1, scaled)
        with Rung(scaled == 2.5):
            copy(1, out)

    (hit,) = _hits(_prog(build))
    assert hit.severity == "advisory"
    text = hit.message
    assert text.isascii()
    assert "range" in text and "tolerance" in text


def test_ne_and_reversed_operands_flagged():
    scaled = Real("Scaled")
    raw = Real("Raw", external=True)

    def build(en, out):
        with Rung(en):
            calc(raw * 0.1, scaled)
        with Rung(scaled != 2.5):
            copy(1, out)
        with Rung(Real("Target", external=True) == scaled):
            copy(1, out)

    assert len(_hits(_prog(build))) == 2


def test_copy_of_calc_real_flagged():
    scaled = Real("Scaled")
    held = Real("Held")
    raw = Real("Raw", external=True)

    def build(en, out):
        with Rung(en):
            calc(raw / 3, scaled)
            copy(scaled, held)
        with Rung(held == 2.5):
            copy(1, out)

    assert len(_hits(_prog(build))) == 1


def test_inline_computed_real_flagged():
    raw = Real("Raw", external=True)

    def build(en, out):
        with Rung((raw * 0.1) == 2.5):
            copy(1, out)

    assert len(_hits(_prog(build))) == 1


def test_unwritten_real_exempt():
    raw = Real("Raw", external=True)
    setpoint = Real("Setpoint")

    def build(en, out):
        with Rung(raw == 2.5, setpoint != 1.5):
            copy(1, out)

    assert not _hits(_prog(build))


def test_real_copied_from_constant_or_int_exempt():
    level = Real("Level")
    counts = Int("Counts", external=True)
    held = Real("Held")

    def build(en, out):
        with Rung(en):
            copy(2.5, level)
            copy(counts, held)
        with Rung(level == 2.5, held == 3.0):
            copy(1, out)

    assert not _hits(_prog(build))


def test_compare_with_zero_exempt():
    scaled = Real("Scaled")
    raw = Real("Raw", external=True)

    def build(en, out):
        with Rung(en):
            calc(raw * 0.1, scaled)
        with Rung(scaled == 0):
            copy(1, out)
        with Rung(scaled != 0):
            copy(0, out)

    assert not _hits(_prog(build))


def test_ordered_compares_exempt():
    scaled = Real("Scaled")
    raw = Real("Raw", external=True)

    def build(en, out):
        with Rung(en):
            calc(raw * 0.1, scaled)
        with Rung(scaled >= 2.5, scaled < 9.0):
            copy(1, out)

    assert not _hits(_prog(build))


def test_integer_calc_into_real_exempt():
    total = Real("Total")
    a = Int("A", external=True)

    def build(en, out):
        with Rung(en):
            calc(a + 1, total)
        with Rung(total == 5):
            copy(1, out)

    assert not _hits(_prog(build))


def test_integer_tag_eq_exempt():
    n = Int("N")
    a = Int("A", external=True)

    def build(en, out):
        with Rung(en):
            calc(a + 1, n)
        with Rung(n == 5):
            copy(1, out)

    assert not _hits(_prog(build))


def test_copy_of_float_expression_flagged():
    scaled = Real("Scaled")
    raw = Real("Raw", external=True)

    def build(en, out):
        with Rung(en):
            copy(raw * 0.1, scaled)
        with Rung(scaled == 2.5):
            copy(1, out)

    assert len(_hits(_prog(build))) == 1


def test_zero_on_left_exempt():
    scaled = Real("Scaled")
    raw = Real("Raw", external=True)

    # Python reflects `0 == tag` to `tag == 0`, so build the compare both ways.
    def build_zero(en, out):
        with Rung(en):
            calc(raw * 0.1, scaled)
        with Rung(0 == scaled):
            copy(1, out)
        with Rung(0.0 != scaled):
            copy(0, out)

    assert not _hits(_prog(build_zero))


def test_block_indexed_real_does_not_crash():
    from pyrung.core import Block, TagType

    blk = Block("Blk", TagType.REAL, 1, 10)
    idx = Int("Idx")
    raw = Real("Raw", external=True)

    def build(en, out):
        with Rung(en):
            copy(raw, blk[idx])
        with Rung(blk[idx] == 2.5):
            copy(1, out)

    assert not _hits(_prog(build))
