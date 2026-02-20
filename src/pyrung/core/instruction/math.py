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


class MathInstruction(OneShotMixin, Instruction):
    """Math instruction.

    Evaluates an expression and stores the result in a destination tag,
    with truncation to the destination's type width.

    Key differences from CopyInstruction:
    - Truncates result to destination tag's bit width (modular wrapping)
    - Division by zero produces 0 (not infinity)
    - Supports "decimal" (signed) and "hex" (unsigned 16-bit) modes
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
