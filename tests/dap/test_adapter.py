"""Tests for the pyrung DAP adapter handlers."""

from __future__ import annotations

import io
import os
import time
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
        {
            "seq": seq,
            "type": "request",
            "command": command,
            "arguments": arguments or {},
        }
    )
    return _drain_messages(out_stream)


def _write_script(tmp_path: Path, name: str, content: str) -> Path:
    script_path = tmp_path / name
    script_path.write_text(content, encoding="utf-8")
    return script_path


def _line_number(script_path: Path, needle: str) -> int:
    for line_number, line in enumerate(script_path.read_text(encoding="utf-8").splitlines(), start=1):
        if needle in line:
            return line_number
    raise AssertionError(f"Could not find line containing {needle!r}")


def _single_response(messages: list[dict[str, Any]]) -> dict[str, Any]:
    responses = [msg for msg in messages if msg.get("type") == "response"]
    assert len(responses) == 1
    return responses[0]


def _stopped_events(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [msg for msg in messages if msg.get("type") == "event" and msg.get("event") == "stopped"]


def _runner_script() -> str:
    return (
        "from pyrung.core import Bool, PLCRunner, Program, Rung, out\n"
        "\n"
        "button = Bool('Button')\n"
        "light = Bool('Light')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(button):\n"
        "        out(light)\n"
        "\n"
        "runner = PLCRunner(prog)\n"
    )


def _program_only_script() -> str:
    return (
        "from pyrung.core import Bool, Program, Rung, out\n"
        "\n"
        "button = Bool('Button')\n"
        "light = Bool('Light')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(button):\n"
        "        out(light)\n"
    )


def _unconditional_script() -> str:
    return (
        "from pyrung.core import Bool, PLCRunner, Program, Rung, out\n"
        "\n"
        "light = Bool('Light')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung():\n"
        "        out(light)\n"
        "\n"
        "runner = PLCRunner(prog)\n"
    )


def _nested_debug_script() -> str:
    return (
        "from pyrung.core import Bool, PLCRunner, Program, Rung, branch, call, out, subroutine\n"
        "\n"
        "main_light = Bool('MainLight')\n"
        "branch_light = Bool('BranchLight')\n"
        "sub_light = Bool('SubLight')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with subroutine('init_sub'):\n"
        "        with Rung():\n"
        "            out(sub_light)\n"
        "\n"
        "    with Rung():\n"
        "        call('init_sub')\n"
        "        with branch():\n"
        "            out(branch_light)\n"
        "        out(main_light)\n"
        "\n"
        "runner = PLCRunner(prog)\n"
    )


def _nested_debug_autorun_script() -> str:
    return (
        "import os\n"
        "from pyrung.core import Bool, Int, PLCRunner, Program, Rung, branch, call, copy, out, return_, subroutine\n"
        "\n"
        "Step = Int('Step')\n"
        "AutoMode = Bool('AutoMode')\n"
        "MainLight = Bool('MainLight')\n"
        "AutoLight = Bool('AutoLight')\n"
        "SubLight = Bool('SubLight')\n"
        "SkippedAfterReturn = Bool('SkippedAfterReturn')\n"
        "\n"
        "with Program(strict=False) as logic:\n"
        "    with Rung(Step == 0):\n"
        "        out(MainLight)\n"
        "        with branch(AutoMode):\n"
        "            out(AutoLight)\n"
        "            copy(1, Step, oneshot=True)\n"
        "        call('init_sub')\n"
        "\n"
        "    with subroutine('init_sub'):\n"
        "        with Rung():\n"
        "            out(SubLight)\n"
        "            return_()\n"
        "            out(SkippedAfterReturn)\n"
        "\n"
        "runner = PLCRunner(logic)\n"
        "runner.patch({'Step': 0, 'AutoMode': True, 'MainLight': False, 'AutoLight': False, 'SubLight': False, 'SkippedAfterReturn': False})\n"
        "if os.getenv('PYRUNG_DAP_ACTIVE') != '1':\n"
        "    runner.step()\n"
    )


def _branch_unpowered_after_first_scan_script() -> str:
    return (
        "from pyrung.core import Bool, Int, PLCRunner, Program, Rung, branch, copy, out\n"
        "\n"
        "Step = Int('Step')\n"
        "AutoMode = Bool('AutoMode')\n"
        "MainLight = Bool('MainLight')\n"
        "AutoLight = Bool('AutoLight')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(Step == 0):\n"
        "        out(MainLight)\n"
        "        with branch(AutoMode):\n"
        "            out(AutoLight)\n"
        "            copy(1, Step, oneshot=True)\n"
        "\n"
        "runner = PLCRunner(prog)\n"
        "runner.patch({'Step': 0, 'AutoMode': True, 'MainLight': False, 'AutoLight': False})\n"
    )


def _branch_then_call_script() -> str:
    return (
        "from pyrung.core import Bool, Int, PLCRunner, Program, Rung, branch, call, copy, out, subroutine\n"
        "\n"
        "Step = Int('Step')\n"
        "AutoMode = Bool('AutoMode')\n"
        "BranchDone = Bool('BranchDone')\n"
        "SubLight = Bool('SubLight')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with subroutine('sub'):\n"
        "        with Rung(Step == 1):\n"
        "            out(SubLight)\n"
        "\n"
        "    with Rung(Step == 0):\n"
        "        with branch(AutoMode):\n"
        "            out(BranchDone)\n"
        "            copy(1, Step, oneshot=True)\n"
        "        call('sub')\n"
        "\n"
        "runner = PLCRunner(prog)\n"
        "runner.patch({'Step': 0, 'AutoMode': True, 'BranchDone': False, 'SubLight': False})\n"
    )


def test_launch_with_runner_emits_entry_stop(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    messages = _send_request(
        adapter,
        out_stream,
        seq=1,
        command="launch",
        arguments={"program": str(script)},
    )

    response = _single_response(messages)
    assert response["success"] is True
    stopped = _stopped_events(messages)
    assert stopped
    assert stopped[0]["body"]["reason"] == "entry"
    assert adapter._runner is not None


def test_launch_wraps_single_program_when_runner_missing(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "program_only.py", _program_only_script())

    messages = _send_request(
        adapter,
        out_stream,
        seq=1,
        command="launch",
        arguments={"program": str(script)},
    )

    response = _single_response(messages)
    assert response["success"] is True
    assert adapter._runner is not None


def test_launch_reports_clear_discovery_error(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "empty.py", "x = 1\n")

    messages = _send_request(
        adapter,
        out_stream,
        seq=1,
        command="launch",
        arguments={"program": str(script)},
    )

    response = _single_response(messages)
    assert response["success"] is False
    assert "Found 0 PLCRunner(s), 0 Program(s)" in response["message"]


def test_set_breakpoints_verifies_instruction_line(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    # line 8 is the `out(light)` instruction in _runner_script.
    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="setBreakpoints",
        arguments={"source": {"path": str(script)}, "lines": [8, 999]},
    )
    response = _single_response(messages)
    breakpoints = response["body"]["breakpoints"]
    assert breakpoints[0]["line"] == 8 and breakpoints[0]["verified"] is True
    assert breakpoints[1]["line"] == 999 and breakpoints[1]["verified"] is False


def test_set_breakpoints_accepts_relative_path(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    relative = os.path.relpath(script, start=Path.cwd())
    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="setBreakpoints",
        arguments={"source": {"path": relative}, "lines": [8]},
    )
    response = _single_response(messages)
    bp = response["body"]["breakpoints"][0]
    assert bp["line"] == 8 and bp["verified"] is True


def test_next_advances_one_rung_and_emits_step_stop(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=2, command="next")
    response = _single_response(messages)
    assert response["success"] is True
    stopped = _stopped_events(messages)
    assert stopped and stopped[0]["body"]["reason"] == "step"
    assert adapter._current_rung_index == 0


def test_stepin_enters_subroutine_but_next_skips_to_top_level_rung(tmp_path: Path):
    stepin_out = io.BytesIO()
    stepin_adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=stepin_out)
    script = _write_script(tmp_path, "nested_logic.py", _nested_debug_script())

    _send_request(stepin_adapter, stepin_out, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(stepin_out)
    _send_request(stepin_adapter, stepin_out, seq=2, command="stepIn")
    _drain_messages(stepin_out)

    assert stepin_adapter._current_step is not None
    assert stepin_adapter._current_step.kind == "subroutine"
    assert stepin_adapter._current_step.subroutine_name == "init_sub"

    next_out = io.BytesIO()
    next_adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=next_out)
    _send_request(next_adapter, next_out, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(next_out)
    _send_request(next_adapter, next_out, seq=2, command="next")
    _drain_messages(next_out)

    assert next_adapter._current_step is not None
    assert next_adapter._current_step.kind == "rung"
    assert next_adapter._current_rung_index == 0


def test_stacktrace_includes_subroutine_context_when_paused_inside_call(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "nested_logic.py", _nested_debug_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=2, command="stepIn")
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=3, command="stackTrace")
    response = _single_response(messages)
    frames = response["body"]["stackFrames"]
    assert any("init_sub" in frame["name"] for frame in frames)


def test_set_breakpoints_verifies_subroutine_line(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "nested_logic.py", _nested_debug_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    sub_line = _line_number(script, "out(sub_light)")
    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="setBreakpoints",
        arguments={"source": {"path": str(script)}, "lines": [sub_line, 9999]},
    )
    response = _single_response(messages)
    breakpoints = response["body"]["breakpoints"]
    assert breakpoints[0]["line"] == sub_line and breakpoints[0]["verified"] is True
    assert breakpoints[1]["line"] == 9999 and breakpoints[1]["verified"] is False


def test_continue_hits_subroutine_breakpoint(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "nested_logic.py", _nested_debug_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    sub_line = _line_number(script, "out(sub_light)")
    _send_request(
        adapter,
        out_stream,
        seq=2,
        command="setBreakpoints",
        arguments={"source": {"path": str(script)}, "lines": [sub_line]},
    )
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=3, command="continue")
    response = _single_response(messages)
    assert response["success"] is True

    found = False
    for _ in range(100):
        adapter._drain_internal_events()
        flushed = _drain_messages(out_stream)
        stops = _stopped_events(flushed)
        if stops and stops[0]["body"]["reason"] == "breakpoint":
            found = True
            break
        time.sleep(0.01)
    assert found is True
    assert adapter._current_step is not None
    assert adapter._current_step.kind == "subroutine"


def test_launch_sets_dap_env_flag_to_skip_script_autorun(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "nested_autorun.py", _nested_debug_autorun_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=2, command="stepIn")
    _drain_messages(out_stream)

    assert adapter._current_step is not None
    assert adapter._current_step.kind == "subroutine"
    assert adapter._current_step.subroutine_name == "init_sub"


def test_stepin_after_branch_becomes_unpowered_stays_on_top_rung(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "branch_unpowered.py", _branch_unpowered_after_first_scan_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    # First scan executes branch and oneshot sets Step=1.
    _send_request(adapter, out_stream, seq=2, command="next")
    _drain_messages(out_stream)
    assert adapter._current_step is not None
    assert adapter._current_step.kind == "rung"

    # Subsequent scans have Step!=0, so parent rung false and branch should not be surfaced.
    _send_request(adapter, out_stream, seq=3, command="stepIn")
    _drain_messages(out_stream)
    assert adapter._current_step is not None
    assert adapter._current_step.kind == "rung"

    _send_request(adapter, out_stream, seq=4, command="stepIn")
    _drain_messages(out_stream)
    assert adapter._current_step is not None
    assert adapter._current_step.kind == "rung"


def test_stepin_skips_powered_branch_when_no_subroutine(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "branch_only.py", _branch_unpowered_after_first_scan_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    # First scan has a powered branch but no subroutine call.
    _send_request(adapter, out_stream, seq=2, command="stepIn")
    _drain_messages(out_stream)
    assert adapter._current_step is not None
    assert adapter._current_step.kind == "rung"


def test_stepin_skips_branch_and_enters_subroutine(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "branch_then_call.py", _branch_then_call_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=2, command="stepIn")
    _drain_messages(out_stream)
    assert adapter._current_step is not None
    assert adapter._current_step.kind == "subroutine"
    assert adapter._current_step.subroutine_name == "sub"


def test_continue_hits_breakpoint_and_emits_stopped_event(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    _send_request(
        adapter,
        out_stream,
        seq=2,
        command="setBreakpoints",
        arguments={"source": {"path": str(script)}, "lines": [8]},
    )
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=3, command="continue")
    response = _single_response(messages)
    assert response["success"] is True
    assert response["body"]["allThreadsContinued"] is True

    found = False
    for _ in range(100):
        adapter._drain_internal_events()
        flushed = _drain_messages(out_stream)
        stops = _stopped_events(flushed)
        if stops and stops[0]["body"]["reason"] == "breakpoint":
            found = True
            break
        time.sleep(0.01)
    assert found is True


def test_pause_stops_running_continue_loop(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=2, command="continue")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="pause")
    _drain_messages(out_stream)

    found = False
    for _ in range(100):
        adapter._drain_internal_events()
        flushed = _drain_messages(out_stream)
        stops = _stopped_events(flushed)
        if stops and stops[0]["body"]["reason"] == "pause":
            found = True
            break
        time.sleep(0.01)
    assert found is True


def test_variables_overlay_pending_mid_scan_values(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic_unconditional.py", _unconditional_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=2, command="next")
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=3,
        command="variables",
        arguments={"variablesReference": adapter.TAGS_SCOPE_REF},
    )
    response = _single_response(messages)
    variables = {item["name"]: item["value"] for item in response["body"]["variables"]}
    assert variables["Light"] == "True"


def test_evaluate_force_commands_mutate_force_map(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    _send_request(
        adapter,
        out_stream,
        seq=2,
        command="evaluate",
        arguments={"expression": "force Button true"},
    )
    assert adapter._runner is not None
    assert adapter._runner.forces["Button"] is True

    _send_request(
        adapter,
        out_stream,
        seq=3,
        command="evaluate",
        arguments={"expression": "remove_force Button"},
    )
    assert "Button" not in adapter._runner.forces

    _send_request(
        adapter,
        out_stream,
        seq=4,
        command="evaluate",
        arguments={"expression": "force Button false"},
    )
    _send_request(
        adapter,
        out_stream,
        seq=5,
        command="evaluate",
        arguments={"expression": "clear_forces"},
    )
    assert dict(adapter._runner.forces) == {}
