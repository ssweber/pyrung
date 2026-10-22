"""Case, group, and result types for the conformance suite."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from pyrung.conformance._slot import SLOT_FIELDS

#: An expected slot value: an exact int, or a ``range`` the value must fall in.
#: Use a range for anything wall-clock dependent over the wire (timer Acc).
Expectation = int | range

_CASE_ID = re.compile(r"[A-Za-z0-9_]+")
_GROUP_NAME = re.compile(r"[a-z][a-z0-9]*(_[a-z0-9]+)*")


@dataclass(frozen=True)
class Case:
    sentence: str
    ladder: Callable[[Any], None]
    expect: dict[str, Expectation]
    #: Stable key of the case's slot in the manifest.
    id: str
    scans: int = 2
    in_process_only: str | None = None
    #: True once the case has passed against a real CLICK.
    confirmed: bool = False


@dataclass(frozen=True)
class Group:
    """A named set of cases that compiles to one CLICK subroutine.

    The subroutine is ``Conf_<Name>`` (``math`` -> ``Conf_Math``,
    ``rung_order`` -> ``Conf_RungOrder``), and slot nicknames start with
    ``<Name>``.  Case ids must be unique within the group.
    """

    name: str
    cases: tuple[Case, ...]

    def __init__(self, name: str, cases: Iterable[Case]) -> None:
        if not _GROUP_NAME.fullmatch(name):
            raise ValueError(
                f"Group name {name!r} must be snake_case: lowercase letters and digits, "
                "words joined by '_', starting with a letter."
            )
        cases = tuple(cases)
        seen: set[str] = set()
        for c in cases:
            if c.id in seen:
                raise ValueError(f"Group {name!r} has two cases with id {c.id!r}; pass id=.")
            seen.add(c.id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "cases", cases)

    @property
    def title(self) -> str:
        """CamelCase group name, used for the subroutine and slot nicknames."""
        return "".join(part.capitalize() for part in self.name.split("_"))

    @property
    def subroutine(self) -> str:
        return f"Conf_{self.title}"


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
    #: Name of the case's group ("" for a bare case list run in-process).
    group: str = ""
    #: The group's subroutine on the PLC does not match this build; the case did not run.
    stale: bool = False


def case(
    sentence: str,
    *,
    ladder: Callable[[Any], None],
    expect: Mapping[str, Expectation],
    scans: int = 2,
    in_process_only: str | None = None,
    id: str | None = None,
    confirmed: bool = False,
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
        id: Stable key of the case's slot in the manifest.  Defaults to the
            ladder function's name without a leading ``_`` or ``prove_``.
        confirmed: True once the case has passed against a real CLICK.
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
    case_id = id if id is not None else _default_id(ladder)
    if not _CASE_ID.fullmatch(case_id):
        raise ValueError(
            f"Case id {case_id!r} must be letters, digits, and '_' only "
            f"(case {sentence!r}); pass id=."
        )
    return Case(
        sentence=sentence,
        ladder=ladder,
        expect=dict(expect),
        id=case_id,
        scans=scans,
        in_process_only=in_process_only,
        confirmed=confirmed,
    )


def _default_id(ladder: Callable[[Any], None]) -> str:
    name = getattr(ladder, "__name__", "")
    name = name.lstrip("_")
    return name.removeprefix("prove_")


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
