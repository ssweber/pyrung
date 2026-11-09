"""Rung end lines come from the file as it is now, not as it was first read."""

from __future__ import annotations

from pathlib import Path

from pyrung.core._source import _capture_with_end_line


def test_an_edited_file_gives_the_new_end_line(tmp_path: Path):
    path = tmp_path / "logic.py"
    path.write_text("with Rung():\n    out(a)\n", encoding="utf-8")
    assert _capture_with_end_line(str(path), 1, context_name="Rung") == 2

    path.write_text("with Rung():\n    out(a)\n    out(b)\n", encoding="utf-8")
    assert _capture_with_end_line(str(path), 1, context_name="Rung") == 3
