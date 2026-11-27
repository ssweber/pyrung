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

These instructions parse their operands instead:

- ``copy()``, ``fill()`` and ``blockcopy()``;
- ``pack_bits()``, ``pack_words()``, ``pack_text()``, ``unpack_to_bits()`` and
  ``unpack_to_words()``;
- ``math()``, ``search()`` and ``shift()``;
- ``forloop()``.  It parses only its count;
- ``run_function()`` and ``run_enabled_function()``.  Each parses its ``outs`` and
  passes each value of its ``ins`` to :func:`parse_value`;
- ``send()`` and ``receive()``.  Each parses only its ``source`` or its ``dest``;
- the coils ``out()``, ``latch()``, ``reset()``, ``toggle()``, ``debounce()``,
  ``flasher()`` and ``timed()``.  Each coil parses its target.  ``debounce()``,
  ``flasher()`` and ``timed()`` also parse their presets;
- the timers ``on_delay()`` and ``off_delay()``, and the counters ``count_up()`` and
  ``count_down()``;
- the drums ``event_drum()``, ``time_drum()`` and ``time_event_drum()``.

Their builder calls ``parse()`` on each declaration, which returns one of the results
below or raises the refusal.  ``search()`` passes the value of its comparison to
:func:`parse_value`.  That function refuses nothing.  The timers, the counters and the
drums parse in their own ``__init__``, and so do the presets of the coils.  The
instruction then holds only results, except in six fields of three coils.
``debounce()`` and ``flasher()`` hold ``on_done``, ``on_acc``, ``off_done`` and
``off_acc`` as plain tags.  ``timed()`` holds ``timer_done`` and ``timer_acc`` as plain
tags.
``execute()``, the code generator and the CLICK export match on them and never test the
raw value again.  A reader that takes the operands of every instruction calls
:func:`as_authored` on each.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, ClassVar, Literal, assert_never

from pyrung.core.condition import Condition
from pyrung.core.copy_converters import CopyConverter, to_text
from pyrung.core.expression import Expression, LiteralExpr, RandIntExpr
from pyrung.core.memory_block import BlockRange, IndirectBlockRange, IndirectExprRef, IndirectRef
from pyrung.core.storage import CHAR_CODE_MAX, CHAR_HOLDS
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
    """Build the ``ImmediateRef`` of an immediate *arg* that has none.

    Refuse a wrapper on an arg that is not immediate, or one that wraps another object.
    """
    if arg.wrapper is not None and not arg.immediate:
        raise ValueError("A wrapper needs immediate=True.")
    if arg.wrapper is not None and arg.wrapper.value is not held_object:
        raise ValueError(
            f"The wrapper holds {arg.wrapper.value!r}, but the operand is {held_object!r}."
        )
    if arg.immediate and arg.wrapper is None:
        object.__setattr__(arg, "wrapper", ImmediateRef(held_object))


Value = TagArg | PointerArg | ConstArg | ExprArg
"""What a field that reads one value holds, such as the source of ``copy()``."""
Dest = TagArg | PointerArg
"""What a field that writes one register holds, such as the destination of ``copy()``."""
Span = RangeArg | PointerRangeArg
"""What a field that takes a range holds, such as the destination of ``fill()``."""
Coil = TagArg | RangeArg
"""What a coil writes, such as the target of ``out()``: one tag, or a range with fixed ends."""
Preset = TagArg | ConstArg
"""What a preset or a step number holds, such as the preset of ``on_delay()``: a tag or an
integer constant."""


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

    def parse_tag(self, call: str, arg: str, value: Any) -> TagArg:
        """Return *value* as one tag, or raise the error that refuses it.

        Call it for a field that refuses a pointer.  The result is then always a tag.
        """
        if self.pointers:
            raise TypeError("parse_tag() needs an operand that refuses a pointer.")
        self._refuse(call, arg, value)
        bare, wrapper = _split(value)
        return TagArg(bare, wrapper is not None, wrapper)


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

    def parse(self, call: str, arg: str, value: Any) -> Value:
        """Return the result for *value*, or raise the error that refuses it.

        The field refuses a range, so the result is never a :data:`Span`.
        """
        self._refuse(call, arg, value)
        result = parse_value(value)
        match result:
            case TagArg() | PointerArg() | ConstArg() | ExprArg():
                return result
            case RangeArg() | PointerRangeArg():
                raise AssertionError(f"{call} accepted a range as {arg}.")
            case _:
                assert_never(result)


@dataclass(frozen=True)
class EachOperand(Operand):
    """A dict whose every value fits *item*, such as the ``outs`` of ``run_function()``."""

    item: TagOperand = TagOperand()

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

    def parse(self, call: str, arg: str, value: Any) -> dict[Any, Dest]:
        """Return the dict with each value parsed, or raise the error that refuses one.

        The result keeps the keys in the author's order.
        """
        self._refuse(call, arg, value)
        return {key: self.item.parse(call, f"{arg}[{key!r}]", item) for key, item in value.items()}


@dataclass(frozen=True)
class CoilOperand(Operand):
    """The target of ``out()``, ``latch()`` or ``reset()``: one tag, or a range with fixed ends.

    The field refuses a pointer, such as ``C[Ptr]``, and a range with pointers, such as
    ``C.select(Lo, Hi)``.  Its refusal does not start with "<call> got ...".  The
    refusals of the other operands do.
    """

    def fits(self, value: Any) -> bool:
        return isinstance(value, (Tag, BlockRange))

    def wanted(self) -> str:
        return "one tag, or a range from .select()"

    def refusal(self, call: str, arg: str, value: Any) -> Exception | None:
        """Return the error that refuses *value*, or None if it fits."""
        if self.accepts(value):
            return None
        return TypeError(
            "Expected Tag, BlockRange from .select(), or immediate(...) wrapper, "
            f"got {type(unwrap(value)[0]).__name__}."
        )

    def parse(self, call: str, arg: str, value: Any) -> Coil:
        """Return *value* as a tag or a range, or raise the error that refuses it."""
        self._refuse(call, arg, value)
        bare, wrapper = _split(value)
        if isinstance(bare, Tag):
            return TagArg(bare, wrapper is not None, wrapper)
        return RangeArg(bare, wrapper is not None, wrapper)


@dataclass(frozen=True)
class CoilBitOperand(Operand):
    """The target of ``toggle()``, ``debounce()``, ``flasher()`` or ``timed()``: one Bool tag.

    Set *immediate* to False for a coil that has no Immediate option in CLICK.  Like
    :class:`CoilOperand`, its refusals have words of their own.
    """

    def fits(self, value: Any) -> bool:
        return isinstance(value, Tag) and value.type is TagType.BOOL

    def wanted(self) -> str:
        return "one Bool tag"

    def refusal(self, call: str, arg: str, value: Any) -> Exception | None:
        """Return the error that refuses *value*, or None if it fits."""
        if self.accepts(value):
            return None
        if isinstance(value, ImmediateRef) and not self.immediate:
            return TypeError(
                f"{call} takes no immediate(...) target; only out() and toggle() "
                "coils write immediately."
            )
        return TypeError(f"{call} writes one Bool tag, got {value!r}.")

    def parse(self, call: str, arg: str, value: Any) -> TagArg:
        """Return *value* as one tag, or raise the error that refuses it."""
        self._refuse(call, arg, value)
        bare, wrapper = _split(value)
        return TagArg(bare, wrapper is not None, wrapper)


@dataclass(frozen=True)
class PayloadOperand(CoilOperand):
    """The tags that ``send()`` sends or ``receive()`` stores: one tag, or a range with fixed ends.

    The field refuses ``immediate()``, a pointer and a range with pointers.  Like
    :class:`CoilOperand`, its refusals have words of their own.
    """

    immediate: bool = False

    def refusal(self, call: str, arg: str, value: Any) -> Exception | None:
        """Return the error that refuses *value*, or None if it fits."""
        if self.accepts(value):
            return None
        return TypeError(f"{arg} must be Tag or BlockRange, got {type(value).__name__}")


TAG = TagOperand()
RANGE = RangeOperand()
VALUE = ValueOperand()
COIL = CoilOperand()


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
            ``fill()`` check text themselves, so they leave this False.
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


TextWrite = Literal["copy", "fill", "blockcopy", "receive", "send", "number", "bit"]
"""What an instruction stores, for :class:`TextInto`.

- ``"copy"``, ``"fill"`` and ``"blockcopy"`` store their source.
- ``"receive"`` stores what it reads from a remote device.
- ``"send"`` writes the Char tags into a remote device.  The addresses there must hold
  text too.
- ``"number"`` stores a number, as ``math()`` does.
- ``"bit"`` stores True or False, as ``out()`` and ``latch()`` do.
"""


@dataclass(frozen=True)
class TextInto:
    """Refuse a write into a Char tag that would store something other than text.

    A Char tag holds only text, and the facts that ``check()`` and ``how()`` read assume
    it.  Into a Char tag, the rule accepts text, a Char tag and a Char range.  A Copy also
    accepts a number, because :meth:`converter` returns ``to_text()`` for it.  A Fill
    accepts an integer constant the same way.  A Copy's text constant needs one Char tag
    for each character, and a Fill's constant must be one character.  A Receive accepts a
    TXT address, or a raw holding or input register.  Each register holds two characters.
    The rule refuses every other source, and every write of a number or a bit.  CLICK's
    editor refuses each of these forms.

    A Send of Char tags to a raw address must go to a holding register.  CLICK's editor
    refuses a coil.

    The rule refuses nothing for tags that are not Char tags.

    Attributes:
        dest: The field that holds the destination.  For ``"send"`` it holds the source,
            the Char tags that the Send reads.
        writes: What the instruction stores.  See :data:`TextWrite`.
        value: The field that holds the source.  Leave it empty when *writes* is
            ``"number"`` or ``"bit"``.  For ``"receive"`` and ``"send"`` the field holds
            the remote start, such as "DS1" or a ``ModbusAddress``.
        convert: The field that holds the converter that the author passed.  Leave it
            empty for an instruction that takes none.
    """

    dest: str
    writes: TextWrite
    value: str = ""
    convert: str = ""

    @property
    def fields(self) -> tuple[str, ...]:
        """The fields that the rule reads."""
        return tuple(name for name in (self.value, self.dest, self.convert) if name)

    def converter(self, value: Any, dest: Any) -> CopyConverter | None:
        """Return the converter that stores *value* into *dest* when the author passed none.

        A number tag, a number pointer or a number constant into a Char tag gets
        ``to_text()``.  CLICK's Copy dialog has no Copy there without a conversion, and it
        starts on Copy Value with Suppress zero.  Any other write gets None, a plain store.
        """
        value = unwrap(value)[0]
        if not _holds_text(unwrap(dest)[0]) or self.writes not in ("copy", "fill"):
            return None
        if _source_kind(value) in ("integer", "real", "number"):
            return to_text()
        return None

    def refusal(
        self, call: str, args: Mapping[str, str], values: Mapping[str, Any]
    ) -> Exception | None:
        """Return the error for *values*, by field, or None if the store holds text."""
        dest = values[self.dest]
        if not _holds_text(dest):
            return None
        target = f"{describe(dest)} as the {args[self.dest]}"
        if self.writes == "number":
            return TypeError(
                f"{call} got {target}. {call} writes a number, and a Char tag holds only "
                "text. Write into a number tag, then copy() that tag into the Char tag."
            )
        if self.writes == "bit":
            return TypeError(
                f"{call} got {target}. {call} writes True or False, and a Char tag holds "
                "only text. Pass a Bool tag."
            )
        value = values[self.value]
        if self.writes == "receive":
            return _receive_refusal(call, value, args[self.value], target)
        if self.writes == "send":
            if isinstance(value, str):
                # The CLICK export refuses a TXT to another CLICK bank, and the run sets
                # the error flag.
                return None
            return _bit_address_refusal(
                call, value, args[self.value], target, "Send to a HOLDING register"
            )
        got = f"{call} got {describe(value)} as the {args[self.value]} and {target}."
        convert = values[self.convert] if self.convert else None
        kind = _source_kind(value)
        if self.writes == "blockcopy":
            if convert is not None:
                return ValueError(
                    f"{call} got convert=to_{convert.mode} and {target}. to_{convert.mode} "
                    "writes a number, and Char tags hold only text. Leave convert out."
                )
            if kind == "text":
                return None
            advice = "Pass a range of Char tags as the source."
            if kind == "number":
                advice += " To write a number's text, copy() each number into its Char tag."
            return ValueError(f"{got} {call} into Char tags reads only Char tags. {advice}")
        if isinstance(value, str) or (self.writes == "fill" and kind == "integer"):
            # A Fill writes an integer's text, so 12 is two characters (OBJ-023).
            return _text_constant_refusal(call, got, str(value), fill=self.writes == "fill")
        if kind in ("bool", "bool constant") and not (
            self.writes == "fill" and kind == "bool constant"
        ):
            return ValueError(f"{got} {_BOOL_ADVICE[self.writes, kind]}")
        if kind == "expression" and self.writes == "copy":
            return ValueError(
                f"{got} {call} writes no expression into a Char tag. Store the result in a "
                "number tag with math(), then copy that tag with convert=to_text()."
            )
        if convert is not None or kind == "text":
            return None
        if self.writes == "fill" and kind in ("real", "number", "expression"):
            # CLICK's editor refuses a number register here (OBJ-024), and an expression is
            # a number from registers.  A CLICK was read writing a Real constant's text at
            # each address in turn, past the range's end.
            return ValueError(
                f"{got} {call} writes only an integer or a text constant into Char tags. "
                "Fill one of those, or copy() the value into one Char tag."
            )
        if kind == "other":
            advice = (
                "Pass text or an integer constant."
                if self.writes == "fill"
                else "Pass text, a number, or a Char or number tag."
            )
            return TypeError(f"{got} A Char tag holds only text. {advice}")
        return None


_BOOL_ADVICE = {
    ("copy", "bool constant"): 'copy() writes no Bool into a Char tag. Copy the text "1" or '
    '"0" instead.',
    ("copy", "bool"): 'copy() writes no Bool into a Char tag. Copy the text "1" in a rung that '
    'has the Bool as its condition, and "0" in a rung that has its inverse.',
    ("fill", "bool"): "fill() writes no Bool tag into Char tags. Fill True in a rung that has "
    "the Bool as its condition, and False in a rung that has its inverse.",
}


def _text_constant_refusal(call: str, got: str, text: str, *, fill: bool) -> Exception | None:
    """Return the error for a constant's *text* into Char tags, or None if the store holds it.

    A Copy writes one character into each Char tag in turn.  A Fill writes the same
    character into each tag, so it takes one.  CLICK's editor refuses a longer one (OBJ-023).
    """
    if fill and len(text) > 1:
        return ValueError(
            f"{got} {call} writes one character into every Char tag. Fill one character, "
            "or copy() the value to write its characters into the tags in turn."
        )
    wide = next((ch for ch in text if ord(ch) > CHAR_CODE_MAX), None)
    if wide is not None:
        return ValueError(
            f"{got} A Char tag holds {CHAR_HOLDS}, and {wide!r} is past {CHAR_CODE_MAX}. "
            f"Pass characters up to code {CHAR_CODE_MAX}."
        )
    return None


def _holds_text(dest: Any) -> bool:
    """Return True if *dest* is a Char tag, a Char pointer or a range of Char tags."""
    kind = dest.block.type if _is_range(dest) else operand_type(dest)
    return kind is TagType.CHAR


def _source_kind(value: Any) -> str:
    """Return what kind of source *value* is, for :class:`TextInto`.

    The kinds are "text", "bool constant", "integer", "real", "bool", "number",
    "expression" and "other".  A tag, a pointer and a range each take the kind of
    their type, so a Char range is "text".
    """
    if isinstance(value, str):
        return "text"
    if isinstance(value, bool):
        return "bool constant"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "real"
    kind = value.block.type if _is_range(value) else operand_type(value)
    if kind is TagType.CHAR:
        return "text"
    if kind is TagType.BOOL:
        return "bool"
    if kind in _NUMBER_TYPES:
        return "number"
    if _is_expression(value):
        return "expression"
    return "other"


def _receive_refusal(call: str, remote: Any, arg: str, target: str) -> Exception | None:
    """Return the error for a Receive from *remote* into Char tags, or None if it holds text.

    *remote* is a CLICK address, such as "TXT1", or a ``ModbusAddress``.  CLICK's TXT
    reads only TXT from a CLICK.  From any other device it reads a holding or input
    register.  Each register holds two characters.
    """
    if isinstance(remote, str):
        from pyclickplc.addresses import parse_address

        bank = parse_address(remote)[0]
        if bank == "TXT":
            return None
        return TypeError(
            f"{call} got the CLICK address {remote} as the {arg} and {target}. Char tags "
            f"receive only from TXT addresses. Receive from a TXT address, or receive into "
            f"tags that suit the {bank} bank."
        )
    return _bit_address_refusal(
        call, remote, arg, target, "Receive from a HOLDING or INPUT register"
    )


def _bit_address_refusal(
    call: str, remote: Any, arg: str, target: str, advice: str
) -> Exception | None:
    """Return the error for Char tags and a raw coil or discrete input, or None for a register.

    *advice* names the register to use instead, such as "Send to a HOLDING register".
    """
    kind = remote.register_type.name
    if kind in ("HOLDING", "INPUT"):
        return None
    return TypeError(
        f"{call} got a {kind} address as the {arg} and {target}. A {kind} address holds "
        f"a bit, and Char tags hold only text. {advice}. Each register holds two "
        "characters."
    )


def check_fields(
    operands: Mapping[str, Operand],
    rules: tuple[FitsRegister | NumberInto | TextInto, ...],
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
    rules: tuple[FitsRegister | NumberInto | TextInto, ...],
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
