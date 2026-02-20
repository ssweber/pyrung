from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core._source import (
    _capture_source,
)
from pyrung.core.condition import (
    Condition,
)
from pyrung.core.instruction import (
    CountDownInstruction,
    CountUpInstruction,
    OffDelayInstruction,
    OnDelayInstruction,
    ShiftInstruction,
)
from pyrung.core.memory_block import BlockRange
from pyrung.core.tag import Tag
from pyrung.core.time_mode import TimeUnit

from .context import _require_rung_context

if TYPE_CHECKING:
    from pyrung.core.memory_block import IndirectBlockRange


class ShiftBuilder:
    """Builder for shift instruction with required .clock().reset() chaining."""

    def __init__(
        self,
        bit_range: BlockRange | IndirectBlockRange,
        data_condition: Any,
        source_file: str | None = None,
        source_line: int | None = None,
    ):
        self._bit_range = bit_range
        self._data_condition = data_condition
        self._clock_condition: Condition | Tag | None = None
        self._rung = _require_rung_context("shift")
        self._source_file = source_file
        self._source_line = source_line

    def clock(self, condition: Condition | Tag) -> ShiftBuilder:
        """Set the shift clock trigger condition."""
        self._clock_condition = condition
        return self

    def reset(self, condition: Condition | Tag) -> BlockRange | IndirectBlockRange:
        """Finalize the shift instruction with required reset condition."""
        if self._clock_condition is None:
            raise RuntimeError("shift().clock(...) must be called before shift().reset(...)")

        instr = ShiftInstruction(
            bit_range=self._bit_range,
            data_condition=self._data_condition,
            clock_condition=self._clock_condition,
            reset_condition=condition,
        )
        instr.source_file, instr.source_line = self._source_file, self._source_line
        self._rung._rung.add_instruction(instr)
        return self._bit_range


def shift(bit_range: BlockRange | IndirectBlockRange) -> ShiftBuilder:
    """Shift register instruction builder.

    Data input comes from current rung power. Use .clock(...) then .reset(...)
    to finalize and add the instruction.

    Example:
        with Rung(DataBit):
            shift(C.select(2, 7)).clock(ClockBit).reset(ResetBit)
    """
    from pyrung.core.memory_block import BlockRange, IndirectBlockRange

    if not isinstance(bit_range, (BlockRange, IndirectBlockRange)):
        raise TypeError(
            f"shift() expects a BlockRange or IndirectBlockRange from .select(), "
            f"got {type(bit_range).__name__}"
        )

    ctx = _require_rung_context("shift")
    data_condition = ctx._rung._get_combined_condition()
    source_file, source_line = _capture_source(depth=2)
    return ShiftBuilder(bit_range, data_condition, source_file, source_line)


class CountUpBuilder:
    """Builder for count_up instruction with chaining API (Click-style).

    Supports optional .down() and required .reset() chaining:
        count_up(done, acc, setpoint=100).reset(reset_tag)
        count_up(done, acc, setpoint=50).down(down_cond).reset(reset_tag)
    """

    def __init__(
        self,
        done_bit: Tag,
        accumulator: Tag,
        setpoint: Tag | int,
        up_condition: Any,
        source_file: str | None = None,
        source_line: int | None = None,
    ):
        self._done_bit = done_bit
        self._accumulator = accumulator
        self._setpoint = setpoint
        self._up_condition = up_condition  # From rung conditions
        self._down_condition: Condition | Tag | None = None
        self._reset_condition: Condition | Tag | None = None
        self._rung = _require_rung_context("count_up")
        self._source_file = source_file
        self._source_line = source_line

    def down(self, condition: Condition | Tag) -> CountUpBuilder:
        """Add down trigger (optional).

        Creates a bidirectional counter that increments on rung true
        and decrements on down condition true.

        Args:
            condition: Condition for decrementing the counter.

        Returns:
            Self for chaining.
        """
        self._down_condition = condition
        return self

    def reset(self, condition: Condition | Tag) -> Tag:
        """Add reset condition (required).

        When reset condition is true, clears both done bit and accumulator.

        Args:
            condition: Condition for resetting the counter.

        Returns:
            The done bit tag.
        """
        self._reset_condition = condition
        # Now build and add the instruction
        instr = CountUpInstruction(
            self._done_bit,
            self._accumulator,
            self._setpoint,
            self._up_condition,
            self._reset_condition,
            self._down_condition,
        )
        instr.source_file, instr.source_line = self._source_file, self._source_line
        self._rung._rung.add_instruction(instr)
        return self._done_bit


class CountDownBuilder:
    """Builder for count_down instruction with chaining API (Click-style).

    Supports required .reset() chaining:
        count_down(done, acc, setpoint=25).reset(reset_tag)
    """

    def __init__(
        self,
        done_bit: Tag,
        accumulator: Tag,
        setpoint: Tag | int,
        down_condition: Any,
        source_file: str | None = None,
        source_line: int | None = None,
    ):
        self._done_bit = done_bit
        self._accumulator = accumulator
        self._setpoint = setpoint
        self._down_condition = down_condition  # From rung conditions
        self._reset_condition: Condition | Tag | None = None
        self._rung = _require_rung_context("count_down")
        self._source_file = source_file
        self._source_line = source_line

    def reset(self, condition: Condition | Tag) -> Tag:
        """Add reset condition (required).

        When reset condition is true, loads setpoint into accumulator
        and clears done bit.

        Args:
            condition: Condition for resetting the counter.

        Returns:
            The done bit tag.
        """
        self._reset_condition = condition
        # Now build and add the instruction
        instr = CountDownInstruction(
            self._done_bit,
            self._accumulator,
            self._setpoint,
            self._down_condition,
            self._reset_condition,
        )
        instr.source_file, instr.source_line = self._source_file, self._source_line
        self._rung._rung.add_instruction(instr)
        return self._done_bit


def count_up(
    done_bit: Tag,
    accumulator: Tag,
    setpoint: Tag | int,
) -> CountUpBuilder:
    """Count Up instruction (CTU) - Click-style.

    Creates a counter that increments on each rising edge of the rung condition.

    Example:
        with Rung(rise(PartSensor)):
            count_up(done_bit, acc, setpoint=100).reset(ResetBtn)

    This is a terminal instruction. Requires .reset() chaining.

    Args:
        done_bit: Tag to set when accumulator >= setpoint.
        accumulator: Tag to increment on each rising edge.
        setpoint: Target value (Tag or int).

    Returns:
        Builder for chaining .down() and .reset().
    """
    ctx = _require_rung_context("count_up")
    up_condition = ctx._rung._get_combined_condition()
    source_file, source_line = _capture_source(depth=2)
    return CountUpBuilder(
        done_bit,
        accumulator,
        setpoint,
        up_condition,
        source_file=source_file,
        source_line=source_line,
    )


def count_down(
    done_bit: Tag,
    accumulator: Tag,
    setpoint: Tag | int,
) -> CountDownBuilder:
    """Count Down instruction (CTD) - Click-style.

    Creates a counter that decrements on each rising edge of the rung condition.

    Example:
        with Rung(rise(Dispense)):
            count_down(done_bit, acc, setpoint=25).reset(Reload)

    This is a terminal instruction. Requires .reset() chaining.

    Args:
        done_bit: Tag to set when accumulator <= -setpoint.
        accumulator: Tag to decrement on each rising edge.
        setpoint: Target value (Tag or int).

    Returns:
        Builder for chaining .reset().
    """
    ctx = _require_rung_context("count_down")
    down_condition = ctx._rung._get_combined_condition()
    source_file, source_line = _capture_source(depth=2)
    return CountDownBuilder(
        done_bit,
        accumulator,
        setpoint,
        down_condition,
        source_file=source_file,
        source_line=source_line,
    )


class OnDelayBuilder:
    """Builder for on_delay instruction with optional .reset() chaining (Click-style).

    Without .reset(): TON behavior (auto-reset on rung false)
    With .reset(): RTON behavior (manual reset required)
    """

    def __init__(
        self,
        done_bit: Tag,
        accumulator: Tag,
        setpoint: Tag | int,
        enable_condition: Any,
        time_unit: TimeUnit,
        source_file: str | None = None,
        source_line: int | None = None,
    ):
        self._done_bit = done_bit
        self._accumulator = accumulator
        self._setpoint = setpoint
        self._enable_condition = enable_condition
        self._time_unit = time_unit
        self._reset_condition: Condition | Tag | None = None
        self._rung = _require_rung_context("on_delay")
        self._added = False
        self._source_file = source_file
        self._source_line = source_line

    def reset(self, condition: Condition | Tag) -> Tag:
        """Add reset condition (makes timer retentive - RTON).

        When reset condition is true, clears both done bit and accumulator.

        Args:
            condition: Condition for resetting the timer.

        Returns:
            The done bit tag.
        """
        self._reset_condition = condition
        self._finalize()
        return self._done_bit

    def _finalize(self) -> None:
        """Build and add the instruction to the rung."""
        if self._added:
            return
        self._added = True
        instr = OnDelayInstruction(
            self._done_bit,
            self._accumulator,
            self._setpoint,
            self._enable_condition,
            self._reset_condition,
            self._time_unit,
        )
        instr.source_file, instr.source_line = self._source_file, self._source_line
        self._rung._rung.add_instruction(instr)

    def __del__(self) -> None:
        """Finalize on garbage collection if not explicitly called."""
        # This handles the case where .reset() is not called (TON behavior)
        self._finalize()


class OffDelayBuilder:
    """Builder for off_delay instruction (TOF behavior, Click-style).

    Auto-resets when re-enabled.
    """

    def __init__(
        self,
        done_bit: Tag,
        accumulator: Tag,
        setpoint: Tag | int,
        enable_condition: Any,
        time_unit: TimeUnit,
        source_file: str | None = None,
        source_line: int | None = None,
    ):
        self._done_bit = done_bit
        self._accumulator = accumulator
        self._setpoint = setpoint
        self._enable_condition = enable_condition
        self._time_unit = time_unit
        self._rung = _require_rung_context("off_delay")
        self._added = False
        self._source_file = source_file
        self._source_line = source_line

    def _finalize(self) -> None:
        """Build and add the instruction to the rung."""
        if self._added:
            return
        self._added = True
        instr = OffDelayInstruction(
            self._done_bit,
            self._accumulator,
            self._setpoint,
            self._enable_condition,
            self._time_unit,
        )
        instr.source_file, instr.source_line = self._source_file, self._source_line
        self._rung._rung.add_instruction(instr)

    def __del__(self) -> None:
        """Finalize on garbage collection if not explicitly called."""
        self._finalize()


def on_delay(
    done_bit: Tag,
    accumulator: Tag,
    setpoint: Tag | int,
    time_unit: TimeUnit = TimeUnit.Tms,
) -> OnDelayBuilder:
    """On-Delay Timer instruction (TON/RTON) - Click-style.

    Accumulates time while rung is true.

    Example:
        with Rung(MotorRunning):
            on_delay(done_bit, acc, setpoint=5000)                 # TON
            on_delay(done_bit, acc, setpoint=5000).reset(ResetBtn) # RTON

    This is a terminal instruction (must be last in rung).
    Optional .reset() chaining for retentive behavior.

    Args:
        done_bit: Tag to set when accumulator >= setpoint.
        accumulator: Tag to increment while enabled.
        setpoint: Target value in time units (Tag or int).
        time_unit: Time unit for accumulator (default: Tms).

    Returns:
        Builder for optional .reset() chaining.
    """
    ctx = _require_rung_context("on_delay")
    enable_condition = ctx._rung._get_combined_condition()
    source_file, source_line = _capture_source(depth=2)
    return OnDelayBuilder(
        done_bit,
        accumulator,
        setpoint,
        enable_condition,
        time_unit,
        source_file=source_file,
        source_line=source_line,
    )


def off_delay(
    done_bit: Tag,
    accumulator: Tag,
    setpoint: Tag | int,
    time_unit: TimeUnit = TimeUnit.Tms,
) -> OffDelayBuilder:
    """Off-Delay Timer instruction (TOF) - Click-style.

    Done bit is True while enabled. After disable, counts until setpoint,
    then done bit goes False. Auto-resets when re-enabled.

    Example:
        with Rung(MotorCommand):
            off_delay(done_bit, acc, setpoint=10000)

    This is a terminal instruction (must be last in rung).

    Args:
        done_bit: Tag that stays True for setpoint time after rung goes false.
        accumulator: Tag to increment while disabled.
        setpoint: Delay time in time units (Tag or int).
        time_unit: Time unit for accumulator (default: Tms).

    Returns:
        Builder for the off_delay instruction.
    """
    ctx = _require_rung_context("off_delay")
    enable_condition = ctx._rung._get_combined_condition()
    source_file, source_line = _capture_source(depth=2)
    return OffDelayBuilder(
        done_bit,
        accumulator,
        setpoint,
        enable_condition,
        time_unit,
        source_file=source_file,
        source_line=source_line,
    )
