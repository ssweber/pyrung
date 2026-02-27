"""Automatically generated module split."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from pyrung.core.tag import Tag

from .base import Instruction, OneShotMixin
from .conversions import (
    _math_out_of_range_for_dest,
    _truncate_to_tag_type,
)
from .resolvers import (
    _set_fault_division_error,
    _set_fault_out_of_range,
    resolve_tag_name_ctx,
    resolve_tag_or_value_ctx,
)
from .utils import guard_oneshot_execution

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext


class CalcInstruction(OneShotMixin, Instruction):
    """Evaluate an arithmetic expression and store the result (CALC).

    Evaluates `expression` (which may reference any `Tag` or arithmetic
    combination thereof) and stores the result into `dest`, truncating
    to the destination type's bit width via modular wrapping.

    **Wrapping semantics:** Overflow wraps — e.g. storing 32 768 into an INT
    tag produces −32 768. This differs from `CopyInstruction`, which clamps.

    **Division by zero:** Result is forced to 0 and the division-error fault
    flag is set. The instruction completes normally.

    **Modes:**

    - ``"decimal"`` (default) — signed arithmetic, result stored as signed int.
    - ``"hex"`` — unsigned 16-bit arithmetic; operands treated as unsigned and
      result wraps at 0xFFFF.

    Args:
        expression: Python expression built from `Tag` objects and literals
            (e.g. ``DS1 + DS2 * 10``).
        dest: Destination `Tag` to write the result into.
        oneshot: When True, execute only on the rung's rising edge. Default False.
        mode: ``"decimal"`` or ``"hex"``. Default ``"decimal"``.
    """

    def __init__(
        self,
        expression: Any,
        dest: Tag,
        oneshot: bool = False,
        mode: str = "decimal",
    ):
        OneShotMixin.__init__(self, oneshot)
        self.expression = expression
        self.dest = dest
        self.mode = mode

    @guard_oneshot_execution
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        # Evaluate expression (handles Tag, Expression, IndirectRef, literal)
        try:
            value = resolve_tag_or_value_ctx(self.expression, ctx)
        except ZeroDivisionError:
            _set_fault_division_error(ctx)
            value = 0

        # Expression division may return non-finite sentinels for divide-by-zero.
        if isinstance(value, float) and not math.isfinite(value):
            _set_fault_division_error(ctx)
            value = 0

        if _math_out_of_range_for_dest(value, self.dest, self.mode):
            _set_fault_out_of_range(ctx)

        # Truncate to destination type
        value = _truncate_to_tag_type(value, self.dest, self.mode)

        # Resolve destination name (handles indirect)
        target_name = resolve_tag_name_ctx(self.dest, ctx)
        ctx.set_tag(target_name, value)
