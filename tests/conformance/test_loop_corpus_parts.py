"""Long runs over whole parts: the programs a CLICK loads, loop against stepping against ``expect``.

Apart from ``test_loop_corpus.py`` because ``make test`` keeps a file on one xdist worker, so
each file gets a worker of its own.
"""

from __future__ import annotations

import pytest

from pyrung.conformance import Case, Group
from pyrung.conformance._ladder import case_label
from tests.conformance import loop_corpus, suite
from tests.conformance.loop_corpus import HOLES, Subject, Tally

#: Why the loop refuses every run of a part as built, for a case or two in it.  Each case that
#: does is alone in ``test_loop_corpus.py``, and each part runs without them below.
PARTS_THAT_DECLINE = {
    "A": "randint",
    "B": "scan-derived system tag",
    "C": "randint",
    "E": "randint",
    "F": "randint",
    "J": "randint",
    "L": "scan-derived system tag",
    "M": "scan-derived system tag",
}

MIN_LOOP_SHARE = 0.4

_IDLE = 100

_EVERY_PART = [*suite.PARTS, *suite.APART_PARTS]


def _picks(part: str, keep: loop_corpus.CaseFilter) -> list[tuple[Group, list[Case]]]:
    picks = []
    for group in suite.groups(part):
        cases = [c for c in group.cases if keep(group, c)]
        if cases:
            picks.append((group, cases))
    return picks


def _on_hardware(group: Group, case: Case) -> bool:
    return not case.in_process_only and case_label(group.name, case) not in HOLES


def _part_as_built(part: str) -> Subject:
    """``suite.build(part)``, unless a case in it shows a hole: then the same, less that case."""
    label = f"part {part}"
    groups = suite.groups(part)
    if any(case_label(g.name, c) in HOLES for g in groups for c in g.cases):
        return loop_corpus.subject_of(_picks(part, _on_hardware), label)
    return loop_corpus.subject_of_build(suite.build(part), label)


@pytest.mark.parametrize("part", _EVERY_PART)
def test_part_as_built(part: str) -> None:
    """Whatever a part declines for, it is the one stated, and what it does run equals stepping."""
    subject = _part_as_built(part)
    assert loop_corpus.whole_run_decline(subject.program) == PARTS_THAT_DECLINE.get(part)
    # A case's scans are few: the loop's share of a part comes from the stretches around them.
    idle = 0 if part in PARTS_THAT_DECLINE else _IDLE
    tally = Tally()
    with loop_corpus.fine_loop():
        tally.add(subject, lambda driver: driver.judged_passes(idle=idle))
    print(f"part {part} as built: {tally.report()}")
    loop_corpus.assert_nothing_found(tally)
    if part in PARTS_THAT_DECLINE:
        assert tally.loop == 0
    else:
        loop_corpus.assert_the_loop_ran(tally, MIN_LOOP_SHARE, None)


@pytest.mark.parametrize("part", sorted(PARTS_THAT_DECLINE))
def test_part_less_what_declines(part: str) -> None:
    """Without those cases each part runs in the loop, with its neighbours in the same scan."""

    def stays(group: Group, case: Case) -> bool:
        alone = loop_corpus.case_alone(group, case).program
        return _on_hardware(group, case) and loop_corpus.whole_run_decline(alone) is None

    subject = loop_corpus.subject_of(_picks(part, stays), f"part {part}, less what declines")
    tally = Tally()
    with loop_corpus.fine_loop():
        tally.add(subject, lambda driver: driver.judged_passes(idle=_IDLE))
    print(f"{subject.label}: {tally.report()}")
    loop_corpus.assert_nothing_found(tally)
    assert not any(reason.startswith("run declined") for reason in tally.reasons)
    loop_corpus.assert_the_loop_ran(tally, MIN_LOOP_SHARE, None)
