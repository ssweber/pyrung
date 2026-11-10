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
one of the questions.  SC46 stays on until a Math clears it, so another case's
op can leave it on.  A case that reads it uses :class:`GuardedProbes`, which
waits for a scan the flag starts off.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from pyrung.conformance import Case, CaseResult, record_confirmations
from pyrung.core import Rung, copy, latch, math, on_delay, out
from tests.conformance.memory import ClickTimer, td


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
        math(into + bit, into)


#: Every range :func:`between` gave, each told from an asserted range by identity.
_CANDIDATES: list[range] = []


def between(*values: int) -> range:
    """Every candidate reading of a field, so that none is asserted."""
    candidates = range(min(values), max(values) + 1)
    _CANDIDATES.append(candidates)
    return candidates


def asks_for_a_reading(case: Case) -> bool:
    """A field of *case* came from :func:`between`: a pass answers nothing, what it read does.

    Not every range: ``system/rtc_seconds_advance`` asserts one.
    """
    return any(value is known for value in case.expect.values() for known in _CANDIDATES)


def reading_lines(results: Iterable[CaseResult]) -> list[str]:
    """A ``read`` line for each case asking for a reading: the report prints one only on a FAIL."""
    return [f"read {r.group}/{r.case.id} {r.actual}" for r in results if asks_for_a_reading(r.case)]


_READINGS_HEADER = """\
# What the probes with no answer yet read on a real CLICK, oldest first, written by
# `make test-conformance PLC=...`.  A case here is not confirmed: its range holds every
# candidate, so a pass says nothing.  hash: the case's exported rows when it read this.
"""


def record_readings(path: str | Path, results: Iterable[CaseResult], *, on: str) -> list[str]:
    """Append what each case asking for a reading read; a boot's next step adds its own.

    Stale and skipped cases read nothing.  ``PYRUNG_CONFORMANCE_STEP``, if set, labels the
    entries (``boot_read``'s procedure step).  Returns the keys written.
    """
    path = Path(path)
    step = os.environ.get("PYRUNG_CONFORMANCE_STEP")
    step_line = f"step = {json.dumps(step)}\n" if step else ""
    entries: list[str] = []
    keys: list[str] = []
    for r in results:
        if r.stale or r.skipped is not None or not r.case_hash or not asks_for_a_reading(r.case):
            continue
        key = f"{r.group}/{r.case.id}"
        read = ", ".join(f"{field} = {value}" for field, value in r.actual.items())
        entries.append(
            f'[[reading]]\ncase = "{key}"\nhash = "{r.case_hash}"\non = "{on}"\n{step_line}'
            f"read = {{ {read} }}\n"
        )
        keys.append(key)
    if entries:
        text = path.read_text(encoding="utf-8") if path.exists() else _READINGS_HEADER
        path.write_text(text + "\n" + "\n".join(entries), encoding="utf-8")
    return keys


def _toml(value: Any) -> str:
    """*value* as a TOML inline value.  It takes str, int, bool, list and dict only."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, dict):
        items = ", ".join(f"{json.dumps(str(k))} = {_toml(v)}" for k, v in value.items())
        return "{ " + items + " }" if items else "{}"
    raise TypeError(f"no TOML form for {value!r}")


def record_raw_readings(
    path: str | Path,
    readings: dict[str, dict[str, Any]],
    *,
    group: str,
    case_hash: str,
    on: str,
    header: str = _READINGS_HEADER,
) -> list[str]:
    """Append one entry per probe of a harness that reads outside the slots (``modbus_raw``).

    A sibling of :func:`record_readings` for readings that are not slot fields.  *header* starts
    a new file.  Returns the keys written.
    """
    path = Path(path)
    entries = [
        f'[[reading]]\ncase = "{group}/{name}"\nhash = "{case_hash}"\non = "{on}"\n'
        f"read = {_toml(read)}\n"
        for name, read in readings.items()
    ]
    if entries:
        text = path.read_text(encoding="utf-8") if path.exists() else header
        path.write_text(text + "\n" + "\n".join(entries), encoding="utf-8")
    return [f"{group}/{name}" for name in readings]


def record_run(
    confirmed: str | Path, readings: str | Path, results: Iterable[CaseResult], *, on: str
) -> tuple[list[str], list[str], list[str]]:
    """A real CLICK's run: what passed goes in *confirmed*, what failed comes out, and a
    case asking for a reading goes in *readings* alone.  Returns (added, dropped, read)."""
    results = list(results)
    asserted = [r for r in results if not asks_for_a_reading(r.case)]
    added, dropped = record_confirmations(confirmed, asserted, on=on)
    return added, dropped, record_readings(readings, results, on=on)


class GuardedProbes:
    """An op once, on the first scan past the stage whose start finds *flag* off.

    For a flag another case's op can leave on (SC46), which would otherwise
    read as this one's.  ``Scratch`` is the scan number (``scans.count_scans``).
    Each probe takes two C bits from *bits*.
    """

    def __init__(self, bits: Iterable[Any], *, stage_scan: int, window: int) -> None:
        self.free = iter(bits)
        self.stage_scan = stage_scan
        self.window = window

    def __call__(
        self,
        slot: Any,
        op: Callable[[], None],
        flag: Any,
        field: Any,
        bit: int = 1,
        also: Iterable[tuple[Any, int]] = (),
    ) -> None:
        """*also*: other ``(flag, bit)`` pairs to read right after the op."""
        now, taken = next(self.free), next(self.free)
        with Rung(slot.Scratch > self.stage_scan, slot.Scratch <= self.window, ~taken, ~flag):
            out(now)
            latch(taken)
        with Rung(now):
            op()
        with Rung(now, flag):
            math(field + bit, field)
        for other, other_bit in also:
            with Rung(now, other):
                math(field + other_bit, field)


def ms_clock(slot: Any, n: int, preset: int = 5000) -> Any:
    """A TON of *n* from ``Cmd``: its TD is the case's clock, in ms."""
    with Rung(slot.Cmd != 0):
        on_delay(ClickTimer(n), preset)
    return td[n]


def in_window(slot: Any, clock: Any, low: int, high: int) -> tuple[Any, ...]:
    """The conditions of a rung true from *low* up to *high* ms on *clock*, until ``Fired``."""
    return (*once(slot), clock >= low, clock < high)


def end_at(slot: Any, clock: Any, ms: int) -> None:
    """Raise ``Fired`` once *clock* reaches *ms*."""
    with Rung(*once(slot), clock >= ms):
        copy(1, slot.Fired)
