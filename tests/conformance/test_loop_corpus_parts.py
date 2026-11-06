"""Long runs over whole parts: the programs a CLICK loads, loop against stepping against ``expect``.

A part whose cases all run in the loop does here.  ``test_loop_corpus_less.py`` has the others.
"""

from __future__ import annotations

import pytest

from tests.conformance import loop_corpus, suite
from tests.conformance.loop_corpus import Subject, Tally

#: Why the loop refuses every run of a part as built, for a case or two in it.  Each case that
#: does runs alone, and ``test_loop_corpus_less.py`` runs its part without it.
PARTS_THAT_DECLINE = {
    "math": "randint",
    "control": "randint",
    "system": "randint",
    "boot": "scan-derived system tag",
}

MIN_LOOP_SHARE = 0.4

#: Parts whose share of scans in the loop is lower, and why: a coverage gap, not a wrong answer.
LOW_LOOP_SHARE = {
    "follow_extra": (
        0.25,
        "adding txt_end (a Copy to text through a DS pointer, a read through a DD pointer "
        "of 0) took the share from 58% to 30%: the loop declines 16 scans for 'iteration "
        "over an unknown value' and 2 for 'call with keyword or starred arguments'; the "
        "rungs are not isolated",
    ),
}

#: A case's scans are few: the loop's share of a part comes from the stretches around them.
IDLE = 20

_EVERY_PART = [*suite.PARTS, *suite.APART_PARTS]


def _part_as_built(part: str) -> Subject:
    """``suite.build(part)``."""
    return loop_corpus.subject_of_build(suite.build(part), f"part {part}")


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
        loop_corpus.assert_the_loop_ran(tally, LOW_LOOP_SHARE.get(part, (MIN_LOOP_SHARE,))[0])


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
