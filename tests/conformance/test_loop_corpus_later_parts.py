"""``test_loop_corpus.py``'s check on the later parts, on an xdist worker of its own."""

from __future__ import annotations

import pytest

from tests.conformance import loop_corpus
from tests.conformance.loop_corpus import FIRST_PARTS


@pytest.mark.parametrize(
    "part", [part for part in loop_corpus.CORPUS_PARTS if part not in FIRST_PARTS]
)
def test_each_case_alone(part: str) -> None:
    loop_corpus.check_each_case_alone(part)
