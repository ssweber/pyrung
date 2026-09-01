"""Tests for the DAP console command dispatcher."""

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


def _runner_script(*, dt: float = 0.010) -> str:
    return (
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
        f"runner = PLC(prog, dt={dt!r})\n"
    )


def _how_script() -> str:
    return (
        "from pyrung.core import Bool, PLC, Program, Rung, out, latch\n"
        "\n"
        "start = Bool('Start', external=True)\n"
        "running = Bool('Running')\n"
        "done = Bool('Done')\n"
        "\n"
        "with Program() as prog:\n"
        "    with Rung(start):\n"
        "        latch(running)\n"
        "    with Rung(running):\n"
        "        out(done)\n"
        "\n"
        "runner = PLC(prog, dt=0.010)\n"
    )


def _how_multi_avoid_script() -> str:
    """``Filling`` reachable via three OR levers — the union-avoid shape."""
    return (
        "from pyrung.core import Bool, Int, PLC, Program, Rung, Or, copy, out\n"
        "\n"
        "a = Bool('A', external=True)\n"
        "b = Bool('B', external=True)\n"
        "c = Bool('C', external=True)\n"
        "step = Int('Step', default=1)\n"
        "filling = Bool('Filling')\n"
        "\n"
        "with Program() as prog:\n"
        "    with Rung(Or(a, b, c)):\n"
        "        copy(2, step)\n"
        "    with Rung(step == 2):\n"
        "        out(filling)\n"
        "\n"
        "runner = PLC(prog, dt=0.010)\n"
    )


def _setup_how_multi_avoid(tmp_path: Path) -> tuple[DAPAdapter, io.BytesIO]:
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic_multi_avoid.py", _how_multi_avoid_script())
    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)
    return adapter, out_stream


def _compound_script() -> str:
    """Mode change resets the step sequencer — the compound-goal shape."""
    return (
        "from pyrung.core import Bool, Int, PLC, Program, Rung, copy\n"
        "\n"
        "go1 = Bool('Go1', external=True)\n"
        "go2 = Bool('Go2', external=True)\n"
        "mode_btn = Bool('ModeBtn', external=True)\n"
        "mode = Int('Mode')\n"
        "step = Int('Step')\n"
        "\n"
        "with Program() as prog:\n"
        "    with Rung(go1, step == 0):\n"
        "        copy(1, step)\n"
        "    with Rung(go2, step == 1):\n"
        "        copy(2, step)\n"
        "    with Rung(mode_btn, mode == 0):\n"
        "        copy(2, mode)\n"
        "        copy(0, step)\n"
        "\n"
        "runner = PLC(prog, dt=0.010)\n"
    )


def _setup_compound(tmp_path: Path) -> tuple[DAPAdapter, io.BytesIO]:
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic_compound.py", _compound_script())
    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)
    return adapter, out_stream


def _setup_how(tmp_path: Path) -> tuple[DAPAdapter, io.BytesIO]:
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic_how.py", _how_script())
    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)
    return adapter, out_stream


def _setup(tmp_path: Path, *, dt: float = 0.010) -> tuple[DAPAdapter, io.BytesIO]:
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _runner_script(dt=dt))
    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)
    return adapter, out_stream


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


# ---------------------------------------------------------------------------
# Existing verbs (regression)
# ---------------------------------------------------------------------------


class TestForceVerbs:
    def test_force(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "force Button true")
        assert resp["success"] is True
        assert adapter._runner.forces["Button"] is True

    def test_unforce(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "force Button true", seq=10)
        resp, _ = _repl(adapter, out, "unforce Button", seq=11)
        assert resp["success"] is True
        assert "Button" not in adapter._runner.forces

    def test_clear_forces(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "force Button true", seq=10)
        resp, _ = _repl(adapter, out, "clear_forces", seq=11)
        assert resp["success"] is True
        assert dict(adapter._runner.forces) == {}


# ---------------------------------------------------------------------------
# Patch
# ---------------------------------------------------------------------------


class TestPatch:
    def test_patch_sets_value(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "patch Button true")
        assert resp["success"] is True
        assert "Patched" in resp["body"]["result"]

    def test_patch_missing_args(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "patch Button")
        assert resp["success"] is False


# ---------------------------------------------------------------------------
# Step
# ---------------------------------------------------------------------------


class TestStep:
    def test_step_one(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        scan_before = adapter._runner.current_state.scan_id
        resp, stopped = _repl(adapter, out, "step")
        assert resp["success"] is True
        assert "1 scan(s)" in resp["body"]["result"]
        assert adapter._runner.current_state.scan_id == scan_before + 1
        assert len(stopped) >= 1

    def test_step_n(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        scan_before = adapter._runner.current_state.scan_id
        resp, stopped = _repl(adapter, out, "step 5")
        assert resp["success"] is True
        assert "5 scan(s)" in resp["body"]["result"]
        assert adapter._runner.current_state.scan_id == scan_before + 5
        assert len(stopped) >= 1

    def test_step_bad_count(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "step abc")
        assert resp["success"] is False

    def test_step_zero(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "step 0")
        assert resp["success"] is False

    def test_step_emits_stopped_event(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _, stopped = _repl(adapter, out, "step")
        assert len(stopped) >= 1
        assert stopped[0]["body"]["reason"] == "step"


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


class TestRun:
    def test_run_cycles(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        scan_before = adapter._runner.current_state.scan_id
        resp, stopped = _repl(adapter, out, "run 10")
        assert resp["success"] is True
        assert "10 cycle(s)" in resp["body"]["result"]
        assert adapter._runner.current_state.scan_id == scan_before + 10
        assert len(stopped) >= 1

    def test_run_duration(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        ts_before = adapter._runner.current_state.timestamp
        resp, stopped = _repl(adapter, out, "run 100ms")
        assert resp["success"] is True
        assert "scan(s)" in resp["body"]["result"]
        ts_after = adapter._runner.current_state.timestamp
        assert ts_after - ts_before >= 0.099
        assert len(stopped) >= 1

    def test_run_duration_with_split_unit(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        ts_before = adapter._runner.current_state.timestamp
        resp, stopped = _repl(adapter, out, "run 100 ms")
        assert resp["success"] is True
        ts_after = adapter._runner.current_state.timestamp
        assert ts_after - ts_before >= 0.099
        assert len(stopped) >= 1

    def test_run_duration_seconds(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        ts_before = adapter._runner.current_state.timestamp
        resp, _ = _repl(adapter, out, "run 1s")
        assert resp["success"] is True
        ts_after = adapter._runner.current_state.timestamp
        assert ts_after - ts_before >= 0.999

    def test_run_missing_spec(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "run")
        assert resp["success"] is False

    def test_run_bad_spec(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "run foobar")
        assert resp["success"] is False


# ---------------------------------------------------------------------------
# Cause / Effect / Recovers
# ---------------------------------------------------------------------------


class TestCausalVerbs:
    def test_cause_with_transition(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "patch Button true", seq=10)
        _repl(adapter, out, "step", seq=11)
        resp, _ = _repl(adapter, out, "cause Light", seq=12)
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "Light" in result

    def test_cause_no_chain(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "cause Light")
        assert resp["success"] is True
        assert "No causal chain" in resp["body"]["result"]

    def test_cause_missing_tag(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "cause")
        assert resp["success"] is False

    def test_effect(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "patch Button true", seq=10)
        _repl(adapter, out, "step", seq=11)
        resp, _ = _repl(adapter, out, "effect Button", seq=12)
        assert resp["success"] is True

    def test_recovers(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "recovers Light")
        assert resp["success"] is True
        assert "recovers:" in resp["body"]["result"]

    def test_why_single_tag(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "patch Button true", seq=10)
        _repl(adapter, out, "step", seq=11)
        resp, _ = _repl(adapter, out, "why Light", seq=12)
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "Light" in result
        assert "why" in result

    def test_why_missing_tag(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "why")
        assert resp["success"] is False

    def test_why_multi_tag(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "patch Button true", seq=10)
        _repl(adapter, out, "step", seq=11)
        resp, _ = _repl(adapter, out, "why Light Button", seq=12)
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "Light" in result

    def test_how_single_tag(self, tmp_path: Path):
        adapter, out = _setup_how(tmp_path)
        resp, _ = _repl(adapter, out, "how Running", seq=10)
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "Reached" in result or "Cannot reach" in result or "Stopped" in result

    def test_how_missing_tag(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "how")
        assert resp["success"] is False

    def test_how_multi_tag(self, tmp_path: Path):
        """Comma-separated targets are a conjunction: both must hold at the end."""
        adapter, out = _setup_how(tmp_path)
        resp, _ = _repl(adapter, out, "how Running, Done", seq=10)
        assert resp["success"] is True, resp
        result = resp["body"]["result"]
        assert "Running" in result and "Done" in result, result
        assert "Reached" in result, result

    def test_how_avoid(self, tmp_path: Path):
        adapter, out = _setup_how(tmp_path)
        resp, _ = _repl(adapter, out, "how Done avoid ~Start", seq=10)
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "Reached" in result or "Cannot reach" in result or "Stopped" in result

    def test_how_avoid_multiple_is_union(self, tmp_path: Path):
        """Comma-separated ``avoid`` conditions are a union of exclusions: both A
        and B are excluded, so the planner reaches ``Filling`` via the clean C
        lever.  (A single ``avoid`` still forwards unchanged — see test_how_avoid.)
        """
        adapter, out = _setup_how_multi_avoid(tmp_path)
        resp, _ = _repl(adapter, out, "how Filling avoid A, B", seq=10)
        assert resp["success"] is True, resp
        result = resp["body"]["result"]
        assert "Reached" in result, result

    def test_how_compound_comparisons(self, tmp_path: Path):
        """Comma-separated comparison conjuncts are a multi-target conjunction."""
        adapter, out = _setup_compound(tmp_path)
        resp, _ = _repl(adapter, out, "how Step == 2, Mode == 2", seq=10)
        assert resp["success"] is True, resp
        result = resp["body"]["result"]
        assert "Step" in result and "Mode" in result, result

    def test_how_avoid_missing_expr(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "how Light avoid")
        assert resp["success"] is False
        assert "after 'avoid'" in resp["message"]

    def test_how_reports_plan(self, tmp_path: Path):
        adapter, out = _setup_how(tmp_path)
        resp, _ = _repl(adapter, out, "how Running", seq=10)
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "Reached" in result
        assert "Running" in result

    def test_how_already_at_target(self, tmp_path: Path):
        adapter, out = _setup_how(tmp_path)
        _repl(adapter, out, "force Start true", seq=10)
        _repl(adapter, out, "step 2", seq=11)
        resp, _ = _repl(adapter, out, "how Running", seq=12)
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "in 0 scan" in result

    def test_how_progress_explains_long_investigation(self):
        from pyrung.core.analysis.pilot.types import PilotEvent
        from pyrung.dap.console import _PilotProgressFormatter

        progress = _PilotProgressFormatter()
        fragments = [
            progress.format(
                PilotEvent(
                    "letrun_ejection",
                    910,
                    {"channel_tag": "State", "from_value": 6, "to_value": 10},
                )
            ),
            progress.format(
                PilotEvent(
                    "departure_check_started",
                    910,
                    {"channel_tag": "State", "from_value": 6, "to_value": 10},
                )
            ),
            progress.format(
                PilotEvent(
                    "investigation_started",
                    910,
                    {"channel_tag": "State", "from_value": 6, "to_value": 10},
                )
            ),
        ]

        assert fragments == [
            "  State jumped 6 -> 10",
            " Checking...",
            " unexpected.\n  Preventable?",
        ]

        result = progress.format(
            PilotEvent(
                "trend_regression",
                950,
                {"investigation": {"confirmed_detail": ({"holds": (("DoorClosed", True),)},)}},
            )
        )
        assert result == " Yes -- keep DoorClosed=True.\n"

    def test_how_progress_streams_trial_result_on_the_same_line(self):
        from pyrung.core.analysis.pilot.types import PilotEvent
        from pyrung.dap.console import _PilotProgressFormatter

        progress = _PilotProgressFormatter()
        trying = progress.format(PilotEvent("candidate_try", 10, {"applied": (("Start", True),)}))
        accepted = progress.format(
            PilotEvent("candidate_accepted", 11, {"applied": (("Start", True),)})
        )

        assert trying == "\nTrying Start=True..."
        assert accepted == " done.\n"

    def test_how_progress_streams_an_atomic_widening_batch(self):
        from pyrung.core.analysis.pilot.types import PilotEvent
        from pyrung.dap.console import _PilotProgressFormatter

        actions = (("ModeRequest", True), ("Production", True))
        progress = _PilotProgressFormatter()

        trying = progress.format(PilotEvent("candidate_try", 10, {"applied": actions}))
        accepted = progress.format(PilotEvent("widening_accepted", 11, {"applied": actions}))

        assert trying == "\nTrying ModeRequest=True, Production=True..."
        assert accepted == " done.\n"

    def test_how_progress_streams_an_exact_crossing_as_one_atomic_pulse(self):
        from pyrung.core.analysis.pilot.types import PilotEvent
        from pyrung.dap.console import _PilotProgressFormatter

        actions = (("ModeRequest", True), ("Production", True))
        progress = _PilotProgressFormatter()

        trying = progress.format(PilotEvent("crossing_try", 10, {"actions": actions}))
        accepted = progress.format(PilotEvent("crossing_accepted", 11, {"applied": actions}))

        assert trying == "\nTrying ModeRequest=True, Production=True..."
        assert accepted == " done.\n"

    def test_how_progress_closes_a_rejected_exact_crossing(self):
        from pyrung.core.analysis.pilot.types import PilotEvent
        from pyrung.dap.console import _PilotProgressFormatter

        actions = (("ModeRequest", True), ("Production", True))
        progress = _PilotProgressFormatter()

        trying = progress.format(PilotEvent("crossing_try", 10, {"actions": actions}))
        rejected = progress.format(PilotEvent("crossing_rejected", 11, {"actions": actions}))

        assert trying == "\nTrying ModeRequest=True, Production=True..."
        assert rejected == " no useful change.\n"

    def test_how_progress_calls_prerequisite_controls_set_and_explains_why(self):
        from pyrung import Bool, Int
        from pyrung.core.analysis.pilot.overlay import PilotRung
        from pyrung.core.analysis.pilot.types import PilotEvent
        from pyrung.dap.console import _PilotProgressFormatter

        temperature = Int("ProgressTest_Temperature", external=True)
        low_band = Int("ProgressTest_LowBand")
        target = Bool("ProgressTest_Target")
        progress = _PilotProgressFormatter()

        result = progress.format(
            PilotEvent(
                "candidates_built",
                10,
                {
                    "prerequisite_pilot_rungs": (PilotRung(temperature.name, -1, ~target),),
                    "lever_notes": {
                        temperature.name: (
                            f"held {temperature.name} < {low_band.name} "
                            f"(e.g., {temperature.name} = -1)"
                        )
                    },
                },
            )
        )

        assert result == (
            "  Set ProgressTest_Temperature=-1 to satisfy "
            "ProgressTest_Temperature < ProgressTest_LowBand.\n"
        )

    def test_how_progress_closes_an_implicit_valid_check_before_the_next_action(self):
        from pyrung.core.analysis.pilot.types import PilotEvent
        from pyrung.dap.console import _PilotProgressFormatter

        progress = _PilotProgressFormatter()
        progress.format(
            PilotEvent(
                "departure_check_started",
                10,
                {"channel_tag": "State", "from_value": 2, "to_value": 4},
            )
        )

        fragment = progress.format(PilotEvent("candidate_try", 11, {"applied": (("Start", True),)}))

        assert fragment == " valid.\n\nTrying Start=True..."

    def test_how_progress_excludes_a_sustained_control_from_the_trial(self):
        from pyrung import Bool, Int
        from pyrung.core.analysis.pilot.overlay import PilotRung
        from pyrung.core.analysis.pilot.types import PilotEvent
        from pyrung.dap.console import _PilotProgressFormatter

        target = Bool("ProgressTest_PulseTarget")
        temperature = Int("ProgressTest_PulseTemperature", external=True)
        progress = _PilotProgressFormatter()
        progress.format(
            PilotEvent(
                "candidates_built",
                10,
                {
                    "prerequisite_pilot_rungs": (PilotRung(temperature.name, -1, ~target),),
                },
            )
        )

        fragment = progress.format(
            PilotEvent(
                "candidate_try",
                11,
                {"applied": (("Start", True), (temperature.name, -1))},
            )
        )

        assert fragment == "\nTrying Start=True..."

    def test_how_progress_resumes_without_repeating_the_wait_target(self):
        from pyrung.core.analysis.pilot.types import PilotEvent
        from pyrung.dap.console import _PilotProgressFormatter

        progress = _PilotProgressFormatter()
        progress._after_correction = True

        assert (
            progress.format(PilotEvent("bearing_coast", 10, {"channel_tag": "HeatDelayDone"}))
            == "\n  Resuming..."
        )

    def test_how_progress_distinguishes_history_rebase_from_pilotrung_resume(self):
        from types import SimpleNamespace

        from pyrung.core.analysis.pilot.types import PilotEvent
        from pyrung.dap.console import _PilotProgressFormatter

        progress = _PilotProgressFormatter()
        progress._after_correction = True

        repaired = progress.format(
            PilotEvent(
                "requirement_locally_repaired",
                1,
                {
                    "requirement": SimpleNamespace(
                        provenance="program-guard-rebase",
                        source_scan=0,
                    ),
                    "assignments": (("HeelBase", True),),
                },
            )
        )
        coast = progress.format(
            PilotEvent("bearing_coast", 1, {"channel_tag": "WaitToSettle_2_Done"})
        )

        assert repaired == "  Rewound to scan 0 and applied HeelBase=True; re-orienting.\n"
        assert coast == "  Waiting for WaitToSettle_2_Done..."

    def test_how_progress_prints_the_exact_self_guarded_correction(self):
        from pyrung import Bool
        from pyrung.core.analysis.pilot.overlay import PilotRung
        from pyrung.core.analysis.pilot.types import PilotEvent
        from pyrung.core.condition import AllCondition
        from pyrung.dap.console import _PilotProgressFormatter

        watchdog_done = Bool("ProgressTest_WatchdogDone")
        sensor = Bool("ProgressTest_Sensor", external=True)
        correction = PilotRung(
            sensor.name,
            True,
            AllCondition(watchdog_done == False, sensor != True),  # noqa: E712
        )
        progress = _PilotProgressFormatter()
        progress.format(PilotEvent("investigation_started", 10))

        result = progress.format(
            PilotEvent(
                "trend_regression",
                20,
                {"investigation": {"confirmed_detail": ({"holds": (correction,)},)}},
            )
        )

        assert result == (
            " Yes -- with rung(And(~ProgressTest_WatchdogDone, "
            "~ProgressTest_Sensor)): latch(ProgressTest_Sensor).\n"
        )

    def test_how_progress_prints_working_theory_corrective_rung(self):
        from types import SimpleNamespace

        from pyrung import Bool
        from pyrung.core.analysis.pilot.overlay import PilotRung
        from pyrung.core.analysis.pilot.types import PilotEvent
        from pyrung.dap.console import _PilotProgressFormatter

        running = Bool("ProgressTest_TheoryRunning")
        sail = Bool("ProgressTest_TheorySail", external=True)
        correction = PilotRung(sail.name, True, running == True)  # noqa: E712
        progress = _PilotProgressFormatter()
        progress.format(PilotEvent("investigation_started", 10))

        result = progress.format(
            PilotEvent(
                "trend_regression",
                20,
                {
                    "investigation": {
                        "working_theory": True,
                        "requirement": SimpleNamespace(corrective_pilot_rungs=(correction,)),
                    }
                },
            )
        )

        assert result == (
            " Yes -- with rung(ProgressTest_TheoryRunning): latch(ProgressTest_TheorySail).\n"
        )

    def test_how_progress_groups_corrections_on_their_exact_rung(self):
        from pyrung import Bool, Int
        from pyrung.core.analysis.pilot.overlay import PilotRung
        from pyrung.core.analysis.pilot.types import PilotEvent
        from pyrung.dap.console import _PilotProgressFormatter

        state = Int("ProgressTest_State")
        door = Bool("ProgressTest_Door", external=True)
        lint_door = Bool("ProgressTest_LintDoor", external=True)
        guard = state == 6
        corrections = (
            PilotRung(door.name, True, guard),
            PilotRung(lint_door.name, True, guard),
        )
        progress = _PilotProgressFormatter()
        progress.format(PilotEvent("investigation_started", 10))

        result = progress.format(
            PilotEvent(
                "trend_regression",
                20,
                {"investigation": {"confirmed_detail": ({"holds": corrections},)}},
            )
        )

        assert result == (
            " Yes -- with rung(ProgressTest_State == 6): "
            "latch(ProgressTest_Door); latch(ProgressTest_LintDoor).\n"
        )

    def test_how_progress_keeps_intrascan_investigation_grounded_in_the_channel(self):
        from pyrung.core.analysis.pilot.types import PilotEvent
        from pyrung.dap.console import _PilotProgressFormatter

        progress = _PilotProgressFormatter()
        progress.format(PilotEvent("investigation_started", 10))

        pending = progress.format(
            PilotEvent(
                "trend_regression",
                11,
                {"investigation": {}, "position": (("HeelStep", 40),)},
            )
        )
        correction = progress.format(
            PilotEvent(
                "theory_correction_composed",
                11,
                {
                    "configuration": (("FirstWatchdogMs", 21),),
                    "requirement_conditions": (("FirstWatchdogMs", ">", 20),),
                    "superseded_configuration_identities": (("old",),),
                    "position": (("HeelStep", 40),),
                },
            )
        )

        assert pending == " Not yet -- returned to HeelStep=40 to investigate.\n"
        assert correction == (
            "  Working theory at HeelStep=40: refine the setting to "
            "FirstWatchdogMs=21 before retrying (FirstWatchdogMs > 20).\n"
        )

    def test_how_progress_names_actual_temporary_logic_revocation_and_replacement(self):
        from pyrung import Bool, Int
        from pyrung.core.analysis.pilot.overlay import PilotRung
        from pyrung.core.analysis.pilot.types import PilotEvent
        from pyrung.dap.console import _PilotProgressFormatter

        state = Int("ProgressTest_RevokeState")
        go = Bool("ProgressTest_RevokeGo", external=True)
        old = PilotRung(go.name, True, state == 6)
        replacement = PilotRung(go.name, False, state == 6)
        progress = _PilotProgressFormatter()
        progress.format(PilotEvent("investigation_started", 10))

        result = progress.format(
            PilotEvent(
                "trend_regression",
                20,
                {
                    "revoked_pilot_rungs": (old,),
                    "investigation": {
                        "confirmed_detail": ({"holds": (replacement,)},),
                    },
                },
            )
        )

        assert result == (
            " Yes.\n"
            "  Remove temporary logic: with rung(ProgressTest_RevokeState == 6): "
            "latch(ProgressTest_RevokeGo).\n"
            "  Replace with: with rung(ProgressTest_RevokeState == 6): "
            "reset(ProgressTest_RevokeGo).\n"
        )

    def test_how_progress_surfaces_exploratory_inputs_as_guidance(self):
        from pyrung.core.analysis.pilot.types import PilotEvent
        from pyrung.dap.console import _PilotProgressFormatter

        progress = _PilotProgressFormatter()
        result = progress.format(
            PilotEvent(
                "guidance_requested",
                10,
                {
                    "candidates": (
                        {"actions": (("PossibleStart", True),)},
                        {"actions": (("PossibleReset", True), ("Mode", 2))},
                    )
                },
            )
        )

        assert result == (
            "\nGuidance required before trying exploratory inputs: "
            "PossibleStart=True; Mode=2, PossibleReset=True.\n"
        )

    def test_how_progress_describes_observed_motion(self):
        from pyrung.core.analysis.pilot.types import PilotEvent
        from pyrung.dap.console import _format_pilot_progress

        event = PilotEvent(
            "bearing_coast_accepted",
            120,
            {
                "scan_before": 100,
                "scan_after": 120,
                "bearing_coast_channel_tag": "State",
                "bearing_coast_before_value": 6,
                "bearing_coast_actual_value": 7,
            },
        )

        assert _format_pilot_progress(event) == "  State 6 -> 7 after 20 scans.\n"

    def test_how_progress_reports_folded_and_kernel_work(self):
        from pyrung.core.analysis.pilot.types import PilotEvent
        from pyrung.dap.console import _format_pilot_progress

        event = PilotEvent(
            "bearing_coast_accepted",
            3897,
            {
                "scan_before": 100,
                "scan_after": 3897,
                "bearing_coast_channel_tag": "State",
                "bearing_coast_before_value": 6,
                "bearing_coast_actual_value": 7,
                "coast_skipped_scans": 3300,
                "coast_kernel_scans": 497,
            },
        )

        assert (
            _format_pilot_progress(event)
            == "  State 6 -> 7 after 3797 scans (3,300 folded; 497 kernel).\n"
        )


# ---------------------------------------------------------------------------
# Prove (always / never)
# ---------------------------------------------------------------------------


class TestProveVerb:
    def test_prove_always(self, tmp_path: Path):
        adapter, out = _setup_how(tmp_path)
        resp, _ = _repl(adapter, out, "prove always Or(~Done, Running)", seq=10)
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "Proven" in result

    def test_prove_never(self, tmp_path: Path):
        adapter, out = _setup_how(tmp_path)
        resp, _ = _repl(adapter, out, "prove never Done, ~Running", seq=10)
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "Proven" in result

    def test_prove_never_counterexample(self, tmp_path: Path):
        adapter, out = _setup_how(tmp_path)
        resp, _ = _repl(adapter, out, "prove never Running", seq=10)
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "Counterexample" in result

    def test_prove_missing_mode(self, tmp_path: Path):
        adapter, out = _setup_how(tmp_path)
        resp, _ = _repl(adapter, out, "always")
        assert resp["success"] is False

    def test_prove_missing_expression(self, tmp_path: Path):
        adapter, out = _setup_how(tmp_path)
        resp, _ = _repl(adapter, out, "prove always")
        assert resp["success"] is False

    def test_prove_invalid_mode(self, tmp_path: Path):
        adapter, out = _setup_how(tmp_path)
        resp, _ = _repl(adapter, out, "prove Or(~Done, Running)")
        assert resp["success"] is False

    def test_prove_always_comparison(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "prove always (Counter == 0)", seq=10)
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "Proven" in result


# ---------------------------------------------------------------------------
# DataView / Upstream / Downstream
# ---------------------------------------------------------------------------


class TestDataViewVerbs:
    def test_dataview_contains(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "dataview Button")
        assert resp["success"] is True
        assert "Button" in resp["body"]["result"]

    def test_dataview_role_prefix(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "dataview t:")
        assert resp["success"] is True
        assert "tag(s)" in resp["body"]["result"]

    def test_dataview_no_match(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "dataview ZZZnonexistent")
        assert resp["success"] is True
        assert "No matching" in resp["body"]["result"]

    def test_dataview_missing_query(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "dataview")
        assert resp["success"] is False

    def test_upstream(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "upstream Light")
        assert resp["success"] is True
        assert "Button" in resp["body"]["result"]

    def test_downstream(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "downstream Button")
        assert resp["success"] is True
        assert "Light" in resp["body"]["result"]

    def test_upstream_missing_tag(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "upstream")
        assert resp["success"] is False

    def test_downstream_missing_tag(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "downstream")
        assert resp["success"] is False


# ---------------------------------------------------------------------------
# Monitor / Unmonitor
# ---------------------------------------------------------------------------


class TestMonitorVerbs:
    def test_monitor_adds(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "monitor Button")
        assert resp["success"] is True
        assert "Monitor added" in resp["body"]["result"]
        assert len(adapter._monitor_meta) == 1

    def test_unmonitor_removes(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "monitor Button", seq=10)
        resp, _ = _repl(adapter, out, "unmonitor Button", seq=11)
        assert resp["success"] is True
        assert "Monitor removed" in resp["body"]["result"]
        assert len(adapter._monitor_meta) == 0

    def test_unmonitor_unknown_tag(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "unmonitor NonExistent")
        assert resp["success"] is False

    def test_monitor_missing_tag(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "monitor")
        assert resp["success"] is False


# ---------------------------------------------------------------------------
# Ladder checks
# ---------------------------------------------------------------------------


class TestCheckVerb:
    def test_check_clean_program(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "check")
        assert resp["success"] is True
        assert resp["body"]["result"] == "No findings."

    def test_check_prints_finding(self, tmp_path: Path):
        adapter, out = _setup_how(tmp_path)
        resp, _ = _repl(adapter, out, "check COIL_STUCK_HIGH")
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "[COIL_STUCK_HIGH] warning" in result
        assert "never reset" in result
        assert "COIL_STUCK_HIGH: 1" in result

    def test_check_rejects_unknown_selector(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "check NOT_A_RULE")
        assert resp["success"] is False
        assert "Unknown rule code or category" in resp["message"]

    def test_check_uses_runner_scan_period(self, tmp_path: Path, monkeypatch):
        adapter, out = _setup(tmp_path, dt=0.05)
        program = adapter._runner.program
        original_check = program.check
        observed: dict[str, float] = {}

        def recording_check(**kwargs):
            observed["dt"] = kwargs["dt"]
            return original_check(**kwargs)

        monkeypatch.setattr(program, "check", recording_check)

        resp, _ = _repl(adapter, out, "check")

        assert resp["success"] is True
        assert observed["dt"] == 0.05


# ---------------------------------------------------------------------------
# Simplified
# ---------------------------------------------------------------------------


class TestSimplifiedVerb:
    def test_simplified_single_tag(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "simplified Light")
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "Light = Button" in result
        assert "permissives: Button" in result
        assert "writer(s)" in result

    def test_simplified_all(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "simplified")
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "terminal(s)" in result
        assert "Light" in result
        assert "permissives: Button" in result

    def test_simplified_non_terminal(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "simplified Button")
        assert resp["success"] is False
        assert "not a terminal" in resp["message"]

    def test_simplified_unknown_tag(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "simplified ZZZnonexistent")
        assert resp["success"] is False
        assert "Unknown tag" in resp["message"]


# ---------------------------------------------------------------------------
# Help and error handling
# ---------------------------------------------------------------------------


class TestHelpAndErrors:
    def test_help_lists_all_verbs(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "help")
        assert resp["success"] is True
        result = resp["body"]["result"]
        for verb in [
            "force",
            "unforce",
            "patch",
            "step",
            "run",
            "cause",
            "effect",
            "check",
            "monitor",
            "simplified",
            "help",
        ]:
            assert verb in result

    def test_unknown_command(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "foobar")
        assert resp["success"] is False
        assert "Unknown command" in resp["message"]
        assert "Available:" in resp["message"]

    def test_unknown_command_still_mentions_watch(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "Button == true")
        assert resp["success"] is False
        assert "Watch" in resp["message"]


# ---------------------------------------------------------------------------
# Enriched dataview output
# ---------------------------------------------------------------------------


def _udt_script() -> str:
    return (
        "from pyrung.core import Bool, Int, Real, Field, Physical, PLC, Program, Rung, out, udt\n"
        "\n"
        "@udt()\n"
        "class Pump:\n"
        "    Running: Bool\n"
        "    Speed: Int = Field(min=0, max=100, uom='rpm')\n"
        "\n"
        "enable = Bool('Enable', external=True)\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(enable):\n"
        "        out(Pump.Running)\n"
        "        out(Pump.Speed)\n"
        "\n"
        "runner = PLC(prog, dt=0.010)\n"
    )


def _setup_udt(tmp_path: Path) -> tuple[DAPAdapter, io.BytesIO]:
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic_udt.py", _udt_script())
    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)
    return adapter, out_stream


class TestEnrichedDataview:
    def test_dataview_shows_type(self, tmp_path: Path):
        adapter, out = _setup_udt(tmp_path)
        resp, _ = _repl(adapter, out, "dataview Speed")
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "Int" in result

    def test_dataview_shows_external_flag(self, tmp_path: Path):
        adapter, out = _setup_udt(tmp_path)
        resp, _ = _repl(adapter, out, "dataview Enable")
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "external" in result

    def test_dataview_shows_min_max_uom(self, tmp_path: Path):
        adapter, out = _setup_udt(tmp_path)
        resp, _ = _repl(adapter, out, "dataview Speed")
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "min:0" in result
        assert "max:100" in result
        assert "uom:rpm" in result

    def test_dataview_shows_structure_info(self, tmp_path: Path):
        adapter, out = _setup_udt(tmp_path)
        resp, _ = _repl(adapter, out, "dataview Running")
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "udt:Pump.Running" in result


class TestStructuresVerb:
    def test_structures_lists_udt(self, tmp_path: Path):
        adapter, out = _setup_udt(tmp_path)
        resp, _ = _repl(adapter, out, "structures")
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "UDTs:" in result
        assert "Pump" in result
        assert "Running" in result
        assert "Speed" in result

    def test_structures_shows_field_metadata(self, tmp_path: Path):
        adapter, out = _setup_udt(tmp_path)
        resp, _ = _repl(adapter, out, "structures")
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "min:0" in result
        assert "max:100" in result
        assert "uom:rpm" in result

    def test_structures_no_structures(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "structures")
        assert resp["success"] is True
        assert "No structures found" in resp["body"]["result"]

    def test_help_includes_structures(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "help")
        assert resp["success"] is True
        assert "structures" in resp["body"]["result"]
