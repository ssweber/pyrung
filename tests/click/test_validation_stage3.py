"""Stage 3 tests for click portability validation."""

from __future__ import annotations

from pyrung.click import TagMap, c, dd, ds, sc, t, td, txt, x, y
from pyrung.click import validation as click_validation
from pyrung.click.validation import (
    CLK_BANK_NOT_WRITABLE,
    CLK_BANK_UNRESOLVED,
    CLK_BANK_WRONG_ROLE,
    CLK_COPY_BANK_INCOMPATIBLE,
    CLK_DRUM_TIME_PRESET_LITERAL_REQUIRED,
    CLK_EXPR_ONLY_IN_CALC,
    CLK_PACK_TEXT_BANK_INCOMPATIBLE,
    CLK_PROFILE_UNAVAILABLE,
    validate_click_program,
)
from pyrung.core import Bool, Dint, Int, as_value
from pyrung.core.program import (
    Program,
    Rung,
    copy,
    event_drum,
    forloop,
    on_delay,
    out,
    pack_text,
    time_drum,
)


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


def test_drum_stage3_valid_mapping_passes_without_role_or_literal_findings():
    enable = Bool("Enable")
    reset = Bool("Reset")
    jump = Bool("Jump")
    jog = Bool("Jog")
    step = Int("Step")
    acc = Int("Acc")
    done = Bool("Done")
    out1 = Bool("Out1")
    out2 = Bool("Out2")
    event1 = Bool("Event1")
    event2 = Bool("Event2")

    def logic():
        with Rung(enable):
            event_drum(
                outputs=[out1, out2],
                events=[event1, event2],
                pattern=[[1, 0], [0, 1]],
                current_step=step,
                completion_flag=done,
            ).reset(reset).jump(jump, step=step).jog(jog)
        with Rung(enable):
            time_drum(
                outputs=[out1, out2],
                presets=[100, 200],
                pattern=[[1, 0], [0, 1]],
                current_step=step,
                accumulator=acc,
                completion_flag=done,
            ).reset(reset).jump(jump, step=step).jog(jog)

    prog = _build_program(logic)
    tag_map = TagMap(
        [
            enable.map_to(x[1]),
            reset.map_to(x[2]),
            jump.map_to(x[3]),
            jog.map_to(x[4]),
            out1.map_to(y[1]),
            out2.map_to(c[1]),
            event1.map_to(x[5]),
            event2.map_to(sc[50]),
            step.map_to(ds[1]),
            acc.map_to(td[1]),
            done.map_to(c[2]),
        ],
        include_system=False,
    )

    report = validate_click_program(prog, tag_map, mode="warn")
    codes = _codes(report)
    assert CLK_BANK_WRONG_ROLE not in codes
    assert CLK_DRUM_TIME_PRESET_LITERAL_REQUIRED not in codes


def test_drum_stage3_wrong_role_failures_are_reported():
    enable = Bool("Enable")
    reset = Bool("Reset")
    step = Dint("Step")
    acc = Int("Acc")
    done = Bool("Done")
    out1 = Bool("Out1")

    def logic():
        with Rung(enable):
            event_drum(
                outputs=[out1],
                events=[step > 0],
                pattern=[[1]],
                current_step=step,
                completion_flag=done,
            ).reset(reset)
        with Rung(enable):
            time_drum(
                outputs=[out1],
                presets=[100],
                pattern=[[1]],
                current_step=step,
                accumulator=acc,
                completion_flag=done,
            ).reset(reset)

    prog = _build_program(logic)
    tag_map = TagMap(
        [
            enable.map_to(x[1]),
            reset.map_to(x[2]),
            out1.map_to(x[3]),  # invalid drum output role
            step.map_to(dd[1]),  # invalid current_step role
            acc.map_to(ds[3]),  # invalid accumulator role
            done.map_to(y[1]),  # invalid completion role
        ],
        include_system=False,
    )

    report = validate_click_program(prog, tag_map, mode="warn")
    assert CLK_BANK_WRONG_ROLE in _codes(report)


def test_time_drum_non_literal_preset_reports_literal_required():
    enable = Bool("Enable")
    reset = Bool("Reset")
    step = Int("Step")
    acc = Int("Acc")
    done = Bool("Done")
    out1 = Bool("Out1")
    preset_tag = Int("PresetTag")

    def logic():
        with Rung(enable):
            time_drum(
                outputs=[out1],
                presets=[preset_tag],
                pattern=[[1]],
                current_step=step,
                accumulator=acc,
                completion_flag=done,
            ).reset(reset)

    prog = _build_program(logic)
    tag_map = TagMap(
        [
            enable.map_to(x[1]),
            reset.map_to(x[2]),
            out1.map_to(y[1]),
            step.map_to(ds[1]),
            acc.map_to(td[1]),
            done.map_to(c[1]),
            preset_tag.map_to(ds[2]),
        ],
        include_system=False,
    )

    report = validate_click_program(prog, tag_map, mode="warn")
    assert CLK_DRUM_TIME_PRESET_LITERAL_REQUIRED in _codes(report)
