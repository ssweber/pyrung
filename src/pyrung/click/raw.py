"""Raw instruction — opaque passthrough for Click ladder export.

Preserves an unrecognised instruction's decoded fields for
binary → CSV → DSL → CSV → binary round-trip fidelity.
No-op in the PLC runner.

Usage::

    from pyrung.click import raw

    with Rung(StartButton):
        raw("Email", "0x2737,1,60a5=1,60a6=,...")
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

    Stores a Click binary class name and decoded field string.  The PLC
    runner treats it as a no-op; the Click ladder exporter emits it as
    ``raw(ClassName,fields)`` in the AF column.
    """

    INERT_WHEN_DISABLED = True
    _reads = ()
    _writes = ()
    _conditions = ()
    _structural_fields = ("class_name", "fields")

    def __init__(self, class_name: str, fields: str) -> None:
        self.class_name = class_name
        self.fields = fields

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        """No-op — raw instructions have no runtime behaviour."""


def raw(class_name: str, fields: str) -> None:
    """Raw instruction passthrough for Click ladder export.

    Preserves decoded field data for binary round-trip fidelity.
    No-op in the PLC runner.

    Args:
        class_name: Binary class name (e.g. ``"Email"``, ``"Home"``).
        fields: Opaque field string from laddercodec
            (e.g. ``"0x2737,1,60a5=1,..."``).
    """
    ctx = _require_rung_context("raw")
    ctx._assert_no_pending_required_builder("raw")
    source_file, source_line = _capture_source(depth=2)
    instruction = RawInstruction(class_name, fields)
    instruction.source_file, instruction.source_line = source_file, source_line
    ctx._rung.add_instruction(instruction)
