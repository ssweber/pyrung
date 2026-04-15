"""Tests for the pyrung DAP adapter handlers."""

from __future__ import annotations

import io
import os
import queue
import time
from pathlib import Path
from typing import Any

from pyrung.core import Counter
from pyrung.core.debug_trace import RungTraceEvent
from pyrung.dap.adapter import DAPAdapter
from pyrung.dap.protocol import read_message

Counter2 = Counter.clone("Counter2")


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
    for line_number, line in enumerate(
        script_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if needle in line:
            return line_number
    raise AssertionError(f"Could not find line containing {needle!r}")


def _single_response(messages: list[dict[str, Any]]) -> dict[str, Any]:
    responses = [msg for msg in messages if msg.get("type") == "response"]
    assert len(responses) == 1
    return responses[0]


def _stopped_events(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [msg for msg in messages if msg.get("type") == "event" and msg.get("event") == "stopped"]


def _trace_events(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        msg for msg in messages if msg.get("type") == "event" and msg.get("event") == "pyrungTrace"
    ]


def _events(messages: list[dict[str, Any]], event_name: str) -> list[dict[str, Any]]:
    return [
        msg for msg in messages if msg.get("type") == "event" and msg.get("event") == event_name
    ]


def _drain_internal_events_with_wait(adapter: DAPAdapter, *, timeout: float = 0.1) -> int:
    processed = adapter._drain_internal_events()
    if processed:
        return processed

    try:
        item = adapter._queue.get(timeout=timeout)
    except queue.Empty:
        return 0

    if item.get("kind") != "internal_event":
        adapter._queue.put(item)
        return 0

    adapter._send_event(item["event"], item.get("body"))
    if item.get("event") == "stopped":
        adapter._emit_trace_event()
    return 1 + adapter._drain_internal_events()


def _wait_for_stop_reason(
    adapter: DAPAdapter,
    out_stream: io.BytesIO,
    *,
    reason: str,
    attempts: int = 100,
) -> bool:
    for _ in range(attempts):
        flushed = _drain_messages(out_stream)
        stops = _stopped_events(flushed)
        if stops and stops[0]["body"]["reason"] == reason:
            return True
        _drain_internal_events_with_wait(adapter)
    return False


def _wait_for_event(
    adapter: DAPAdapter,
    out_stream: io.BytesIO,
    *,
    event_name: str,
    predicate: Any = None,
    attempts: int = 100,
) -> dict[str, Any] | None:
    for _ in range(attempts):
        flushed = _drain_messages(out_stream)
        matches = _events(flushed, event_name)
        for match in matches:
            if predicate is None or predicate(match):
                return match
        _drain_internal_events_with_wait(adapter)
    return None


def _runner_script() -> str:
    return (
        "from pyrung.core import Bool, PLC, Program, Rung, out\n"
        "\n"
        "button = Bool('Button')\n"
        "light = Bool('Light')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(button):\n"
        "        out(light)\n"
        "\n"
        "runner = PLC(prog)\n"
    )


def _conditional_breakpoint_script(*, button: bool) -> str:
    return (
        "from pyrung.core import Bool, PLC, Program, Rung, out\n"
        "from pyrung.core.state import SystemState\n"
        "\n"
        "button = Bool('Button')\n"
        "light = Bool('Light')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(button):\n"
        "        out(light)\n"
        "\n"
        f"runner = PLC(prog, initial_state=SystemState().with_tags({{'Button': {button!r}}}))\n"
    )


def _monitor_change_script() -> str:
    return (
        "from pyrung.core import Bool, PLC, Program, Rung, out\n"
        "\n"
        "Tick = Bool('Tick')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung():\n"
        "        out(Tick)\n"
        "\n"
        "runner = PLC(prog)\n"
    )


def _snapshot_once_script() -> str:
    return (
        "from pyrung.core import Bool, PLC, Program, Rung, out\n"
        "from pyrung.core.state import SystemState\n"
        "\n"
        "Tick = Bool('Tick')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung():\n"
        "        out(Tick)\n"
        "\n"
        "runner = PLC(prog, initial_state=SystemState().with_tags({'Tick': False}))\n"
    )


def _counter_change_script() -> str:
    return (
        "from pyrung.core import Int, PLC, Program, Rung, copy\n"
        "\n"
        "Counter = Int('Counter')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung():\n"
        "        copy(Counter + 1, Counter)\n"
        "\n"
        "runner = PLC(prog)\n"
    )


def _tag_hints_script() -> str:
    return (
        "from enum import IntEnum\n"
        "from pyrung.core import Int, PLC, Program, Rung, copy\n"
        "\n"
        "class Mode(IntEnum):\n"
        "    IDLE = 0\n"
        "    RUN = 1\n"
        "\n"
        "ModeTag = Int('ModeTag', choices=Mode, readonly=True)\n"
        "Plain = Int('Plain')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung():\n"
        "        copy(0, ModeTag)\n"
        "        copy(0, Plain)\n"
        "\n"
        "runner = PLC(prog)\n"
    )


def _history_changes_script() -> str:
    return (
        "from enum import IntEnum\n"
        "from pyrung.core import Bool, Int, PLC, Program, Rung, copy\n"
        "\n"
        "class ConveyorState(IntEnum):\n"
        "    IDLE = 0\n"
        "    DETECTING = 1\n"
        "    SORTING = 2\n"
        "\n"
        "StepCount = Int('StepCount')\n"
        "State = Int('State', choices=ConveyorState)\n"
        "Running = Bool('Running')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung():\n"
        "        copy(StepCount + 1, StepCount)\n"
        "    with Rung(StepCount == 1):\n"
        "        copy(1, State)\n"
        "        copy(True, Running)\n"
        "    with Rung(StepCount == 3):\n"
        "        copy(2, State)\n"
        "    with Rung(StepCount == 5):\n"
        "        copy(False, Running)\n"
        "\n"
        "runner = PLC(prog)\n"
    )


def _char_runner_script() -> str:
    return (
        "from pyrung.core import Char, PLC, Program, Rung, copy\n"
        "\n"
        "Mode = Char('Mode')\n"
        "Mirror = Char('Mirror')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung():\n"
        "        copy(Mode, Mirror)\n"
        "\n"
        "runner = PLC(prog)\n"
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
        "from pyrung.core import Bool, PLC, Program, Rung, out\n"
        "\n"
        "light = Bool('Light')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung():\n"
        "        out(light)\n"
        "\n"
        "runner = PLC(prog)\n"
    )


def _two_rung_unconditional_script() -> str:
    return (
        "from pyrung.core import Bool, PLC, Program, Rung, out\n"
        "\n"
        "light = Bool('Light')\n"
        "other = Bool('Other')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung():\n"
        "        out(light)\n"
        "    with Rung():\n"
        "        out(other)\n"
        "\n"
        "runner = PLC(prog)\n"
    )


def _empty_logic_runner_script() -> str:
    return "from pyrung.core import PLC\n\nrunner = PLC()\n"


def _composite_condition_script() -> str:
    return (
        "from pyrung.core import Bool, PLC, Program, Rung, And, Or, out\n"
        "\n"
        "start = Bool('Start')\n"
        "ready = Bool('Ready')\n"
        "auto = Bool('Auto')\n"
        "light = Bool('Light')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(Or(start, And(ready, auto))):\n"
        "        out(light)\n"
        "\n"
        "runner = PLC(prog)\n"
    )


def _all_of_short_circuit_script() -> str:
    return (
        "from pyrung.core import Bool, Int, PLC, Program, Rung, And, out\n"
        "\n"
        "Step = Int('Step')\n"
        "AutoMode = Bool('AutoMode')\n"
        "Light = Bool('Light')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(And(Step == 1, AutoMode)):\n"
        "        out(Light)\n"
        "\n"
        "runner = PLC(prog)\n"
        "runner.patch({'Step': 0, 'AutoMode': True})\n"
    )


def _any_of_short_circuit_script() -> str:
    return (
        "from pyrung.core import Bool, Int, PLC, Program, Rung, Or, out\n"
        "\n"
        "Step = Int('Step')\n"
        "AutoMode = Bool('AutoMode')\n"
        "Light = Bool('Light')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(Or(Step == 0, AutoMode)):\n"
        "        out(Light)\n"
        "\n"
        "runner = PLC(prog)\n"
        "runner.patch({'Step': 0, 'AutoMode': False})\n"
    )


def _any_of_rise_short_circuit_script() -> str:
    return (
        "from pyrung.core import Bool, PLC, Program, Rung, Or, out, rise\n"
        "\n"
        "Pulse = Bool('Pulse')\n"
        "AutoMode = Bool('AutoMode')\n"
        "Light = Bool('Light')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(Or(rise(Pulse), AutoMode)):\n"
        "        out(Light)\n"
        "\n"
        "runner = PLC(prog)\n"
        "runner.patch({'Pulse': True, 'AutoMode': False})\n"
    )


def _indirect_condition_script() -> str:
    return (
        "from pyrung.core import Block, Bool, Int, PLC, Program, Rung, TagType, out\n"
        "\n"
        "Step = Block('Step', TagType.INT, 0, 9, address_formatter=lambda name, addr: f\"{name}[{addr}]\")\n"
        "CurStep = Int('CurStep')\n"
        "DebugStep = Int('DebugStep')\n"
        "Light = Bool('Light')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(Step[CurStep] == DebugStep):\n"
        "        out(Light)\n"
        "\n"
        "runner = PLC(prog)\n"
        "runner.patch({'CurStep': 1, 'Step[1]': 0, 'DebugStep': 5})\n"
    )


def _right_indirect_condition_script() -> str:
    return (
        "from pyrung.core import Block, Bool, Int, PLC, Program, Rung, TagType, out\n"
        "\n"
        "Step = Block('Step', TagType.INT, 0, 9, address_formatter=lambda name, addr: f\"{name}[{addr}]\")\n"
        "CurStep = Int('CurStep')\n"
        "DebugStep = Int('DebugStep')\n"
        "Light = Bool('Light')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(DebugStep == Step[CurStep]):\n"
        "        out(Light)\n"
        "\n"
        "runner = PLC(prog)\n"
        "runner.patch({'CurStep': 1, 'Step[1]': 0, 'DebugStep': 5})\n"
    )


def _right_indirect_expr_condition_script() -> str:
    return (
        "from pyrung.core import Block, Bool, Int, PLC, Program, Rung, TagType, out\n"
        "\n"
        "Step = Block('Step', TagType.INT, 0, 9, address_formatter=lambda name, addr: f\"{name}[{addr}]\")\n"
        "CurStep = Int('CurStep')\n"
        "DebugStep = Int('DebugStep')\n"
        "Light = Bool('Light')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(DebugStep == Step[CurStep + 1]):\n"
        "        out(Light)\n"
        "\n"
        "runner = PLC(prog)\n"
        "runner.patch({'CurStep': 4, 'Step[5]': 5, 'DebugStep': 5})\n"
    )


def _nested_debug_script() -> str:
    return (
        "from pyrung.core import Bool, PLC, Program, Rung, branch, call, out, subroutine\n"
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
        "runner = PLC(prog)\n"
    )


def _chained_builder_debug_script() -> str:
    return (
        "from pyrung.core import Block, Bool, Counter, PLC, Program, Rung, TagType, Timer, count_down, count_up, on_delay, shift\n"
        "\n"
        "Counter2 = Counter.clone('Counter2')\n"
        "\n"
        "Enable = Bool('Enable')\n"
        "Down = Bool('Down')\n"
        "Reset = Bool('Reset')\n"
        "Clock = Bool('Clock')\n"
        "C = Block('C', TagType.BOOL, 1, 8)\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(Enable):\n"
        "        cu_builder = count_up(Counter[1], preset=5)\n"
        "        cu_builder = cu_builder.down(Down)\n"
        "        cu_builder.reset(Reset)\n"
        "\n"
        "    with Rung(Enable):\n"
        "        cd_builder = count_down(Counter2, preset=5)\n"
        "        cd_builder.reset(Reset)\n"
        "\n"
        "    with Rung(Enable):\n"
        "        timer_builder = on_delay(Timer[1], preset=50)\n"
        "        timer_builder.reset(Reset)\n"
        "\n"
        "    with Rung(Enable):\n"
        "        shift_builder = shift(C.select(1, 3))\n"
        "        shift_builder = shift_builder.clock(Clock)\n"
        "        shift_builder.reset(Reset)\n"
        "\n"
        "runner = PLC(prog)\n"
        "runner.patch({'Enable': True, 'Down': True, 'Reset': False, 'Clock': True})\n"
    )


def _nested_debug_autorun_script() -> str:
    return (
        "import os\n"
        "from pyrung.core import Bool, Int, PLC, Program, Rung, branch, call, copy, out, return_early, subroutine\n"
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
        "            return_early()\n"
        "            out(SkippedAfterReturn)\n"
        "\n"
        "runner = PLC(logic)\n"
        "runner.patch({'Step': 0, 'AutoMode': True, 'MainLight': False, 'AutoLight': False, 'SubLight': False, 'SkippedAfterReturn': False})\n"
        "if os.getenv('PYRUNG_DAP_ACTIVE') != '1':\n"
        "    runner.step()\n"
    )


def _branch_unpowered_after_first_scan_script() -> str:
    return (
        "from pyrung.core import Bool, Int, PLC, Program, Rung, branch, copy, out\n"
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
        "runner = PLC(prog)\n"
        "runner.patch({'Step': 0, 'AutoMode': True, 'MainLight': False, 'AutoLight': False})\n"
    )


def _branch_then_call_script() -> str:
    return (
        "from pyrung.core import Bool, Int, PLC, Program, Rung, branch, call, copy, out, subroutine\n"
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
        "runner = PLC(prog)\n"
        "runner.patch({'Step': 0, 'AutoMode': True, 'BranchDone': False, 'SubLight': False})\n"
    )


def test_initialize_advertises_capabilities():
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)

    messages = _send_request(adapter, out_stream, seq=1, command="initialize")
    response = _single_response(messages)
    body = response["body"]
    assert response["success"] is True
    assert body["supportsConfigurationDoneRequest"] is True
    assert body["supportsEvaluateForHovers"] is False
    assert body["supportsStepBack"] is False
    assert body["supportsStepOut"] is True
    assert body["supportsTerminateRequest"] is True
    initialized = [
        msg for msg in messages if msg.get("type") == "event" and msg.get("event") == "initialized"
    ]
    assert initialized


def test_terminate_stops_adapter_and_emits_terminated_event():
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)

    messages = _send_request(adapter, out_stream, seq=1, command="terminate")
    response = _single_response(messages)
    assert response["success"] is True
    terminated = [
        msg for msg in messages if msg.get("type") == "event" and msg.get("event") == "terminated"
    ]
    assert terminated
    assert adapter._stop_event.is_set() is True
    assert adapter._pause_event.is_set() is True


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


def test_launch_wraps_single_program_when_runner_is_omitted(tmp_path: Path):
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
    assert "Found 0 PLC(s), 0 Program(s)" in response["message"]


def test_launch_requires_program_string(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    _write_script(tmp_path, "logic.py", _runner_script())

    messages = _send_request(
        adapter,
        out_stream,
        seq=1,
        command="launch",
        arguments={"program": 123},
    )

    response = _single_response(messages)
    assert response["success"] is False
    assert response["message"] == "launch.program must be a Python file path"


def test_variables_non_object_arguments_keep_legacy_internal_error_shape(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    adapter.handle_request(
        {
            "seq": 2,
            "type": "request",
            "command": "variables",
            "arguments": [1],
        }
    )
    response = _single_response(_drain_messages(out_stream))
    assert response["success"] is False
    assert response["message"] == "Internal adapter error: 'list' object has no attribute 'get'"


def test_set_breakpoints_non_object_arguments_keep_legacy_internal_error_shape(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    adapter.handle_request(
        {
            "seq": 2,
            "type": "request",
            "command": "setBreakpoints",
            "arguments": [1],
        }
    )
    response = _single_response(_drain_messages(out_stream))
    assert response["success"] is False
    assert response["message"] == "Internal adapter error: 'list' object has no attribute 'get'"


def test_variables_reference_coerces_numeric_string(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="variables",
        arguments={"variablesReference": "1"},
    )
    response = _single_response(messages)
    assert response["success"] is True
    assert "variables" in response["body"]


def test_stacktrace_startframe_and_levels_coerce_numeric_strings(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=2, command="next")
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=3,
        command="stackTrace",
        arguments={"startFrame": "0", "levels": "1"},
    )
    response = _single_response(messages)
    assert response["success"] is True
    assert response["body"]["totalFrames"] >= 1
    assert len(response["body"]["stackFrames"]) == 1


def test_pyrung_add_monitor_requires_non_empty_tag(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="pyrungAddMonitor",
        arguments={"tag": "   "},
    )
    response = _single_response(messages)
    assert response["success"] is False
    assert response["message"] == "pyrungAddMonitor.tag is required"


def test_pyrung_remove_monitor_requires_int_id(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="pyrungRemoveMonitor",
        arguments={"id": "1"},
    )
    response = _single_response(messages)
    assert response["success"] is False
    assert response["message"] == "pyrungRemoveMonitor.id must be an integer"


def test_pyrung_find_label_requires_non_empty_label(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="pyrungFindLabel",
        arguments={"label": ""},
    )
    response = _single_response(messages)
    assert response["success"] is False
    assert response["message"] == "pyrungFindLabel.label is required"


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


def test_next_with_empty_logic_runner_keeps_scan_stack_frame(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "empty_runner.py", _empty_logic_runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=2, command="next")
    response = _single_response(messages)
    assert response["success"] is True
    stopped = _stopped_events(messages)
    assert stopped and stopped[0]["body"]["reason"] == "step"
    assert adapter._current_step is None

    stack_messages = _send_request(adapter, out_stream, seq=3, command="stackTrace")
    stack_response = _single_response(stack_messages)
    assert stack_response["body"]["stackFrames"][0]["name"] == "Scan"


def test_stepin_enters_subroutine_but_next_skips_to_top_level_rung(tmp_path: Path):
    stepin_out = io.BytesIO()
    stepin_adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=stepin_out)
    script = _write_script(tmp_path, "nested_logic.py", _nested_debug_script())

    _send_request(
        stepin_adapter, stepin_out, seq=1, command="launch", arguments={"program": str(script)}
    )
    _drain_messages(stepin_out)
    _send_request(stepin_adapter, stepin_out, seq=2, command="stepIn")
    _drain_messages(stepin_out)

    call_line = _line_number(script, "call('init_sub')")
    assert stepin_adapter._current_step is not None
    assert stepin_adapter._current_step.kind == "instruction"
    assert stepin_adapter._current_step.subroutine_name is None
    assert stepin_adapter._current_step.source_line == call_line

    _send_request(stepin_adapter, stepin_out, seq=3, command="stepIn")
    _drain_messages(stepin_out)
    assert stepin_adapter._current_step is not None
    assert stepin_adapter._current_step.kind == "instruction"
    assert stepin_adapter._current_step.subroutine_name == "init_sub"

    next_out = io.BytesIO()
    next_adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=next_out)
    _send_request(
        next_adapter, next_out, seq=1, command="launch", arguments={"program": str(script)}
    )
    _drain_messages(next_out)
    _send_request(next_adapter, next_out, seq=2, command="next")
    _drain_messages(next_out)

    assert next_adapter._current_step is not None
    assert next_adapter._current_step.kind == "rung"
    assert next_adapter._current_rung_index == 0


def test_stepout_returns_from_subroutine_to_caller_context(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "nested_logic.py", _nested_debug_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=2, command="stepIn")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="stepIn")
    _drain_messages(out_stream)

    assert adapter._current_step is not None
    origin_stack_len = len(adapter._current_step.call_stack)
    assert origin_stack_len > 0

    messages = _send_request(adapter, out_stream, seq=4, command="stepOut")
    response = _single_response(messages)
    assert response["success"] is True
    stopped = _stopped_events(messages)
    assert stopped and stopped[0]["body"]["reason"] == "step"
    assert adapter._current_step is not None
    assert len(adapter._current_step.call_stack) < origin_stack_len


def test_stepout_exits_branch_to_parent_depth(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(
        tmp_path, "branch_unpowered.py", _branch_unpowered_after_first_scan_script()
    )

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=2, command="stepIn")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="stepIn")
    _drain_messages(out_stream)

    assert adapter._current_step is not None
    origin_depth = adapter._current_step.depth
    assert origin_depth > 0

    messages = _send_request(adapter, out_stream, seq=4, command="stepOut")
    response = _single_response(messages)
    assert response["success"] is True
    stopped = _stopped_events(messages)
    assert stopped and stopped[0]["body"]["reason"] == "step"
    assert adapter._current_step is not None
    assert adapter._current_step.depth < origin_depth


def test_stepout_from_top_level_advances_to_new_scan_context(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=2, command="next")
    _drain_messages(out_stream)

    assert adapter._current_step is not None
    assert adapter._current_step.depth == 0
    assert len(adapter._current_step.call_stack) == 0
    origin_ctx = adapter._current_ctx

    messages = _send_request(adapter, out_stream, seq=3, command="stepOut")
    response = _single_response(messages)
    assert response["success"] is True
    stopped = _stopped_events(messages)
    assert stopped and stopped[0]["body"]["reason"] == "step"
    assert adapter._current_step is not None
    assert adapter._current_ctx is not origin_ctx


def test_pyrung_step_scan_from_entry_advances_to_next_scan(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    assert adapter._current_ctx is None

    messages = _send_request(adapter, out_stream, seq=2, command="pyrungStepScan")
    response = _single_response(messages)
    assert response["success"] is True
    stopped = _stopped_events(messages)
    assert stopped and stopped[0]["body"]["reason"] == "step"
    assert adapter._current_ctx is not None
    assert adapter._current_scan_id == 2


def test_pyrung_step_scan_from_nested_step_advances_to_new_scan_context(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "nested_logic.py", _nested_debug_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=2, command="stepIn")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="stepIn")
    _drain_messages(out_stream)

    assert adapter._current_step is not None
    assert len(adapter._current_step.call_stack) > 0
    origin_ctx = adapter._current_ctx
    origin_scan_id = adapter._current_scan_id

    messages = _send_request(adapter, out_stream, seq=4, command="pyrungStepScan")
    response = _single_response(messages)
    assert response["success"] is True
    stopped = _stopped_events(messages)
    assert stopped and stopped[0]["body"]["reason"] == "step"
    assert adapter._current_ctx is not origin_ctx
    assert adapter._current_scan_id is not None
    assert origin_scan_id is not None
    assert adapter._current_scan_id > origin_scan_id


def test_stepin_walks_chained_builder_substeps_with_friendly_labels_and_trace_lines(
    tmp_path: Path,
):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "chained_debug.py", _chained_builder_debug_script())
    expected_path = os.path.normcase(os.path.normpath(os.path.abspath(str(script))))

    expected = [
        ("Count Up", _line_number(script, "cu_builder = count_up(Counter[1], preset=5)")),
        ("Count Down", _line_number(script, "cu_builder = cu_builder.down(Down)")),
        ("Reset", _line_number(script, "cu_builder.reset(Reset)")),
        (
            "Count Down",
            _line_number(script, "cd_builder = count_down(Counter2, preset=5)"),
        ),
        ("Reset", _line_number(script, "cd_builder.reset(Reset)")),
        (
            "Enable",
            _line_number(script, "timer_builder = on_delay(Timer[1], preset=50)"),
        ),
        ("Reset", _line_number(script, "timer_builder.reset(Reset)")),
        ("Data", _line_number(script, "shift_builder = shift(C.select(1, 3))")),
        ("Clock", _line_number(script, "shift_builder = shift_builder.clock(Clock)")),
        ("Reset", _line_number(script, "shift_builder.reset(Reset)")),
    ]

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    first_messages = _send_request(adapter, out_stream, seq=2, command="stepIn")
    _single_response(first_messages)
    assert adapter._current_step is not None
    assert adapter._current_step.kind == "instruction"
    assert adapter._current_step.instruction_kind == expected[0][0]
    assert adapter._current_step.source_line == expected[0][1]

    stack_messages = _send_request(adapter, out_stream, seq=3, command="stackTrace")
    stack_response = _single_response(stack_messages)
    assert expected[0][0] in stack_response["body"]["stackFrames"][0]["name"]

    first_trace = _trace_events(first_messages)
    assert first_trace
    first_body = first_trace[0]["body"]
    assert first_body["step"]["instructionKind"] == expected[0][0]
    assert first_body["step"]["line"] == expected[0][1]
    assert first_body["step"]["source"]["path"] == expected_path
    assert len(first_body["regions"]) == 1
    assert len(first_body["regions"][0]["conditions"]) == 1
    assert first_body["regions"][0]["conditions"][0]["line"] == expected[0][1]

    seq = 4
    for instruction_kind, source_line in expected[1:]:
        messages = _send_request(adapter, out_stream, seq=seq, command="stepIn")
        seq += 1
        _single_response(messages)
        assert adapter._current_step is not None
        assert adapter._current_step.kind == "instruction"
        assert adapter._current_step.instruction_kind == instruction_kind
        assert adapter._current_step.source_line == source_line

        traces = _trace_events(messages)
        assert traces
        body = traces[0]["body"]
        assert body["step"]["instructionKind"] == instruction_kind
        assert body["step"]["line"] == source_line
        assert len(body["regions"]) == 1
        conditions = body["regions"][0]["conditions"]
        assert len(conditions) == 1
        assert conditions[0]["line"] == source_line


def test_stacktrace_includes_subroutine_context_when_paused_inside_call(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "nested_logic.py", _nested_debug_script())
    sub_rung_line = _line_number(script, "with Rung():")
    expected_path = os.path.normcase(os.path.normpath(os.path.abspath(str(script))))

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=2, command="stepIn")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="stepIn")
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=4, command="stackTrace")
    response = _single_response(messages)
    frames = response["body"]["stackFrames"]
    sub_frame = next(frame for frame in frames if frame["name"] == "Subroutine init_sub")
    assert sub_frame["source"]["path"] == expected_path
    assert sub_frame["line"] == sub_rung_line


def test_stacktrace_includes_subroutine_name_in_instruction_frame(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "nested_logic.py", _nested_debug_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=2, command="stepIn")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="stepIn")
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=4, command="stackTrace")
    response = _single_response(messages)
    frames = response["body"]["stackFrames"]
    assert "OutInstruction" in frames[0]["name"]
    assert "init_sub" in frames[0]["name"]


def test_stacktrace_uses_call_instruction_line_when_paused_on_call(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "nested_logic.py", _nested_debug_script())
    call_line = _line_number(script, "call('init_sub')")

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=2, command="stepIn")
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=3, command="stackTrace")
    response = _single_response(messages)
    frames = response["body"]["stackFrames"]
    assert frames[0]["line"] == call_line
    assert "CallInstruction" in frames[0]["name"]


def test_next_emits_trace_event_with_condition_details(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=2, command="next")
    traces = _trace_events(messages)
    assert traces
    body = traces[0]["body"]
    assert body["traceVersion"] == DAPAdapter.TRACE_VERSION
    assert body["traceSource"] == "live"
    assert body["scanId"] == 1
    assert body["rungId"] == 0
    assert body["step"]["kind"] == "rung"
    assert body["step"]["enabledState"] == "disabled_local"
    assert body["step"]["displayStatus"] == "disabled"
    assert body["step"]["displayText"] == "[OFF] Rung"
    expected_path = os.path.normcase(os.path.normpath(os.path.abspath(str(script))))
    assert body["step"]["source"]["path"] == expected_path
    regions = body["regions"]
    assert regions
    assert regions[0]["enabledState"] == "disabled_local"
    assert regions[0]["source"]["path"] == expected_path
    conditions = regions[0]["conditions"]
    assert conditions
    assert conditions[0]["status"] == "false"
    assert conditions[0]["source"]["path"] == expected_path
    assert isinstance(conditions[0]["summary"], str) and conditions[0]["summary"]
    assert isinstance(conditions[0]["annotation"], str) and conditions[0]["annotation"].startswith(
        "[F]"
    )
    detail_names = {item["name"] for item in conditions[0]["details"]}
    assert {"tag", "value"}.issubset(detail_names)


def test_trace_body_uses_committed_core_event_when_no_inflight_scan_context(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    # First next stops on scan 1 rung without exhausting the generator.
    _send_request(adapter, out_stream, seq=2, command="next")
    _drain_messages(out_stream)

    # Second next crosses StopIteration and starts scan 2.
    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)

    # Drop in-flight scan context so runner.debug.last_event() falls back to committed data.
    if adapter._scan_gen is not None:
        adapter._scan_gen.close()
    adapter._scan_gen = None
    adapter._current_scan_id = None
    adapter._current_step = None
    adapter._current_ctx = None
    adapter._current_rung = None
    adapter._current_rung_index = None

    body = adapter._current_trace_body_locked()
    assert body is not None
    assert body["traceSource"] == "inspect"
    assert body["scanId"] == 1
    assert body["rungId"] == 0
    assert body["step"]["kind"] == "instruction"
    assert body["regions"]


def test_trace_body_with_unsupported_trace_type_returns_empty_regions(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=2, command="next")
    _drain_messages(out_stream)

    assert adapter._runner is not None
    inspect_event = adapter._runner.debug.last_event()
    assert inspect_event is not None
    scan_id, rung_id, event = inspect_event
    adapter._runner._latest_inflight_trace_event = (
        scan_id,
        rung_id,
        RungTraceEvent(
            kind=event.kind,
            source_file=event.source_file,
            source_line=event.source_line,
            end_line=event.end_line,
            subroutine_name=event.subroutine_name,
            depth=event.depth,
            call_stack=event.call_stack,
            enabled_state=event.enabled_state,
            instruction_kind=event.instruction_kind,
            trace="unexpected-trace-type",
        ),
    )

    body = adapter._current_trace_body_locked()
    assert body is not None
    assert body["traceSource"] == "live"
    assert body["regions"] == []


def test_next_trace_formats_composite_conditions_with_operators(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "composite_logic.py", _composite_condition_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=2, command="next")
    traces = _trace_events(messages)
    assert traces
    body = traces[0]["body"]
    conditions = body["regions"][0]["conditions"]
    assert conditions

    expression = str(conditions[0]["expression"])
    assert "|" in expression
    assert "&" in expression
    assert "Or" not in expression
    assert "And" not in expression
    details = {item["name"]: item["value"] for item in conditions[0]["details"]}
    terms = str(details.get("terms", ""))
    assert "(true)" in terms or "(false)" in terms
    assert "=True" not in terms
    assert "=False" not in terms
    assert conditions[0]["annotation"].startswith("[")


def test_next_trace_marks_short_circuited_all_of_child_as_skipped(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "all_of_short_circuit.py", _all_of_short_circuit_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=2, command="next")
    traces = _trace_events(messages)
    assert traces
    condition = traces[0]["body"]["regions"][0]["conditions"][0]
    details = {item["name"]: item["value"] for item in condition["details"]}
    terms = str(details.get("terms", ""))
    assert "Step(0) == 1(false)" in terms
    assert "AutoMode(skipped)" in terms


def test_next_trace_marks_short_circuited_any_of_child_as_skipped(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "any_of_short_circuit.py", _any_of_short_circuit_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=2, command="next")
    traces = _trace_events(messages)
    assert traces
    condition = traces[0]["body"]["regions"][0]["conditions"][0]
    details = {item["name"]: item["value"] for item in condition["details"]}
    terms = str(details.get("terms", ""))
    assert "Step(0) == 0(true)" in terms
    assert "AutoMode(skipped)" in terms


def test_next_trace_any_of_with_rise_term_keeps_skipped_child(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(
        tmp_path, "any_of_rise_short_circuit.py", _any_of_rise_short_circuit_script()
    )

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=2, command="next")
    traces = _trace_events(messages)
    assert traces
    condition = traces[0]["body"]["regions"][0]["conditions"][0]
    details = {item["name"]: item["value"] for item in condition["details"]}
    terms = str(details.get("terms", "")).lower()
    assert "prev(false)" in terms
    assert "automode(skipped)" in terms


def test_next_trace_emits_pointer_condition_details(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "pointer_logic.py", _indirect_condition_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=2, command="next")
    traces = _trace_events(messages)
    assert traces
    condition = traces[0]["body"]["regions"][0]["conditions"][0]
    assert condition["status"] == "false"
    assert condition["expression"] == "Step[CurStep] == DebugStep"

    details = {item["name"]: item["value"] for item in condition["details"]}
    assert details["left"] == "Step[1]"
    assert details["left_value"] == "0"
    assert details["right_value"] == "5"
    assert details["left_pointer_expr"] == "Step[CurStep]"
    assert details["left_pointer"] == "CurStep"
    assert details["left_pointer_value"] == "1"


def test_next_trace_emits_right_pointer_condition_details(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "right_pointer_logic.py", _right_indirect_condition_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=2, command="next")
    traces = _trace_events(messages)
    assert traces
    condition = traces[0]["body"]["regions"][0]["conditions"][0]
    assert condition["status"] == "true"
    assert condition["expression"] == "DebugStep == Step[CurStep]"

    details = {item["name"]: item["value"] for item in condition["details"]}
    assert details["right"] == "Step[1]"
    assert details["right_value"] == "0"
    assert details["right_pointer_expr"] == "Step[CurStep]"
    assert details["right_pointer"] == "CurStep"
    assert details["right_pointer_value"] == "1"


def test_next_trace_collapses_right_pointer_expression_to_resolved_tag(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(
        tmp_path, "right_pointer_expr_logic.py", _right_indirect_expr_condition_script()
    )

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=2, command="next")
    traces = _trace_events(messages)
    assert traces
    condition = traces[0]["body"]["regions"][0]["conditions"][0]
    assert condition["status"] == "false"

    details = {item["name"]: item["value"] for item in condition["details"]}
    assert details["right"] == "Step[5]"
    assert details["right_value"] == "5"
    assert "right_pointer_expr" not in details


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

    assert _wait_for_stop_reason(adapter, out_stream, reason="breakpoint") is True
    assert adapter._current_step is not None
    assert adapter._current_step.kind == "subroutine"
    assert adapter._current_step.subroutine_name == "init_sub"


def test_launch_sets_dap_env_flag_to_skip_script_autorun(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "nested_autorun.py", _nested_debug_autorun_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=2, command="stepIn")
    _drain_messages(out_stream)

    assert adapter._current_step is not None
    assert adapter._current_step.kind == "instruction"


def test_stepin_with_unpowered_branch_stays_on_top_rung(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(
        tmp_path, "branch_unpowered.py", _branch_unpowered_after_first_scan_script()
    )

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
    assert adapter._current_step.kind == "instruction"

    _send_request(adapter, out_stream, seq=4, command="stepIn")
    _drain_messages(out_stream)
    assert adapter._current_step is not None
    assert adapter._current_step.kind == "instruction"


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
    assert adapter._current_step.kind == "instruction"


def test_stepin_skips_branch_and_enters_subroutine(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "branch_then_call.py", _branch_then_call_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=2, command="stepIn")
    _drain_messages(out_stream)
    assert adapter._current_step is not None
    assert adapter._current_step.kind == "instruction"


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

    assert _wait_for_stop_reason(adapter, out_stream, reason="breakpoint") is True


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

    assert _wait_for_stop_reason(adapter, out_stream, reason="pause") is True


def test_continue_pause_cycles_emit_single_pause_stop_per_cycle(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    seq = 2
    for _ in range(3):
        _send_request(adapter, out_stream, seq=seq, command="continue")
        seq += 1
        _drain_messages(out_stream)
        _send_request(adapter, out_stream, seq=seq, command="pause")
        seq += 1
        _drain_messages(out_stream)

        pause_stops = 0
        for _ in range(100):
            flushed = _drain_messages(out_stream)
            for stopped in _stopped_events(flushed):
                if stopped["body"]["reason"] == "pause":
                    pause_stops += 1
            if pause_stops:
                break
            _drain_internal_events_with_wait(adapter)
        assert pause_stops == 1

        # After a pause transition, additional drains should not emit duplicate stops.
        for _ in range(5):
            adapter._drain_internal_events()
            flushed = _drain_messages(out_stream)
            assert _stopped_events(flushed) == []


def test_continue_breakpoint_stop_emits_trace_once(tmp_path: Path):
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

    _send_request(adapter, out_stream, seq=3, command="continue")
    _drain_messages(out_stream)

    breakpoint_stops = 0
    traces = 0
    for _ in range(100):
        flushed = _drain_messages(out_stream)
        for stopped in _stopped_events(flushed):
            if stopped["body"]["reason"] == "breakpoint":
                breakpoint_stops += 1
        traces += len(_trace_events(flushed))
        if breakpoint_stops and traces:
            break
        _drain_internal_events_with_wait(adapter)

    assert breakpoint_stops == 1
    assert traces >= 1

    for _ in range(5):
        adapter._drain_internal_events()
        flushed = _drain_messages(out_stream)
        assert _stopped_events(flushed) == []


def test_variables_overlay_pending_mid_scan_values(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic_two_rung.py", _two_rung_unconditional_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    # First next lands on rung 0 step (before instructions).
    _send_request(adapter, out_stream, seq=2, command="next")
    _drain_messages(out_stream)
    # Second next advances past rung 0 content, landing on rung 1 step.
    # Rung 0's out(light) has now executed; its effect is pending in the scan context.
    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=4,
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
        arguments={"expression": "unforce Button"},
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


def test_evaluate_watch_predicate_expression_returns_boolean_result(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(
        tmp_path, "conditional_logic.py", _conditional_breakpoint_script(button=True)
    )

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="evaluate",
        arguments={"expression": "Button == true", "context": "watch"},
    )
    response = _single_response(messages)
    assert response["success"] is True
    assert response["body"]["result"] == "True"


def test_evaluate_watch_bare_tag_returns_raw_value(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(
        tmp_path,
        "seeded_state.py",
        (
            "from pyrung.core import PLC\n"
            "from pyrung.core.state import SystemState\n"
            "\n"
            "runner = PLC(initial_state=SystemState().with_tags({'Counter': 7}))\n"
        ),
    )

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="evaluate",
        arguments={"expression": "Counter", "context": "watch"},
    )
    response = _single_response(messages)
    assert response["success"] is True
    assert response["body"]["result"] == "7"


def test_evaluate_watch_udt_count_one_qualified_reference_resolves(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(
        tmp_path,
        "udt_state.py",
        (
            "from pyrung.core import Bool, PLC, udt\n"
            "from pyrung.core.state import SystemState\n"
            "\n"
            "@udt()\n"
            "class Tmr:\n"
            "    GreenDone: Bool\n"
            "\n"
            "runner = PLC(initial_state=SystemState().with_tags({'Tmr_GreenDone': True}))\n"
        ),
    )

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="evaluate",
        arguments={"expression": "Tmr.GreenDone == true", "context": "watch"},
    )
    response = _single_response(messages)
    assert response["success"] is True
    assert response["body"]["result"] == "True"


def test_evaluate_watch_indexed_tag_reference_resolves(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(
        tmp_path,
        "indexed_state.py",
        (
            "from pyrung.core import PLC\n"
            "from pyrung.core.state import SystemState\n"
            "\n"
            "runner = PLC(initial_state=SystemState().with_tags({'DS1': 123}))\n"
        ),
    )

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="evaluate",
        arguments={"expression": "DS[1]", "context": "watch"},
    )
    response = _single_response(messages)
    assert response["success"] is True
    assert response["body"]["result"] == "123"


def test_evaluate_watch_struct_instance_reference_resolves_with_unique_suffix(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(
        tmp_path,
        "struct_state.py",
        (
            "from pyrung.core import PLC\n"
            "from pyrung.core.state import SystemState\n"
            "\n"
            "runner = PLC(initial_state=SystemState().with_tags({'Alarm1_id': 42}))\n"
        ),
    )

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="evaluate",
        arguments={"expression": "alarms[1].id", "context": "watch"},
    )
    response = _single_response(messages)
    assert response["success"] is True
    assert response["body"]["result"] == "42"


def test_evaluate_watch_named_array_instance_reference_resolves(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(
        tmp_path,
        "named_array_state.py",
        (
            "from pyrung.core import PLC\n"
            "from pyrung.core.state import SystemState\n"
            "\n"
            "runner = PLC(initial_state=SystemState().with_tags({'SubName1_xCall': 9}))\n"
        ),
    )

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="evaluate",
        arguments={"expression": "SubName[1].xCall", "context": "watch"},
    )
    response = _single_response(messages)
    assert response["success"] is True
    assert response["body"]["result"] == "9"


def test_evaluate_watch_field_index_reference_resolves(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(
        tmp_path,
        "field_index_state.py",
        (
            "from pyrung.core import PLC\n"
            "from pyrung.core.state import SystemState\n"
            "\n"
            "runner = PLC(initial_state=SystemState().with_tags({'SubName1_xCall': 9}))\n"
        ),
    )

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="evaluate",
        arguments={"expression": "SubName.xCall[1]", "context": "watch"},
    )
    response = _single_response(messages)
    assert response["success"] is True
    assert response["body"]["result"] == "9"


def test_evaluate_watch_struct_instance_reference_ambiguous_suffix_errors(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(
        tmp_path,
        "struct_state_ambiguous.py",
        (
            "from pyrung.core import PLC\n"
            "from pyrung.core.state import SystemState\n"
            "\n"
            "runner = PLC(\n"
            "    initial_state=SystemState().with_tags({'Alarm1_id': 42, 'Pump1_id': 84})\n"
            ")\n"
        ),
    )

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="evaluate",
        arguments={"expression": "items[1].id", "context": "watch"},
    )
    response = _single_response(messages)
    assert response["success"] is False
    assert "Unknown tag or memory reference: items[1].id" in response["message"]


def test_evaluate_watch_uses_pending_values_mid_scan(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic_two_rung.py", _two_rung_unconditional_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    # First next lands on rung 0 step (before instructions).
    _send_request(adapter, out_stream, seq=2, command="next")
    _drain_messages(out_stream)
    # Second next advances past rung 0 content, landing on rung 1 step.
    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=4,
        command="evaluate",
        arguments={"expression": "Light", "context": "watch"},
    )
    response = _single_response(messages)
    assert response["success"] is True
    assert response["body"]["result"] == "True"


def test_evaluate_watch_unknown_reference_returns_error(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="evaluate",
        arguments={"expression": "MissingTag", "context": "watch"},
    )
    response = _single_response(messages)
    assert response["success"] is False
    assert "Unknown tag or memory reference: MissingTag" in response["message"]


def test_evaluate_repl_non_command_points_to_watch(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="evaluate",
        arguments={"expression": "Button == true", "context": "repl"},
    )
    response = _single_response(messages)
    assert response["success"] is False
    assert "Use Watch for predicate expressions." in response["message"]


def test_conditional_breakpoint_stops_when_condition_is_true(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(
        tmp_path, "conditional_logic.py", _conditional_breakpoint_script(button=True)
    )
    line = _line_number(script, "out(light)")

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    set_messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="setBreakpoints",
        arguments={
            "source": {"path": str(script)},
            "breakpoints": [{"line": line, "condition": "Button == true"}],
        },
    )
    set_response = _single_response(set_messages)
    assert set_response["body"]["breakpoints"][0]["verified"] is True

    _send_request(adapter, out_stream, seq=3, command="continue")
    _drain_messages(out_stream)

    assert _wait_for_stop_reason(adapter, out_stream, reason="breakpoint") is True


def test_set_breakpoints_with_bad_condition_returns_unverified(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())
    line = _line_number(script, "out(light)")

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="setBreakpoints",
        arguments={
            "source": {"path": str(script)},
            "breakpoints": [{"line": line, "condition": "MotorTemp >>"}],
        },
    )
    response = _single_response(messages)
    breakpoint = response["body"]["breakpoints"][0]
    assert breakpoint["verified"] is False
    assert "Expected literal value" in breakpoint["message"]


def test_snapshot_logpoint_emits_snapshot_event_and_label_lookup(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "monitor_change.py", _monitor_change_script())
    line = _line_number(script, "out(Tick)")

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    _send_request(
        adapter,
        out_stream,
        seq=2,
        command="setBreakpoints",
        arguments={
            "source": {"path": str(script)},
            "breakpoints": [{"line": line, "logMessage": "Snapshot: tick_hit"}],
        },
    )
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=3, command="continue")
    _drain_messages(out_stream)

    saw_snapshot = False
    saw_output = False
    snapshot_event_body: dict[str, Any] | None = None
    for _ in range(100):
        flushed = _drain_messages(out_stream)
        snapshots = _events(flushed, "pyrungSnapshot")
        if snapshots:
            saw_snapshot = True
            snapshot_event_body = snapshots[-1].get("body", {})
        outputs = _events(flushed, "output")
        if any(
            "Snapshot taken: tick_hit" in str(event.get("body", {}).get("output", ""))
            for event in outputs
        ):
            saw_output = True
        if saw_snapshot and saw_output:
            break
        _drain_internal_events_with_wait(adapter)
    assert saw_snapshot is True
    assert saw_output is True
    assert snapshot_event_body is not None
    assert isinstance(snapshot_event_body.get("rtcIso"), str)
    assert isinstance(snapshot_event_body.get("rtcOffsetSeconds"), (int, float))

    _send_request(adapter, out_stream, seq=4, command="pause")
    _drain_messages(out_stream)
    assert _wait_for_stop_reason(adapter, out_stream, reason="pause") is True

    messages = _send_request(
        adapter,
        out_stream,
        seq=5,
        command="pyrungFindLabel",
        arguments={"label": "tick_hit"},
    )
    response = _single_response(messages)
    matches = response["body"]["matches"]
    assert matches
    assert matches[0]["scanId"] >= 1
    assert isinstance(matches[0]["rtcIso"], str)
    assert isinstance(matches[0]["rtcOffsetSeconds"], (int, float))


def test_snapshot_logpoint_labels_active_scan(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "snapshot_once.py", _snapshot_once_script())
    line = _line_number(script, "out(Tick)")

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    _send_request(
        adapter,
        out_stream,
        seq=2,
        command="setBreakpoints",
        arguments={
            "source": {"path": str(script)},
            "breakpoints": [
                {
                    "line": line,
                    "condition": "Tick == false",
                    "logMessage": "Snapshot: tick_once",
                }
            ],
        },
    )
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=3, command="continue")
    _drain_messages(out_stream)

    snapshot_event = _wait_for_event(adapter, out_stream, event_name="pyrungSnapshot")
    snapshot_scan_id = int(snapshot_event["body"]["scanId"]) if snapshot_event is not None else None
    assert snapshot_scan_id is not None

    _send_request(adapter, out_stream, seq=4, command="pause")
    _drain_messages(out_stream)
    assert _wait_for_stop_reason(adapter, out_stream, reason="pause") is True

    messages = _send_request(
        adapter,
        out_stream,
        seq=5,
        command="pyrungFindLabel",
        arguments={"label": "tick_once"},
    )
    response = _single_response(messages)
    matches = response["body"]["matches"]
    assert len(matches) == 1
    assert matches[0]["scanId"] == snapshot_scan_id
    assert snapshot_scan_id == 1
    assert isinstance(matches[0]["rtcIso"], str)
    assert isinstance(matches[0]["rtcOffsetSeconds"], (int, float))


def test_plain_logpoint_emits_output_and_execution_continues(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "monitor_change.py", _monitor_change_script())
    line = _line_number(script, "out(Tick)")

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    _send_request(
        adapter,
        out_stream,
        seq=2,
        command="setBreakpoints",
        arguments={
            "source": {"path": str(script)},
            "breakpoints": [{"line": line, "logMessage": "Tick executed"}],
        },
    )
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=3, command="continue")
    _drain_messages(out_stream)

    output_event = _wait_for_event(
        adapter,
        out_stream,
        event_name="output",
        predicate=lambda event: "Tick executed" in str(event.get("body", {}).get("output", "")),
    )
    assert output_event is not None

    _send_request(adapter, out_stream, seq=4, command="pause")
    _drain_messages(out_stream)
    assert _wait_for_stop_reason(adapter, out_stream, reason="pause") is True


def test_source_breakpoint_hit_condition_triggers_on_every_nth_hit(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "monitor_change.py", _monitor_change_script())
    line = _line_number(script, "out(Tick)")

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="setBreakpoints",
        arguments={
            "source": {"path": str(script)},
            "breakpoints": [{"line": line, "hitCondition": "2"}],
        },
    )
    response = _single_response(messages)
    assert response["body"]["breakpoints"][0]["verified"] is True

    _send_request(adapter, out_stream, seq=3, command="continue")
    _drain_messages(out_stream)
    assert _wait_for_stop_reason(adapter, out_stream, reason="breakpoint") is True
    first_scan = adapter._current_scan_id
    assert first_scan is not None

    _send_request(adapter, out_stream, seq=4, command="continue")
    _drain_messages(out_stream)
    assert _wait_for_stop_reason(adapter, out_stream, reason="breakpoint") is True
    second_scan = adapter._current_scan_id
    assert second_scan is not None
    assert second_scan == first_scan + 2


def test_step_next_emits_plain_logpoint_output(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "monitor_change.py", _monitor_change_script())
    line = _line_number(script, "out(Tick)")

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    _send_request(
        adapter,
        out_stream,
        seq=2,
        command="setBreakpoints",
        arguments={
            "source": {"path": str(script)},
            "breakpoints": [{"line": line, "logMessage": "step log"}],
        },
    )
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)

    output_event = _wait_for_event(
        adapter,
        out_stream,
        event_name="output",
        predicate=lambda event: "step log" in str(event.get("body", {}).get("output", "")),
    )
    assert output_event is not None


def test_step_next_emits_snapshot_event_and_labels_history(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "monitor_change.py", _monitor_change_script())
    line = _line_number(script, "out(Tick)")

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    _send_request(
        adapter,
        out_stream,
        seq=2,
        command="setBreakpoints",
        arguments={
            "source": {"path": str(script)},
            "breakpoints": [{"line": line, "logMessage": "Snapshot: step_snapshot"}],
        },
    )
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=4, command="next")
    _drain_messages(out_stream)

    snapshot_event = _wait_for_event(
        adapter,
        out_stream,
        event_name="pyrungSnapshot",
        predicate=lambda event: event.get("body", {}).get("label") == "step_snapshot",
    )
    assert snapshot_event is not None
    snapshot_scan_id = int(snapshot_event["body"]["scanId"])
    assert isinstance(snapshot_event["body"]["rtcIso"], str)
    assert isinstance(snapshot_event["body"]["rtcOffsetSeconds"], (int, float))

    messages = _send_request(
        adapter,
        out_stream,
        seq=5,
        command="pyrungFindLabel",
        arguments={"label": "step_snapshot"},
    )
    response = _single_response(messages)
    matches = response["body"]["matches"]
    assert matches
    assert int(matches[0]["scanId"]) == snapshot_scan_id
    assert isinstance(matches[0]["rtcIso"], str)
    assert isinstance(matches[0]["rtcOffsetSeconds"], (int, float))


def test_next_with_source_breakpoint_reports_step_reason(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "monitor_change.py", _monitor_change_script())
    line = _line_number(script, "out(Tick)")

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    _send_request(
        adapter,
        out_stream,
        seq=2,
        command="setBreakpoints",
        arguments={"source": {"path": str(script)}, "breakpoints": [{"line": line}]},
    )
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=3, command="next")
    stops = _stopped_events(messages)
    assert stops
    assert stops[0]["body"]["reason"] == "step"


def test_monitor_scope_and_variables_requests(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(
        tmp_path, "conditional_logic.py", _conditional_breakpoint_script(button=True)
    )

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    add_messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="pyrungAddMonitor",
        arguments={"tag": "Button"},
    )
    add_response = _single_response(add_messages)
    monitor_id = add_response["body"]["id"]
    assert add_response["body"]["tag"] == "Button"

    scope_messages = _send_request(adapter, out_stream, seq=3, command="scopes")
    scope_response = _single_response(scope_messages)
    scopes = scope_response["body"]["scopes"]
    monitor_scope = next(scope for scope in scopes if scope["name"] == "PLC Monitors")

    variable_messages = _send_request(
        adapter,
        out_stream,
        seq=4,
        command="variables",
        arguments={"variablesReference": monitor_scope["variablesReference"]},
    )
    variable_response = _single_response(variable_messages)
    monitor_values = {
        entry["name"]: entry["value"] for entry in variable_response["body"]["variables"]
    }
    assert monitor_values["Button"] == "True"

    remove_messages = _send_request(
        adapter,
        out_stream,
        seq=5,
        command="pyrungRemoveMonitor",
        arguments={"id": monitor_id},
    )
    remove_response = _single_response(remove_messages)
    assert remove_response["body"]["removed"] is True


def test_data_breakpoint_info_and_stop_on_change(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "monitor_change.py", _monitor_change_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    _send_request(
        adapter,
        out_stream,
        seq=2,
        command="pyrungAddMonitor",
        arguments={"tag": "Tick"},
    )
    _drain_messages(out_stream)

    info_messages = _send_request(
        adapter,
        out_stream,
        seq=3,
        command="dataBreakpointInfo",
        arguments={"variablesReference": adapter.MONITORS_SCOPE_REF, "name": "Tick"},
    )
    info_response = _single_response(info_messages)
    data_id = info_response["body"]["dataId"]
    assert data_id == "tag:Tick"

    set_data_messages = _send_request(
        adapter,
        out_stream,
        seq=4,
        command="setDataBreakpoints",
        arguments={"breakpoints": [{"dataId": data_id}]},
    )
    set_data_response = _single_response(set_data_messages)
    assert set_data_response["body"]["breakpoints"][0]["verified"] is True

    _send_request(adapter, out_stream, seq=5, command="continue")
    _drain_messages(out_stream)

    assert _wait_for_stop_reason(adapter, out_stream, reason="data breakpoint") is True


def test_set_data_breakpoints_preserves_response_order(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "monitor_change.py", _monitor_change_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    _send_request(
        adapter,
        out_stream,
        seq=2,
        command="pyrungAddMonitor",
        arguments={"tag": "Tick"},
    )
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=3,
        command="setDataBreakpoints",
        arguments={
            "breakpoints": [
                {"dataId": "tag:Tick"},
                {"dataId": "   "},
            ]
        },
    )
    response = _single_response(messages)
    breakpoints = response["body"]["breakpoints"]
    assert len(breakpoints) == 2
    assert breakpoints[0]["verified"] is True
    assert breakpoints[1]["verified"] is False
    assert breakpoints[1]["message"] == "dataId is required"


def test_data_breakpoint_hit_condition_triggers_on_every_nth_change(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "counter_change.py", _counter_change_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    _send_request(
        adapter,
        out_stream,
        seq=2,
        command="pyrungAddMonitor",
        arguments={"tag": "Counter"},
    )
    _drain_messages(out_stream)

    set_messages = _send_request(
        adapter,
        out_stream,
        seq=3,
        command="setDataBreakpoints",
        arguments={"breakpoints": [{"dataId": "tag:Counter", "hitCondition": "2"}]},
    )
    set_response = _single_response(set_messages)
    assert set_response["body"]["breakpoints"][0]["verified"] is True

    _send_request(adapter, out_stream, seq=4, command="continue")
    _drain_messages(out_stream)
    assert _wait_for_stop_reason(adapter, out_stream, reason="data breakpoint") is True
    first_scan = adapter._runner.current_state.scan_id if adapter._runner is not None else None
    assert first_scan is not None

    _send_request(adapter, out_stream, seq=5, command="continue")
    _drain_messages(out_stream)
    assert _wait_for_stop_reason(adapter, out_stream, reason="data breakpoint") is True
    second_scan = adapter._runner.current_state.scan_id if adapter._runner is not None else None
    assert second_scan is not None
    assert second_scan == first_scan + 2


def test_monitor_callback_emits_custom_event(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "monitor_change.py", _monitor_change_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    _send_request(
        adapter,
        out_stream,
        seq=2,
        command="pyrungAddMonitor",
        arguments={"tag": "Tick"},
    )
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=3, command="continue")
    _drain_messages(out_stream)

    monitor_event = _wait_for_event(adapter, out_stream, event_name="pyrungMonitor")
    assert monitor_event is not None
    event_body = monitor_event["body"]
    assert event_body["tag"] == "Tick"

    _send_request(adapter, out_stream, seq=4, command="pause")
    _drain_messages(out_stream)
    assert _wait_for_stop_reason(adapter, out_stream, reason="pause") is True


def test_shutdown_clears_monitor_and_data_breakpoint_registrations(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "monitor_change.py", _monitor_change_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    _send_request(
        adapter,
        out_stream,
        seq=2,
        command="pyrungAddMonitor",
        arguments={"tag": "Tick"},
    )
    _drain_messages(out_stream)
    _send_request(
        adapter,
        out_stream,
        seq=3,
        command="setDataBreakpoints",
        arguments={"breakpoints": [{"dataId": "tag:Tick"}]},
    )
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=4, command="terminate")
    _drain_messages(out_stream)

    assert adapter._monitor_handles == {}
    assert adapter._monitor_meta == {}
    assert adapter._monitor_values == {}
    assert adapter._data_bp_handles == {}
    assert adapter._data_bp_meta == {}


# ---------------------------------------------------------------------------
# pyrungHistoryInfo / pyrungSeek
# ---------------------------------------------------------------------------


def test_pyrung_history_info_returns_range_at_entry(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=2, command="pyrungHistoryInfo")
    response = _single_response(messages)
    assert response["success"] is True
    body = response["body"]
    assert body["minScanId"] == 0
    assert body["maxScanId"] == 0
    assert body["playhead"] == 0
    assert body["count"] == 1


def test_pyrung_history_info_grows_after_stepping(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=2, command="pyrungStepScan")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="pyrungStepScan")
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=4, command="pyrungHistoryInfo")
    response = _single_response(messages)
    body = response["body"]
    assert body["minScanId"] == 0
    assert body["maxScanId"] > 0
    assert body["count"] > 1


def test_pyrung_seek_returns_tags_at_scan(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _unconditional_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    # Step a couple of scans so history accumulates.
    _send_request(adapter, out_stream, seq=2, command="pyrungStepScan")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="pyrungStepScan")
    _drain_messages(out_stream)

    # Seek back to scan 0.
    messages = _send_request(
        adapter, out_stream, seq=4, command="pyrungSeek", arguments={"scanId": 0}
    )
    response = _single_response(messages)
    assert response["success"] is True
    assert response["body"]["scanId"] == 0
    assert "tags" in response["body"]


def test_pyrung_seek_requires_integer_scan_id(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter, out_stream, seq=2, command="pyrungSeek", arguments={"scanId": "abc"}
    )
    response = _single_response(messages)
    assert response["success"] is False
    assert "integer" in response["message"]


def test_pyrung_seek_rejects_evicted_scan(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter, out_stream, seq=2, command="pyrungSeek", arguments={"scanId": 999}
    )
    response = _single_response(messages)
    assert response["success"] is False
    assert "not retained" in response["message"]


def test_pyrung_seek_updates_playhead(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _unconditional_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=2, command="pyrungStepScan")
    _drain_messages(out_stream)

    # Playhead should be at the latest scan after stepping.
    messages = _send_request(adapter, out_stream, seq=3, command="pyrungHistoryInfo")
    info_after_step = _single_response(messages)["body"]
    assert info_after_step["playhead"] == info_after_step["maxScanId"]

    # Seek to scan 0.
    _send_request(adapter, out_stream, seq=4, command="pyrungSeek", arguments={"scanId": 0})
    _drain_messages(out_stream)

    # Playhead should now be 0.
    messages = _send_request(adapter, out_stream, seq=5, command="pyrungHistoryInfo")
    info_after_seek = _single_response(messages)["body"]
    assert info_after_seek["playhead"] == 0


def test_pyrung_tag_changes_returns_only_matching_scans_and_supports_pagination(
    tmp_path: Path,
):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "history_changes.py", _history_changes_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    for seq in range(2, 7):
        _send_request(adapter, out_stream, seq=seq, command="pyrungStepScan")
        _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=7,
        command="pyrungTagChanges",
        arguments={"tags": ["State", "Running"], "count": 2},
    )
    response = _single_response(messages)
    assert response["success"] is True
    entries = response["body"]["entries"]
    assert [entry["scanId"] for entry in entries] == [5, 3]
    assert [entry["prevScanId"] for entry in entries] == [4, 2]
    assert entries[0]["changes"] == {"Running": ["True", "False"]}
    assert entries[1]["changes"] == {"State": ["1", "2"]}

    messages = _send_request(
        adapter,
        out_stream,
        seq=8,
        command="pyrungTagChanges",
        arguments={"tags": ["State", "Running"], "count": 2, "beforeScan": 3},
    )
    response = _single_response(messages)
    assert response["success"] is True
    entries = response["body"]["entries"]
    assert [entry["scanId"] for entry in entries] == [1]
    assert [entry["prevScanId"] for entry in entries] == [0]
    assert entries[0]["changes"] == {
        "State": ["0", "1"],
        "Running": ["False", "True"],
    }

    # afterScan: only entries newer than scan 3
    messages = _send_request(
        adapter,
        out_stream,
        seq=9,
        command="pyrungTagChanges",
        arguments={"tags": ["State", "Running"], "count": 50, "afterScan": 3},
    )
    response = _single_response(messages)
    assert response["success"] is True
    entries = response["body"]["entries"]
    assert [entry["scanId"] for entry in entries] == [5]
    assert entries[0]["changes"] == {"Running": ["True", "False"]}

    # afterScan + beforeScan: window between scan 1 and scan 5
    messages = _send_request(
        adapter,
        out_stream,
        seq=10,
        command="pyrungTagChanges",
        arguments={
            "tags": ["State", "Running"],
            "count": 50,
            "afterScan": 1,
            "beforeScan": 5,
        },
    )
    response = _single_response(messages)
    assert response["success"] is True
    entries = response["body"]["entries"]
    assert [entry["scanId"] for entry in entries] == [3]
    assert entries[0]["changes"] == {"State": ["1", "2"]}

    # afterScan at latest: no new entries
    messages = _send_request(
        adapter,
        out_stream,
        seq=11,
        command="pyrungTagChanges",
        arguments={"tags": ["State", "Running"], "count": 50, "afterScan": 5},
    )
    response = _single_response(messages)
    assert response["success"] is True
    assert response["body"]["entries"] == []


def test_pyrung_fork_at_replaces_runner_and_can_step_from_branch(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "history_changes.py", _history_changes_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    for seq in range(2, 7):
        _send_request(adapter, out_stream, seq=seq, command="pyrungStepScan")
        _drain_messages(out_stream)

    _send_request(
        adapter,
        out_stream,
        seq=7,
        command="pyrungAddMonitor",
        arguments={"tag": "Running"},
    )
    _drain_messages(out_stream)
    _send_request(
        adapter,
        out_stream,
        seq=8,
        command="setDataBreakpoints",
        arguments={"breakpoints": [{"dataId": "tag:Running"}]},
    )
    _drain_messages(out_stream)
    assert adapter._monitor_handles
    assert adapter._data_bp_handles

    messages = _send_request(
        adapter,
        out_stream,
        seq=9,
        command="pyrungForkAt",
        arguments={"scanId": 3},
    )
    response = _single_response(messages)
    assert response["success"] is True
    assert response["body"]["scanId"] == 3
    assert adapter._runner is not None
    assert response["body"]["timestamp"] == adapter._runner.current_state.timestamp
    assert adapter._runner.current_state.scan_id == 3
    assert _stopped_events(messages)[0]["body"]["reason"] == "pause"
    assert adapter._monitor_handles == {}
    assert adapter._monitor_meta == {}
    assert adapter._monitor_values == {}
    assert adapter._data_bp_handles == {}
    assert adapter._data_bp_meta == {}
    assert adapter._scan_gen is None
    assert adapter._current_step is None

    _send_request(adapter, out_stream, seq=10, command="pyrungStepScan")
    _drain_messages(out_stream)
    assert adapter._runner.current_state.scan_id == 4
    assert adapter._runner.current_state.tags["StepCount"] == 4
    assert adapter._runner.current_state.tags["State"] == 2
    assert adapter._runner.current_state.tags["Running"] is True

    _send_request(adapter, out_stream, seq=11, command="pyrungStepScan")
    _drain_messages(out_stream)
    assert adapter._runner.current_state.scan_id == 5
    assert adapter._runner.current_state.tags["StepCount"] == 5
    assert adapter._runner.current_state.tags["Running"] is False


# ---------------------------------------------------------------------------
# Live trace emission during continue
# ---------------------------------------------------------------------------


def test_continue_emits_live_traces_with_step_none(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=3, command="continue")
    _drain_messages(out_stream)

    # Let the continue thread complete some scans before pausing.
    time.sleep(0.05)
    _send_request(adapter, out_stream, seq=4, command="pause")
    _drain_messages(out_stream)

    # Collect all messages until the pause stop.
    live_traces: list[dict[str, Any]] = []
    on_stop_traces: list[dict[str, Any]] = []
    stopped = False
    for _ in range(200):
        _drain_internal_events_with_wait(adapter)
        flushed = _drain_messages(out_stream)
        live_traces.extend(
            t for t in _trace_events(flushed) if t.get("body", {}).get("step") is None
        )
        on_stop_traces.extend(
            t for t in _trace_events(flushed) if t.get("body", {}).get("step") is not None
        )
        if _stopped_events(flushed):
            stopped = True
            break

    assert stopped, "Expected pause stop"
    assert len(live_traces) >= 1, "Expected at least one live trace with step=None"
    for trace in live_traces:
        assert trace["body"]["step"] is None
    assert len(on_stop_traces) >= 1, "Expected on-stop trace with step populated"


def test_continue_live_trace_requires_configuration_done(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    # Deliberately omit configurationDone.

    _send_request(adapter, out_stream, seq=2, command="continue")
    _drain_messages(out_stream)

    # Let the continue thread run — scans complete, but no live traces should emit.
    time.sleep(0.05)
    _send_request(adapter, out_stream, seq=3, command="pause")
    _drain_messages(out_stream)

    live_traces: list[dict[str, Any]] = []
    for _ in range(200):
        _drain_internal_events_with_wait(adapter)
        flushed = _drain_messages(out_stream)
        live_traces.extend(
            t for t in _trace_events(flushed) if t.get("body", {}).get("step") is None
        )
        if _stopped_events(flushed):
            break

    assert live_traces == [], "No live traces should emit without configurationDone"


def test_continue_live_trace_not_emitted_during_step(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)

    # Perform several step-next commands.
    for seq_num in range(3, 8):
        messages = _send_request(adapter, out_stream, seq=seq_num, command="next")
        traces = _trace_events(messages)
        for trace in traces:
            assert trace["body"]["step"] is not None, (
                "Step commands should never emit traces with step=None"
            )


# ---------------------------------------------------------------------------
# pyrungForce / pyrungUnforce / pyrungClearForces / pyrungPatch / pyrungListForces
# ---------------------------------------------------------------------------


def test_pyrung_force_sets_force_and_appears_in_trace(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)

    # Step to get first stopped state
    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)

    # Force a tag via structured request
    messages = _send_request(
        adapter,
        out_stream,
        seq=4,
        command="pyrungForce",
        arguments={"tag": "Button", "value": True},
    )
    response = _single_response(messages)
    assert response["success"] is True

    # Runner should reflect the force
    assert adapter._runner.forces["Button"] is True

    # Step and check trace has forces field
    messages = _send_request(adapter, out_stream, seq=5, command="next")
    traces = _trace_events(messages)
    assert traces
    assert traces[0]["body"]["forces"] == {"Button": True}


def test_pyrung_unforce_removes_force(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)

    # Force then unforce
    _send_request(
        adapter,
        out_stream,
        seq=4,
        command="pyrungForce",
        arguments={"tag": "Button", "value": True},
    )
    messages = _send_request(
        adapter, out_stream, seq=5, command="pyrungUnforce", arguments={"tag": "Button"}
    )
    response = _single_response(messages)
    assert response["success"] is True
    assert "Button" not in adapter._runner.forces


def test_pyrung_clear_forces_removes_all(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)

    _send_request(
        adapter,
        out_stream,
        seq=4,
        command="pyrungForce",
        arguments={"tag": "Button", "value": True},
    )
    _send_request(
        adapter, out_stream, seq=5, command="pyrungForce", arguments={"tag": "Light", "value": True}
    )

    messages = _send_request(adapter, out_stream, seq=6, command="pyrungClearForces")
    response = _single_response(messages)
    assert response["success"] is True
    assert dict(adapter._runner.forces) == {}


def test_pyrung_patch_sets_one_shot_value(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=4,
        command="pyrungPatch",
        arguments={"tag": "Button", "value": True},
    )
    response = _single_response(messages)
    assert response["success"] is True

    # Patch does NOT appear in forces
    assert "Button" not in adapter._runner.forces


def test_pyrung_list_forces_returns_current_map(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)

    # Empty before any forces
    messages = _send_request(adapter, out_stream, seq=3, command="pyrungListForces")
    response = _single_response(messages)
    assert response["success"] is True
    assert response["body"]["forces"] == {}

    _send_request(adapter, out_stream, seq=4, command="next")
    _drain_messages(out_stream)

    # Add a force and re-query
    _send_request(
        adapter,
        out_stream,
        seq=5,
        command="pyrungForce",
        arguments={"tag": "Button", "value": True},
    )
    messages = _send_request(adapter, out_stream, seq=6, command="pyrungListForces")
    response = _single_response(messages)
    assert response["body"]["forces"] == {"Button": True}


def test_pyrung_force_requires_tag(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter, out_stream, seq=2, command="pyrungForce", arguments={"tag": "", "value": True}
    )
    response = _single_response(messages)
    assert response["success"] is False
    assert "tag is required" in response["message"]


def test_pyrung_force_requires_bool_number_or_string_value(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="pyrungForce",
        arguments={"tag": "Button", "value": ["true"]},
    )
    response = _single_response(messages)
    assert response["success"] is False
    assert "must be a bool, number, or string" in response["message"]


def test_pyrung_force_accepts_string_value(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _char_runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="pyrungForce",
        arguments={"tag": "Mode", "value": "Z"},
    )
    response = _single_response(messages)
    assert response["success"] is True
    assert adapter._runner.forces["Mode"] == "Z"


def test_pyrung_unforce_unknown_tag_succeeds_idempotent(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter, out_stream, seq=2, command="pyrungUnforce", arguments={"tag": "Nonexistent"}
    )
    response = _single_response(messages)
    assert response["success"] is True


def test_pyrung_unforce_requires_tag(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter, out_stream, seq=2, command="pyrungUnforce", arguments={"tag": "  "}
    )
    response = _single_response(messages)
    assert response["success"] is False
    assert "tag is required" in response["message"]


def test_pyrung_patch_requires_tag(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter, out_stream, seq=2, command="pyrungPatch", arguments={"tag": "", "value": 1}
    )
    response = _single_response(messages)
    assert response["success"] is False
    assert "tag is required" in response["message"]


def test_pyrung_patch_requires_bool_number_or_string_value(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter, out_stream, seq=2, command="pyrungPatch", arguments={"tag": "Button", "value": {}}
    )
    response = _single_response(messages)
    assert response["success"] is False
    assert "must be a bool, number, or string" in response["message"]


def test_pyrung_patch_batch_applies_multiple_tags(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=4,
        command="pyrungPatch",
        arguments={"patches": {"Button": True, "Light": True}},
    )
    response = _single_response(messages)
    assert response["success"] is True

    # Patches do NOT appear in forces
    assert "Button" not in adapter._runner.forces
    assert "Light" not in adapter._runner.forces


def test_pyrung_patch_batch_empty_raises_error(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter, out_stream, seq=2, command="pyrungPatch", arguments={"patches": {}}
    )
    response = _single_response(messages)
    assert response["success"] is False
    assert "must not be empty" in response["message"]


def test_pyrung_patch_batch_invalid_value_type_raises_error(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="pyrungPatch",
        arguments={"patches": {"Button": ["yes"]}},
    )
    response = _single_response(messages)
    assert response["success"] is False
    assert "must be a bool, number, or string" in response["message"]


def test_pyrung_patch_batch_not_a_dict_raises_error(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter, out_stream, seq=2, command="pyrungPatch", arguments={"patches": [1, 2]}
    )
    response = _single_response(messages)
    assert response["success"] is False
    assert "must be an object" in response["message"]


def test_trace_forces_field_empty_when_no_forces(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=3, command="next")
    traces = _trace_events(messages)
    assert traces
    assert traces[0]["body"]["forces"] == {}


def test_trace_forces_field_present_after_force_and_unforce(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)

    # Force Button
    _send_request(
        adapter,
        out_stream,
        seq=4,
        command="pyrungForce",
        arguments={"tag": "Button", "value": True},
    )

    messages = _send_request(adapter, out_stream, seq=5, command="next")
    traces = _trace_events(messages)
    assert traces
    assert traces[0]["body"]["forces"] == {"Button": True}

    # Unforce Button
    _send_request(adapter, out_stream, seq=6, command="pyrungUnforce", arguments={"tag": "Button"})

    messages = _send_request(adapter, out_stream, seq=7, command="next")
    traces = _trace_events(messages)
    assert traces
    assert traces[0]["body"]["forces"] == {}


def test_trace_tag_values_present_on_step(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=3, command="next")
    traces = _trace_events(messages)
    assert traces
    body = traces[0]["body"]
    assert "tagValues" in body
    tag_values = body["tagValues"]
    assert isinstance(tag_values, dict)
    assert "Button" in tag_values
    assert "Light" in tag_values


def test_trace_tag_values_reflect_forced_state(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)

    # Force Button to True
    _send_request(
        adapter,
        out_stream,
        seq=4,
        command="pyrungForce",
        arguments={"tag": "Button", "value": True},
    )

    messages = _send_request(adapter, out_stream, seq=5, command="next")
    traces = _trace_events(messages)
    assert traces
    tag_values = traces[0]["body"]["tagValues"]
    # Forced value should be reflected in tagValues
    assert tag_values["Button"] == "True"


def test_trace_tag_hints_present_for_choice_and_readonly_tags(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _tag_hints_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=3, command="next")
    traces = _trace_events(messages)
    assert traces
    body = traces[0]["body"]
    assert body["tagHints"] == {"ModeTag": {"choices": {"0": "IDLE", "1": "RUN"}, "readonly": True}}


def test_live_trace_body_includes_tag_hints(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _tag_hints_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=4, command="next")
    _drain_messages(out_stream)

    with adapter._state_lock:
        body = adapter._live_trace_body_locked()

    assert body is not None
    assert body["tagHints"] == {"ModeTag": {"choices": {"0": "IDLE", "1": "RUN"}, "readonly": True}}


def test_trace_tag_groups_for_structured_tags(tmp_path: Path):
    """tagGroups maps structure name to its member tag names."""
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _chained_builder_debug_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=3, command="next")
    traces = _trace_events(messages)
    assert traces
    body = traces[0]["body"]
    assert "tagGroups" in body
    tag_groups = body["tagGroups"]
    assert isinstance(tag_groups, dict)

    # Counter[1] and Timer[1] are structured UDTs — they should produce groups
    assert "Counter" in tag_groups
    assert "Timer" in tag_groups
    assert "Counter2" in tag_groups

    # Each group should list its member tag names (Done, Acc, etc.)
    counter_members = tag_groups["Counter"]
    assert isinstance(counter_members, list)
    assert len(counter_members) >= 2  # at least Done + Acc
    assert any("Done" in m for m in counter_members)
    assert any("Acc" in m for m in counter_members)


def test_trace_tag_groups_empty_for_plain_tags(tmp_path: Path):
    """Programs with only plain tags should have an empty tagGroups."""
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=3, command="next")
    traces = _trace_events(messages)
    assert traces
    body = traces[0]["body"]
    assert body["tagGroups"] == {}
    assert body["tagHints"] == {}


def test_existing_debug_console_force_commands_still_work(tmp_path: Path):
    """Ensure the Debug Console text commands are unaffected."""
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)

    # Debug Console: force
    messages = _send_request(
        adapter,
        out_stream,
        seq=4,
        command="evaluate",
        arguments={"expression": "force Button true", "context": "repl"},
    )
    response = _single_response(messages)
    assert response["success"] is True
    assert adapter._runner.forces["Button"] is True

    # Debug Console: unforce
    messages = _send_request(
        adapter,
        out_stream,
        seq=5,
        command="evaluate",
        arguments={"expression": "unforce Button", "context": "repl"},
    )
    response = _single_response(messages)
    assert response["success"] is True
    assert "Button" not in adapter._runner.forces

    # Debug Console: clear_forces
    _send_request(
        adapter,
        out_stream,
        seq=6,
        command="evaluate",
        arguments={"expression": "force Button true", "context": "repl"},
    )
    messages = _send_request(
        adapter,
        out_stream,
        seq=7,
        command="evaluate",
        arguments={"expression": "clear_forces", "context": "repl"},
    )
    response = _single_response(messages)
    assert response["success"] is True
    assert dict(adapter._runner.forces) == {}


def test_pyrung_force_read_only_tag_returns_error(tmp_path: Path):
    script_content = (
        "from pyrung.core import Bool, Int, PLC, Program, Rung, out\n"
        "\n"
        "button = Bool('Button')\n"
        "light = Bool('Light')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(button):\n"
        "        out(light)\n"
        "\n"
        "runner = PLC(prog)\n"
    )
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", script_content)

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)

    # Try to force sys.always_on which is a read-only system point
    messages = _send_request(
        adapter,
        out_stream,
        seq=4,
        command="pyrungForce",
        arguments={"tag": "sys.always_on", "value": False},
    )
    response = _single_response(messages)
    assert response["success"] is False
    assert "cannot force:" in response["message"]


def test_pyrung_force_numeric_value_types(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _counter_change_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)

    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)

    # Force with int
    messages = _send_request(
        adapter, out_stream, seq=4, command="pyrungForce", arguments={"tag": "Counter", "value": 99}
    )
    response = _single_response(messages)
    assert response["success"] is True
    assert adapter._runner.forces["Counter"] == 99

    # Force with float
    messages = _send_request(
        adapter,
        out_stream,
        seq=5,
        command="pyrungForce",
        arguments={"tag": "Counter", "value": 3.14},
    )
    response = _single_response(messages)
    assert response["success"] is True
    assert adapter._runner.forces["Counter"] == 3.14


# ------------------------------------------------------------------
# pyrungGraph / pyrungSlice
# ------------------------------------------------------------------


def _graph_script() -> str:
    return (
        "from pyrung.core import Bool, PLC, Program, Rung, out, latch\n"
        "\n"
        "button = Bool('Button')\n"
        "relay = Bool('Relay')\n"
        "light = Bool('Light')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(button):\n"
        "        latch(relay)\n"
        "    with Rung(relay):\n"
        "        out(light)\n"
        "\n"
        "runner = PLC(prog)\n"
    )


def test_pyrung_graph_returns_valid_json(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _graph_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(adapter, out_stream, seq=2, command="pyrungGraph")
    response = _single_response(messages)
    assert response["success"] is True

    body = response["body"]
    assert isinstance(body["rungNodes"], list)
    assert isinstance(body["tagRoles"], dict)
    assert isinstance(body["graphEdges"], list)
    assert "Button" in body["tagRoles"]
    assert "Relay" in body["tagRoles"]
    assert "Light" in body["tagRoles"]

    # Check that graph edges are bipartite
    for edge in body["graphEdges"]:
        assert edge["type"] in ("condition", "data", "write")
        if edge["type"] in ("condition", "data"):
            assert not edge["source"].startswith("rung:")
            assert edge["target"].startswith("rung:")
        else:
            assert edge["source"].startswith("rung:")
            assert not edge["target"].startswith("rung:")


def test_pyrung_slice_upstream(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _graph_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="pyrungSlice",
        arguments={"tag": "Light", "direction": "upstream"},
    )
    response = _single_response(messages)
    assert response["success"] is True

    body = response["body"]
    assert "Light" in body["tags"]
    assert "Relay" in body["tags"]
    assert "Button" in body["tags"]


def test_pyrung_slice_downstream(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _graph_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="pyrungSlice",
        arguments={"tag": "Button", "direction": "downstream"},
    )
    response = _single_response(messages)
    assert response["success"] is True

    body = response["body"]
    assert "Button" in body["tags"]
    assert "Relay" in body["tags"]
    assert "Light" in body["tags"]


def test_pyrung_slice_requires_tag(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _graph_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="pyrungSlice",
        arguments={"direction": "upstream"},
    )
    response = _single_response(messages)
    assert response["success"] is False


def test_pyrung_slice_requires_valid_direction(tmp_path: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _graph_script())

    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _drain_messages(out_stream)

    messages = _send_request(
        adapter,
        out_stream,
        seq=2,
        command="pyrungSlice",
        arguments={"tag": "Light", "direction": "sideways"},
    )
    response = _single_response(messages)
    assert response["success"] is False
