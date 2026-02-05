"""Condition classes for the immutable PLC engine.

Conditions are evaluated lazily at scan time against SystemState.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext
    from pyrung.core.memory_bank import IndirectRef
    from pyrung.core.tag import Tag


class Condition(ABC):
    """Base class for all conditions.

    Conditions are pure functions: evaluate(state) -> bool.
    They read from state but never modify it.

    Supports both direct evaluation via evaluate(state) and
    context-based evaluation via evaluate(ctx) for batched scans.
    """

    @abstractmethod
    def evaluate(self, ctx: ScanContext) -> bool:
        """Evaluate this condition against a ScanContext.

        Uses context for read-after-write visibility within a scan.
        """
        pass

    def __eq__(self, other: object) -> bool:
        """Identity comparison, with helpful error for precedence mistakes."""
        if not isinstance(other, Condition):
            raise TypeError(
                f"Cannot compare Condition with {type(other).__name__}. "
                f"If using | with comparisons, add parentheses: Button | (Step == 0)"
            )
        return self is other

    def __hash__(self) -> int:
        """Allow conditions to be used in sets/dicts."""
        return id(self)

    def __or__(self, other: Condition | Tag) -> AnyCondition:
        """OR two conditions: (Step == 0) | Start."""
        from pyrung.core.tag import Tag

        if isinstance(other, Condition | Tag):
            return AnyCondition(self, other)
        return NotImplemented

    def __ror__(self, other: Tag) -> AnyCondition:
        """Support Tag | Condition."""
        from pyrung.core.tag import Tag

        if isinstance(other, Tag):
            return AnyCondition(other, self)
        return NotImplemented


class CompareEq(Condition):
    """Equality comparison: tag == value or tag == other_tag."""

    def __init__(self, tag: Tag, value: Any):
        self.tag = tag
        self.value = value

    def evaluate(self, ctx: ScanContext) -> bool:
        from pyrung.core.tag import Tag

        tag_value = ctx.get_tag(self.tag.name)
        if isinstance(self.value, Tag):
            other_value = ctx.get_tag(self.value.name)
        else:
            other_value = self.value
        return tag_value == other_value


class CompareNe(Condition):
    """Inequality comparison: tag != value or tag != other_tag."""

    def __init__(self, tag: Tag, value: Any):
        self.tag = tag
        self.value = value

    def evaluate(self, ctx: ScanContext) -> bool:
        from pyrung.core.tag import Tag

        tag_value = ctx.get_tag(self.tag.name)
        if isinstance(self.value, Tag):
            other_value = ctx.get_tag(self.value.name)
        else:
            other_value = self.value
        return tag_value != other_value


def _resolve_value(value: Any, ctx: ScanContext) -> Any:
    """Resolve a value that may be a Tag, Expression, or literal."""
    from pyrung.core.expression import Expression
    from pyrung.core.tag import Tag

    if isinstance(value, Expression):
        return value.evaluate(ctx)
    if isinstance(value, Tag):
        return ctx.get_tag(value.name, value.default)
    return value


class CompareLt(Condition):
    """Less-than comparison: tag < value."""

    def __init__(self, tag: Tag, value: Any):
        self.tag = tag
        self.value = value

    def evaluate(self, ctx: ScanContext) -> bool:
        tag_value = ctx.get_tag(self.tag.name, 0)
        other_value = _resolve_value(self.value, ctx)
        return tag_value < other_value


class CompareLe(Condition):
    """Less-than-or-equal comparison: tag <= value."""

    def __init__(self, tag: Tag, value: Any):
        self.tag = tag
        self.value = value

    def evaluate(self, ctx: ScanContext) -> bool:
        tag_value = ctx.get_tag(self.tag.name, 0)
        other_value = _resolve_value(self.value, ctx)
        return tag_value <= other_value


class CompareGt(Condition):
    """Greater-than comparison: tag > value."""

    def __init__(self, tag: Tag, value: Any):
        self.tag = tag
        self.value = value

    def evaluate(self, ctx: ScanContext) -> bool:
        tag_value = ctx.get_tag(self.tag.name, 0)
        other_value = _resolve_value(self.value, ctx)
        return tag_value > other_value


class CompareGe(Condition):
    """Greater-than-or-equal comparison: tag >= value."""

    def __init__(self, tag: Tag, value: Any):
        self.tag = tag
        self.value = value

    def evaluate(self, ctx: ScanContext) -> bool:
        tag_value = ctx.get_tag(self.tag.name, 0)
        other_value = _resolve_value(self.value, ctx)
        return tag_value >= other_value


class BitCondition(Condition):
    """Normally open contact (XIC) - true when bit is on.

    This is the default condition when a Bit tag is used directly in a Rung.
    """

    def __init__(self, tag: Tag):
        self.tag = tag

    def evaluate(self, ctx: ScanContext) -> bool:
        return bool(ctx.get_tag(self.tag.name, False))


class NormallyClosedCondition(Condition):
    """Normally closed contact (XIO) - true when bit is off.

    The inverse of BitCondition.
    """

    def __init__(self, tag: Tag):
        self.tag = tag

    def evaluate(self, ctx: ScanContext) -> bool:
        return not bool(ctx.get_tag(self.tag.name, False))


class RisingEdgeCondition(Condition):
    """Rising edge detection - true only on 0->1 transition.

    Reads previous value from state.memory["_prev:{tag.name}"].
    """

    def __init__(self, tag: Tag):
        self.tag = tag

    def evaluate(self, ctx: ScanContext) -> bool:
        current = bool(ctx.get_tag(self.tag.name, False))
        previous = bool(ctx.get_memory(f"_prev:{self.tag.name}", False))
        return current and not previous


class FallingEdgeCondition(Condition):
    """Falling edge detection - true only on 1->0 transition.

    Reads previous value from state.memory["_prev:{tag.name}"].
    """

    def __init__(self, tag: Tag):
        self.tag = tag

    def evaluate(self, ctx: ScanContext) -> bool:
        current = bool(ctx.get_tag(self.tag.name, False))
        previous = bool(ctx.get_memory(f"_prev:{self.tag.name}", False))
        return not current and previous


# =============================================================================
# Indirect Comparison Conditions
# =============================================================================


class IndirectCompareEq(Condition):
    """Equality comparison for IndirectRef: indirect_ref == value or indirect_ref == tag."""

    def __init__(self, indirect_ref: IndirectRef, value: Any):
        self.indirect_ref = indirect_ref
        self.value = value

    def evaluate(self, ctx: ScanContext) -> bool:
        from pyrung.core.tag import Tag

        resolved_tag = self.indirect_ref.resolve_ctx(ctx)
        resolved_value = ctx.get_tag(resolved_tag.name, resolved_tag.default)
        if isinstance(self.value, Tag):
            other_value = ctx.get_tag(self.value.name)
        else:
            other_value = self.value
        return resolved_value == other_value


class IndirectCompareNe(Condition):
    """Inequality comparison for IndirectRef: indirect_ref != value or indirect_ref != tag."""

    def __init__(self, indirect_ref: IndirectRef, value: Any):
        self.indirect_ref = indirect_ref
        self.value = value

    def evaluate(self, ctx: ScanContext) -> bool:
        from pyrung.core.tag import Tag

        resolved_tag = self.indirect_ref.resolve_ctx(ctx)
        resolved_value = ctx.get_tag(resolved_tag.name, resolved_tag.default)
        if isinstance(self.value, Tag):
            other_value = ctx.get_tag(self.value.name)
        else:
            other_value = self.value
        return resolved_value != other_value


class IndirectCompareLt(Condition):
    """Less-than comparison for IndirectRef: indirect_ref < value."""

    def __init__(self, indirect_ref: IndirectRef, value: Any):
        self.indirect_ref = indirect_ref
        self.value = value

    def evaluate(self, ctx: ScanContext) -> bool:
        resolved_tag = self.indirect_ref.resolve_ctx(ctx)
        tag_value = ctx.get_tag(resolved_tag.name, resolved_tag.default)
        return tag_value < self.value


class IndirectCompareLe(Condition):
    """Less-than-or-equal comparison for IndirectRef: indirect_ref <= value."""

    def __init__(self, indirect_ref: IndirectRef, value: Any):
        self.indirect_ref = indirect_ref
        self.value = value

    def evaluate(self, ctx: ScanContext) -> bool:
        resolved_tag = self.indirect_ref.resolve_ctx(ctx)
        tag_value = ctx.get_tag(resolved_tag.name, resolved_tag.default)
        return tag_value <= self.value


class IndirectCompareGt(Condition):
    """Greater-than comparison for IndirectRef: indirect_ref > value."""

    def __init__(self, indirect_ref: IndirectRef, value: Any):
        self.indirect_ref = indirect_ref
        self.value = value

    def evaluate(self, ctx: ScanContext) -> bool:
        resolved_tag = self.indirect_ref.resolve_ctx(ctx)
        tag_value = ctx.get_tag(resolved_tag.name, resolved_tag.default)
        return tag_value > self.value


class IndirectCompareGe(Condition):
    """Greater-than-or-equal comparison for IndirectRef: indirect_ref >= value."""

    def __init__(self, indirect_ref: IndirectRef, value: Any):
        self.indirect_ref = indirect_ref
        self.value = value

    def evaluate(self, ctx: ScanContext) -> bool:
        resolved_tag = self.indirect_ref.resolve_ctx(ctx)
        tag_value = ctx.get_tag(resolved_tag.name, resolved_tag.default)
        return tag_value >= self.value


# =============================================================================
# Composite Conditions (any_of)
# =============================================================================


class AnyCondition(Condition):
    """OR condition - true when any sub-condition is true.

    Example:
        with Rung(Step == 1, any_of(Start, oCmdStart)):
            out(Light)
    """

    def __init__(self, *conditions: Condition | Tag):
        from pyrung.core.tag import Tag, TagType

        self.conditions: list[Condition] = []
        for cond in conditions:
            if isinstance(cond, Tag):
                if cond.type == TagType.BOOL:
                    self.conditions.append(BitCondition(cond))
                else:
                    raise TypeError(
                        f"Non-BOOL tag '{cond.name}' cannot be used directly as condition. "
                        "Use comparison operators: tag == value, tag > 0, etc."
                    )
            elif isinstance(cond, Condition):
                self.conditions.append(cond)
            else:
                raise TypeError(f"Expected Condition or Tag, got {type(cond)}")

    def evaluate(self, ctx: ScanContext) -> bool:
        return any(cond.evaluate(ctx) for cond in self.conditions)

    def __or__(self, other: Condition | Tag) -> AnyCondition:
        """Support chaining: (A | B) | C flattens to AnyCondition(A, B, C)."""
        from pyrung.core.tag import Tag

        if isinstance(other, Condition | Tag):
            return AnyCondition(*self.conditions, other)
        return NotImplemented

    def __ror__(self, other: Condition | Tag) -> AnyCondition:
        """Support reverse: C | (A | B)."""
        from pyrung.core.tag import Tag

        if isinstance(other, Condition | Tag):
            return AnyCondition(other, *self.conditions)
        return NotImplemented
