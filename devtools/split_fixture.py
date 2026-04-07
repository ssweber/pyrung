#!/usr/bin/env python3
"""Split a multi-rung CSV fixture into one file per rung.

Usage:
    python devtools/split_fixture.py tests/fixtures/user_shapes/multi.csv name1 name2 name3

Splits each rung (with its preceding comment rows) into a separate CSV,
re-adding the header row. Output files land in the same directory as the
input.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path


def main() -> None:
    src = Path(sys.argv[1])
    names = sys.argv[2:]

    with src.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))

    header = rows[0]

    # Segment into rungs: each rung starts at an "R" marker, with
    # preceding "#" comment rows attached.
    rungs: list[list[list[str]]] = []
    pending_comments: list[list[str]] = []

    for row in rows[1:]:
        marker = row[0] if row else ""
        if marker == "#":
            pending_comments.append(row)
        elif marker == "R":
            rungs.append([*pending_comments, row])
            pending_comments = []
        else:
            if rungs:
                rungs[-1].append(row)

    if len(names) != len(rungs):
        print(f"Error: {len(rungs)} rungs but {len(names)} names given", file=sys.stderr)
        sys.exit(1)

    out_dir = src.parent
    for name, rung_rows in zip(names, rungs):
        out_path = out_dir / f"{name}.csv"
        with out_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rung_rows)
        print(f"  {out_path}")

    # Remove source file
    src.unlink()
    print(f"  Removed {src}")


if __name__ == "__main__":
    main()
