"""The facts of each idiom in the corpus hold on every scan of its runs.

``tests/facts/check.py`` says what each fact claims and how a run checks it.  A fact a run
refutes is a soundness bug.  :data:`KNOWN` lists the ones not fixed yet.  Each has a strict
xfail, so the test fails once the bug is fixed, and the entry has to go.
"""

from __future__ import annotations

from functools import cache

import pytest

from tests.facts.check import Report, check_program
from tests.facts.corpus import CORPUS

#: Seeds of the random runs, one run each.
SEEDS = (1, 2)

#: Facts the runs refute, by idiom: ``(fact, tag)`` -> the first scan that refutes it.
KNOWN: dict[str, dict[tuple[str, str], str]] = {
    "funcdep_follows_its_source": {
        ("functional dep", "PosOffset"): (
            "scan 1 ends with PosOffset = 0 and 5 + 1 * Pos = 5.  PosOffset is elided, but"
            " the relation only holds once the rung has written it."
        ),
    },
    "funcdep_written_before_its_source_moves": {
        ("functional dep", "Lag"): (
            "scan 1 ends with Lag = 5 and 5 + 1 * Lead = 6.  The rung writes Lag before"
            " Lead moves, and the dependency check ignores that order."
        ),
    },
}


@cache
def _report(name: str) -> Report:
    return check_program(name, CORPUS[name], seeds=SEEDS)


def _found(report: Report) -> set[tuple[str, str]]:
    return {(v.fact, v.key) for v in report.violations}


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_facts_hold(name: str) -> None:
    report = _report(name)
    assert report.intractable is None
    assert report.tally.scans > 0 and report.tally.judged
    known = KNOWN.get(name, {})
    new = [str(v) for v in report.violations if (v.fact, v.key) not in known]
    assert not new, "\n".join(new)
    # The runs move every stepping tag, so they reach the values its writes produce.
    assert not report.unmoved


@pytest.mark.parametrize(
    ("name", "fact", "key"),
    [
        pytest.param(
            name,
            fact,
            key,
            marks=pytest.mark.xfail(strict=True, reason=f"{name}: {fact} of {key}, {why}"),
        )
        for name, failures in sorted(KNOWN.items())
        for (fact, key), why in sorted(failures.items())
    ],
)
def test_known_failure_is_fixed(name: str, fact: str, key: str) -> None:
    assert (fact, key) not in _found(_report(name))
