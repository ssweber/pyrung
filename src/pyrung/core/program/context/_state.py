"""Automatically generated module split."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from pyrung.core.rung import Rung as RungLogic

if TYPE_CHECKING:
    from ._control_flow import ForLoop
    from ._program import Program
    from ._rung import Rung


class _Building(threading.local):
    """The open scopes of what this thread is building, innermost last.

    Each thread has its own, so two threads can build programs at once.
    """

    def __init__(self) -> None:
        self.programs: list[Program] = []
        self.rungs: list[Rung] = []
        # The length of the forloop() stack is the nesting depth.
        self.forloops: list[ForLoop] = []


_building = _Building()

_MAX_FORLOOP_DEPTH = 3


def _current_rung() -> Rung | None:
    """Get the current rung context (if any)."""
    rungs = _building.rungs
    return rungs[-1] if rungs else None


def _require_rung_context(func_name: str) -> Rung:
    """Get current rung or raise error."""
    rung = _current_rung()
    if rung is None:
        raise RuntimeError(f"{func_name}() must be called inside a Rung context")
    return rung


def _current_forloop_body() -> ForLoop | None:
    """The forloop whose body is the current capture scope, if any.

    Inside ``with forloop(...)`` the current rung context is the loop's
    capture context until a body ``Rung`` (or a nested loop) opens.
    """
    if not _building.forloops:
        return None
    loop = _building.forloops[-1]
    current = _current_rung()
    if current is not None and current is loop._capture_ctx:
        return loop
    return None


def _push_rung_context(ctx: Rung) -> None:
    """Push a rung context onto the active stack."""
    _building.rungs.append(ctx)


def _pop_rung_context() -> None:
    """Pop the current rung context from the active stack."""
    _building.rungs.pop()


def _new_capture_context(rung: RungLogic) -> Rung:
    """Create a lightweight Rung wrapper for temporary capture scopes."""
    from ._rung import Rung

    ctx = Rung.__new__(Rung)
    ctx._rung = rung
    ctx._pending_required_builder = None
    return ctx
