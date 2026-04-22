"""Causal-minimum condenser for DAP console captures.

The capture layer records exactly what the console evaluated.  The
condenser turns that raw buffer into the v1 reproducer shape: keep the
mutating commands that matter, drop observation-only commands, and shrink
``run``/``step`` spans to the last observed relevant transition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pyrung.core.physical import parse_duration
from pyrung.dap.capture import CaptureEntry

CommandKind = Literal["mutation", "span", "query", "note", "unknown"]
SpanKind = Literal["run", "step"]


@dataclass(frozen=True)
class CommandInfo:
    """Parsed console command summary used by the condenser."""

    kind: CommandKind
    verb: str
    tag: str | None = None
    value: str | None = None
    span_kind: SpanKind | None = None
    span_scans: int | None = None
    span_duration_ms: int | None = None
    run_uses_duration: bool = False


@dataclass(frozen=True)
class ProvenanceLine:
    """A transcript line split into provenance and console command."""

    source: str | None
    command: str


@dataclass
class ReductionStats:
    """Small accounting payload for the reduction report."""

    raw_entries: int = 0
    kept_lines: int = 0
    dropped_queries: int = 0
    dropped_spans: int = 0
    shrunk_spans: int = 0
    coalesced_fumbles: int = 0

    def report(self) -> str:
        parts = [
            f"kept {self.kept_lines}/{self.raw_entries}",
            f"dropped {self.dropped_queries} quer{'y' if self.dropped_queries == 1 else 'ies'}",
            f"dropped {self.dropped_spans} idle span{'s' if self.dropped_spans != 1 else ''}",
            f"shrunk {self.shrunk_spans} span{'s' if self.shrunk_spans != 1 else ''}",
            f"coalesced {self.coalesced_fumbles} fumble{'s' if self.coalesced_fumbles != 1 else ''}",
        ]
        return "; ".join(parts)


@dataclass(frozen=True)
class CondensedCapture:
    """Result of condensing a raw capture."""

    action: str
    transcript: str
    report: str
    lines: tuple[str, ...]
    stats: ReductionStats = field(compare=False)


_QUERY_VERBS = frozenset(
    {
        "cause",
        "dataview",
        "downstream",
        "effect",
        "help",
        "log",
        "monitor",
        "recovers",
        "simplified",
        "unmonitor",
        "upstream",
    }
)


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


def classify_command(command: str) -> CommandInfo:
    """Classify a successful console command for condensation."""

    parts = command.strip().split()
    if not parts:
        return CommandInfo(kind="query", verb="")

    verb = parts[0].lower()

    if verb == "patch":
        tag, value = _tag_value(parts, command)
        return CommandInfo(kind="mutation", verb=verb, tag=tag, value=value)
    if verb == "force":
        tag, value = _tag_value(parts, command)
        return CommandInfo(kind="mutation", verb=verb, tag=tag, value=value)
    if verb == "unforce":
        tag = parts[1] if len(parts) >= 2 else None
        return CommandInfo(kind="mutation", verb=verb, tag=tag)
    if verb == "clear_forces":
        return CommandInfo(kind="mutation", verb=verb)

    if verb == "step":
        scans = 1
        if len(parts) >= 2:
            try:
                scans = int(parts[1])
            except ValueError:
                return CommandInfo(kind="unknown", verb=verb)
        return CommandInfo(kind="span", verb=verb, span_kind="step", span_scans=scans)

    if verb == "run":
        if len(parts) < 2:
            return CommandInfo(kind="unknown", verb=verb)
        spec = _run_spec(parts)
        try:
            scans = int(spec)
        except ValueError:
            try:
                ms = parse_duration(spec)
            except ValueError:
                return CommandInfo(kind="unknown", verb=verb)
            return CommandInfo(
                kind="span",
                verb=verb,
                span_kind="run",
                span_duration_ms=ms,
                run_uses_duration=True,
            )
        return CommandInfo(kind="span", verb=verb, span_kind="run", span_scans=scans)

    if verb == "note":
        return CommandInfo(kind="note", verb=verb)

    if verb == "harness" and len(parts) >= 2 and parts[1].lower() == "status":
        return CommandInfo(kind="query", verb=verb)
    if verb in _QUERY_VERBS:
        return CommandInfo(kind="query", verb=verb)

    return CommandInfo(kind="unknown", verb=verb)


def condense_capture(
    action: str,
    entries: list[CaptureEntry],
    runner: Any,
    *,
    start_scan_id: int | None = None,
) -> CondensedCapture:
    """Condense a raw capture into a replayable causal-minimum transcript."""

    stats = ReductionStats(raw_entries=len(entries))
    relevant_tags = _relevant_tags(entries, runner)

    raw_cursor = _initial_scan(start_scan_id, entries, runner)
    segment_start_scan = raw_cursor
    pending_mutations: list[CaptureEntry] = []
    pending_harness: list[CaptureEntry] = []
    output: list[str] = []

    for entry in entries:
        if _is_harness_provenance(entry.provenance):
            pending_harness.append(entry)
            continue

        info = classify_command(entry.command)

        if info.kind == "query":
            stats.dropped_queries += 1
            continue

        if info.kind == "note":
            reduced, coalesced = coalesce_fumbles(
                pending_mutations, runner, segment_start_scan,
            )
            output.extend(_format_entry(e) for e in reduced)
            stats.coalesced_fumbles += coalesced
            pending_mutations = []
            output.append(_format_note(entry.command))
            continue

        if info.kind == "span":
            span_end = entry.scan_id if entry.scan_id is not None else raw_cursor
            if span_end is None:
                span_end = raw_cursor
            if raw_cursor is None:
                raw_cursor = span_end

            kept_end = _last_relevant_transition_scan(
                runner,
                raw_cursor,
                span_end,
                relevant_tags,
            )
            if kept_end is None:
                stats.dropped_spans += 1
                raw_cursor = span_end
                pending_harness = [
                    h for h in pending_harness if h.scan_id is None or h.scan_id > span_end
                ]
                continue

            reduced, coalesced = coalesce_fumbles(
                pending_mutations,
                runner,
                segment_start_scan,
            )
            output.extend(_format_entry(e) for e in reduced)
            stats.coalesced_fumbles += coalesced
            pending_mutations = []

            kept_scans = max(0, kept_end - raw_cursor)
            original_scans = max(0, span_end - raw_cursor)
            if kept_scans < original_scans:
                stats.shrunk_spans += 1

            output.extend(
                _span_lines(
                    info,
                    scans=kept_scans,
                    dt_seconds=_dt_seconds(runner),
                    span_start=raw_cursor,
                    kept_end=kept_end,
                    harness_entries=pending_harness,
                )
            )
            pending_harness = [
                h for h in pending_harness if h.scan_id is None or h.scan_id > span_end
            ]
            raw_cursor = span_end
            segment_start_scan = raw_cursor
            continue

        if info.kind == "mutation":
            pending_mutations.append(entry)
            continue

        reduced, coalesced = coalesce_fumbles(
            pending_mutations,
            runner,
            segment_start_scan,
        )
        output.extend(_format_entry(e) for e in reduced)
        stats.coalesced_fumbles += coalesced
        pending_mutations = []
        output.append(_format_entry(entry))
        segment_start_scan = entry.scan_id if entry.scan_id is not None else raw_cursor

    reduced, coalesced = coalesce_fumbles(pending_mutations, runner, segment_start_scan)
    output.extend(_format_entry(e) for e in reduced)
    stats.coalesced_fumbles += coalesced

    stats.kept_lines = len(output)
    lines = [f"# action: {action}", f"# reduction: {stats.report()}", *output]
    transcript = "\n".join(lines) + "\n"
    return CondensedCapture(
        action=action,
        transcript=transcript,
        report=stats.report(),
        lines=tuple(lines),
        stats=stats,
    )


def coalesce_fumbles(
    entries: list[CaptureEntry],
    runner: Any,
    segment_start_scan: int | None,
) -> tuple[list[CaptureEntry], int]:
    """Coalesce v1 fumbles inside one no-transition mutation segment."""

    if not entries:
        return [], 0

    result: list[CaptureEntry] = []
    patch_index: dict[str, int] = {}
    force_index: dict[str, int] = {}
    force_touched: set[str] = set()

    for entry in entries:
        info = classify_command(entry.command)
        if info.verb == "patch" and info.tag is not None:
            existing = patch_index.get(info.tag)
            if existing is not None:
                result[existing] = entry
            else:
                patch_index[info.tag] = len(result)
                result.append(entry)
            continue
        if info.verb in {"force", "unforce"} and info.tag is not None:
            force_touched.add(info.tag)
            existing = force_index.get(info.tag)
            if existing is not None:
                result[existing] = entry
            else:
                force_index[info.tag] = len(result)
                result.append(entry)
            continue
        result.append(entry)

    original_tags = _state_tags_at(runner, segment_start_scan)
    kept: list[CaptureEntry] = []
    for entry in result:
        info = classify_command(entry.command)
        if info.verb == "patch" and info.tag is not None:
            original = original_tags.get(info.tag)
            if _value_matches_original(info.value, original):
                continue
        if info.verb == "unforce" and info.tag in force_touched:
            # A force/unforce pair in one no-transition segment has no
            # lasting replay effect for v1.  Lone ``unforce`` commands are
            # preserved because active forces at record start are only warned.
            if (
                sum(
                    1
                    for e in entries
                    if classify_command(e.command).tag == info.tag
                    and classify_command(e.command).verb in {"force", "unforce"}
                )
                > 1
            ):
                continue
        kept.append(entry)

    return kept, len(entries) - len(kept)


def _tag_value(parts: list[str], command: str) -> tuple[str | None, str | None]:
    if len(parts) < 3:
        return None, None
    split = command.strip().split(None, 2)
    return split[1], split[2] if len(split) >= 3 else None


def _run_spec(parts: list[str]) -> str:
    if len(parts) >= 3 and _looks_like_split_duration(parts[1], parts[2]):
        return f"{parts[1]}{parts[2]}"
    return parts[1]


def _looks_like_split_duration(value: str, unit: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return unit.lower() in {"ms", "s", "m", "min", "h", "d"}


def _is_harness_provenance(provenance: str) -> bool:
    return provenance.startswith("harness:")


def _format_entry(entry: CaptureEntry) -> str:
    if entry.provenance == "console":
        return entry.command
    return f"{entry.provenance}: {entry.command}"


def _format_note(command: str) -> str:
    rest = command.strip()
    idx = rest.find(" ")
    text = rest[idx:].strip() if idx >= 0 else ""
    return f"# note: {text}"


def _dt_seconds(runner: Any) -> float:
    return float(getattr(runner, "_dt", 0.010))


def _initial_scan(
    start_scan_id: int | None,
    entries: list[CaptureEntry],
    runner: Any,
) -> int:
    if start_scan_id is not None:
        return start_scan_id
    for entry in entries:
        if entry.scan_id is not None:
            return entry.scan_id
    history = getattr(runner, "history", None)
    if history is not None:
        return int(getattr(history, "oldest_scan_id", 0))
    return 0


def _state_tags_at(runner: Any, scan_id: int | None) -> dict[str, Any]:
    if scan_id is None:
        return {}
    history = getattr(runner, "history", None)
    if history is None or not history.contains(scan_id):
        return {}
    return dict(history.at(scan_id).tags)


def _value_matches_original(raw_value: str | None, original: Any) -> bool:
    if raw_value is None:
        return False
    parsed = _parse_literalish(raw_value)
    return parsed == original


def _parse_literalish(raw_value: str) -> Any:
    low = raw_value.strip().lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in {"none", "null"}:
        return None
    try:
        return int(raw_value)
    except ValueError:
        pass
    try:
        return float(raw_value)
    except ValueError:
        pass
    return raw_value.strip("\"'")


def _relevant_tags(entries: list[CaptureEntry], runner: Any) -> set[str]:
    touched: set[str] = set()
    harness_touched: set[str] = set()

    for entry in entries:
        info = classify_command(entry.command)
        if info.tag is None:
            continue
        if _is_harness_provenance(entry.provenance):
            harness_touched.add(info.tag)
        elif info.kind == "mutation":
            touched.add(info.tag)

    relevant = set(touched)
    graph = _program_graph(runner)
    if graph is not None:
        for tag in list(touched):
            relevant.update(graph.downstream_slice(tag))

    # Harness-caused feedback transitions are part of the observed causal
    # span in v1, even when the feedback tag is not consumed by the PDG yet.
    relevant.update(harness_touched)
    return relevant


def _program_graph(runner: Any) -> Any | None:
    ensure = getattr(runner, "_ensure_pdg", None)
    if callable(ensure):
        try:
            return ensure()
        except Exception:
            return None
    program = getattr(runner, "program", None)
    if program is None:
        return None
    try:
        from pyrung.core.analysis.pdg import build_program_graph

        return build_program_graph(program)
    except Exception:
        return None


def _last_relevant_transition_scan(
    runner: Any,
    span_start: int,
    span_end: int,
    relevant_tags: set[str],
) -> int | None:
    if span_end <= span_start or not relevant_tags:
        return None

    history = getattr(runner, "history", None)
    if history is None:
        return None

    last: int | None = None
    for scan_id in range(span_start + 1, span_end + 1):
        if not history.contains(scan_id - 1) or not history.contains(scan_id):
            continue
        before = history.at(scan_id - 1).tags
        after = history.at(scan_id).tags
        for tag in relevant_tags:
            if before.get(tag) != after.get(tag):
                last = scan_id
                break
    return last


def _span_lines(
    info: CommandInfo,
    *,
    scans: int,
    dt_seconds: float,
    span_start: int,
    kept_end: int,
    harness_entries: list[CaptureEntry],
) -> list[str]:
    if scans <= 0:
        return []

    lines: list[str] = []
    cursor = span_start
    for entry in sorted(
        (h for h in harness_entries if h.scan_id is not None and h.scan_id <= kept_end),
        key=lambda e: (e.scan_id if e.scan_id is not None else span_start, e.command),
    ):
        event_scan = max(cursor, min(entry.scan_id or cursor, kept_end))
        before = event_scan - cursor
        if before > 0:
            lines.append(_format_span_fragment(info, before, dt_seconds))
            cursor = event_scan
        lines.append(_format_entry(entry))

    remaining = kept_end - cursor
    if remaining > 0:
        lines.append(_format_span_fragment(info, remaining, dt_seconds))
    return lines


def _format_span_fragment(info: CommandInfo, scans: int, dt_seconds: float) -> str:
    if info.span_kind == "step":
        return f"step {scans}"
    if info.run_uses_duration:
        ms = scans * dt_seconds * 1000.0
        return f"run {_format_ms(ms)}ms"
    return f"run {scans}"


def _format_ms(ms: float) -> str:
    rounded = round(ms)
    if abs(ms - rounded) < 1e-9:
        return str(int(rounded))
    return f"{ms:.3f}".rstrip("0").rstrip(".")


__all__ = [
    "CommandInfo",
    "CondensedCapture",
    "ProvenanceLine",
    "ReductionStats",
    "classify_command",
    "coalesce_fumbles",
    "condense_capture",
    "parse_provenance_line",
]
