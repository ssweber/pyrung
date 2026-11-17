"""Test long runs over whole parts, the programs a CLICK loads: loop, stepping and ``expect``.

A part whose cases all run in the loop is tested here. ``test_loop_corpus_less.py`` has the others.
"""

from __future__ import annotations

import pytest

from tests.conformance import loop_corpus, suite
from tests.conformance.loop_corpus import Subject, Tally

#: Why the loop refuses every run of a part as built, because of a case or two in it. Each such
#: case runs alone, and ``test_loop_corpus_less.py`` runs its part without it.
PARTS_THAT_DECLINE = {
    "math": "randint",
    "control": "randint",
    "system": "randint",
    "boot": "scan-derived system tag",
}

MIN_LOOP_SHARE = 0.4

#: Parts whose share of scans in the loop is lower, and why. This is a coverage gap, not a
#: wrong answer.
LOW_LOOP_SHARE: dict[str, tuple[float, str]] = {}

#: A case has few scans of its own. The loop's share of a part comes from the stretches around them.
IDLE = 20

_EVERY_PART = loop_corpus.CORPUS_PARTS


def _part_as_built(part: str) -> Subject:
    """Return a subject for the program that ``suite.build(part)`` builds."""
    return loop_corpus.subject_of_build(suite.build(part), f"part {part}")


@pytest.mark.parametrize("part", _EVERY_PART)
def test_part_as_built(part: str) -> None:
    """A part declines only for the stated reason, and what it does run equals stepping."""
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
    """Run the part as a user does: 1000 scans interpreted, then a run long enough for the loop."""
    tally = Tally()
    with loop_corpus.production_constants():
        tally.add(_part_as_built(part), lambda driver: driver.long_wait(2200))
    print(f"part {part} with nothing lowered: {tally.report()}")
    loop_corpus.assert_nothing_found(tally)
    loop_corpus.assert_every_run_checked(tally)
    assert tally.loop > 0
