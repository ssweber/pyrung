"""Automatically generated module split."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.expression import (
    DOMAIN_LIMITED_MATH_FUNCS,
    Expression,
    MathDomainError,
    MathFuncExpr,
    RandIntExpr,
)
from pyrung.core.memory_block import BlockRange, IndirectExprRef, IndirectRef
from pyrung.core.storage import check_real_value
from pyrung.core.tag import ImmediateRef, Tag, TagType

from .base import Instruction, OneShotMixin
from .conversions import (
    _math_out_of_range_for_dest,
    _truncate_to_tag_type,
)
from .resolvers import (
    _set_fault_division_error,
    _set_fault_math_operation_error,
    _set_fault_out_of_range,
    resolve_tag_name_ctx,
    resolve_tag_or_value_ctx,
)
from .utils import guard_oneshot_execution

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext


CalcMode = Literal["decimal", "hex"]


@dataclass(frozen=True)
class CalcModeInference:
    mode: CalcMode
    mixed_families: bool
    has_hex_family: bool
    has_decimal_family: bool


def _calc_family_for_tag_type(tag_type: TagType) -> CalcMode:
    return "hex" if tag_type == TagType.WORD else "decimal"


def _collect_calc_tag_types(value: Any, found: set[TagType], seen: set[int]) -> None:
    value_id = id(value)
    if value_id in seen:
        return
    seen.add(value_id)

    if isinstance(value, ImmediateRef):
        _collect_calc_tag_types(value.value, found, seen)
        return

    if isinstance(value, Tag):
        found.add(value.type)
        return

    if isinstance(value, IndirectRef):
        # Pointer type is addressing metadata; family comes from the indexed block.
        found.add(value.block.type)
        return

    if isinstance(value, IndirectExprRef):
        # Address expression type does not affect arithmetic family.
        found.add(value.block.type)
        return

    if isinstance(value, BlockRange):
        found.add(value.block.type)
        return

    if isinstance(value, RandIntExpr):
        # The draw's hidden DINT tag holds the result; the bounds set the family.
        _collect_calc_tag_types(value.lo, found, seen)
        _collect_calc_tag_types(value.hi, found, seen)
        return

    if isinstance(value, Expression):
        for child in vars(value).values():
            _collect_calc_tag_types(child, found, seen)
        return

    if isinstance(value, dict):
        for item in value.items():
            _collect_calc_tag_types(item, found, seen)
        return

    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _collect_calc_tag_types(item, found, seen)


def has_domain_limited_func(value: Any, seen: set[int] | None = None) -> bool:
    """True when *value* (a calc expression) calls a math function that can raise
    :class:`~pyrung.core.expression.MathDomainError` (``sqrt``, ``log``, ...)."""
    if seen is None:
        seen = set()
    if id(value) in seen:
        return False
    seen.add(id(value))
    if isinstance(value, MathFuncExpr) and value.name in DOMAIN_LIMITED_MATH_FUNCS:
        return True
    if isinstance(value, Expression):
        return any(has_domain_limited_func(child, seen) for child in vars(value).values())
    if isinstance(value, (list, tuple)):
        return any(has_domain_limited_func(item, seen) for item in value)
    return False


def check_float_literals(value: Any, seen: set[int] | None = None) -> None:
    """Refuse a float constant in a calc expression outside CLICK's REAL range."""
    if seen is None:
        seen = set()
    if id(value) in seen:
        return
    seen.add(id(value))
    if isinstance(value, float):
        check_real_value(value, "calc() constant")
    elif isinstance(value, Expression):
        for child in vars(value).values():
            check_float_literals(child, seen)
    elif isinstance(value, (list, tuple)):
        for item in value:
            check_float_literals(item, seen)


def infer_calc_mode(expression: Any, dest: Tag) -> CalcModeInference:
    """Infer calc arithmetic mode and whether the expression mixes mode families."""
    tag_types: set[TagType] = set()
    _collect_calc_tag_types(expression, tag_types, set())
    tag_types.add(dest.type)

    has_hex_family = any(_calc_family_for_tag_type(tag_type) == "hex" for tag_type in tag_types)
    has_decimal_family = any(
        _calc_family_for_tag_type(tag_type) == "decimal" for tag_type in tag_types
    )

    mode: CalcMode = "hex" if has_hex_family and not has_decimal_family else "decimal"
    return CalcModeInference(
        mode=mode,
        mixed_families=has_hex_family and has_decimal_family,
        has_hex_family=has_hex_family,
        has_decimal_family=has_decimal_family,
    )


class CalcInstruction(OneShotMixin, Instruction):
    """Evaluate an arithmetic expression and store the result (CALC).

    Evaluates `expression` (which may reference any `Tag` or arithmetic
    combination thereof) and stores the result into `dest`, truncating
    to the destination type's bit width via modular wrapping.

    **Wrapping semantics:** Overflow wraps — e.g. storing 32 768 into an INT
    tag produces −32 768. This differs from `CopyInstruction`, which clamps.

    **Division by zero:** Result is forced to 0 and the division-error fault
    flag is set. The instruction completes normally.

    **Math domain error** (``sqrt`` of a negative, ``log``/``log10`` of 0 or a
    negative, ``asin``/``acos`` outside -1..1): Result is forced to 0 and the
    math-operation error flag (``system.fault.math_operation_error``, CLICK's
    SC46) is set.  The rest of the scan runs; at its end the PLC goes to STOP
    with ``system.sys.cmd_mode_stop`` (SC50) on, as a CLICK does.

    **Mode inference:**
    - Uses ``"hex"`` only when all referenced tags (plus destination) are WORD.
    - Uses ``"decimal"`` otherwise.
    - Mixed WORD/non-WORD families stay runtime-permissive and fall back to decimal.

    Args:
        expression: Python expression built from `Tag` objects and literals
            (e.g. ``DS1 + DS2 * 10``).
        dest: Destination `Tag` to write the result into.
        oneshot: When True, execute only on the rung's rising edge. Default False.
    """

    _reads = ("expression",)
    _writes = ("dest",)
    _fault_writes: tuple[str, ...] = ("fault.division_error", "fault.out_of_range")
    _conditions = ()
    _structural_fields = ("mode",)

    def __init__(
        self,
        expression: Any,
        dest: Tag,
        *,
        oneshot: bool = False,
    ):
        OneShotMixin.__init__(self, oneshot)
        check_float_literals(expression)
        self.expression = expression
        self.dest = dest
        self.mode_inference = infer_calc_mode(expression, dest)
        self.mode = self.mode_inference.mode
        if has_domain_limited_func(expression):
            # Only a calc that can hit a domain error sets SC46: the analyses
            # (PDG, prover) see the flag written where it can be.
            self._fault_writes = (*CalcInstruction._fault_writes, "fault.math_operation_error")

    @guard_oneshot_execution
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        # Evaluate expression (handles Tag, Expression, IndirectRef, literal)
        try:
            value = resolve_tag_or_value_ctx(self.expression, ctx)
        except ZeroDivisionError:
            _set_fault_division_error(ctx)
            value = 0
        except OverflowError:
            _set_fault_out_of_range(ctx)
            value = 0
        except MathDomainError:
            _set_fault_math_operation_error(ctx)
            value = 0

        # Expression division may return non-finite sentinels for divide-by-zero.
        if isinstance(value, float) and not math.isfinite(value):
            _set_fault_division_error(ctx)
            value = 0

        if _math_out_of_range_for_dest(value, self.dest, self.mode):
            _set_fault_out_of_range(ctx)

        # Truncate to destination type
        try:
            value = _truncate_to_tag_type(value, self.dest, self.mode)
        except OverflowError:
            _set_fault_out_of_range(ctx)
            value = _truncate_to_tag_type(0, self.dest, self.mode)

        # Resolve destination name (handles indirect)
        target_name = resolve_tag_name_ctx(self.dest, ctx)
        ctx.set_tag(target_name, value)
