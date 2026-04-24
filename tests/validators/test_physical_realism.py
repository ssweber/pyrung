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
    CORE_ANTITOGGLE,
    CORE_MISSING_PROFILE,
    CORE_RANGE_VIOLATION,
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
        CORE_RANGE_VIOLATION,
        CORE_RANGE_VIOLATION,
        CORE_RANGE_VIOLATION,
    ]
    assert {finding.target_name for finding in report.findings} == {
        "Pressure",
        "Values1",
        "Values2",
    }


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
    assert report.findings[0].code == CORE_MISSING_PROFILE
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
    assert report.findings[0].code == CORE_ANTITOGGLE
    assert report.findings[0].target_name == "Enable"
