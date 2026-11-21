"""Check each operand of an instruction when the program is built.

An instruction class declares what each of its fields accepts in ``_operands``.  It
declares the rules that tie two fields together in ``_operand_rules``::

    _operands = {
        "bit_block": RANGE.of(TagType.BOOL),
        "dest": TAG.of(TagType.INT, TagType.WORD, TagType.DINT, TagType.REAL),
    }
    _operand_rules = (FitsRegister(bits="bit_block", register="dest"),)

``Rung.add_instruction`` calls ``Instruction.check_operands()``.  The check raises an
error that names the operand and says what to pass.  If ``__init__`` reads something
from an operand, such as the type of a destination, the builder calls
``Instruction.check_values()`` first.  The check then refuses a wrong operand before
``__init__`` raises ``AttributeError``.

Each kind of operand is a subclass of ``Operand``.  A kind refuses an operand when no
input values let the instruction write a result with it.  Each scan with such an
operand raises, sets a fault flag, or does nothing.  A kind judges one operand, and a
rule judges two together.  So an operand that writes nothing only with some converter
or operator still builds.

A kind also refuses these operands, which can write a result:

- an operand that its builder refused before this check existed, such as an Int tag
  as the ``found`` of ``search()``;
- a Real constant as a ``forloop()`` count, such as 5.0, because CLICK's editor
  refuses one.

A kind does not check the length of a range with pointers, because the pointers are
read at scan time.  The scan checks it.

``copy()``, ``fill()`` and ``blockcopy()`` parse their operands instead.  Their builder
calls ``parse()`` on each declaration, which returns one of the results below or raises
the refusal.  The instruction then holds only results.  ``execute()``, the code
generator and the CLICK export match on them and never test the raw value again.  A
reader that takes the operands of every instruction calls :func:`as_authored` on each.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, ClassVar, assert_never

from pyrung.core.condition import Condition
from pyrung.core.expression import Expression, LiteralExpr, RandIntExpr
from pyrung.core.memory_block import BlockRange, IndirectBlockRange, IndirectExprRef, IndirectRef
from pyrung.core.structure import InstanceView
from pyrung.core.tag import ImmediateRef, Tag, TagType

if TYPE_CHECKING:
    from pyrung.core.memory_block import Block

_NUMBER_TYPES = frozenset({TagType.INT, TagType.DINT, TagType.WORD, TagType.REAL})


# The results of a parse.  ``eq=False`` keeps identity equality, because ``Tag.__eq__``
# builds a condition.  The results keep a ``__dict__``, so the walkers that follow
# ``vars()`` through a program still reach the tags inside them.


@dataclass(frozen=True, eq=False)
class TagArg:
    """One tag.  *immediate* is True when the author wrapped it in ``immediate()``.

    *wrapper* is the ``ImmediateRef`` that the author passed.  :func:`held` returns it.  A
    reader that keys on ``id()`` then sees one object for as long as the instruction lives.
    """

    tag: Tag
    immediate: bool = False
    wrapper: ImmediateRef | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        _check_wrapper(self, self.tag)


@dataclass(frozen=True, eq=False)
class PointerArg:
    """One element that a pointer picks when the scan runs, such as ``DS[Ptr]``."""

    ref: IndirectRef | IndirectExprRef

    @property
    def block(self) -> Block:
        """The block that the pointer indexes."""
        return self.ref.block


@dataclass(frozen=True, eq=False)
class ConstArg:
    """A constant, such as 5, 1.5, True or "AB".

    *value* can be any other object that the field accepts, such as a structure that
    ``copy()`` writes into a TXT.  The scan stores it as it is.
    """

    value: object


@dataclass(frozen=True, eq=False)
class ExprArg:
    """An expression that the scan evaluates, such as ``DS1 + 1``."""

    expr: Expression


@dataclass(frozen=True, eq=False)
class RangeArg:
    """A range with fixed ends, such as ``DS.select(1, 10)``.

    *immediate* and *wrapper* work as they do on :class:`TagArg`.
    """

    span: BlockRange
    immediate: bool = False
    wrapper: ImmediateRef | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        _check_wrapper(self, self.span)


@dataclass(frozen=True, eq=False)
class PointerRangeArg:
    """A range whose ends the scan reads, such as ``DS.select(Lo, Hi)``."""

    span: IndirectBlockRange


def _check_wrapper(arg: TagArg | RangeArg, held_object: Tag | BlockRange) -> None:
    """Build the ``ImmediateRef`` of an immediate *arg* that was made without one."""
    if arg.wrapper is not None and not arg.immediate:
        raise ValueError("A wrapper needs immediate=True.")
    if arg.immediate and arg.wrapper is None:
        object.__setattr__(arg, "wrapper", ImmediateRef(held_object))


Value = TagArg | PointerArg | ConstArg | ExprArg
"""What a field that reads one value holds, such as the source of ``copy()``."""
Dest = TagArg | PointerArg
"""What a field that writes one register holds, such as the destination of ``copy()``."""
Span = RangeArg | PointerRangeArg
"""What a field that takes a range holds, such as the destination of ``fill()``."""


def held(arg: Value | Span) -> Any:
    """Return the object that *arg* holds, as the author passed it.

    A tag or a range in ``immediate()`` comes back in the ``ImmediateRef`` that the
    author passed.  Each call on one operand returns the same object.  The readers that
    the other instructions share, such as ``operand_name()`` and the program walkers,
    still take the authored object.  They call this function on a parsed operand before
    they read it.
    """
    match arg:
        case TagArg(tag=tag, wrapper=wrapper):
            return tag if wrapper is None else wrapper
        case RangeArg(span=span, wrapper=wrapper):
            return span if wrapper is None else wrapper
        case PointerArg(ref=ref):
            return ref
        case PointerRangeArg(span=span):
            return span
        case ExprArg(expr=expr):
            return expr
        case ConstArg(value=value):
            return value
        case _:
            assert_never(arg)


def inner(arg: Value | Span) -> Any:
    """Return the object that *arg* holds, without its ``immediate()``."""
    match arg:
        case TagArg(tag=tag):
            return tag
        case RangeArg(span=span) | PointerRangeArg(span=span):
            return span
        case PointerArg(ref=ref):
            return ref
        case ExprArg(expr=expr):
            return expr
        case ConstArg(value=value):
            return value
        case _:
            assert_never(arg)


PARSED = (TagArg, PointerArg, ConstArg, ExprArg, RangeArg, PointerRangeArg)
"""Every result type, for a shared reader that tests whether it holds a parsed operand."""


def as_authored(value: Any) -> Any:
    """Return the authored object for a parsed operand, and any other value unchanged.

    A reader that takes the operands of every instruction calls this function on each
    one.  It then reads a Copy's operand as it reads any other instruction's.
    """
    return held(value) if isinstance(value, PARSED) else value


def parse_value(value: Any) -> Value | Span:
    """Return the result for *value*, which a declaration has accepted."""
    bare, wrapper = _split(value)
    if isinstance(bare, Tag):
        return TagArg(bare, wrapper is not None, wrapper)
    if isinstance(bare, BlockRange):
        return RangeArg(bare, wrapper is not None, wrapper)
    # immediate() wraps only a tag or a range with fixed ends.
    if isinstance(bare, (IndirectRef, IndirectExprRef)):
        return PointerArg(bare)
    if isinstance(bare, IndirectBlockRange):
        return PointerRangeArg(bare)
    if _is_expression(bare):
        return ExprArg(bare)
    return ConstArg(bare)


def _split(value: Any) -> tuple[Any, ImmediateRef | None]:
    """Return the operand inside ``immediate()``, and the ``ImmediateRef`` or None."""
    if isinstance(value, ImmediateRef):
        return value.value, value
    return value, None


def unwrap(value: Any) -> tuple[Any, bool]:
    """Return the operand inside ``immediate()``, and whether *value* was wrapped in it."""
    if isinstance(value, ImmediateRef):
        return value.value, True
    return value, False


def operand_type(value: Any) -> TagType | None:
    """Return the type of *value* if it is a tag or a pointer.  Otherwise return None."""
    if isinstance(value, Tag):
        return value.type
    if isinstance(value, (IndirectRef, IndirectExprRef)):
        return value.block.type
    return None


def _is_range(value: Any) -> bool:
    return isinstance(value, (BlockRange, IndirectBlockRange))


@dataclass(frozen=True)
class Operand:
    """What one field of an instruction accepts.

    Attributes:
        arg: The name of the builder's argument that sets the field, if it differs
            from the field's name.  A refusal names the argument.
        immediate: Whether the field accepts an operand wrapped in ``immediate()``.
        hint: A sentence that a refusal adds after it says what to pass.
    """

    arg: str = ""
    immediate: bool = True
    hint: str = ""

    def fits(self, value: Any) -> bool:
        """Return True if the field accepts *value*.

        ``accepts()`` calls this method with the operand taken out of ``immediate()``.
        """
        raise NotImplementedError

    def wanted(self) -> str:
        """Return what the field accepts, as in "one Int tag, or a pointer to one"."""
        raise NotImplementedError

    def accepts(self, value: Any) -> bool:
        """Return True if the field accepts *value*."""
        inner, wrapped = unwrap(value)
        if wrapped and not self.immediate:
            return False
        return self.fits(inner)

    def refusal(self, call: str, arg: str, value: Any) -> Exception | None:
        """Return the error that refuses *value* as *arg* of *call*, or None if it fits.

        *call* is the call that the author wrote, as in ``"pack_bits()"``.
        """
        if self.accepts(value):
            return None
        return TypeError(self._message(call, f"{describe(value)} as {arg}", value))

    def _refuse(self, call: str, arg: str, value: Any) -> None:
        error = self.refusal(call, arg, value)
        if error is not None:
            raise error

    def _message(self, call: str, got: str, value: Any) -> str:
        wanted = self.wanted()
        if isinstance(value, ImmediateRef) and not self.immediate and self.fits(value.value):
            wanted += ", without immediate()"
        message = f"{call} got {got}. Pass {wanted}."
        return f"{message} {self.hint}" if self.hint else message


@dataclass(frozen=True)
class TagOperand(Operand):
    """One tag, or a pointer to one, such as ``DS[Ptr]``.

    Attributes:
        types: The tag types that the field accepts.  Empty accepts every type.
        pointers: Whether the field accepts a pointer.
        pointer_hint: A sentence that a refusal of a pointer adds, in place of *hint*.
    """

    types: tuple[TagType, ...] = ()
    pointers: bool = True
    pointer_hint: str = ""

    def of(self, *types: TagType, **changes: Any) -> TagOperand:
        """Return a copy of this operand that accepts only tags of *types*.

        Each keyword in *changes* sets another attribute of the copy, as in
        ``arg="target"``.
        """
        return replace(self, types=types, **changes)

    def fits(self, value: Any) -> bool:
        if not self.pointers and not isinstance(value, Tag):
            return False
        tag_type = operand_type(value)
        return tag_type is not None and (not self.types or tag_type in self.types)

    def wanted(self) -> str:
        pointer = ", or a pointer to one" if self.pointers else ""
        return f"one {_types_text(self.types)}tag{pointer}"

    def refusal(self, call: str, arg: str, value: Any) -> Exception | None:
        """Return the error that refuses *value*, or None if it fits."""
        inner = unwrap(value)[0]
        if self.pointer_hint and isinstance(inner, (IndirectRef, IndirectExprRef)):
            return replace(self, hint=self.pointer_hint, pointer_hint="").refusal(call, arg, value)
        return super().refusal(call, arg, value)

    def parse(self, call: str, arg: str, value: Any) -> Dest:
        """Return *value* as a tag or a pointer, or raise the error that refuses it.

        *call* is the call that the author wrote, and *arg* names the argument.
        """
        self._refuse(call, arg, value)
        bare, wrapper = _split(value)
        if isinstance(bare, Tag):
            return TagArg(bare, wrapper is not None, wrapper)
        return PointerArg(bare)


@dataclass(frozen=True)
class RangeOperand(Operand):
    """A range from ``.select()``.

    A range with pointers, such as ``DS.select(Lo, Hi)``, has its length only when the
    scan runs.  So the scan checks its length.

    Attributes:
        types: The tag types that the field accepts.  Empty accepts every type.
        count: The number of tags that the range must hold, or ``None`` for any number.
        example: A range that the field accepts, which a refusal shows.
    """

    types: tuple[TagType, ...] = ()
    count: int | None = None
    example: str = ""

    def of(self, *types: TagType, **changes: Any) -> RangeOperand:
        """Return a copy of this operand that accepts only ranges of *types*.

        Each keyword in *changes* sets another attribute of the copy, as in ``count=2``.
        """
        return replace(self, types=types, **changes)

    def fits(self, value: Any) -> bool:
        if not _is_range(value):
            return False
        if self.count is not None and isinstance(value, BlockRange) and len(value) != self.count:
            return False
        return not self.types or value.block.type in self.types

    def wanted(self) -> str:
        count = "" if self.count is None else f"{self.count} "
        example = f", such as {self.example}" if self.example else ""
        return f"a range of {count}{_types_text(self.types)}tags from .select(){example}"

    def refusal(self, call: str, arg: str, value: Any) -> Exception | None:
        """Return the error that refuses *value*, or None if it fits.

        A range of the wrong length raises ``ValueError``.  Anything else raises
        ``TypeError``.
        """
        if self.accepts(value):
            return None
        counted = self.count is not None
        got = f"{describe(value, count=counted)} as {arg}"
        error = ValueError if counted and replace(self, count=None).accepts(value) else TypeError
        return error(self._message(call, got, value))

    def parse(self, call: str, arg: str, value: Any) -> Span:
        """Return *value* as a range, or raise the error that refuses it.

        *call* is the call that the author wrote, and *arg* names the argument.
        """
        self._refuse(call, arg, value)
        bare, wrapper = _split(value)
        if isinstance(bare, BlockRange):
            return RangeArg(bare, wrapper is not None, wrapper)
        return PointerRangeArg(bare)


@dataclass(frozen=True)
class ValueOperand(Operand):
    """One value, such as a tag, a pointer to one, a constant or an expression.

    None is not a value.  Nor is a condition such as ``DS1 > 5``, or an expression with
    a part that the scan cannot read, such as ``DS1 + None``.  What else the field
    accepts can depend on the destination, and ``NumberInto`` checks that.

    The field accepts a range.  ``math(ds.select(1, 2), c[3])`` writes True into C3.
    ``copy()`` and ``fill()`` refuse a range themselves.
    """

    # Whether a list or a tuple inside an expression is a bad part.
    # ``copy(ds[1] * (1, 2), c[3])`` writes True into C3, so a value accepts one.
    _lists: ClassVar[bool] = False

    def fits(self, value: Any) -> bool:
        if value is None or _is_condition(value):
            return False
        if _is_range(value):
            return True
        return _bad_part(value, lists=self._lists) is _NO_PART

    def wanted(self) -> str:
        return "one tag, a pointer to one, a constant or an expression"

    def refusal(self, call: str, arg: str, value: Any) -> Exception | None:
        """Return the error that refuses *value*, or None if it fits.

        For an expression with a bad part, the message names the part.
        """
        if self.accepts(value):
            return None
        part = _bad_part(unwrap(value)[0], lists=self._lists)
        if part is _NO_PART:
            return TypeError(self._message(call, f"{describe(value)} as {arg}", value))
        return TypeError(
            f"{call} got {describe(part)} inside the {arg}. "
            "Pass a number, a tag or an expression in its place."
        )

    def parse(self, call: str, arg: str, value: Any) -> Value | Span:
        """Return the result for *value*, or raise the error that refuses it.

        *call* is the call that the author wrote, and *arg* names the argument.
        """
        self._refuse(call, arg, value)
        return parse_value(value)


@dataclass(frozen=True)
class NumberOperand(ValueOperand):
    """One value that the scan turns into an integer, such as a count.

    The field refuses None, a range, a list, a tuple, a dict, a structure and text that
    is not an integer.  It accepts a Char tag, because the tag can hold a digit.

    Attributes:
        whole: Whether the field refuses a Real constant, such as 3.5 or 5.0.  CLICK's
            For refuses a count with a decimal point.
        span: The lowest and highest integer constant that the field accepts, or None
            for any integer.
    """

    _lists: ClassVar[bool] = True
    whole: bool = False
    span: tuple[int, int] | None = None

    def fits(self, value: Any) -> bool:
        if _is_range(value) or _holds_no_number(value):
            return False
        if isinstance(value, str):
            return _parses(value, TagType.INT)
        if self.whole and isinstance(value, float):
            return False
        if self._outside_span(value):
            return False
        return super().fits(value)

    def _outside_span(self, value: Any) -> bool:
        if self.span is None or not isinstance(value, int):
            return False
        least, most = self.span
        return not least <= value <= most

    def wanted(self) -> str:
        number = "a whole number" if self.whole else "a number"
        return f"{number}, a tag, a pointer to one or an expression"

    def refusal(self, call: str, arg: str, value: Any) -> Exception | None:
        """Return the error that refuses *value*, or None if it fits.

        For a Real constant, the message also says that a constant count has no decimal
        point, because 5.0 looks like a whole number.  An integer constant outside
        ``span`` raises ``ValueError``.
        """
        inner = unwrap(value)[0]
        if self.span is not None and self._outside_span(inner):
            least, most = self.span
            return ValueError(
                f"{call} got {describe(value)} as {arg}. Pass a whole number from {least} "
                f"to {most}, or a tag."
            )
        if not (self.whole and isinstance(inner, float)):
            return super().refusal(call, arg, value)
        return TypeError(
            f"{call} got {describe(value)} as {arg}. Pass a whole number or a tag. "
            "A constant count has no decimal point."
        )


@dataclass(frozen=True)
class EachOperand(Operand):
    """A dict whose every value fits *item*, such as the ``outs`` of ``run_function()``."""

    item: Operand = TagOperand()

    def fits(self, value: Any) -> bool:
        return isinstance(value, Mapping) and all(self.item.accepts(v) for v in value.values())

    def wanted(self) -> str:
        return f"a dict whose every value is {self.item.wanted()}"

    def refusal(self, call: str, arg: str, value: Any) -> Exception | None:
        """Return the error that refuses the first value in the dict that does not fit."""
        if not isinstance(value, Mapping):
            return super().refusal(call, arg, value)
        for key, item in value.items():
            error = self.item.refusal(call, f"{arg}[{key!r}]", item)
            if error is not None:
                return error
        return None


TAG = TagOperand()
RANGE = RangeOperand()
VALUE = ValueOperand()


@dataclass(frozen=True)
class FitsRegister:
    """Refuse a range of more bits than the register that the bits pack into or come from.

    An Int or Word holds 16 bits, and a Dint or Real holds 32.  A range with pointers
    has its length only when the scan runs, so the scan checks it.
    """

    bits: str
    register: str

    @property
    def fields(self) -> tuple[str, ...]:
        """The fields that the rule reads."""
        return (self.bits, self.register)

    def refusal(
        self, call: str, args: Mapping[str, str], values: Mapping[str, Any]
    ) -> Exception | None:
        """Return the error for *values*, by field, or None if the bits fit."""
        bits, register = values[self.bits], values[self.register]
        if not isinstance(bits, BlockRange):
            return None
        width = 16 if operand_type(register) in {TagType.INT, TagType.WORD} else 32
        if len(bits) <= width:
            return None
        advice = f"Pass at most {width} bits"
        advice += f", or a Dint or Real tag as {args[self.register]}." if width == 16 else "."
        return ValueError(
            f"{call} got {len(bits)} bits as {args[self.bits]}, and {_name(register)} holds "
            f"{width} bits. {advice}"
        )


@dataclass(frozen=True)
class NumberInto:
    """Refuse a value that cannot become a number when the destination is a number register.

    A list, a tuple, a dict or a structure such as ``Timer[1]`` cannot.  Nor can an
    expression with a list or a tuple in it.  Text can if it parses as the destination's
    number, as "5" does for an Int and "1.5" for a Real.  A Char tag can if it holds a
    digit, so the rule never refuses one.  Into a Bool or a Char, the rule refuses nothing.

    A range cannot become a number either.  ``copy()`` and ``fill()`` refuse a range
    themselves, so only ``math()`` reaches the rule with one.

    Attributes:
        value: The field that holds the value.
        dest: The field that holds the destination.
        text: Whether the rule refuses text that does not parse as the destination's
            number.  ``math("A", ds[3])`` raises on every scan.  ``copy()`` and
            ``fill()`` check text themselves.  ``copy("A", ds[3], convert=to_ascii)``
            writes 65 into DS3, so ``copy()`` leaves this False.
    """

    value: str
    dest: str
    text: bool = False

    @property
    def fields(self) -> tuple[str, ...]:
        """The fields that the rule reads."""
        return (self.value, self.dest)

    def refusal(
        self, call: str, args: Mapping[str, str], values: Mapping[str, Any]
    ) -> Exception | None:
        """Return the error for *values*, by field, or None if the value fits."""
        value, dest = values[self.value], values[self.dest]
        if _is_range(dest):
            number = dest.block.type
            holds = f"{_name(dest)} hold numbers"
        else:
            number = operand_type(dest)
            holds = f"{_name(dest)} holds a number"
        if value is None or number not in _NUMBER_TYPES:
            return None
        arg = args[self.value]
        if isinstance(value, str):
            if not self.text or _parses(value, number):
                return None
        elif not _is_range(value) and not _holds_no_number(value):
            part = _bad_part(value, lists=True)
            if part is _NO_PART:
                return None
            return TypeError(
                f"{call} got {describe(part)} inside the {arg}, and {holds}. "
                "Pass a number, a tag or an expression in its place."
            )
        return TypeError(
            f"{call} got {describe(value)} as {arg}, and {holds}. "
            "Pass a number, a tag, a pointer to one or an expression."
        )


def check_fields(
    operands: Mapping[str, Operand],
    rules: tuple[FitsRegister | NumberInto, ...],
    call: str,
    values: Mapping[str, Any],
) -> None:
    """Raise the error for the first value in *values*, by field, that does not fit.

    The rules see each value without its ``immediate()``.  A field that *values* leaves
    out is not checked, and nor is a rule that reads it.
    """
    for field_name, value in values.items():
        operand = operands.get(field_name)
        if operand is not None:
            error = operand.refusal(call, operand.arg or field_name, value)
            if error is not None:
                raise error
    check_rules(operands, rules, call, values)


def check_rules(
    operands: Mapping[str, Operand],
    rules: tuple[FitsRegister | NumberInto, ...],
    call: str,
    values: Mapping[str, Any],
) -> None:
    """Raise the error for the first rule in *rules* that refuses *values*, by field.

    The rules see each value without its ``immediate()``.  A rule that reads a field
    that *values* leaves out is not checked.
    """
    if not rules:
        return
    args = {name: (operands[name].arg if name in operands else "") or name for name in values}
    plain = {name: unwrap(value)[0] for name, value in values.items()}
    for rule in rules:
        if all(name in plain for name in rule.fields):
            error = rule.refusal(call, args, plain)
            if error is not None:
                raise error


_NO_PART = object()


def _bad_part(value: Any, *, lists: bool) -> Any:
    """Return the first part of the expression *value* that the scan cannot read.

    Return ``_NO_PART`` if the scan can read every part.  These parts are bad:
    None, a dict, a condition, a structure and an ``immediate()``.  With *lists*, a list
    and a tuple are bad parts too.

    The walk skips the bounds of a ``randint()`` with a Real bound.  The scan never
    reads them, and the draw is 0.  The walk uses a list, not recursion, so a deep
    expression does not reach Python's recursion limit.
    """
    pending = [value]
    while pending:
        node = pending.pop()
        if not _is_expression(node):
            continue
        if type(node) is RandIntExpr and node.real_bounds:
            continue
        if type(node) is LiteralExpr:
            part = node.value
            if part is None or _is_condition(part):
                return part
            if isinstance(part, (dict, InstanceView, ImmediateRef)):
                return part
            if lists and isinstance(part, (list, tuple)):
                return part
            continue
        pending.extend(getattr(node, "__dict__", {}).values())
    return _NO_PART


def _holds_no_number(value: Any) -> bool:
    """Return True if *value* is a list, a tuple, a dict or a structure."""
    return isinstance(value, (list, tuple, dict, InstanceView))


def _parses(text: str, number: TagType) -> bool:
    """Return True if the scan can turn *text* into the value of a *number* tag.

    The scan calls ``float()`` for a Real and ``int()`` for the other number types.
    """
    try:
        float(text) if number is TagType.REAL else int(text)
    except ValueError:
        return False
    return True


# Expression and Condition are ABCs, and an isinstance check against an ABC is slow.  The
# check reads these caches, by type, instead.
_EXPRESSION_TYPES: dict[type, bool] = {}
_CONDITION_TYPES: dict[type, bool] = {}


def _is_expression(value: Any) -> bool:
    kind = type(value)
    known = _EXPRESSION_TYPES.get(kind)
    if known is None:
        known = _EXPRESSION_TYPES[kind] = issubclass(kind, Expression)
    return known


def _is_condition(value: Any) -> bool:
    kind = type(value)
    known = _CONDITION_TYPES.get(kind)
    if known is None:
        known = _CONDITION_TYPES[kind] = issubclass(kind, Condition)
    return known


def _types_text(types: tuple[TagType, ...]) -> str:
    """Return "Int or Word " for *types*, or "" for every type."""
    if not types:
        return ""
    names = [tag_type.name.capitalize() for tag_type in types]
    if len(names) == 1:
        return f"{names[0]} "
    return f"{', '.join(names[:-1])} or {names[-1]} "


def _name(value: Any) -> str:
    from pyrung.core.validation.render import operand_name

    return operand_name(value)


def describe(value: Any, *, count: bool = False) -> str:
    """Return *value* as a refusal names it, as in "the range DS1..DS2 of Int tags".

    With *count*, the text for a range gives its number of tags, as in "the range
    DS1..DS3 of 3 Int tags".
    """
    if isinstance(value, ImmediateRef):
        return f"{describe(value.value, count=count)} in immediate()"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, tuple):
        return "a tuple"
    if isinstance(value, dict):
        return "a dict"
    if value is None:
        return "None"
    if isinstance(value, str):
        return f"the text {value!r}"
    if isinstance(value, (bool, int, float)):
        return f"the constant {value!r}"
    if isinstance(value, Tag):
        return f"the {value.type.name.capitalize()} tag {_name(value)}"
    if isinstance(value, (IndirectRef, IndirectExprRef)):
        return f"the {value.block.type.name.capitalize()} pointer {_name(value)}"
    if _is_range(value):
        size = f"{len(value)} " if count and isinstance(value, BlockRange) else ""
        return f"the range {_name(value)} of {size}{_types_text((value.block.type,))}tags"
    if isinstance(value, InstanceView):
        return f"the structure {value._owner.name}[{value._index}]"
    if _is_condition(value):
        return f"the condition {_name(value)}"
    if isinstance(value, Expression):
        return f"the expression {_name(value)}"
    return _name(value)
