"""Automatically generated module split."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pyrung.core._source import _capture_source, _capture_with_call_arg_lines
from pyrung.core.condition import ConditionTerm
from pyrung.core.instruction import ForLoopInstruction
from pyrung.core.rung import Rung as RungLogic
from pyrung.core.tag import Tag, TagType

from ..validation import _check_function_body_strict
from . import _state
from ._program import Program, _validate_subroutine_name
from ._rung import Rung, _set_scope_end_line
from ._state import (
    _current_rung,
    _new_capture_context,
    _pop_rung_context,
    _push_rung_context,
    _require_rung_context,
)


class Subroutine:
    """Context manager for defining a subroutine.

    Subroutines are named blocks of rungs that are only executed when called.

    Example:
        with subroutine("my_sub"):
            with Rung():
                out(Light)
    """

    def __init__(self, name: str, *, strict: bool = True) -> None:
        self._name = _validate_subroutine_name(name)
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
        if _state._forloop_active:
            raise RuntimeError("Nested forloop is not permitted")

        self._parent_ctx = _require_rung_context("forloop")
        self._parent_ctx._assert_no_pending_required_builder("forloop")
        _state._forloop_active = True

        # Capture body instructions to a temporary rung (like Branch capture).
        self._capture_rung = RungLogic(
            source_file=self._source_file,
            source_line=self._source_line,
        )
        self._capture_ctx = _new_capture_context(self._capture_rung)
        _push_rung_context(self._capture_ctx)

        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        _pop_rung_context()
        _state._forloop_active = False

        if exc_type is None and self._capture_ctx is not None:
            self._capture_ctx._assert_required_builders_resolved("forloop")

        if self._parent_ctx is None or self._capture_rung is None:
            return

        _set_scope_end_line(
            self._capture_rung,
            source_file=self._source_file,
            source_line=self._source_line,
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
        *conditions: ConditionTerm,
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
        if _state._forloop_active:
            raise RuntimeError("branch() is not permitted inside forloop()")

        # Get parent rung context
        self._parent_ctx = _current_rung()
        if self._parent_ctx is None:
            raise RuntimeError("branch() must be called inside a Rung context")
        self._parent_ctx._assert_no_pending_required_builder("branch")

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
        self._branch_ctx = _new_capture_context(self._branch_rung)
        _push_rung_context(self._branch_ctx)

        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        # Pop our branch context
        _pop_rung_context()

        if exc_type is None and self._branch_ctx is not None:
            self._branch_ctx._assert_required_builders_resolved("branch")

        # Add the branch as a nested rung to the parent
        if self._parent_ctx is not None and self._branch_rung is not None:
            _set_scope_end_line(
                self._branch_rung,
                source_file=self._source_file,
                source_line=self._source_line,
                context_name="branch",
            )
            self._parent_ctx._rung.add_branch(self._branch_rung)


def branch(*conditions: ConditionTerm) -> Branch:
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
