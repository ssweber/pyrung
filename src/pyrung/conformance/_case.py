"""Case, group, and result types for the conformance suite."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pyrung.conformance._slot import SLOT_FIELDS
from pyrung.core.tag import Tag

#: An expected slot value: an exact int, or a ``range`` the value must fall in.
#: Use a range for anything wall-clock dependent over the wire (timer Acc).
Expectation = int | range

#: Slot fields a case may expect: the harness owns ``Cmd`` and the reset owns ``Ready``.
RESULT_FIELDS = tuple(f for f in SLOT_FIELDS if f not in ("Cmd", "Ready"))

_CASE_ID = re.compile(r"[A-Za-z0-9_]+")
_SC_ADDRESS = re.compile(r"SC[1-9][0-9]*")
_GROUP_NAME = re.compile(r"[a-z][a-z0-9]*(_[a-z0-9]+)*")

#: The fix steps a ``pending`` case can wait on, each a change that brings
#: pyrung to what a CLICK does.  The tag names the step whose landing closes
#: the gap, so its fixer finds every case it must flip.
PENDING_STEPS: Mapping[str, str] = {
    "S2": "counters count edges of their inputs",
    "S3": "edge contacts keep per-contact memory",
    "S4": "rungs solve row by row, live",
    "DRUM": "drum ordering (jump/jog/reset/pause/completion)",
    "OUT": "Out coils never set T<n>; flasher phases",
    "NOSTOP": "a Math error does not stop the PLC",
    "INF": "REAL overflow stores +/-Inf",
    "TEXT": "text, pack, NUL, search cursor and MDN fixes",
    "SYS": "SC3/SD9 phase and the fault-flag model",
}


@dataclass(frozen=True)
class Case:
    sentence: str
    ladder: Callable[[Any], None]
    expect: dict[str, Expectation]
    #: Stable key of the case's slot in the manifest.
    id: str
    scans: int = 2
    in_process_only: str | None = None
    #: Instructions run every scan while ``Cmd == 0``, after the slot is zeroed.
    init: Callable[[Any], None] | None = None
    #: Conditions that must hold, while ``Cmd == 0``, before the case reports ``Ready``.
    ready: Callable[[Any], Any] | None = None
    #: Tags the reset leaves alone on purpose; the leak check skips them.
    keep: tuple[Tag, ...] = ()
    #: System bits (``"SC46"``) the harness reads itself once the case has run.
    expect_system: dict[str, Expectation] = field(default_factory=dict)
    #: Fix step (a key of :data:`PENDING_STEPS`) pyrung waits on to meet ``expect``.
    pending: str | None = None
    #: The open hardware question the case asks; ``expect`` is the hypothesis.
    probe: str | None = None

    @property
    def checks(self) -> dict[str, Expectation]:
        """Everything the harness compares: slot fields, then system bits."""
        return {**self.expect, **self.expect_system}


@dataclass(frozen=True)
class Group:
    """A named set of cases that compiles to one CLICK subroutine.

    The subroutine is ``Conf_<Name>`` (``math`` -> ``Conf_Math``,
    ``rung_order`` -> ``Conf_RungOrder``), and slot nicknames start with
    ``<Name>``.  Case ids must be unique within the group.  The main program
    calls the subroutine *calls* times a scan (one call rung each), so a group
    can ask what a subroutine called more than once in a scan does.
    """

    name: str
    cases: tuple[Case, ...]
    calls: int

    def __init__(self, name: str, cases: Iterable[Case], *, calls: int = 1) -> None:
        if not _GROUP_NAME.fullmatch(name):
            raise ValueError(
                f"Group name {name!r} must be snake_case: lowercase letters and digits, "
                "words joined by '_', starting with a letter."
            )
        if calls < 1:
            raise ValueError(f"Group {name!r}: calls must be >= 1, got {calls}")
        cases = tuple(cases)
        seen: set[str] = set()
        for c in cases:
            if c.id in seen:
                raise ValueError(f"Group {name!r} has two cases with id {c.id!r}; pass id=.")
            seen.add(c.id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "cases", cases)
        object.__setattr__(self, "calls", calls)

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
    #: The case is ``pending`` and failed its expectation, as it must until its fix lands.
    xfail: bool = False
    #: Hash of the case's exported rows (over the wire only; ``""`` in-process).
    case_hash: str = ""


def case(
    sentence: str,
    *,
    ladder: Callable[[Any], None],
    expect: Mapping[str, Expectation],
    scans: int = 2,
    in_process_only: str | None = None,
    id: str | None = None,
    init: Callable[[Any], None] | None = None,
    ready: Callable[[Any], Any] | None = None,
    keep: Sequence[Tag] = (),
    expect_system: Mapping[str, Expectation] | None = None,
    pending: str | None = None,
    probe: str | None = None,
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
        init: Puts the case's own state back the way a fresh PLC has it, using
            the instructions' own semantics (a counter's reset input, a drum's
            reset, ``reset()`` on a latched bit, ``fill(0, ...)`` on staged
            registers).  Given the slot, it adds instructions to a rung that is
            true every scan while ``Cmd == 0``; the slot itself is already zeroed.
        ready: Given the slot, a condition (or a sequence of them) the reset
            must reach before the case reports ``Ready``, for state that takes
            more than one scan to settle (a debounce coil's off side).
        keep: Tags the reset leaves alone on purpose: an accumulator that keeps
            running while the case is idle, or a value the experiment carries
            across the reset.  The in-process leak check skips them; the case
            must still pass on every repeat.
        expect_system: CLICK system bit (``"SC11"``) -> expected value (0/1 or
            a ``range``), read by the harness after the case has run, not by
            the ladder: what a case that puts the PLC in STOP leaves behind
            (``SC11`` is off in STOP, and a stopped PLC runs no ladder).
        pending: The fix step (a key of :data:`PENDING_STEPS`) that brings
            pyrung to *expect*, which is what a CLICK does.  Until it lands,
            a run on pyrung's own targets (in-process, the local sim, the
            generated P1AM code) passes the case only if it does NOT meet
            *expect*: a strict xfail, so the case fails once pyrung starts
            matching and the fixer removes the marker.  A run against a real
            CLICK judges *expect* as usual.
        probe: The open hardware question the case asks, in a few words.
            *expect* is the answer pyrung would adopt; the case's module
            docstring says which reading means which answer, so a failure on
            a CLICK is an answer, not a bug.
    """
    bad = set(expect) - set(RESULT_FIELDS)
    if bad:
        raise ValueError(f"Unknown slot fields in expect: {bad}")
    system_expect = dict(expect_system or {})
    for address in system_expect:
        if not _SC_ADDRESS.fullmatch(address) or address not in _system_bits():
            raise ValueError(
                f"expect_system key {address!r} must be a CLICK system bit pyrung serves "
                "(SC1, SC46, ...)"
            )
    for field_name, value in [*expect.items(), *system_expect.items()]:
        if isinstance(value, bool) or not isinstance(value, (int, range)):
            raise TypeError(f"expect[{field_name!r}] must be an int or a range, got {value!r}")
    if scans < 1:
        raise ValueError(f"scans must be >= 1, got {scans}")
    if pending is not None and pending not in PENDING_STEPS:
        raise ValueError(f"pending={pending!r} must name a fix step: {', '.join(PENDING_STEPS)}")
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
        init=init,
        ready=ready,
        keep=tuple(keep),
        expect_system=system_expect,
        pending=pending,
        probe=probe,
    )


def system_tag_name(address: str) -> str:
    """The pyrung system tag served at CLICK system bit *address* (``"SC46"``)."""
    return _system_bits()[address]


def _system_bits() -> Mapping[str, str]:
    from pyrung.click.system_mappings import SYSTEM_TAG_NAMES_BY_HARDWARE

    return SYSTEM_TAG_NAMES_BY_HARDWARE


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


def judge(
    c: Case, met: Sequence[bool], clean: bool, actual: Mapping[str, int], *, pending: bool
) -> tuple[bool, bool, str]:
    """``(passed, xfail, note)`` for case *c*.

    *met* says, per run, whether the case fired and met its expectation;
    *clean* whether every reset completed and left nothing behind.  With
    *pending* on (a run on one of pyrung's own targets) a case marked
    ``pending`` passes only if no run met its expectation: one that does
    fails, so its marker comes off when its fix lands.
    """
    if not pending or c.pending is None:
        return all(met) and clean, False, ""
    step = f"pending {c.pending} ({PENDING_STEPS[c.pending]})"
    if all(met):
        return False, False, f"{step}, but pyrung now meets CLICK's expectation: remove pending="
    if any(met):
        return False, False, f"{step}, but some runs meet CLICK's expectation and some do not"
    return clean, clean, f"{step}: pyrung reads {dict(actual)}"


def format_expect(expect: Mapping[str, Expectation]) -> str:
    """Render an expectation dict in ASCII, showing ranges as ``lo..hi``."""
    parts = [f"{field_name!r}: {_value_text(wanted)}" for field_name, wanted in expect.items()]
    return "{" + ", ".join(parts) + "}"


def describe_expect(expect: Mapping[str, Expectation]) -> str:
    """Render an expectation as a sentence fragment: ``Result1 = 5, Result2 in 1..6``."""
    return ", ".join(
        f"{field_name} {'in' if isinstance(wanted, range) else '='} {_value_text(wanted)}"
        for field_name, wanted in expect.items()
    )


def _value_text(wanted: Expectation) -> str:
    if isinstance(wanted, range):
        return f"{wanted.start}..{wanted.stop - 1}" if wanted.step == 1 else repr(wanted)
    return str(wanted)
