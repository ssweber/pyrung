"""Case and result types for the conformance suite."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pyrung.conformance._slot import SLOT_FIELDS

#: An expected slot value: an exact int, or a ``range`` the value must fall in.
#: Use a range for anything wall-clock dependent over the wire (timer Acc).
Expectation = int | range


@dataclass(frozen=True)
class Case:
    sentence: str
    ladder: Callable[[Any], None]
    expect: dict[str, Expectation]
    scans: int = 2
    in_process_only: str | None = None


@dataclass(frozen=True)
class CaseResult:
    case: Case
    passed: bool
    actual: dict[str, int]
    slot_index: int
    fired: bool = False
    #: Why the case failed beyond a value mismatch (for example an unstable read).
    note: str = ""
    #: Set when the runner did not run the case; holds the reason.
    skipped: str | None = None


def case(
    sentence: str,
    *,
    ladder: Callable[[Any], None],
    expect: Mapping[str, Expectation],
    scans: int = 2,
    in_process_only: str | None = None,
) -> Case:
    """Define a conformance case.

    Args:
        sentence: What should be true, in plain English.
        ladder: Builds the rungs that prove it, given the case's slot.
        expect: Slot field -> expected value (an int, or a ``range``).
        scans: Scans to run after ``Cmd`` goes up.  In-process this is an
            exact scan count; over Modbus it becomes wall-clock settle time
            at the nominal 10 ms scan.
        in_process_only: Reason the case cannot run over Modbus.  The Modbus
            runner reports it as skipped with this reason.
    """
    result_fields = set(SLOT_FIELDS) - {"Cmd"}
    bad = set(expect) - result_fields
    if bad:
        raise ValueError(f"Unknown slot fields in expect: {bad}")
    for field_name, value in expect.items():
        if isinstance(value, bool) or not isinstance(value, (int, range)):
            raise TypeError(f"expect[{field_name!r}] must be an int or a range, got {value!r}")
    if scans < 1:
        raise ValueError(f"scans must be >= 1, got {scans}")
    return Case(
        sentence=sentence,
        ladder=ladder,
        expect=dict(expect),
        scans=scans,
        in_process_only=in_process_only,
    )


def expectation_met(expect: Mapping[str, Expectation], actual: Mapping[str, int]) -> bool:
    """True when every expected field in *actual* equals or falls in its expectation."""
    for field_name, wanted in expect.items():
        got = actual.get(field_name)
        if got is None:
            return False
        if isinstance(wanted, range):
            if got not in wanted:
                return False
        elif got != wanted:
            return False
    return True


def format_expect(expect: Mapping[str, Expectation]) -> str:
    """Render an expectation dict in ASCII, showing ranges as ``lo..hi``."""
    parts = []
    for field_name, wanted in expect.items():
        if isinstance(wanted, range):
            text = f"{wanted.start}..{wanted.stop - 1}" if wanted.step == 1 else repr(wanted)
        else:
            text = str(wanted)
        parts.append(f"{field_name!r}: {text}")
    return "{" + ", ".join(parts) + "}"
