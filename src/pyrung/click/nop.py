"""NOP instruction — explicit no-operation for Click ladder programs.

Click PLCs use NOP to mark comment-only rungs in the ladder CSV.
The PLC runner treats it as a no-op; the Click ladder exporter emits
``NOP`` in the AF column.

Usage::

    from pyrung.click import nop

    with Rung() as r:
        r.comment = "Section header comment"
        nop()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyrung.core._source import _capture_source
from pyrung.core.instruction import Instruction
from pyrung.core.program.context import _require_rung_context

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext


class NopInstruction(Instruction):
    """Explicit no-operation instruction for Click ladder programs.

    Occupies a rung slot so comment-only rungs survive round-trip
    through ladder CSV export/import.  No-op in the PLC runner.
    """

    INERT_WHEN_DISABLED = True

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        """No-op — NOP instructions have no runtime behaviour."""

    def is_terminal(self) -> bool:
        """NOP must be the only instruction in its rung."""
        return True


def nop() -> None:
    """No-operation instruction (NOP) for Click ladder programs.

    Explicit placeholder for comment-only rungs.  Only one ``nop()``
    is allowed per rung, and it must be the sole instruction.

    Example::

        from pyrung.click import nop

        with Rung() as r:
            r.comment = "Section header"
            nop()
    """
    ctx = _require_rung_context("nop")
    ctx._assert_no_pending_required_builder("nop")
    if ctx._rung._instructions:
        raise RuntimeError("nop() must be the only instruction in a rung")
    source_file, source_line = _capture_source(depth=2)
    instruction = NopInstruction()
    instruction.source_file, instruction.source_line = source_file, source_line
    ctx._rung.add_instruction(instruction)
