"""Shared structures and errors for Click ladder CSV export."""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
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


# ---- Export summary ----
@dataclass(frozen=True)
class ExportSummary:
    """Summary of transformations applied during ladder export."""

    renames: tuple[tuple[str, str], ...]  # (dsl_name, csv_name) pairs
    added_next: int  # number of for-loops closed with next()
    added_return: int  # number of subroutines given a return() tail
    added_end: bool  # whether end() was appended to main

    def summary(self) -> str:
        """Render a human-readable summary string."""
        lines: list[str] = []
        if self.renames:
            parts = [f"{dsl} \u2192 {csv}" for dsl, csv in self.renames]
            lines.append(f"Renamed: {', '.join(parts)}")
        added: list[str] = []
        if self.added_next:
            s = "s" if self.added_next != 1 else ""
            added.append(f"next() closing {self.added_next} for-loop{s}")
        if self.added_return:
            s = "s" if self.added_return != 1 else ""
            added.append(f"return() on {self.added_return} subroutine{s}")
        if self.added_end:
            added.append("end() on main")
        if added:
            lines.append(f"Added:   {', '.join(added)}")
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.summary()


# ---- Export payload ----
@dataclass(frozen=True)
class LadderBundle:
    """Row-matrix CSV payload for Click ladder export."""

    main_rows: tuple[tuple[str, ...], ...]
    subroutine_rows: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...]
    export_summary: ExportSummary = field(
        default_factory=lambda: ExportSummary(
            renames=(), added_next=0, added_return=0, added_end=False
        )
    )

    def write(self, directory: str | Path) -> None:
        output_dir = Path(directory)
        output_dir.mkdir(parents=True, exist_ok=True)

        with (output_dir / "main.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerows(self.main_rows)

        if self.subroutine_rows:
            subroutine_dir = output_dir / "subroutines"
            subroutine_dir.mkdir(parents=True, exist_ok=True)

            name_counts: dict[str, int] = {}
            for subroutine_name, rows in self.subroutine_rows:
                count = name_counts.get(subroutine_name, 0)
                name_counts[subroutine_name] = count + 1
                # Keep filenames unique when multiple entries share the same name.
                stem = subroutine_name if count == 0 else f"{subroutine_name}_{count + 1}"

                with (subroutine_dir / f"{stem}.csv").open(
                    "w", encoding="utf-8", newline=""
                ) as handle:
                    writer = csv.writer(handle)
                    writer.writerows(rows)

        summary_text = self.export_summary.summary()
        if summary_text:
            logging.getLogger("pyrung.click.ladder").info(summary_text)


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


__all__ = [
    "ExportSummary",
    "Issue",
    "LadderBundle",
    "LadderExportError",
    "_ConditionRow",
    "_OutputSlot",
    "_RenderError",
]
