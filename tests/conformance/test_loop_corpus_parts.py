"""Long runs over whole parts: the programs a CLICK loads, loop against stepping against ``expect``.

A part whose cases all run in the loop does here.  ``test_loop_corpus_less.py`` has the others.
"""

from __future__ import annotations

import pytest

from pyrung.conformance._ladder import case_label
from tests.conformance import loop_corpus, suite
from tests.conformance.loop_corpus import HOLES, Subject, Tally

#: Why the loop refuses every run of a part as built, for a case or two in it.  Each case that
#: does runs alone, and ``test_loop_corpus_less.py`` runs its part without it.
PARTS_THAT_DECLINE = {
    "math": "randint",
    "control": "randint",
    "system": "randint",
    "boot": "scan-derived system tag",
}

MIN_LOOP_SHARE = 0.4

#: A case's scans are few: the loop's share of a part comes from the stretches around them.
IDLE = 20

_EVERY_PART = [*suite.PARTS, *suite.APART_PARTS]


def _part_as_built(part: str) -> Subject:
    """``suite.build(part)``, unless a case in it shows a hole: then the same, less that case."""
    label = f"part {part}"
    groups = suite.groups(part)
    if any(case_label(g.name, c) in HOLES for g in groups for c in g.cases):
        return loop_corpus.subject_of(loop_corpus.picks(part, loop_corpus.on_hardware), label)
    return loop_corpus.subject_of_build(suite.build(part), label)


@pytest.mark.parametrize("part", _EVERY_PART)
def test_part_as_built(part: str) -> None:
    """Whatever a part declines for, it is the one stated, and what it does run equals stepping."""
    subject = _part_as_built(part)
    assert loop_corpus.whole_run_decline(subject.program) == PARTS_THAT_DECLINE.get(part)
    idle = 0 if part in PARTS_THAT_DECLINE else IDLE
    tally = Tally()
    with loop_corpus.fine_loop():
        tally.add(subject, lambda driver: driver.judged_passes(idle=idle))
    print(f"part {part} as built: {tally.report()}")
    loop_corpus.assert_nothing_found(tally)
    loop_corpus.assert_every_run_checked(tally)
    if part in PARTS_THAT_DECLINE:
        assert tally.loop == 0
    else:
        loop_corpus.assert_the_loop_ran(tally, MIN_LOOP_SHARE)


@pytest.mark.parametrize("part", ["fixed_scan", "text_flasher"])
def test_a_part_with_nothing_lowered(part: str) -> None:
    """As a user runs it: 1000 scans interpreted, then a run long enough for the loop to take."""
    tally = Tally()
    with loop_corpus.production_constants():
        tally.add(_part_as_built(part), lambda driver: driver.long_wait(2200))
    print(f"part {part} with nothing lowered: {tally.report()}")
    loop_corpus.assert_nothing_found(tally)
    loop_corpus.assert_every_run_checked(tally)
    assert tally.loop > 0
