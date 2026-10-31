"""Condition classes for the immutable PLC engine.

Conditions are evaluated lazily at scan time against SystemState.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeAlias

if TYPE_CHECKING:
    from pyrung.core.context import ConditionView, ScanContext
    from pyrung.core.memory_block import IndirectRef
    from pyrung.core.tag import ImmediateRef, Tag


ConditionTerm: TypeAlias = "Condition | Tag | ImmediateRef"
ConditionGroup: TypeAlias = "tuple[ConditionTerm, ...] | list[ConditionTerm]"
ConditionInput: TypeAlias = "ConditionTerm | ConditionGroup"


def _contact_tag(value: Tag | ImmediateRef) -> Tag:
    from pyrung.core.tag import ImmediateRef, Tag

    if isinstance(value, ImmediateRef):
        if not isinstance(value.value, Tag):
            raise TypeError(
                f"Immediate contact operand must wrap a Tag. Got {type(value.value).__name__}."
            )
        return value.value
    return value


class Condition(ABC):
    """Base class for all conditions.

    Conditions are pure functions: evaluate(state) -> bool.
    They read from state but never modify it.

    Supports both direct evaluation via evaluate(state) and
    context-based evaluation via evaluate(ctx) for batched scans.
    """

    source_file: str | None = None
    source_line: int | None = None
    # Edge contacts: the site key ``bind_sites`` stamps (``e{N}``); see
    # :mod:`pyrung.core.site_memory`.
    _site_key: str | None = None
    _site_program_owned: bool = False

    @abstractmethod
    def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
        """Evaluate this condition against a ScanContext or ConditionView.

        Accepts either a live ScanContext (read-after-write within a scan)
        or a frozen ConditionView (rung-entry snapshot for branch conditions).
        """
        pass

    def __eq__(self, other: object) -> bool:
        """Identity comparison, with helpful error for precedence mistakes."""
        if not isinstance(other, Condition):
            raise TypeError(
                f"Cannot compare Condition with {type(other).__name__}. "
                f"Use Or() or And() to combine conditions: Or(Button, Step == 0)"
            )
        return self is other

    def __bool__(self) -> bool:
        raise TypeError(
            f"Cannot use Condition '{type(self).__name__}' as boolean. "
            "Use .evaluate(ctx) inside a scan, or compare against .default "
            "for plain-value assertions: assert val == Tag.default"
        )

    def __hash__(self) -> int:
        """Allow conditions to be used in sets/dicts."""
        return id(self)

    def __repr__(self) -> str:
        """Render as the pyrung source that built this condition.

        The default object repr is an address, which makes a condition opaque
        everywhere it is shown -- logs, exceptions, plan output, and golden
        files.  :func:`~pyrung.core.validation.render.render_condition` is the
        neutral owner of that text and never fails; import it lazily because it
        reads this module's classes.  This is presentation only: ``==`` and
        ``hash`` remain identity-based, so an equal repr is not equality.
        """
        try:
            from pyrung.core.validation.render import render_condition

            return render_condition(self)
        except Exception:  # noqa: BLE001 - repr must never raise
            return f"<{type(self).__name__}>"


class CompareEq(Condition):
    """Equality comparison: tag == value or tag == other_tag."""

    def __init__(self, tag: Tag, value: Any):
        self.tag = tag
        self.value = value
        self._value_kind = _classify_value(value)

    def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
        tag_value = ctx.get_tag(self.tag.name, self.tag.default)
        other_value = _resolve_classified(self.value, self._value_kind, ctx)
        return tag_value == other_value


class CompareNe(Condition):
    """Inequality comparison: tag != value or tag != other_tag."""

    def __init__(self, tag: Tag, value: Any):
        self.tag = tag
        self.value = value
        self._value_kind = _classify_value(value)

    def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
        tag_value = ctx.get_tag(self.tag.name, self.tag.default)
        other_value = _resolve_classified(self.value, self._value_kind, ctx)
        return tag_value != other_value


_VALUE_LITERAL = 0
_VALUE_TAG = 1
_VALUE_EXPR = 2


_VALUE_INDIRECT = 3


def _classify_value(value: Any) -> int:
    from pyrung.core.expression import Expression
    from pyrung.core.memory_block import IndirectRef
    from pyrung.core.tag import Tag

    if isinstance(value, Expression):
        return _VALUE_EXPR
    if isinstance(value, Tag):
        return _VALUE_TAG
    if isinstance(value, IndirectRef):
        return _VALUE_INDIRECT
    return _VALUE_LITERAL


def _resolve_value(value: Any, ctx: ScanContext | ConditionView) -> Any:
    """Resolve a value that may be a Tag, IndirectRef, Expression, or literal."""
    from pyrung.core.expression import Expression
    from pyrung.core.memory_block import IndirectRef
    from pyrung.core.tag import Tag

    if isinstance(value, Expression):
        return value.evaluate(ctx)
    if isinstance(value, Tag):
        return ctx.get_tag(value.name, value.default)
    if isinstance(value, IndirectRef):
        resolved_tag = value.resolve_ctx(ctx)
        return ctx.get_tag(resolved_tag.name, resolved_tag.default)
    return value


def _resolve_classified(value: Any, kind: int, ctx: ScanContext | ConditionView) -> Any:
    if kind == _VALUE_EXPR:
        return value.evaluate(ctx)
    if kind == _VALUE_TAG:
        return ctx.get_tag(value.name, value.default)
    if kind == _VALUE_INDIRECT:
        resolved_tag = value.resolve_ctx(ctx)
        return ctx.get_tag(resolved_tag.name, resolved_tag.default)
    return value


class CompareLt(Condition):
    """Less-than comparison: tag < value."""

    def __init__(self, tag: Tag, value: Any):
        self.tag = tag
        self.value = value
        self._value_kind = _classify_value(value)

    def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
        tag_value = ctx.get_tag(self.tag.name, self.tag.default)
        other_value = _resolve_classified(self.value, self._value_kind, ctx)
        return tag_value < other_value


class CompareLe(Condition):
    """Less-than-or-equal comparison: tag <= value."""

    def __init__(self, tag: Tag, value: Any):
        self.tag = tag
        self.value = value
        self._value_kind = _classify_value(value)

    def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
        tag_value = ctx.get_tag(self.tag.name, self.tag.default)
        other_value = _resolve_classified(self.value, self._value_kind, ctx)
        return tag_value <= other_value


class CompareGt(Condition):
    """Greater-than comparison: tag > value."""

    def __init__(self, tag: Tag, value: Any):
        self.tag = tag
        self.value = value
        self._value_kind = _classify_value(value)

    def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
        tag_value = ctx.get_tag(self.tag.name, self.tag.default)
        other_value = _resolve_classified(self.value, self._value_kind, ctx)
        return tag_value > other_value


class CompareGe(Condition):
    """Greater-than-or-equal comparison: tag >= value."""

    def __init__(self, tag: Tag, value: Any):
        self.tag = tag
        self.value = value
        self._value_kind = _classify_value(value)

    def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
        tag_value = ctx.get_tag(self.tag.name, self.tag.default)
        other_value = _resolve_classified(self.value, self._value_kind, ctx)
        return tag_value >= other_value


class BitCondition(Condition):
    """Normally open contact (XIC) - true when bit is on.

    This is the default condition when a BOOL tag is used directly in a Rung.
    """

    def __init__(self, tag: Tag | ImmediateRef):
        self.tag = tag
        self._resolved_tag = _contact_tag(tag)

    def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
        return bool(ctx.get_tag(self._resolved_tag.name, False))


class IntTruthyCondition(Condition):
    """INT truthiness contact - true when value is nonzero."""

    def __init__(self, tag: Tag):
        self.tag = tag

    def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
        return int(ctx.get_tag(self.tag.name, self.tag.default)) != 0


class NormallyClosedCondition(Condition):
    """Normally closed contact (XIO) - true when bit is off.

    The inverse of BitCondition.
    """

    def __init__(self, tag: Tag | ImmediateRef):
        self.tag = tag
        self._resolved_tag = _contact_tag(tag)

    def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
        return not bool(ctx.get_tag(self._resolved_tag.name, False))


class _EdgeCondition(Condition):
    """An edge contact: a site with memory of its own (``_edge:e{N}``).

    The memory holds the bit as this contact last saw it, stored whenever the
    contact's row is scanned, powered or not, and off at RUN (CLICK).  Two
    contacts on one tag, the same contact in a subroutine called twice, or in a
    For body on successive iterations each compare against their own last
    look.  The row stores the memory after taking its condition view
    (:func:`pyrung.core.site_memory.scan_edge_frame`), so ``evaluate()`` only
    reads: the bit and the memory both come from the view.
    """

    _memory_key: str | None = None

    def __init__(self, tag: Tag | ImmediateRef):
        self.tag = tag
        self._resolved_tag = _contact_tag(tag)

    @property
    def _site_key(self) -> str | None:  # type: ignore[override]
        key = self._memory_key
        return None if key is None else key[len("_edge:") :]

    @_site_key.setter
    def _site_key(self, key: str | None) -> None:
        self._memory_key = None if key is None else f"_edge:{key}"

    def _edges(self, ctx: ScanContext | ConditionView) -> tuple[bool, bool]:
        key = self._memory_key
        if key is None:
            from pyrung.core.site_memory import edge_memory_key

            key = edge_memory_key(self)  # raises: never bound
        current = bool(ctx.get_tag(self._resolved_tag.name, False))
        previous = bool(ctx.get_memory(key, False))
        return current, previous


class RisingEdgeCondition(_EdgeCondition):
    """Rising edge: the bit is on and was off when this contact last saw it."""

    def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
        current, previous = self._edges(ctx)
        return current and not previous


class FallingEdgeCondition(_EdgeCondition):
    """Falling edge: the bit is off and was on when this contact last saw it."""

    def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
        current, previous = self._edges(ctx)
        return previous and not current


# =============================================================================
# Indirect Comparison Conditions
# =============================================================================


class IndirectCompareEq(Condition):
    """Equality comparison for IndirectRef: indirect_ref == value or indirect_ref == tag."""

    def __init__(self, indirect_ref: IndirectRef, value: Any):
        from pyrung.core.tag import Tag as _Tag

        self.indirect_ref = indirect_ref
        self.value = value
        self._value_is_tag = isinstance(value, _Tag)

    def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
        resolved_tag = self.indirect_ref.resolve_ctx(ctx)
        resolved_value = ctx.get_tag(resolved_tag.name, resolved_tag.default)
        if self._value_is_tag:
            other_value = ctx.get_tag(self.value.name, self.value.default)
        else:
            other_value = _resolve_value(self.value, ctx)
        return resolved_value == other_value


class IndirectCompareNe(Condition):
    """Inequality comparison for IndirectRef: indirect_ref != value or indirect_ref != tag."""

    def __init__(self, indirect_ref: IndirectRef, value: Any):
        from pyrung.core.tag import Tag as _Tag

        self.indirect_ref = indirect_ref
        self.value = value
        self._value_is_tag = isinstance(value, _Tag)

    def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
        resolved_tag = self.indirect_ref.resolve_ctx(ctx)
        resolved_value = ctx.get_tag(resolved_tag.name, resolved_tag.default)
        if self._value_is_tag:
            other_value = ctx.get_tag(self.value.name, self.value.default)
        else:
            other_value = _resolve_value(self.value, ctx)
        return resolved_value != other_value


class IndirectCompareLt(Condition):
    """Less-than comparison for IndirectRef: indirect_ref < value."""

    def __init__(self, indirect_ref: IndirectRef, value: Any):
        self.indirect_ref = indirect_ref
        self.value = value

    def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
        resolved_tag = self.indirect_ref.resolve_ctx(ctx)
        tag_value = ctx.get_tag(resolved_tag.name, resolved_tag.default)
        return tag_value < _resolve_value(self.value, ctx)


class IndirectCompareLe(Condition):
    """Less-than-or-equal comparison for IndirectRef: indirect_ref <= value."""

    def __init__(self, indirect_ref: IndirectRef, value: Any):
        self.indirect_ref = indirect_ref
        self.value = value

    def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
        resolved_tag = self.indirect_ref.resolve_ctx(ctx)
        tag_value = ctx.get_tag(resolved_tag.name, resolved_tag.default)
        return tag_value <= _resolve_value(self.value, ctx)


class IndirectCompareGt(Condition):
    """Greater-than comparison for IndirectRef: indirect_ref > value."""

    def __init__(self, indirect_ref: IndirectRef, value: Any):
        self.indirect_ref = indirect_ref
        self.value = value

    def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
        resolved_tag = self.indirect_ref.resolve_ctx(ctx)
        tag_value = ctx.get_tag(resolved_tag.name, resolved_tag.default)
        return tag_value > _resolve_value(self.value, ctx)


class IndirectCompareGe(Condition):
    """Greater-than-or-equal comparison for IndirectRef: indirect_ref >= value."""

    def __init__(self, indirect_ref: IndirectRef, value: Any):
        self.indirect_ref = indirect_ref
        self.value = value

    def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
        resolved_tag = self.indirect_ref.resolve_ctx(ctx)
        tag_value = ctx.get_tag(resolved_tag.name, resolved_tag.default)
        return tag_value >= _resolve_value(self.value, ctx)


# =============================================================================
# Composite Conditions (And / Or)
# =============================================================================


def _as_condition(cond: object) -> Condition:
    """Normalize a Tag/Condition into a concrete Condition."""
    from pyrung.core.tag import ImmediateRef, Tag, TagType

    if isinstance(cond, ImmediateRef):
        wrapped = cond.value
        if not isinstance(wrapped, Tag):
            raise TypeError(
                f"Immediate contact requires a Tag operand. Got {type(wrapped).__name__}."
            )
        if wrapped.type != TagType.BOOL:
            raise TypeError(
                f"Immediate contact Tag '{wrapped.name}' must be BOOL, got {wrapped.type.name}."
            )
        return BitCondition(cond)

    if isinstance(cond, Tag):
        if cond.type == TagType.BOOL:
            return BitCondition(cond)
        if cond.type in (TagType.INT, TagType.DINT):
            return IntTruthyCondition(cond)
        raise TypeError(
            f"Tag '{cond.name}' of type {cond.type.name} cannot be used directly as condition. "
            "Use comparison operators: tag == value, tag > 0, etc."
        )
    if isinstance(cond, Condition):
        return cond
    raise TypeError(f"Expected Condition or Tag, got {type(cond)}")


def _flatten_condition_inputs(
    *conditions: object,
    coerce: Callable[[object], Condition] = _as_condition,
    group_empty_error: str = "condition group cannot be empty",
) -> list[Condition]:
    """Flatten variadic and grouped condition inputs into concrete Conditions."""

    flattened: list[Condition] = []

    for condition in conditions:
        if isinstance(condition, tuple | list):
            if not condition:
                raise ValueError(group_empty_error)
            for item in condition:
                flattened.append(coerce(item))
        else:
            flattened.append(coerce(condition))
    return flattened


class AllCondition(Condition):
    """AND condition - true when all sub-conditions are true.

    Example:
        with Rung(And(Ready, AutoMode)):
            out(StartPermissive)
    """

    def __init__(self, *conditions: ConditionInput):
        self.conditions = _flatten_condition_inputs(
            *conditions,
            coerce=_as_condition,
            group_empty_error="And() group cannot be empty",
        )

        if not self.conditions:
            raise ValueError("And() requires at least one condition")

    def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
        for cond in self.conditions:
            if not cond.evaluate(ctx):
                return False
        return True


class AnyCondition(Condition):
    """OR condition - true when any sub-condition is true.

    Example:
        with Rung(Step == 1, Or(Start, oCmdStart)):
            out(Light)
    """

    def __init__(self, *conditions: ConditionTerm):
        self.conditions: list[Condition] = []
        for cond in conditions:
            if isinstance(cond, tuple | list):
                raise TypeError(
                    "Or() does not accept tuple/list groups. Use And(...) for grouped AND terms."
                )
            self.conditions.append(_as_condition(cond))

        if not self.conditions:
            raise ValueError("Or() requires at least one condition")

    def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
        for cond in self.conditions:
            if cond.evaluate(ctx):
                return True
        return False


def _normalize_and_condition(
    *conditions: object,
    coerce: Callable[[object], Condition] = _as_condition,
    empty_error: str = "condition requires at least one condition",
    group_empty_error: str = "condition group cannot be empty",
) -> Condition:
    """Normalize variadic/grouped inputs into one Condition with AND semantics."""
    normalized = _flatten_condition_inputs(
        *conditions,
        coerce=coerce,
        group_empty_error=group_empty_error,
    )
    if not normalized:
        raise ValueError(empty_error)
    if len(normalized) == 1:
        return normalized[0]
    return AllCondition(*normalized)
