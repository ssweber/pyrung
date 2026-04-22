"""Session capture buffer for the DAP console.

Passive recording layer: every console command evaluated while a
recording is active gets appended to the buffer with its scan_id
and timestamp.  ``record ACTION`` / ``record stop`` bracket a
named capture.  On stop the buffer yields a plain-text transcript
ready for replay or condensation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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

    def append(self, command: str, scan_id: int | None, timestamp: float) -> None:
        if self.recording:
            self.entries.append(CaptureEntry(command, scan_id, timestamp))

    def stop(self) -> tuple[str, list[CaptureEntry]]:
        """Stop recording and return ``(transcript, raw_entries)``."""
        lines = [f"# action: {self.action}"]
        for entry in self.entries:
            lines.append(entry.command)
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

_CAPTURE_EXCLUDED = frozenset({"record", "help"})


def capture_hook(adapter: Any, verb: str, expression: str) -> None:
    """Called by ``dispatch()`` after every successful command."""
    capture: CaptureBuffer | None = getattr(adapter, "_capture", None)
    if capture is None or not capture.recording or verb in _CAPTURE_EXCLUDED:
        return
    runner = getattr(adapter, "_runner", None)
    timestamp = runner.current_state.timestamp if runner else 0.0
    scan_id = getattr(adapter, "_current_scan_id", None)
    capture.append(expression.strip(), scan_id, timestamp)


@register("record", usage="record <action> | record stop")
def _cmd_record(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: record <action> | record stop")

    sub = parts[1]
    capture: CaptureBuffer = adapter._capture

    if sub.lower() == "stop":
        if not capture.recording:
            raise adapter.DAPAdapterError("No active recording")
        transcript, _entries = capture.stop()
        return ConsoleResult(f"Recording stopped.\n{transcript}")

    if capture.recording:
        raise adapter.DAPAdapterError(
            f"Already recording '{capture.action}'. Use 'record stop' first."
        )

    action_name = sub
    runner = adapter._require_runner_locked()
    scan_id = adapter._current_scan_id
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
