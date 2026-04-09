from __future__ import annotations

from typing import TypeVar

from pyrung.core._source import (
    _capture_source,
)
from pyrung.core.condition import (
    AllCondition,
    AnyCondition,
    Condition,
    FallingEdgeCondition,
    RisingEdgeCondition,
)
from pyrung.core.tag import ImmediateRef, Tag

ConditionType = TypeVar("ConditionType", bound=Condition)


def _make_condition(
    condition_cls: type[ConditionType],
    *args: object,
    source_depth: int = 3,
    propagate_children: bool = False,
) -> ConditionType:
    """Construct a condition and attach call-site source metadata."""
    condition = condition_cls(*args)
    source_file, source_line = _capture_source(depth=source_depth)
    condition.source_file, condition.source_line = source_file, source_line

    if propagate_children and hasattr(condition, "conditions"):
        for child in condition.conditions:  # ty: ignore[not-iterable]
            if child.source_file is None:
                child.source_file = source_file
            if child.source_line is None:
                child.source_line = source_line

    return condition


def rise(tag: Tag | ImmediateRef) -> RisingEdgeCondition:
    """Rising edge contact (RE).

    True only on 0->1 transition. Requires PLC to track previous values.

    Example:
        with Rung(rise(Button)):
            latch(MotorRunning)  # Latches on button press, not while held
    """
    return _make_condition(RisingEdgeCondition, tag)


def fall(tag: Tag | ImmediateRef) -> FallingEdgeCondition:
    """Falling edge contact (FE).

    True only on 1->0 transition. Requires PLC to track previous values.

    Example:
        with Rung(fall(Button)):
            reset(MotorRunning)  # Resets when button is released
    """
    return _make_condition(FallingEdgeCondition, tag)


def Or(
    *conditions: Condition | Tag | ImmediateRef,
) -> AnyCondition:
    """OR condition - true when any sub-condition is true.

    Use this to combine multiple conditions with OR logic within a rung.
    Multiple conditions passed directly to Rung() are ANDed together.

    Example:
        with Rung(Step == 1, Or(Start, CmdStart)):
            out(Light)  # True if Step==1 AND (Start OR CmdStart)

        # Grouped AND inside OR (explicit):
        with Rung(Or(Start, And(AutoMode, Ready), RemoteStart)):
            out(Light)

    Args:
        conditions: Conditions to OR together.

    Returns:
        AnyCondition that evaluates True if any sub-condition is True.
    """
    return _make_condition(
        AnyCondition,
        *conditions,
        propagate_children=True,
    )


def And(
    *conditions: Condition
    | Tag
    | ImmediateRef
    | tuple[Condition | Tag | ImmediateRef, ...]
    | list[Condition | Tag | ImmediateRef],
) -> AllCondition:
    """AND condition - true when all sub-conditions are true.

    This is equivalent to comma-separated rung conditions, but useful when building
    grouped condition trees with Or().

    Example:
        with Rung(And(Ready, AutoMode)):
            out(StartPermissive)

        with Rung(Or(And(Ready, AutoMode), RemoteStart)):
            out(StartPermissive)
    """
    return _make_condition(
        AllCondition,
        *conditions,
        propagate_children=True,
    )
