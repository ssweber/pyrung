"""Shared structures and errors for Click ladder CSV export."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyrung.core.condition import Condition

# ---- Shared issue payload ----
Issue = dict[str, str | int | None]


# ---- Public/raised errors ----
class LadderExportError(RuntimeError):
    """Raised when strict ladder export prevalidation or lowering fails."""

    def __init__(self, issues: list[Issue] | tuple[Issue, ...]):
        def _safe_int(value: Any) -> int | None:
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        normalized: list[Issue] = []
        for issue in issues:
            normalized.append(
                {
                    "path": str(issue.get("path", "")),
                    "message": str(issue.get("message", "")),
                    "source_file": (
                        None if issue.get("source_file") is None else str(issue.get("source_file"))
                    ),
                    "source_line": _safe_int(issue.get("source_line")),
                }
            )
        self.issues: tuple[Issue, ...] = tuple(normalized)

        if self.issues:
            preview = "; ".join(f"{issue['path']}: {issue['message']}" for issue in self.issues[:3])
            if len(self.issues) > 3:
                preview += f" (+{len(self.issues) - 3} more)"
        else:
            preview = "Ladder export failed."
        super().__init__(preview)


# ---- Export payload ----
@dataclass(frozen=True)
class LadderBundle:
    """Row-matrix CSV payload for Click ladder export."""

    main_rows: tuple[tuple[str, ...], ...]
    subroutine_rows: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...]

    def write(self, directory: str | Path) -> None:
        output_dir = Path(directory)
        output_dir.mkdir(parents=True, exist_ok=True)

        with (output_dir / "main.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerows(self.main_rows)

        slug_counts: dict[str, int] = {}
        for subroutine_name, rows in self.subroutine_rows:
            base_slug = _slugify(subroutine_name)
            count = slug_counts.get(base_slug, 0)
            slug_counts[base_slug] = count + 1
            # Keep filenames unique when multiple subroutines slugify to the same value.
            slug = base_slug if count == 0 else f"{base_slug}_{count + 1}"

            with (output_dir / f"sub_{slug}.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerows(rows)


class _RenderError(RuntimeError):
    def __init__(self, issue: Issue):
        self.issue = issue
        super().__init__(f"{issue.get('path')}: {issue.get('message')}")


@dataclass
class _ConditionRow:
    cells: list[str]
    cursor: int
    accepts_terms: bool = True

    def clone(self) -> _ConditionRow:
        return _ConditionRow(
            cells=self.cells.copy(),
            cursor=self.cursor,
            accepts_terms=self.accepts_terms,
        )


@dataclass(frozen=True)
class _OutputSlot:
    output_token: str
    local_conditions: tuple[Condition, ...] = ()


# ---- Internal helpers ----
def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    return slug if slug else "subroutine"

__all__ = [
    "Issue",
    "LadderBundle",
    "LadderExportError",
    "_ConditionRow",
    "_OutputSlot",
    "_RenderError",
]
