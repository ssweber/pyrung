"""Tests for static physical realism validators."""

from pyrung.core import (
    Block,
    Bool,
    Physical,
    Program,
    Real,
    Rung,
    TagType,
    copy,
    fill,
    out,
    rise,
)
from pyrung.core.validation import (
    PHYS_ANTITOGGLE,
    PHYS_MISSING_PROFILE,
    TAG_RANGE_VIOLATION,
    validate_physical_realism,
)


def test_range_violation_flags_literal_copy_and_fill():
    pressure = Real("Pressure", min=0, max=100, uom="psi")
    values = Block("Values", TagType.REAL, 1, 2)
    values.slot(1, min=0, max=10)
    values.slot(2, min=0, max=10)

    with Program() as prog:
        with Rung():
            copy(150, pressure)
            fill(12, values.select(1, 2))

    report = validate_physical_realism(prog)
    assert [finding.code for finding in report.findings] == [
        TAG_RANGE_VIOLATION,
        TAG_RANGE_VIOLATION,
        TAG_RANGE_VIOLATION,
    ]
    assert {finding.target_name for finding in report.findings} == {
        "Pressure",
        "Values1",
        "Values2",
    }
    pressure_finding = next(f for f in report.findings if f.target_name == "Pressure")
    assert pressure_finding.display.frames[0].caret_label == "outside min=0, max=100"
    assert pressure_finding.display.hint == (
        "use a value allowed by Pressure's min/max, or widen those limits"
    )


def test_range_violation_describes_one_sided_min_max_limits():
    floor = Real("Floor", min=0)
    ceiling = Real("Ceiling", max=100)

    with Program() as prog:
        with Rung():
            copy(-1, floor)
            copy(101, ceiling)

    report = validate_physical_realism(prog)
    assert [f.display.frames[0].caret_label for f in report.findings] == [
        "below min=0",
        "above max=100",
    ]


def test_range_validator_allows_in_range_literals():
    pressure = Real("Pressure", min=0, max=100, uom="psi")

    with Program() as prog:
        with Rung():
            copy(50, pressure)

    report = validate_physical_realism(prog)
    assert report.findings == ()


def test_range_validator_skips_dynamic_writes():
    source = Real("Source")
    pressure = Real("Pressure", min=0, max=100, uom="psi")

    with Program() as prog:
        with Rung():
            copy(source, pressure)

    report = validate_physical_realism(prog)
    assert report.findings == ()


def test_missing_analog_profile_finding():
    cmd = Real("Cmd")
    pv = Real("Pv", link="Cmd")

    with Program() as prog:
        with Rung():
            copy(cmd, pv)

    report = validate_physical_realism(prog)
    assert len(report.findings) == 1
    assert report.findings[0].code == PHYS_MISSING_PROFILE
    assert report.findings[0].target_name == "Pv"


def test_one_direction_bool_timing_skips_antitoggle():
    start = Bool("Start")
    enable = Bool("Enable")
    feedback = Bool("Running", physical=Physical("Running", on_delay="2s"), link="Enable")
    seen = Bool("Seen")

    with Program() as prog:
        with Rung(rise(start)):
            out(enable)
        with Rung(feedback):
            out(seen)

    report = validate_physical_realism(prog, dt=0.010)
    assert report.findings == ()


def test_both_direction_bool_timing_flags_one_scan_edge_pulse():
    start = Bool("Start")
    enable = Bool("Enable")
    feedback = Bool(
        "Running",
        physical=Physical("Running", on_delay="2s", off_delay="500ms"),
        link="Enable",
    )
    seen = Bool("Seen")

    with Program() as prog:
        with Rung(rise(start)):
            out(enable)
        with Rung(feedback):
            out(seen)

    report = validate_physical_realism(prog, dt=0.010)
    assert len(report.findings) == 1
    assert report.findings[0].code == PHYS_ANTITOGGLE
    assert report.findings[0].target_name == "Enable"
    assert "Enable can pulse for 10 ms; feedback needs ~2500 ms." in report.findings[0].message
    assert "hold Enable long enough for feedback to respond" in report.findings[0].message


def test_opposing_writes_recommend_tracking_desired_state_separately():
    select = Bool("Select")
    enable = Bool("Enable")
    feedback = Bool(
        "Running",
        physical=Physical("Running", on_delay="2s", off_delay="500ms"),
        link="Enable",
    )
    seen = Bool("Seen")

    with Program() as prog:
        with Rung():
            out(enable)
        with Rung(select):
            out(enable)
        with Rung(feedback):
            out(seen)

    report = validate_physical_realism(prog, dt=0.010)
    finding = next(f for f in report.findings if f.code == PHYS_ANTITOGGLE)
    assert "Enable is set on and off in one scan" in finding.message
    assert "track the desired state separately, then drive Enable from one out()" in finding.message
