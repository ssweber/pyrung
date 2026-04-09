"""Automatically generated module split."""

from __future__ import annotations

import textwrap
from typing import Any

from pyrung.core._source import (
    _capture_source,
    _capture_with_call_arg_lines,
    _capture_with_end_line,
)
from pyrung.core.condition import ConditionTerm
from pyrung.core.rung import Rung as RungLogic

from ._program import Program
from ._state import _pop_rung_context, _push_rung_context


def _set_scope_end_line(
    rung: RungLogic,
    *,
    source_file: str | None,
    source_line: int | None,
    context_name: str,
) -> None:
    """Populate end-line metadata for a scope-backed rung."""
    rung.end_line = _capture_with_end_line(
        source_file,
        source_line,
        context_name=context_name,
    )


class Rung:
    """Context manager for defining a rung.

    Example:
        with Rung(Button):
            out(Light)

        with Rung(Step == 0):
            out(Light1)
            copy(1, Step, oneshot=True)
    """

    def __init__(self, *conditions: ConditionTerm) -> None:
        source_file, source_line = _capture_source(depth=2)
        self._rung = RungLogic(*conditions, source_file=source_file, source_line=source_line)
        self._pending_required_builder: tuple[int, str] | None = None
        condition_arg_lines = _capture_with_call_arg_lines(
            source_file,
            source_line,
            context_name="Rung",
        )

        # Consume any pending comment() call.
        prog = Program._current()
        if prog is not None and prog._pending_comment is not None:
            self._rung.comment = prog._pending_comment
            prog._pending_comment = None

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

    def continued(self) -> Rung:
        """Mark this rung as a continuation of the previous rung's condition snapshot.

        All conditions in this rung will evaluate against the same frozen
        state as the prior rung, rather than taking a fresh snapshot. This
        models multiple independent wires on the same visual rung in Click's
        ladder editor.

        Returns:
            self, for chaining: ``with Rung(B).continued(): ...``
        """
        if self._rung.comment is not None:
            raise RuntimeError(
                "A continued() rung cannot have its own comment. "
                "Set the comment on the original rung instead."
            )
        self._rung._use_prior_snapshot = True
        return self

    def _set_pending_required_builder(self, builder: object, descriptor: str) -> None:
        pending = self._pending_required_builder
        if pending is not None:
            _, existing = pending
            raise RuntimeError(f"{existing} must be completed before starting {descriptor}.")
        self._pending_required_builder = (id(builder), descriptor)

    def _assert_pending_required_builder_owner(self, builder: object, method_name: str) -> None:
        pending = self._pending_required_builder
        if pending is None:
            raise RuntimeError(
                f"{method_name}() called on a builder that is not pending in this flow."
            )
        pending_id, descriptor = pending
        if pending_id != id(builder):
            raise RuntimeError(
                f"{descriptor} must be completed before calling {method_name}() "
                "on a different builder."
            )

    def _clear_pending_required_builder(self, builder: object) -> None:
        self._assert_pending_required_builder_owner(builder, "finalize")
        self._pending_required_builder = None

    def _assert_no_pending_required_builder(self, next_action: str) -> None:
        pending = self._pending_required_builder
        if pending is None:
            return
        _, descriptor = pending
        raise RuntimeError(f"{descriptor} must be completed before calling {next_action}().")

    def _assert_required_builders_resolved(self, scope_name: str) -> None:
        pending = self._pending_required_builder
        if pending is None:
            return
        _, descriptor = pending
        raise RuntimeError(f"{descriptor} must be completed before exiting {scope_name} flow.")

    def __enter__(self) -> Rung:
        _push_rung_context(self)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            if exc_type is None:
                self._assert_required_builders_resolved("Rung")
        finally:
            _pop_rung_context()
        _set_scope_end_line(
            self._rung,
            source_file=self._rung.source_file,
            source_line=self._rung.source_line,
            context_name="Rung",
        )
        # Add rung to current program
        prog = Program._current()
        if prog is not None:
            prog._add_rung(self._rung)


_MAX_COMMENT_LENGTH = 1400


def comment(text: str) -> None:
    """Attach a comment to the next rung.

    The comment is consumed by the immediately following ``Rung()`` constructor.
    Must be called inside a ``Program`` context.

    Example::

        comment("UnitMode Change")
        with Rung(C_UnitModeChgRequest):
            copy(1, C_UnitModeChgRequestBool, oneshot=True)
    """
    prog = Program._current()
    if prog is None:
        raise RuntimeError("comment() must be used inside a Program context")
    if prog._pending_comment is not None:
        raise RuntimeError("comment() already set — missing a Rung after the previous comment()?")
    text = textwrap.dedent(text).strip()
    if len(text) > _MAX_COMMENT_LENGTH:
        raise ValueError(f"Rung comment is {len(text)} chars, max is {_MAX_COMMENT_LENGTH}.")
    prog._pending_comment = text


RungContext = Rung
