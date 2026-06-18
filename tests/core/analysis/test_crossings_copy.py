"""Crossings — CopyCrossing + the copy-source dedup helper.

Covers the plain copy/fill data-flow half (incl. the clamp-rail soundness fix),
the bijective ``to_ascii``/``to_binary`` conversions (exact), the conservative
``to_value``/``to_text`` fallthroughs, the block-copy fallthrough, and the
neutral ``copy_source_binding`` helper that both the walker and
``projected_cause`` share (until Step 4 routes them through the registry).
"""

from __future__ import annotations

from types import SimpleNamespace

from pyrung import Char, Dint, Int, Word
from pyrung.core.analysis import crossings
from pyrung.core.analysis.crossings.copy import CopyCrossing
from pyrung.core.analysis.sp_values import copy_source_binding
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


# --- block copy ---------------------------------------------------------------


def test_block_copy_falls_through() -> None:
    src_blk = Block("DS", TagType.INT, 1, 5)
    dst_blk = Block("DD", TagType.INT, 1, 5)
    instr = BlockCopyInstruction(src_blk.select(1, 3), dst_blk.select(1, 3))
    assert crossings.reverse(instr, None, eq_target("DD1", 1), _ctx()).fallthrough is True


# --- copy_source_binding (the dedup helper, until Step 4 routes via registry) --


def _rung(*instructions):
    return SimpleNamespace(_instructions=list(instructions))


def test_binding_names_distinct_copy_source() -> None:
    src, dest = Int("Src"), Int("Dest")
    rung = _rung(CopyInstruction(src, dest))
    assert copy_source_binding(rung, "Dest", 7) == ("Src", 7)


def test_binding_none_for_literal_source() -> None:
    dest = Int("Dest")
    assert copy_source_binding(_rung(CopyInstruction(7, dest)), "Dest", 7) is None


def test_binding_none_for_self_copy() -> None:
    dest = Int("Dest")
    assert copy_source_binding(_rung(CopyInstruction(dest, dest)), "Dest", 7) is None


def test_binding_none_for_indirect_source() -> None:
    blk = Block("DS", TagType.INT, 1, 5)
    ptr, dest = Int("Ptr"), Int("Dest")
    assert copy_source_binding(_rung(CopyInstruction(blk[ptr], dest)), "Dest", 7) is None
