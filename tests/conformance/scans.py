"""Scan-paced cases: a case that counts its own scans and records what it saw.

A case about how a bit moves from scan to scan cannot use the harness's clock:
over the wire nothing counts scans.  So the case counts them itself.
:func:`count_scans` adds 1 to ``Scratch`` on every scan ``Cmd != 0``, up to
``window + 1``, so ``Scratch`` is the number of the scan in progress (1 on the
first).  The rungs under test key on it (``slot.Scratch >= 3, slot.Scratch <=
5``), :func:`trace` and :func:`edges` record what happened over scans
1..``window``, and :func:`done` raises ``Fired`` on scan ``window + 1``, once
the record is final.  Nothing moves after the harness sees ``Fired``, so every
expectation is an exact int, the same in-process and on the wire.

Pass ``scans=scans_for(window)`` to ``case()``.  Every helper takes the same
``window``; the default is :data:`WINDOW`.
"""

from __future__ import annotations

from typing import Any

from pyrung.core import Rung, calc, copy

#: Scans a case records by default.
WINDOW = 8


def scans_for(window: int = WINDOW) -> int:
    """Scans to run in-process: the window, the scan that raises ``Fired``, one spare."""
    return window + 2


def count_scans(slot: Any, window: int = WINDOW) -> None:
    """``Scratch`` = the scan in progress, holding at ``window + 1``.  The case's first rung."""
    with Rung(slot.Cmd != 0, slot.Scratch <= window):
        calc(slot.Scratch + 1, slot.Scratch)


def trace(slot: Any, bit: Any, window: int = WINDOW) -> None:
    """Over the window: Result1 = scans *bit* is on, Result2 = the first, Result3 = the last.

    A count with its first and last scan pins a contiguous run exactly.
    """
    with Rung(slot.Cmd != 0, slot.Scratch <= window, bit, slot.Result1 == 0):
        copy(slot.Scratch, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch <= window, bit):
        calc(slot.Result1 + 1, slot.Result1)
        copy(slot.Scratch, slot.Result3)


def edges(edge: Any, slot: Any, count: Any, at: Any = None, window: int = WINDOW) -> None:
    """Over the window: *count* = scans *edge* passes power, *at* = the last such scan.

    The edge contact comes first on its rung, so it has power on its left on
    every scan whatever the rest of the rung says.  Whether a CLICK edge contact
    behind an open contact still tracks its bit is a probe of its own.
    """
    with Rung(edge, slot.Cmd != 0, slot.Scratch <= window):
        calc(count + 1, count)
        if at is not None:
            copy(slot.Scratch, at)


def done(slot: Any, window: int = WINDOW) -> None:
    """Raise ``Fired`` once the window is recorded.  The case's last rung."""
    with Rung(slot.Cmd != 0, slot.Scratch > window):
        copy(1, slot.Fired)
