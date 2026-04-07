"""Small smoke tests for Click ladder codegen entrypoints."""

from __future__ import annotations

import csv
from pathlib import Path


def _write_simple_main_csv(path: Path) -> None:
    header = [
        "marker",
        *[chr(ord("A") + i) for i in range(26)],
        *[f"A{chr(ord('A') + i)}" for i in range(5)],
        "AF",
    ]
    rows = [
        header,
        ["R", "X001", *["-"] * 30, "out(Y001)"],
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


def test_ladder_to_pyrung_smoke(tmp_path: Path):
    from pyrung.click import ladder_to_pyrung, pyrung_to_ladder

    csv_path = tmp_path / "main.csv"
    _write_simple_main_csv(csv_path)

    code = ladder_to_pyrung(csv_path)

    ns: dict = {}
    exec(code, ns)

    bundle = pyrung_to_ladder(ns["logic"], ns["mapping"])
    assert list(bundle.main_rows)[1][-1] == "out(Y001)"
