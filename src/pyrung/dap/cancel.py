"""Cooperative cancellation for long-running console commands.

A console command that can run for a long time (``how``) polls a
:class:`CancelToken` from inside the progress callback it already emits, and
raises :class:`ConsoleCancelled` once a stop has been requested.  Cancellation
is therefore exactly as responsive as the command's progress reporting.

The token is *set* from outside the adapter's ``_state_lock`` — see
``LiveServer._handle``, which intercepts the ``stop`` verb before it would
otherwise queue behind the very command it is meant to interrupt.
"""

from __future__ import annotations

import threading


class ConsoleCancelled(Exception):
    """Raised inside a console handler when a stop has been requested."""


class CancelToken:
    """A resettable stop flag shared by the live server and console handlers.

    Set by the thread serving a ``stop`` request; polled by the thread running
    the command.  ``threading.Event`` supplies the memory barrier, so no
    additional locking is needed.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def requested(self) -> bool:
        """True once :meth:`request` has been called and not yet reset."""
        return self._event.is_set()

    def request(self) -> None:
        """Ask the in-flight command to stop at its next poll point."""
        self._event.set()

    def reset(self) -> None:
        """Clear the flag. Called before each command so stale stops don't leak."""
        self._event.clear()

    def check(self, what: str = "command") -> None:
        """Raise :class:`ConsoleCancelled` if a stop has been requested."""
        if self._event.is_set():
            raise ConsoleCancelled(f"{what} cancelled")
