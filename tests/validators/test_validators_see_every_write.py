"""Each validator sees a write by any instruction, through any operand and in a forloop body.

A validator reads a tag's flags from the tag an instruction writes.  It reads the tags
the program uses, and their declared domains, from the program graph.
"""

from __future__ import annotations

from pyrung.core import (
    Block,
    Bool,
    Int,
    Physical,
    Program,
    TagType,
    Word,
    copy,
    fill,
    forloop,
    latch,
    math,
    out,
    rung,
    unpack_to_bits,
)
from pyrung.core.validation import (
    CMP_STATIC_ON_LEFT,
    PHYS_LINK_UNKNOWN,
    PHYS_MISSING_PROFILE,
    validate_cmp_conditions,
    validate_physical_realism,
)
from pyrung.core.validation.choices_violation import TAG_CHOICES_VIOLATION, validate_choices
from pyrung.core.validation.stuck_bits import COIL_STUCK_HIGH, validate_stuck_bits

Trigger = Bool("Trigger")
Guard = Bool("Guard")
Seen = Bool("Seen")


def _findings(report: object) -> list[tuple[str, str]]:
    return sorted((f.code, f.target_name) for f in report.findings)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# TAG_CHOICES_VIOLATION
# ---------------------------------------------------------------------------


def test_choices_reports_a_fill_into_slots_with_choices() -> None:
    modes = Block("Modes", TagType.INT, 1, 4)
    modes.slot(1, choices={0: "Off", 1: "On"})
    modes.slot(2, choices={0: "Off", 1: "On"})

    with Program(strict=False) as prog:
        with rung():
            fill(9, modes.select(1, 2))

    assert _findings(validate_choices(prog)) == [
        (TAG_CHOICES_VIOLATION, "Modes1"),
        (TAG_CHOICES_VIOLATION, "Modes2"),
    ]


def test_choices_reports_a_copy_in_a_forloop_body() -> None:
    mode = Int("Mode", choices={0: "Off", 1: "On"})

    with Program(strict=False) as prog:
        with rung():
            with forloop(3):
                copy(9, mode)

    assert _findings(validate_choices(prog)) == [(TAG_CHOICES_VIOLATION, "Mode")]


# ---------------------------------------------------------------------------
# COIL_STUCK_HIGH / COIL_STUCK_LOW
# ---------------------------------------------------------------------------


def test_stuck_bits_skips_a_readonly_or_external_tag_latched_in_a_forloop_body() -> None:
    held = Bool("Held", readonly=True)
    hmi = Bool("Hmi", external=True)

    with Program(strict=False) as prog:
        with rung(Trigger):
            with forloop(2):
                latch(held)
                latch(hmi)

    assert _findings(validate_stuck_bits(prog)) == []


def test_stuck_bits_reads_a_fill_as_a_latch_or_a_reset() -> None:
    lamps = Block("Lamps", TagType.BOOL, 1, 4)

    with Program(strict=False) as prog:
        with rung(Trigger):
            latch(lamps.select(1, 2))
        with rung(Guard):
            fill(False, lamps.select(1, 2))
        with rung(Trigger):
            fill(True, lamps.select(3, 4))

    assert _findings(validate_stuck_bits(prog)) == [
        (COIL_STUCK_HIGH, "Lamps3"),
        (COIL_STUCK_HIGH, "Lamps4"),
    ]


def test_stuck_bits_skips_a_bit_another_instruction_writes() -> None:
    bits = Block("Bits", TagType.BOOL, 1, 16)
    pattern = Word("Pattern", external=True)

    with Program(strict=False) as prog:
        with rung(Trigger):
            latch(bits[1])
        with rung(Guard):
            unpack_to_bits(pattern, bits.select(1, 16))

    assert _findings(validate_stuck_bits(prog)) == []


def test_stuck_bits_skips_a_bit_a_pointer_may_reset() -> None:
    bits = Block("Flags", TagType.BOOL, 1, 4)
    ptr = Int("Ptr", min=1, max=2)

    with Program(strict=False) as prog:
        with rung(Trigger):
            latch(bits[1])
        with rung(Guard):
            copy(0, bits[ptr])

    assert _findings(validate_stuck_bits(prog)) == []


def test_stuck_bits_skips_a_bit_a_pointer_in_the_same_rung_may_reset() -> None:
    small = Block("Small", TagType.BOOL, 1, 8)
    small_ptr = Int("SmallPtr", min=1, max=2)
    # A pointer with no bounds into a block this big names none of its targets.
    big = Block("Big", TagType.BOOL, 1, 2000)
    big_ptr = Int("BigPtr", external=True)

    with Program(strict=False) as prog:
        with rung(Trigger):
            latch(small[1])
            copy(0, small[small_ptr])
            latch(big[5])
            copy(0, big[big_ptr])

    assert _findings(validate_stuck_bits(prog)) == []


def test_stuck_bits_reports_a_latch_a_pointer_only_sets() -> None:
    alarms = Block("Alarms", TagType.BOOL, 1, 16)
    idx = Int("AlarmIdx", min=1, max=16)

    with Program(strict=False) as prog:
        with rung(Trigger):
            latch(alarms[3])
        with rung(Guard):
            copy(1, alarms[idx])

    assert _findings(validate_stuck_bits(prog)) == [(COIL_STUCK_HIGH, "Alarms3")]


def test_stuck_bits_skips_spares_only_a_fill_clears() -> None:
    coils = Block("Coils", TagType.BOOL, 1, 8)

    with Program(strict=False) as prog:
        with rung(Trigger):
            latch(coils.select(1, 4))
        with rung(Guard):
            fill(False, coils.select(1, 8))

    assert _findings(validate_stuck_bits(prog)) == []


# ---------------------------------------------------------------------------
# PHYS_LINK_UNKNOWN / PHYS_MISSING_PROFILE
# ---------------------------------------------------------------------------


def test_link_to_an_enable_only_a_fill_writes_is_known() -> None:
    motors = Block("Motors", TagType.BOOL, 1, 3)
    feedback = Bool(
        "Running", physical=Physical("Running", on_delay="100ms", off_delay="100ms"), link="Motors1"
    )

    with Program(strict=False) as prog:
        with rung(Trigger):
            fill(True, motors.select(1, 3))
        with rung(feedback):
            out(Seen)

    assert _findings(validate_physical_realism(prog)) == []


def test_link_to_an_enable_a_pointer_may_write_is_known() -> None:
    # A pointer with no bounds into a block this big names none of its targets.
    coils = Block("Coils", TagType.BOOL, 1, 2000)
    ptr = Int("CoilPtr", external=True)
    feedback = Bool(
        "Open", physical=Physical("Open", on_delay="100ms", off_delay="100ms"), link="Coils5"
    )

    with Program(strict=False) as prog:
        with rung(Trigger):
            copy(1, coils[ptr])
        with rung(feedback):
            out(Seen)

    assert PHYS_LINK_UNKNOWN not in [code for code, _ in _findings(validate_physical_realism(prog))]


def test_missing_profile_reports_a_feedback_only_math_reads() -> None:
    speed = Int("Speed")
    feedback = Int("SpeedFb", link="Speed")
    doubled = Int("Doubled")

    with Program(strict=False) as prog:
        with rung(Trigger):
            copy(3, speed)
        with rung():
            math(feedback * 2, doubled)

    assert _findings(validate_physical_realism(prog)) == [(PHYS_MISSING_PROFILE, "SpeedFb")]


# ---------------------------------------------------------------------------
# CMP_STATIC_ON_LEFT
# ---------------------------------------------------------------------------


def test_static_on_left_counts_a_write_through_a_pointer() -> None:
    levels = Block("Levels", TagType.INT, 1, 10)
    ptr = Int("LevelPtr", min=1, max=3)
    reading = Int("Reading", external=True)
    setpoint = Int("Setpoint", external=True)

    with Program(strict=False) as prog:
        with rung(Trigger):
            copy(reading, levels[ptr])
        with rung(setpoint < levels[2]):
            out(Seen)

    assert [f.code for f in validate_cmp_conditions(prog).findings] == [CMP_STATIC_ON_LEFT]
