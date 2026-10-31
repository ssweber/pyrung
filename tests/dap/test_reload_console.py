"""Tests for the DAP reload console commands."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from pyrung.dap.adapter import DAPAdapter
from pyrung.dap.protocol import read_message


def _drain_messages(stream: io.BytesIO) -> list[dict[str, Any]]:
    data = stream.getvalue()
    reader = io.BytesIO(data)
    messages: list[dict[str, Any]] = []
    while True:
        message = read_message(reader)
        if message is None:
            break
        messages.append(message)
    stream.seek(0)
    stream.truncate(0)
    return messages


def _send_request(
    adapter: DAPAdapter,
    out_stream: io.BytesIO,
    *,
    seq: int,
    command: str,
    arguments: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    adapter.handle_request(
        {"seq": seq, "type": "request", "command": command, "arguments": arguments or {}}
    )
    return _drain_messages(out_stream)


def _write_script(tmp_path: Path, name: str, content: str) -> Path:
    script_path = tmp_path / name
    script_path.write_text(content, encoding="utf-8")
    return script_path


def _single_response(messages: list[dict[str, Any]]) -> dict[str, Any]:
    responses = [msg for msg in messages if msg.get("type") == "response"]
    assert len(responses) == 1
    return responses[0]


def _stopped_events(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [msg for msg in messages if msg.get("type") == "event" and msg.get("event") == "stopped"]


def _repl(
    adapter: DAPAdapter, out_stream: io.BytesIO, expression: str, *, seq: int = 10
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    messages = _send_request(
        adapter,
        out_stream,
        seq=seq,
        command="evaluate",
        arguments={"expression": expression, "context": "repl"},
    )
    response = _single_response(messages)
    stopped = _stopped_events(messages)
    return response, stopped


BASIC_SCRIPT = (
    "from pyrung.core import Bool, Int, PLC, Program, Rung, out, copy\n"
    "\n"
    "button = Bool('Button')\n"
    "light = Bool('Light')\n"
    "counter = Int('Counter')\n"
    "\n"
    "with Program(strict=False) as prog:\n"
    "    with Rung(button):\n"
    "        out(light)\n"
    "    with Rung():\n"
    "        copy(0, counter)\n"
    "\n"
    "runner = PLC(prog, dt=0.010)\n"
)


def _setup(tmp_path: Path, script: str = BASIC_SCRIPT) -> tuple[DAPAdapter, io.BytesIO, Path]:
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script_path = _write_script(tmp_path, "logic.py", script)
    _send_request(
        adapter, out_stream, seq=1, command="launch", arguments={"program": str(script_path)}
    )
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)
    return adapter, out_stream, script_path


class TestReload:
    def test_reload_basic(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "step 5", seq=10)
        old_scan_id = adapter._runner.current_state.scan_id

        resp, stopped = _repl(adapter, out, "reload", seq=11)
        assert resp["success"] is True
        assert "Reloaded" in resp["body"]["result"]
        assert adapter._runner.current_state.scan_id == old_scan_id
        assert len(stopped) >= 1

    def test_reload_preserves_tag_values(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 1", seq=11)
        assert adapter._runner.current_state.tags["Light"] is True

        _repl(adapter, out, "reload", seq=12)
        assert adapter._runner.current_state.tags["Light"] is True

    def test_reload_preserves_forces(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "reload", seq=11)
        assert adapter._runner.forces["Button"] is True

    def test_reload_scan_continuity(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "step 5", seq=10)
        scan_before = adapter._runner.current_state.scan_id

        _repl(adapter, out, "reload", seq=11)
        _repl(adapter, out, "step 1", seq=12)
        assert adapter._runner.current_state.scan_id == scan_before + 1

    def test_reload_emits_stopped_event(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        resp, stopped = _repl(adapter, out, "reload", seq=10)
        assert resp["success"] is True
        assert len(stopped) >= 1
        assert stopped[0]["body"]["reason"] == "entry"

    def test_reload_with_new_rung(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 1", seq=11)
        old_scan_id = adapter._runner.current_state.scan_id

        new_script = (
            "from pyrung.core import Bool, Int, PLC, Program, Rung, out, copy\n"
            "\n"
            "button = Bool('Button')\n"
            "light = Bool('Light')\n"
            "counter = Int('Counter')\n"
            "extra = Bool('Extra')\n"
            "\n"
            "with Program(strict=False) as prog:\n"
            "    with Rung(button):\n"
            "        out(light)\n"
            "    with Rung():\n"
            "        copy(0, counter)\n"
            "    with Rung(button):\n"
            "        out(extra)\n"
            "\n"
            "runner = PLC(prog, dt=0.010)\n"
        )
        script_path.write_text(new_script, encoding="utf-8")

        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
        assert adapter._runner.current_state.scan_id == old_scan_id
        assert adapter._runner.current_state.tags["Light"] is True
        assert adapter._runner.forces["Button"] is True
        assert "Extra" in adapter._runner._known_tags_by_name


ONESHOT_SCRIPT = (
    "from pyrung.core import Bool, PLC, Program, Rung, out\n"
    "\n"
    "button = Bool('Button')\n"
    "pulse = Bool('Pulse')\n"
    "extra = Bool('Extra')\n"
    "\n"
    "with Program(strict=False) as prog:\n"
    "{extra}"
    "    with Rung(button):\n"
    "        out(pulse, oneshot=True)\n"
    "\n"
    "runner = PLC(prog, dt=0.010)\n"
)
EXTRA_RUNG = "    with Rung(button):\n        out(extra, oneshot=True)\n"
EDGE_SCRIPT = (
    ONESHOT_SCRIPT.replace("import Bool,", "import Bool, rise,")
    .replace("with Rung(button):", "with Rung(rise(button)):")
    .replace("out(pulse, oneshot=True)", "out(pulse)")
)
EDGE_EXTRA_RUNG = "    with Rung(rise(button)):\n        out(extra)\n"


class TestReloadSiteMemory:
    def test_oneshot_memory_follows_its_site(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path, ONESHOT_SCRIPT.format(extra=""))
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 2", seq=11)
        assert adapter._runner.current_state.memory["_oneshot:i1"] is True
        assert adapter._runner.current_state.tags["Pulse"] is False

        script_path.write_text(ONESHOT_SCRIPT.format(extra=EXTRA_RUNG), encoding="utf-8")
        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
        memory = adapter._runner.current_state.memory
        # Pulse's one-shot moved from i1 to i2; the new rung's is unwritten.
        assert memory["_oneshot:i2"] is True
        assert "_oneshot:i1" not in memory

        _repl(adapter, out, "step 1", seq=13)
        tags = adapter._runner.current_state.tags
        assert tags["Pulse"] is False  # already fired before the reload
        assert tags["Extra"] is True

    def test_unmatched_site_memory_is_dropped(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path, ONESHOT_SCRIPT.format(extra=""))
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 1", seq=11)
        script_path.write_text(
            ONESHOT_SCRIPT.format(extra="").replace("out(pulse", "out(extra"), encoding="utf-8"
        )
        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
        assert "no matching site" in resp["body"]["result"]
        assert "_oneshot:i1" not in adapter._runner.current_state.memory

    def test_an_inserted_edge_contact_does_not_rise_on_a_held_bit(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path, EDGE_SCRIPT.format(extra=""))
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 2", seq=11)
        assert adapter._runner.current_state.memory["_edge:e1"] is True
        assert adapter._runner.current_state.tags["Pulse"] is False

        # The new contact lands on the old one's line, above it.
        script_path.write_text(
            EDGE_SCRIPT.format(extra=EDGE_EXTRA_RUNG),
            encoding="utf-8",
        )
        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "no matching site" not in result
        assert "1 edge contact(s) new to the program start from their tag's current value" in result
        assert "rise(Button) line 8 = on" in result
        memory = adapter._runner.current_state.memory
        assert memory["_edge:e1"] is True
        assert memory["_edge:e2"] is True

        _repl(adapter, out, "step 1", seq=13)
        tags = adapter._runner.current_state.tags
        assert tags["Pulse"] is False
        assert tags["Extra"] is False


class TestReloadTagTypeChange:
    def test_type_change_drops_value(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 1", seq=11)
        assert adapter._runner.current_state.tags["Light"] is True

        changed_script = (
            "from pyrung.core import Int, PLC, Program, Rung, copy\n"
            "\n"
            "light = Int('Light')\n"
            "counter = Int('Counter')\n"
            "\n"
            "with Program(strict=False) as prog:\n"
            "    with Rung():\n"
            "        copy(0, light)\n"
            "    with Rung():\n"
            "        copy(0, counter)\n"
            "\n"
            "runner = PLC(prog, dt=0.010)\n"
        )
        script_path.write_text(changed_script, encoding="utf-8")

        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
        assert "type changed" in resp["body"]["result"]
        assert adapter._runner.current_state.tags["Light"] == 0


class TestReloadErrors:
    def test_syntax_error(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        old_runner = adapter._runner
        script_path.write_text("def broken(\n", encoding="utf-8")

        resp, _ = _repl(adapter, out, "reload", seq=10)
        assert resp["success"] is False
        assert adapter._runner is old_runner

    def test_no_runner(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        old_runner = adapter._runner
        script_path.write_text("x = 42\n", encoding="utf-8")

        resp, _ = _repl(adapter, out, "reload", seq=10)
        assert resp["success"] is False
        assert adapter._runner is old_runner


class TestReloadBlockedByRecording:
    def test_reload_blocked_during_recording(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "record test_action", seq=10)
        resp, _ = _repl(adapter, out, "reload", seq=11)
        assert resp["success"] is False
        assert "recording" in resp.get("message", resp.get("body", {}).get("result", "")).lower()

    def test_reload_allowed_after_recording_stops(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "record test_action", seq=10)
        _repl(adapter, out, "patch Button true", seq=11)
        _repl(adapter, out, "step 3", seq=12)
        _repl(adapter, out, "patch Button false", seq=13)
        _repl(adapter, out, "step 3", seq=14)
        _repl(adapter, out, "patch Button true", seq=15)
        _repl(adapter, out, "step 3", seq=16)
        _repl(adapter, out, "patch Button false", seq=17)
        _repl(adapter, out, "step 3", seq=18)
        _repl(adapter, out, "record stop", seq=19)
        resp, _ = _repl(adapter, out, "reload", seq=20)
        assert resp["success"] is True
        assert "Reloaded" in resp["body"]["result"]


class TestAutoReload:
    def test_autoreload_starts_thread(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "autoreload", seq=10)
        assert resp["success"] is True
        assert "Auto-reload enabled" in resp["body"]["result"]
        assert getattr(adapter, "_autoreload_thread", None) is not None
        assert adapter._autoreload_thread.is_alive()

        resp, _ = _repl(adapter, out, "autoreload off", seq=11)
        assert resp["success"] is True
        assert "disabled" in resp["body"]["result"]
        assert getattr(adapter, "_autoreload_thread", None) is None

    def test_autoreload_already_running(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "autoreload", seq=10)
        resp, _ = _repl(adapter, out, "autoreload", seq=11)
        assert "Already" in resp["body"]["result"]

        _repl(adapter, out, "autoreload off", seq=12)

    def test_autoreload_off_when_not_running(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "autoreload off", seq=10)
        assert "disabled" in resp["body"]["result"]


class TestMemoryPaneLabels:
    def test_site_memory_names_its_contact_and_sorts_by_number(self, tmp_path: Path):
        rungs = "".join(
            f"    with Rung(rise(button)):\n        out(Bool('Out{n}'))\n" for n in range(11)
        )
        adapter, out, _script_path = _setup(tmp_path, EDGE_SCRIPT.format(extra=rungs))
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 1", seq=11)
        messages = _send_request(
            adapter,
            out,
            seq=12,
            command="variables",
            arguments={"variablesReference": adapter.MEMORY_SCOPE_REF},
        )
        variables = _single_response(messages)["body"]["variables"]
        edges = [item for item in variables if item["name"].startswith("_edge:")]
        assert [item["name"] for item in edges] == [f"_edge:e{n}" for n in range(1, 13)]
        assert edges[0]["value"] == "True (rise(Button) line 8)"
        assert edges[11]["value"] == "True (rise(Button) line 30)"
