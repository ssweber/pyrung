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

from pyrung.core._source import _capture_source
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

    def __eq__(self, other: object) -> Condition:  # type: ignore[override]
        """Create equality comparison condition."""
        from pyrung.core.condition import CompareEq

        cond = CompareEq(self, other)
        cond.source_file, cond.source_line = _capture_source(depth=2)
        return cond

    def __ne__(self, other: object) -> Condition:  # type: ignore[override]
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
            cond = AnyCondition(self, other)
            cond.source_file, cond.source_line = _capture_source(depth=2)
            for child in cond.conditions:
                if child.source_file is None:
                    child.source_file = cond.source_file
                if child.source_line is None:
                    child.source_line = cond.source_line
            return cond
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
            cond = AnyCondition(other, self)
            cond.source_file, cond.source_line = _capture_source(depth=2)
            for child in cond.conditions:
                if child.source_file is None:
                    child.source_file = cond.source_file
                if child.source_line is None:
                    child.source_line = cond.source_line
            return cond
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

    @property
    def value(self) -> Any:
        """Read or write this tag's value through the active runner scope.

        Returns the current value as seen by the runner, including any pending
        patches or forces. Writes are staged as one-shot patches consumed at
        the next `step()`.

        Raises:
            RuntimeError: If called outside `with runner.active(): ...`.

        Example:
            ```python
            runner = PLCRunner(logic)
            with runner.active():
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

    def as_value(self) -> Any:
        """Wrap this tag for text->numeric character-value conversion."""
        from pyrung.core.copy_modifiers import as_value

        return as_value(self)

    def as_ascii(self) -> Any:
        """Wrap this tag for text->numeric ASCII-code conversion."""
        from pyrung.core.copy_modifiers import as_ascii

        return as_ascii(self)

    def as_text(
        self,
        *,
        suppress_zero: bool = True,
        pad: int | None = None,
        exponential: bool = False,
        termination_code: int | str | None = None,
    ) -> Any:
        """Wrap this tag for numeric->text conversion."""
        from pyrung.core.copy_modifiers import as_text

        return as_text(
            self,
            suppress_zero=suppress_zero,
            pad=pad,
            exponential=exponential,
            termination_code=termination_code,
        )

    def as_binary(self) -> Any:
        """Wrap this tag for numeric->text binary-copy conversion."""
        from pyrung.core.copy_modifiers import as_binary

        return as_binary(self)

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
                cond = AllCondition(self, other)
                cond.source_file, cond.source_line = _capture_source(depth=2)
                for child in cond.conditions:
                    if child.source_file is None:
                        child.source_file = cond.source_file
                    if child.source_line is None:
                        child.source_line = cond.source_line
                return cond
            if isinstance(other, Tag) and other.type == TagType.BOOL:
                cond = AllCondition(self, other)
                cond.source_file, cond.source_line = _capture_source(depth=2)
                for child in cond.conditions:
                    if child.source_file is None:
                        child.source_file = cond.source_file
                    if child.source_line is None:
                        child.source_line = cond.source_line
                return cond

        return TagExpr(self) & other

    def __rand__(self, other: Any) -> Any:
        """Support reverse AND for conditions and bitwise expressions."""
        from pyrung.core.condition import AllCondition
        from pyrung.core.condition import Condition as CondBase
        from pyrung.core.expression import TagExpr

        if self.type == TagType.BOOL:
            if isinstance(other, CondBase):
                cond = AllCondition(other, self)
                cond.source_file, cond.source_line = _capture_source(depth=2)
                for child in cond.conditions:
                    if child.source_file is None:
                        child.source_file = cond.source_file
                    if child.source_line is None:
                        child.source_line = cond.source_line
                return cond
            if isinstance(other, Tag) and other.type == TagType.BOOL:
                cond = AllCondition(other, self)
                cond.source_file, cond.source_line = _capture_source(depth=2)
                for child in cond.conditions:
                    if child.source_file is None:
                        child.source_file = cond.source_file
                    if child.source_line is None:
                        child.source_line = cond.source_line
                return cond

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
            f"Tag '{tag_name}' is not bound to an active runner. Use: with runner.active(): ..."
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
        `with runner.active(): ...` raises `RuntimeError`.
    """


@dataclass(frozen=True)
class ImmediateRef:
    """Reference to the immediate (physical) value of an I/O tag.

    Wraps an InputTag or OutputTag to access the physical I/O value
    directly, bypassing the scan-cycle image table.
    """

    tag: Tag


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
                f"Auto tag declaration '{name}' must be defined on an AutoTag subclass. "
                f"Use explicit naming instead: Bool('{name}')."
            )
        self._bound_tag = LiveTag(name, self._tag_type, self._retentive)

    def __get__(self, instance: object, owner: type | None = None) -> LiveTag:
        if self._bound_tag is None:
            raise TypeError("Auto tag declaration is not bound to a class attribute.")
        return self._bound_tag


class AutoTag:
    """Base class for class-body auto-naming of tags.

    Subclass `AutoTag` and declare tags using the type constructors
    without a name argument. The attribute name is used as the tag name
    automatically. This is equivalent to ``Bool("Start")`` but removes
    the string duplication.

    Note:
        ``AutoTag`` is available from both ``pyrung`` and ``pyrung.core``::

            from pyrung import AutoTag

    Example:
        ```python
        from pyrung import AutoTag, Bool, Int, Real

        class Tags(AutoTag):
            Start    = Bool()
            Stop     = Bool()
            Step     = Int(retentive=True)
            preset = Real()

        # Access tags via the class:
        with Rung(Tags.Start):
            latch(Tags.Stop)

        # Or unpack for convenience:
        Start, Stop, Step = Tags.Start, Tags.Stop, Tags.Step
        ```

    Tag constructors called *outside* an `AutoTag` class body without a
    name raise `TypeError`. Explicit naming (``Bool("Start")``) is always
    valid and is the canonical cross-context form.

    `AutoTag` class bodies accept tag declarations only. Memory blocks
    (``Block``, ``InputBlock``, ``OutputBlock``) must be declared outside
    the `AutoTag` class.

    Duplicate tag names across the class hierarchy are detected at class
    definition time and raise `ValueError`.
    """

    _PYRUNG_IS_TAG_NAMESPACE = True
    __pyrung_tags__: dict[str, LiveTag] = cast(dict[str, LiveTag], MappingProxyType({}))

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)

        from pyrung.core.memory_block import Block, InputBlock, OutputBlock

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
            elif isinstance(value, (Block, InputBlock, OutputBlock)):
                raise TypeError(
                    f"{cls.__name__}.{attr_name} is {type(value).__name__}. "
                    "Block declarations are not allowed on AutoTag subclasses; "
                    "declare Block/InputBlock/OutputBlock at module scope."
                )

        _validate_duplicate_names(cls.__name__, inherited_origins + local_origins)

        merged = dict(inherited_tags)
        merged.update(local_tags)
        cls.__pyrung_tags__ = cast(dict[str, LiveTag], MappingProxyType(merged))

    @classmethod
    def tags(cls) -> dict[str, LiveTag]:
        """Return a copy of this class' declared tag registry."""
        return dict(cls.__pyrung_tags__)

    @classmethod
    def export(cls, namespace: dict[str, Any], *, overwrite: bool = False) -> dict[str, LiveTag]:
        """Export declared tags into a target namespace mapping.

        Typical usage is flattening class-declared tags into module scope:

            class Devices(AutoTag):
                Start = Bool()
                Count = Int()

            Devices.export(globals())

        Args:
            namespace: Mapping to receive exported names (for example, ``globals()``).
            overwrite: If ``False`` (default), raising ``ValueError`` on conflicting
                existing names. If ``True``, existing names are replaced.

        Returns:
            Copy of tags exported to ``namespace``.
        """
        conflicts = [
            name
            for name, tag in cls.__pyrung_tags__.items()
            if name in namespace and namespace[name] is not tag and not overwrite
        ]
        if conflicts:
            joined = ", ".join(sorted(conflicts))
            raise ValueError(
                f"{cls.__name__}.export() conflicts with existing names: {joined}. "
                "Pass overwrite=True to replace them."
            )

        namespace.update(cls.__pyrung_tags__)
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
                f"{cls.__name__}() without a name is only valid in an AutoTag class body. "
                f"Use {cls.__name__}('TagName') or declare inside "
                f"`class Tags(AutoTag): ...`."
            )
        return _AutoTagDecl(cls._tag_type, retentive)


class Bool(_TagTypeBase):
    """Create a BOOL (1-bit boolean) tag.

    Not retentive by default — resets to `False` on power cycle.

    Example:
        ```python
        Button = Bool("Button")
        Light  = Bool("Light", retentive=True)

        # In an AutoTag class body (auto-named):
        class Tags(AutoTag):
            Start = Bool()
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

    Not retentive by default. Use for bit-packed status registers or hex values.
    In the Click dialect, `Hex` is an alias for `Word`.

    Example:
        ```python
        StatusWord = Word("StatusWord")
        ```
    """

    _tag_type = TagType.WORD
    _default_retentive = False


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
