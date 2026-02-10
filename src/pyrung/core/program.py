"""Program and Rung context managers for the immutable PLC engine.

Provides DSL syntax for building PLC programs:

    with Program() as logic:
        with Rung(Button):
            out(Light)

    runner = PLCRunner(logic)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pyrung.core.condition import (
    AnyCondition,
    Condition,
    FallingEdgeCondition,
    NormallyClosedCondition,
    RisingEdgeCondition,
)
from pyrung.core.instruction import (
    BlockCopyInstruction,
    CallInstruction,
    CopyInstruction,
    CountDownInstruction,
    CountUpInstruction,
    FillInstruction,
    LatchInstruction,
    MathInstruction,
    OffDelayInstruction,
    OnDelayInstruction,
    OutInstruction,
    PackBitsInstruction,
    PackWordsInstruction,
    ResetInstruction,
    UnpackToBitsInstruction,
    UnpackToWordsInstruction,
)
from pyrung.core.rung import Rung as RungLogic
from pyrung.core.tag import Tag
from pyrung.core.time_mode import TimeUnit

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext
    from pyrung.core.state import SystemState


# Context stack for tracking current rung
_rung_stack: list[Rung] = []


def _current_rung() -> Rung | None:
    """Get the current rung context (if any)."""
    return _rung_stack[-1] if _rung_stack else None


def _require_rung_context(func_name: str) -> Rung:
    """Get current rung or raise error."""
    rung = _current_rung()
    if rung is None:
        raise RuntimeError(f"{func_name}() must be called inside a Rung context")
    return rung


class Program:
    """Container for PLC logic (rungs and subroutines).

    Used as a context manager to capture rungs:
        with Program() as logic:
            with Rung(Button):
                out(Light)

    Also works with PLCRunner:
        runner = PLCRunner(logic)
    """

    _current: Program | None = None

    def __init__(self) -> None:
        self.rungs: list[RungLogic] = []
        self.subroutines: dict[str, list[RungLogic]] = {}
        self._current_subroutine: str | None = None  # Track if we're in a subroutine

    def __enter__(self) -> Program:
        Program._current = self
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        Program._current = None

    def add_rung(self, rung: RungLogic) -> None:
        """Add a rung to the program or current subroutine."""
        if self._current_subroutine is not None:
            self.subroutines[self._current_subroutine].append(rung)
        else:
            self.rungs.append(rung)

    def start_subroutine(self, name: str) -> None:
        """Start defining a subroutine."""
        self._current_subroutine = name
        self.subroutines[name] = []

    def end_subroutine(self) -> None:
        """End subroutine definition."""
        self._current_subroutine = None

    def call_subroutine(self, name: str, state: SystemState) -> SystemState:
        """Execute a subroutine by name (legacy state-based API)."""
        # This method is kept for backwards compatibility but should not be used
        # with ScanContext-based execution. Use call_subroutine_ctx instead.
        from pyrung.core.context import ScanContext

        if name not in self.subroutines:
            raise KeyError(f"Subroutine '{name}' not defined")
        ctx = ScanContext(state)
        for rung in self.subroutines[name]:
            rung.evaluate(ctx)
        return ctx.commit(dt=0.0)

    def call_subroutine_ctx(self, name: str, ctx: ScanContext) -> None:
        """Execute a subroutine by name within a ScanContext."""
        if name not in self.subroutines:
            raise KeyError(f"Subroutine '{name}' not defined")
        for rung in self.subroutines[name]:
            rung.evaluate(ctx)

    @classmethod
    def current(cls) -> Program | None:
        """Get the current program context (if any)."""
        return cls._current

    def evaluate(self, ctx: ScanContext) -> None:
        """Evaluate all main rungs in order (not subroutines) within a ScanContext."""
        for rung in self.rungs:
            rung.evaluate(ctx)


class Rung:
    """Context manager for defining a rung.

    Example:
        with Rung(Button):
            out(Light)

        with Rung(Step == 0):
            out(Light1)
            copy(1, Step, oneshot=True)
    """

    def __init__(self, *conditions: Condition | Tag) -> None:
        self._rung = RungLogic(*conditions)

    def __enter__(self) -> Rung:
        _rung_stack.append(self)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        _rung_stack.pop()
        # Add rung to current program
        prog = Program.current()
        if prog is not None:
            prog.add_rung(self._rung)


# ============================================================================
# DSL Functions - called inside Rung context
# ============================================================================


def out(target: Tag, oneshot: bool = False) -> Tag:
    """Output coil instruction (OUT).

    Sets target to True when rung is true.
    Resets to False when rung goes false.

    Example:
        with Rung(Button):
            out(Light)
    """
    ctx = _require_rung_context("out")
    ctx._rung.add_instruction(OutInstruction(target, oneshot))
    ctx._rung.register_coil(target)
    return target


def latch(target: Tag) -> Tag:
    """Latch/Set instruction (SET).

    Sets target to True. Unlike OUT, does NOT reset when rung goes false.
    Use reset() to turn off.

    Example:
        with Rung(StartButton):
            latch(MotorRunning)
    """
    ctx = _require_rung_context("latch")
    ctx._rung.add_instruction(LatchInstruction(target))
    return target


def reset(target: Tag) -> Tag:
    """Reset/Unlatch instruction (RST).

    Sets target to its default value (False for bits, 0 for ints).

    Example:
        with Rung(StopButton):
            reset(MotorRunning)
    """
    ctx = _require_rung_context("reset")
    ctx._rung.add_instruction(ResetInstruction(target))
    return target


def copy(source: Tag | Any, target: Tag, oneshot: bool = False) -> Tag:
    """Copy instruction (CPY/MOV).

    Copies source value to target.

    Example:
        with Rung(Button):
            copy(5, StepNumber)
    """
    ctx = _require_rung_context("copy")
    ctx._rung.add_instruction(CopyInstruction(source, target, oneshot))
    return target


def blockcopy(source: Any, dest: Any, oneshot: bool = False) -> None:
    """Block copy instruction.

    Copies values from source BlockRange to dest BlockRange.
    Both ranges must have the same length.

    Example:
        with Rung(CopyEnable):
            blockcopy(DS.select(1, 10), DD.select(1, 10))

    Args:
        source: Source BlockRange or IndirectBlockRange from .select().
        dest: Dest BlockRange or IndirectBlockRange from .select().
        oneshot: If True, execute only once per rung activation.
    """
    ctx = _require_rung_context("blockcopy")
    ctx._rung.add_instruction(BlockCopyInstruction(source, dest, oneshot))


def fill(value: Any, dest: Any, oneshot: bool = False) -> None:
    """Fill instruction.

    Writes a constant value to every element in a BlockRange.

    Example:
        with Rung(ClearEnable):
            fill(0, DS.select(1, 100))

    Args:
        value: Value to write (literal, Tag, or Expression).
        dest: Dest BlockRange or IndirectBlockRange from .select().
        oneshot: If True, execute only once per rung activation.
    """
    ctx = _require_rung_context("fill")
    ctx._rung.add_instruction(FillInstruction(value, dest, oneshot))


def pack_bits(bit_block: Any, dest: Any, oneshot: bool = False) -> None:
    """Pack BOOL tags from a BlockRange into a register destination."""
    ctx = _require_rung_context("pack_bits")
    ctx._rung.add_instruction(PackBitsInstruction(bit_block, dest, oneshot))


def pack_words(word_block: Any, dest: Any, oneshot: bool = False) -> None:
    """Pack two 16-bit tags from a BlockRange into a 32-bit destination."""
    ctx = _require_rung_context("pack_words")
    ctx._rung.add_instruction(PackWordsInstruction(word_block, dest, oneshot))


def unpack_to_bits(source: Any, bit_block: Any, oneshot: bool = False) -> None:
    """Unpack a register source into BOOL tags in a BlockRange."""
    ctx = _require_rung_context("unpack_to_bits")
    ctx._rung.add_instruction(UnpackToBitsInstruction(source, bit_block, oneshot))


def unpack_to_words(source: Any, word_block: Any, oneshot: bool = False) -> None:
    """Unpack a 32-bit register source into two 16-bit tags in a BlockRange."""
    ctx = _require_rung_context("unpack_to_words")
    ctx._rung.add_instruction(UnpackToWordsInstruction(source, word_block, oneshot))


def math(expression: Any, dest: Tag, oneshot: bool = False, mode: str = "decimal") -> Tag:
    """Math instruction.

    Evaluates an expression and stores the result in dest, with
    truncation to the destination tag's bit width (modular wrapping).

    Key differences from copy():
    - Truncates result to destination tag's type width
    - Division by zero produces 0 (not infinity)
    - Supports "decimal" (signed) and "hex" (unsigned 16-bit) modes

    Example:
        with Rung(Enable):
            math(DS1 * DS2 + DS3, Result)
            math(MaskA & MaskB, MaskResult, mode="hex")

    Args:
        expression: Expression, Tag, or literal to evaluate.
        dest: Destination tag (type determines truncation width).
        oneshot: If True, execute only once per rung activation.
        mode: "decimal" (signed arithmetic) or "hex" (unsigned 16-bit wrap).

    Returns:
        The dest tag.
    """
    ctx = _require_rung_context("math")
    ctx._rung.add_instruction(MathInstruction(expression, dest, oneshot, mode))
    return dest


# ============================================================================
# Condition helpers - used in Rung conditions
# ============================================================================


def nc(tag: Tag) -> NormallyClosedCondition:
    """Normally closed contact (XIO).

    True when tag is False/0.

    Example:
        with Rung(StartButton, nc(StopButton)):
            latch(MotorRunning)
    """
    return NormallyClosedCondition(tag)


def rise(tag: Tag) -> RisingEdgeCondition:
    """Rising edge contact (RE).

    True only on 0->1 transition. Requires PLCRunner to track previous values.

    Example:
        with Rung(rise(Button)):
            latch(MotorRunning)  # Latches on button press, not while held
    """
    return RisingEdgeCondition(tag)


def fall(tag: Tag) -> FallingEdgeCondition:
    """Falling edge contact (FE).

    True only on 1->0 transition. Requires PLCRunner to track previous values.

    Example:
        with Rung(fall(Button)):
            reset(MotorRunning)  # Resets when button is released
    """
    return FallingEdgeCondition(tag)


def any_of(*conditions: Condition | Tag) -> AnyCondition:
    """OR condition - true when any sub-condition is true.

    Use this to combine multiple conditions with OR logic within a rung.
    Multiple conditions passed directly to Rung() are ANDed together.

    Example:
        with Rung(Step == 1, any_of(Start, CmdStart)):
            out(Light)  # True if Step==1 AND (Start OR CmdStart)

        # Also works with | operator:
        with Rung(Step == 1, Start | CmdStart):
            out(Light)

    Args:
        conditions: Two or more conditions (Tags or Conditions) to OR together.

    Returns:
        AnyCondition that evaluates True if any sub-condition is True.
    """
    return AnyCondition(*conditions)


def call(target: str | SubroutineFunc) -> None:
    """Call a subroutine instruction.

    Executes the named subroutine when the rung is true.
    Accepts either a string name or a @subroutine-decorated function.

    Example:
        with Rung(Button):
            call("init_sequence")

        with subroutine("init_sequence"):
            with Rung():
                out(Light)

        # Or with decorator:
        @subroutine("init")
        def init_sequence():
            with Rung():
                out(Light)

        with Program() as logic:
            with Rung(Button):
                call(init_sequence)
    """
    ctx = _require_rung_context("call")
    prog = Program.current()
    if prog is None:
        raise RuntimeError("call() must be used inside a Program context")

    if isinstance(target, SubroutineFunc):
        name = target.name
        if name not in prog.subroutines:
            target._register(prog)
    else:
        name = target

    ctx._rung.add_instruction(CallInstruction(name, prog))


# ============================================================================
# Decorator
# ============================================================================


def program(fn: Callable[[], None]) -> Program:
    """Decorator to create a Program from a function.

    Example:
        @program
        def my_logic():
            with Rung(Button):
                out(Light)

        runner = PLCRunner(my_logic)
    """
    prog = Program()
    with prog:
        fn()
    return prog


# ============================================================================
# Subroutine - named block of rungs
# ============================================================================


class Subroutine:
    """Context manager for defining a subroutine.

    Subroutines are named blocks of rungs that are only executed when called.

    Example:
        with subroutine("my_sub"):
            with Rung():
                out(Light)
    """

    def __init__(self, name: str) -> None:
        self._name = name

    def __enter__(self) -> Subroutine:
        prog = Program.current()
        if prog is None:
            raise RuntimeError("subroutine() must be used inside a Program context")
        prog.start_subroutine(self._name)
        return self

    def __call__(self, fn: Callable[[], None]) -> SubroutineFunc:
        """Use subroutine() as a decorator.

        Example:
            @subroutine("init")
            def init_sequence():
                with Rung():
                    out(Light)
        """
        return SubroutineFunc(self._name, fn)

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        prog = Program.current()
        if prog is not None:
            prog.end_subroutine()


class SubroutineFunc:
    """A decorated function that represents a subroutine.

    Created by using @subroutine("name") as a decorator. When passed to call(),
    auto-registers with the current Program on first use.

    Example:
        @subroutine("init")
        def init_sequence():
            with Rung():
                out(Light)

        with Program() as logic:
            with Rung(Button):
                call(init_sequence)
    """

    def __init__(self, name: str, fn: Callable[[], None]) -> None:
        self._name = name
        self._fn = fn

    @property
    def name(self) -> str:
        """The subroutine name."""
        return self._name

    def _register(self, prog: Program) -> None:
        """Register this subroutine's rungs with a Program."""
        prog.start_subroutine(self._name)
        self._fn()
        prog.end_subroutine()


def subroutine(name: str) -> Subroutine:
    """Define a named subroutine.

    Subroutines are only executed when called via call().
    They are NOT executed during normal program scan.

    Example:
        with Program() as logic:
            with Rung(Button):
                call("my_sub")

            with subroutine("my_sub"):
                with Rung():
                    out(Light)
    """
    return Subroutine(name)


# ============================================================================
# Branch - parallel path within a rung
# ============================================================================


class Branch:
    """Context manager for a parallel branch within a rung.

    A branch executes when both the parent rung conditions AND
    the branch's own conditions are true.

    Example:
        with Rung(Step == 0):
            out(Light1)
            with branch(AutoMode):  # Only executes if Step==0 AND AutoMode
                out(Light2)
                copy(1, Step, oneshot=True)
    """

    def __init__(self, *conditions: Condition | Tag) -> None:
        """Create a branch with additional conditions.

        Args:
            conditions: Conditions that must be true (in addition to parent rung)
                        for this branch's instructions to execute.
        """
        self._conditions = list(conditions)
        self._branch_rung: RungLogic | None = None
        self._parent_ctx: Rung | None = None
        self._branch_ctx: Rung | None = None

    def __enter__(self) -> Branch:
        # Get parent rung context
        self._parent_ctx = _current_rung()
        if self._parent_ctx is None:
            raise RuntimeError("branch() must be called inside a Rung context")

        # Create a nested rung for the branch that includes BOTH parent and branch conditions
        # This ensures terminal instructions (counters, timers) see the full condition chain
        parent_conditions = self._parent_ctx._rung._conditions
        combined_conditions = parent_conditions + self._conditions
        self._branch_rung = RungLogic(*combined_conditions)

        # Push a new "fake" rung context so instructions go to the branch
        self._branch_ctx = Rung.__new__(Rung)
        self._branch_ctx._rung = self._branch_rung
        _rung_stack.append(self._branch_ctx)

        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        # Pop our branch context
        _rung_stack.pop()

        # Add the branch as a nested rung to the parent
        if self._parent_ctx is not None and self._branch_rung is not None:
            self._parent_ctx._rung.add_branch(self._branch_rung)


def branch(*conditions: Condition | Tag) -> Branch:
    """Create a parallel branch within a rung.

    A branch executes when both the parent rung conditions AND
    the branch's own conditions are true.

    Example:
        with Rung(Step == 0):
            out(Light1)
            with branch(AutoMode):  # Only executes if Step==0 AND AutoMode
                out(Light2)
                copy(1, Step, oneshot=True)

    Args:
        conditions: Conditions that must be true (in addition to parent rung)
                    for this branch's instructions to execute.

    Returns:
        Branch context manager.
    """
    return Branch(*conditions)


# Backwards compatibility alias
RungContext = Rung


# ============================================================================
# Counter Instructions - chaining API
# ============================================================================


class CountUpBuilder:
    """Builder for count_up instruction with chaining API (Click-style).

    Supports optional .down() and required .reset() chaining:
        count_up(done, acc, setpoint=100).reset(reset_tag)
        count_up(done, acc, setpoint=50).down(down_cond).reset(reset_tag)
    """

    def __init__(self, done_bit: Tag, accumulator: Tag, setpoint: Tag | int, up_condition: Any):
        self._done_bit = done_bit
        self._accumulator = accumulator
        self._setpoint = setpoint
        self._up_condition = up_condition  # From rung conditions
        self._down_condition: Condition | Tag | None = None
        self._reset_condition: Condition | Tag | None = None
        self._rung = _require_rung_context("count_up")

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
        self._rung._rung.add_instruction(instr)
        return self._done_bit


class CountDownBuilder:
    """Builder for count_down instruction with chaining API (Click-style).

    Supports required .reset() chaining:
        count_down(done, acc, setpoint=25).reset(reset_tag)
    """

    def __init__(self, done_bit: Tag, accumulator: Tag, setpoint: Tag | int, down_condition: Any):
        self._done_bit = done_bit
        self._accumulator = accumulator
        self._setpoint = setpoint
        self._down_condition = down_condition  # From rung conditions
        self._reset_condition: Condition | Tag | None = None
        self._rung = _require_rung_context("count_down")

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
    return CountUpBuilder(done_bit, accumulator, setpoint, up_condition)


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
    return CountDownBuilder(done_bit, accumulator, setpoint, down_condition)


# ============================================================================
# Timer Instructions - chaining API
# ============================================================================


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
    ):
        self._done_bit = done_bit
        self._accumulator = accumulator
        self._setpoint = setpoint
        self._enable_condition = enable_condition
        self._time_unit = time_unit
        self._reset_condition: Condition | Tag | None = None
        self._rung = _require_rung_context("on_delay")
        self._added = False

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
    ):
        self._done_bit = done_bit
        self._accumulator = accumulator
        self._setpoint = setpoint
        self._enable_condition = enable_condition
        self._time_unit = time_unit
        self._rung = _require_rung_context("off_delay")
        self._added = False

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
    return OnDelayBuilder(done_bit, accumulator, setpoint, enable_condition, time_unit)


def off_delay(
    done_bit: Tag,
    accumulator: Tag,
    setpoint: int,
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
        setpoint: Delay time in time units.
        time_unit: Time unit for accumulator (default: Tms).

    Returns:
        Builder for the off_delay instruction.
    """
    ctx = _require_rung_context("off_delay")
    enable_condition = ctx._rung._get_combined_condition()
    return OffDelayBuilder(done_bit, accumulator, setpoint, enable_condition, time_unit)
