"""Raw instruction — opaque passthrough for Click ladder export.

Preserves an unrecognised instruction blob for binary → CSV → DSL →
CSV → binary round-trip fidelity.  No-op in the PLC runner.

Usage::

    from pyrung.click import raw

    with Rung(StartButton):
        raw("Copy", blob=bytes.fromhex("0a1b2c3d..."))
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyrung.core._source import _capture_source
from pyrung.core.instruction import Instruction
from pyrung.core.program.context import _require_rung_context

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext


class RawInstruction(Instruction):
    """Opaque instruction passthrough.

    Stores a Click binary class name and raw blob bytes.  The PLC runner
    treats it as a no-op; the Click ladder exporter emits it as
    ``raw(ClassName,hex)`` in the AF column.
    """

    INERT_WHEN_DISABLED = True

    def __init__(self, class_name: str, *, blob: bytes) -> None:
        self.class_name = class_name
        self.blob = blob

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        """No-op — raw instructions have no runtime behaviour."""


def raw(class_name: str, *, blob: bytes) -> None:
    """Raw instruction passthrough for Click ladder export.

    Preserves an opaque instruction blob for binary round-trip fidelity.
    No-op in the PLC runner.

    Args:
        class_name: Binary class name (e.g. ``"Copy"``, ``"Cnt"``).
        blob: Full instruction blob bytes.
    """
    ctx = _require_rung_context("raw")
    ctx._assert_no_pending_required_builder("raw")
    source_file, source_line = _capture_source(depth=2)
    instruction = RawInstruction(class_name, blob=blob)
    instruction.source_file, instruction.source_line = source_file, source_line
    ctx._rung.add_instruction(instruction)
