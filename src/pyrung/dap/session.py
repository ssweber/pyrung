"""Internal mutable adapter session state."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any

from pyrung.core import PLC
from pyrung.core.context import ScanContext
from pyrung.core.rung import Rung
from pyrung.core.runner import ScanStep


@dataclass
class ScanFrameBuffer:
    """Per-emission accumulator for continue-mode compound events."""

    monitors: list[dict[str, Any]] = field(default_factory=list)
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    previous_tags: dict[str, Any] = field(default_factory=dict)


@dataclass
class DebugSession:
    runner: PLC | None = None
    scan_gen: Generator[ScanStep, None, None] | None = None
    current_scan_id: int | None = None
    current_step: ScanStep | None = None
    current_rung_index: int | None = None
    current_rung: Rung | None = None
    current_ctx: ScanContext | None = None
    program_path: str | None = None
    pending_predicate_pause: bool = False
    configuration_done: bool = False
    scan_frame_buffer: ScanFrameBuffer | None = None
