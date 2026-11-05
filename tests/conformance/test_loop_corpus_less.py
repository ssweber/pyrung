"""The parts that decline as built, without the cases that make them: then they run in the loop.

Such a case keeps its whole part out, RAND or a read of the scan counter, so here its neighbours
get their turn, all in one scan.
"""

from __future__ import annotations

import pytest

from pyrung.conformance import Case, Group
from tests.conformance import loop_corpus
from tests.conformance.loop_corpus import Tally
from tests.conformance.test_loop_corpus_parts import MIN_LOOP_SHARE, PARTS_THAT_DECLINE


def _stays(group: Group, case: Case) -> bool:
    if not loop_corpus.on_hardware(group, case):
        return False
    return loop_corpus.whole_run_decline(loop_corpus.case_alone(group, case).program) is None


@pytest.mark.parametrize("part", sorted(PARTS_THAT_DECLINE))
def test_part_less_what_declines(part: str) -> None:
    subject = loop_corpus.subject_of(
        loop_corpus.picks(part, _stays), f"part {part}, less what declines"
    )
    tally = Tally()
    with loop_corpus.fine_loop():
        tally.add(subject, lambda driver: driver.judged_passes(idle=40))
    print(f"{subject.label}: {tally.report()}")
    loop_corpus.assert_nothing_found(tally)
    loop_corpus.assert_every_run_checked(tally)
    assert not any(reason.startswith("run declined") for reason in tally.reasons)
    loop_corpus.assert_the_loop_ran(tally, MIN_LOOP_SHARE)
