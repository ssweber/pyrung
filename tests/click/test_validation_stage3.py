"""Stage 3 tests for click portability validation."""

from __future__ import annotations

import re

import pytest

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()
from pyrung.click import validation as click_validation
from pyrung.click.ladder.types import LadderExportError
from pyrung.click.validation import (
    CLK_BANK_NOT_WRITABLE,
    CLK_BANK_UNRESOLVED,
    CLK_BANK_WRONG_ROLE,
    CLK_COPY_BANK_INCOMPATIBLE,
    CLK_COPY_CONVERTER_INCOMPATIBLE,
    CLK_DRUM_JUMP_STEP_TAG_REQUIRED,
    CLK_EXPR_ONLY_IN_CALC,
    CLK_PACK_TEXT_BANK_INCOMPATIBLE,
    CLK_PROFILE_UNAVAILABLE,
    validate_click_program,
)
from pyrung.click.validation import resolve as click_resolve
from pyrung.core import Bool, Char, Dint, Int, Timer, to_ascii, to_binary, to_text, to_value
from pyrung.core.program import (
    Program,
    Rung,
    blockcopy,
    copy,
    event_drum,
    forloop,
    latch,
    on_delay,
    out,
    pack_text,
    reset,
    time_drum,
)

Timer2 = Timer.clone("Timer2")


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
    def logic():
        with Rung():
            on_delay(Timer[1], preset=10)
            on_delay(Timer2, preset=10)

    prog = _build_program(logic)
    tag_map = TagMap(
        [
            Timer[1].Done.map_to(t[1]),
            Timer[1].Acc.map_to(td[1]),
            Timer2.Done.map_to(c[1]),
            Timer2.Acc.map_to(td[2]),
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


def test_r8_blockcopy_reports_each_incompatible_bank_pair_once():
    def logic():
        with Rung():
            blockcopy(x.select(1, 16), ds.select(1, 16))

    prog = _build_program(logic)
    report = validate_click_program(prog, TagMap(include_system=False), mode="warn")
    findings = [
        finding
        for finding in (*report.errors, *report.warnings, *report.hints)
        if finding.code == CLK_COPY_BANK_INCOMPATIBLE
    ]

    assert len(findings) == 1


def test_validation_reuses_operand_resolutions_within_each_invocation(monkeypatch):
    direct_resolution_calls = 0
    original_resolve_direct_tag = click_resolve._resolve_direct_tag

    def count_direct_resolution(tag, tag_map):
        nonlocal direct_resolution_calls
        direct_resolution_calls += 1
        return original_resolve_direct_tag(tag, tag_map)

    monkeypatch.setattr(click_resolve, "_resolve_direct_tag", count_direct_resolution)

    def logic():
        with Rung():
            blockcopy(ds.select(1, 16), dd.select(1, 16))

    prog = _build_program(logic)
    tag_map = TagMap(include_system=False)

    validate_click_program(prog, tag_map, mode="warn")
    validate_click_program(prog, tag_map, mode="warn")

    assert direct_resolution_calls == 64


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
    findings = [
        finding
        for finding in (*report.errors, *report.warnings, *report.hints)
        if finding.code == CLK_PACK_TEXT_BANK_INCOMPATIBLE
    ]
    assert len(findings) == 1


def test_wrapped_copy_source_keeps_copy_context_rules():
    pointer = Int("Pointer")
    dest = Int("Dest")

    def logic():
        with Rung():
            copy(txt[pointer], dest, convert=to_value)

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
    assert CLK_DRUM_JUMP_STEP_TAG_REQUIRED not in codes


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
    wrong_role_locations = [
        finding.location
        for finding in (*report.errors, *report.warnings, *report.hints)
        if finding.code == CLK_BANK_WRONG_ROLE
    ]
    assert len(wrong_role_locations) == len(set(wrong_role_locations))


def test_time_drum_register_preset_validates_and_exports():
    """CLICK v4 takes register durations; version checks belong to whoever pastes into CLICK."""
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

    report = validate_click_program(prog, tag_map, mode="strict")
    assert not (*report.errors, *report.warnings, *report.hints)
    tokens = [row[-1] for row in pyrung_to_ladder(prog, tag_map).main_rows]
    assert any("presets=[DS2]" in token for token in tokens)


def test_event_drum_literal_jump_step_reports_tag_required():
    enable = Bool("Enable")
    reset = Bool("Reset")
    jump = Bool("Jump")
    step = Int("Step")
    done = Bool("Done")
    out1 = Bool("Out1")
    event1 = Bool("Event1")

    def logic():
        with Rung(enable):
            event_drum(
                outputs=[out1],
                events=[event1],
                pattern=[[1]],
                current_step=step,
                completion_flag=done,
            ).reset(reset).jump(jump, step=3)

    prog = _build_program(logic)
    tag_map = TagMap(
        [
            enable.map_to(x[1]),
            reset.map_to(x[2]),
            jump.map_to(x[3]),
            out1.map_to(y[1]),
            event1.map_to(x[4]),
            step.map_to(ds[1]),
            done.map_to(c[1]),
        ],
        include_system=False,
    )

    report = validate_click_program(prog, tag_map, mode="warn")
    assert CLK_DRUM_JUMP_STEP_TAG_REQUIRED in _codes(report)


def test_time_drum_literal_jump_step_reports_tag_required():
    enable = Bool("Enable")
    reset = Bool("Reset")
    jump = Bool("Jump")
    step = Int("Step")
    acc = Int("Acc")
    done = Bool("Done")
    out1 = Bool("Out1")

    def logic():
        with Rung(enable):
            time_drum(
                outputs=[out1],
                presets=[100],
                pattern=[[1]],
                current_step=step,
                accumulator=acc,
                completion_flag=done,
            ).reset(reset).jump(jump, step=2)

    prog = _build_program(logic)
    tag_map = TagMap(
        [
            enable.map_to(x[1]),
            reset.map_to(x[2]),
            jump.map_to(x[3]),
            out1.map_to(y[1]),
            step.map_to(ds[1]),
            acc.map_to(td[1]),
            done.map_to(c[1]),
        ],
        include_system=False,
    )

    report = validate_click_program(prog, tag_map, mode="warn")
    assert CLK_DRUM_JUMP_STEP_TAG_REQUIRED in _codes(report)


# ---------------------------------------------------------------------------
# Converter / bank compatibility
# ---------------------------------------------------------------------------


class TestConverterBankCompatibility:
    """Converter mode must match source/dest bank data types."""

    def test_to_text_valid_numeric_source_txt_dest(self):
        source = Int("Source")
        dest = Char("Dest")

        def logic():
            with Rung():
                copy(source, dest, convert=to_text())

        prog = _build_program(logic)
        tag_map = TagMap([source.map_to(ds[1]), dest.map_to(txt[1])], include_system=False)
        report = validate_click_program(prog, tag_map, mode="warn")
        assert CLK_COPY_CONVERTER_INCOMPATIBLE not in _codes(report)

    def test_to_text_rejects_txt_source(self):
        source = Char("Source")
        dest = Char("Dest")

        def logic():
            with Rung():
                copy(source, dest, convert=to_text())

        prog = _build_program(logic)
        tag_map = TagMap([source.map_to(txt[1]), dest.map_to(txt[2])], include_system=False)
        report = validate_click_program(prog, tag_map, mode="warn")
        assert CLK_COPY_CONVERTER_INCOMPATIBLE in _codes(report)

    def test_to_text_rejects_numeric_dest(self):
        source = Int("Source")
        dest = Int("Dest")

        def logic():
            with Rung():
                copy(source, dest, convert=to_text())

        prog = _build_program(logic)
        tag_map = TagMap([source.map_to(ds[1]), dest.map_to(ds[2])], include_system=False)
        report = validate_click_program(prog, tag_map, mode="warn")
        assert CLK_COPY_CONVERTER_INCOMPATIBLE in _codes(report)

    def test_to_binary_valid(self):
        source = Int("Source")
        dest = Char("Dest")

        def logic():
            with Rung():
                copy(source, dest, convert=to_binary)

        prog = _build_program(logic)
        tag_map = TagMap([source.map_to(ds[1]), dest.map_to(txt[1])], include_system=False)
        report = validate_click_program(prog, tag_map, mode="warn")
        assert CLK_COPY_CONVERTER_INCOMPATIBLE not in _codes(report)

    def test_to_binary_rejects_txt_source(self):
        source = Char("Source")
        dest = Char("Dest")

        def logic():
            with Rung():
                copy(source, dest, convert=to_binary)

        prog = _build_program(logic)
        tag_map = TagMap([source.map_to(txt[1]), dest.map_to(txt[2])], include_system=False)
        report = validate_click_program(prog, tag_map, mode="warn")
        assert CLK_COPY_CONVERTER_INCOMPATIBLE in _codes(report)

    def test_to_value_valid_txt_source_numeric_dest(self):
        source = Char("Source")
        dest = Int("Dest")

        def logic():
            with Rung():
                copy(source, dest, convert=to_value)

        prog = _build_program(logic)
        tag_map = TagMap([source.map_to(txt[1]), dest.map_to(ds[1])], include_system=False)
        report = validate_click_program(prog, tag_map, mode="warn")
        assert CLK_COPY_CONVERTER_INCOMPATIBLE not in _codes(report)

    def test_to_value_rejects_numeric_source(self):
        source = Int("Source")
        dest = Int("Dest")

        def logic():
            with Rung():
                copy(source, dest, convert=to_value)

        prog = _build_program(logic)
        tag_map = TagMap([source.map_to(ds[1]), dest.map_to(ds[2])], include_system=False)
        report = validate_click_program(prog, tag_map, mode="warn")
        assert CLK_COPY_CONVERTER_INCOMPATIBLE in _codes(report)

    def test_to_value_rejects_txt_dest(self):
        source = Char("Source")
        dest = Char("Dest")

        def logic():
            with Rung():
                copy(source, dest, convert=to_value)

        prog = _build_program(logic)
        tag_map = TagMap([source.map_to(txt[1]), dest.map_to(txt[2])], include_system=False)
        report = validate_click_program(prog, tag_map, mode="warn")
        assert CLK_COPY_CONVERTER_INCOMPATIBLE in _codes(report)

    def test_to_ascii_valid(self):
        source = Char("Source")
        dest = Int("Dest")

        def logic():
            with Rung():
                copy(source, dest, convert=to_ascii)

        prog = _build_program(logic)
        tag_map = TagMap([source.map_to(txt[1]), dest.map_to(ds[1])], include_system=False)
        report = validate_click_program(prog, tag_map, mode="warn")
        assert CLK_COPY_CONVERTER_INCOMPATIBLE not in _codes(report)

    def test_to_ascii_rejects_numeric_source(self):
        source = Int("Source")
        dest = Int("Dest")

        def logic():
            with Rung():
                copy(source, dest, convert=to_ascii)

        prog = _build_program(logic)
        tag_map = TagMap([source.map_to(ds[1]), dest.map_to(ds[2])], include_system=False)
        report = validate_click_program(prog, tag_map, mode="warn")
        assert CLK_COPY_CONVERTER_INCOMPATIBLE in _codes(report)

    def test_copy_without_converter_no_converter_finding(self):
        """Plain copy (no convert=) should not emit converter findings."""
        source = Int("Source")
        dest = Int("Dest")

        def logic():
            with Rung():
                copy(source, dest)

        prog = _build_program(logic)
        tag_map = TagMap([source.map_to(ds[1]), dest.map_to(ds[2])], include_system=False)
        report = validate_click_program(prog, tag_map, mode="warn")
        assert CLK_COPY_CONVERTER_INCOMPATIBLE not in _codes(report)

    def test_blockcopy_to_value_valid(self):
        dest = Int("Dest")

        def logic():
            with Rung():
                blockcopy(txt.select(1, 3), dest, convert=to_value)

        prog = _build_program(logic)
        tag_map = TagMap([dest.map_to(ds[1])], include_system=False)
        report = validate_click_program(prog, tag_map, mode="warn")
        assert CLK_COPY_CONVERTER_INCOMPATIBLE not in _codes(report)

    def test_blockcopy_to_value_rejects_numeric_source(self):
        dest = Int("Dest")

        def logic():
            with Rung():
                blockcopy(ds.select(1, 3), dest, convert=to_value)

        prog = _build_program(logic)
        tag_map = TagMap([dest.map_to(ds[10])], include_system=False)
        report = validate_click_program(prog, tag_map, mode="warn")
        findings = [
            finding
            for finding in (*report.errors, *report.warnings, *report.hints)
            if finding.code == CLK_COPY_CONVERTER_INCOMPATIBLE
        ]
        assert len(findings) == 1

    def test_converter_finding_includes_suggestion(self):
        source = Int("Source")
        dest = Int("Dest")

        def logic():
            with Rung():
                copy(source, dest, convert=to_value)

        prog = _build_program(logic)
        tag_map = TagMap([source.map_to(ds[1]), dest.map_to(ds[2])], include_system=False)
        report = validate_click_program(prog, tag_map, mode="warn")
        converter_findings = [
            f
            for f in (*report.errors, *report.warnings, *report.hints)
            if f.code == CLK_COPY_CONVERTER_INCOMPATIBLE
        ]
        assert converter_findings
        assert all(f.suggestion for f in converter_findings)

    def test_strict_mode_emits_error(self):
        source = Int("Source")
        dest = Int("Dest")

        def logic():
            with Rung():
                copy(source, dest, convert=to_value)

        prog = _build_program(logic)
        tag_map = TagMap([source.map_to(ds[1]), dest.map_to(ds[2])], include_system=False)
        report = validate_click_program(prog, tag_map, mode="strict")
        assert any(
            f.code == CLK_COPY_CONVERTER_INCOMPATIBLE and f.severity == "error"
            for f in report.errors
        )


class TestOutVariants:
    """Each phase of a debounce/flasher/timed coil claims a whole Timer (T + TD),
    a register preset must be DS, and the units follow CLICK v4's Out dialog."""

    Timers = Timer.clone("OutTimers", count=4)

    def _map(self, *, bad_acc: bool = False, extra=()) -> TagMap:
        timers = self.Timers
        return TagMap(
            [
                Bool("Coil").map_to(y[1]),
                Bool("Lamp").map_to(c[1]),
                timers._blocks["Done"].map_to(t.select(1, 4)),
                timers._blocks["Acc"].map_to((ds if bad_acc else td).select(1, 4)),
                *extra,
            ],
            include_system=False,
        )

    def _report(self, body, tag_map: TagMap, mode="strict"):
        return validate_click_program(_build_program(body), tag_map, mode=mode)

    def test_valid_mapping_passes(self) -> None:
        timers = self.Timers
        preset = Int("OnTime")

        def body():
            with Rung(Bool("In")):
                from pyrung import debounce, flasher, timed, toggle

                toggle(Bool("Lamp"))
                debounce(Bool("Coil"), on=(timers[1], preset), off=(timers[2], 10))
                flasher(Bool("Coil"), on=(timers[3], 10), off=(timers[4], 10), unit="sec")
                timed(Bool("Coil"), timers[1], 3, "min")

        tag_map = self._map(extra=[preset.map_to(ds[1]), Bool("In").map_to(x[1])])
        assert _codes(self._report(body, tag_map)) == []

    def test_timer_accumulator_must_be_td(self) -> None:
        timers = self.Timers

        def body():
            with Rung(Bool("In")):
                from pyrung import debounce

                debounce(Bool("Coil"), on=(timers[1], 10), off=(timers[2], 10))

        tag_map = self._map(bad_acc=True, extra=[Bool("In").map_to(x[1])])
        assert CLK_BANK_WRONG_ROLE in _codes(self._report(body, tag_map))

    def test_preset_register_must_be_ds(self) -> None:
        timers = self.Timers
        preset = Dint("OnTime")

        def body():
            with Rung(Bool("In")):
                from pyrung import timed

                timed(Bool("Coil"), timers[1], preset)

        tag_map = self._map(extra=[preset.map_to(dd[1]), Bool("In").map_to(x[1])])
        assert CLK_BANK_WRONG_ROLE in _codes(self._report(body, tag_map))

    def test_units_follow_the_out_dialog(self) -> None:
        from pyrung.click.validation import CLK_OUT_UNIT_UNSUPPORTED

        timers = self.Timers

        def body():
            with Rung(Bool("In")):
                from pyrung import debounce, flasher, timed

                debounce(Bool("Coil"), on=(timers[1], 1), off=(timers[2], 1), unit="min")
                flasher(Bool("Coil"), on=(timers[3], 1), off=(timers[4], 1), unit="hour")
                timed(Bool("Coil"), timers[1], 1, "day")

        tag_map = self._map(extra=[Bool("In").map_to(x[1])])
        report = self._report(body, tag_map)
        assert _codes(report).count(CLK_OUT_UNIT_UNSUPPORTED) == 3
        messages = [f.message for f in report.errors if f.code == CLK_OUT_UNIT_UNSUPPORTED]
        assert "debounce coil times in ms/sec, not min" in messages[0]
        assert "timed coil times in ms/sec/min, not day" in messages[2]

    def test_preset_overflow(self) -> None:
        from pyrung.click.validation import CLK_TIMER_PRESET_OVERFLOW

        timers = self.Timers

        def body():
            with Rung(Bool("In")):
                from pyrung import debounce

                debounce(Bool("Coil"), on=(timers[1], 40_000), off=(timers[2], 50_000))

        tag_map = self._map(extra=[Bool("In").map_to(x[1])])
        assert _codes(self._report(body, tag_map)).count(CLK_TIMER_PRESET_OVERFLOW) == 2

    def test_immediate_toggle_needs_y(self) -> None:
        from pyrung import immediate, toggle
        from pyrung.click.validation import CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y

        def body():
            with Rung(Bool("In")):
                toggle(immediate(Bool("Lamp")))

        tag_map = self._map(extra=[Bool("In").map_to(x[1])])
        assert CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y in _codes(self._report(body, tag_map))


def test_timer_done_and_acc_must_share_one_number():
    """CLICK names one timer (or counter) number; T5 with TD7 cannot be stored."""
    from pyrung import debounce
    from pyrung.click.validation import CLK_TIMER_PAIR_MISMATCH

    split, other = Timer.clone("SplitTimer"), Timer.clone("OtherTimer")
    paired = Timer.clone("PairedTimer")

    def logic():
        with Rung(Bool("In")):
            debounce(Bool("Coil"), on=(split, 10), off=(paired, 10))
            on_delay(other, preset=10)

    tag_map = TagMap(
        [
            Bool("In").map_to(x[1]),
            Bool("Coil").map_to(y[1]),
            split.Done.map_to(t[5]),
            split.Acc.map_to(td[7]),
            paired.Done.map_to(t[6]),
            paired.Acc.map_to(td[6]),
            other.Done.map_to(t[7]),
            other.Acc.map_to(td[5]),
        ],
        include_system=False,
    )
    report = validate_click_program(_build_program(logic), tag_map, mode="strict")
    mismatches = [f for f in report.errors if f.code == CLK_TIMER_PAIR_MISMATCH]
    assert len(mismatches) == 2
    assert "T5 and TD7" in mismatches[0].message
    assert "T7 and TD5" in mismatches[1].message


@pytest.mark.parametrize(
    ("instruction", "target", "expected"),
    [
        (reset, txt[1], "reset(TXT1) at main.rung[0]"),
        (reset, ds[1], "reset(DS1) at main.rung[0]"),
        (reset, ds.select(1, 3), "reset(DS1..DS3) at main.rung[0]"),
        (latch, dd[1], "latch(DD1) at main.rung[0]"),
        (out, df[1], "out(DF1) at main.rung[0]"),
    ],
)
def test_coil_on_non_bit_memory_is_refused(instruction, target, expected):
    """CLICK's Out, Set and Reset coils take bit memory only (X, Y, C, T, CT, SC)."""
    coil = {reset: "Reset", latch: "Set", out: "Out"}[instruction]

    def logic():
        with Rung(c[1]):
            instruction(target)

    prog = _build_program(logic)
    report = validate_click_program(prog, TagMap(), mode="strict")
    wrong = [f for f in report.errors if f.code == CLK_BANK_WRONG_ROLE]
    assert len(wrong) == 1, _codes(report)
    message = wrong[0].message
    assert message.startswith(expected), message
    assert f"CLICK's {coil} coil takes bit memory only" in message
    assert message.isascii()
    with pytest.raises(LadderExportError, match=re.escape(expected)):
        pyrung_to_ladder(prog, TagMap())


def test_coil_on_bit_memory_passes():
    def logic():
        with Rung(x[1]):
            out(y[1])
            latch(c[1])
            reset(c.select(2, 4))
            reset(sc[50])

    report = validate_click_program(_build_program(logic), TagMap(), mode="strict")
    assert CLK_BANK_WRONG_ROLE not in _codes(report)
