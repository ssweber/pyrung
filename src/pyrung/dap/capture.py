"""Session capture buffer for the DAP console.

Passive recording layer: every console command evaluated while a
recording is active gets appended to the buffer with its scan_id
and timestamp.  ``record ACTION`` / ``record stop`` bracket a
named capture.  On stop the buffer yields a plain-text transcript
that ``replay`` runs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pyrung.dap.console import ConsoleResult, register
from pyrung.dap.expressions import Compare, Literal, _resolve_choice_label
from pyrung.dap.grammar import Slot
from pyrung.dap.handlers.stack_variables_evaluate import condition_predicate, parse_known_condition

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
        transcript, _entries = capture.stop()
        return ConsoleResult(f"Recording stopped.\n{transcript}")

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
    typed = parsed.right.value
    expected = typed
    if isinstance(typed, str):
        label_value = _resolve_choice_label(runner.tags, name, typed)
        if label_value is not None:
            expected = label_value
    actual = adapter._state_value_for_reference(state, name)

    if isinstance(actual, float) and _is_number(expected):
        matched = math.isclose(actual, expected, rel_tol=REAL_TOLERANCE, abs_tol=REAL_TOLERANCE)
    else:
        check = Compare(tag=parsed.tag, op="==", right=Literal(value=expected))
        matched = condition_predicate(adapter, check)(state)
    if not matched:
        raise adapter.DAPAdapterError(f"assert failed: {name} = {actual!r}, expected {typed!r}")
    return ConsoleResult(f"assert ok: {name} = {actual!r}")


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


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
    last_stop: dict[str, Any] | None = None
    for lineno, cmd in commands:
        try:
            result = dispatch(adapter, cmd)
        except Exception as exc:
            return ConsoleResult(
                f"Replay error at line {lineno}: {exc}\n"
                f"Executed {executed}/{len(commands)} command(s)"
            )
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
    return ConsoleResult(
        f"Replayed {executed}/{len(commands)} command(s), now at scan {scan_id}"
        f" [harness={harness_mode}]{suffix}",
        events=events,
        hit_breakpoint=hit_bp,
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
