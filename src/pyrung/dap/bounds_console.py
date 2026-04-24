"""Bounds-violation integration for the DAP debug session.

Emits ``[bounds]`` messages to the debug console when tags exceed their
declared ``min=/max=`` or ``choices=`` constraints, and provides the
``bounds`` / ``bounds clear`` console verbs for session-level review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pyrung.core.bounds import BoundsViolation
from pyrung.dap.console import ConsoleResult, register


@dataclass
class _AccEntry:
    violation: BoundsViolation
    scan_id: int
    count: int


def _format_violation(v: BoundsViolation, choices_map: dict[Any, str] | None) -> str:
    if v.kind == "range":
        lo = v.constraint.min
        hi = v.constraint.max
        if lo is not None and hi is not None:
            return f"[bounds] {v.tag_name}={v.value!r} (range: {lo}–{hi})"
        if lo is not None:
            return f"[bounds] {v.tag_name}={v.value!r} (min: {lo})"
        return f"[bounds] {v.tag_name}={v.value!r} (max: {hi})"
    if choices_map is not None:
        return f"[bounds] {v.tag_name}={v.value!r} (choices: {choices_map})"
    keys = v.constraint.choices_keys
    assert keys is not None
    return f"[bounds] {v.tag_name}={v.value!r} (choices: {set(keys)})"


def emit_bounds_violations(adapter: Any) -> None:
    """Check the runner for bounds violations and emit to the debug console.

    Also accumulates into the session-level ``_bounds_accumulator``.
    """
    runner = adapter._runner
    if runner is None:
        return
    violations = runner.bounds_violations
    if not violations:
        return

    acc: dict[str, _AccEntry] = adapter._bounds_accumulator
    scan_id = runner.current_state.scan_id
    tags_by_name = runner._known_tags_by_name

    parts: list[str] = []
    for tag_name, v in violations.items():
        tag_def = tags_by_name.get(tag_name)
        choices_map = tag_def.choices if tag_def else None
        parts.append(_format_violation(v, choices_map))

        entry = acc.get(tag_name)
        if entry is None:
            acc[tag_name] = _AccEntry(violation=v, scan_id=scan_id, count=1)
        else:
            entry.violation = v
            entry.scan_id = scan_id
            entry.count += 1

    text = "\n".join(parts) + "\n"
    adapter._enqueue_internal_event("output", {"category": "console", "output": text})


# ---------------------------------------------------------------------------
# Console verb
# ---------------------------------------------------------------------------


@register("bounds", usage="bounds [clear]", group="data")
def _cmd_bounds(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()

    if len(parts) >= 2 and parts[1].lower() == "clear":
        adapter._bounds_accumulator.clear()
        return ConsoleResult("Bounds violation history cleared.")

    acc: dict[str, _AccEntry] = adapter._bounds_accumulator
    if not acc:
        return ConsoleResult("No bounds violations recorded.")

    runner = adapter._runner
    tags_by_name = runner._known_tags_by_name if runner else {}

    lines: list[str] = []
    for tag_name in sorted(acc):
        entry = acc[tag_name]
        v = entry.violation
        tag_def = tags_by_name.get(tag_name)
        choices_map = tag_def.choices if tag_def else None
        detail = _format_violation(v, choices_map).removeprefix("[bounds] ")
        lines.append(f"  {detail}  (scan {entry.scan_id}, {entry.count}x)")

    return ConsoleResult(f"{len(acc)} tag(s) violated bounds:\n" + "\n".join(lines))
