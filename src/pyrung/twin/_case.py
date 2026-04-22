"""Case and result types for the twin harness."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pyrung.twin._slot import SLOT_FIELDS


@dataclass(frozen=True)
class Case:
    sentence: str
    ladder: Callable[[Any], None]
    expect: dict[str, int]
    scans: int = 2


@dataclass(frozen=True)
class CaseResult:
    case: Case
    passed: bool
    actual: dict[str, int]
    slot_index: int


def case(
    sentence: str,
    *,
    ladder: Callable[[Any], None],
    expect: dict[str, int],
    scans: int = 2,
) -> Case:
    result_fields = set(SLOT_FIELDS) - {"Cmd"}
    bad = set(expect) - result_fields
    if bad:
        raise ValueError(f"Unknown slot fields in expect: {bad}")
    return Case(sentence=sentence, ladder=ladder, expect=expect, scans=scans)
