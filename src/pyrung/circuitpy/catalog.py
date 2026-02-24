"""P1AM-200 module catalog.

Static metadata for all Productivity1000 series I/O modules supported by
the P1AM-200 base unit.  Each entry describes the module's part number,
channel layout, and data type so that :class:`~pyrung.circuitpy.hardware.P1AM`
can construct the appropriate :class:`~pyrung.core.memory_block.InputBlock` /
:class:`~pyrung.core.memory_block.OutputBlock` instances.

Data sourced from the Arduino ``Module_List.h`` master table and the
`CircuitPython P1AM API reference <https://facts-engineering.github.io/api_reference.html>`_.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

from pyrung.core.tag import TagType


class ModuleDirection(Enum):
    """I/O direction of a P1AM module."""

    INPUT = "input"
    OUTPUT = "output"
    COMBO = "combo"


@dataclass(frozen=True)
class ChannelGroup:
    """One homogeneous group of channels within a module.

    Simple modules have a single group.  Combo modules (e.g. P1-16CDR)
    have two groups — one input, one output.

    Attributes:
        direction: ``INPUT`` or ``OUTPUT`` (never ``COMBO``).
        count: Number of channels in this group (positive).
        tag_type: IEC data type for the channels.
    """

    direction: ModuleDirection
    count: int
    tag_type: TagType


@dataclass(frozen=True)
class ModuleSpec:
    """Static specification for a single P1AM I/O module.

    Attributes:
        part_number: Manufacturer part number (e.g. ``"P1-08SIM"``).
        description: Human-readable summary.
        groups: One or two :class:`ChannelGroup` entries describing
            the module's channels.
    """

    part_number: str
    description: str
    groups: tuple[ChannelGroup, ...]

    @property
    def direction(self) -> ModuleDirection:
        """Overall direction: INPUT, OUTPUT, or COMBO."""
        if len(self.groups) == 1:
            return self.groups[0].direction
        return ModuleDirection.COMBO

    @property
    def is_combo(self) -> bool:
        """True if the module has both input and output channels."""
        return len(self.groups) > 1

    @property
    def input_group(self) -> ChannelGroup | None:
        """The input channel group, or ``None`` if the module has no inputs."""
        for g in self.groups:
            if g.direction is ModuleDirection.INPUT:
                return g
        return None

    @property
    def output_group(self) -> ChannelGroup | None:
        """The output channel group, or ``None`` if the module has no outputs."""
        for g in self.groups:
            if g.direction is ModuleDirection.OUTPUT:
                return g
        return None


# ---------------------------------------------------------------------------
# Helper factories — keep catalog entries concise
# ---------------------------------------------------------------------------


def _di(count: int) -> tuple[ChannelGroup, ...]:
    return (ChannelGroup(ModuleDirection.INPUT, count, TagType.BOOL),)


def _do(count: int) -> tuple[ChannelGroup, ...]:
    return (ChannelGroup(ModuleDirection.OUTPUT, count, TagType.BOOL),)


def _ai(count: int) -> tuple[ChannelGroup, ...]:
    return (ChannelGroup(ModuleDirection.INPUT, count, TagType.INT),)


def _ao(count: int) -> tuple[ChannelGroup, ...]:
    return (ChannelGroup(ModuleDirection.OUTPUT, count, TagType.INT),)


def _combo_discrete(di: int, do: int) -> tuple[ChannelGroup, ...]:
    return (
        ChannelGroup(ModuleDirection.INPUT, di, TagType.BOOL),
        ChannelGroup(ModuleDirection.OUTPUT, do, TagType.BOOL),
    )


def _combo_analog(ai: int, ao: int) -> tuple[ChannelGroup, ...]:
    return (
        ChannelGroup(ModuleDirection.INPUT, ai, TagType.INT),
        ChannelGroup(ModuleDirection.OUTPUT, ao, TagType.INT),
    )


# ---------------------------------------------------------------------------
# MODULE_CATALOG — all P1000-series modules supported by the P1AM-200.
#
# PWM (P1-04PWM) and high-speed counter (P1-02HSC) are excluded from the
# initial implementation; they require multi-tag channel models.
# ---------------------------------------------------------------------------

MODULE_CATALOG: Final[dict[str, ModuleSpec]] = {
    # --- Discrete Input (Bool) ------------------------------------------------
    "P1-08ND-TTL": ModuleSpec("P1-08ND-TTL", "8-ch discrete input (TTL)", _di(8)),
    "P1-08ND3": ModuleSpec("P1-08ND3", "8-ch discrete input (24V sink)", _di(8)),
    "P1-08NA": ModuleSpec("P1-08NA", "8-ch discrete input (120V AC)", _di(8)),
    "P1-08SIM": ModuleSpec("P1-08SIM", "8-ch discrete input simulator", _di(8)),
    "P1-08NE3": ModuleSpec("P1-08NE3", "8-ch discrete input (24V source)", _di(8)),
    "P1-16ND3": ModuleSpec("P1-16ND3", "16-ch discrete input (24V sink)", _di(16)),
    "P1-16NE3": ModuleSpec("P1-16NE3", "16-ch discrete input (24V source)", _di(16)),
    # --- Discrete Output (Bool) -----------------------------------------------
    "P1-04TRS": ModuleSpec("P1-04TRS", "4-ch relay output", _do(4)),
    "P1-08TA": ModuleSpec("P1-08TA", "8-ch AC output", _do(8)),
    "P1-08TRS": ModuleSpec("P1-08TRS", "8-ch relay output", _do(8)),
    "P1-16TR": ModuleSpec("P1-16TR", "16-ch relay output", _do(16)),
    "P1-08TD-TTL": ModuleSpec("P1-08TD-TTL", "8-ch discrete output (TTL)", _do(8)),
    "P1-08TD1": ModuleSpec("P1-08TD1", "8-ch discrete output (24V sink)", _do(8)),
    "P1-08TD2": ModuleSpec("P1-08TD2", "8-ch discrete output (24V source)", _do(8)),
    "P1-15TD1": ModuleSpec("P1-15TD1", "15-ch discrete output (24V sink)", _do(15)),
    "P1-15TD2": ModuleSpec("P1-15TD2", "15-ch discrete output (24V source)", _do(15)),
    # --- Combo Discrete (Bool in + Bool out) -----------------------------------
    "P1-16CDR": ModuleSpec("P1-16CDR", "8-ch DI + 8-ch relay DO", _combo_discrete(8, 8)),
    "P1-15CDD1": ModuleSpec("P1-15CDD1", "8-ch DI + 7-ch DO (24V sink)", _combo_discrete(8, 7)),
    "P1-15CDD2": ModuleSpec("P1-15CDD2", "8-ch DI + 7-ch DO (24V source)", _combo_discrete(8, 7)),
    # --- Analog Input (Int — raw ADC counts) -----------------------------------
    "P1-04AD": ModuleSpec("P1-04AD", "4-ch analog input (voltage/current)", _ai(4)),
    "P1-04AD-1": ModuleSpec("P1-04AD-1", "4-ch analog input (voltage)", _ai(4)),
    "P1-04AD-2": ModuleSpec("P1-04AD-2", "4-ch analog input (current)", _ai(4)),
    "P1-04RTD": ModuleSpec("P1-04RTD", "4-ch RTD temperature input", _ai(4)),
    "P1-04THM": ModuleSpec("P1-04THM", "4-ch thermocouple input", _ai(4)),
    "P1-04NTC": ModuleSpec("P1-04NTC", "4-ch NTC temperature input", _ai(4)),
    "P1-04ADL-1": ModuleSpec("P1-04ADL-1", "4-ch analog input (voltage, low-cost)", _ai(4)),
    "P1-04ADL-2": ModuleSpec("P1-04ADL-2", "4-ch analog input (current, low-cost)", _ai(4)),
    "P1-08ADL-1": ModuleSpec("P1-08ADL-1", "8-ch analog input (voltage, low-cost)", _ai(8)),
    "P1-08ADL-2": ModuleSpec("P1-08ADL-2", "8-ch analog input (current, low-cost)", _ai(8)),
    # --- Analog Output (Int) ---------------------------------------------------
    "P1-04DAL-1": ModuleSpec("P1-04DAL-1", "4-ch analog output (voltage, low-cost)", _ao(4)),
    "P1-04DAL-2": ModuleSpec("P1-04DAL-2", "4-ch analog output (current, low-cost)", _ao(4)),
    "P1-08DAL-1": ModuleSpec("P1-08DAL-1", "8-ch analog output (voltage, low-cost)", _ao(8)),
    "P1-08DAL-2": ModuleSpec("P1-08DAL-2", "8-ch analog output (current, low-cost)", _ao(8)),
    # --- Combo Analog (Int in + Int out) ---------------------------------------
    "P1-4ADL2DAL-1": ModuleSpec(
        "P1-4ADL2DAL-1", "4-ch AI (voltage) + 2-ch AO (voltage)", _combo_analog(4, 2)
    ),
    "P1-4ADL2DAL-2": ModuleSpec(
        "P1-4ADL2DAL-2", "4-ch AI (current) + 2-ch AO (current)", _combo_analog(4, 2)
    ),
}
