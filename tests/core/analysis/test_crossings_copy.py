"""Crossings — copy, fill, and block-copy projected reverse.

Covers the plain copy/fill data-flow half (incl. the clamp-rail soundness fix),
the bijective ``to_ascii``/``to_binary`` conversions (exact), the conservative
``to_value``/``to_text`` fallthroughs, and aligned block-copy slots.
"""

from __future__ import annotations

from pyrung import Char, Dint, Int, Real, Word
from pyrung.core.analysis import crossings
from pyrung.core.analysis.crossings.copy import CopyCrossing
from pyrung.core.copy_converters import to_ascii, to_binary, to_text, to_value
from pyrung.core.crossing import Cmp, CrossingContext, Eq, eq_target
from pyrung.core.instruction.data_transfer import (
    BlockCopyInstruction,
    CopyInstruction,
    FillInstruction,
)
from pyrung.core.memory_block import Block
from pyrung.core.tag import TagType

_COPY = CopyCrossing()


def _ctx(*tags) -> CrossingContext:
    return CrossingContext(tags_by_name={t.name: t for t in tags})


def _only(result):
    (branch,) = result.branches
    return branch


# --- plain copy / fill --------------------------------------------------------


def test_copy_from_tag_is_exact_source_constraint() -> None:
    src, dest = Int("Src"), Int("Dest")
    r = _COPY.reverse(CopyInstruction(src, dest), None, eq_target("Dest", 7), _ctx(src, dest))
    assert _only(r) == (Eq("Src", frozenset({7})),)
    assert r.exact is True


def test_copy_from_readonly_constant_match_is_satisfied() -> None:
    src, dest = Int("K", readonly=True, default=5), Int("Dest")
    r = _COPY.reverse(CopyInstruction(src, dest), None, eq_target("Dest", 5), _ctx(src, dest))
    assert r.exact is True
    assert r.branches == ((),)  # satisfied: no input constraint needed


def test_copy_from_readonly_constant_mismatch_is_unsatisfiable() -> None:
    src, dest = Int("K", readonly=True, default=5), Int("Dest")
    r = _COPY.reverse(CopyInstruction(src, dest), None, eq_target("Dest", 9), _ctx(src, dest))
    assert _only(r) == (Eq("Dest", frozenset()),)


def test_copy_from_literal_match_and_mismatch() -> None:
    dest = Int("Dest")
    hit = _COPY.reverse(CopyInstruction(7, dest), None, eq_target("Dest", 7), _ctx(dest))
    assert hit.exact is True and hit.branches == ((),)
    miss = _COPY.reverse(CopyInstruction(7, dest), None, eq_target("Dest", 8), _ctx(dest))
    assert _only(miss) == (Eq("Dest", frozenset()),)


def test_copy_indirect_source_falls_through() -> None:
    blk = Block("DS", TagType.INT, 1, 5)
    ptr, dest = Int("Ptr"), Int("Dest")
    r = _COPY.reverse(CopyInstruction(blk[ptr], dest), None, eq_target("Dest", 3), _ctx(ptr, dest))
    assert r.fallthrough is True


def test_fill_from_tag_is_exact_source_constraint() -> None:
    blk = Block("DS", TagType.INT, 1, 5)
    src = Int("Src")
    r = _COPY.reverse(FillInstruction(src, blk.select(1, 3)), None, eq_target("DS2", 4), _ctx(src))
    assert _only(r) == (Eq("Src", frozenset({4})),)
    assert r.exact is True


# --- clamp-rail soundness (the bug the contract change fixes) ------------------


def test_wide_source_at_dest_max_rail_inverts_to_cmp_not_singleton() -> None:
    # DINT -> INT clamps; Dest==32767 means Src was *anything* >= 32767, not {32767}.
    src, dest = Dint("Wide"), Int("Dest")
    r = _COPY.reverse(CopyInstruction(src, dest), None, eq_target("Dest", 32767), _ctx(src, dest))
    assert _only(r) == (Cmp("Wide", ">=", 32767),)
    assert r.exact is True  # the *range* is necessary and sufficient


def test_wide_source_at_dest_min_rail_inverts_to_cmp() -> None:
    src, dest = Dint("Wide"), Int("Dest")
    r = _COPY.reverse(CopyInstruction(src, dest), None, eq_target("Dest", -32768), _ctx(src, dest))
    assert _only(r) == (Cmp("Wide", "<=", -32768),)


def test_wide_source_interior_value_is_still_exact_singleton() -> None:
    src, dest = Dint("Wide"), Int("Dest")
    r = _COPY.reverse(CopyInstruction(src, dest), None, eq_target("Dest", 100), _ctx(src, dest))
    assert _only(r) == (Eq("Wide", frozenset({100})),)
    assert r.exact is True


def test_same_width_source_at_rail_is_exact_singleton() -> None:
    # INT -> INT can't overflow, so the rail value is the only producer.
    src, dest = Int("Src"), Int("Dest")
    r = _COPY.reverse(CopyInstruction(src, dest), None, eq_target("Dest", 32767), _ctx(src, dest))
    assert _only(r) == (Eq("Src", frozenset({32767})),)


# --- affine expression source (the copy-side twin of calc's affine inverse) ---


def test_affine_expr_source_interior_inverts_exactly() -> None:
    # copy(Src + 100, Dest): Dest==250 (interior) <=> Src + 100 == 250 <=> Src == 150.
    src, dest = Int("Src"), Int("Dest")
    r = _COPY.reverse(
        CopyInstruction(src + 100, dest), None, eq_target("Dest", 250), _ctx(src, dest)
    )
    assert _only(r) == (Eq("Src", frozenset({150})),)
    assert r.exact is True  # clamp is the identity in the interior -> exact


def test_affine_expr_source_forward_is_affine() -> None:
    src, dest = Int("Src"), Int("Dest")
    fwd = _COPY.forward(CopyInstruction(src + 100, dest), _ctx(src, dest))
    assert (fwd.source, fwd.scale, fwd.offset) == ("Src", 1, 100)


def test_affine_expr_source_negated_partner() -> None:
    # copy(100 - Src, Dest): Dest==30 <=> 100 - Src == 30 <=> Src == 70.
    src, dest = Int("Src"), Int("Dest")
    r = _COPY.reverse(
        CopyInstruction(100 - src, dest), None, eq_target("Dest", 30), _ctx(src, dest)
    )
    assert _only(r) == (Eq("Src", frozenset({70})),)


def test_affine_expr_source_at_clamp_rail_punts() -> None:
    # At the INT rails many sources collapse to one value -> not a singleton -> punt.
    src, dest = Int("Src"), Int("Dest")
    hi = _COPY.reverse(
        CopyInstruction(src + 100, dest), None, eq_target("Dest", 32767), _ctx(src, dest)
    )
    lo = _COPY.reverse(
        CopyInstruction(src + 100, dest), None, eq_target("Dest", -32768), _ctx(src, dest)
    )
    assert hi.fallthrough is True
    assert lo.fallthrough is True


def test_affine_expr_source_multiply_non_divisible_punts() -> None:
    # copy(Src * 2, Dest): Dest==11 has no integer preimage -> defer.
    src, dest = Int("Src"), Int("Dest")
    odd = _COPY.reverse(
        CopyInstruction(src * 2, dest), None, eq_target("Dest", 11), _ctx(src, dest)
    )
    even = _COPY.reverse(
        CopyInstruction(src * 2, dest), None, eq_target("Dest", 10), _ctx(src, dest)
    )
    assert odd.fallthrough is True
    assert _only(even) == (Eq("Src", frozenset({5})),)


def test_two_tag_expr_source_punts() -> None:
    # A ± B over two mutable tags is not a single-tag affine map -> defer.
    a, b, dest = Int("A"), Int("B"), Int("Dest")
    r = _COPY.reverse(CopyInstruction(a + b, dest), None, eq_target("Dest", 5), _ctx(a, b, dest))
    assert r.fallthrough is True


def test_affine_expr_readonly_source_punts() -> None:
    # A constant source is not a steerable single-tag affine -> defer.
    k, dest = Int("K", readonly=True, default=3), Int("Dest")
    r = _COPY.reverse(CopyInstruction(k + 100, dest), None, eq_target("Dest", 103), _ctx(k, dest))
    assert r.fallthrough is True


def test_affine_expr_non_clamping_dest_punts() -> None:
    # A REAL destination does not saturate-clamp; the interior-exactness argument
    # (and rail reasoning) does not apply -> defer.
    src, dest = Int("Src"), Real("RD")
    r = _COPY.reverse(CopyInstruction(src + 100, dest), None, eq_target("RD", 250), _ctx(src, dest))
    assert r.fallthrough is True


# --- bijective conversions ----------------------------------------------------


def test_to_ascii_inverts_to_char_exactly() -> None:
    src, dest = Char("ModeChar"), Int("Code")
    instr = CopyInstruction(src, dest, convert=to_ascii)
    r = _COPY.reverse(instr, None, eq_target("Code", 53), _ctx(src, dest))  # ord('5') == 53
    assert _only(r) == (Eq("ModeChar", frozenset({"5"})),)
    assert r.exact is True


def test_to_ascii_out_of_range_is_unsatisfiable() -> None:
    src, dest = Char("ModeChar"), Int("Code")
    instr = CopyInstruction(src, dest, convert=to_ascii)
    r = _COPY.reverse(instr, None, eq_target("Code", 200), _ctx(src, dest))  # > 127
    assert _only(r) == (Eq("Code", frozenset()),)


def test_to_ascii_non_int_target_falls_through() -> None:
    src, dest = Char("ModeChar"), Int("Code")
    instr = CopyInstruction(src, dest, convert=to_ascii)
    assert _COPY.reverse(instr, None, eq_target("Code", "x"), _ctx(src, dest)).fallthrough is True


def test_to_binary_byte_ranged_source_inverts_to_code() -> None:
    src, dest = Word("Byte", min=0, max=255), Char("Out")
    instr = CopyInstruction(src, dest, convert=to_binary)
    r = _COPY.reverse(instr, None, eq_target("Out", "{"), _ctx(src, dest))  # ord('{') == 123
    assert _only(r) == (Eq("Byte", frozenset({123})),)
    assert r.exact is True


def test_to_binary_unbounded_source_falls_through() -> None:
    src, dest = Int("Wide"), Char("Out")  # no min/max -> & 0xFF aliases
    instr = CopyInstruction(src, dest, convert=to_binary)
    assert _COPY.reverse(instr, None, eq_target("Out", "{"), _ctx(src, dest)).fallthrough is True


def test_to_binary_non_ascii_target_is_unsatisfiable() -> None:
    src, dest = Word("Byte", min=0, max=255), Char("Out")
    instr = CopyInstruction(src, dest, convert=to_binary)
    r = _COPY.reverse(instr, None, eq_target("Out", "é"), _ctx(src, dest))  # ord 233 > 127
    assert _only(r) == (Eq("Out", frozenset()),)


def test_to_value_and_to_text_fall_through() -> None:
    src, dest = Char("C"), Int("Code")
    assert (
        _COPY.reverse(
            CopyInstruction(src, dest, convert=to_value),
            None,
            eq_target("Code", 5),
            _ctx(src, dest),
        ).fallthrough
        is True
    )
    src2, dest2 = Int("Num"), Char("Txt")
    assert (
        _COPY.reverse(
            CopyInstruction(src2, dest2, convert=to_text()),
            None,
            eq_target("Txt", "5"),
            _ctx(src2, dest2),
        ).fallthrough
        is True
    )


# --- block copy (element-wise per-slot inversion) -----------------------------


def test_block_copy_inverts_aligned_slot() -> None:
    src_blk = Block("DS", TagType.INT, 1, 5)
    dst_blk = Block("DD", TagType.INT, 1, 5)
    instr = BlockCopyInstruction(src_blk.select(1, 3), dst_blk.select(1, 3))
    # DD2 is the 2nd dest slot -> aligned source slot is DS2.
    r = crossings.reverse(instr, None, eq_target("DD2", 9), _ctx())
    assert _only(r) == (Eq("DS2", frozenset({9})),)
    assert r.exact is True


def test_block_copy_wide_source_slot_at_rail_inverts_to_cmp() -> None:
    src_blk = Block("DS", TagType.DINT, 1, 5)
    dst_blk = Block("DD", TagType.INT, 1, 5)
    instr = BlockCopyInstruction(src_blk.select(1, 3), dst_blk.select(1, 3))
    r = crossings.reverse(instr, None, eq_target("DD1", 32767), _ctx())
    assert _only(r) == (Cmp("DS1", ">=", 32767),)


def test_block_copy_converting_falls_through() -> None:
    src_blk = Block("DS", TagType.CHAR, 1, 5)
    dst_blk = Block("DD", TagType.INT, 1, 5)
    instr = BlockCopyInstruction(src_blk.select(1, 3), dst_blk.select(1, 3), convert=to_value)
    assert crossings.reverse(instr, None, eq_target("DD1", 1), _ctx()).fallthrough is True
