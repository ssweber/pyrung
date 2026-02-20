from __future__ import annotations

import inspect
import os
from collections.abc import Callable
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
    DebugInstructionSubStep,
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


def _capture_rung_condition_and_source(
    func_name: str,
    *,
    source_depth: int = 3,
) -> tuple[Any, str | None, int | None]:
    """Capture current rung combined condition and source location."""
    ctx = _require_rung_context(func_name)
    source_file, source_line = _capture_source(depth=source_depth)
    return ctx._rung._get_combined_condition(), source_file, source_line


def _capture_chained_method_source() -> tuple[str | None, int | None]:
    """Capture source location for a chained builder method call site.

    Walks stack frames until it finds the first frame outside this module,
    so debugger steps always anchor to user code, never builder internals.
    """
    frame = inspect.currentframe()
    if frame is None:
        return None, None

    module_file = os.path.normcase(os.path.abspath(__file__))
    try:
        current = frame.f_back
        while current is not None:
            filename = current.f_code.co_filename
            normalized = os.path.normcase(os.path.abspath(filename))
            if normalized != module_file:
                return filename, current.f_lineno
            current = current.f_back
    finally:
        del frame
    return None, None


class _BuilderBase:
    """Shared rung/source bookkeeping for terminal instruction builders."""

    def __init__(
        self,
        *,
        func_name: str,
        source_file: str | None = None,
        source_line: int | None = None,
    ) -> None:
        self._rung = _require_rung_context(func_name)
        self._source_file = source_file
        self._source_line = source_line

    def _append_instruction(self, instruction: Any) -> None:
        """Attach source metadata and append the built instruction."""
        instruction.source_file, instruction.source_line = self._source_file, self._source_line
        self._rung._rung.add_instruction(instruction)


class _AutoFinalizeBuilderBase(_BuilderBase):
    """Base for builders that can finalize once via explicit call or __del__."""

    def __init__(self, *, func_name: str, source_file: str | None, source_line: int | None) -> None:
        super().__init__(func_name=func_name, source_file=source_file, source_line=source_line)
        self._added = False

    def _append_once(self, instruction_factory: Callable[[], Any]) -> None:
        if self._added:
            return
        self._added = True
        self._append_instruction(instruction_factory())


class ShiftBuilder(_BuilderBase):
    """Builder for shift instruction with required .clock().reset() chaining."""

    def __init__(
        self,
        bit_range: BlockRange | IndirectBlockRange,
        data_condition: Any,
        source_file: str | None = None,
        source_line: int | None = None,
    ):
        super().__init__(func_name="shift", source_file=source_file, source_line=source_line)
        self._bit_range = bit_range
        self._data_condition = data_condition
        self._clock_condition: Condition | Tag | None = None
        self._clock_source_file: str | None = None
        self._clock_source_line: int | None = None
        self._reset_source_file: str | None = None
        self._reset_source_line: int | None = None

    def clock(self, condition: Condition | Tag) -> ShiftBuilder:
        """Set the shift clock trigger condition."""
        self._clock_source_file, self._clock_source_line = _capture_chained_method_source()
        self._clock_condition = condition
        return self

    def reset(self, condition: Condition | Tag) -> BlockRange | IndirectBlockRange:
        """Finalize the shift instruction with required reset condition."""
        self._reset_source_file, self._reset_source_line = _capture_chained_method_source()
        if self._clock_condition is None:
            raise RuntimeError("shift().clock(...) must be called before shift().reset(...)")

        instr = ShiftInstruction(
            bit_range=self._bit_range,
            data_condition=self._data_condition,
            clock_condition=self._clock_condition,
            reset_condition=condition,
        )
        instr.debug_substeps = (
            DebugInstructionSubStep(
                instruction_kind="Data",
                source_file=self._source_file,
                source_line=self._source_line,
                eval_mode="enabled",
                expression="Data",
            ),
            DebugInstructionSubStep(
                instruction_kind="Clock",
                source_file=self._clock_source_file or self._source_file,
                source_line=self._clock_source_line or self._source_line,
                eval_mode="condition",
                condition=instr.clock_condition,
            ),
            DebugInstructionSubStep(
                instruction_kind="Reset",
                source_file=self._reset_source_file or self._source_file,
                source_line=self._reset_source_line or self._source_line,
                eval_mode="condition",
                condition=instr.reset_condition,
            ),
        )
        self._append_instruction(instr)
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

    data_condition, source_file, source_line = _capture_rung_condition_and_source("shift")
    return ShiftBuilder(bit_range, data_condition, source_file, source_line)


class CountUpBuilder(_BuilderBase):
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
        super().__init__(func_name="count_up", source_file=source_file, source_line=source_line)
        self._done_bit = done_bit
        self._accumulator = accumulator
        self._setpoint = setpoint
        self._up_condition = up_condition  # From rung conditions
        self._down_condition: Condition | Tag | None = None
        self._reset_condition: Condition | Tag | None = None
        self._down_source_file: str | None = None
        self._down_source_line: int | None = None
        self._reset_source_file: str | None = None
        self._reset_source_line: int | None = None

    def down(self, condition: Condition | Tag) -> CountUpBuilder:
        """Add down trigger (optional).

        Creates a bidirectional counter that increments on rung true
        and decrements on down condition true.

        Args:
            condition: Condition for decrementing the counter.

        Returns:
            Self for chaining.
        """
        self._down_source_file, self._down_source_line = _capture_chained_method_source()
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
        self._reset_source_file, self._reset_source_line = _capture_chained_method_source()
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
        substeps: list[DebugInstructionSubStep] = [
            DebugInstructionSubStep(
                instruction_kind="Count Up",
                source_file=self._source_file,
                source_line=self._source_line,
                eval_mode="enabled",
                expression="Count Up",
            )
        ]
        if instr.down_condition is not None:
            substeps.append(
                DebugInstructionSubStep(
                    instruction_kind="Count Down",
                    source_file=self._down_source_file or self._source_file,
                    source_line=self._down_source_line or self._source_line,
                    eval_mode="condition",
                    condition=instr.down_condition,
                )
            )
        substeps.append(
            DebugInstructionSubStep(
                instruction_kind="Reset",
                source_file=self._reset_source_file or self._source_file,
                source_line=self._reset_source_line or self._source_line,
                eval_mode="condition",
                condition=instr.reset_condition,
            )
        )
        instr.debug_substeps = tuple(substeps)
        self._append_instruction(instr)
        return self._done_bit


class CountDownBuilder(_BuilderBase):
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
        super().__init__(func_name="count_down", source_file=source_file, source_line=source_line)
        self._done_bit = done_bit
        self._accumulator = accumulator
        self._setpoint = setpoint
        self._down_condition = down_condition  # From rung conditions
        self._reset_condition: Condition | Tag | None = None
        self._reset_source_file: str | None = None
        self._reset_source_line: int | None = None

    def reset(self, condition: Condition | Tag) -> Tag:
        """Add reset condition (required).

        When reset condition is true, loads setpoint into accumulator
        and clears done bit.

        Args:
            condition: Condition for resetting the counter.

        Returns:
            The done bit tag.
        """
        self._reset_source_file, self._reset_source_line = _capture_chained_method_source()
        self._reset_condition = condition
        # Now build and add the instruction
        instr = CountDownInstruction(
            self._done_bit,
            self._accumulator,
            self._setpoint,
            self._down_condition,
            self._reset_condition,
        )
        instr.debug_substeps = (
            DebugInstructionSubStep(
                instruction_kind="Count Down",
                source_file=self._source_file,
                source_line=self._source_line,
                eval_mode="enabled",
                expression="Count Down",
            ),
            DebugInstructionSubStep(
                instruction_kind="Reset",
                source_file=self._reset_source_file or self._source_file,
                source_line=self._reset_source_line or self._source_line,
                eval_mode="condition",
                condition=instr.reset_condition,
            ),
        )
        self._append_instruction(instr)
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
    up_condition, source_file, source_line = _capture_rung_condition_and_source("count_up")
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
    down_condition, source_file, source_line = _capture_rung_condition_and_source("count_down")
    return CountDownBuilder(
        done_bit,
        accumulator,
        setpoint,
        down_condition,
        source_file=source_file,
        source_line=source_line,
    )


class OnDelayBuilder(_AutoFinalizeBuilderBase):
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
        super().__init__(func_name="on_delay", source_file=source_file, source_line=source_line)
        self._done_bit = done_bit
        self._accumulator = accumulator
        self._setpoint = setpoint
        self._enable_condition = enable_condition
        self._time_unit = time_unit
        self._reset_condition: Condition | Tag | None = None
        self._reset_source_file: str | None = None
        self._reset_source_line: int | None = None

    def reset(self, condition: Condition | Tag) -> Tag:
        """Add reset condition (makes timer retentive - RTON).

        When reset condition is true, clears both done bit and accumulator.

        Args:
            condition: Condition for resetting the timer.

        Returns:
            The done bit tag.
        """
        self._reset_source_file, self._reset_source_line = _capture_chained_method_source()
        self._reset_condition = condition
        self._finalize()
        return self._done_bit

    def _finalize(self) -> None:
        """Build and add the instruction to the rung."""
        def _build_instruction() -> OnDelayInstruction:
            instr = OnDelayInstruction(
                self._done_bit,
                self._accumulator,
                self._setpoint,
                self._enable_condition,
                self._reset_condition,
                self._time_unit,
            )
            if instr.reset_condition is not None:
                instr.debug_substeps = (
                    DebugInstructionSubStep(
                        instruction_kind="Enable",
                        source_file=self._source_file,
                        source_line=self._source_line,
                        eval_mode="enabled",
                        expression="Enable",
                    ),
                    DebugInstructionSubStep(
                        instruction_kind="Reset",
                        source_file=self._reset_source_file or self._source_file,
                        source_line=self._reset_source_line or self._source_line,
                        eval_mode="condition",
                        condition=instr.reset_condition,
                    ),
                )
            return instr

        self._append_once(_build_instruction)

    def __del__(self) -> None:
        """Finalize on garbage collection if not explicitly called."""
        # This handles the case where .reset() is not called (TON behavior)
        self._finalize()


class OffDelayBuilder(_AutoFinalizeBuilderBase):
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
        super().__init__(func_name="off_delay", source_file=source_file, source_line=source_line)
        self._done_bit = done_bit
        self._accumulator = accumulator
        self._setpoint = setpoint
        self._enable_condition = enable_condition
        self._time_unit = time_unit

    def _finalize(self) -> None:
        """Build and add the instruction to the rung."""
        self._append_once(
            lambda: OffDelayInstruction(
                self._done_bit,
                self._accumulator,
                self._setpoint,
                self._enable_condition,
                self._time_unit,
            )
        )

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
    enable_condition, source_file, source_line = _capture_rung_condition_and_source("on_delay")
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
    enable_condition, source_file, source_line = _capture_rung_condition_and_source("off_delay")
    return OffDelayBuilder(
        done_bit,
        accumulator,
        setpoint,
        enable_condition,
        time_unit,
        source_file=source_file,
        source_line=source_line,
    )
