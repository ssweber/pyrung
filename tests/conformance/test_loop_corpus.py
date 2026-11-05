"""Long runs over the suite's programs: each case alone, loop against stepping against ``expect``.

Here each case runs alone, so a neighbour that declines (a RAND, a read of the scan counter) keeps
no other case out of the loop.  ``loop_corpus.py`` says why any of this is needed.  ``make test``
keeps a file on one xdist worker, which is why the corpus is four files: this one has the first
parts, ``test_loop_corpus_later_parts.py`` the rest, and ``test_loop_corpus_parts.py`` and
``test_loop_corpus_less.py`` run parts whole.
"""

from __future__ import annotations

import os

import pytest

from pyrung.core import PLC, Int, Program, Rung, calc, randint
from tests.conformance import loop_corpus, suite
from tests.conformance.loop_corpus import FIRST_PARTS, HOLES, Tally

#: Set it to run every drill of ``loop_corpus.Driver`` on every case: a matter of many minutes,
#: too long for ``make test``, for a change to the loop or to the suite.
DEEP = "PYRUNG_LOOP_CORPUS_DEEP"


@pytest.mark.parametrize("part", FIRST_PARTS)
def test_each_case_alone(part: str) -> None:
    loop_corpus.check_each_case_alone(part)


@pytest.mark.skipif(DEEP not in os.environ, reason=f"{DEEP} is not set")
@pytest.mark.parametrize("part", [*suite.PARTS, *suite.APART_PARTS])
def test_every_drill_on_each_case(part: str) -> None:
    tally = Tally()
    for group, case in loop_corpus.cases_of(part):
        loop_corpus.every_drill(loop_corpus.case_alone(group, case), tally)
    print(f"part {part}, every drill: {tally.report()}")
    loop_corpus.assert_nothing_found(tally)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        f"hole {loop_corpus.UNCUT_DRAW!r}: a wide RAND into an Int has two draw tags, _rand.N and "
        "_rand.N.cut.  Stepping draws both (every site among the known tags), a compiled run only "
        "the one the kernel reads, so _rand.N stands still.  Nothing reads it.  Closed? Remove "
        "this xfail and loop_corpus.HOLES."
    ),
)
def test_a_long_run_of_a_wide_rand_equals_stepping(monkeypatch: pytest.MonkeyPatch) -> None:
    # A run as a user gets it: nothing lowered, 3000 scans.
    loop_corpus.not_checked_by_the_session(monkeypatch)
    r = Int("R")
    with Program(strict=False) as program:
        with Rung():
            calc(randint(70000, 70002), r)
    plc = PLC(program, dt=loop_corpus.DT)
    loop_corpus.assert_equals_stepping(plc, lambda: plc.run_for(30.0))


@pytest.mark.parametrize(
    "label",
    [
        pytest.param(
            label,
            marks=pytest.mark.xfail(strict=True, raises=AssertionError, reason=f"hole {hole!r}"),
        )
        for label, hole in sorted(HOLES.items())
    ],
)
def test_a_hole_still_shows(label: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Strict, as the xfail above: the day it closes, the case goes back in the corpus."""
    loop_corpus.not_checked_by_the_session(monkeypatch)
    group_name, _, case_id = label.partition("/")
    group = next(g for g in [*suite.ALL_GROUPS, *suite.APART_GROUPS] if g.name == group_name)
    case = next(c for c in group.cases if c.id == case_id)
    subject = loop_corpus.case_alone(group, case)
    with loop_corpus.fine_loop():
        plc = PLC(subject.program, dt=loop_corpus.DT)
        loop_corpus.assert_equals_stepping(plc, lambda: plc.run_for(1.0))
