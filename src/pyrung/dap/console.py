"""Console command dispatcher for the DAP REPL.

Each verb is registered in a module-level registry. The dispatcher
looks up the verb and delegates. All handlers run under the adapter's
``_state_lock`` — they must NOT call handler entry points that also
acquire it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pyrung.dap import execution_flow

# grammar.py reads this registry, but only from inside command_grammar() — so
# importing it here at module level does not cycle.
from pyrung.dap.grammar import Slot

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]


@dataclass(frozen=True)
class ConsoleResult:
    """Return value from a console command."""

    text: str
    events: list[tuple[str, dict[str, Any] | None]] = field(default_factory=list)


@dataclass(frozen=True)
class CommandEntry:
    """One registered console command."""

    handler: Callable[..., ConsoleResult]
    usage: str = ""
    group: str = ""
    hint: str = ""
    #: Declared argument grammar. ``None`` means "derive it from ``usage``" —
    #: right for most commands. Declare it when the prose is ambiguous, and read
    #: the result through :func:`pyrung.dap.grammar.command_grammar`.
    slots: tuple[Slot, ...] | None = None


_REGISTRY: dict[str, CommandEntry] = {}


def register(
    verb: str,
    *,
    usage: str = "",
    group: str = "",
    hint: str = "",
    slots: tuple[Slot, ...] | None = None,
) -> Callable[..., Any]:
    def decorator(fn: Callable[..., ConsoleResult]) -> Callable[..., ConsoleResult]:
        _REGISTRY[verb] = CommandEntry(fn, usage, group, hint, slots)
        return fn

    return decorator


_GROUP_ORDER = ["execution", "data", "analysis", "capture", "review", ""]

_GROUP_LAYOUT: dict[str, list[str | None]] = {
    "analysis": [
        "log",
        None,
        "dataview",
        "downstream",
        "upstream",
        "structures",
        None,
        "cause",
        "effect",
        "recovers",
        "why",
        "how",
        "prove",
        None,
        "simplified",
    ],
}


def _format_grouped_help() -> str:
    verb_groups: dict[str, list[str]] = {g: [] for g in _GROUP_ORDER}
    for verb, entry in sorted(_REGISTRY.items()):
        verb_groups.setdefault(entry.group, []).append(verb)
    usage_by_verb = {v: (e.usage or v) for v, e in _REGISTRY.items()}
    hint_by_verb = {v: e.hint for v, e in _REGISTRY.items() if e.hint}
    lines: list[str] = []
    for group in _GROUP_ORDER:
        verbs = verb_groups.get(group)
        if not verbs:
            continue
        if lines:
            lines.append("")
        lines.append(f"{group}:" if group else "other:")
        layout = _GROUP_LAYOUT.get(group)
        if layout:
            for item in layout:
                if item is None:
                    lines.append("")
                else:
                    lines.append(_format_verb_line(usage_by_verb[item], hint_by_verb.get(item)))
        else:
            for verb in verbs:
                lines.append(_format_verb_line(usage_by_verb[verb], hint_by_verb.get(verb)))
    return "\n".join(lines)


def _format_verb_line(usage: str, hint: str | None) -> str:
    base = f"  {usage}"
    if not hint:
        return base
    pad = max(2, 56 - len(base))
    return f"{base}{' ' * pad}{hint}"


def dispatch(adapter: Any, expression: str, *, provenance: str = "console") -> ConsoleResult:
    parts = expression.strip().split()
    if not parts:
        raise adapter.DAPAdapterError("Empty command")
    verb = parts[0].lower()
    entry = _REGISTRY.get(verb)
    if entry is None:
        known = ", ".join(sorted(_REGISTRY))
        raise adapter.DAPAdapterError(
            f"Unknown command '{verb}'. Available: {known}. Use Watch for predicate expressions."
        )
    result = entry.handler(adapter, expression)

    from pyrung.dap.capture import capture_hook

    capture_hook(adapter, verb, expression, provenance=provenance)

    action_log: list[tuple[int | None, str, str]] | None = getattr(adapter, "_action_log", None)
    if action_log is not None:
        runner = getattr(adapter, "_runner", None)
        scan_id = runner.current_state.scan_id if runner else None
        action_log.append((scan_id, expression.strip(), provenance))

    return result


# ---------------------------------------------------------------------------
# Existing verbs (migrated from stack_variables_evaluate)
# ---------------------------------------------------------------------------


@register("get", usage="get <tag> [tag2 ...]", group="data")
def _cmd_get(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: get <tag> [tag2 ...]")
    runner = adapter._require_runner_locked()
    tags = runner._state.tags
    lines: list[str] = []
    for name in parts[1:]:
        if name not in tags:
            raise adapter.DAPAdapterError(f"get: unknown tag '{name}'")
        lines.append(f"{name} = {tags[name]!r}")
    return ConsoleResult("\n".join(lines))


@register("force", usage="force <tag> <value>", group="data")
def _cmd_force(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split(None, 2)
    if len(parts) < 3:
        raise adapter.DAPAdapterError("Usage: force <tag> <value>")
    tag = parts[1]
    raw_value = parts[2]
    value = adapter._parse_literal(raw_value)
    runner = adapter._require_runner_locked()
    runner.force(tag, value)
    execution_flow.invalidate_mid_scan(adapter)
    return ConsoleResult(f"Forced {tag}={value!r}")


@register("unforce", usage="unforce <tag>", group="data")
def _cmd_unforce(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) != 2:
        raise adapter.DAPAdapterError("Usage: unforce <tag>")
    tag = parts[1]
    runner = adapter._require_runner_locked()
    runner.unforce(tag)
    execution_flow.invalidate_mid_scan(adapter)
    return ConsoleResult(f"Removed force {tag}")


@register("clear_forces", usage="clear_forces", group="data")
def _cmd_clear_forces(adapter: Any, _expression: str) -> ConsoleResult:
    runner = adapter._require_runner_locked()
    runner.clear_forces()
    execution_flow.invalidate_mid_scan(adapter)
    return ConsoleResult("Cleared all forces")


# ---------------------------------------------------------------------------
# New state-mutation verbs
# ---------------------------------------------------------------------------


@register("patch", usage="patch <tag> <value>", group="data")
def _cmd_patch(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split(None, 2)
    if len(parts) < 3:
        raise adapter.DAPAdapterError("Usage: patch <tag> <value>")
    tag = parts[1]
    raw_value = parts[2]
    value = adapter._parse_literal(raw_value)
    runner = adapter._require_runner_locked()
    runner.patch({tag: value})
    execution_flow.invalidate_mid_scan(adapter)
    return ConsoleResult(f"Patched {tag}={value!r}")


@register("continue", usage="continue", group="execution")
def _cmd_continue(adapter: Any, _expression: str) -> ConsoleResult:
    import threading

    adapter._require_runner_locked()
    if adapter._thread_running_locked():
        raise adapter.DAPAdapterError("Already running")
    adapter._pause_event.clear()
    thread = threading.Thread(
        target=adapter._continue_worker,
        daemon=True,
        name="pyrung-dap-continue",
    )
    adapter._continue_thread = thread
    thread.start()
    return ConsoleResult("Continuing")


@register("pause", usage="pause", group="execution")
def _cmd_pause(adapter: Any, _expression: str) -> ConsoleResult:
    if not adapter._thread_running_locked():
        raise adapter.DAPAdapterError("Not running")
    adapter._pause_event.set()
    return ConsoleResult("Pausing after current scan")


@register("step", usage="step [N]", group="execution")
def _cmd_step(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    n = 1
    if len(parts) > 1:
        try:
            n = int(parts[1])
        except ValueError as exc:
            raise adapter.DAPAdapterError(
                f"step count must be an integer, got '{parts[1]}'"
            ) from exc
    if n < 1:
        raise adapter.DAPAdapterError("step count must be >= 1")

    adapter._assert_can_step_locked()
    scans_completed = 0
    hit_bp = False
    for _ in range(n):
        _advance_one_full_scan(adapter)
        scans_completed += 1
        if adapter._current_rung_hits_breakpoint_locked():
            hit_bp = True
            break

    scan_id = adapter._current_scan_id
    suffix = " (breakpoint)" if hit_bp else ""
    return ConsoleResult(
        f"Stepped {scans_completed} scan(s), now at scan {scan_id}{suffix}",
        events=[("stopped", adapter._stopped_body("step"))],
    )


@register("run", usage="run <N | duration>  (e.g. 10, 500ms, 2 s)", group="execution")
def _cmd_run(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError(
            "Usage: run <cycles> or run <duration> (e.g. run 10, run 500ms)"
        )
    spec = _run_spec(parts)
    adapter._assert_can_step_locked()
    runner = adapter._require_runner_locked()

    try:
        cycles = int(spec)
        return _run_cycles(adapter, runner, cycles)
    except ValueError:
        pass

    from pyrung.core.physical import parse_duration

    try:
        ms = parse_duration(spec)
    except ValueError as exc:
        raise adapter.DAPAdapterError(f"Cannot parse '{spec}' as cycle count or duration") from exc

    return _run_duration(adapter, runner, ms / 1000.0)


def _run_cycles(adapter: Any, runner: Any, cycles: int) -> ConsoleResult:
    if cycles < 1:
        raise adapter.DAPAdapterError("cycle count must be >= 1")
    scans = 0
    hit_bp = False
    for _ in range(cycles):
        _advance_one_full_scan(adapter)
        scans += 1
        if adapter._current_rung_hits_breakpoint_locked():
            hit_bp = True
            break
    scan_id = adapter._current_scan_id
    suffix = " (breakpoint)" if hit_bp else ""
    return ConsoleResult(
        f"Ran {scans} cycle(s), now at scan {scan_id}{suffix}",
        events=[("stopped", adapter._stopped_body("step"))],
    )


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


def _run_duration(adapter: Any, runner: Any, seconds: float) -> ConsoleResult:
    if seconds <= 0:
        raise adapter.DAPAdapterError("duration must be positive")
    start_time = runner.current_state.timestamp
    target_time = start_time + seconds
    scans = 0
    hit_bp = False
    while runner.current_state.timestamp < target_time:
        _advance_one_full_scan(adapter)
        scans += 1
        if adapter._current_rung_hits_breakpoint_locked():
            hit_bp = True
            break
    elapsed = runner.current_state.timestamp - start_time
    suffix = " (breakpoint)" if hit_bp else ""
    return ConsoleResult(
        f"Ran {scans} scan(s) ({elapsed:.3f}s elapsed){suffix}",
        events=[("stopped", adapter._stopped_body("step"))],
    )


def _advance_one_full_scan(adapter: Any) -> None:
    """Advance through one complete scan using the debug stepping machinery."""
    origin_ctx = adapter._current_ctx
    if origin_ctx is None:
        if not adapter._advance_with_step_logpoints_locked():
            return
        origin_ctx = adapter._current_ctx
    while adapter._current_ctx is origin_ctx:
        if not adapter._advance_with_step_logpoints_locked():
            return
    from pyrung.dap.bounds_console import emit_bounds_violations

    emit_bounds_violations(adapter)


# ---------------------------------------------------------------------------
# Query verbs
# ---------------------------------------------------------------------------


@register("cause", usage="cause <tag>[@scan|:value]", group="analysis")
def _cmd_cause(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: cause <tag>[@scan|:value]")
    tag, scan, has_value, value = _parse_tag_spec(parts[1])
    runner = adapter._require_runner_locked()
    if has_value:
        chain = runner.cause(tag, to=value)
    else:
        chain = runner.cause(tag, scan=scan)
    if chain is None:
        return ConsoleResult(f"No causal chain found for {tag}")
    return ConsoleResult(str(chain))


@register("effect", usage="effect <tag>[@scan|:value]", group="analysis")
def _cmd_effect(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: effect <tag>[@scan|:value]")
    tag, scan, has_value, value = _parse_tag_spec(parts[1])
    runner = adapter._require_runner_locked()
    if has_value:
        chain = runner.effect(tag, from_=value)
    else:
        chain = runner.effect(tag, scan=scan)
    if chain is None:
        return ConsoleResult(f"No effect chain found for {tag}")
    return ConsoleResult(str(chain))


@register("recovers", usage="recovers <tag>", group="analysis")
def _cmd_recovers(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: recovers <tag>")
    tag_name = parts[1]
    runner = adapter._require_runner_locked()
    ok = runner.recovers(tag_name)
    resting = runner._resolve_resting_value(tag_name)
    witness = runner.cause(tag_name, to=resting)
    text = f"recovers: {ok}"
    if witness is not None:
        text += f"\n{witness}"
    return ConsoleResult(text)


@register("why", usage="why <tag> [tag2 ...]", group="analysis")
def _cmd_why(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: why <tag> [tag2 ...]")
    tags = parts[1:]
    runner = adapter._require_runner_locked()
    chain = runner.why(*tags)
    return ConsoleResult(str(chain))


def _pilot_value(value: Any) -> str:
    if value is True:
        return "True"
    if value is False:
        return "False"
    return repr(value) if isinstance(value, str) and " " in value else str(value)


def _pilot_assignments(actions: Any) -> str:
    ordered = sorted(actions, key=lambda pair: pair[0])
    return ", ".join(f"{tag}={_pilot_value(value)}" for tag, value in ordered)


class _PilotProgressFormatter:
    """Render Pilot events as a compact transcript, including live fragments.

    A trial or investigation is deliberately an unfinished sentence while the
    work is running.  The later event supplies ``done``, ``valid``, or the
    confirmed correction on that same console line.
    """

    def __init__(self) -> None:
        self._target: tuple[str, Any] | None = None
        self._trial_open = False
        self._retry_open = False
        self._wait_open = False
        self._wait_channel: str | None = None
        self._resuming_open = False
        self._movement_open = False
        self._departure_open = False
        self._investigation_open = False
        self._after_correction = False
        self._last_holds: tuple[tuple[str, Any], ...] = ()

    def _confirmed_corrections(self, data: dict[str, Any]) -> list[str]:
        from pyrung.core.analysis.graph import _format_synthesis_instruction
        from pyrung.core.validation.render import render_condition

        confirmed = data.get("investigation", {}).get("confirmed_detail", ())
        steady: list[str] = []
        by_guard: dict[str, list[str]] = {}
        for hypothesis in confirmed:
            for proposal in hypothesis.get("holds", ()):
                if hasattr(proposal, "dest"):
                    guard = render_condition(proposal.guard)
                    instruction = _format_synthesis_instruction(proposal)
                    instructions = by_guard.setdefault(guard, [])
                    if instruction not in instructions:
                        instructions.append(instruction)
                else:
                    assignment = f"{proposal[0]}={_pilot_value(proposal[1])}"
                    if assignment not in steady:
                        steady.append(assignment)
        corrections = [
            f"with rung({guard}): {'; '.join(instructions)}"
            for guard, instructions in by_guard.items()
        ]
        if steady:
            corrections.insert(0, f"keep {', '.join(steady)}")
        for tag, value in data.get("released_holds", ()):
            corrections.append(f"release {tag}={_pilot_value(value)}")
        return corrections

    def _render_rungs(self, rungs: Any) -> list[str]:
        """Render exact temporary logic as compact, source-shaped operations."""
        from pyrung.core.analysis.graph import _format_synthesis_instruction
        from pyrung.core.validation.render import render_condition

        by_guard: dict[str, list[str]] = {}
        for rung in rungs:
            guard = render_condition(rung.guard)
            instruction = _format_synthesis_instruction(rung)
            instructions = by_guard.setdefault(guard, [])
            if instruction not in instructions:
                instructions.append(instruction)
        return [
            f"with rung({guard}): {'; '.join(instructions)}"
            for guard, instructions in by_guard.items()
        ]

    def format(self, event: Any) -> str | None:
        kind = event.kind
        data = event.data

        if kind == "started":
            self._target = data["target"]
            tag, value = self._target
            return f"Finding a way to reach {tag}={_pilot_value(value)}...\n"

        if kind == "candidates_built":
            rungs = data.get("prerequisite_rungs", ())
            holds = tuple(sorted((rung.dest, rung.value) for rung in rungs))
            if not holds or holds == self._last_holds or self._after_correction:
                return None
            self._last_holds = holds
            reasons = []
            lever_notes = data.get("lever_notes", {})
            if lever_notes:
                from pyrung.core.analysis.graph import _lever_requirement

                reasons = [
                    _lever_requirement(lever_notes[tag])
                    for tag, _value in holds
                    if tag in lever_notes
                ]
            why = f" to satisfy {'; '.join(dict.fromkeys(reasons))}" if reasons else ""
            return f"  Set {_pilot_assignments(holds)}{why}.\n"

        if kind == "candidate_try":
            actions = data.get("applied", ())
            if not actions:
                return None
            prefix = ""
            if self._departure_open:
                self._departure_open = False
                prefix = " valid.\n"
            self._trial_open = not self._after_correction
            self._retry_open = self._after_correction
            self._after_correction = False
            if self._retry_open:
                return prefix + "\nRetrying..."
            # Prerequisite rungs are already reported as sustained temporary
            # logic.  Do not mislabel their witness values as momentary pulses.
            pulsed = tuple(action for action in actions if action not in self._last_holds)
            return prefix + f"\nPulse {_pilot_assignments(pulsed or actions)}..."

        if kind == "candidate_rejected":
            if self._trial_open:
                self._trial_open = False
                return " no useful change.\n"
            if self._retry_open:
                self._retry_open = False
                return " no useful change.\n"
            return None

        if kind == "candidate_accepted":
            if self._trial_open:
                self._trial_open = False
                return " done.\n"
            # A retry stays open until its resulting motion is known.
            return None

        if kind == "zoom":
            if self._retry_open:
                return None
            self._resuming_open = self._after_correction
            prefix = "\n  Resuming..." if self._resuming_open else "  Waiting"
            self._after_correction = False
            if self._wait_open:
                prefix = "\n" + prefix.lstrip("\n")
            self._wait_open = True
            channel = data.get("channel_tag")
            self._wait_channel = channel
            if self._resuming_open:
                return prefix
            return f"{prefix} for {channel}..." if channel else f"{prefix}..."

        if kind == "zoom_rejected":
            if self._wait_open:
                self._wait_open = False
                self._wait_channel = None
                self._resuming_open = False
                return " not ready yet.\n"
            return None

        if kind == "zoom_accepted":
            scan_before = data.get("scan_before")
            scan_after = data.get("scan_after", event.scan)
            span = scan_after - scan_before if scan_before is not None else None
            skipped = data.get("coast_skipped_scans")
            kernel = data.get("coast_kernel_scans")
            channel = data.get("zoom_channel_tag")
            before = data.get("zoom_before_value")
            after = data.get("zoom_actual_value")
            elapsed = f" after {span} scan{'s' if span != 1 else ''}" if span is not None else ""
            if isinstance(skipped, int) and skipped > 0 and isinstance(kernel, int):
                elapsed += f" ({skipped:,} folded; {kernel:,} kernel)"
            prefix = " " if self._wait_open or self._retry_open else "  "
            wait_channel = self._wait_channel
            resuming = self._resuming_open
            self._wait_open = False
            self._wait_channel = None
            self._resuming_open = False
            self._retry_open = False
            snapshot = data.get("snapshot", {})
            if self._target is not None and snapshot.get(self._target[0]) == self._target[1]:
                tag, value = self._target
                outcome = f"{tag}={_pilot_value(value)}{elapsed}"
            elif channel is not None and before != after:
                tag = "" if channel == wait_channel and not resuming else f"{channel} "
                if isinstance(before, bool) and isinstance(after, bool):
                    outcome = f"{tag}-> {_pilot_value(after)}{elapsed}"
                else:
                    outcome = f"{tag}{_pilot_value(before)} -> {_pilot_value(after)}{elapsed}"
            else:
                outcome = f"advanced{elapsed}"
            if data.get("ejected"):
                self._movement_open = True
                return prefix + outcome
            return prefix + outcome + ".\n"

        if kind == "letrun_ejection":
            if self._movement_open:
                self._movement_open = False
                return "."
            tag = data.get("channel_tag")
            before = data.get("from_value")
            after = data.get("to_value")
            prefix = " " if self._retry_open else "  "
            self._retry_open = False
            self._movement_open = True
            if tag is None:
                return prefix + "The program moved away from the expected path"
            return f"{prefix}{tag} jumped {_pilot_value(before)} -> {_pilot_value(after)}"

        if kind == "departure_check_started":
            self._movement_open = False
            self._departure_open = True
            return " Checking..."

        if kind in {"provisional_started", "departure_investigated"}:
            if self._departure_open:
                self._departure_open = False
                return " valid.\n"
            return None

        if kind == "investigation_started":
            prefix = " unexpected.\n" if self._departure_open else "\n"
            self._departure_open = False
            self._investigation_open = True
            return prefix + "  Preventable?"

        if kind == "trend_regression":
            corrections = self._confirmed_corrections(data)
            revoked = self._render_rungs(data.get("revoked_rungs", ()))
            transitions = data.get("channel_transitions", ())
            was_investigating = self._investigation_open
            self._investigation_open = False
            self._after_correction = True
            if was_investigating:
                if revoked:
                    lines = [" Yes.", f"  Remove temporary logic: {'; '.join(revoked)}."]
                    if corrections:
                        lines.append(f"  Replace with: {'; '.join(corrections)}.")
                    return "\n".join(lines) + "\n"
                if corrections:
                    return f" Yes -- {'; '.join(corrections)}.\n"
                return " Unknown -- no corrective temporary logic was confirmed.\n"
            moved = ", ".join(
                f"{tag} {_pilot_value(before)} -> {_pilot_value(after)}"
                for tag, before, after in transitions
            )
            after_move = f" after {moved}" if moved else ""
            if corrections:
                correction = "; ".join(corrections)
                correction = correction[:1].upper() + correction[1:]
                return f"\n  Returning to the last good state{after_move}. {correction}.\n"
            return f"\n  Returning to the last good state{after_move}; no correction was found.\n"

        if kind == "stuck":
            reason = str(data.get("reason") or "No productive next action was found.")
            reason = reason.removeprefix("pilot: ").removeprefix("stuck: ")
            prefix = (
                "\n"
                if any(
                    (
                        self._trial_open,
                        self._retry_open,
                        self._wait_open,
                        self._departure_open,
                        self._investigation_open,
                    )
                )
                else ""
            )
            return f"{prefix}\nStopping: {reason}\n"

        if kind == "finished":
            return "\n"

        return None


def _format_pilot_progress(event: Any) -> str | None:
    """Format one standalone event; command streams should reuse a formatter."""
    return _PilotProgressFormatter().format(event)


@register(
    "how",
    usage=(
        "how <expression>[, <expression>...] "
        "[avoid <expression>[, <expression>...]] [via <expression>]"
    ),
    group="analysis",
    hint="(runs planner)",
    # Declared, not derived: the usage prose can't say that `avoid`/`via` are
    # *keyword-introduced* clauses, nor that targets and avoids are comma-separated
    # conjuncts within a single slot. Completers need both facts.
    slots=(
        Slot(kind="expression", label="target", repeat=True, separator=","),
        Slot(
            kind="expression",
            label="avoid",
            required=False,
            repeat=True,
            separator=",",
            keyword="avoid",
        ),
        Slot(kind="expression", label="via", required=False, keyword="via"),
    ),
)
def _cmd_how(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        raise adapter.DAPAdapterError(
            "Usage: how <expression>[, <expression>...] "
            "[avoid <expression>[, <expression>...]] [via <expression>]  "
            "(e.g. how Running, how State == HELD avoid State == FAULTED, "
            "how Burner avoid ProdMode, MaintFault, how Burner via MaintMode).  "
            "Comma-separated targets must all hold at the end of the path.  "
            "Comma-separated avoid conditions are a union; each is avoided "
            "independently."
        )
    expr_str = parts[1].strip()
    runner = adapter._require_runner_locked()
    from pyrung.dap.expressions import (
        ExpressionParseError,
        to_conditions,
    )
    from pyrung.dap.expressions import (
        parse as parse_expr,
    )

    # Split the target from trailing `avoid`/`via` clauses (either order).
    tokens = re.split(r"\b(avoid|via)\b", expr_str)
    expr_str = tokens[0].strip()
    clauses: dict[str, str] = {}
    for i in range(1, len(tokens), 2):
        keyword = tokens[i]
        value = tokens[i + 1].strip() if i + 1 < len(tokens) else ""
        if not value:
            raise adapter.DAPAdapterError(f"how: missing expression after '{keyword}'")
        clauses[keyword] = value
    if not expr_str:
        raise adapter.DAPAdapterError("how: missing target expression")

    def _resolve(label: str, text: str) -> Any:
        try:
            parsed = parse_expr(text)
        except ExpressionParseError as exc:
            raise adapter.DAPAdapterError(f"how {label}: {exc}") from exc
        try:
            conds = to_conditions(parsed, runner._known_tags_by_name)
        except KeyError as exc:
            raise adapter.DAPAdapterError(f"how {label}: unknown tag {exc}") from exc
        return conds

    conditions = _resolve("target", expr_str)

    def _single(label: str) -> Any:
        if label not in clauses:
            return None
        conds = _resolve(label, clauses[label])
        return conds if len(conds) != 1 else conds[0]

    progress = _PilotProgressFormatter()

    def _on_pilot_event(event: Any) -> None:
        fragment = progress.format(event)
        if fragment is not None:
            adapter._send_event("output", {"category": "console", "output": fragment})

    path = runner.how(
        *conditions, avoid=_single("avoid"), via=_single("via"), on_event=_on_pilot_event
    )
    return ConsoleResult(str(path))


@register(
    "prove",
    usage="prove always|never <expression> [--settled] [--paced]",
    group="analysis",
    hint="(exhaustive, needs annotations)",
)
def _cmd_prove(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split(maxsplit=2)
    if len(parts) < 2:
        raise adapter.DAPAdapterError(
            "Usage: prove always|never <expression>  "
            "(e.g. prove always Done, prove never OverTemp, ~CoolingPump)"
        )
    mode = parts[1].lower()
    if mode not in ("always", "never"):
        raise adapter.DAPAdapterError(
            "Usage: prove always|never <expression>  "
            "(e.g. prove always Done, prove never OverTemp, ~CoolingPump)"
        )
    if len(parts) < 3 or not parts[2].strip():
        raise adapter.DAPAdapterError(
            "Usage: prove always|never <expression>  "
            "(e.g. prove always Done, prove never OverTemp, ~CoolingPump)"
        )
    expr_str = parts[2].strip()
    runner = adapter._require_runner_locked()
    if runner._program is None:
        raise adapter.DAPAdapterError("prove requires a program loaded from a .py file")

    settled = False
    paced = False
    if "--settled" in expr_str:
        settled = True
        expr_str = expr_str.replace("--settled", "").strip()
    if "--paced" in expr_str:
        paced = True
        expr_str = expr_str.replace("--paced", "").strip()
    if not expr_str:
        raise adapter.DAPAdapterError(
            "Usage: prove always|never <expression>  "
            "(e.g. prove always Done, prove never OverTemp, ~CoolingPump)"
        )

    from pyrung.dap.expressions import (
        ExpressionParseError,
        compile_for_dict,
        referenced_tags,
    )
    from pyrung.dap.expressions import (
        parse as parse_expr,
    )

    try:
        expr = parse_expr(expr_str)
    except ExpressionParseError as exc:
        raise adapter.DAPAdapterError(f"prove: {exc}") from exc

    scope = sorted(referenced_tags(expr))
    predicate = compile_for_dict(expr, tags=runner._known_tags_by_name)

    adapter._send_event("output", {"category": "console", "output": "Verifying...\n"})

    from pyrung.core.analysis.prove import always as always_fn

    if mode == "never":
        predicate = (lambda p: lambda s: not p(s))(predicate)

    result = always_fn(
        runner._program,
        predicate,
        scope=scope if scope else None,
        settled=settled,
        paced=paced,
    )

    return ConsoleResult(_format_prove_result(result))


def _format_prove_result(result: Any) -> str:
    from pyrung.core.analysis.prove.results import Counterexample, Intractable, Proven

    if isinstance(result, Proven):
        lines = [f"Proven ({result.states_explored} states explored)"]
        for caveat in result.caveats:
            lines.append(f"  caveat: {caveat}")
        if result.aggressive_counterexample is not None:
            lines.append("  note: aggressive (non-paced) counterexample exists")
        return "\n".join(lines)

    if isinstance(result, Counterexample):
        lines = ["Counterexample found:"]
        for i, step in enumerate(result.trace):
            inputs = ", ".join(f"{k}={v}" for k, v in sorted(step.inputs.items()))
            scans = f" ({step.scans} scans)" if step.scans > 1 else ""
            lines.append(f"  step {i}: {inputs}{scans}")
        for caveat in result.caveats:
            lines.append(f"  caveat: {caveat}")
        return "\n".join(lines)

    if isinstance(result, Intractable):
        lines = [f"Intractable: {result.reason}"]
        if result.tags:
            lines.append(f"  blocking tags: {', '.join(result.tags)}")
        for hint in result.hints:
            lines.append(f"  hint: {hint}")
        return "\n".join(lines)

    return str(result)


def _resolve_tags(runner: Any, names: list[str], *, verb: str) -> list[Any]:
    tags = []
    for name in names:
        tag = runner._known_tags_by_name.get(name)
        if tag is None:
            raise ValueError(f"{verb}: unknown tag '{name}'")
        tags.append(tag)
    return tags


def _parse_tag_spec(spec: str) -> tuple[str, int | None, bool, Any]:
    """Parse ``Tag``, ``Tag@5``, or ``Tag:value`` into (tag, scan, has_value, value)."""
    if "@" in spec:
        tag, _, scan_s = spec.partition("@")
        try:
            scan = int(scan_s.strip())
        except ValueError as exc:
            raise ValueError(f"scan after '@' must be an integer, got '{scan_s}'") from exc
        return (tag.strip(), scan, False, None)
    if ":" in spec:
        tag, _, value_s = spec.partition(":")
        return (tag.strip(), None, True, _parse_value(value_s))
    return (spec.strip(), None, False, None)


def _parse_value(raw: str) -> Any:
    s = raw.strip()
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("none", "null"):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


# ---------------------------------------------------------------------------
# DataView verbs
# ---------------------------------------------------------------------------


@register("dataview", usage="dataview <text | i: | p: | t: | upstream:tag>", group="analysis")
def _cmd_dataview(adapter: Any, expression: str) -> ConsoleResult:
    rest = expression.strip().split(None, 1)
    if len(rest) < 2:
        raise adapter.DAPAdapterError(
            "Usage: dataview <query> (e.g. dataview Motor, dataview i:, dataview upstream:Running)"
        )
    query = rest[1]
    runner = adapter._require_runner_locked()
    view = runner.program.dataview()

    from pyrung.dap.handlers.graph_slice import _parse_query

    result = _parse_query(query, view)
    return _format_dataview(result)


@register("upstream", usage="upstream <tag>", group="analysis")
def _cmd_upstream(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: upstream <tag>")
    tag_name = parts[1]
    runner = adapter._require_runner_locked()
    view = runner.program.dataview().upstream(tag_name)
    return _format_dataview(view)


@register("downstream", usage="downstream <tag>", group="analysis")
def _cmd_downstream(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: downstream <tag>")
    tag_name = parts[1]
    runner = adapter._require_runner_locked()
    view = runner.program.dataview().downstream(tag_name)
    return _format_dataview(view)


def _tag_annotations(detail: Any) -> str:
    parts: list[str] = []
    if detail.type != "bool":
        parts.append(detail.type.capitalize())
    flags = []
    if detail.retentive:
        flags.append("retentive")
    if detail.readonly:
        flags.append("readonly")
    if detail.external:
        flags.append("external")
    if detail.final:
        flags.append("final")
    if detail.public:
        flags.append("public")
    if detail.lock:
        flags.append("lock")
    if flags:
        parts.append(", ".join(flags))
    range_parts = []
    if detail.min is not None:
        range_parts.append(f"min:{detail.min}")
    if detail.max is not None:
        range_parts.append(f"max:{detail.max}")
    if detail.uom:
        range_parts.append(f"uom:{detail.uom}")
    if range_parts:
        parts.append(" ".join(range_parts))
    if detail.physical:
        parts.append(f"physical:{detail.physical}")
    if detail.link:
        parts.append(f"link:{detail.link}")
    if detail.choices:
        parts.append(f"choices:{detail.choices}")
    if detail.structure_kind and detail.structure_name:
        struct = f"{detail.structure_kind}:{detail.structure_name}"
        if detail.structure_field:
            struct += f".{detail.structure_field}"
        parts.append(struct)
    return "  ".join(parts)


def _format_dataview(view: Any) -> ConsoleResult:
    details = view.details()
    if not details:
        return ConsoleResult("No matching tags")
    sorted_names = sorted(details)
    max_name = max(len(n) for n in sorted_names)
    lines = []
    for name in sorted_names:
        d = details[name]
        pad = " " * (max_name - len(name))
        ann = _tag_annotations(d)
        suffix = f"  {ann}" if ann else ""
        lines.append(f"  {name}{pad}  ({d.role}){suffix}")
    return ConsoleResult(f"{len(details)} tag(s):\n" + "\n".join(lines))


def _field_annotations(f: Any) -> str:
    parts: list[str] = []
    flags = []
    if f.readonly:
        flags.append("readonly")
    if f.external:
        flags.append("external")
    if f.final:
        flags.append("final")
    if f.public:
        flags.append("public")
    if f.lock:
        flags.append("lock")
    if flags:
        parts.append(", ".join(flags))
    range_parts = []
    if f.min is not None:
        range_parts.append(f"min:{f.min}")
    if f.max is not None:
        range_parts.append(f"max:{f.max}")
    if f.uom:
        range_parts.append(f"uom:{f.uom}")
    if range_parts:
        parts.append(" ".join(range_parts))
    if f.physical:
        parts.append(f"physical:{f.physical}")
    if f.link:
        parts.append(f"link:{f.link}")
    if f.choices:
        parts.append(f"choices:{f.choices}")
    return "  ".join(parts)


@register("structures", usage="structures", group="analysis")
def _cmd_structures(adapter: Any, expression: str) -> ConsoleResult:
    runner = adapter._require_runner_locked()
    view = runner.program.dataview()
    structs = view.structures()
    if not structs:
        return ConsoleResult("No structures found")

    sections: list[str] = []
    udts = [s for s in structs if s.kind == "udt"]
    named_arrays = [s for s in structs if s.kind == "named_array"]

    if udts:
        lines = ["UDTs:"]
        for s in udts:
            lines.append(f"  {s.name} (count={s.count})")
            if s.fields:
                max_fname = max(len(f.name) for f in s.fields)
                for f in s.fields:
                    pad = " " * (max_fname - len(f.name))
                    ann = _field_annotations(f)
                    suffix = f"  {ann}" if ann else ""
                    lines.append(f"    {f.name}{pad}  {f.type.capitalize()}{suffix}")
        sections.append("\n".join(lines))

    if named_arrays:
        lines = ["Named Arrays:"]
        for s in named_arrays:
            header = f"  {s.name} (count={s.count}"
            if s.stride is not None:
                header += f", stride={s.stride}"
            if s.base_type is not None:
                header += f", type={s.base_type.capitalize()}"
            header += ")"
            lines.append(header)
            if s.fields:
                max_fname = max(len(f.name) for f in s.fields)
                for f in s.fields:
                    pad = " " * (max_fname - len(f.name))
                    ann = _field_annotations(f)
                    suffix = f"  {ann}" if ann else ""
                    lines.append(f"    {f.name}{pad}  {f.type.capitalize()}{suffix}")
        sections.append("\n".join(lines))

    return ConsoleResult("\n\n".join(sections))


# ---------------------------------------------------------------------------
# Simplified form
# ---------------------------------------------------------------------------


@register("simplified", usage="simplified [tag]", group="analysis")
def _cmd_simplified(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    runner = adapter._require_runner_locked()
    forms = runner.program.simplified()

    if len(parts) >= 2:
        tag_name = parts[1]
        form = forms.get(tag_name)
        if form is None:
            if tag_name not in {n for n in runner.program.dataview().tags}:
                raise adapter.DAPAdapterError(f"Unknown tag '{tag_name}'")
            raise adapter.DAPAdapterError(
                f"'{tag_name}' is not a terminal tag. Only terminals have simplified forms."
            )
        stats = f"  ({form.writer_count} writer(s), {form.pivot_count} pivot(s) resolved, depth {form.depth})"
        return ConsoleResult(f"{form}\n{stats}")

    if not forms:
        return ConsoleResult("No terminal tags found")
    lines = [str(f) for f in forms.values()]
    return ConsoleResult(f"{len(forms)} terminal(s):\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# Monitor verbs
# ---------------------------------------------------------------------------


@register("monitor", usage="monitor <tag>", group="data")
def _cmd_monitor(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: monitor <tag>")
    tag_name = parts[1].strip()
    runner = adapter._require_runner_locked()
    monitor_id_ref: dict[str, int] = {"id": 0}
    handle = runner.monitor(
        tag_name,
        adapter._build_monitor_callback(tag_name=tag_name, monitor_id_ref=monitor_id_ref),
    )
    monitor_id_ref["id"] = handle.id
    adapter._monitor_handles[handle.id] = handle
    from pyrung.dap.handlers.monitor_data_breakpoints import MonitorMeta

    adapter._monitor_meta[handle.id] = MonitorMeta(id=handle.id, tag=tag_name, enabled=True)
    current = runner.current_state.tags.get(tag_name)
    adapter._monitor_values[handle.id] = adapter._format_value(current)
    return ConsoleResult(f"Monitor added: {tag_name} (id={handle.id})")


@register("unmonitor", usage="unmonitor <tag>", group="data")
def _cmd_unmonitor(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: unmonitor <tag>")
    tag_name = parts[1].strip()
    target_id = None
    for mid, meta in adapter._monitor_meta.items():
        if meta.tag == tag_name:
            target_id = mid
            break
    if target_id is None:
        raise adapter.DAPAdapterError(f"No monitor found for tag '{tag_name}'")
    handle = adapter._monitor_handles.pop(target_id, None)
    if handle is not None:
        handle.remove()
    adapter._monitor_meta.pop(target_id, None)
    adapter._monitor_values.pop(target_id, None)
    return ConsoleResult(f"Monitor removed: {tag_name}")


# ---------------------------------------------------------------------------
# Note / Log
# ---------------------------------------------------------------------------


@register("note", usage="note <text>", group="data")
def _cmd_note(adapter: Any, expression: str) -> ConsoleResult:
    rest = expression.strip()
    idx = rest.find(" ")
    if idx < 0 or not rest[idx:].strip():
        raise adapter.DAPAdapterError("Usage: note <text>")
    text = rest[idx:].strip()
    runner = adapter._require_runner_locked()
    scan_id = runner.current_state.scan_id
    adapter._notes.setdefault(scan_id, []).append(text)
    return ConsoleResult(f"Note at scan {scan_id}: {text}")


@register("log", usage="log [N]", group="analysis")
def _cmd_log(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    n = 20
    i = 1
    while i < len(parts):
        if parts[i] == "-n" and i + 1 < len(parts):
            try:
                n = int(parts[i + 1])
            except ValueError as exc:
                raise adapter.DAPAdapterError(
                    f"log count must be integer, got '{parts[i + 1]}'"
                ) from exc
            i += 2
        else:
            try:
                n = int(parts[i])
            except ValueError as exc:
                raise adapter.DAPAdapterError(
                    f"log count must be integer, got '{parts[i]}'"
                ) from exc
            i += 1
    if n < 1:
        raise adapter.DAPAdapterError("log count must be >= 1")

    runner = adapter._require_runner_locked()
    log = runner._scan_log
    tip = runner._state.scan_id
    forces = dict(runner.forces)

    start = max(log.base_scan, tip - n + 1)
    lines: list[str] = []
    lines.append(f"scan {tip}  forces: {_format_forces(forces)}")
    lines.append("")

    notes: dict[int, list[str]] = getattr(adapter, "_notes", {})
    action_log: list[tuple[int | None, str, str]] = getattr(adapter, "_action_log", [])
    scan_sources: dict[int, set[str]] = {}
    for a_scan, _cmd, prov in action_log:
        if a_scan is not None and prov != "console":
            scan_sources.setdefault(a_scan, set()).add(prov)

    prev_state = None
    for scan_id in range(start, tip + 1):
        entries: list[str] = []
        for note_text in notes.get(scan_id, []):
            entries.append(f"  # {note_text}")
        patches = log._patches_by_scan.get(scan_id)
        if patches:
            for tag, val in sorted(patches.items()):
                entries.append(f"  patch {tag} {val!r}")
        force_snap = log._force_changes_by_scan.get(scan_id)
        if force_snap is not None:
            prev_scan = scan_id - 1
            prev_forces = (
                log._force_changes_by_scan.get(prev_scan, {}) if prev_scan >= log.base_scan else {}
            )
            for tag in sorted(set(force_snap) | set(prev_forces)):
                old = prev_forces.get(tag)
                new = force_snap.get(tag)
                if old != new:
                    if new is not None and old is None:
                        entries.append(f"  force {tag} {new!r}")
                    elif new is None and old is not None:
                        entries.append(f"  unforce {tag}")
                    else:
                        entries.append(f"  force {tag} {old!r} -> {new!r}")

        cur_state = runner.history.at(scan_id)
        if prev_state is not None:
            for key in sorted(set(cur_state.tags.keys()) | set(prev_state.tags.keys())):
                old_v = prev_state.tags.get(key)
                new_v = cur_state.tags.get(key)
                if old_v != new_v:
                    entries.append(f"  {key}: {old_v!r} -> {new_v!r}")
        prev_state = cur_state

        if entries:
            sources = scan_sources.get(scan_id)
            tag = f"  ({', '.join(sorted(sources))})" if sources else ""
            lines.append(f"scan {scan_id}:{tag}")
            lines.extend(entries)

    if len(lines) == 2:
        lines.append("(no changes)")

    return ConsoleResult("\n".join(lines))


def _format_forces(forces: dict[str, Any]) -> str:
    if not forces:
        return "none"
    return ", ".join(f"{k}={v!r}" for k, v in sorted(forces.items()))


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


@register("help", usage="help")
def _cmd_help(adapter: Any, _expression: str) -> ConsoleResult:
    return ConsoleResult(_format_grouped_help())
