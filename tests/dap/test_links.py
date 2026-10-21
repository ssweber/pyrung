"""Tests for bench links: session wiring over the program's declared links."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from pyrung.dap.adapter import DAPAdapter
from pyrung.dap.capture import _replay_commands
from pyrung.dap.protocol import read_message

_PROGRAM = """\
from pyrung.click import ClickBlocks, TagMap
from pyrung.core import Bool, Int, PLC, Program, Rung, out
from pyrung.core.physical import Physical

blocks = ClickBlocks()
x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = blocks

cmd = Bool("Cmd")
motor = Bool("Motor")
aux = Bool("Aux", physical=Physical("Aux", on_delay="20ms", off_delay="10ms"), link="Motor")
spare = Bool("Spare")
running = Bool("Running")
lamp = Bool("Lamp")
state = Int("State")
sorted_fb = Bool("Sorted")
busy = Bool("Busy")
done = Bool("Done")

with Program(strict=False) as prog:
    with Rung(cmd):
        out(motor)
    with Rung(aux):
        out(running)
    with Rung(spare):
        out(lamp)
    with Rung(state == 2):
        out(busy)
    with Rung(sorted_fb):
        out(done)

mapping = TagMap(
    {cmd: x[1], motor: y[1], aux: x[2], spare: x[3], running: y[2], lamp: y[3], state: ds[1]}
)
runner = PLC(prog, dt=0.010)
"""

_SPARE_DECLARED = _PROGRAM.replace(
    'spare = Bool("Spare")',
    'spare = Bool("Spare", physical=Physical("Spare", on_delay="0ms"), link="Motor")',
)


def _drain(stream: io.BytesIO) -> list[dict[str, Any]]:
    reader = io.BytesIO(stream.getvalue())
    messages: list[dict[str, Any]] = []
    while (message := read_message(reader)) is not None:
        messages.append(message)
    stream.seek(0)
    stream.truncate(0)
    return messages


def _request(
    adapter: DAPAdapter, out: io.BytesIO, command: str, arguments: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    adapter.handle_request(
        {"seq": 1, "type": "request", "command": command, "arguments": arguments or {}}
    )
    adapter._drain_internal_events()
    return _drain(out)


def _response(messages: list[dict[str, Any]]) -> dict[str, Any]:
    responses = [m for m in messages if m.get("type") == "response"]
    assert len(responses) == 1
    return responses[0]


def _ok(
    adapter: DAPAdapter, out: io.BytesIO, command: str, arguments: dict[str, Any] | None = None
):
    response = _response(_request(adapter, out, command, arguments))
    assert response["success"], response
    return response.get("body") or {}


def _repl(adapter: DAPAdapter, out: io.BytesIO, expression: str) -> dict[str, Any]:
    return _response(
        _request(adapter, out, "evaluate", {"expression": expression, "context": "repl"})
    )


def _say(adapter: DAPAdapter, out: io.BytesIO, expression: str) -> str:
    response = _repl(adapter, out, expression)
    assert response["success"], response
    return response["body"]["result"]


def _launch(tmp_path: Path, source: str = _PROGRAM) -> tuple[DAPAdapter, io.BytesIO]:
    out = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out)
    script = tmp_path / "logic.py"
    script.write_text(source, encoding="utf-8")
    _ok(adapter, out, "launch", {"program": str(script)})
    _ok(adapter, out, "configurationDone")
    return adapter, out


def _tag(adapter: DAPAdapter, name: str) -> Any:
    runner = adapter._runner
    assert runner is not None
    return runner.current_state.tags.get(name, False)


def _run_motor(adapter: DAPAdapter, out: io.BytesIO) -> None:
    _say(adapter, out, "patch Cmd true")
    _say(adapter, out, "run 100ms")


def _rows(adapter: DAPAdapter, out: io.BytesIO) -> list[dict[str, Any]]:
    return _ok(adapter, out, "pyrungListLinks")["links"]


class TestConsole:
    def test_links_lists_the_program_link(self, tmp_path: Path):
        adapter, out = _launch(tmp_path)
        text = _say(adapter, out, "links")
        assert text.splitlines()[0] == "Bench wiring: on"
        assert "project  Motor -> Aux  on_delay=20ms off_delay=10ms" in text

    def test_link_by_address_drives_a_bench_feedback(self, tmp_path: Path):
        adapter, out = _launch(tmp_path)
        assert _say(adapter, out, "link X003 Y001 on_delay=0ms") == (
            "Linked Motor -> Spare on the bench"
        )
        _run_motor(adapter, out)
        assert _tag(adapter, "Spare") is True
        assert _tag(adapter, "Lamp") is True
        assert "bench    Motor -> Spare  on_delay=0ms" in _say(adapter, out, "links")

    def test_unlink_cuts_and_restore_brings_back_the_program_link(self, tmp_path: Path):
        adapter, out = _launch(tmp_path)
        assert "free input" in _say(adapter, out, "unlink Aux")
        _run_motor(adapter, out)
        assert _tag(adapter, "Aux") is False
        assert "(cut on the bench)" in _say(adapter, out, "links")

        assert _say(adapter, out, "restore X002") == "Restored the program's link on Aux"
        _say(adapter, out, "run 100ms")
        assert _tag(adapter, "Aux") is True

    def test_unlink_removes_a_bench_only_link(self, tmp_path: Path):
        adapter, out = _launch(tmp_path)
        _say(adapter, out, "link Spare Motor on_delay=0ms")
        assert _say(adapter, out, "unlink Spare") == "Removed the bench link on Spare"
        assert adapter._bench_links == {}

    def test_link_rejects_a_feedback_the_program_writes(self, tmp_path: Path):
        adapter, out = _launch(tmp_path)
        response = _repl(adapter, out, "link Lamp Cmd on_delay=1s")
        assert response["success"] is False
        assert "Lamp is written by the program at Main:R3" in response["message"]

    def test_link_needs_a_response(self, tmp_path: Path):
        adapter, out = _launch(tmp_path)
        response = _repl(adapter, out, "link Spare Motor physical=Spare")
        assert response["success"] is False
        assert "neither timing nor profile" in response["message"]

    def test_link_rejects_unknown_keys(self, tmp_path: Path):
        adapter, out = _launch(tmp_path)
        response = _repl(adapter, out, "link Spare Motor delay=1s")
        assert response["success"] is False
        assert "unknown link key(s): delay" in response["message"]

    def test_trigger_value(self, tmp_path: Path):
        adapter, out = _launch(tmp_path)
        _say(adapter, out, "link Sorted State:2 on_delay=0ms")
        _say(adapter, out, "patch State 2")
        _say(adapter, out, "run 50ms")
        assert _tag(adapter, "Sorted") is True
        assert _tag(adapter, "Done") is True

    def test_relinking_the_program_wiring_drops_the_bench_change(self, tmp_path: Path):
        adapter, out = _launch(tmp_path)
        _say(adapter, out, "link Aux Motor on_delay=50ms")
        text = _say(adapter, out, "link Aux Motor on_delay=20ms off_delay=10ms")
        assert "already has this link in the program" in text
        assert adapter._bench_links == {}

    def test_bench_off_sets_bench_links_aside(self, tmp_path: Path):
        adapter, out = _launch(tmp_path)
        _say(adapter, out, "link Spare Motor on_delay=0ms")
        assert _say(adapter, out, "links bench off") == "Bench wiring off"
        _run_motor(adapter, out)
        assert _tag(adapter, "Spare") is False
        assert _tag(adapter, "Aux") is True  # the program's link still runs

        _say(adapter, out, "links bench on")
        _say(adapter, out, "run 50ms")
        assert _tag(adapter, "Spare") is True

    def test_disable_one_bench_change(self, tmp_path: Path):
        adapter, out = _launch(tmp_path)
        _say(adapter, out, "link Spare Motor on_delay=0ms")
        _say(adapter, out, "links disable Spare")
        assert "(disabled)" in _say(adapter, out, "links")
        _run_motor(adapter, out)
        assert _tag(adapter, "Spare") is False

    def test_link_installs_the_harness_after_removal(self, tmp_path: Path):
        adapter, out = _launch(tmp_path)
        _say(adapter, out, "harness remove")
        _say(adapter, out, "link Spare Motor on_delay=0ms")
        assert adapter._harness is not None
        _run_motor(adapter, out)
        assert _tag(adapter, "Spare") is True


class TestReload:
    def test_bench_links_survive_reload(self, tmp_path: Path):
        adapter, out = _launch(tmp_path)
        _say(adapter, out, "link Spare Motor on_delay=0ms")
        _say(adapter, out, "unlink Aux")
        _say(adapter, out, "reload")
        assert adapter._harness is not None
        assert set(adapter._harness.overrides) == {"Spare", "Aux"}
        _run_motor(adapter, out)
        assert _tag(adapter, "Spare") is True
        assert _tag(adapter, "Aux") is False

    def test_a_link_that_reached_the_program_is_dropped(self, tmp_path: Path):
        adapter, out = _launch(tmp_path)
        _say(adapter, out, "link Spare Motor on_delay=0ms")
        (tmp_path / "logic.py").write_text(_SPARE_DECLARED, encoding="utf-8")
        text = _say(adapter, out, "reload")
        assert "Bench link on Spare is now in the program." in text
        assert adapter._bench_links == {}


class TestRequests:
    def test_link_list_set_unlink_restore(self, tmp_path: Path):
        adapter, out = _launch(tmp_path)
        body = _ok(
            adapter, out, "pyrungLink", {"feedback": "X003", "enable": "Y001", "onDelay": "0ms"}
        )
        assert body == {
            "text": "Linked Motor -> Spare on the bench",
            "command": "link Spare Motor on_delay=0ms",
        }

        rows = {(row["source"], row["feedback"]): row for row in _rows(adapter, out)}
        assert rows["project", "Aux"]["active"] is True
        assert rows["bench", "Spare"] == {
            "feedback": "Spare",
            "enable": "Motor",
            "trigger": None,
            "onDelay": "0ms",
            "offDelay": None,
            "profile": None,
            "physical": "Spare",
            "system": None,
            "source": "bench",
            "cut": False,
            "enabled": True,
            "active": True,
            "pending": False,
        }

        body = _ok(adapter, out, "pyrungSetLinks", {"bench": False})
        assert body["bench"] is False
        assert all(not row["active"] for row in body["links"] if row["source"] == "bench")
        _ok(adapter, out, "pyrungSetLinks", {"bench": True, "enabled": {"Spare": False}})

        _ok(adapter, out, "pyrungUnlink", {"feedback": "Aux"})
        rows = {(row["source"], row["feedback"]): row for row in _rows(adapter, out)}
        assert rows["project", "Aux"]["active"] is False
        assert rows["bench", "Aux"]["cut"] is True

        _ok(adapter, out, "pyrungRestoreLink", {"feedback": "Aux"})
        assert {row["feedback"] for row in _rows(adapter, out) if row["source"] == "bench"} == {
            "Spare"
        }

    def test_link_errors_are_request_failures(self, tmp_path: Path):
        adapter, out = _launch(tmp_path)
        response = _response(
            _request(
                adapter, out, "pyrungLink", {"feedback": "Lamp", "enable": "Cmd", "onDelay": "1s"}
            )
        )
        assert response["success"] is False
        assert "written by the program" in response["message"]

        response = _response(_request(adapter, out, "pyrungRestoreLink", {"feedback": "Spare"}))
        assert response["success"] is False
        assert response["message"] == "Spare has no bench change"

    def test_set_links_changes_nothing_when_an_entry_is_unknown(self, tmp_path: Path):
        adapter, out = _launch(tmp_path)
        response = _response(
            _request(adapter, out, "pyrungSetLinks", {"bench": False, "enabled": {"Spare": True}})
        )
        assert response["success"] is False
        assert response["message"] == "Spare has no bench change"
        assert adapter._bench_links_on is True

    def test_trigger_argument(self, tmp_path: Path):
        adapter, out = _launch(tmp_path)
        body = _ok(
            adapter,
            out,
            "pyrungLink",
            {"feedback": "Sorted", "enable": "State", "trigger": 2, "onDelay": "0ms"},
        )
        assert body["command"] == "link Sorted State:2 on_delay=0ms"

    def test_a_change_sends_a_frame_with_the_links_revision(self, tmp_path: Path):
        adapter, out = _launch(tmp_path)
        before = adapter._links_revision
        messages = _request(
            adapter, out, "pyrungLink", {"feedback": "Spare", "enable": "Motor", "onDelay": "0ms"}
        )
        frames = [
            m["body"]
            for m in messages
            if m.get("type") == "event" and m["event"] == "pyrungScanFrame"
        ]
        assert frames and frames[-1]["linksRevision"] > before


class TestCapture:
    def test_requests_are_recorded_as_console_commands(self, tmp_path: Path):
        adapter, out = _launch(tmp_path)
        _say(adapter, out, "record wiring")
        _ok(adapter, out, "pyrungLink", {"feedback": "Spare", "enable": "Motor", "onDelay": "0ms"})
        _ok(adapter, out, "pyrungUnlink", {"feedback": "Aux"})
        _say(adapter, out, "links")
        transcript = _say(adapter, out, "record stop")
        assert "link Spare Motor on_delay=0ms" in transcript
        assert "unlink Aux" in transcript
        assert "\nlinks\n" not in transcript

    def test_replay_keeps_link_verbs_only_with_the_current_harness(self):
        lines = ["link Spare Motor on_delay=0ms", "unlink Aux", "step 2"]
        assert [cmd for _n, cmd in _replay_commands(lines, harness_mode="current")] == lines
        for mode in ("recorded", "off"):
            assert [cmd for _n, cmd in _replay_commands(lines, harness_mode=mode)] == ["step 2"]
