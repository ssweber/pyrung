"""The conformance cases as programs for the facts check, each case alone on its slot.

A case runs the way the harness runs it.  ``Cmd`` goes to 0 until the case reports
``Ready``, then to 1 for the case's scans and a few more.  Then ``RandomInputs`` toggles
``Cmd`` at random, so a case also starts, stops and restarts at scans the harness never
picks.  The harness writes ``Cmd`` from outside, so the check marks it
``external`` while the facts are built.  Every other tag a case reads is the case's own.

The check counts each tag the outside may write (``Facts.steerable``) as an input.  So it
never probes such a tag as scan-local, because how() may set it at any scan.  The idiom runs
drive each of them.  This driver writes ``Cmd`` alone.  A case's other such tags, like its
command bits, change only when its rungs write them.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import fields
from typing import Any

from pyrung.circuitpy.codegen import compile_kernel
from pyrung.conformance._case import Case, Group
from pyrung.conformance._ladder import case_label
from pyrung.core import PLC
from pyrung.core.analysis.prove import passes as prove_passes
from pyrung.core.tag import Tag, note_metadata_edit
from tests.conformance import loop_corpus, suite
from tests.facts.check import DT, RandomInputs, Report, Step, check_program

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
        walk.values, walk.aims = {cmd.name: [0, 1]}, []
        for _ in range(RANDOM_SCANS):
            scan(_only(walk.next(), cmd.name))

    with _external(cmd):
        return check_program(subject.label, lambda: subject.program, seeds=(1,), driver=drive)


def prior_text(label: str) -> str:
    """The facts how() reads for the case *label*, one per line, every set sorted."""
    (subject,) = [
        loop_corpus.case_alone(group, case)
        for group, case in cases()
        if case_label(group.name, case) == label
    ]
    with _external(subject.entries[0].fields.Cmd):
        compiled = compile_kernel(subject.program, blockless=True, proof_metadata=True)
        ctx = prove_passes._PassContext(program=subject.program, dt=DT, compiled=compiled)
        prior = prove_passes._run_prior_pipeline(ctx)
    lines = [f"{f.name} {_sorted(getattr(prior, f.name))!r}" for f in fields(prior)]
    unclassified = set(ctx._pending_infeasible_tags) | ctx._unclassified_written
    return "\n".join([*lines, f"unclassified {sorted(unclassified)!r}"])


def _sorted(value: Any) -> Any:
    """*value* with each dict and set as a list sorted by repr, so no hash order shows."""
    if isinstance(value, dict):
        return sorted(((_sorted(k), _sorted(v)) for k, v in value.items()), key=repr)
    if isinstance(value, (set, frozenset)):
        return sorted((_sorted(item) for item in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [_sorted(item) for item in value]
    return value


def _only(patch: Mapping[str, Any], name: str) -> dict[str, Any]:
    return {name: patch[name]} if name in patch else {}
