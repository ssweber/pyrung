"""Modbus-receive crossing — external payload, local request/status writes.

Only tags in ``dest`` are supplied by the recorded/injected transport result;
those invert to :class:`External`, a deliberate chase stop. ``receiving``,
``success``, ``error``, and ``exception_response`` are written by the local
request state machine from enable/pending/result state. Their full temporal
preimage is not represented, so they fall through instead of being mislabeled
as external payload.
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
    """Reverse only receive payload destinations to an external-input stop."""

    def reverse(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> ReverseResult:
        if not (isinstance(target, Eq) and len(target.values) == 1):
            return REVERSE_FALLTHROUGH
        if not isinstance(instr, ModbusReceiveInstruction):
            return REVERSE_FALLTHROUGH
        if target.tag in instr.external_payload_names:
            return single(External(target.tag), exact=True)
        # Status fields are locally written from request/replay state. Until
        # that temporal state has a constraint shape, make no reverse claim.
        return REVERSE_FALLTHROUGH


register(ModbusReceiveInstruction, ModbusReceiveCrossing())
