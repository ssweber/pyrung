"""Conformance suite -- plain-English cases observed only through slot registers."""

from pyrung.conformance._case import Case, CaseResult, case
from pyrung.conformance._runner import assert_all_passed, run

__all__ = [
    "Case",
    "CaseResult",
    "assert_all_passed",
    "case",
    "run",
]
