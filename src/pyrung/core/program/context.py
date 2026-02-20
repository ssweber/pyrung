from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, ClassVar

from pyrung.core._source import (
    _capture_source,
    _capture_with_call_arg_lines,
    _capture_with_end_line,
)
from pyrung.core.condition import (
    Condition,
)
from pyrung.core.instruction import (
    ForLoopInstruction,
    SubroutineReturnSignal,
)
from pyrung.core.rung import Rung as RungLogic
from pyrung.core.tag import Tag, TagType

from .validation import _check_function_body_strict, _check_with_body_from_frame

_rung_stack: list[Rung] = []


_forloop_active = False


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
    _dialect_validators: ClassVar[dict[str, DialectValidator]] = {}

    def __init__(self, *, strict: bool = True) -> None:
        self._strict = strict
        self.rungs: list[RungLogic] = []
        self.subroutines: dict[str, list[RungLogic]] = {}
        self._current_subroutine: str | None = None  # Track if we're in a subroutine

    def __enter__(self) -> Program:
        if self._strict:
            frame = inspect.currentframe()
            try:
                caller = frame.f_back if frame is not None else None
                if caller is not None:
                    _check_with_body_from_frame(caller, opt_out_hint="Program(strict=False)")
            finally:
                del frame
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
        try:
            for rung in self.subroutines[name]:
                rung.evaluate(ctx)
        except SubroutineReturnSignal:
            return

    @classmethod
    def current(cls) -> Program | None:
        """Get the current program context (if any)."""
        return cls._current

    @classmethod
    def register_dialect(cls, name: str, validator: DialectValidator) -> None:
        """Register a portability validator callback for a dialect name."""
        existing = cls._dialect_validators.get(name)
        if existing is None:
            cls._dialect_validators[name] = validator
            return
        if existing is validator:
            return
        raise ValueError(f"Dialect {name!r} already registered to a different validator")

    @classmethod
    def registered_dialects(cls) -> tuple[str, ...]:
        """Return registered dialect names in deterministic order."""
        return tuple(sorted(cls._dialect_validators))

    def validate(self, dialect: str, *, mode: str = "warn", **kwargs: Any) -> Any:
        """Run dialect-specific portability validation for this Program."""
        validator = self._dialect_validators.get(dialect)
        if validator is None:
            available = ", ".join(self.registered_dialects()) or "<none>"
            raise KeyError(
                f"Unknown validation dialect {dialect!r}. "
                f"Available dialects: {available}. "
                f"Import the dialect package first (example: import pyrung.{dialect})."
            )
        return validator(self, mode=mode, **kwargs)

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
        source_file, source_line = _capture_source(depth=2)
        self._rung = RungLogic(*conditions, source_file=source_file, source_line=source_line)
        condition_arg_lines = _capture_with_call_arg_lines(
            source_file,
            source_line,
            context_name="Rung",
        )

        # Direct Tag conditions are converted internally and would otherwise
        # have no source metadata.
        for idx, condition in enumerate(self._rung._conditions):
            if condition.source_file is None:
                condition.source_file = source_file
            if condition.source_line is None:
                if idx < len(condition_arg_lines):
                    condition.source_line = condition_arg_lines[idx]
                else:
                    condition.source_line = source_line

    def __enter__(self) -> Rung:
        _rung_stack.append(self)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        _rung_stack.pop()
        self._rung.end_line = _capture_with_end_line(
            self._rung.source_file,
            self._rung.source_line,
            context_name="Rung",
        )
        # Add rung to current program
        prog = Program.current()
        if prog is not None:
            prog.add_rung(self._rung)


class Subroutine:
    """Context manager for defining a subroutine.

    Subroutines are named blocks of rungs that are only executed when called.

    Example:
        with subroutine("my_sub"):
            with Rung():
                out(Light)
    """

    def __init__(self, name: str, *, strict: bool = True) -> None:
        self._name = name
        self._strict = strict

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
        if self._strict:
            _check_function_body_strict(
                fn,
                opt_out_hint=f'@subroutine("{self._name}", strict=False)',
                source_label=f'@subroutine("{self._name}") {getattr(fn, "__qualname__", repr(fn))}',
            )
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


def subroutine(name: str, *, strict: bool = True) -> Subroutine:
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
    return Subroutine(name, strict=strict)


class ForLoop:
    """Context manager for a repeated instruction block within a rung."""

    def __init__(
        self,
        count: Tag | int,
        oneshot: bool = False,
        source_file: str | None = None,
        source_line: int | None = None,
    ) -> None:
        self.count = count
        self.oneshot = oneshot
        self.idx = Tag("_forloop_idx", TagType.DINT)
        self._parent_ctx: Rung | None = None
        self._capture_rung: RungLogic | None = None
        self._capture_ctx: Rung | None = None
        self._source_file = source_file
        self._source_line = source_line

    def __enter__(self) -> ForLoop:
        global _forloop_active

        if _forloop_active:
            raise RuntimeError("Nested forloop is not permitted")

        self._parent_ctx = _require_rung_context("forloop")
        _forloop_active = True

        # Capture body instructions to a temporary rung (like Branch capture).
        self._capture_rung = RungLogic(
            source_file=self._source_file,
            source_line=self._source_line,
        )
        self._capture_ctx = Rung.__new__(Rung)
        self._capture_ctx._rung = self._capture_rung
        _rung_stack.append(self._capture_ctx)

        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        global _forloop_active

        _rung_stack.pop()
        _forloop_active = False

        if self._parent_ctx is None or self._capture_rung is None:
            return

        self._capture_rung.end_line = _capture_with_end_line(
            self._source_file,
            self._source_line,
            context_name="forloop",
        )

        instruction = ForLoopInstruction(
            count=self.count,
            idx_tag=self.idx,
            instructions=self._capture_rung._instructions,
            oneshot=self.oneshot,
        )
        instruction.source_file, instruction.source_line = self._source_file, self._source_line
        self._parent_ctx._rung.add_instruction(instruction)


def forloop(count: Tag | int, oneshot: bool = False) -> ForLoop:
    """Create a repeated instruction block context.

    Example:
        with Rung(Enable):
            with forloop(10) as loop:
                copy(Source[loop.idx + 1], Dest[loop.idx + 1])
    """
    source_file, source_line = _capture_source(depth=2)
    return ForLoop(count, oneshot=oneshot, source_file=source_file, source_line=source_line)


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

    def __init__(
        self,
        *conditions: Condition | Tag,
        source_file: str | None = None,
        source_line: int | None = None,
    ) -> None:
        """Create a branch with additional conditions.

        Args:
            conditions: Conditions that must be true (in addition to parent rung)
                        for this branch's instructions to execute.
        """
        self._conditions = list(conditions)
        self._branch_rung: RungLogic | None = None
        self._parent_ctx: Rung | None = None
        self._branch_ctx: Rung | None = None
        self._source_file = source_file
        self._source_line = source_line

    def __enter__(self) -> Branch:
        if _forloop_active:
            raise RuntimeError("branch() is not permitted inside forloop()")

        # Get parent rung context
        self._parent_ctx = _current_rung()
        if self._parent_ctx is None:
            raise RuntimeError("branch() must be called inside a Rung context")

        # Create a nested rung for the branch that includes BOTH parent and branch conditions
        # This ensures terminal instructions (counters, timers) see the full condition chain
        parent_conditions = self._parent_ctx._rung._conditions
        condition_arg_lines = _capture_with_call_arg_lines(
            self._source_file,
            self._source_line,
            context_name="branch",
        )
        combined_conditions = parent_conditions + self._conditions
        self._branch_rung = RungLogic(
            *combined_conditions,
            source_file=self._source_file,
            source_line=self._source_line,
        )
        self._branch_rung._branch_condition_start = len(parent_conditions)

        local_conditions = self._branch_rung._conditions[
            self._branch_rung._branch_condition_start :
        ]
        for idx, condition in enumerate(local_conditions):
            if condition.source_line is None:
                if idx < len(condition_arg_lines):
                    condition.source_line = condition_arg_lines[idx]
                else:
                    condition.source_line = self._source_line

        for condition in self._branch_rung._conditions:
            if condition.source_file is None:
                condition.source_file = self._source_file
            if condition.source_line is None:
                condition.source_line = self._source_line

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
            self._branch_rung.end_line = _capture_with_end_line(
                self._source_file,
                self._source_line,
                context_name="branch",
            )
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
    source_file, source_line = _capture_source(depth=2)
    return Branch(*conditions, source_file=source_file, source_line=source_line)


RungContext = Rung
