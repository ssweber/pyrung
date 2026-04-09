"""Context-local binding for live tag access."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyrung.core.runner import PLC

_ACTIVE_RUNNER: ContextVar[PLC | None] = ContextVar("pyrung_active_runner", default=None)


def set_active_runner(runner: PLC) -> Token[PLC | None]:
    """Bind a runner in the current context."""
    return _ACTIVE_RUNNER.set(runner)


def reset_active_runner(token: Token[PLC | None]) -> None:
    """Restore a previous active-runner binding."""
    _ACTIVE_RUNNER.reset(token)


def get_active_runner() -> PLC | None:
    """Get the currently active runner in this context."""
    return _ACTIVE_RUNNER.get()
