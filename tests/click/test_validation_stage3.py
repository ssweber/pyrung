"""Stage 3 tests for click portability validation."""

from __future__ import annotations

from pyrung.click import TagMap, c, ds, sc, t, td, txt, x, y
from pyrung.click import validation as click_validation
from pyrung.click.validation import (
    CLK_BANK_NOT_WRITABLE,
    CLK_BANK_UNRESOLVED,
    CLK_BANK_WRONG_ROLE,
    CLK_COPY_BANK_INCOMPATIBLE,
    CLK_EXPR_ONLY_IN_CALC,
    CLK_PACK_TEXT_BANK_INCOMPATIBLE,
    CLK_PROFILE_UNAVAILABLE,
    validate_click_program,
)
from pyrung.core import Bool, Int, as_value
from pyrung.core.program import Program, Rung, copy, forloop, on_delay, out, pack_text


def _build_program(fn):
    prog = Program(strict=False)
    with prog:
        fn()
    return prog


def _codes(report) -> list[str]:
    return [f.code for f in (*report.errors, *report.warnings, *report.hints)]


def test_r6_non_writable_target_bank():
    target = Bool("Target")

    def logic():
        with Rung():
            out(target)

    prog = _build_program(logic)
    tag_map = TagMap([target.map_to(x[1])], include_system=False)

    report = validate_click_program(prog, tag_map, mode="warn")
    assert CLK_BANK_NOT_WRITABLE in _codes(report)


def test_r6_sc_subset_address_aware():
    ok = Bool("Ok")
    bad = Bool("Bad")

    def logic():
        with Rung():
            out(ok)
            out(bad)

    prog = _build_program(logic)
    tag_map = TagMap([ok.map_to(sc[50]), bad.map_to(sc[1])], include_system=False)

    report = validate_click_program(prog, tag_map, mode="warn")
    assert CLK_BANK_NOT_WRITABLE in _codes(report)


def test_r7_timer_role_validation():
    done_ok = Bool("DoneOk")
    acc_ok = Int("AccOk")
    done_bad = Bool("DoneBad")
    acc_bad = Int("AccBad")

    def logic():
        with Rung():
            on_delay(done_ok, acc_ok, preset=10)
            on_delay(done_bad, acc_bad, preset=10)

    prog = _build_program(logic)
    tag_map = TagMap(
        [
            done_ok.map_to(t[1]),
            acc_ok.map_to(td[1]),
            done_bad.map_to(c[1]),
            acc_bad.map_to(td[2]),
        ],
        include_system=False,
    )

    report = validate_click_program(prog, tag_map, mode="warn")
    assert CLK_BANK_WRONG_ROLE in _codes(report)


def test_r8_copy_family_incompatible_pair():
    source = Bool("Source")
    dest = Int("Dest")

    def logic():
        with Rung():
            copy(source, dest)

    prog = _build_program(logic)
    tag_map = TagMap([source.map_to(x[1]), dest.map_to(ds[1])], include_system=False)

    report = validate_click_program(prog, tag_map, mode="warn")
    assert CLK_COPY_BANK_INCOMPATIBLE in _codes(report)


def test_r8_copy_family_compatible_pair():
    source = Bool("Source")
    dest = Bool("Dest")

    def logic():
        with Rung():
            copy(source, dest)

    prog = _build_program(logic)
    tag_map = TagMap([source.map_to(x[1]), dest.map_to(y[1])], include_system=False)

    report = validate_click_program(prog, tag_map, mode="warn")
    assert CLK_COPY_BANK_INCOMPATIBLE not in _codes(report)


def test_stage3_recurses_into_forloop_children_for_r6():
    target = Bool("Target")

    def logic():
        with Rung():
            with forloop(2):
                out(target)

    prog = _build_program(logic)
    tag_map = TagMap([target.map_to(x[1])], include_system=False)

    report = validate_click_program(prog, tag_map, mode="warn")
    assert CLK_BANK_NOT_WRITABLE in _codes(report)


def test_stage3_recurses_into_forloop_children_for_r8():
    source = Bool("Source")
    dest = Int("Dest")

    def logic():
        with Rung():
            with forloop(2):
                copy(source, dest)

    prog = _build_program(logic)
    tag_map = TagMap([source.map_to(x[1]), dest.map_to(ds[1])], include_system=False)

    report = validate_click_program(prog, tag_map, mode="warn")
    assert CLK_COPY_BANK_INCOMPATIBLE in _codes(report)


def test_unmapped_target_emits_bank_unresolved():
    target = Bool("Target")

    def logic():
        with Rung():
            out(target)

    prog = _build_program(logic)
    tag_map = TagMap(include_system=False)

    report = validate_click_program(prog, tag_map, mode="warn")
    assert CLK_BANK_UNRESOLVED in _codes(report)


def test_profile_unavailable_still_reports_stage2_findings(monkeypatch):
    a = Int("A")
    dest = Int("Dest")

    def logic():
        with Rung():
            copy(a * 2, dest)

    prog = _build_program(logic)
    tag_map = TagMap([dest.map_to(ds[1])], include_system=False)

    monkeypatch.setattr(click_validation, "_load_default_profile", lambda: None)
    report = validate_click_program(prog, tag_map, mode="warn")

    assert CLK_PROFILE_UNAVAILABLE in _codes(report)
    assert CLK_EXPR_ONLY_IN_CALC in _codes(report)


def test_program_and_tagmap_validation_facades_match_direct():
    target = Bool("Target")

    def logic():
        with Rung():
            out(target)

    prog = _build_program(logic)
    tag_map = TagMap([target.map_to(y[1])], include_system=False)

    direct = validate_click_program(prog, tag_map, mode="warn")
    via_program = prog.validate("click", mode="warn", tag_map=tag_map)
    via_tag_map = tag_map.validate(prog, mode="warn")

    assert _codes(direct) == _codes(via_program)
    assert _codes(direct) == _codes(via_tag_map)


def test_pack_text_stage3_compatible_banks():
    source = txt.select(1, 3)
    dest = Int("Dest")

    def logic():
        with Rung():
            pack_text(source, dest)

    prog = _build_program(logic)
    tag_map = TagMap([dest.map_to(ds[1])], include_system=False)

    report = validate_click_program(prog, tag_map, mode="warn")
    assert CLK_PACK_TEXT_BANK_INCOMPATIBLE not in _codes(report)


def test_pack_text_stage3_incompatible_source_bank():
    dest = Int("Dest")

    def logic():
        with Rung():
            pack_text(ds.select(1, 2), dest)

    prog = _build_program(logic)
    tag_map = TagMap([dest.map_to(ds[2])], include_system=False)

    report = validate_click_program(prog, tag_map, mode="warn")
    assert CLK_PACK_TEXT_BANK_INCOMPATIBLE in _codes(report)


def test_wrapped_copy_source_keeps_copy_context_rules():
    pointer = Int("Pointer")
    dest = Int("Dest")

    def logic():
        with Rung():
            copy(as_value(txt[pointer]), dest)

    prog = _build_program(logic)
    tag_map = TagMap([pointer.map_to(ds[100]), dest.map_to(ds[1])], include_system=False)

    report = validate_click_program(prog, tag_map, mode="warn")
    codes = _codes(report)
    assert "CLK_PTR_CONTEXT_ONLY_COPY" not in codes
