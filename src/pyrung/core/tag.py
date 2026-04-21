"""Tag definitions for the immutable PLC engine.

Tags are lightweight references to values in SystemState.
They carry type metadata but hold no runtime state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import TYPE_CHECKING, Any, ClassVar, cast

from pyrung.core._source import _capture_source
from pyrung.core.live_binding import get_active_runner

if TYPE_CHECKING:
    from pyrung.core.condition import Condition
    from pyrung.core.memory_block import Block, BlockRange
    from pyrung.core.physical import Physical


class TagType(Enum):
    """Data types for tags (IEC 61131-3 naming)."""

    BOOL = "bool"  # Boolean: True/False
    INT = "int"  # 16-bit signed: -32768 to 32767
    DINT = "dint"  # 32-bit signed (Double INT)
    REAL = "real"  # 32-bit float
    WORD = "word"  # 16-bit unsigned
    CHAR = "char"  # Single ASCII character


ChoiceKey = int | float | str
ChoiceMap = dict[ChoiceKey, str]


def _structured_choice_items(raw: object, *, owner: str):
    structure_kind = getattr(raw, "_structure_kind", None)
    if structure_kind not in {"udt", "named_array"}:
        return None

    count = getattr(raw, "count", None)
    if count != 1:
        raise TypeError(f"{owner} structure choices require count=1, got {count!r}.")

    try:
        field_names = tuple(cast(Any, raw).field_names)
    except Exception as exc:  # pragma: no cover - defensive
        raise TypeError(f"{owner} structure choices must expose field_names.") from exc

    def _items():
        for field_name in field_names:
            tag = getattr(raw, field_name)
            yield tag.default, field_name

    return _items()


def _normalize_choices(
    raw: object,
    *,
    tag_type: TagType,
    owner: str = "choices",
) -> ChoiceMap | None:
    if raw is None:
        return None
    if tag_type == TagType.BOOL:
        raise TypeError(f"{owner} are not supported for BOOL tags.")

    struct_items = _structured_choice_items(raw, owner=owner)
    if struct_items is not None:
        items = struct_items
    elif isinstance(raw, type) and issubclass(raw, IntEnum):
        items = ((member.value, member.name) for member in raw)
    else:
        try:
            items = dict(cast(Mapping[object, object] | Any, raw)).items()
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"{owner} must be a mapping, IntEnum type, or count-one structure, "
                f"got {type(raw).__name__}."
            ) from exc

    normalized: ChoiceMap = {}
    for key, label in items:
        if isinstance(key, bool) or not isinstance(key, (int, float, str)):
            raise TypeError(f"{owner} keys must be int, float, or str, got {key!r}.")
        if not isinstance(label, str):
            raise TypeError(f"{owner} labels must be strings, got {label!r}.")
        normalized[key] = label

    return normalized or None


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
        default: Default value (None means use type default).
        retentive: Whether value survives power cycles.
        comment: Optional address comment metadata for CSV round-tripping.
    """

    name: str
    type: TagType = TagType.BOOL
    default: Any = field(default=None)
    retentive: bool = False
    comment: str = ""
    choices: ChoiceMap | None = None
    readonly: bool = False
    external: bool = False
    final: bool = False
    public: bool = False
    physical: Physical | None = None
    link: str | None = None
    min: int | float | None = None
    max: int | float | None = None
    uom: str | None = None

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

        # Mutual exclusivity checks
        if self.readonly and self.final:
            raise ValueError(
                f"Tag {self.name!r}: readonly and final are mutually exclusive "
                "(readonly = zero writers, final = exactly one)."
            )
        if self.readonly and self.external:
            raise ValueError(
                f"Tag {self.name!r}: readonly and external are mutually exclusive "
                "(readonly = nothing writes it, external = something outside the ladder writes it)."
            )
        if self.min is not None and self.max is not None and self.min >= self.max:
            raise ValueError(f"Tag {self.name!r}: min must be less than max.")
        if self.choices is not None and (self.min is not None or self.max is not None):
            raise ValueError(f"Tag {self.name!r}: choices cannot be combined with min/max.")
        if self.readonly and self.physical is not None:
            raise ValueError(f"Tag {self.name!r}: readonly cannot be combined with physical.")

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> Condition:  # ty: ignore[invalid-method-override]
        """Create equality comparison condition."""
        from pyrung.core.condition import CompareEq

        cond = CompareEq(self, other)
        cond.source_file, cond.source_line = _capture_source(depth=2)
        return cond

    def __ne__(self, other: object) -> Condition:  # ty: ignore[invalid-method-override]
        """Create inequality comparison condition."""
        from pyrung.core.condition import CompareNe

        cond = CompareNe(self, other)
        cond.source_file, cond.source_line = _capture_source(depth=2)
        return cond

    def __lt__(self, other: Any) -> Condition:
        """Create less-than comparison condition."""
        from pyrung.core.condition import CompareLt

        cond = CompareLt(self, other)
        cond.source_file, cond.source_line = _capture_source(depth=2)
        return cond

    def __le__(self, other: Any) -> Condition:
        """Create less-than-or-equal comparison condition."""
        from pyrung.core.condition import CompareLe

        cond = CompareLe(self, other)
        cond.source_file, cond.source_line = _capture_source(depth=2)
        return cond

    def __gt__(self, other: Any) -> Condition:
        """Create greater-than comparison condition."""
        from pyrung.core.condition import CompareGt

        cond = CompareGt(self, other)
        cond.source_file, cond.source_line = _capture_source(depth=2)
        return cond

    def __ge__(self, other: Any) -> Condition:
        """Create greater-than-or-equal comparison condition."""
        from pyrung.core.condition import CompareGe

        cond = CompareGe(self, other)
        cond.source_file, cond.source_line = _capture_source(depth=2)
        return cond

    def __or__(self, other: object) -> Any:
        """Bitwise OR expression: Tag | value."""
        from pyrung.core.condition import Condition as CondBase
        from pyrung.core.expression import TagExpr

        if self.type == TagType.BOOL:
            if isinstance(other, (Tag, CondBase)):
                raise TypeError(
                    f"Cannot use '|' to combine conditions. Use Or({self.name}, ...) instead."
                )
            if isinstance(other, (int, float)):
                raise TypeError(
                    f"Cannot use bitwise | between Bool tag {self.name!r} and {other!r}. "
                    "This is usually a precedence mistake — "
                    "add parentheses: Ready | (Speed > 50)"
                )
        if isinstance(other, CondBase):
            raise TypeError(
                f"Cannot OR Tag with {type(other).__name__}. "
                "Bitwise OR requires numeric/tag expression operands."
            )
        return TagExpr(self) | cast(Any, other)

    def __ror__(self, other: Any) -> Any:
        """Bitwise OR expression (reverse): value | Tag."""
        from pyrung.core.condition import Condition as CondBase
        from pyrung.core.expression import TagExpr

        if self.type == TagType.BOOL:
            if isinstance(other, (Tag, CondBase)):
                raise TypeError(
                    f"Cannot use '|' to combine conditions. Use Or(..., {self.name}) instead."
                )
            if isinstance(other, (int, float)):
                raise TypeError(
                    f"Cannot use bitwise | between {other!r} and Bool tag {self.name!r}. "
                    "This is usually a precedence mistake — "
                    "add parentheses: (Speed > 50) | Ready"
                )
        if isinstance(other, CondBase):
            raise TypeError(
                f"Cannot OR {type(other).__name__} with Tag. "
                "Bitwise OR requires numeric/tag expression operands."
            )
        return other | TagExpr(self)

    def __bool__(self) -> bool:
        """Prevent accidental use as boolean."""
        raise TypeError(
            f"Cannot use Tag '{self.name}' as boolean. "
            "Use it in a Rung condition instead: Rung(tag) or Rung(tag == value)"
        )

    @property
    def value(self) -> Any:
        """Read or write this tag's value through the active runner scope.

        Returns the current value as seen by the runner, including any pending
        patches or forces. Writes are staged as one-shot patches consumed at
        the next `step()`.

        Raises:
            RuntimeError: If called outside a ``with PLC(...) as plc:`` block.

        Example:
            ```python
            with PLC(logic) as plc:
                print(StartButton.value)    # read current value
                StartButton.value = True    # queue for next scan
            ```
        """
        runner = _require_active_runner(self.name)
        return runner._peek_live_tag_value(self.name, self.default)

    @value.setter
    def value(self, new_value: Any) -> None:
        runner = _require_active_runner(self.name)
        runner.patch({self.name: new_value})

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
        """Bitwise AND expression: Tag & value."""
        from pyrung.core.condition import Condition as CondBase
        from pyrung.core.expression import TagExpr

        if self.type == TagType.BOOL and isinstance(other, (Tag, CondBase)):
            raise TypeError(
                f"Cannot use '&' to combine conditions. Use And({self.name}, ...) instead."
            )
        return TagExpr(self) & other

    def __rand__(self, other: Any) -> Any:
        """Bitwise AND expression (reverse): value & Tag."""
        from pyrung.core.condition import Condition as CondBase
        from pyrung.core.expression import TagExpr

        if self.type == TagType.BOOL:
            if isinstance(other, (Tag, CondBase)):
                raise TypeError(
                    f"Cannot use '&' to combine conditions. Use And(..., {self.name}) instead."
                )
            if isinstance(other, (int, float)):
                raise TypeError(
                    f"Cannot use bitwise & between {other!r} and Bool tag {self.name!r}. "
                    "This is usually a precedence mistake — "
                    "add parentheses: (Speed > 50) & Ready"
                )

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
        """Create a normally-closed condition for BOOL, bitwise invert expression otherwise."""
        if self.type == TagType.BOOL:
            from pyrung.core.condition import NormallyClosedCondition

            cond = NormallyClosedCondition(self)
            cond.source_file, cond.source_line = _capture_source(depth=2)
            return cond

        from pyrung.core.expression import TagExpr

        return ~TagExpr(self)


def _require_active_runner(tag_name: str):
    runner = get_active_runner()
    if runner is None:
        raise RuntimeError(
            f"Tag '{tag_name}' is not bound to an active runner. Use: with PLC(...) as plc: ..."
        )
    return runner


class LiveTag(Tag):
    """Tag with runner-bound staged value access via `.value`.

    `LiveTag` is the concrete type returned by all IEC constructor functions
    (`Bool`, `Int`, `Dint`, `Real`, `Word`, `Char`) and by block indexing.
    It extends `Tag` with the `.value` property, which provides read/write
    access to the current runner state.

    Note:
        `.value` requires an active runner scope. Access outside
        a ``with PLC(...) as plc:`` block raises `RuntimeError`.
    """


@dataclass(frozen=True)
class ImmediateRef:
    """Reference to the immediate (physical) value of an I/O tag.

    Wraps an InputTag or OutputTag to access the physical I/O value
    directly, bypassing the scan-cycle image table.
    """

    value: Tag | BlockRange

    def __post_init__(self) -> None:
        from pyrung.core.memory_block import BlockRange

        if not isinstance(self.value, Tag | BlockRange):
            raise TypeError(
                f"ImmediateRef value must be Tag or BlockRange, got {type(self.value).__name__}."
            )

    @property
    def tag(self) -> Tag:
        """Backward-compatible alias for tag-wrapped immediate operands."""
        if isinstance(self.value, Tag):
            return self.value
        raise TypeError("ImmediateRef.tag is only available when value wraps a Tag.")

    def __invert__(self) -> Condition:
        """Create a normally-closed immediate contact condition."""
        from pyrung.core.condition import NormallyClosedCondition

        cond = NormallyClosedCondition(self)
        cond.source_file, cond.source_line = _capture_source(depth=2)
        return cond


def immediate(value: Tag | BlockRange | ImmediateRef) -> ImmediateRef:
    """Wrap a tag or block range as an immediate operand."""
    from pyrung.core.memory_block import BlockRange

    if isinstance(value, ImmediateRef):
        return value
    if isinstance(value, Tag | BlockRange):
        return ImmediateRef(value)
    raise TypeError(
        f"immediate() expects Tag, BlockRange, or ImmediateRef, got {type(value).__name__}."
    )


@dataclass(frozen=True)
class InputTag(Tag):
    """Tag representing a physical input channel.

    `InputTag` instances are produced exclusively by indexing an `InputBlock`.
    They add the `.immediate` property for bypassing the scan-cycle image table.

    `.immediate` semantics by context:

    - **Simulation (pure):** validation-time annotation only; no runtime effect.
    - **Click dialect:** transcription hint for Click software export.
    - **CircuitPython dialect:** generates direct hardware-read code.
    - **Hardware-in-the-loop:** triggers a real hardware read mid-scan.

    You cannot create an `InputTag` directly; use `InputBlock[n]` instead.

    Example:
        ```python
        X = InputBlock("X", TagType.BOOL, 1, 16)
        sensor = X[3]          # LiveInputTag
        sensor.immediate       # ImmediateRef — bypass image table
        ```
    """

    @property
    def immediate(self) -> ImmediateRef:
        """Return an ImmediateRef that bypasses the input image table."""
        return ImmediateRef(self)


@dataclass(frozen=True)
class OutputTag(Tag):
    """Tag representing a physical output channel.

    `OutputTag` instances are produced exclusively by indexing an `OutputBlock`.
    They add the `.immediate` property for bypassing the scan-cycle image table.

    `.immediate` semantics by context:

    - **Simulation (pure):** validation-time annotation only; no runtime effect.
    - **Click dialect:** transcription hint for Click software export.
    - **CircuitPython dialect:** generates direct hardware-write code.
    - **Hardware-in-the-loop:** triggers a real hardware write mid-scan.

    You cannot create an `OutputTag` directly; use `OutputBlock[n]` instead.

    Example:
        ```python
        Y = OutputBlock("Y", TagType.BOOL, 1, 16)
        valve = Y[1]           # LiveOutputTag
        valve.immediate        # ImmediateRef — bypass image table
        ```
    """

    @property
    def immediate(self) -> ImmediateRef:
        """Return an ImmediateRef that bypasses the output image table."""
        return ImmediateRef(self)


class LiveInputTag(LiveTag, InputTag):
    """InputTag with runner-bound staged value access via .value."""


class LiveOutputTag(LiveTag, OutputTag):
    """OutputTag with runner-bound staged value access via .value."""


class _TagTypeBase(LiveTag):
    """Base class for tag type marker constructors."""

    _tag_type: ClassVar[TagType]
    _default_retentive: ClassVar[bool]

    def __init__(
        self,
        name: str,
        *,
        default: Any = None,
        retentive: bool | None = None,
        comment: str = "",
        choices: type[IntEnum] | ChoiceMap | None = None,
        readonly: bool = False,
        external: bool = False,
        final: bool = False,
        public: bool = False,
        physical: Physical | None = None,
        link: str | None = None,
        min: int | float | None = None,
        max: int | float | None = None,
        uom: str | None = None,
    ) -> None:
        # __new__ returns LiveTag and bypasses this initializer.
        return None

    def __new__(
        cls,
        name: str,
        *,
        default: Any = None,
        retentive: bool | None = None,
        comment: str = "",
        choices: type[IntEnum] | ChoiceMap | None = None,
        readonly: bool = False,
        external: bool = False,
        final: bool = False,
        public: bool = False,
        physical: Physical | None = None,
        link: str | None = None,
        min: int | float | None = None,
        max: int | float | None = None,
        uom: str | None = None,
    ) -> LiveTag:
        if retentive is None:
            retentive = cls._default_retentive
        if not isinstance(name, str):
            raise TypeError(f"{cls.__name__}() name must be a string.")
        if not isinstance(comment, str):
            raise TypeError(f"{cls.__name__}() comment must be a string.")
        normalized_choices = _normalize_choices(
            choices,
            tag_type=cls._tag_type,
            owner=f"{cls.__name__}() choices",
        )
        return LiveTag(
            name,
            cls._tag_type,
            default,
            retentive,
            comment,
            normalized_choices,
            bool(readonly),
            bool(external),
            bool(final),
            bool(public),
            physical,
            link,
            min,
            max,
            uom,
        )


class Bool(_TagTypeBase):
    """Create a BOOL (1-bit boolean) tag.

    Not retentive by default — resets to `False` on power cycle.

    Example:
        ```python
        Button = Bool("Button")
        Light  = Bool("Light", retentive=True)
        ```
    """

    _tag_type = TagType.BOOL
    _default_retentive = False


class Int(_TagTypeBase):
    """Create an INT (16-bit signed integer, −32768 to 32767) tag.

    Retentive by default — survives power cycles.

    Example:
        ```python
        Step     = Int("Step")
        preset = Int("preset", retentive=False)
        ```
    """

    _tag_type = TagType.INT
    _default_retentive = True


class Dint(_TagTypeBase):
    """Create a DINT (32-bit signed integer, ±2 147 483 647) tag.

    Retentive by default. Use for counters or values that exceed INT range.

    Example:
        ```python
        TotalCount = Dint("TotalCount")
        ```
    """

    _tag_type = TagType.DINT
    _default_retentive = True


class Real(_TagTypeBase):
    """Create a REAL (32-bit IEEE 754 float) tag.

    Retentive by default. Use for analog presets and process values.

    Example:
        ```python
        Temperature = Real("Temperature")
        FlowRate    = Real("FlowRate", retentive=False)
        ```
    """

    _tag_type = TagType.REAL
    _default_retentive = True


class Word(_TagTypeBase):
    """Create a WORD (16-bit unsigned integer, 0x0000–0xFFFF) tag.

    Retentive by default. Use for bit-packed status registers or hex values.
    In the Click dialect, `Hex` is an alias for `Word`.

    Example:
        ```python
        StatusWord = Word("StatusWord")
        ```
    """

    _tag_type = TagType.WORD
    _default_retentive = True


class Char(_TagTypeBase):
    """Create a CHAR (8-bit ASCII character) tag.

    Retentive by default. Use for single-character text values.
    For multi-character strings, use a `Block` of `CHAR` tags.
    In the Click dialect, `Txt` is an alias for `Char`.

    Example:
        ```python
        ModeChar = Char("ModeChar")
        ```
    """

    _tag_type = TagType.CHAR
    _default_retentive = True
