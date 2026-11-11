"""Tests for ckp_check.py's legacy_encodings.json handling, on a fake CLICK temp dir.

Run: timeout 120 uv run pytest devtools/test_ckp_check.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import ckp_check  # noqa: E402

MAIN = "R1,copy(7,DS1)\n"
SUB = "R1,out(Y1)\n"


@pytest.fixture
def project(tmp_path, monkeypatch):
    exports = tmp_path / "exports"
    (exports / "part" / "subroutines").mkdir(parents=True)
    (exports / "part.export.txt").write_text("layout 7 in DS1", encoding="utf-8")
    for root in (exports / "part", tmp_path / "click" / "pyrung_project" / "csv"):
        (root / "subroutines").mkdir(parents=True, exist_ok=True)
        (root / "main.csv").write_text(MAIN, encoding="utf-8")
        (root / "nicknames.csv").write_text("X1,a\n", encoding="utf-8")
        (root / "subroutines" / "Conf_Faults.csv").write_text(SUB, encoding="utf-8")
    monkeypatch.setattr(ckp_check, "EXPORTS", exports)
    return tmp_path / "click", tmp_path / "click" / "pyrung_project" / "csv"


def run(click, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ckp_check", "part", "--dir", str(click)])
    code = ckp_check.main()
    return code, capsys.readouterr().out


def test_no_sidecar_is_clean(project, monkeypatch, capsys):
    code, out = run(project[0], monkeypatch, capsys)
    assert code == 0
    assert "STALE" not in out


def test_finding_marks_a_matching_file_stale(project, monkeypatch, capsys):
    finding = {
        "file": "subroutines/Conf_Faults.csv",
        "program": "Conf_Faults",
        "rung": 12,
        "instruction": "Math",
        "token": "H70000",
        "detail": "it is stored as 16-bit and CLICK runs it as 4464.",
    }
    (project[1] / "legacy_encodings.json").write_text(json.dumps([finding]), encoding="utf-8")
    code, out = run(project[0], monkeypatch, capsys)
    assert code == 1
    assert "  same      subroutines/Conf_Faults.csv" in out
    assert (
        "  STALE     subroutines/Conf_Faults.csv  rung 12: Math H70000, "
        "it is stored as 16-bit and CLICK runs it as 4464. Paste it again."
    ) in out
    assert "1 problem(s)" in out


def test_finding_without_rung_names_the_whole_file(project, monkeypatch, capsys):
    finding = {
        "file": "main.csv",
        "rung": None,
        "instruction": "Copy",
        "token": "X",
        "detail": "d.",
    }
    (project[1] / "legacy_encodings.json").write_text(json.dumps([finding]), encoding="utf-8")
    code, out = run(project[0], monkeypatch, capsys)
    assert code == 1
    assert "STALE     main.csv  whole file: Copy X, d. Paste it again." in out


def test_unreadable_sidecar_prints_a_note_and_finds_nothing(project, monkeypatch, capsys):
    (project[1] / "legacy_encodings.json").write_text("{not json", encoding="utf-8")
    code, out = run(project[0], monkeypatch, capsys)
    assert code == 0
    assert "note: could not read legacy_encodings.json" in out
    assert "STALE" not in out
