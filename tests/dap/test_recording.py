"""Tests for saved recordings: the header, ``record save``, and replay with no debugger."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from pyrung.core.time_mode import TimeMode
from pyrung.dap.adapter import DAPAdapter
from pyrung.dap.recording import (
    RECORDING_SUFFIX,
    RecordingFormatError,
    RecordingHeader,
    ReplayFailed,
    parse_header,
    replay_recording,
)
from tests.dap.test_capture import _drain_messages, _repl, _send_request

_SCRIPT = (
    "from pyrung.core import Bool, PLC, Program, Rung, out\n"
    "\n"
    "button = Bool('Button')\n"
    "light = Bool('Light')\n"
    "\n"
    "with Program(strict=False) as prog:\n"
    "    with Rung(button):\n"
    "        out(light)\n"
    "\n"
    "runner = PLC(prog, dt=0.010)\n"
)


def _setup(tmp_path: Path, script: str = _SCRIPT) -> tuple[DAPAdapter, io.BytesIO]:
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script_path = tmp_path / "logic.py"
    script_path.write_text(script, encoding="utf-8")
    _send_request(
        adapter, out_stream, seq=1, command="launch", arguments={"program": str(script_path)}
    )
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)
    return adapter, out_stream


def _record(adapter: DAPAdapter, out: io.BytesIO, *commands: str) -> None:
    _repl(adapter, out, "record start_machine")
    for command in commands:
        resp, _ = _repl(adapter, out, command)
        assert resp["success"] is True, resp


def _ok(resp: dict[str, Any]) -> str:
    assert resp["success"] is True, resp.get("message")
    return resp["body"]["result"]


class TestHeader:
    def test_a_header_reads_back_as_written(self):
        header = RecordingHeader(
            action="start_machine",
            program="logic.py",
            program_sha256="ab" * 32,
            pyrung_version="1.2.3",
            scan_time=0.1,
            harness=True,
        )
        assert parse_header([*header.lines(), "step 1"]) == header

    def test_realtime_reads_back(self):
        header = RecordingHeader(realtime=True)
        assert header.lines() == ["# scan time: realtime"]
        assert parse_header(header.lines()) == header

    def test_the_header_ends_at_the_first_command(self):
        lines = ["# action: a", "step 1", "# harness: off"]
        assert parse_header(lines) == RecordingHeader(action="a")

    def test_a_note_in_the_header_stays_a_note(self):
        lines = ["# action: a", "# started after the pump warmed up", "# intent: checking"]
        assert parse_header(lines) == RecordingHeader(action="a")

    def test_a_bad_scan_time_names_its_line(self):
        with pytest.raises(RecordingFormatError, match="scan time") as info:
            parse_header(["# action: a", "# scan time: 10 ms"])
        assert info.value.line == 2

    def test_a_bad_harness_names_its_line(self):
        with pytest.raises(RecordingFormatError, match="harness") as info:
            parse_header(["# harness: maybe"])
        assert info.value.line == 1

    def test_a_scan_time_unit_in_capitals_reads(self):
        assert parse_header(["# scan time: 0.1 S"]).scan_time == 0.1

    def test_a_program_path_with_backslashes_reads_with_slashes(self):
        assert parse_header(["# program: ..\\plc\\logic.py"]).program == "../plc/logic.py"

    def test_the_plugin_collects_the_suffix_recordings_are_saved_with(self):
        from pyrung import pytest_plugin

        assert pytest_plugin._RECORDING_SUFFIX == RECORDING_SUFFIX


class TestRecordSave:
    def test_save_writes_the_header_and_commands_next_to_the_program(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _record(adapter, out, "patch Button true", "step 1", "assert Light == true")
        _ok(_repl(adapter, out, "record stop")[0])
        result = _ok(_repl(adapter, out, "record save")[0])

        saved = tmp_path / "start_machine.rec"
        assert f"to {saved}" in result
        lines = saved.read_text(encoding="utf-8").splitlines()
        header = parse_header(lines)
        assert header.action == "start_machine"
        assert header.program == "logic.py"
        assert header.scan_time == 0.010
        assert header.harness is False
        assert header.program_sha256 is not None and len(header.program_sha256) == 64
        assert lines[-3:] == ["patch Button true", "step 1", "assert Light == true"]

    def test_save_while_recording_stops_the_recording(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _record(adapter, out, "step 1")
        result = _ok(_repl(adapter, out, "record save")[0])
        assert result.startswith("Recording stopped. Saved 'start_machine' (1 command(s))")
        assert adapter._capture.recording is False
        assert (tmp_path / "start_machine.rec").is_file()

    def test_a_file_in_another_folder_names_the_program_from_there(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        (tmp_path / "tests").mkdir()
        _record(adapter, out, "step 1")
        _ok(_repl(adapter, out, "record save tests/start.rec")[0])
        header = parse_header((tmp_path / "tests" / "start.rec").read_text().splitlines())
        assert header.program == "../logic.py"

    def test_a_name_with_no_suffix_gets_rec(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _record(adapter, out, "step 1")
        _ok(_repl(adapter, out, "record save start")[0])
        assert (tmp_path / "start.rec").is_file()
        assert not (tmp_path / "start").exists()

    def test_a_quoted_path_with_a_space_saves_and_replays(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        (tmp_path / "my tests").mkdir()
        _record(adapter, out, "step 1")
        _ok(_repl(adapter, out, 'record save "my tests/start.rec"')[0])
        assert (tmp_path / "my tests" / "start.rec").is_file()
        result = _ok(_repl(adapter, out, 'replay "my tests/start.rec"')[0])
        assert result.startswith("Replayed 1/1")

    def test_save_over_a_file_says_it_replaced_it(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _record(adapter, out, "step 1")
        first = _ok(_repl(adapter, out, "record save")[0])
        assert "replaced" not in first
        second = _ok(_repl(adapter, out, "record save")[0])
        assert second.endswith("start_machine.rec (replaced the file that was there)")

    def test_save_with_nothing_recorded_is_an_error(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "record save")
        assert resp["success"] is False
        assert "Nothing to save" in resp["message"]

    def test_save_into_a_missing_folder_is_an_error(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _record(adapter, out, "step 1")
        resp, _ = _repl(adapter, out, "record save nowhere/start.rec")
        assert resp["success"] is False
        assert "Folder not found" in resp["message"]

    def test_stop_prints_the_header_and_names_the_file_save_writes(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _record(adapter, out, "step 1")
        result = _ok(_repl(adapter, out, "record stop")[0])
        first, transcript = result.split("\n", 1)
        assert first == "Recording stopped. 'record save' writes it to start_machine.rec."
        assert transcript.startswith("# action: start_machine\n# program: logic.py\n")


class TestReplayReadsTheHeader:
    def test_replay_takes_the_recorded_scan_time(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        rec = tmp_path / "slow.rec"
        rec.write_text("# scan time: 0.1 s\nrun 10\n", encoding="utf-8")
        runner = adapter._runner
        assert runner is not None
        assert runner.current_state.scan_id == 0
        start = runner.current_state.timestamp
        result = _ok(_repl(adapter, out, f"replay {rec}")[0])
        assert result.startswith("Scan time set to 0.1 s, as recorded\n")
        assert runner.dt == 0.1
        assert runner.time_mode == TimeMode.FIXED_STEP
        assert runner.current_state.timestamp - start == pytest.approx(1.0)

    def test_a_force_set_before_a_new_scan_time_stays_in_history(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _ok(_repl(adapter, out, "force Button true")[0])
        rec = tmp_path / "slow.rec"
        rec.write_text("# scan time: 0.1 s\nstep 5\n", encoding="utf-8")
        assert _ok(_repl(adapter, out, f"replay {rec}")[0]).startswith("Scan time set")
        runner = adapter._runner
        assert runner is not None
        assert runner.current_state.tags["Light"] is True
        replayed = runner.replay_to(3).current_state.tags
        assert replayed["Button"] is True
        assert replayed["Light"] is True

    def test_a_plc_that_has_scanned_keeps_its_scan_time_and_history(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _ok(_repl(adapter, out, "step 3")[0])
        runner = adapter._runner
        assert runner is not None
        checkpoint_scans = set(runner._checkpoints)
        rec = tmp_path / "slow.rec"
        rec.write_text("# scan time: 0.1 s\nstep 1\n", encoding="utf-8")
        result = _ok(_repl(adapter, out, f"replay {rec}")[0])
        assert result.startswith(
            "Note: the recording ran at a scan time of 0.1 s. "
            "This PLC has already scanned, so the replay keeps its 0.01 s.\n"
        )
        assert runner.dt == 0.01
        assert checkpoint_scans <= set(runner._checkpoints)

    def test_a_plc_in_the_middle_of_its_first_scan_keeps_its_scan_time(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _send_request(adapter, out, seq=3, command="stepIn")
        assert adapter._scan_gen is not None
        rec = tmp_path / "slow.rec"
        rec.write_text("# scan time: 0.1 s\nstep 1\n", encoding="utf-8")
        result = _ok(_repl(adapter, out, f"replay {rec}")[0])
        assert result.startswith("Note: the recording ran at a scan time of 0.1 s.")
        assert adapter._runner is not None
        assert adapter._runner.dt == 0.01

    def test_replay_leaves_a_matching_scan_time_alone(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        rec = tmp_path / "same.rec"
        rec.write_text("# scan time: 0.01 s\nstep 1\n", encoding="utf-8")
        result = _ok(_repl(adapter, out, f"replay {rec}")[0])
        assert result.startswith("Replayed 1/1")

    def test_a_recording_with_the_harness_off_replays_with_none(self, tmp_path: Path):
        from tests.dap.test_harness_console import _setup_with_harness

        adapter, out = _setup_with_harness(tmp_path)
        assert adapter._harness is not None
        rec = tmp_path / "off.rec"
        rec.write_text("# harness: off\nstep 1\n", encoding="utf-8")
        result = _ok(_repl(adapter, out, f"replay {rec}")[0])
        assert "[harness=off]" in result
        assert adapter._harness is None

    def test_the_harness_flag_wins_over_the_header(self, tmp_path: Path):
        from tests.dap.test_harness_console import _setup_with_harness

        adapter, out = _setup_with_harness(tmp_path)
        rec = tmp_path / "off.rec"
        rec.write_text("# harness: off\nstep 1\n", encoding="utf-8")
        result = _ok(_repl(adapter, out, f"replay {rec} --harness current")[0])
        assert "[harness=current]" in result
        assert adapter._harness is not None

    def test_replay_reads_a_relative_path_from_the_program_folder(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        (tmp_path / "start.rec").write_text("step 1\n", encoding="utf-8")
        assert _ok(_repl(adapter, out, "replay start.rec")[0]).startswith("Replayed 1/1")

    def test_a_bad_header_line_stops_the_replay_at_that_line(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        rec = tmp_path / "bad.rec"
        rec.write_text("# action: a\n# harness: maybe\nstep 1\n", encoding="utf-8")
        resp, _ = _repl(adapter, out, f"replay {rec}")
        assert resp["success"] is False
        assert resp["message"].startswith("Replay error at line 2: harness must be on or off")


class TestReplayWithNoDebugger:
    def test_a_saved_recording_replays_on_a_fresh_program(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _record(adapter, out, "patch Button true", "step 1", "assert Light == true")
        _ok(_repl(adapter, out, "record save")[0])

        result = replay_recording(tmp_path / "start_machine.rec")
        assert result.startswith("Replayed 3/3 command(s)")
        assert result.endswith("[harness=off]")

    def test_a_recording_made_with_the_harness_replays_with_it(self, tmp_path: Path):
        from tests.dap.test_harness_console import _setup_with_harness

        adapter, out = _setup_with_harness(tmp_path)
        _record(adapter, out, "patch Cmd true", "run 50ms", "assert Device_Fb == true")
        _ok(_repl(adapter, out, "record save")[0])
        rec = tmp_path / "start_machine.rec"
        assert parse_header(rec.read_text(encoding="utf-8").splitlines()).harness is True

        assert replay_recording(rec).endswith("[harness=current]")

    def test_a_failed_assert_names_its_line_and_reason(self, tmp_path: Path):
        (tmp_path / "logic.py").write_text(_SCRIPT, encoding="utf-8")
        rec = tmp_path / "fail.rec"
        rec.write_text(
            "# program: logic.py\npatch Button true\nstep 1\nassert Light == false\nstep 1\n",
            encoding="utf-8",
        )
        with pytest.raises(ReplayFailed) as info:
            replay_recording(rec)
        assert info.value.line == 4
        assert info.value.reason == "assert failed: Light = True, expected False"

    def test_a_milestone_past_its_limit_names_its_line(self, tmp_path: Path):
        (tmp_path / "logic.py").write_text(_SCRIPT, encoding="utf-8")
        rec = tmp_path / "late.rec"
        rec.write_text("# program: logic.py\nrun until Light within 5\n", encoding="utf-8")
        with pytest.raises(ReplayFailed) as info:
            replay_recording(rec)
        assert info.value.line == 2
        assert "Light" in info.value.reason

    def test_a_recording_with_no_program_line_says_so(self, tmp_path: Path):
        rec = tmp_path / "orphan.rec"
        rec.write_text("step 1\n", encoding="utf-8")
        with pytest.raises(ReplayFailed, match="names no program"):
            replay_recording(rec)

    def test_a_missing_program_says_so(self, tmp_path: Path):
        rec = tmp_path / "orphan.rec"
        rec.write_text("# program: gone.py\nstep 1\n", encoding="utf-8")
        with pytest.raises(ReplayFailed, match="Cannot find the program"):
            replay_recording(rec)

    def test_a_program_that_fails_to_load_says_so(self, tmp_path: Path):
        (tmp_path / "logic.py").write_text("raise RuntimeError('no PLC today')\n")
        rec = tmp_path / "start.rec"
        rec.write_text("# program: logic.py\nstep 1\n", encoding="utf-8")
        with pytest.raises(ReplayFailed) as info:
            replay_recording(rec)
        assert info.value.line is None
        assert info.value.reason == "program failed to load: RuntimeError: no PLC today"

    def test_programs_import_their_own_sibling_modules(self, tmp_path: Path):
        import sys

        path_before = list(sys.path)
        # Another test in this process may have loaded a module named helpers.
        cached = sys.modules.get("helpers")
        for folder in ("a", "b"):
            project = tmp_path / folder
            project.mkdir()
            (project / "helpers.py").write_text(f"LIGHT = 'Light_{folder}'\n")
            (project / "logic.py").write_text(_SIBLING_SCRIPT, encoding="utf-8")
            (project / "start.rec").write_text(
                f"# program: logic.py\npatch Button true\nstep 1\nassert Light_{folder} == true\n",
                encoding="utf-8",
            )
        for folder in ("a", "b"):
            assert replay_recording(tmp_path / folder / "start.rec").startswith("Replayed 3/3")
        assert sys.modules.get("helpers") is cached
        assert sys.path == path_before

    def test_a_file_named_like_a_standard_module_leaves_that_module_alone(self, tmp_path: Path):
        import sys

        warnings_before = sys.modules["warnings"]
        pkgutil_before = sys.modules.get("pkgutil")
        for name in ("warnings", "pkgutil"):
            (tmp_path / f"{name}.py").write_text(f"raise RuntimeError('user {name}.py')\n")
        (tmp_path / "logic.py").write_text(_SCRIPT, encoding="utf-8")
        rec = tmp_path / "start.rec"
        rec.write_text(
            "# program: logic.py\n# scan time: 0.1 s\npatch Button true\n"
            "run until Light within 5\nassert Light == true\n",
            encoding="utf-8",
        )

        assert replay_recording(rec).startswith("Scan time set")
        assert sys.modules["warnings"] is warnings_before
        assert sys.modules.get("pkgutil") is pkgutil_before

    def test_a_module_of_the_same_name_loaded_elsewhere_gives_way(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import sys
        import types

        elsewhere = types.ModuleType("helpers")
        elsewhere.__file__ = str(tmp_path / "elsewhere" / "helpers.py")
        elsewhere.LIGHT = "Light_elsewhere"
        monkeypatch.setitem(sys.modules, "helpers", elsewhere)
        (tmp_path / "helpers.py").write_text("LIGHT = 'Light_here'\n")
        (tmp_path / "logic.py").write_text(_SIBLING_SCRIPT, encoding="utf-8")
        rec = tmp_path / "start.rec"
        rec.write_text(
            "# program: logic.py\npatch Button true\nstep 1\nassert Light_here == true\n",
            encoding="utf-8",
        )

        assert replay_recording(rec).startswith("Replayed 3/3")
        assert sys.modules["helpers"] is elsewhere

    def test_a_package_deeper_in_the_program_folder_stays_loaded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import sys

        # An editable install under src/, which the program imports by its own name.
        package = tmp_path / "src" / "rsmylib"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("LIGHT = 'Light'\n")
        monkeypatch.syspath_prepend(str(tmp_path / "src"))
        (tmp_path / "helpers.py").write_text("PREFIX = 'x'\n")
        script = _SIBLING_SCRIPT.replace(
            "from helpers import LIGHT\n", "import helpers\nfrom rsmylib import LIGHT\n"
        )
        (tmp_path / "logic.py").write_text(script, encoding="utf-8")
        rec = tmp_path / "start.rec"
        rec.write_text("# program: logic.py\nstep 1\n", encoding="utf-8")

        cached = sys.modules.get("helpers")
        try:
            replay_recording(rec)
            assert "rsmylib" in sys.modules
            assert sys.modules.get("helpers") is cached
        finally:
            sys.modules.pop("rsmylib", None)


_SIBLING_SCRIPT = (
    "from helpers import LIGHT\n"
    "from pyrung.core import Bool, PLC, Program, Rung, out\n"
    "\n"
    "button = Bool('Button')\n"
    "light = Bool(LIGHT)\n"
    "\n"
    "with Program(strict=False) as prog:\n"
    "    with Rung(button):\n"
    "        out(light)\n"
    "\n"
    "runner = PLC(prog, dt=0.010)\n"
)


class TestPytestPlugin:
    def test_each_recording_is_a_test(self, pytester: pytest.Pytester) -> None:
        pytester.path.joinpath("logic.py").write_text(_SCRIPT, encoding="utf-8")
        pytester.path.joinpath("good.rec").write_text(
            "# program: logic.py\npatch Button true\nstep 1\nassert Light == true\n",
            encoding="utf-8",
        )
        pytester.path.joinpath("bad.rec").write_text(
            "# program: logic.py\nstep 1\nassert Light == true\nstep 1\n",
            encoding="utf-8",
        )
        result = pytester.runpytest("-p", "pyrung.pytest_plugin", "--pyrung-coverage-json=")
        result.assert_outcomes(passed=1, failed=1)
        result.stdout.fnmatch_lines(["*bad.rec:3: assert failed: Light = False, expected True*"])
