"""Pytest configuration and test helpers."""

from pyrung.core import SystemState
from pyrung.core.condition import Condition
from pyrung.core.context import ScanContext
from pyrung.core.instruction import Instruction
from pyrung.core.program import Program as ProgramLogic
from pyrung.core.rung import Rung


def execute(instr: Instruction, state: SystemState, *, dt: float = 0.0) -> SystemState:
    """Execute an instruction and return the new state.

    Test helper that wraps the ScanContext API for simpler unit testing
    of individual instructions.

    Args:
        instr: The instruction to execute.
        state: The initial system state.
        dt: Time delta in seconds (for timer instructions).

    Returns:
        New SystemState with the instruction's effects applied.
    """
    ctx = ScanContext(state)
    ctx.set_memory("_dt", dt)  # For timer instructions
    instr.execute(ctx, True)
    return ctx.commit(dt=dt)


def evaluate_rung(rung: Rung, state: SystemState, *, dt: float = 0.0) -> SystemState:
    """Evaluate a rung and return the new state.

    Test helper that wraps the ScanContext API for simpler unit testing
    of individual rungs.

    Args:
        rung: The rung to evaluate.
        state: The initial system state.
        dt: Time delta in seconds (for timer instructions).

    Returns:
        New SystemState with the rung's effects applied.
    """
    ctx = ScanContext(state)
    ctx.set_memory("_dt", dt)  # For timer instructions
    rung.evaluate(ctx)
    return ctx.commit(dt=dt)


def evaluate_condition(cond: Condition, state: SystemState) -> bool:
    """Evaluate a condition and return the result.

    Test helper that wraps the ScanContext API for simpler unit testing
    of individual conditions.

    Args:
        cond: The condition to evaluate.
        state: The system state to evaluate against.

    Returns:
        Boolean result of the condition evaluation.
    """
    ctx = ScanContext(state)
    return cond.evaluate(ctx)


def evaluate_program(program: ProgramLogic, state: SystemState, *, dt: float = 0.0) -> SystemState:
    """Evaluate a program and return the new state.

    Test helper that wraps the ScanContext API for simpler unit testing
    of complete programs.

    Args:
        program: The program to evaluate.
        state: The initial system state.
        dt: Time delta in seconds (for timer instructions).

    Returns:
        New SystemState with the program's effects applied.
    """
    ctx = ScanContext(state)
    ctx.set_memory("_dt", dt)  # For timer instructions
    program.evaluate(ctx)
    return ctx.commit(dt=dt)
