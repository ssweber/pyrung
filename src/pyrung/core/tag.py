"""Tag definitions for the immutable PLC engine.

Tags are lightweight references to values in SystemState.
They carry type metadata but hold no runtime state.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, cast, overload

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


class _AutoTagDecl:
    """Descriptor used for class-based auto tag naming declarations."""

    def __init__(self, tag_type: TagType, retentive: bool):
        self._tag_type = tag_type
        self._retentive = retentive
        self._bound_tag: LiveTag | None = None

    @property
    def bound_tag(self) -> LiveTag | None:
        return self._bound_tag

    def __set_name__(self, owner: type, name: str) -> None:
        if not getattr(owner, "_PYRUNG_IS_TAG_NAMESPACE", False):
            raise TypeError(
                f"Auto tag declaration '{name}' must be defined on a TagNamespace subclass. "
                f"Use explicit naming instead: Bool('{name}')."
            )
        self._bound_tag = LiveTag(name, self._tag_type, self._retentive)

    def __get__(self, instance: object, owner: type | None = None) -> LiveTag:
        if self._bound_tag is None:
            raise TypeError("Auto tag declaration is not bound to a class attribute.")
        return self._bound_tag


class TagNamespace:
    """Base class for opt-in class-based auto tag naming declarations."""

    _PYRUNG_IS_TAG_NAMESPACE = True
    __pyrung_tags__: Mapping[str, LiveTag] = MappingProxyType({})

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)

        inherited_tags: dict[str, LiveTag] = {}
        inherited_origins: list[tuple[str, LiveTag]] = []
        for base in reversed(cls.__mro__[1:-1]):
            if not getattr(base, "_PYRUNG_IS_TAG_NAMESPACE", False):
                continue
            base_tags = getattr(base, "__pyrung_tags__", None)
            if not isinstance(base_tags, Mapping):
                continue
            for attr_name, tag in base_tags.items():
                inherited_tags[attr_name] = tag
                inherited_origins.append((f"{base.__name__}.{attr_name}", tag))

        local_tags: dict[str, LiveTag] = {}
        local_origins: list[tuple[str, LiveTag]] = []
        for attr_name, value in cls.__dict__.items():
            if isinstance(value, _AutoTagDecl):
                bound = value.bound_tag
                if bound is None:
                    raise RuntimeError(
                        f"Auto tag declaration '{attr_name}' on {cls.__name__} was not bound."
                    )
                setattr(cls, attr_name, bound)
                local_tags[attr_name] = bound
                local_origins.append((f"{cls.__name__}.{attr_name}", bound))
            elif isinstance(value, Tag):
                live_tag = _coerce_live_tag(value)
                if live_tag is not value:
                    setattr(cls, attr_name, live_tag)
                local_tags[attr_name] = live_tag
                local_origins.append((f"{cls.__name__}.{attr_name}", live_tag))

        _validate_duplicate_names(cls.__name__, inherited_origins + local_origins)

        merged = dict(inherited_tags)
        merged.update(local_tags)
        cls.__pyrung_tags__ = MappingProxyType(merged)

    @classmethod
    def tags(cls) -> dict[str, LiveTag]:
        """Return a copy of this class' declared tag registry."""
        return dict(cls.__pyrung_tags__)


def _coerce_live_tag(tag: Tag) -> LiveTag:
    if isinstance(tag, LiveTag):
        return tag
    return LiveTag(tag.name, tag.type, tag.retentive, tag.default)


def _validate_duplicate_names(class_name: str, origins: list[tuple[str, LiveTag]]) -> None:
    names_to_origins: dict[str, list[str]] = {}
    for origin, tag in origins:
        names_to_origins.setdefault(tag.name, []).append(origin)

    duplicates = {name: refs for name, refs in names_to_origins.items() if len(refs) > 1}
    if not duplicates:
        return

    details = "; ".join(
        f"{tag_name!r} declared by {', '.join(refs)}"
        for tag_name, refs in sorted(duplicates.items())
    )
    raise ValueError(f"Duplicate tag names in {class_name}: {details}")


def _is_class_declaration_context(stack_depth: int) -> bool:
    frame = inspect.currentframe()
    try:
        for _ in range(stack_depth):
            if frame is None:
                return False
            frame = frame.f_back
        if frame is None:
            return False
        return "__module__" in frame.f_locals and "__qualname__" in frame.f_locals
    finally:
        del frame


class _TagTypeBase(LiveTag):
    """Base class for tag type marker constructors."""

    _tag_type: ClassVar[TagType]
    _default_retentive: ClassVar[bool]

    def __init__(self, name: str | None = None, retentive: bool | None = None) -> None:
        # __new__ returns LiveTag/_AutoTagDecl and bypasses this initializer.
        return None

    @overload
    def __new__(cls, name: str, retentive: bool | None = None) -> LiveTag: ...

    @overload
    def __new__(cls, name: None = None, retentive: bool | None = None) -> _AutoTagDecl: ...

    def __new__(
        cls, name: str | None = None, retentive: bool | None = None
    ) -> LiveTag | _AutoTagDecl:
        if retentive is None:
            retentive = cls._default_retentive
        if name is not None:
            return LiveTag(name, cls._tag_type, retentive)
        if not _is_class_declaration_context(stack_depth=2):
            raise TypeError(
                f"{cls.__name__}() without a name is only valid in a TagNamespace class body. "
                f"Use {cls.__name__}('TagName') or declare inside "
                f"`class Tags(TagNamespace): ...`."
            )
        return _AutoTagDecl(cls._tag_type, retentive)


class Bool(_TagTypeBase):
    _tag_type = TagType.BOOL
    _default_retentive = False


class Int(_TagTypeBase):
    _tag_type = TagType.INT
    _default_retentive = True


class Dint(_TagTypeBase):
    _tag_type = TagType.DINT
    _default_retentive = True


class Real(_TagTypeBase):
    _tag_type = TagType.REAL
    _default_retentive = True


class Word(_TagTypeBase):
    _tag_type = TagType.WORD
    _default_retentive = False


class Char(_TagTypeBase):
    _tag_type = TagType.CHAR
    _default_retentive = True
