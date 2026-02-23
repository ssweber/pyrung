"""Internal mutable adapter session state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pyrung.core.context import ScanContext
from pyrung.core.rung import Rung
from pyrung.core.runner import ScanStep


@dataclass
class DebugSession:
    runner: Any = None
    scan_gen: Any = None
    current_scan_id: int | None = None
    current_step: ScanStep | None = None
    current_rung_index: int | None = None
    current_rung: Rung | None = None
    current_ctx: ScanContext | None = None
    program_path: str | None = None
    pending_predicate_pause: bool = False
