"""Automatically generated module split."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.expression import (
    ABS_OF_TYPE,
    CALC_OPS,
    DOMAIN_LIMITED_MATH_FUNCS,
    STRICT_DIVISION,
    BinaryExpr,
    Expression,
    LiteralExpr,
    MathDomainError,
    MathFuncExpr,
    QuietMathError,
    RandIntExpr,
    ShiftFuncExpr,
    TagExpr,
    UnaryExpr,
    _op_and,
    may_be_float,
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
    _set_fault_address_error,
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
    """True when *value* (a calc expression) can raise
    :class:`~pyrung.core.expression.MathDomainError`: a math function with a
    domain (``sqrt``, ``log``, ...), a ``**`` whose exponent may be fractional
    (``(-8.0) ** 0.5`` has no real value), either of those in a ``randint``
    bound, or a ``randint`` whose bounds can be equal (equal constants, or
    any tag bound)."""
    if seen is None:
        seen = set()
    if id(value) in seen:
        return False
    seen.add(id(value))
    if isinstance(value, RandIntExpr):
        # A bare Real tag as a bound leaves both bounds unread.  Equal bounds are
        # the error themselves, and tag bounds may hold one value.  A computed
        # bound is read in the calc too, where sqrt(N) can raise.
        if value.real_bounds:
            return False
        return (
            value.equal_bounds
            or value.reads_bounds
            or has_domain_limited_func(value.lo, seen)
            or has_domain_limited_func(value.hi, seen)
        )
    if isinstance(value, MathFuncExpr) and value.name in DOMAIN_LIMITED_MATH_FUNCS:
        return True
    if isinstance(value, BinaryExpr) and value.symbol == "**" and may_be_float(value.right):
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


def _int16(value: int) -> int:
    """*value* cut to its low 16 bits, read as a signed Int."""
    return ((value + 0x8000) & 0xFFFF) - 0x8000


@dataclass(frozen=True)
class _CalcForm:
    """How :func:`calc_form` rewrites a formula: see there."""

    unsigned: bool = False
    rand: bool = True
    abs_ops: bool = False
    mask_shifts: bool = False
    calc_ops: bool = True

    def cut(self, value: int) -> int:
        return value & 0xFFFF if self.unsigned else _int16(value)


def _same(a: Any, b: Any) -> bool:
    """Whether bounds *a* and *b* are one constant, or one node (``==`` on an
    expression builds a condition, not a bool)."""
    a = a.value if isinstance(a, LiteralExpr) else a
    b = b.value if isinstance(b, LiteralExpr) else b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return a is b


def _rebuild(value: Any, form: _CalcForm) -> Any:
    """*value* rewritten by *form*: itself when nothing changes, else a copy of the
    tree with the changed nodes rebuilt."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return form.cut(value)
    if isinstance(value, LiteralExpr):
        cut = _rebuild(value.value, form)
        return value if cut == value.value else LiteralExpr(cut)
    if isinstance(value, BinaryExpr):
        left = _rebuild(value.left, form)
        right = _rebuild(value.right, form)
        op = CALC_OPS.get(value.op, value.op) if form.calc_ops else value.op
        node = value
        if left is not value.left or right is not value.right or op is not value.op:
            node = BinaryExpr(left, right, op, value.symbol)
        if form.mask_shifts and value.symbol == "<<":
            node = BinaryExpr(node, LiteralExpr(0xFFFF), _op_and, "&")
        return node
    if isinstance(value, UnaryExpr):
        operand = _rebuild(value.operand, form)
        op = value.op
        if form.abs_ops and value.symbol == "abs" and isinstance(operand, TagExpr):
            op = ABS_OF_TYPE.get(operand.tag.type.name, op)
        if operand is value.operand and op is value.op:
            return value
        return UnaryExpr(operand, op, value.symbol)
    if isinstance(value, MathFuncExpr):
        operand = _rebuild(value.operand, form)
        if operand is value.operand:
            return value
        return type(value)(operand, value.func, value.name)
    if isinstance(value, ShiftFuncExpr):
        shifted = _rebuild(value.value, form)
        count = _rebuild(value.count, form)
        node = value
        if shifted is not value.value or count is not value.count:
            node = type(value)(shifted, count, value.func, value.name)
        if form.mask_shifts and value.name == "lsh":
            return BinaryExpr(node, LiteralExpr(0xFFFF), _op_and, "&")
        return node
    if isinstance(value, RandIntExpr):
        if not form.rand:
            return value
        # A bound keeps its ops: only its constants are cut (CLICK's editor takes
        # no formula as a bound, CLK_CALC_ARG_NOT_PLAIN).
        form = replace(form, calc_ops=False)
        lo = _rebuild(value.lo, form)
        hi = _rebuild(value.hi, form)
        if lo is value.lo and hi is value.hi:
            return value
        # A Word's cut is its own draw only where it differs from an Int's.
        signed = _CalcForm(calc_ops=False)
        unsigned = form.unsigned and not (
            _same(_rebuild(value.lo, signed), lo) and _same(_rebuild(value.hi, signed), hi)
        )
        return value.with_cut_bounds(lo, hi, unsigned=unsigned)
    return value


def cut_int_constants(value: Any) -> Any:
    """*value* (a calc expression) with each integer constant cut to 16 bits, signed.

    What a decimal Math into an Int runs (:func:`calc_form`), the bounds of a
    ``randint()`` included: ``randint(70000, 70002)`` draws 4464..4466.
    """
    return _rebuild(value, _CalcForm(calc_ops=False))


def calc_form(expression: Any, mode: CalcMode, dest_type: TagType | None) -> Any:
    """*expression* as a CLICK Math into *dest_type* runs it (measured, 2026-11-05).

    - Each integer constant is cut to 16 bits whatever the destination: signed in
      decimal mode (40000 is -25536, so ``DD1 * 40000`` is ``DD1 * -25536``, and
      ``DD1 / 65536`` divides by 0), unsigned in hex mode (the export's ``1170h``
      for 70000).  Operands read from memory keep their width.
    - The constant bounds of a ``randint()`` are cut into an Int and a Word, and
      kept whole into a Dint or a Real (``randint(70000, 70002)`` into a Dint draws
      70000..70002).
    - ``%`` truncates and ``**`` takes ``0 ^ 0`` and ``0 ^ -1`` for an error that
      stores 0 with no flag (:data:`~pyrung.core.expression.CALC_OPS`); outside
      ``calc`` they stay Python's.
    - ``abs()`` of an Int or Dint tag cannot take the bottom of its range:
      ``abs(DS1)`` with DS1 = -32768 into an Int stores 0 and sets SC43 (read).
      Keyed on the operand's type, so into a wider destination too, and the
      same for a Dint at -2147483648: pyrung's choice, not yet read.
    - In hex mode a left shift keeps 16 bits: the bits shifted out are gone, so
      ``lsh(DH1, 1)`` of 0x8001 stores 0x0002 and raises no SC43 (read).  They
      are gone mid-formula too (``rsh(lsh(DH1, 1), 1)`` is 1): pyrung's choice,
      not yet read.

    Returns *expression* itself when nothing changes.
    """
    if mode == "hex":
        form = _CalcForm(unsigned=True, mask_shifts=True)
    else:
        form = _CalcForm(rand=dest_type == TagType.INT, abs_ops=True)
    return _rebuild(expression, form)


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


def _dest_type(dest: Any) -> TagType | None:
    if isinstance(dest, Tag):
        return dest.type
    block = getattr(dest, "block", None)
    return getattr(block, "type", None)


class CalcInstruction(OneShotMixin, Instruction):
    """Evaluate an arithmetic expression and store the result (CALC).

    Evaluates `expression` (which may reference any `Tag` or arithmetic
    combination thereof) and stores the result into `dest`, truncating
    to the destination type's bit width via modular wrapping.

    **Wrapping semantics:** Overflow wraps — e.g. storing 32 768 into an INT
    tag produces −32 768. This differs from `CopyInstruction`, which clamps.

    **Division by zero:** Result is forced to 0 and the division-error fault
    flag is set. The instruction completes normally.

    **REAL overflow:** a result past +/-3.4028235E+38 stores +/-Inf and sets
    the out-of-range flag (``system.fault.out_of_range``, CLICK's SC43), as a
    CLICK's float32 Math does.  An Inf operand follows IEEE 754 (``Inf / 2`` is
    Inf, ``Inf - Inf`` is NaN); a REAL result of +/-Inf sets the flag too, and
    NaN is stored with no flag.  Into an INT, DINT or WORD, a result of +/-Inf
    stores 0 and sets the flag, and NaN stores 0 with no flag.  A bitwise or
    shift operation on an Inf or NaN operand stores 0 and sets the flag,
    whatever the destination.  The range is checked on the stored
    result only: a step of the formula is a double, as on a CLICK (an integer
    step past 2 ** 53 is rounded to one, and ``**`` past the double range is
    +/-Inf).

    **Math domain error** (``sqrt`` of a negative, ``log``/``log10`` of 0 or a
    negative, ``asin``/``acos`` outside -1..1, a negative base to a fractional
    power): Result is forced to 0 and the math-operation error flag
    (``system.fault.math_operation_error``, CLICK's SC46) is set for the rest of
    the scan; the next scan starts with it off.  The PLC stays in RUN, as a
    CLICK does (its help says SC50 turns on and the PLC goes to STOP; the
    hardware does not).  ``0 ** 0``, ``0 ** -1`` and a MOD by 0 store 0 and set
    no flag.  MOD truncates: the result has the dividend's sign.

    **Constants:** each integer constant in `expression` is cut to 16 bits, as
    a CLICK Math stores it, whatever the destination: ``calc(DD1 / 65536, DD2)``
    divides by 0 (writes 0 and sets the division-error flag), and
    ``calc(DD1 * 40000, DD2)`` multiplies by -25536.  Tags keep their width
    (``calc(DD1 / DD2, DS1)`` with ``DD2 = 65536`` divides by 65536).  See
    :func:`calc_form`.  ``expression`` holds the formula as it runs;
    ``source_expression`` the one written, which the CLICK export writes.

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
    _fault_writes: tuple[str, ...] = (
        "fault.division_error",
        "fault.out_of_range",
        "fault.address_error",
    )
    _fault_follows = _fault_writes
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
        self.source_expression = expression
        self.dest = dest
        self.mode_inference = infer_calc_mode(expression, dest)
        self.mode = self.mode_inference.mode
        self.expression = calc_form(expression, self.mode, _dest_type(dest))
        if has_domain_limited_func(self.expression):
            # Only a calc that can hit a domain error sets SC46: the analyses
            # (PDG, prover) see the flag written where it can be.
            self._fault_writes = (*CalcInstruction._fault_writes, "fault.math_operation_error")

    @guard_oneshot_execution
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        # Evaluate expression (handles Tag, Expression, IndirectRef, literal).
        # A division by zero raises here (STRICT_DIVISION), so a result that is
        # not finite comes from an Inf or NaN operand, or an overflow.
        token = STRICT_DIVISION.set(True)
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
        except QuietMathError:
            # 0 ** 0, 0 ** -1, a MOD by 0: 0, and a CLICK sets no flag.
            value = 0
        except IndexError:
            # An indexed source below its block: the address-error flag,
            # nothing written (as copy(), the kernel and CircuitPython do).
            _set_fault_address_error(ctx)
            return
        finally:
            STRICT_DIVISION.reset(token)

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
