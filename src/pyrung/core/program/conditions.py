from __future__ import annotations

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


def nc(tag: Tag) -> NormallyClosedCondition:
    """Normally closed contact (XIO).

    True when tag is False/0.

    Example:
        with Rung(StartButton, nc(StopButton)):
            latch(MotorRunning)
    """
    cond = NormallyClosedCondition(tag)
    cond.source_file, cond.source_line = _capture_source(depth=2)
    return cond


def rise(tag: Tag) -> RisingEdgeCondition:
    """Rising edge contact (RE).

    True only on 0->1 transition. Requires PLCRunner to track previous values.

    Example:
        with Rung(rise(Button)):
            latch(MotorRunning)  # Latches on button press, not while held
    """
    cond = RisingEdgeCondition(tag)
    cond.source_file, cond.source_line = _capture_source(depth=2)
    return cond


def fall(tag: Tag) -> FallingEdgeCondition:
    """Falling edge contact (FE).

    True only on 1->0 transition. Requires PLCRunner to track previous values.

    Example:
        with Rung(fall(Button)):
            reset(MotorRunning)  # Resets when button is released
    """
    cond = FallingEdgeCondition(tag)
    cond.source_file, cond.source_line = _capture_source(depth=2)
    return cond


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
    cond = AnyCondition(*conditions)
    cond.source_file, cond.source_line = _capture_source(depth=2)
    for child in cond.conditions:
        if child.source_file is None:
            child.source_file = cond.source_file
        if child.source_line is None:
            child.source_line = cond.source_line
    return cond


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
    cond = AllCondition(*conditions)
    cond.source_file, cond.source_line = _capture_source(depth=2)
    for child in cond.conditions:
        if child.source_file is None:
            child.source_file = cond.source_file
        if child.source_line is None:
            child.source_line = cond.source_line
    return cond
