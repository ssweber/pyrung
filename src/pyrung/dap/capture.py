"""Session capture buffer for the DAP console.

Passive recording layer: every console command evaluated while a
recording is active gets appended to the buffer with its scan_id
and timestamp.  ``record ACTION`` / ``record stop`` bracket a
named capture.  On stop the buffer yields a plain-text transcript
ready for replay or condensation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pyrung.dap.console import ConsoleResult, register


@dataclass(frozen=True)
class CaptureEntry:
    """A single recorded console command."""

    command: str
    scan_id: int | None
    timestamp: float
    provenance: str = "console"


@dataclass
class CaptureBuffer:
    """Accumulates console commands between ``record`` / ``record stop``."""

    action: str | None = None
    entries: list[CaptureEntry] = field(default_factory=list)
    start_scan_id: int | None = None
    start_timestamp: float = 0.0

    @property
    def recording(self) -> bool:
        return self.action is not None

    def start(self, action: str, scan_id: int | None, timestamp: float) -> None:
        self.action = action
        self.entries = []
        self.start_scan_id = scan_id
        self.start_timestamp = timestamp

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
        """Stop recording and return ``(transcript, raw_entries)``."""
        lines = [f"# action: {self.action}"]
        for entry in self.entries:
            if entry.provenance == "console":
                lines.append(entry.command)
            else:
                lines.append(f"{entry.provenance}: {entry.command}")
        raw = list(self.entries)
        self.action = None
        self.entries = []
        return "\n".join(lines) + "\n", raw

    def reset(self) -> None:
        self.action = None
        self.entries = []
        self.start_scan_id = None
        self.start_timestamp = 0.0


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


@register("record", usage="record <action> | record stop", group="capture")
def _cmd_record(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: record <action> | record stop")

    sub = parts[1]
    capture: CaptureBuffer = adapter._capture

    if sub.lower() == "stop":
        if not capture.recording:
            raise adapter.DAPAdapterError("No active recording")
        action = capture.action or "capture"
        start_scan_id = capture.start_scan_id
        _raw_transcript, entries = capture.stop()

        from pyrung.dap.condenser import condense_capture
        from pyrung.dap.miner import mine_candidates

        runner = adapter._require_runner_locked()
        condensed = condense_capture(
            action,
            entries,
            runner,
            start_scan_id=start_scan_id,
        )

        suppressed = frozenset(getattr(adapter, "_miner_suppressed", set()))
        candidates = mine_candidates(
            action,
            entries,
            runner,
            start_scan_id=start_scan_id,
            suppressed=suppressed,
        )
        adapter._miner_candidates = candidates

        suffix = ""
        if candidates:
            suffix = f"\n{len(candidates)} candidate invariant(s) — use `candidates` to review"
        return ConsoleResult(f"Recording stopped.\n{condensed.transcript}{suffix}")

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

    capture.start(action_name, scan_id, timestamp)

    text = f"Recording '{action_name}' from scan {scan_id}"
    if warnings:
        text += "\n" + "\n".join(warnings)
    return ConsoleResult(text)


# ---------------------------------------------------------------------------
# Replay verb
# ---------------------------------------------------------------------------


@register("replay", usage="replay <filepath> [--harness current|recorded|off]", group="capture")
def _cmd_replay(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split(None, 1)
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: replay <filepath> [--harness current|recorded|off]")

    filepath_text, harness_mode = _parse_replay_args(parts[1].strip())
    filepath = Path(filepath_text).expanduser().resolve()
    if not filepath.is_file():
        raise adapter.DAPAdapterError(f"File not found: {filepath}")

    lines = filepath.read_text(encoding="utf-8").splitlines()
    commands = _replay_commands(lines, harness_mode=harness_mode)

    _configure_replay_harness(adapter, harness_mode)

    from pyrung.dap.console import dispatch

    executed = 0
    hit_bp = False
    for lineno, cmd in commands:
        try:
            result = dispatch(adapter, cmd)
        except Exception as exc:
            return ConsoleResult(
                f"Replay error at line {lineno}: {exc}\n"
                f"Executed {executed}/{len(commands)} command(s)"
            )
        executed += 1
        for event_name, _body in result.events:
            if event_name == "stopped":
                hit_bp = True
                break
        if hit_bp:
            break

    scan_id = adapter._current_scan_id
    remaining = len(commands) - executed
    suffix = ""
    if hit_bp:
        suffix = f" (breakpoint, {remaining} command(s) remaining)"
    events = [("stopped", adapter._stopped_body("step"))] if hit_bp else []
    return ConsoleResult(
        f"Replayed {executed}/{len(commands)} command(s), now at scan {scan_id}"
        f" [harness={harness_mode}]{suffix}",
        events=events,
    )


def _parse_replay_args(rest: str) -> tuple[str, str]:
    modes = {"current", "recorded", "off"}
    tokens = rest.split()
    mode = "current"
    if len(tokens) >= 2 and tokens[-2] == "--harness" and tokens[-1] in modes:
        mode = tokens[-1]
        return " ".join(tokens[:-2]), mode
    if len(tokens) >= 2 and tokens[-1] in modes:
        mode = tokens[-1]
        return " ".join(tokens[:-1]), mode
    return rest, mode


def _replay_commands(lines: list[str], *, harness_mode: str) -> list[tuple[int, str]]:
    from pyrung.dap.condenser import parse_provenance_line

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
