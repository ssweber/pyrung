"""What the grey-area probes share: a one-scan gate, reading a value as a class.

The probes of ``test_word_store.py``, ``test_real_bits.py`` and so on ask what a
CLICK does where pyrung only assumes.  A case runs its op once, on the scan its
``Cmd`` arrives, and reads what it needs on the rungs right below it, before
``Fired`` goes up.  SC40, SC43 and SC44 follow the last Copy or Math (recorded,
``system``), so a flag is read on the rung right after the op it asks about and
nothing from another case can show.

Most values read back as a class, 1 for the first reading the case names, 2 for
the second and so on, 0 for anything else: a Word or a Real does not fit a slot's
Int whatever a Copy does with it, and how a Word comes out of a Copy is itself
one of the questions.  SC46 stays on to the end of the scan, so a case that
reads it uses :class:`GuardedProbes`, which waits for a scan the flag starts off.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

from pyrung.core import Rung, calc, copy, latch, out


def once(slot: Any) -> tuple[Any, Any]:
    """The contacts of a rung that runs on one scan: ``Cmd`` is in, ``Fired`` not yet."""
    return (slot.Cmd != 0, slot.Fired == 0)


def finish(slot: Any) -> None:
    """Raise ``Fired``: the case's last rung."""
    with Rung(*once(slot)):
        copy(1, slot.Fired)


def class_of(slot: Any, value: Any, readings: Sequence[Any], into: Any) -> None:
    """``into`` = 1 when *value* equals ``readings[0]``, 2 for the next, 0 for none."""
    for number, reading in enumerate(readings, start=1):
        with Rung(*once(slot), value == reading):
            copy(number, into)


def flag_bit(slot: Any, flag: Any, into: Any, bit: int = 1) -> None:
    """Add *bit* to ``into`` when *flag* is on: the rung right after the op it asks about."""
    with Rung(*once(slot), flag):
        calc(into + bit, into)


class GuardedProbes:
    """An op once, on the first scan past the stage whose start finds *flag* off.

    For a flag that stays on to the end of the scan (SC46), where another case's
    op on the same scan would otherwise read as this one's.  ``Scratch`` is the
    scan number (``scans.count_scans``).  Each probe takes two C bits from *bits*.
    """

    def __init__(self, bits: Iterable[Any], *, stage_scan: int, window: int) -> None:
        self.free = iter(bits)
        self.stage_scan = stage_scan
        self.window = window

    def __call__(
        self, slot: Any, op: Callable[[], None], flag: Any, field: Any, bit: int = 1
    ) -> None:
        now, taken = next(self.free), next(self.free)
        with Rung(slot.Scratch > self.stage_scan, slot.Scratch <= self.window, ~taken, ~flag):
            out(now)
            latch(taken)
        with Rung(now):
            op()
        with Rung(now, flag):
            calc(field + bit, field)
