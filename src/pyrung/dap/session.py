"""Internal mutable adapter session state."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass

from pyrung.core import PLCRunner
from pyrung.core.context import ScanContext
from pyrung.core.rung import Rung
from pyrung.core.runner import ScanStep


@dataclass
class DebugSession:
    runner: PLCRunner | None = None
    scan_gen: Generator[ScanStep, None, None] | None = None
    current_scan_id: int | None = None
    current_step: ScanStep | None = None
    current_rung_index: int | None = None
    current_rung: Rung | None = None
    current_ctx: ScanContext | None = None
    program_path: str | None = None
    pending_predicate_pause: bool = False
