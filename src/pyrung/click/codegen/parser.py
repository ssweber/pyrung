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
    """Read a laddercodec CSV file and segment into raw rungs."""
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
    """Parse ``subroutines/{slug}.csv`` files and match them to call() names."""
    subroutine_dir = dir_path / "subroutines"
    if not subroutine_dir.is_dir():
        raise ValueError(
            f"subroutines directory not found in {dir_path}; expected {subroutine_dir}"
        )

    sub_entries = sorted(
        (p, p.stem) for p in subroutine_dir.iterdir() if p.is_file() and p.suffix.lower() == ".csv"
    )
    # call_names keys are slugified; file stems may preserve original
    # casing (e.g. sub_fillFilling.csv).  Slugify file stems too so both
    # sides match consistently.
    stem_slug_to_path: dict[str, tuple[Path, str]] = {
        _slugify(slug): (p, slug) for p, slug in sub_entries
    }
    missing = sorted(slug for slug in call_names if slug not in stem_slug_to_path)
    if missing:
        expected = ", ".join(f"{slug}.csv" for slug in missing)
        raise ValueError(f"Missing subroutine CSV file(s) in {subroutine_dir}: {expected}")

    subs: list[_SubroutineInfo] = []
    for sub_path, stem in sub_entries:
        name = call_names.get(_slugify(stem), stem)
        raw = _parse_csv(sub_path)
        analyzed = _analyze_rungs(raw)
        subs.append(_SubroutineInfo(name=name, analyzed=analyzed))

    return subs
