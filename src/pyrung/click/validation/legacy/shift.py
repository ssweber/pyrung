"""pyrung's checks of shift()."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..checks import Check

if TYPE_CHECKING:
    pass


CODES: tuple[str, ...] = ()

CHECKS: tuple[Check, ...] = ()

TABLES: dict[str, dict[Any, Any]] = {"WRITE_TARGET_FIELDS": {"ShiftInstruction": ("bit_range",)}}
