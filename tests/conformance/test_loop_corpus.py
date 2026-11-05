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

from pyrung.core import PLC, Int, Program, Rung, calc, copy, randint
from tests.conformance import loop_corpus, suite
from tests.conformance.loop_corpus import FIRST_PARTS, HOLES, OnlyUncutDrawsDiffer, Tally

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
    raises=OnlyUncutDrawsDiffer,
    reason=(
        f"hole {loop_corpus.UNCUT_DRAW!r}: a wide RAND into an Int has two draw tags, _rand.N and "
        "_rand.N.cut.  Stepping draws both (every site among the known tags), a compiled run only "
        "the one the kernel reads, so _rand.N stands still.  Nothing reads it.  Closed? Remove "
        "this xfail and loop_corpus.HOLES."
    ),
)
def test_a_long_run_of_a_wide_rand_equals_stepping(monkeypatch: pytest.MonkeyPatch) -> None:
    # A run as a user gets it: nothing lowered, 3000 scans.  The bounds leave a draw 20001 values,
    # so the tag that stands still does not land on the stepped value by chance.
    loop_corpus.not_checked_by_the_session(monkeypatch)
    r = Int("R")
    with Program(strict=False) as program:
        with Rung():
            calc(randint(70000, 90000), r)
    plc = PLC(program, dt=loop_corpus.DT)
    loop_corpus.assert_equals_stepping(plc, lambda: plc.run_for(30.0))


@pytest.mark.parametrize(
    "label",
    [
        pytest.param(
            label,
            marks=pytest.mark.xfail(
                strict=True, raises=OnlyUncutDrawsDiffer, reason=f"hole {hole!r}"
            ),
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
        # faults/rand_unequal_bounds draws over 3 values: 30 landings, not one.
        loop_corpus.assert_equals_stepping(plc, lambda: plc.run_for(0.1), times=30)


def _plc_with_a_draw() -> tuple[PLC, dict[str, str]]:
    """A wide RAND beside a plain tag, and the name of each tag a test may set apart from a twin."""
    r = Int("R")
    other = Int("Other")
    draw = randint(70000, 90000)
    with Program(strict=False) as program:
        with Rung():
            calc(draw, r)
        with Rung(other == 5):
            copy(1, Int("OtherSeen"))
    plc = PLC(program, dt=loop_corpus.DT)
    return plc, {"uncut": draw.tag.name, "cut": draw.cut_tag.name, "other": other.name}


def _landings(plc: PLC, names: dict[str, str], *patches: tuple[str, ...]) -> None:
    """One scan per entry of *patches*, each with those tags set on *plc* alone.

    To 1, outside every draw's bounds, so it cannot land on what the twin drew.
    """
    left = list(patches)

    def run() -> None:
        plc.patch({names[which]: 1 for which in left.pop(0)})
        plc.step()

    loop_corpus.assert_equals_stepping(plc, run, times=len(patches))


@pytest.mark.filterwarnings("ignore:BoundsViolation")
def test_a_lone_uncut_draw_is_the_hole(monkeypatch: pytest.MonkeyPatch) -> None:
    loop_corpus.not_checked_by_the_session(monkeypatch)
    plc, names = _plc_with_a_draw()
    with pytest.raises(OnlyUncutDrawsDiffer):
        _landings(plc, names, ("uncut",))


@pytest.mark.filterwarnings("ignore:BoundsViolation")
@pytest.mark.parametrize(
    "patches",
    [
        [("other",)],
        [("cut",)],
        [("uncut", "other")],
        [("uncut",), ("other",)],
    ],
    ids=["other tag", "cut draw", "beside an uncut draw", "at a later landing"],
)
def test_any_other_difference_stays_a_plain_failure(
    patches: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Else a regression in a program's own tags would xfail quietly as the hole."""
    loop_corpus.not_checked_by_the_session(monkeypatch)
    plc, names = _plc_with_a_draw()
    with pytest.raises(AssertionError) as caught:
        _landings(plc, names, *patches)
    assert not isinstance(caught.value, OnlyUncutDrawsDiffer)
