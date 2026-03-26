from __future__ import annotations

import csv
import re
from collections.abc import Iterable
from pathlib import Path

from pyrung.click.codegen.analyzer import _analyze_rungs
from pyrung.click.codegen.constants import _HEADER_WIDTH
from pyrung.click.codegen.models import _RawRung, _SubroutineInfo
from pyrung.click.codegen.utils import _slugify

# ---------------------------------------------------------------------------
# Phase 1: Parse CSV → Raw Rungs
# ---------------------------------------------------------------------------


def _parse_rows(all_rows: Iterable[Iterable[str]]) -> list[_RawRung]:
    """Segment header+data rows (strings) into raw rungs.

    Accepts any iterable of string sequences — CSV reader output,
    ``LadderBundle.main_rows``, or ``LadderBundle.subroutine_rows`` entries.
    """
    rows_list = [list(row) for row in all_rows]

    if not rows_list:
        return []

    header = rows_list[0]
    if len(header) != _HEADER_WIDTH:
        raise ValueError(f"Expected {_HEADER_WIDTH}-column header, got {len(header)} columns.")

    rungs: list[_RawRung] = []
    pending_comments: list[str] = []
    current_rung: _RawRung | None = None

    for row in rows_list[1:]:
        while len(row) < _HEADER_WIDTH:
            row.append("")

        marker = row[0]

        if marker == "#":
            pending_comments.append(row[1] if len(row) > 1 else "")
            continue

        if marker == "R":
            if current_rung is not None:
                rungs.append(current_rung)
            current_rung = _RawRung(
                comment_lines=pending_comments,
                rows=[row],
            )
            pending_comments = []
            continue

        if current_rung is not None:
            current_rung.rows.append(row)

    if current_rung is not None:
        rungs.append(current_rung)

    return rungs


def _parse_csv(csv_path: Path) -> list[_RawRung]:
    """Read a CSV v2 file and segment into raw rungs."""
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        all_rows = list(reader)
    return _parse_rows(all_rows)


# ---------------------------------------------------------------------------
# Nickname Loading
# ---------------------------------------------------------------------------


def _load_nicknames_from_csv(csv_path: Path) -> dict[str, str]:
    """Load a {display_address: nickname} map from a Click nickname CSV."""
    import pyclickplc

    records = pyclickplc.read_csv(str(csv_path))
    result: dict[str, str] = {}
    for record in records.values():
        if record.nickname:
            result[record.display_address] = record.nickname
    return result


# ---------------------------------------------------------------------------
# Subroutine Parsing
# ---------------------------------------------------------------------------


def _find_call_names(raw_rungs: list[_RawRung]) -> dict[str, str]:
    """Scan main rungs for call("name") tokens → {slug: original_name}."""
    call_names: dict[str, str] = {}
    call_re = re.compile(r'^call\("(.*)"\)$')
    for rung in raw_rungs:
        for row in rung.rows:
            af = row[-1] if row else ""
            m = call_re.match(af)
            if m:
                name = m.group(1)
                call_names[_slugify(name)] = name
    return call_names


def _parse_subroutines(
    dir_path: Path,
    call_names: dict[str, str],
) -> list[_SubroutineInfo]:
    """Parse sub_*.csv files and match to call() names."""
    sub_paths = sorted(
        p
        for p in dir_path.iterdir()
        if p.is_file() and p.suffix.lower() == ".csv" and p.name.startswith("sub_")
    )

    subs: list[_SubroutineInfo] = []
    for sub_path in sub_paths:
        slug = sub_path.stem[4:]  # remove "sub_" prefix
        name = call_names.get(slug, slug)
        raw = _parse_csv(sub_path)
        analyzed = _analyze_rungs(raw)
        subs.append(_SubroutineInfo(name=name, analyzed=analyzed))

    return subs
