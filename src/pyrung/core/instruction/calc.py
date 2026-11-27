"""Automatically generated module split."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, assert_never

from pyrung.core.expression import (
    ABS_OF_TYPE,
    CALC_FAULTS,
    CALC_OPS,
    REAL_CALC_OPS,
    BinaryExpr,
    CalcBinaryExpr,
    CalcUnaryExpr,
    Expression,
    LiteralExpr,
    MathFuncExpr,
    RandIntExpr,
    ShiftFuncExpr,
    TagExpr,
    UnaryExpr,
    _op_and,
    _op_div,
    _op_hex_div,
)
from pyrung.core.memory_block import BlockRange, IndirectExprRef, IndirectRef
from pyrung.core.storage import check_real_value, computed_real
from pyrung.core.tag import ImmediateRef, Tag, TagType

from .base import Instruction, OneShotMixin
from .conversions import (
    _math_out_of_range_for_dest,
    _truncate_to_tag_type,
)
from .data_transfer import _read
from .operands import (
    TAG,
    ConstArg,
    ExprArg,
    NumberInto,
    PointerArg,
    PointerRangeArg,
    RangeArg,
    Span,
    TagArg,
    TextInto,
    Value,
    ValueOperand,
    check_rules,
)
from .resolvers import (
    _raise_fault,
    _set_fault_address_error,
    _set_fault_out_of_range,
)
from .utils import guard_oneshot_execution

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext


CalcMode = Literal["decimal", "hex"]

_SOURCE = ValueOperand(arg="expression")
# A pointer or a constant has no tag type for the mode to read.
_DEST = TAG.of(
    pointers=False,
    immediate=False,
    pointer_hint="Write into a tag, then copy() that tag to the pointer.",
)


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
        # The draw's hidden DINT tag holds the result.  The bounds set the family.
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


def check_float_literals(value: Any, seen: set[int] | None = None) -> None:
    """Refuse a float constant in a Math expression outside CLICK's REAL range."""
    if seen is None:
        seen = set()
    if id(value) in seen:
        return
    seen.add(id(value))
    if isinstance(value, float):
        check_real_value(value, "math() constant")
    elif isinstance(value, Expression):
        for child in vars(value).values():
            check_float_literals(child, seen)
    elif isinstance(value, (list, tuple)):
        for item in value:
            check_float_literals(item, seen)


def _int32(value: int) -> int:
    """Cut *value* to its low 32 bits and read it as a signed Dint."""
    return ((value + 0x8000_0000) & 0xFFFF_FFFF) - 0x8000_0000


#: The binary ops whose result a hex Math keeps to 16 unsigned bits (:func:`calc_form`).
_HEX_MASKED_SYMBOLS = frozenset({"<<", "&", "|", "^"})


@dataclass(frozen=True)
class _CalcForm:
    """How :func:`calc_form` rewrites a formula: see there."""

    unsigned: bool = False
    abs_ops: bool = False
    mask_bits: bool = False
    calc_ops: bool = True
    whole_division: bool = False
    real_zeros: bool = False

    def cut(self, value: int) -> int:
        return value & 0xFFFF if self.unsigned else _int32(value)


def _same(a: Any, b: Any) -> bool:
    """Return whether bounds *a* and *b* are one constant, or one node.

    ``==`` on an expression builds a condition, not a bool, so this compares nodes by identity.
    """
    a = a.value if isinstance(a, LiteralExpr) else a
    b = b.value if isinstance(b, LiteralExpr) else b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return a is b


def _rebuild(value: Any, form: _CalcForm) -> Any:
    """Return *value* rewritten by *form*.

    Return *value* itself when nothing changes.  Otherwise return a copy of the tree with
    the changed nodes rebuilt.
    """
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
        op = value.op
        if form.calc_ops:
            op = CALC_OPS.get(op, op)
            if form.whole_division and op is _op_div:
                op = _op_hex_div
            if form.real_zeros and not (_holds_double(left) or _holds_double(right)):
                op = REAL_CALC_OPS.get(op, op)
        cls = CalcBinaryExpr if form.calc_ops else type(value)
        node = value
        if (
            left is not value.left
            or right is not value.right
            or op is not value.op
            or type(value) is not cls
        ):
            node = cls(left, right, op, value.symbol)
        if form.mask_bits and value.symbol in _HEX_MASKED_SYMBOLS:
            node = cls(node, LiteralExpr(0xFFFF), _op_and, "&")
        return node
    if isinstance(value, UnaryExpr):
        operand = _rebuild(value.operand, form)
        op = value.op
        if form.abs_ops and value.symbol == "abs" and isinstance(operand, TagExpr):
            op = ABS_OF_TYPE.get(operand.tag.type.name, op)
        if form.calc_ops and form.real_zeros and not _holds_double(operand):
            op = REAL_CALC_OPS.get(op, op)
        cls = CalcUnaryExpr if form.calc_ops else type(value)
        if operand is value.operand and op is value.op and type(value) is cls:
            return value
        return cls(operand, op, value.symbol)
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
        if form.mask_bits and value.name == "lsh":
            return BinaryExpr(node, LiteralExpr(0xFFFF), _op_and, "&")
        return node
    if isinstance(value, RandIntExpr):
        # A bound keeps its ops, and only its constants are cut.  CLICK's editor takes
        # no formula as a bound (math-arg-expression).
        form = replace(form, calc_ops=False)
        lo = _rebuild(value.lo, form)
        hi = _rebuild(value.hi, form)
        if lo is value.lo and hi is value.hi:
            return value
        # A hex Math's cut is its own draw only where it differs from a decimal Math's.
        signed = _CalcForm(calc_ops=False)
        unsigned = form.unsigned and not (
            _same(_rebuild(value.lo, signed), lo) and _same(_rebuild(value.hi, signed), hi)
        )
        return value.with_cut_bounds(lo, hi, unsigned=unsigned)
    return value


def _holds_double(value: Any) -> bool:
    """Return whether *value* is a Real tag or a float constant, which always holds a double."""
    if isinstance(value, LiteralExpr):
        return isinstance(value.value, float)
    return isinstance(value, TagExpr) and value.tag.type is TagType.REAL


def may_be_text(value: Any) -> bool:
    """Return True if *value* reads text, a Char tag or a pointer into Char tags anywhere.

    Text that an expression builds, such as ``TXT1 + TXT2``, is text too.
    """
    if isinstance(value, ImmediateRef):
        value = value.value
    if isinstance(value, (IndirectRef, IndirectExprRef)):
        return value.block.type.name == "CHAR"
    if isinstance(value, Tag):
        return value.type.name == "CHAR"
    if isinstance(value, LiteralExpr):
        return may_be_text(value.value)
    if isinstance(value, Expression):
        # An operator's symbol, such as "+", is a str but not an operand.
        parts = [part for part in vars(value).values() if not isinstance(part, str)]
        return any(may_be_text(part) for part in parts)
    if isinstance(value, (list, tuple)):
        return any(may_be_text(part) for part in value)
    return isinstance(value, str)


def cut_int_constants(value: Any) -> Any:
    """Return *value* (a Math expression) with each integer constant cut to 32 bits, signed.

    This is what a decimal Math runs (:func:`calc_form`), including the bounds of a
    ``randint()``.  Only a constant past a Dint's range changes.
    """
    return _rebuild(value, _CalcForm(calc_ops=False))


def calc_form(expression: Any, mode: CalcMode, *, into_real: bool = False) -> Any:
    """Return *expression* as a CLICK Math in *mode* runs it (measured, 2026-11-11).

    - A decimal Math keeps each integer constant whole, whatever the destination
      (``DD1 / 65536`` divides by 65536).  CLICK's editor refuses a constant past
      a Dint's range.  pyrung cuts one to its low 32 bits, as a CLICK ran a pasted one.
    - A hex Math cuts each integer constant to 16 bits, unsigned (the export's
      ``1170h`` for 70000).
    - The constant bounds of a ``randint()`` are cut as the other constants are, so
      ``randint(70000, 70002)`` draws 70000..70002 in decimal mode and 4464..4466 in
      hex mode.
    - Each ``BinaryExpr`` and ``UnaryExpr`` becomes a ``CalcBinaryExpr`` or
      ``CalcUnaryExpr``, whose integer steps past 2 ** 53 are doubles.
    - A hex Math truncates the result of each ``/`` toward zero
      (:func:`~pyrung.core.expression._op_hex_div`).  So ``7h / 2h * 2h`` stores 6h
      (read, 2026-11-26).  A decimal ``/`` keeps the fraction.
    - ``%`` truncates.  A ``%`` by 0 is 0 and sets SC46.  ``0 ^ 0`` is 0 with no
      flag, and 0 to a negative power is +Inf
      (:data:`~pyrung.core.expression.CALC_OPS`).  Outside a Math they stay Python's.
    - ``abs()`` of an Int or Dint tag cannot return the bottom of its range.
      ``abs(DS1)`` with DS1 = -32768 is 0 and sets SC43.  ``abs(DD1)`` with
      DD1 = -2147483648 does the same.  The rule depends on the operand's type, so it
      also applies with a wider destination: ``abs(DS1)`` into a Dint read 0 on a CLICK.
    - In hex mode a left shift keeps 16 bits: the bits shifted out are gone, so
      ``lsh(DH1, 1)`` of 0x8001 stores 0x0002 and raises no SC43 (read).  They
      are gone mid-formula too (``rsh(lsh(DH1, 1), 1)`` is 1): pyrung's choice,
      not yet read.
    - In hex mode AND, OR and XOR each give 16 unsigned bits, and a later step reads
      those 16 bits.  With DH1 = 2h, ``- DH1 OR 0h`` stores FFFEh and
      ``( ( 0h - DH1 ) OR 0h ) + 1h`` stores FFFFh.  Neither raises SC43 (read,
      2026-11-26).  An operand past 16 bits gets the same cut, with no SC43.  So
      ``( DH1 * DH2 ) OR 0h`` with 100h and 100h stores 0000h (read, 2026-11-26).
    - With *into_real*, for a decimal Math into a Real, a minus sign, ``*`` and MOD give a
      0 the sign that a double gives it.  A CLICK computes a DS of 0 as the double +0.0,
      so ``- DS1`` with DS1 = 0 stores -0.0 and ``DS1 * DS2`` with 0 and -5 stores -0.0
      (read, 2026-11-26).  A step on a Real tag or a float constant needs no other op,
      because Python computes it in doubles.

    Return *expression* itself when nothing changes (no operation in it).
    """
    if mode == "hex":
        form = _CalcForm(unsigned=True, mask_bits=True, whole_division=True)
    else:
        form = _CalcForm(abs_ops=True, real_zeros=into_real)
    return _rebuild(expression, form)


def infer_calc_mode(expression: Any, dest: Tag) -> CalcModeInference:
    """Infer Math arithmetic mode and whether the expression mixes mode families."""
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
    """Evaluate an arithmetic expression and store the result (Math).

    Evaluates `expression` (which may reference any `Tag` or arithmetic
    combination thereof) and stores the result into `dest`, truncating
    to the destination type's bit width via modular wrapping.

    **Wrapping semantics:** Overflow wraps — e.g. storing 32 768 into an INT
    tag produces −32 768. This differs from `CopyInstruction`, which clamps.

    **Math errors are the failing operation's:** it is 0, its flag is set, and
    the formula carries on, as a CLICK's Math does (``DD1 / DD2 + 5`` with DD2 =
    0 stores 5, into a REAL 5.0).  A division by zero sets the division-error
    flag (``system.fault.division_error``, CLICK's SC40).  The instruction
    completes normally.

    **Flags:** every Math the scan reaches clears SC40, SC43, SC44 and SC46 as
    it starts, before it reads.  That holds on a false rung and for a one-shot
    that does not fire too.  A flag left on carries into the next scan.

    **REAL overflow:** a result past +/-3.4028235E+38 stores +/-Inf and sets
    the out-of-range flag (``system.fault.out_of_range``, CLICK's SC43), as a
    CLICK's float32 Math does.  An Inf operand follows IEEE 754 (``Inf / 2`` is
    Inf, ``Inf - Inf`` is NaN); a REAL result of +/-Inf sets the flag too, and
    NaN is stored with no flag.  Into an INT, DINT or WORD, a result of +/-Inf
    stores 0 and sets the flag, and NaN stores 0 with no flag.  A bitwise or
    shift operation on an Inf or NaN operand is 0 and sets the flag, whatever
    the destination.  The range is checked on the stored
    result only: a step of the formula is a double, as on a CLICK (an integer
    step past 2 ** 53 is rounded to one, and ``**`` past the double range is
    +/-Inf).

    **Math domain error** (``sqrt`` of a negative, ``log``/``log10`` of 0 or a
    negative, ``asin``/``acos`` outside -1..1, a negative base to a fractional
    power, a MOD by 0): the operation is 0 and the math-operation error flag
    (``system.fault.math_operation_error``, CLICK's SC46) is set until the next
    Math clears it, as SC40 is.  The PLC stays in RUN, as a
    CLICK does (its help says SC50 turns on and the PLC goes to STOP; the
    hardware does not).  ``0 ** 0`` is 0 and sets no flag (into a REAL 0.0).
    0 to a negative power is +Inf, so into an INT, DINT or WORD the formula
    stores 0 and sets the out-of-range flag.  MOD truncates: the
    result has the dividend's sign.

    **Constants:** a decimal Math keeps each integer constant whole, as a CLICK
    Math does, so ``math(DD1 / 65536, DS1)`` divides by 65536.  A hex Math cuts
    each one to 16 bits, unsigned.  See :func:`calc_form`.  ``expression`` holds
    the formula as it runs.

    The builder parses both operands (:mod:`pyrung.core.instruction.operands`).
    ``source_expression`` holds the formula as it was written, as a ``Value`` or a
    ``Span``, and the CLICK export writes it.  ``dest`` holds a ``TagArg``.

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
        "fault.math_operation_error",
    )
    _fault_follows = _fault_writes
    # A CLICK Math clears its four flags even on a false rung, and even as a one-shot
    # that does not fire (``flag_steps``, ``flag_reads``).
    _fault_settles_idle = True
    _conditions = ()
    _structural_fields = ("mode",)
    _operands = {"source_expression": _SOURCE, "dest": _DEST}
    _operand_rules = (
        NumberInto(value="source_expression", dest="dest", text=True),
        TextInto(dest="dest", writes="number"),
    )
    _call_operands = (("expression", "source_expression"), ("dest", "dest"))

    def __init__(
        self,
        expression: Any,
        dest: Any,
        *,
        oneshot: bool = False,
    ):
        OneShotMixin.__init__(self, oneshot)
        self.source_expression: Value | Span = _SOURCE.parse("math()", "expression", expression)
        self.dest: TagArg = _DEST.parse_tag("math()", "dest", dest)
        check_rules(
            self._operands,
            self._operand_rules,
            "math()",
            {"source_expression": expression, "dest": dest},
        )
        check_float_literals(expression)
        self.mode_inference = infer_calc_mode(expression, self.dest.tag)
        self.mode = self.mode_inference.mode
        # Only a Real shows the sign of a 0.  Text keeps Python's ops, so "5" * 2 is "55".
        into_real = self.dest.tag.type is TagType.REAL and not may_be_text(expression)
        # The analyses and the code generator read this formula, the one that the mode
        # runs.  It is what calc_form() returns, not a parse result.
        self.expression = calc_form(expression, self.mode, into_real=into_real)

    def check_operands(self, call: str | None = None) -> None:
        """Do nothing, because the builder parsed every operand."""

    @guard_oneshot_execution
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        # A failing operation gives 0 and notes its flag in *faults* (CALC_FAULTS).  The
        # formula carries on.  A division by zero gives 0 and not Inf, so a result that
        # is not finite comes from an Inf or NaN operand, or from an overflow.
        faults: list[str] = []
        token = CALC_FAULTS.set(faults)
        try:
            value = self._read(ctx)
        except IndexError:
            # An indexed source below its block sets the address-error flag and writes
            # nothing, as copy(), the kernel and CircuitPython do.  The flag comes after
            # the flags of the failing operations in its address.
            value = None
        finally:
            CALC_FAULTS.reset(token)
        for name in faults:
            _raise_fault(ctx, name)
        if value is None:
            _set_fault_address_error(ctx)
            return

        dest = self.dest.tag
        if _math_out_of_range_for_dest(value, dest, self.mode):
            _set_fault_out_of_range(ctx)

        # Truncate to destination type
        try:
            value = computed_real(_truncate_to_tag_type(value, dest, self.mode))
        except OverflowError:
            _set_fault_out_of_range(ctx)
            value = _truncate_to_tag_type(0, dest, self.mode)

        ctx.set_tag(dest.name, value)

    def _read(self, ctx: ScanContext) -> Any:
        """Return the value of the formula on this scan.

        A formula or a constant is read from ``expression``, the form that the mode runs.
        The mode never changes a tag, a pointer or a range.
        """
        source = self.source_expression
        match source:
            case TagArg() | PointerArg():
                return _read(source, ctx)
            case ExprArg():
                return self.expression.evaluate(ctx)
            case ConstArg():
                return self.expression
            case RangeArg(span=span) | PointerRangeArg(span=span):
                # NumberInto lets a range through only into a Bool.  The store converts
                # the range object to a Bool.
                return span
            case _:
                assert_never(source)
