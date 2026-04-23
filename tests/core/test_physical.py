"""Tests for Physical feedback declarations, duration parsing, and tag-level fields."""

import pytest

from pyrung import Block, Bool, Field, Int, Real, TagType, named_array, udt
from pyrung.core.physical import Physical, parse_duration


class TestParseDuration:
    def test_milliseconds(self):
        assert parse_duration("500ms") == 500

    def test_seconds(self):
        assert parse_duration("2s") == 2000

    def test_minutes(self):
        assert parse_duration("1min") == 60_000

    def test_minutes_short(self):
        assert parse_duration("5m") == 300_000

    def test_hours(self):
        assert parse_duration("1h") == 3_600_000

    def test_days(self):
        assert parse_duration("1d") == 86_400_000

    def test_compound_seconds_ms(self):
        assert parse_duration("2s50ms") == 2050

    def test_compound_hours_minutes(self):
        assert parse_duration("1h30min") == 5_400_000

    def test_fractional_value(self):
        assert parse_duration("1.5s") == 1500

    def test_whitespace_stripped(self):
        assert parse_duration("  2s  ") == 2000

    def test_iec_duration_prefix(self):
        assert parse_duration("T#5ms") == 5
        assert parse_duration("T#3s") == 3000
        assert parse_duration(" T#1min30s ") == 90_000

    def test_empty_string(self):
        with pytest.raises(ValueError, match="empty duration"):
            parse_duration("")

    def test_whitespace_only(self):
        with pytest.raises(ValueError, match="empty duration"):
            parse_duration("   ")

    def test_no_unit(self):
        with pytest.raises(ValueError, match="no duration tokens"):
            parse_duration("500")

    def test_just_unit(self):
        with pytest.raises(ValueError, match="no duration tokens"):
            parse_duration("ms")

    def test_garbage(self):
        with pytest.raises(ValueError, match="no duration tokens"):
            parse_duration("hello")

    def test_trailing_garbage(self):
        with pytest.raises(ValueError, match="unexpected"):
            parse_duration("2s foo")

    def test_leading_garbage(self):
        with pytest.raises(ValueError, match="unexpected"):
            parse_duration("foo 2s")


class TestPhysical:
    def test_bool_feedback_on_delay_only(self):
        p = Physical("MotorFb", on_delay="2s")
        assert p.feedback_type == "bool"
        assert p.on_delay_ms == 2000
        assert p.off_delay_ms is None

    def test_bool_feedback_both_delays(self):
        p = Physical("MotorFb", on_delay="2s", off_delay="500ms")
        assert p.feedback_type == "bool"
        assert p.on_delay_ms == 2000
        assert p.off_delay_ms == 500

    def test_bool_feedback_off_delay_only(self):
        p = Physical("ValveFb", off_delay="100ms")
        assert p.feedback_type == "bool"
        assert p.off_delay_ms == 100

    def test_analog_feedback(self):
        p = Physical("TempSensor", profile="first_order")
        assert p.feedback_type == "analog"
        assert p.profile == "first_order"

    def test_system_field(self):
        p = Physical("MotorFb", on_delay="2s", system="cooling")
        assert p.system == "cooling"

    def test_empty_name_rejects(self):
        with pytest.raises(ValueError, match="non-empty"):
            Physical("", on_delay="2s")

    def test_both_timing_and_profile_rejects(self):
        with pytest.raises(ValueError, match="both timing and profile"):
            Physical("Bad", on_delay="2s", profile="first_order")

    def test_neither_timing_nor_profile_rejects(self):
        with pytest.raises(ValueError, match="neither timing nor profile"):
            Physical("Empty")

    def test_neither_with_system_only_rejects(self):
        with pytest.raises(ValueError, match="neither timing nor profile"):
            Physical("SystemOnly", system="cooling")

    def test_bad_on_delay_rejects(self):
        with pytest.raises(ValueError):
            Physical("Bad", on_delay="not_a_duration")

    def test_bad_off_delay_rejects(self):
        with pytest.raises(ValueError):
            Physical("Bad", off_delay="not_a_duration")

    def test_frozen(self):
        p = Physical("MotorFb", on_delay="2s")
        with pytest.raises(AttributeError):
            p.name = "changed"  # ty: ignore[invalid-assignment]

    def test_compound_delay(self):
        p = Physical("SlowMotor", on_delay="2s50ms")
        assert p.on_delay_ms == 2050


motor_fb = Physical("MotorFb", on_delay="2s", off_delay="500ms", system="cooling")
temp_sensor = Physical("TempSensor", profile="first_order", system="cooling")


class TestTagFields:
    def test_standalone_tag_physical(self):
        fb = Bool("Running", physical=motor_fb, link="Enable")
        assert fb.physical is motor_fb
        assert fb.link == "Enable"

    def test_standalone_tag_range(self):
        t = Real("Temp", physical=temp_sensor, link="En", min=0, max=150, uom="degC")
        assert t.physical is temp_sensor
        assert t.min == 0
        assert t.max == 150
        assert t.uom == "degC"

    def test_standalone_range_min_must_be_less_than_max(self):
        with pytest.raises(ValueError, match="min must be less than max"):
            Real("Temp", min=100, max=100)

    def test_choices_cannot_combine_with_range(self):
        with pytest.raises(ValueError, match="choices cannot be combined"):
            Int("Mode", choices={0: "Off", 1: "On"}, min=0)

    def test_readonly_cannot_combine_with_physical(self):
        with pytest.raises(ValueError, match="readonly cannot be combined with physical"):
            Bool("Running", readonly=True, physical=motor_fb)

    def test_standalone_tag_defaults_none(self):
        b = Bool("Plain")
        assert b.physical is None
        assert b.link is None
        assert b.min is None
        assert b.max is None
        assert b.uom is None

    def test_field_in_udt(self):
        @udt()
        class Pump:
            Enable: Bool
            Running_Fb: Bool = Field(physical=motor_fb, link="Enable")  # ty: ignore[invalid-assignment]
            Temp: Real = Field(  # ty: ignore[invalid-assignment]
                physical=temp_sensor, link="Enable", min=0, max=150, uom="degC"
            )

        p = Pump[1]
        assert p.Running_Fb.physical is motor_fb
        assert p.Running_Fb.link == "Enable"
        assert p.Temp.physical is temp_sensor
        assert p.Temp.min == 0
        assert p.Temp.max == 150
        assert p.Temp.uom == "degC"

    def test_field_without_physical(self):
        @udt()
        class Simple:
            Status: Bool

        s = Simple[1]
        assert s.Status.physical is None

    def test_udt_counted_shares_physical(self):
        @udt(count=3)
        class Motor:
            Enable: Bool
            Fb: Bool = Field(physical=motor_fb, link="Enable")  # ty: ignore[invalid-assignment]

        assert Motor[1].Fb.physical is motor_fb
        assert Motor[2].Fb.physical is motor_fb
        assert Motor[3].Fb.physical is motor_fb

    def test_named_array_link(self):
        @named_array(Bool, count=2, stride=2)
        class Valve:
            Enable = Field()
            Open_Fb = Field(physical=motor_fb, link="Enable")

        assert Valve[1].Open_Fb.link == "Enable"
        assert Valve[2].Open_Fb.physical is motor_fb

    def test_block_slot_physical_link_metadata(self):
        alarms = Block("Alarms", TagType.BOOL, 1, 2)
        alarms.slot(1, physical=motor_fb, link="Enable")

        assert alarms.slot(1).physical is motor_fb
        assert alarms.slot(1).link == "Enable"
        assert alarms[1].physical is motor_fb
        assert alarms[1].link == "Enable"

    def test_bad_same_scope_link_rejects_at_decorator_construction(self):
        with pytest.raises(ValueError, match="unknown field"):

            @udt()
            class BadLink:
                Enable: Bool
                Running_Fb: Bool = Field(  # ty: ignore[invalid-assignment]
                    physical=motor_fb, link="Missing"
                )

    def test_linked_bool_requires_physical_timing_or_profile(self):
        with pytest.raises(ValueError, match="linked BOOL feedback"):

            @udt()
            class BadFeedback:
                Enable: Bool
                Running_Fb: Bool = Field(link="Enable")  # ty: ignore[invalid-assignment]

    def test_profile_without_link_rejects_tag(self):
        with pytest.raises(ValueError, match="profile requires link"):
            Real("Temp", physical=temp_sensor)

    def test_profile_without_link_rejects_field(self):
        with pytest.raises(ValueError, match="profile requires link"):

            @udt()
            class BadProfile:
                Temp: Real = Field(physical=temp_sensor)  # ty: ignore[invalid-assignment]

    def test_bool_with_profile_allowed_tag(self):
        tag = Bool("Fb", physical=temp_sensor, link="En")
        assert tag.physical is not None
        assert tag.physical.profile == "first_order"

    def test_bool_with_profile_allowed_field(self):
        @udt()
        class ProfileBool:
            En: Bool
            Fb: Bool = Field(physical=temp_sensor, link="En")  # ty: ignore[invalid-assignment]

        assert ProfileBool.Fb.physical is not None
        assert ProfileBool.Fb.physical.profile == "first_order"

    def test_linked_analog_without_profile_allowed_at_construction(self):
        @udt()
        class AnalogLoop:
            Cmd: Real
            Pv: Real = Field(link="Cmd")  # ty: ignore[invalid-assignment]

        assert AnalogLoop.Pv.link == "Cmd"
