"""Pytest configuration and test helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

import pytest

pytest_plugins = ["pytester"]

from pyrung.core import CompiledPLC, PLC, Program, SystemState
from pyrung.core.condition import Condition
from pyrung.core.context import ScanContext
from pyrung.core.instruction import Instruction
from pyrung.core.program import Program as ProgramLogic
from pyrung.core.rung import Rung


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("pyrung-test", "pyrung test runner selection")
    group.addoption(
        "--runner-backend",
        action="store",
        default="classic",
        choices=("classic", "compiled", "both"),
        help=(
            "Backend used by tests that opt into runner_factory: "
            "'classic' uses PLC, 'compiled' uses CompiledPLC, "
            "'both' runs both and asserts state parity."
        ),
    )


def _assert_states_match(left: PLC | CompiledPLC, right: PLC | CompiledPLC) -> None:
    left_state = left.current_state
    right_state = right.current_state
    assert left_state.scan_id == right_state.scan_id
    assert left_state.timestamp == pytest.approx(right_state.timestamp)
    assert dict(left_state.tags) == dict(right_state.tags)
    assert dict(left_state.memory) == dict(right_state.memory)


class _RunnerPair:
    """Run both backends in lockstep and expose a PLC-like test surface."""

    def __init__(self, classic: PLC, compiled: CompiledPLC) -> None:
        self._classic = classic
        self._compiled = compiled
        _assert_states_match(self._classic, self._compiled)

    @property
    def current_state(self) -> SystemState:
        _assert_states_match(self._classic, self._compiled)
        return self._classic.current_state

    @property
    def simulation_time(self) -> float:
        _assert_states_match(self._classic, self._compiled)
        return self._classic.simulation_time

    @property
    def forces(self):  # noqa: ANN201
        assert dict(self._classic.forces) == dict(self._compiled.forces)
        return self._classic.forces

    def patch(self, updates: dict[str, Any]) -> None:
        self._classic.patch(updates)
        self._compiled.patch(updates)

    def force(self, tag: str | Any, value: bool | int | float | str) -> None:
        self._classic.force(tag, value)
        self._compiled.force(tag, value)
        assert dict(self._classic.forces) == dict(self._compiled.forces)

    def unforce(self, tag: str | Any) -> None:
        self._classic.unforce(tag)
        self._compiled.unforce(tag)
        assert dict(self._classic.forces) == dict(self._compiled.forces)

    def clear_forces(self) -> None:
        self._classic.clear_forces()
        self._compiled.clear_forces()
        assert dict(self._classic.forces) == dict(self._compiled.forces) == {}

    @contextmanager
    def forced(self, overrides: dict[str, Any] | dict[Any, Any]) -> Iterator[_RunnerPair]:
        with ExitStack() as stack:
            stack.enter_context(self._classic.forced(overrides))
            stack.enter_context(self._compiled.forced(overrides))
            assert dict(self._classic.forces) == dict(self._compiled.forces)
            yield self
        assert dict(self._classic.forces) == dict(self._compiled.forces)

    def set_rtc(self, value) -> None:  # noqa: ANN001
        self._classic.set_rtc(value)
        self._compiled.set_rtc(value)

    def step(self) -> SystemState:
        self._classic.step()
        self._compiled.step()
        _assert_states_match(self._classic, self._compiled)
        return self._classic.current_state

    def run(self, cycles: int) -> SystemState:
        self._classic.run(cycles)
        self._compiled.run(cycles)
        _assert_states_match(self._classic, self._compiled)
        return self._classic.current_state

    def run_for(self, seconds: float) -> SystemState:
        self._classic.run_for(seconds)
        self._compiled.run_for(seconds)
        _assert_states_match(self._classic, self._compiled)
        return self._classic.current_state

    def stop(self) -> None:
        self._classic.stop()
        self._compiled.stop()
        _assert_states_match(self._classic, self._compiled)


@pytest.fixture
def runner_backend(request: pytest.FixtureRequest) -> str:
    return str(request.config.getoption("runner_backend"))


@pytest.fixture
def runner_factory(runner_backend: str):
    """Build a backend-selected runner for fixed-step Program tests."""

    def _build(*args: Any, **kwargs: Any) -> PLC | CompiledPLC | _RunnerPair:
        if len(args) > 1:
            pytest.skip("runner_factory only supports a single positional logic argument")

        logic = args[0] if args else kwargs.pop("logic", None)
        if logic is None:
            pytest.skip("runner_factory requires a Program when using compiled replay backends")
        if not isinstance(logic, Program):
            pytest.skip("runner_factory compiled backends currently support Program inputs only")

        unsupported = sorted(set(kwargs) - {"dt", "initial_state", "compiled"})
        if unsupported:
            joined = ", ".join(unsupported)
            pytest.skip(f"runner_factory compiled backends do not support kwargs: {joined}")

        if runner_backend == "classic":
            return PLC(logic, **kwargs)
        if runner_backend == "compiled":
            return CompiledPLC(logic, **kwargs)

        classic = PLC(logic, **kwargs)
        compiled = CompiledPLC(logic, **kwargs)
        return _RunnerPair(classic, compiled)

    return _build


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
    program._evaluate(ctx)
    return ctx.commit(dt=dt)
