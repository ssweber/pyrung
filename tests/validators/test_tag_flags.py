"""Tests for tag flag plumbing and flag-based validators.

Covers: external, final, public fields on Tag; mutual exclusivity;
Block/SlotView plumbing; Click comment parser; CORE_READONLY_WRITE,
CORE_CHOICES_VIOLATION, CORE_FINAL_MULTIPLE_WRITERS validators;
stuck-bits skipping readonly/external; recovers() external support.
"""

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Int,
    Program,
    Rung,
    copy,
    latch,
    out,
)
from pyrung.core.memory_block import Block
from pyrung.core.structure import Field, udt
from pyrung.core.tag import Tag, TagType

# ===========================================================================
# 1. Tag construction + mutual exclusivity
# ===========================================================================


class TestTagConstruction:
    def test_external_field(self):
        t = Bool("T", external=True)
        assert t.external is True
        assert t.final is False
        assert t.public is False

    def test_final_field(self):
        t = Int("T", final=True)
        assert t.final is True

    def test_public_field(self):
        t = Bool("T", public=True)
        assert t.public is True

    def test_readonly_final_mutually_exclusive(self):
        with pytest.raises(ValueError, match="readonly and final are mutually exclusive"):
            Tag(name="T", readonly=True, final=True)

    def test_readonly_external_mutually_exclusive(self):
        with pytest.raises(ValueError, match="readonly and external are mutually exclusive"):
            Tag(name="T", readonly=True, external=True)

    def test_external_final_allowed(self):
        t = Bool("T", external=True, final=True)
        assert t.external is True
        assert t.final is True

    def test_public_combines_with_anything(self):
        t1 = Bool("T1", readonly=True, public=True)
        assert t1.readonly and t1.public
        t2 = Bool("T2", external=True, public=True)
        assert t2.external and t2.public
        t3 = Bool("T3", final=True, public=True)
        assert t3.final and t3.public


# ===========================================================================
# 2. Block / SlotView plumbing
# ===========================================================================


class TestBlockSlotFlags:
    def test_slot_external_override(self):
        ds = Block("DS", TagType.INT, 1, 10)
        ds.slot(1, external=True)
        sv = ds.slot(1)
        assert sv.external is True
        assert sv.external_overridden is True
        assert sv.final is False

    def test_slot_final_override(self):
        ds = Block("DS", TagType.INT, 1, 10)
        ds.slot(2, final=True)
        sv = ds.slot(2)
        assert sv.final is True
        assert sv.final_overridden is True

    def test_slot_public_override(self):
        ds = Block("DS", TagType.INT, 1, 10)
        ds.slot(3, public=True)
        sv = ds.slot(3)
        assert sv.public is True
        assert sv.public_overridden is True

    def test_slot_reset_clears_new_flags(self):
        ds = Block("DS", TagType.INT, 1, 10)
        ds.slot(1, external=True, final=True, public=True)
        ds.slot(1).reset()
        sv = ds.slot(1)
        assert sv.external is False
        assert sv.final is False
        assert sv.public is False

    def test_tag_from_slot_carries_flags(self):
        ds = Block("DS", TagType.INT, 1, 10)
        ds.slot(1, external=True, public=True)
        tag = ds[1]
        assert tag.external is True
        assert tag.public is True
        assert tag.final is False


# ===========================================================================
# 3. Structure (UDT) plumbing
# ===========================================================================


class TestStructureFlags:
    def test_udt_decorator_level_flags(self):
        from typing import Any, cast

        @udt(external=True, public=True)
        class Cmd:
            Speed: Int

        cmd = cast(Any, Cmd)
        assert cmd.external is True
        assert cmd.public is True
        assert cmd.fields["Speed"].external is True
        assert cmd.fields["Speed"].public is True
        # count=1 tag access
        assert cmd.Speed.external is True
        assert cmd.Speed.public is True

    def test_field_level_override(self):
        from typing import Any, cast

        @udt()
        class Status:
            Value: Int = Field(final=True)
            Mode: Int = Field(external=True)

        status = cast(Any, Status)
        assert status.fields["Value"].final is True
        assert status.fields["Value"].external is False
        assert status.fields["Mode"].external is True

    def test_clone_preserves_flags(self):

        @udt(external=True, public=True)
        class Cmd:
            Speed: Int

        Cmd2 = Cmd.clone("Cmd2")
        assert Cmd2.external is True
        assert Cmd2.public is True


# ===========================================================================
# 4. Click comment parser
# ===========================================================================


class TestTagMetaParser:
    def test_parse_external(self):
        from pyrung.click.tag_map._parsers import parse_tag_meta

        meta, remaining = parse_tag_meta("[external] HMI cmd")
        assert meta is not None
        assert meta.external is True
        assert remaining == "HMI cmd"

    def test_parse_final(self):
        from pyrung.click.tag_map._parsers import parse_tag_meta

        meta, remaining = parse_tag_meta("[final]")
        assert meta is not None
        assert meta.final is True

    def test_parse_public(self):
        from pyrung.click.tag_map._parsers import parse_tag_meta

        meta, remaining = parse_tag_meta("[public]")
        assert meta is not None
        assert meta.public is True

    def test_parse_combined(self):
        from pyrung.click.tag_map._parsers import parse_tag_meta

        meta, _ = parse_tag_meta("[external, public, choices=Off:0|On:1]")
        assert meta is not None
        assert meta.external is True
        assert meta.public is True
        assert meta.choices == {0: "Off", 1: "On"}

    def test_format_round_trip(self):
        from pyrung.click.tag_map._parsers import TagMeta, format_tag_meta, parse_tag_meta

        original = TagMeta(external=True, final=True, choices={0: "Off", 1: "On"})
        formatted = format_tag_meta(original)
        parsed, _ = parse_tag_meta(formatted)
        assert parsed == original

    def test_format_empty_when_no_flags(self):
        from pyrung.click.tag_map._parsers import TagMeta, format_tag_meta

        assert format_tag_meta(TagMeta()) == ""
        assert format_tag_meta(None) == ""

    def test_unrecognized_token_still_raises(self):
        from pyrung.click.tag_map._parsers import parse_tag_meta

        with pytest.raises(ValueError, match="Unsupported TagMeta token"):
            parse_tag_meta("[external, bogus]")


# ===========================================================================
# 5. CORE_READONLY_WRITE validator
# ===========================================================================


class TestReadonlyWriteValidator:
    def test_write_to_readonly_flagged(self):
        from pyrung.core.validation.readonly_write import (
            CORE_READONLY_WRITE,
            validate_readonly_writes,
        )

        Light = Bool("Light", readonly=True)
        Button = Bool("Button")

        with Program() as prog:
            with Rung(Button):
                out(Light)

        report = validate_readonly_writes(prog)
        assert len(report.findings) == 1
        assert report.findings[0].code == CORE_READONLY_WRITE
        assert report.findings[0].target_name == "Light"

    def test_no_write_to_readonly_clean(self):
        from pyrung.core.validation.readonly_write import validate_readonly_writes

        Sensor = Bool("Sensor", readonly=True)
        Light = Bool("Light")

        with Program() as prog:
            with Rung(Sensor):
                out(Light)

        report = validate_readonly_writes(prog)
        assert len(report.findings) == 0

    def test_copy_to_readonly_flagged(self):
        from pyrung.core.validation.readonly_write import (
            validate_readonly_writes,
        )

        Setpoint = Int("Setpoint", readonly=True)

        with Program() as prog:
            with Rung():
                copy(42, Setpoint)

        report = validate_readonly_writes(prog)
        assert len(report.findings) == 1
        assert report.findings[0].target_name == "Setpoint"

    def test_latch_to_readonly_flagged(self):
        from pyrung.core.validation.readonly_write import (
            validate_readonly_writes,
        )

        Flag = Bool("Flag", readonly=True)
        Trigger = Bool("Trigger")

        with Program() as prog:
            with Rung(Trigger):
                latch(Flag)

        report = validate_readonly_writes(prog)
        assert len(report.findings) == 1
        assert report.findings[0].target_name == "Flag"


# ===========================================================================
# 6. CORE_CHOICES_VIOLATION validator
# ===========================================================================


class TestChoicesViolationValidator:
    def test_literal_outside_choices_flagged(self):
        from pyrung.core.validation.choices_violation import (
            CORE_CHOICES_VIOLATION,
            validate_choices,
        )

        Mode = Int("Mode", choices={0: "Off", 1: "On", 2: "Auto"})

        with Program() as prog:
            with Rung():
                copy(99, Mode)

        report = validate_choices(prog)
        assert len(report.findings) == 1
        assert report.findings[0].code == CORE_CHOICES_VIOLATION
        assert report.findings[0].value == 99

    def test_literal_in_choices_clean(self):
        from pyrung.core.validation.choices_violation import validate_choices

        Mode = Int("Mode", choices={0: "Off", 1: "On"})

        with Program() as prog:
            with Rung():
                copy(1, Mode)

        report = validate_choices(prog)
        assert len(report.findings) == 0

    def test_dynamic_source_skipped(self):
        from pyrung.core.validation.choices_violation import validate_choices

        Mode = Int("Mode", choices={0: "Off", 1: "On"})
        Cmd = Int("Cmd")

        with Program() as prog:
            with Rung():
                copy(Cmd, Mode)

        report = validate_choices(prog)
        assert len(report.findings) == 0


# ===========================================================================
# 7. CORE_FINAL_MULTIPLE_WRITERS validator
# ===========================================================================


class TestFinalWritersValidator:
    def test_single_writer_clean(self):
        from pyrung.core.validation.final_writers import validate_final_writers

        Counter = Int("Counter", final=True)

        with Program() as prog:
            with Rung():
                copy(0, Counter)

        report = validate_final_writers(prog)
        assert len(report.findings) == 0

    def test_multiple_writers_flagged(self):
        from pyrung.core.validation.final_writers import (
            CORE_FINAL_MULTIPLE_WRITERS,
            validate_final_writers,
        )

        Total = Int("Total", final=True)
        Button = Bool("Button")

        with Program() as prog:
            with Rung():
                copy(0, Total)
            with Rung(Button):
                copy(100, Total)

        report = validate_final_writers(prog)
        assert len(report.findings) == 1
        assert report.findings[0].code == CORE_FINAL_MULTIPLE_WRITERS
        assert report.findings[0].target_name == "Total"
        assert len(report.findings[0].sites) == 2

    def test_non_final_multiple_writers_clean(self):
        from pyrung.core.validation.final_writers import validate_final_writers

        Counter = Int("Counter")
        Button = Bool("Button")

        with Program() as prog:
            with Rung():
                copy(0, Counter)
            with Rung(Button):
                copy(100, Counter)

        report = validate_final_writers(prog)
        assert len(report.findings) == 0


# ===========================================================================
# 8. Stuck-bits: skip readonly and external
# ===========================================================================


class TestStuckBitsReadonlySkip:
    def test_readonly_latch_no_reset_not_flagged(self):
        from pyrung.core.validation.stuck_bits import validate_stuck_bits

        Alarm = Bool("Alarm", readonly=True)
        Trigger = Bool("Trigger")

        with Program() as prog:
            with Rung(Trigger):
                latch(Alarm)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0


class TestStuckBitsExternalSkip:
    def test_external_latch_no_reset_not_flagged(self):
        from pyrung.core.validation.stuck_bits import validate_stuck_bits

        HmiCmd = Bool("HmiCmd", external=True)
        Trigger = Bool("Trigger")

        with Program() as prog:
            with Rung(Trigger):
                latch(HmiCmd)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 0

    def test_non_external_latch_no_reset_still_flagged(self):
        from pyrung.core.validation.stuck_bits import CORE_STUCK_HIGH, validate_stuck_bits

        Light = Bool("Light")
        Trigger = Bool("Trigger")

        with Program() as prog:
            with Rung(Trigger):
                latch(Light)

        report = validate_stuck_bits(prog)
        assert len(report.findings) == 1
        assert report.findings[0].code == CORE_STUCK_HIGH


# ===========================================================================
# 9. recovers() external support
# ===========================================================================


class TestRecoversExternal:
    def test_external_tag_always_recovers(self):
        HmiCmd = Bool("HmiCmd", external=True)
        Trigger = Bool("Trigger")

        with Program() as prog:
            with Rung(Trigger):
                latch(HmiCmd)

        with PLC(prog, dt=0.1) as plc:
            plc.patch({Trigger.name: True})
            plc.step()
            assert plc.recovers(HmiCmd) is True
