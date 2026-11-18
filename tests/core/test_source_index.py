"""Check that rung end lines come from the file as it is now, not as it was first read."""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

from pyrung.core._source import _capture_with_call_arg_lines, _capture_with_end_line
from pyrung.core.program.validation import _RUNG_NAMES


def test_an_edited_file_gives_the_new_end_line(tmp_path: Path):
    path = tmp_path / "logic.py"
    path.write_text("with rung():\n    out(a)\n", encoding="utf-8")
    assert _capture_with_end_line(str(path), 1, context_name="Rung") == 2

    path.write_text("with rung():\n    out(a)\n    out(b)\n", encoding="utf-8")
    assert _capture_with_end_line(str(path), 1, context_name="Rung") == 3


def test_the_rung_arg_lines_come_from_the_rung_item_in_either_spelling(tmp_path: Path):
    # Program() comes first on the line, so a lookup that knows only `Rung` would take
    # its empty argument list.
    for index, spelling in enumerate(("rung", "Rung")):
        path = tmp_path / f"logic{index}.py"
        path.write_text(
            f"with Program() as p, {spelling}(\n    a,\n    b,\n):\n    out(c)\n",
            encoding="utf-8",
        )
        assert _capture_with_call_arg_lines(str(path), 1, context_name=_RUNG_NAMES) == [2, 3]


def test_each_rung_condition_gets_the_line_it_is_written_on(tmp_path: Path):
    # This builds the program from a real file, so it checks the names that rung() passes to
    # the source lookup.
    path = tmp_path / "logic_conditions.py"
    path.write_text(
        textwrap.dedent(
            """\
            from pyrung import Bool, Program, out, rung

            a, b, c = Bool("A"), Bool("B"), Bool("C")

            with Program() as p, rung(
                a,
                b,
            ):
                out(c)
            """
        ),
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("logic_conditions", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    (built,) = module.p.rungs
    assert [condition.source_line for condition in built._conditions] == [6, 7]
