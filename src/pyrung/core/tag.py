"""Tag definitions for the immutable PLC engine.

Tags are lightweight references to values in SystemState.
They carry type metadata but hold no runtime state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

from pyrung.core.live_binding import get_active_runner

if TYPE_CHECKING:
    from pyrung.core.condition import Condition
    from pyrung.core.memory_block import Block, BlockRange


class TagType(Enum):
    """Data types for tags (IEC 61131-3 naming)."""

    BOOL = "bool"  # Boolean: True/False
    INT = "int"  # 16-bit signed: -32768 to 32767
    DINT = "dint"  # 32-bit signed (Double INT)
    REAL = "real"  # 32-bit float
    WORD = "word"  # 16-bit unsigned
    CHAR = "char"  # Single ASCII character


@dataclass(frozen=True)
class MappingEntry:
    """Logical-to-hardware mapping declaration used by TagMap."""

    source: Tag | Block
    target: Tag | BlockRange


@dataclass(frozen=True)
class Tag:
    """A reference to a value in SystemState.

    Tags define what a value is (name, type, behavior) but hold no runtime state.
    Values live only in SystemState.tags.

    Attributes:
        name: Unique identifier for this tag.
        type: Data type (BOOL, INT, DINT, REAL, WORD, CHAR).
        retentive: Whether value survives power cycles.
        default: Default value (None means use type default).
    """

    name: str
    type: TagType = TagType.BOOL
    retentive: bool = False
    default: Any = field(default=None)

    def __post_init__(self):
        # Set type-appropriate default if not specified
        if self.default is None:
            defaults = {
                TagType.BOOL: False,
                TagType.INT: 0,
                TagType.DINT: 0,
                TagType.REAL: 0.0,
                TagType.WORD: 0,
                TagType.CHAR: "",
            }
            # Use object.__setattr__ because frozen=True
            object.__setattr__(self, "default", defaults.get(self.type, 0))

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> Condition:
        """Create equality comparison condition."""
        from pyrung.core.condition import CompareEq

        return CompareEq(self, other)

    def __ne__(self, other: object) -> Condition:
        """Create inequality comparison condition."""
        from pyrung.core.condition import CompareNe

        return CompareNe(self, other)

    def __lt__(self, other: Any) -> Condition:
        """Create less-than comparison condition."""
        from pyrung.core.condition import CompareLt

        return CompareLt(self, other)

    def __le__(self, other: Any) -> Condition:
        """Create less-than-or-equal comparison condition."""
        from pyrung.core.condition import CompareLe

        return CompareLe(self, other)

    def __gt__(self, other: Any) -> Condition:
        """Create greater-than comparison condition."""
        from pyrung.core.condition import CompareGt

        return CompareGt(self, other)

    def __ge__(self, other: Any) -> Condition:
        """Create greater-than-or-equal comparison condition."""
        from pyrung.core.condition import CompareGe

        return CompareGe(self, other)

    def __or__(self, other: object) -> Any:
        """Create OR condition (for BOOL) or bitwise OR expression (for non-BOOL)."""
        from pyrung.core.condition import AnyCondition
        from pyrung.core.condition import Condition as CondBase
        from pyrung.core.expression import TagExpr

        # For non-BOOL tags, use bitwise OR
        if self.type != TagType.BOOL:
            if isinstance(other, CondBase):
                raise TypeError(
                    f"Cannot OR Tag with {type(other).__name__}. "
                    "Bitwise OR requires numeric/tag expression operands."
                )
            return TagExpr(self) | cast(Any, other)

        if isinstance(other, Tag | CondBase):
            return AnyCondition(self, other)
        raise TypeError(
            f"Cannot OR Tag with {type(other).__name__}. "
            f"If using comparisons with |, add parentheses: (Step == 0) | (Mode == 1)"
        )

    def __ror__(self, other: Any) -> Any:
        """Support reverse OR for both condition and bitwise operations."""
        from pyrung.core.condition import AnyCondition
        from pyrung.core.condition import Condition as CondBase
        from pyrung.core.expression import TagExpr

        # For non-BOOL tags, use bitwise OR
        if self.type != TagType.BOOL:
            if isinstance(other, CondBase):
                raise TypeError(
                    f"Cannot OR {type(other).__name__} with Tag. "
                    "Bitwise OR requires numeric/tag expression operands."
                )
            return other | TagExpr(self)

        if isinstance(other, Tag | CondBase):
            return AnyCondition(other, self)
        raise TypeError(
            f"Cannot OR {type(other).__name__} with Tag. "
            f"If using comparisons with |, add parentheses: (Step == 0) | (Mode == 1)"
        )

    def __bool__(self) -> bool:
        """Prevent accidental use as boolean."""
        raise TypeError(
            f"Cannot use Tag '{self.name}' as boolean. "
            "Use it in a Rung condition instead: Rung(tag) or Rung(tag == value)"
        )

    def map_to(self, target: Tag) -> MappingEntry:
        """Create a logical-to-hardware mapping entry."""
        return MappingEntry(source=self, target=target)

    # =========================================================================
    # Arithmetic Operators -> Expression
    # =========================================================================

    def __add__(self, other: Any) -> Any:
        """Create addition expression: Tag + value."""
        from pyrung.core.expression import TagExpr

        return TagExpr(self) + other

    def __radd__(self, other: Any) -> Any:
        """Create addition expression: value + Tag."""
        from pyrung.core.expression import TagExpr

        return other + TagExpr(self)

    def __sub__(self, other: Any) -> Any:
        """Create subtraction expression: Tag - value."""
        from pyrung.core.expression import TagExpr

        return TagExpr(self) - other

    def __rsub__(self, other: Any) -> Any:
        """Create subtraction expression: value - Tag."""
        from pyrung.core.expression import TagExpr

        return other - TagExpr(self)

    def __mul__(self, other: Any) -> Any:
        """Create multiplication expression: Tag * value."""
        from pyrung.core.expression import TagExpr

        return TagExpr(self) * other

    def __rmul__(self, other: Any) -> Any:
        """Create multiplication expression: value * Tag."""
        from pyrung.core.expression import TagExpr

        return other * TagExpr(self)

    def __truediv__(self, other: Any) -> Any:
        """Create division expression: Tag / value."""
        from pyrung.core.expression import TagExpr

        return TagExpr(self) / other

    def __rtruediv__(self, other: Any) -> Any:
        """Create division expression: value / Tag."""
        from pyrung.core.expression import TagExpr

        return other / TagExpr(self)

    def __floordiv__(self, other: Any) -> Any:
        """Create floor division expression: Tag // value."""
        from pyrung.core.expression import TagExpr

        return TagExpr(self) // other

    def __rfloordiv__(self, other: Any) -> Any:
        """Create floor division expression: value // Tag."""
        from pyrung.core.expression import TagExpr

        return other // TagExpr(self)

    def __mod__(self, other: Any) -> Any:
        """Create modulo expression: Tag % value."""
        from pyrung.core.expression import TagExpr

        return TagExpr(self) % other

    def __rmod__(self, other: Any) -> Any:
        """Create modulo expression: value % Tag."""
        from pyrung.core.expression import TagExpr

        return other % TagExpr(self)

    def __pow__(self, other: Any) -> Any:
        """Create power expression: Tag ** value."""
        from pyrung.core.expression import TagExpr

        return TagExpr(self) ** other

    def __rpow__(self, other: Any) -> Any:
        """Create power expression: value ** Tag."""
        from pyrung.core.expression import TagExpr

        return other ** TagExpr(self)

    def __neg__(self) -> Any:
        """Create negation expression: -Tag."""
        from pyrung.core.expression import TagExpr

        return -TagExpr(self)

    def __pos__(self) -> Any:
        """Create positive expression: +Tag."""
        from pyrung.core.expression import TagExpr

        return +TagExpr(self)

    def __abs__(self) -> Any:
        """Create absolute value expression: abs(Tag)."""
        from pyrung.core.expression import TagExpr

        return abs(TagExpr(self))

    # =========================================================================
    # Bitwise Operators -> Expression
    # =========================================================================

    def __and__(self, other: Any) -> Any:
        """Create AND condition (for BOOL) or bitwise AND expression (non-BOOL)."""
        from pyrung.core.condition import AllCondition
        from pyrung.core.condition import Condition as CondBase
        from pyrung.core.expression import TagExpr

        if self.type == TagType.BOOL:
            if isinstance(other, CondBase):
                return AllCondition(self, other)
            if isinstance(other, Tag) and other.type == TagType.BOOL:
                return AllCondition(self, other)

        return TagExpr(self) & other

    def __rand__(self, other: Any) -> Any:
        """Support reverse AND for conditions and bitwise expressions."""
        from pyrung.core.condition import AllCondition
        from pyrung.core.condition import Condition as CondBase
        from pyrung.core.expression import TagExpr

        if self.type == TagType.BOOL:
            if isinstance(other, CondBase):
                return AllCondition(other, self)
            if isinstance(other, Tag) and other.type == TagType.BOOL:
                return AllCondition(other, self)

        return other & TagExpr(self)

    def __xor__(self, other: Any) -> Any:
        """Create bitwise XOR expression: Tag ^ value."""
        from pyrung.core.expression import TagExpr

        return TagExpr(self) ^ other

    def __rxor__(self, other: Any) -> Any:
        """Create bitwise XOR expression: value ^ Tag."""
        from pyrung.core.expression import TagExpr

        return other ^ TagExpr(self)

    def __lshift__(self, other: Any) -> Any:
        """Create left shift expression: Tag << value."""
        from pyrung.core.expression import TagExpr

        return TagExpr(self) << other

    def __rlshift__(self, other: Any) -> Any:
        """Create left shift expression: value << Tag."""
        from pyrung.core.expression import TagExpr

        return other << TagExpr(self)

    def __rshift__(self, other: Any) -> Any:
        """Create right shift expression: Tag >> value."""
        from pyrung.core.expression import TagExpr

        return TagExpr(self) >> other

    def __rrshift__(self, other: Any) -> Any:
        """Create right shift expression: value >> Tag."""
        from pyrung.core.expression import TagExpr

        return other >> TagExpr(self)

    def __invert__(self) -> Any:
        """Create bitwise invert expression: ~Tag."""
        from pyrung.core.expression import TagExpr

        return ~TagExpr(self)


def _require_active_runner(tag_name: str):
    runner = get_active_runner()
    if runner is None:
        raise RuntimeError(
            f"Tag '{tag_name}' is not bound to an active runner. Use: with runner.active(): ..."
        )
    return runner


class _LiveValueMixin:
    """Add staged read/write value access through the active PLCRunner."""

    @property
    def value(self) -> Any:
        tag = cast(Any, self)
        runner = _require_active_runner(tag.name)
        return runner._peek_live_tag_value(tag.name, tag.default)

    @value.setter
    def value(self, new_value: Any) -> None:
        tag = cast(Any, self)
        runner = _require_active_runner(tag.name)
        runner.patch({tag.name: new_value})


class LiveTag(_LiveValueMixin, Tag):
    """Tag with runner-bound staged value access via .value."""


@dataclass(frozen=True)
class ImmediateRef:
    """Reference to the immediate (physical) value of an I/O tag.

    Wraps an InputTag or OutputTag to access the physical I/O value
    directly, bypassing the scan-cycle image table.
    """

    tag: Tag


@dataclass(frozen=True)
class InputTag(Tag):
    """Tag representing a physical input.

    InputTags have an .immediate property to access the physical
    input value directly, bypassing the input image table.
    """

    @property
    def immediate(self) -> ImmediateRef:
        return ImmediateRef(self)


@dataclass(frozen=True)
class OutputTag(Tag):
    """Tag representing a physical output.

    OutputTags have an .immediate property to access the physical
    output value directly, bypassing the output image table.
    """

    @property
    def immediate(self) -> ImmediateRef:
        return ImmediateRef(self)


class LiveInputTag(LiveTag, InputTag):
    """InputTag with runner-bound staged value access via .value."""


class LiveOutputTag(LiveTag, OutputTag):
    """OutputTag with runner-bound staged value access via .value."""


def Bool(name: str, retentive: bool = False) -> LiveTag:
    """Create a BOOL tag (boolean).

    Args:
        name: Tag name.
        retentive: Whether value survives power cycles. Default False.
    """
    return LiveTag(name, TagType.BOOL, retentive)


def Int(name: str, retentive: bool = True) -> LiveTag:
    """Create an INT tag (16-bit signed integer).

    Args:
        name: Tag name.
        retentive: Whether value survives power cycles. Default True.
    """
    return LiveTag(name, TagType.INT, retentive)


def Dint(name: str, retentive: bool = True) -> LiveTag:
    """Create a DINT tag (32-bit signed integer).

    Args:
        name: Tag name.
        retentive: Whether value survives power cycles. Default True.
    """
    return LiveTag(name, TagType.DINT, retentive)


def Real(name: str, retentive: bool = True) -> LiveTag:
    """Create a REAL tag (32-bit float).

    Args:
        name: Tag name.
        retentive: Whether value survives power cycles. Default True.
    """
    return LiveTag(name, TagType.REAL, retentive)


def Word(name: str, retentive: bool = False) -> LiveTag:
    """Create a WORD tag (16-bit unsigned).

    Args:
        name: Tag name.
        retentive: Whether value survives power cycles. Default False.
    """
    return LiveTag(name, TagType.WORD, retentive)


def Char(name: str, retentive: bool = True) -> LiveTag:
    """Create a CHAR tag (single ASCII character).

    Args:
        name: Tag name.
        retentive: Whether value survives power cycles. Default True.
    """
    return LiveTag(name, TagType.CHAR, retentive)
