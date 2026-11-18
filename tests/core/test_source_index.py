"""Check that rung end lines come from the file as it is now, not as it was first read."""

from __future__ import annotations

from pathlib import Path

from pyrung.core._source import _capture_with_call_arg_lines, _capture_with_end_line


def test_an_edited_file_gives_the_new_end_line(tmp_path: Path):
    path = tmp_path / "logic.py"
    path.write_text("with rung():\n    out(a)\n", encoding="utf-8")
    assert _capture_with_end_line(str(path), 1, context_name="Rung") == 2

    path.write_text("with rung():\n    out(a)\n    out(b)\n", encoding="utf-8")
    assert _capture_with_end_line(str(path), 1, context_name="Rung") == 3


def test_the_rung_arg_lines_come_from_the_rung_item_in_either_spelling(tmp_path: Path):
    # Program() comes first on the line, so a lookup that knows only `Rung` would take
    # its empty argument list.
    names = ("rung", "Rung", "RungContext")
    for index, spelling in enumerate(("rung", "Rung")):
        path = tmp_path / f"logic{index}.py"
        path.write_text(
            f"with Program() as p, {spelling}(\n    a,\n    b,\n):\n    out(c)\n",
            encoding="utf-8",
        )
        assert _capture_with_call_arg_lines(str(path), 1, context_name=names) == [2, 3]
