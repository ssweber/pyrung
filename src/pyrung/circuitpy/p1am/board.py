"""P1AM-200 onboard peripheral tag model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pyrung.core.tag import InputTag, OutputTag, Tag, TagType


@dataclass(frozen=True)
class P1AMNeoPixelNamespace:
    """Onboard single NeoPixel RGB channels."""

    r: OutputTag
    g: OutputTag
    b: OutputTag


@dataclass(frozen=True)
class P1AMBoardNamespace:
    """Onboard P1AM peripheral tags."""

    switch: InputTag
    led: OutputTag
    neopixel: P1AMNeoPixelNamespace
    save_memory_cmd: OutputTag


@dataclass(frozen=True)
class RunStopConfig:
    """Optional RUN/STOP hardware-mode mapping for generated runtime."""

    source: Literal["board.switch"] = "board.switch"
    run_when_high: bool = True
    debounce_ms: int = 30
    expose_mode_tags: bool = True

    def __post_init__(self) -> None:
        if self.source != "board.switch":
            raise ValueError("runstop source must be 'board.switch'")
        if not isinstance(self.run_when_high, bool):
            raise TypeError("run_when_high must be bool")
        if not isinstance(self.debounce_ms, int):
            raise TypeError("debounce_ms must be int")
        if self.debounce_ms < 0:
            raise ValueError("debounce_ms must be >= 0")
        if not isinstance(self.expose_mode_tags, bool):
            raise TypeError("expose_mode_tags must be bool")


board = P1AMBoardNamespace(
    switch=InputTag("board.switch", TagType.BOOL, False, False),
    led=OutputTag("board.led", TagType.BOOL, False, False),
    neopixel=P1AMNeoPixelNamespace(
        r=OutputTag("board.neopixel.r", TagType.INT, 0, False),
        g=OutputTag("board.neopixel.g", TagType.INT, 0, False),
        b=OutputTag("board.neopixel.b", TagType.INT, 0, False),
    ),
    save_memory_cmd=OutputTag("board.save_memory_cmd", TagType.BOOL, False, False),
)

BOARD_TAGS: tuple[Tag, ...] = (
    board.switch,
    board.led,
    board.neopixel.r,
    board.neopixel.g,
    board.neopixel.b,
    board.save_memory_cmd,
)

BOARD_TAG_NAMES = frozenset(tag.name for tag in BOARD_TAGS)


def is_board_tag(tag: Tag) -> bool:
    """Return True when the tag belongs to the onboard P1AM model."""
    return tag.name in BOARD_TAG_NAMES


__all__ = [
    "BOARD_TAGS",
    "BOARD_TAG_NAMES",
    "P1AMBoardNamespace",
    "P1AMNeoPixelNamespace",
    "RunStopConfig",
    "board",
    "is_board_tag",
]
