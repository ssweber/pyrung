"""Context-local binding for live tag access."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyrung.core.runner import PLCRunner

_ACTIVE_RUNNER: ContextVar[PLCRunner | None] = ContextVar("pyrung_active_runner", default=None)


def set_active_runner(runner: PLCRunner) -> Token[PLCRunner | None]:
    """Bind a runner in the current context."""
    return _ACTIVE_RUNNER.set(runner)


def reset_active_runner(token: Token[PLCRunner | None]) -> None:
    """Restore a previous active-runner binding."""
    _ACTIVE_RUNNER.reset(token)


def get_active_runner() -> PLCRunner | None:
    """Get the currently active runner in this context."""
    return _ACTIVE_RUNNER.get()
