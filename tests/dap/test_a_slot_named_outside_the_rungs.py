"""What the debugger knows of a slot a range reaches, named outside the rungs.

The adapter's readers know a slot the rungs reach, whether or not the user's code names it.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from pyrung.dap.adapter import DAPAdapter
from pyrung.dap.protocol import read_message

_NAMED_OUTSIDE_THE_RUNGS = """
from pyrung.core import PLC, Block, Program, Rung, TagType, fill

recipe = Block("Rq", TagType.INT, 1, 5)
recipe.slot(2, choices={0: "zero", 7: "seven"})
alias = recipe[2]

with Program(strict=False) as prog:
    with Rung():
        fill(7, recipe.select(1, 3))

runner = PLC(prog, dt=0.01)
"""


def _drain(stream: io.BytesIO) -> list[dict[str, Any]]:
    reader = io.BytesIO(stream.getvalue())
    messages: list[dict[str, Any]] = []
    while (message := read_message(reader)) is not None:
        messages.append(message)
    stream.seek(0)
    stream.truncate(0)
    return messages


def _launched(tmp_path: Path, script: str = _NAMED_OUTSIDE_THE_RUNGS) -> tuple[DAPAdapter, Any]:
    out = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out)
    path = tmp_path / "logic.py"
    path.write_text(script, encoding="utf-8")
    for seq, (command, arguments) in enumerate(
        [("launch", {"program": str(path)}), ("configurationDone", {})], start=1
    ):
        adapter.handle_request(
            {"seq": seq, "type": "request", "command": command, "arguments": arguments}
        )
    _drain(out)
    return adapter, out


def _say(adapter: DAPAdapter, out: io.BytesIO, text: str) -> dict[str, Any]:
    adapter.handle_request(
        {
            "seq": 100,
            "type": "request",
            "command": "evaluate",
            "arguments": {"expression": text, "context": "repl"},
        }
    )
    return next(msg for msg in _drain(out) if msg.get("type") == "response")


def test_prove_resolves_a_label_of_such_a_slot(tmp_path: Path) -> None:
    adapter, out = _launched(tmp_path)
    answer = _say(adapter, out, "prove always Rq2 != seven")
    assert answer["success"] is True, answer
    assert "Counterexample" in answer["body"]["result"]


def test_prove_resolves_a_label_of_such_a_slot_by_its_own_name(tmp_path: Path) -> None:
    adapter, out = _launched(
        tmp_path,
        _NAMED_OUTSIDE_THE_RUNGS.replace("recipe.slot(2, ", 'recipe.slot(2, name="Recipe", '),
    )
    answer = _say(adapter, out, "prove always Recipe != seven")
    assert answer["success"] is True, answer
    assert "Counterexample" in answer["body"]["result"]


def test_how_knows_such_a_slot(tmp_path: Path) -> None:
    adapter, out = _launched(tmp_path)
    answer = _say(adapter, out, "how Rq2 == 7")
    assert "unknown tag" not in str(answer)


_NOT_NAMED = _NAMED_OUTSIDE_THE_RUNGS.replace("alias = recipe[2]\n", "")


def test_prove_resolves_a_label_of_a_slot_no_code_names(tmp_path: Path) -> None:
    adapter, out = _launched(tmp_path, _NOT_NAMED)
    answer = _say(adapter, out, "prove always Rq2 != seven")
    assert answer["success"] is True, answer
    assert "Counterexample" in answer["body"]["result"]


def test_how_knows_a_slot_no_code_names(tmp_path: Path) -> None:
    adapter, out = _launched(tmp_path, _NOT_NAMED)
    answer = _say(adapter, out, "how Rq2 == seven")
    assert "unknown tag" not in str(answer)
    assert answer["success"] is True, answer


def test_the_data_view_knows_its_type_and_hints(tmp_path: Path) -> None:
    adapter, _out = _launched(tmp_path)
    runner = adapter._runner
    assert adapter._tag_types_locked(runner)["Rq2"] == "int"
    assert adapter._tag_hints_locked(runner)["Rq2"]["choices"] == {"0": "zero", "7": "seven"}


def test_reload_warns_of_its_type_changing(tmp_path: Path) -> None:
    adapter, out = _launched(tmp_path)
    (tmp_path / "logic.py").write_text(
        _NAMED_OUTSIDE_THE_RUNGS.replace("TagType.INT", "TagType.REAL").replace(
            'recipe.slot(2, choices={0: "zero", 7: "seven"})\n', ""
        ),
        encoding="utf-8",
    )
    answer = _say(adapter, out, "reload")
    assert "type changed" in answer["body"]["result"]


def test_patch_refuses_a_word_on_a_numeric_slot_cleanly(tmp_path: Path) -> None:
    adapter, out = _launched(tmp_path)
    answer = _say(adapter, out, "patch Rq2 seven")
    assert answer["success"] is False
    assert answer["message"] == "cannot patch: patch() of Rq2: INT holds a number, not 'seven'"


def test_force_refuses_a_word_on_a_numeric_slot_cleanly(tmp_path: Path) -> None:
    adapter, out = _launched(tmp_path)
    answer = _say(adapter, out, "force Rq2 seven")
    assert answer["success"] is False
    assert answer["message"] == "cannot force: force() of Rq2: INT holds a number, not 'seven'"
