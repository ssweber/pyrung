"""Session capture buffer for the DAP console.

Passive recording layer: every console command evaluated while a
recording is active gets appended to the buffer with its scan_id
and timestamp.  ``record ACTION`` / ``record stop`` bracket a
named capture.  On stop the buffer yields a plain-text transcript
that ``replay`` runs, and ``record save`` writes it to a file.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pyrung.core.time_mode import TimeMode
from pyrung.dap.console import ConsoleResult, register
from pyrung.dap.expressions import Compare
from pyrung.dap.grammar import Slot
from pyrung.dap.handlers.stack_variables_evaluate import condition_predicate, parse_known_condition
from pyrung.dap.recording import (
    RECORDING_SUFFIX,
    FinishedRecording,
    RecordingFormatError,
    RecordingHeader,
    parse_header,
    program_sha256,
    pyrung_version,
)

#: A CLICK keeps a Real to about 7 significant digits, so ``assert`` takes a Real
#: within one part in a million (or 1e-6 near zero) as equal.
REAL_TOLERANCE = 1e-6


@dataclass(frozen=True)
class CaptureEntry:
    """A single recorded console command."""

    command: str
    scan_id: int | None
    timestamp: float
    provenance: str = "console"


@dataclass(frozen=True)
class ProvenanceLine:
    """A transcript line split into provenance and console command."""

    source: str | None
    command: str


def parse_provenance_line(line: str) -> ProvenanceLine:
    """Parse ``source: command`` transcript lines.

    Sources may themselves contain colons, as in
    ``harness:analog:thermal: patch TankTemp 2.0``.  Splitting on the
    final ``": "`` preserves the full provenance source.
    """

    stripped = line.strip()
    source, sep, command = stripped.rpartition(": ")
    if sep and source and not any(ch.isspace() for ch in source):
        return ProvenanceLine(source=source, command=command.strip())
    return ProvenanceLine(source=None, command=stripped)


@dataclass
class CaptureBuffer:
    """Accumulates console commands between ``record`` / ``record stop``."""

    action: str | None = None
    entries: list[CaptureEntry] = field(default_factory=list)
    start_scan_id: int | None = None
    start_timestamp: float = 0.0
    header: RecordingHeader = field(default_factory=RecordingHeader)
    program_path: Path | None = None
    #: The last stopped recording, which ``record save`` writes.
    finished: FinishedRecording | None = None

    @property
    def recording(self) -> bool:
        return self.action is not None

    def start(
        self,
        action: str,
        scan_id: int | None,
        timestamp: float,
        *,
        header: RecordingHeader | None = None,
        program_path: Path | None = None,
    ) -> None:
        self.action = action
        self.entries = []
        self.start_scan_id = scan_id
        self.start_timestamp = timestamp
        self.header = header if header is not None else RecordingHeader()
        self.program_path = program_path

    def append(
        self,
        command: str,
        scan_id: int | None,
        timestamp: float,
        provenance: str = "console",
    ) -> None:
        if self.recording:
            self.entries.append(CaptureEntry(command, scan_id, timestamp, provenance=provenance))

    def stop(self) -> tuple[str, list[CaptureEntry]]:
        """Stop recording and return ``(transcript, raw_entries)``.

        The transcript names the program by its file name, as a recording saved beside it does.
        """
        lines: list[str] = []
        for entry in self.entries:
            if entry.provenance == "console":
                lines.append(entry.command)
            else:
                lines.append(f"{entry.provenance}: {entry.command}")
        self.finished = FinishedRecording(
            header=replace(self.header, action=self.action),
            program_path=self.program_path,
            commands=lines,
        )
        raw = list(self.entries)
        self.action = None
        self.entries = []
        folder = self.program_path.parent if self.program_path is not None else None
        return self.finished.text(folder), raw

    def reset(self) -> None:
        self.action = None
        self.entries = []
        self.start_scan_id = None
        self.start_timestamp = 0.0
        self.header = RecordingHeader()
        self.program_path = None
        self.finished = None


# ---------------------------------------------------------------------------
# Console verb
# ---------------------------------------------------------------------------

_CAPTURE_EXCLUDED = frozenset({"record", "replay", "help", "pause", "continue"})


def capture_hook(adapter: Any, verb: str, expression: str, *, provenance: str = "console") -> None:
    """Called by ``dispatch()`` after every successful command."""
    capture: CaptureBuffer | None = getattr(adapter, "_capture", None)
    if capture is None or not capture.recording or verb in _CAPTURE_EXCLUDED:
        return
    runner = getattr(adapter, "_runner", None)
    timestamp = runner.current_state.timestamp if runner else 0.0
    scan_id = runner.current_state.scan_id if runner else getattr(adapter, "_current_scan_id", None)
    capture.append(expression.strip(), scan_id, timestamp, provenance=provenance)


_RECORD_USAGE = "record <action> | record stop | record save [file]"


@register("record", usage=_RECORD_USAGE, group="capture")
def _cmd_record(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError(f"Usage: {_RECORD_USAGE}")

    sub = parts[1]
    capture: CaptureBuffer = adapter._capture

    if sub.lower() == "stop":
        if not capture.recording:
            raise adapter.DAPAdapterError("No active recording")
        transcript, _entries = capture.stop()
        return ConsoleResult(
            f"Recording stopped. 'record save' writes it to {_default_file_name(capture.finished)}.\n"
            f"{transcript}"
        )

    if sub.lower() == "save":
        rest = expression.strip().split(None, 2)
        return _save_recording(adapter, capture, rest[2] if len(rest) > 2 else None)

    if capture.recording:
        raise adapter.DAPAdapterError(
            f"Already recording '{capture.action}'. Use 'record stop' first."
        )

    action_name = sub
    runner = adapter._require_runner_locked()
    scan_id = runner.current_state.scan_id
    timestamp = runner.current_state.timestamp

    warnings: list[str] = []
    if runner.forces:
        forced = ", ".join(sorted(runner.forces.keys()))
        warnings.append(f"Warning: active forces at record start: {forced}")

    program_text = getattr(adapter, "_program_path", None)
    program_path = Path(program_text) if program_text else None
    realtime = runner.time_mode == TimeMode.REALTIME
    header = RecordingHeader(
        program_sha256=program_sha256(program_path) if program_path is not None else None,
        pyrung_version=pyrung_version(),
        scan_time=None if realtime else runner.dt,
        realtime=realtime,
        harness=adapter._harness is not None,
    )
    capture.start(action_name, scan_id, timestamp, header=header, program_path=program_path)

    text = f"Recording '{action_name}' from scan {scan_id}"
    if warnings:
        text += "\n" + "\n".join(warnings)
    return ConsoleResult(text)


def _default_file_name(finished: FinishedRecording | None) -> str:
    action = finished.header.action if finished is not None else None
    return f"{action}{RECORDING_SUFFIX}"


def _save_recording(adapter: Any, capture: CaptureBuffer, file_text: str | None) -> ConsoleResult:
    stopped = ""
    if capture.recording:
        capture.stop()
        stopped = "Recording stopped. "
    finished = capture.finished
    if finished is None:
        raise adapter.DAPAdapterError("Nothing to save. Start a recording with 'record <action>'.")
    target = resolve_session_path(adapter, file_text or _default_file_name(finished))
    if not target.suffix:
        target = target.with_name(target.name + RECORDING_SUFFIX)
    if not target.parent.is_dir():
        raise adapter.DAPAdapterError(f"Folder not found: {target.parent}")
    replaced = " (replaced the file that was there)" if target.exists() else ""
    try:
        target.write_text(finished.text(target.parent), encoding="utf-8")
    except OSError as exc:
        raise adapter.DAPAdapterError(f"Cannot write {target}: {exc}") from exc
    return ConsoleResult(
        f"{stopped}Saved '{finished.header.action}' ({len(finished.commands)} command(s))"
        f" to {target}{replaced}"
    )


def resolve_session_path(adapter: Any, text: str) -> Path:
    """Read a file path typed at the console, taking a relative one from the program's folder.

    The debugger's own working folder depends on how it was started, so the program's
    folder is the one place ``record save`` and ``replay`` both know.
    """
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    path = Path(text).expanduser()
    program_text = getattr(adapter, "_program_path", None)
    if not path.is_absolute() and program_text:
        path = Path(program_text).parent / path
    return path.resolve()


# ---------------------------------------------------------------------------
# Assert verb
# ---------------------------------------------------------------------------

_ASSERT_USAGE = "Usage: assert <tag> == <value>"


@register(
    "assert",
    usage="assert <tag> == <value>",
    group="capture",
    slots=(Slot(kind="tag", label="tag"), Slot(kind="value", label="value", keyword="==")),
)
def _cmd_assert(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split(None, 1)
    if len(parts) < 2:
        raise adapter.DAPAdapterError(_ASSERT_USAGE)
    runner = adapter._require_runner_locked()
    state = runner.current_state
    parsed = parse_known_condition(adapter, state, parts[1])
    if not isinstance(parsed, Compare) or parsed.op != "==" or parsed.right is None:
        raise adapter.DAPAdapterError(_ASSERT_USAGE)

    name = parsed.tag.name
    expected = parsed.right.value
    actual = adapter._state_value_for_reference(state, name)

    if isinstance(actual, float) and isinstance(expected, int | float):
        matched = math.isclose(actual, expected, rel_tol=REAL_TOLERANCE, abs_tol=REAL_TOLERANCE)
    else:
        matched = condition_predicate(adapter, parsed)(state)
    if not matched:
        raise adapter.DAPAdapterError(f"assert failed: {name} = {actual!r}, expected {expected!r}")
    return ConsoleResult(f"assert ok: {name} = {actual!r}")


# ---------------------------------------------------------------------------
# Replay verb
# ---------------------------------------------------------------------------


_REPLAY_USAGE = "replay <filepath> [--harness current|recorded|off]"


@register("replay", usage=_REPLAY_USAGE, group="capture")
def _cmd_replay(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split(None, 1)
    if len(parts) < 2:
        raise adapter.DAPAdapterError(f"Usage: {_REPLAY_USAGE}")

    filepath_text, harness_mode = _parse_replay_args(parts[1].strip())
    filepath = resolve_session_path(adapter, filepath_text)
    if not filepath.is_file():
        raise adapter.DAPAdapterError(f"File not found: {filepath}")
    return replay_file(adapter, filepath, harness_mode)


def replay_file(adapter: Any, filepath: Path, harness_mode: str | None) -> ConsoleResult:
    """Run every command of a transcript, and stop at the first that fails.

    The console's ``replay`` and pytest both replay through here. A failure raises
    ``DAPAdapterError`` with ``replay_line`` and ``replay_reason`` set. With no
    *harness_mode*, a recording made with the harness off replays with no harness.
    """
    from pyrung.dap.console import dispatch, queue_stop_for_error, scan_mark, scans_ran_since

    lines = filepath.read_text(encoding="utf-8").splitlines()
    try:
        header = parse_header(lines)
    except RecordingFormatError as exc:
        error = adapter.DAPAdapterError(f"Replay error at line {exc.line}: {exc}")
        error.replay_line = exc.line
        error.replay_reason = str(exc)
        raise error from exc
    if harness_mode is None:
        harness_mode = "off" if header.harness is False else "current"
    commands = _replay_commands(lines, harness_mode=harness_mode)

    scan_time_note = _apply_scan_time(adapter, header)
    _configure_replay_harness(adapter, harness_mode)

    executed = 0
    hit_bp = False
    last_stop: dict[str, Any] | None = None
    start = scan_mark(adapter)
    for lineno, cmd in commands:
        try:
            result = dispatch(adapter, cmd)
        except Exception as exc:
            error = adapter.DAPAdapterError(
                f"Replay error at line {lineno}: {exc}\n"
                f"Executed {executed}/{len(commands)} command(s)"
            )
            error.stop_queued = getattr(exc, "stop_queued", False)
            error.replay_line = lineno
            error.replay_reason = str(exc)
            if last_stop is not None or scans_ran_since(adapter, start):
                # A "step" stop, never a reload's "entry", which would clear the editor's
                # decorations.
                queue_stop_for_error(adapter, error)
            raise error from exc
        executed += 1
        for event_name, body in result.events:
            if event_name == "stopped":
                last_stop = body
        if result.hit_breakpoint:
            hit_bp = True
            break

    scan_id = adapter._current_scan_id
    remaining = len(commands) - executed
    suffix = ""
    if hit_bp:
        suffix = f" (breakpoint, {remaining} command(s) remaining)"
        last_stop = adapter._stopped_body("breakpoint")
    # One stop at the end tells the client where the replay left the scan.
    events: list[tuple[str, dict[str, Any] | None]] = (
        [("stopped", last_stop)] if last_stop is not None else []
    )
    text = (
        f"Replayed {executed}/{len(commands)} command(s), now at scan {scan_id}"
        f" [harness={harness_mode}]{suffix}"
    )
    if scan_time_note is not None:
        text = f"{scan_time_note}\n{text}"
    return ConsoleResult(text, events=events, hit_breakpoint=hit_bp)


def _apply_scan_time(adapter: Any, header: RecordingHeader) -> str | None:
    """Give the PLC the recording's scan time, because its timers count by it.

    Only a PLC that has run no scan takes it. A change drops the scan history, and a
    replay in the middle of a session must leave that history alone.
    """
    runner = adapter._require_runner_locked()
    if header.realtime:
        mode, dt = TimeMode.REALTIME, runner.dt
    elif header.scan_time is not None:
        mode, dt = TimeMode.FIXED_STEP, header.scan_time
    else:
        return None
    if runner.time_mode == mode and (mode == TimeMode.REALTIME or runner.dt == dt):
        return None
    recorded = _scan_time_text(mode, dt)
    if runner.current_state.scan_id != 0 or adapter._scan_gen is not None:
        current = _scan_time_text(runner.time_mode, runner.dt)
        return (
            f"Note: the recording ran at a scan time of {recorded}. "
            f"This PLC has already scanned, so the replay keeps its {current}."
        )
    runner._set_time_mode(mode, dt=dt)
    return f"Scan time set to {recorded}, as recorded"


def _scan_time_text(mode: TimeMode, dt: float) -> str:
    return "realtime" if mode == TimeMode.REALTIME else f"{dt!r} s"


def _parse_replay_args(rest: str) -> tuple[str, str | None]:
    modes = {"current", "recorded", "off"}
    tokens = rest.split()
    mode: str | None = None
    if len(tokens) >= 2 and tokens[-2] == "--harness" and tokens[-1] in modes:
        mode = tokens[-1]
        return " ".join(tokens[:-2]), mode
    if len(tokens) >= 2 and tokens[-1] in modes:
        mode = tokens[-1]
        return " ".join(tokens[:-1]), mode
    return rest, mode


#: Bench-link verbs change the harness wiring; replayed only with the current harness.
_LINK_VERBS = frozenset({"link", "unlink", "restore", "links"})


def _replay_commands(lines: list[str], *, harness_mode: str) -> list[tuple[int, str]]:
    commands: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parsed = parse_provenance_line(stripped)
        if parsed.source is not None and parsed.source.startswith("harness:"):
            if harness_mode in {"current", "off"}:
                continue
            commands.append((i + 1, parsed.command))
            continue
        verb = parsed.command.split(maxsplit=1)[0].lower()
        if verb in _LINK_VERBS and harness_mode != "current":
            # Recorded feedback already reflects the wiring, and "off" means no harness.
            continue
        commands.append((i + 1, parsed.command))
    return commands


def _configure_replay_harness(adapter: Any, harness_mode: str) -> None:
    if harness_mode == "current":
        if adapter._harness is None:
            from pyrung.dap.harness_console import try_auto_install

            try_auto_install(adapter)
        return

    from pyrung.dap.harness_console import uninstall_harness

    uninstall_harness(adapter)
