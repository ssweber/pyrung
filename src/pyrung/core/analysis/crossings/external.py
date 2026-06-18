"""Modbus-receive crossing — the target is an external input, so the chase stops.

``modbus_receive`` writes local tags from off-PLC data, so a target on one of
them inverts to :class:`External` — not a fallthrough (could not invert) but a
*stop* (there is nothing upstream in this program to chase).  ``modbus_send``
writes no local tag and stays exempt.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.crossings import BaseCrossing, register
from pyrung.core.crossing import (
    REVERSE_FALLTHROUGH,
    Constraint,
    CrossingContext,
    Eq,
    External,
    ReverseResult,
    single,
)
from pyrung.core.instruction.send_receive._core import ModbusReceiveInstruction


class ModbusReceiveCrossing(BaseCrossing):
    """Reverse ``modbus_receive``: the written tag is an external input."""

    def reverse(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> ReverseResult:
        if not (isinstance(target, Eq) and len(target.values) == 1):
            return REVERSE_FALLTHROUGH
        return single(External(target.tag), exact=True)


register(ModbusReceiveInstruction, ModbusReceiveCrossing())
