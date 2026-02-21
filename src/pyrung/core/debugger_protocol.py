"""Internal protocol boundary used by PLCDebugger to interact with a runner."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from pyrung.core.context import ScanContext
from pyrung.core.rung import Rung


class DebugRunner(Protocol):
    """Internal runner API consumed by the debugger."""

    def prepare_scan(self) -> tuple[ScanContext, float]:
        """Create/initialize one scan context and return (context, dt)."""

    def commit_scan(self, ctx: ScanContext, dt: float) -> None:
        """Commit one completed scan context."""

    def iter_top_level_rungs(self) -> Iterable[Rung]:
        """Return top-level rungs in scan order."""

    def evaluate_condition_value(
        self,
        condition: Any,
        ctx: ScanContext,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Evaluate one condition and return (value, details)."""

    def condition_term_text(self, condition: Any, details: list[dict[str, Any]]) -> str:
        """Return formatted summary for one condition/details pair."""

    def condition_annotation(self, *, status: str, expression: str, summary: str) -> str:
        """Return annotation text for debugger display."""

    def condition_expression(self, condition: Any) -> str:
        """Return human-readable condition expression."""
