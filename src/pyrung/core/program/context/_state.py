"""Automatically generated module split."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyrung.core.rung import Rung as RungLogic

if TYPE_CHECKING:
    from ._rung import Rung

_rung_stack: list[Rung] = []

_forloop_active: bool = False


def _current_rung() -> Rung | None:
    """Get the current rung context (if any)."""
    return _rung_stack[-1] if _rung_stack else None


def _require_rung_context(func_name: str) -> Rung:
    """Get current rung or raise error."""
    rung = _current_rung()
    if rung is None:
        raise RuntimeError(f"{func_name}() must be called inside a Rung context")
    return rung


def _push_rung_context(ctx: Rung) -> None:
    """Push a rung context onto the active stack."""
    _rung_stack.append(ctx)


def _pop_rung_context() -> None:
    """Pop the current rung context from the active stack."""
    _rung_stack.pop()


def _new_capture_context(rung: RungLogic) -> Rung:
    """Create a lightweight Rung wrapper for temporary capture scopes."""
    from ._rung import Rung

    ctx = Rung.__new__(Rung)
    ctx._rung = rung
    ctx._pending_required_builder = None
    return ctx
