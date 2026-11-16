"""The conformance cases as programs for the facts check, each case alone on its slot.

A case runs the way the harness runs it.  ``Cmd`` goes to 0 until the case reports
``Ready``, then to 1 for the case's scans and a few more.  Then ``Cmd`` toggles at random,
in the stretches ``RandomInputs`` makes, so a case also starts, stops and restarts at scans
the harness never picks.  The harness
writes ``Cmd`` from outside, so the check marks it ``external`` while the facts are built.
Every other tag a case reads is the case's own.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from pyrung.conformance._case import Case, Group
from pyrung.conformance._ladder import case_label
from pyrung.core import PLC
from pyrung.core.tag import Tag, note_metadata_edit
from tests.conformance import loop_corpus, suite
from tests.facts.check import RandomInputs, Report, Step, check_program

#: Scans a case may take to report ``Ready``: ``run()``'s 10 s.
READY_SCANS = 1000
#: Scans past a case's own, so what it does once done is checked too.
AFTER_SCANS = 10
#: Scans of the random run.
RANDOM_SCANS = 40

#: Cases left out, and why.
LEFT_OUT = {
    "for_signed/": "runs For bodies 135535 times in five scans (over 4 GB and 100 s)",
}


def cases() -> list[tuple[Group, Case]]:
    """Every case of the suite and its parts apart, but the ones :data:`LEFT_OUT` names."""
    found = []
    for group in [*suite.ALL_GROUPS, *suite.APART_GROUPS]:
        for case in group.cases:
            label = case_label(group.name, case)
            if not any(label.startswith(prefix) for prefix in LEFT_OUT):
                found.append((group, case))
    return found


@contextmanager
def _external(tag: Tag) -> Iterator[None]:
    """*tag* marked external, as a TagMap marks a tag something outside writes."""
    was = tag.external
    object.__setattr__(tag, "external", True)
    note_metadata_edit()
    try:
        yield
    finally:
        object.__setattr__(tag, "external", was)
        note_metadata_edit()


def check_case(group: Group, case: Case) -> Report:
    """Check the facts of *case*'s program, run as the module docstring says."""
    subject = loop_corpus.case_alone(group, case)
    (entry,) = subject.entries
    cmd, ready = entry.fields.Cmd, entry.fields.Ready

    def drive(plc: PLC, scan: Step, walk: RandomInputs) -> None:
        scan({cmd.name: 0})
        for _ in range(READY_SCANS):
            if plc.current_state.tags.get(ready.name, 0):
                break
            scan({})
        scan({cmd.name: 1})
        for _ in range(case.scans + AFTER_SCANS):
            scan({})
        walk.values = {cmd.name: [0, 1]}
        for _ in range(RANDOM_SCANS):
            scan(_only(walk.next(), cmd.name))

    with _external(cmd):
        return check_program(subject.label, lambda: subject.program, seeds=(1,), driver=drive)


def _only(patch: Mapping[str, Any], name: str) -> dict[str, Any]:
    return {name: patch[name]} if name in patch else {}
