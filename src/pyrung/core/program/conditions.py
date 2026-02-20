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
    NormallyClosedCondition,
    RisingEdgeCondition,
)
from pyrung.core.tag import Tag

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
        for child in condition.conditions:  # type: ignore[attr-defined]
            if child.source_file is None:
                child.source_file = source_file
            if child.source_line is None:
                child.source_line = source_line

    return condition


def nc(tag: Tag) -> NormallyClosedCondition:
    """Normally closed contact (XIO).

    True when tag is False/0.

    Example:
        with Rung(StartButton, nc(StopButton)):
            latch(MotorRunning)
    """
    return _make_condition(NormallyClosedCondition, tag)


def rise(tag: Tag) -> RisingEdgeCondition:
    """Rising edge contact (RE).

    True only on 0->1 transition. Requires PLCRunner to track previous values.

    Example:
        with Rung(rise(Button)):
            latch(MotorRunning)  # Latches on button press, not while held
    """
    return _make_condition(RisingEdgeCondition, tag)


def fall(tag: Tag) -> FallingEdgeCondition:
    """Falling edge contact (FE).

    True only on 1->0 transition. Requires PLCRunner to track previous values.

    Example:
        with Rung(fall(Button)):
            reset(MotorRunning)  # Resets when button is released
    """
    return _make_condition(FallingEdgeCondition, tag)


def any_of(
    *conditions: Condition | Tag,
) -> AnyCondition:
    """OR condition - true when any sub-condition is true.

    Use this to combine multiple conditions with OR logic within a rung.
    Multiple conditions passed directly to Rung() are ANDed together.

    Example:
        with Rung(Step == 1, any_of(Start, CmdStart)):
            out(Light)  # True if Step==1 AND (Start OR CmdStart)

        # Also works with | operator:
        with Rung(Step == 1, Start | CmdStart):
            out(Light)

        # Grouped AND inside OR (explicit):
        with Rung(any_of(Start, all_of(AutoMode, Ready), RemoteStart)):
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


def all_of(
    *conditions: Condition | Tag | tuple[Condition | Tag, ...] | list[Condition | Tag],
) -> AllCondition:
    """AND condition - true when all sub-conditions are true.

    This is equivalent to comma-separated rung conditions, but useful when building
    grouped condition trees with any_of() or `&`.

    Example:
        with Rung(all_of(Ready, AutoMode)):
            out(StartPermissive)

        # Equivalent operator form:
        with Rung((Ready & AutoMode) | RemoteStart):
            out(StartPermissive)
    """
    return _make_condition(
        AllCondition,
        *conditions,
        propagate_children=True,
    )
