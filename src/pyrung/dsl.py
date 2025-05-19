from typing import Any, Callable, Union, List, Optional, Set

# Import our classes for IDE completions
from plc import PLC
from registry import get_current_plc
from memory_model import PLCExecutionContext, PLCVariable
from conditions import (
    BitCondition,
    NormallyClosedCondition,
    RisingEdgeCondition,
    FallingEdgeCondition,
)
from instructions import (
    CallInstruction,
    OutInstruction,
    LatchInstruction,
    ResetInstruction,
    CopyInstruction,
    CopyBlockInstruction,
    CopyFillInstruction,
    CopyPackInstruction,
    CopyUnpackInstruction,
    MathInstruction,
)
from program import Rung, Branch, Subroutine


def requires_rung_context_or_branch(func):
    """Decorator ensuring a function is called within an active rung or branch context."""

    def wrapper(*args, **kwargs):
        plc = get_current_plc()
        if not plc:
            raise RuntimeError(f"{func.__name__}() called with no active PLC context")

        # Check for branch context first (innermost)
        current_branch = plc.program.get_current_branch()
        if current_branch:
            kwargs["plc"] = plc
            kwargs["current_context"] = current_branch
            return func(*args, **kwargs)

        # Check for rung context
        current_rung = plc.program.get_current_rung()
        if not current_rung:
            raise RuntimeError(f"{func.__name__}() called outside of a Rung or branch context")

        # Add plc and current_rung as keyword arguments
        kwargs["plc"] = plc
        kwargs["current_context"] = current_rung

        return func(*args, **kwargs)

    return wrapper


# DSL Functions


def nc(variable: PLCVariable):
    """Create a normally closed contact (XIO)"""
    return NormallyClosedCondition(variable)


def re(variable: PLCVariable):
    """Create a rising edge detection (ONS)"""
    return RisingEdgeCondition(variable)


def fe(variable: PLCVariable):
    """Create a falling edge detection"""
    return FallingEdgeCondition(variable)


@requires_rung_context_or_branch
def out(target: PLCVariable, oneshot: bool = False, *, plc=None, current_context=None):
    """Create an output coil instruction"""
    # Create and add the instruction
    instruction = OutInstruction(target, oneshot)
    current_context.add_instruction(instruction)

    # Register this variable for auto-reset when rung goes false
    current_context.add_coil_output(target)

    return target


@requires_rung_context_or_branch
def latch(target: PLCVariable, *, plc=None, current_context=None):
    """Create a latch coil instruction"""
    # Create and add the instruction
    instruction = LatchInstruction(target)
    current_context.add_instruction(instruction)

    return target


@requires_rung_context_or_branch
def reset(target: PLCVariable, *, plc=None, current_context=None):
    """Create an unlatch coil instruction"""

    # Create and add the instruction
    instruction = ResetInstruction(target)
    current_context.add_instruction(instruction)

    return target


@requires_rung_context_or_branch
def copy(
    source: Union[PLCVariable, Any],
    dest: PLCVariable,
    oneshot: bool = False,
    *,
    plc=None,
    current_context=None,
):
    """Create a basic copy instruction"""
    instruction = CopyInstruction(source, dest, oneshot)
    current_context.add_instruction(instruction)
    return dest


@requires_rung_context_or_branch
def copy_block(
    source_start: PLCVariable,
    source_end: PLCVariable,
    dest_start: PLCVariable,
    oneshot: bool = False,
    *,
    plc=None,
    current_context=None,
):
    """Create a copy block instruction"""
    instruction = CopyBlockInstruction(source_start, source_end, dest_start, oneshot)
    current_context.add_instruction(instruction)
    return dest_start


@requires_rung_context_or_branch
def copy_fill(
    source: Union[PLCVariable, Any],
    dest_start: PLCVariable,
    dest_end: PLCVariable,
    oneshot: bool = False,
    *,
    plc=None,
    current_context=None,
):
    """Create a copy fill instruction"""
    instruction = CopyFillInstruction(source, dest_start, count, oneshot)
    current_context.add_instruction(instruction)
    return dest_start


@requires_rung_context_or_branch
def copy_pack(
    source_start: PLCVariable,
    source_end: PLCVariable,
    dest: PLCVariable,
    oneshot: bool = False,
    *,
    plc=None,
    current_context=None,
):
    """Create a copy pack instruction"""
    instruction = CopyPackInstruction(source_bit_start, dest_word, bit_count, oneshot)
    current_context.add_instruction(instruction)
    return dest_word


@requires_rung_context_or_branch
def copy_unpack(
    source_word: PLCVariable,
    dest_start: PLCVariable,
    dest_end: PLCVariable,
    oneshot: bool = False,
    *,
    plc=None,
    current_context=None,
):
    """Create a copy unpack instruction"""
    instruction = CopyUnpackInstruction(source_word, dest_bit_start, bit_count, oneshot)
    current_context.add_instruction(instruction)
    return dest_bit_start


@requires_rung_context_or_branch
def math_decimal(
    expression_func: Callable[[], Any],
    target: PLCVariable,
    oneshot: bool = False,
    *,
    plc=None,
    current_context=None,
):
    """Create a math instruction"""

    # Create and add the instruction
    instruction = MathInstruction(expression_func, target, oneshot)
    current_context.add_instruction(instruction)

    return target


@requires_rung_context_or_branch
def call(subroutine_name: str, *, plc=None, current_context=None):
    """Create a call instruction to execute a subroutine"""
    instruction = CallInstruction(subroutine_name)
    current_context.add_instruction(instruction)
    return subroutine_name


class BranchContextManager(Branch):
    """Context manager for Branch definition"""

    def __init__(self, *conditions):
        super().__init__(*conditions)

    def __enter__(self):
        plc = get_current_plc()
        if not plc:
            raise RuntimeError("No active PLC context")

        # A branch must be defined within a Rung
        current_rung = plc.program.get_current_rung()
        if not current_rung:
            raise RuntimeError("branch() must be used inside a Rung context")

        # Set parent rung and add to its branches
        self.parent_rung = current_rung
        current_rung.add_branch(self)

        # Push to stack for DSL functions to use
        plc.program.push_branch_context(self)

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        plc = get_current_plc()
        if not plc:
            raise RuntimeError("No active PLC context")

        # Pop from stack
        plc.program.pop_branch_context()


class RungContextManager(Rung):
    """Context manager wrapper for Rung to handle enter/exit"""

    def __init__(self, *conditions):
        super().__init__(*conditions)

    def __enter__(self):
        plc = get_current_plc()
        if not plc:
            raise RuntimeError("No active PLC context")

        # Check if we're in a branch context
        current_branch = plc.program.get_current_branch()
        if current_branch:
            raise RuntimeError("Nested Rungs are not allowed within a Branch")

        # Check if we're in another rung context
        current_rung = plc.program.get_current_rung()
        if current_rung:
            raise RuntimeError("Nested Rungs are not allowed. Use sequential Rungs or branch()")

        # Check for subroutine context
        current_subroutine = plc.program.get_current_subroutine()
        if current_subroutine:
            # Add this rung to the current subroutine
            current_subroutine.add_rung(self)
        else:
            # Add to main program
            plc.program.main_program.add_rung(self)

        # Push to stack for DSL functions to use
        plc.program.push_rung_context(self)

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        plc = get_current_plc()
        if not plc:
            raise RuntimeError("No active PLC context")

        # Remove from stack
        plc.program.pop_rung_context()


class SubroutineContextManager:
    """Context manager for subroutine definition"""

    def __init__(self, name: str):
        self.name = name
        self.subroutine = None

    def __enter__(self):
        plc = get_current_plc()
        if not plc:
            raise RuntimeError("No active PLC context")

        # Check we're not in another context
        if (
            plc.program.get_current_rung()
            or plc.program.get_current_branch()
            or plc.program.get_current_subroutine()
        ):
            raise RuntimeError("Subroutine cannot be defined inside another context")

        # Create subroutine
        self.subroutine = Subroutine(self.name)

        # Register in program
        if self.name in plc.program.subroutines:
            raise ValueError(f"Subroutine '{self.name}' already defined")
        plc.program.subroutines[self.name] = self.subroutine

        # Push to stack
        plc.program.push_subroutine_context(self.subroutine)

        return self.subroutine

    def __exit__(self, exc_type, exc_val, exc_tb):
        plc = get_current_plc()
        if plc:
            plc.program.pop_subroutine_context()


# Export
branch = BranchContextManager
Rung = RungContextManager
subroutine = SubroutineContextManager


# Constants for timer units
class TimeUnit:
    Td = "days"
    Th = "hours"
    Tm = "minutes"
    Ts = "seconds"
    Tms = "milliseconds"
