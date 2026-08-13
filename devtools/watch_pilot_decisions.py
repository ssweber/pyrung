"""Run one bounded PILOT drive with the same live prose as DAP ``how``.

This is intentionally a tap, not another planner.  A disposable worker owns
the real ``pilot_events`` drive and DAP's existing progress formatter.  The
parent process owns the clocks, so it can stop a regression even when PILOT is
stuck inside one expensive operation and has not yielded the next event.

On timeout the parent reports the last DAP-visible output and recent structured
events, asks the worker to dump every Python thread, then terminates only that
worker.  The test/orchestrator therefore gets a pointable failure instead of a
pytest process that continues consuming CPU for minutes.
"""

from __future__ import annotations

import argparse
import faulthandler
import importlib
import multiprocessing as mp
import queue as queue_module
import sys
import threading
import time
import traceback
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot as pilot_module
from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.analysis.pilot.compass import Compass
from pyrung.core.analysis.pilot.intrascan import IntrascanResult
from pyrung.core.analysis.pilot.navigation_contracts import Bearing
from pyrung.core.analysis.pilot.types import (
    _AttemptResult,
    _CausalCheckpoint,
    _PilotState,
)
from pyrung.core.analysis.pilot.working_theory import theory_view
from pyrung.core.runner import _compile_avoid
from pyrung.dap.console import _PilotProgressFormatter

EXIT_FAILED = 1
EXIT_TIMEOUT = 2
EXIT_STOPPED = 3

_DECISION_EVENT_KINDS = frozenset(
    {
        "bearing_coast",
        "candidate_try",
        "conductivity_research_requested",
        "skiff",
        "theory_correction_composed",
    }
)


def _value(text: str) -> Any:
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(text)
    except ValueError:
        return text


def _target(text: str) -> tuple[str, Any]:
    """Parse ``TAG`` as Boolean true or ``TAG=VALUE`` explicitly."""

    tag, separator, value = text.partition("=")
    tag = tag.strip()
    if not tag:
        raise argparse.ArgumentTypeError("target tag cannot be empty")
    if not separator:
        return tag, True
    if not value:
        raise argparse.ArgumentTypeError("expected TAG=VALUE")
    return tag, _value(value)


def _positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a number of seconds") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("seconds must be greater than zero")
    return value


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a positive integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return value


def _nonnegative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a nonnegative integer") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("value must be zero or greater")
    return value


def _candidate_pairs(data: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    return tuple((candidate["tag"], candidate["value"]) for candidate in data["candidates"])


def _target_condition(tag: Any, value: Any) -> Any:
    """Match the DAP expression adapter's Boolean target semantics."""

    if value is True:
        return tag
    if value is False:
        return ~tag
    return tag == value


def _decision_lines(
    event: Any,
    snapshot: Mapping[str, Any],
    stop_action: tuple[str, Any] | None,
) -> tuple[tuple[str, ...], bool]:
    if event.kind == "conductivity_research_requested":
        stop = event.data["displacement"]
        return (
            (
                "[research] "
                f"scan={event.scan} stop={(stop.tag, stop.rung, stop.values)!r} "
                f"enabling_reads={tuple(event.data['enabling_reads'])!r} "
                f"requirement_drifts={tuple(event.data['requirement_drifts'])!r} "
                f"reason={event.data.get('reason')!r}"
            ),
        ), False
    if event.kind == "theory_correction_composed":
        rung = event.data["pilot_rung"]
        return (
            (
                "[composition] "
                f"scan={event.scan} rung={(rung.dest, rung.value)!r} "
                f"conditions={tuple(event.data['conditions'])!r} "
                f"reason={event.data.get('reason')!r}"
            ),
        ), False
    if event.kind == "bearing_coast_accepted":
        effects = tuple(
            (
                getattr(effect, "disposition", None),
                getattr(getattr(effect, "obligation", None), "tag", None),
                getattr(getattr(effect, "appeared", None), "scan_id", None),
            )
            for effect in event.data.get("effect_observations") or ()
        )
        return (
            (
                "[coast-receipt] "
                f"scan={event.scan} "
                f"channel={event.data.get('bearing_coast_channel_tag')!r} "
                f"motion={event.data.get('bearing_coast_before_value')!r}->"
                f"{event.data.get('bearing_coast_target_value')!r}/"
                f"{event.data.get('bearing_coast_actual_value')!r} "
                f"stop={event.data.get('bearing_stop_reason')!r} "
                f"progress={event.data.get('progress')!r} "
                f"effects={effects!r}"
            ),
        ), False
    if event.kind != "candidates_built":
        return (), False

    candidates = _candidate_pairs(event.data)
    trace = tuple(event.data["trace_actions"])
    route = tuple(event.data["route_candidates"])
    prerequisites = tuple(
        (rung.dest, rung.value) for rung in event.data.get("prerequisite_pilot_rungs", ())
    )
    active_alarms = tuple(
        name
        for name, value in snapshot.items()
        if name.startswith("A_Alm") and name.endswith("_Status") and bool(value)
    )
    lines = [
        "[decision] "
        f"scan={event.scan} "
        f"state={snapshot.get('Sts_StateCurrent')!r} "
        f"step={snapshot.get('Internal__Step')!r} "
        f"candidates={candidates!r} trace={trace!r} route={route!r} "
        f"holds={prerequisites!r} "
        f"wait={event.data.get('wait_reason')!r} "
        f"frontier={tuple(event.data.get('completion_frontier') or ())!r} "
        f"program_step={event.data.get('program_step')!r} "
        f"alarm_extent={snapshot.get('A_AlmExtent')!r} "
        f"active_alarms={active_alarms!r}"
    ]
    stopped = stop_action is not None and (
        stop_action in candidates or stop_action in trace or stop_action in prerequisites
    )
    if stopped:
        for detail in event.data.get("trace_action_details", ()):
            if detail.pair != stop_action:
                continue
            lines.append(
                "[receipt] "
                f"provenance={detail.provenance!r} "
                f"writer_path={detail.writer_path!r} "
                f"operation_boundary={detail.operation_boundary!r} "
                f"until={detail.until!r}"
            )
        lines.append(f"[stop] candidate construction surfaced {stop_action!r}")
    return tuple(lines), stopped


def _event_context(event: Any, snapshot: Mapping[str, Any]) -> str:
    """Compact, serialization-safe context for a watchdog receipt."""

    parts = [
        f"state={snapshot.get('Sts_StateCurrent')!r}",
        f"step={snapshot.get('Internal__Step')!r}",
    ]
    candidate = event.data.get("candidate")
    if isinstance(candidate, Mapping):
        pair = candidate.get("pair")
        if pair is not None:
            parts.append(f"candidate={pair!r}")
    applied = event.data.get("applied")
    if applied:
        parts.append(f"applied={tuple(applied)!r}")
    channel = event.data.get("channel_tag") or event.data.get("bearing_coast_channel_tag")
    if channel is not None:
        parts.append(f"channel={channel!r}")
    reason = event.data.get("reason")
    if reason:
        rendered = str(reason)
        parts.append(f"reason={rendered[:240]!r}")
    if event.kind == "bearing_coast_accepted":
        parts.append(
            "motion="
            f"{event.data.get('bearing_coast_before_value')!r}->"
            f"{event.data.get('bearing_coast_target_value')!r}/"
            f"{event.data.get('bearing_coast_actual_value')!r} "
            f"stop={event.data.get('bearing_stop_reason')!r}"
        )
        effects = tuple(
            (
                getattr(effect, "disposition", None),
                getattr(getattr(effect, "obligation", None), "tag", None),
                getattr(getattr(effect, "appeared", None), "scan_id", None),
            )
            for effect in event.data.get("effect_observations") or ()
        )
        if effects:
            parts.append(f"effects={effects!r}")
    return " ".join(parts)


def _interpretation_line(message: Mapping[str, Any]) -> str:
    """Render one temporal diagnosis without turning it into a public event."""

    support = repr(tuple(message["supporting_identities"]))
    if len(support) > 1200:
        support = f"{support[:1200]}..."
    return (
        "[interpretation] "
        f"scan={message['scan']} kind={message['kind']} "
        f"projected_scans={message['projected_scans']} "
        f"assertion_projection_cached={message['assertion_projection_cached']} "
        f"reason={message['reason']!r} support={support}"
    )


def _conductivity_line(message: Mapping[str, Any]) -> str:
    """Render the small immutable front receipt captured after theory reduction."""

    return (
        "[conductivity] "
        f"attempts={tuple(message['attempts'])!r} "
        f"comparisons={tuple(message['comparisons'])!r} "
        f"research={message['research']!r}"
    )


def _arm_stack_dump(dump_request: Any, dump_ready: Any) -> None:
    """Dump the worker while its drive thread is still at the slow operation."""

    def dump_when_requested() -> None:
        dump_request.wait()
        print("\n[watchdog] worker Python stacks at timeout:", file=sys.stderr, flush=True)
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        dump_ready.set()

    threading.Thread(
        target=dump_when_requested,
        name="pilot-watchdog-stack-dumper",
        daemon=True,
    ).start()


def _drive_worker(
    config: dict[str, Any],
    messages: Any,
    dump_request: Any,
    dump_ready: Any,
) -> None:
    """Execute the real drive; communicate only compact diagnostic receipts."""

    _arm_stack_dump(dump_request, dump_ready)
    started = time.monotonic()
    event_count = 0
    decision_count = 0
    interpretation_count = 0
    last_snapshot: dict[str, Any] = {}
    last_scan: int | None = None
    try:
        fixture = importlib.import_module(config["fixture"])
        plc = PLC(fixture.logic, dt=config["dt"])
        for _ in range(config["entry_steps"]):
            plc.step()
        tags = plc._known_tags_by_name
        target_tag, target_value = config["target"]
        condition = _target_condition(tags[target_tag], target_value)
        avoid_conditions = tuple(tags[name] for name in config["avoid"])
        avoid_pred = _compile_avoid(avoid_conditions) if avoid_conditions else None
        formatter = _PilotProgressFormatter()
        original_interpret = pilot_module._theory_transition_from_attempt
        original_record = pilot_module._record_working_theory_transition
        projection_receipts: dict[tuple[Any, ...], tuple[int, tuple[int, ...], bool]] = {}

        def observation_key(
            observation: pilot_module._TheoryTransitionEvidence,
        ) -> tuple[Any, ...]:
            return (
                observation.claim.identity,
                observation.source,
                observation.execution_owner_token,
                observation.act_identity,
            )

        def observe_interpretation(
            state: _PilotState,
            attempt: _AttemptResult,
            bearing: Bearing,
            checkpoint: _CausalCheckpoint | None,
            *,
            prior_requirement_identities: frozenset[tuple[Any, ...]],
            intrascan_report: IntrascanResult | None = None,
        ) -> pilot_module._TheoryTransitionEvidence | None:
            observation = original_interpret(
                state,
                attempt,
                bearing,
                checkpoint,
                prior_requirement_identities=prior_requirement_identities,
                intrascan_report=intrascan_report,
            )
            if observation is None:
                return None
            executed = attempt.executed_attempt
            projected_scans = (
                tuple(sorted(executed.pulse._projection_cache)) if executed is not None else ()
            )
            if executed is not None:
                projection_receipts[observation_key(observation)] = (
                    executed.assertion_scan,
                    projected_scans,
                    executed.assertion_scan in projected_scans,
                )
            return observation

        def observe_recorded_interpretation(
            state: _PilotState,
            observation: pilot_module._TheoryTransitionEvidence | None,
            *,
            remaining_budget: int,
        ) -> None:
            nonlocal interpretation_count
            if observation is None:
                original_record(state, observation, remaining_budget=remaining_budget)
                return
            scan, projected_scans, assertion_cached = projection_receipts.pop(
                observation_key(observation),
                (observation.source.scan_id, (), False),
            )
            interpretation_count += 1
            interpretation = observation.interpretation
            messages.put(
                {
                    "type": "interpretation",
                    "elapsed": time.monotonic() - started,
                    "index": interpretation_count,
                    "scan": scan,
                    "kind": interpretation.kind.value,
                    "reason": interpretation.reason,
                    "supporting_identities": interpretation.supporting_identities,
                    "projected_scans": projected_scans,
                    "assertion_projection_cached": assertion_cached,
                    "stop": interpretation.kind.value == config["stop_interpretation"],
                }
            )
            original_record(state, observation, remaining_budget=remaining_budget)
            view = theory_view(state.theory_state)
            front = Compass().conductivity_front(view)
            if front is None:
                return
            request = Compass().conductivity_research(view)
            messages.put(
                {
                    "type": "conductivity",
                    "elapsed": time.monotonic() - started,
                    "attempts": tuple(
                        {
                            "source_scan": attempt.source.scan_id,
                            "stops": tuple(
                                (flow.displacement.tag, flow.displacement.rung)
                                for flow in attempt.flows
                                if flow.displacement is not None
                            ),
                            "requirement_count": len(attempt.requirements),
                        }
                        for attempt in front.attempts
                    ),
                    "comparisons": tuple(
                        {
                            "progress": comparison.progress.value,
                            "drifts": len(comparison.requirement_drifts),
                        }
                        for comparison in front.comparisons
                    ),
                    "research": request is not None,
                }
            )

        vars(pilot_module)["_theory_transition_from_attempt"] = observe_interpretation
        vars(pilot_module)["_record_working_theory_transition"] = (
            observe_recorded_interpretation
        )
        messages.put(
            {
                "type": "started",
                "elapsed": time.monotonic() - started,
                "scan": plc.state.scan_id,
            }
        )

        for event in pilot_events(
            plc,
            condition,
            max_scans=config["max_scans"],
            avoid_pred=avoid_pred,
        ):
            event_count += 1
            if event.kind in _DECISION_EVENT_KINDS:
                decision_count += 1
            last_scan = event.scan
            if event.kind == "iteration":
                last_snapshot = dict(event.data["snapshot"])
            rendered = formatter.format(event)
            decision_lines, stopped = _decision_lines(
                event,
                last_snapshot,
                config["stop_action"],
            )
            messages.put(
                {
                    "type": "event",
                    "elapsed": time.monotonic() - started,
                    "index": event_count,
                    "kind": event.kind,
                    "scan": event.scan,
                    "rendered": rendered,
                    "decision_lines": decision_lines,
                    "context": _event_context(event, last_snapshot),
                }
            )
            if stopped:
                messages.put(
                    {
                        "type": "result",
                        "status": "stopped",
                        "elapsed": time.monotonic() - started,
                        "events": event_count,
                        "scan": last_scan,
                        "reason": f"candidate construction surfaced {config['stop_action']!r}",
                    }
                )
                return
            if (
                config["decision_budget"] is not None
                and decision_count >= config["decision_budget"]
            ):
                messages.put(
                    {
                        "type": "result",
                        "status": "stopped",
                        "elapsed": time.monotonic() - started,
                        "events": event_count,
                        "scan": last_scan,
                        "reason": (
                            f"decision budget {config['decision_budget']} reached "
                            f"at {event.kind}"
                        ),
                    }
                )
                return
            if event.kind == "finished":
                messages.put(
                    {
                        "type": "result",
                        "status": "reached" if event.data.get("reached") else "not-reached",
                        "elapsed": time.monotonic() - started,
                        "events": event_count,
                        "scan": last_scan,
                        "reason": event.data.get("reason"),
                    }
                )
                return

        messages.put(
            {
                "type": "result",
                "status": "failed",
                "elapsed": time.monotonic() - started,
                "events": event_count,
                "scan": last_scan,
                "reason": "PILOT event stream ended without a finished event",
            }
        )
    except BaseException:  # noqa: BLE001 - report child failures to the orchestrator
        messages.put(
            {
                "type": "error",
                "elapsed": time.monotonic() - started,
                "events": event_count,
                "scan": last_scan,
                "traceback": traceback.format_exc(),
            }
        )


def _stop_worker(process: Any) -> None:
    if not process.is_alive():
        process.join(timeout=0.1)
        return
    process.terminate()
    process.join(timeout=2.0)
    if process.is_alive():
        process.kill()
        process.join(timeout=2.0)


def _worker_rss_mb(process: Any) -> float | None:
    """Return worker-tree RSS without letting a vanished child hide the result."""

    try:
        root = psutil.Process(process.pid)
        workers = (root, *root.children(recursive=True))
        return sum(worker.memory_info().rss for worker in workers) / (1024 * 1024)
    except (psutil.AccessDenied, psutil.NoSuchProcess, TypeError):
        return None


def _timeout_report(
    reason: str,
    *,
    elapsed: float,
    last_event: Mapping[str, Any] | None,
    last_visible: Mapping[str, Any] | None,
    recent: tuple[Mapping[str, Any], ...],
    current_rss_mb: float | None,
    peak_rss_mb: float,
) -> None:
    print(f"\n[watchdog] TIMEOUT after {elapsed:.2f}s: {reason}", file=sys.stderr)
    if current_rss_mb is not None:
        print(
            f"[watchdog] worker RSS: current={current_rss_mb:.1f} MB peak={peak_rss_mb:.1f} MB",
            file=sys.stderr,
        )
    if last_event is None:
        print("[watchdog] no PILOT event was received", file=sys.stderr)
    else:
        print(
            "[watchdog] last event: "
            f"#{last_event['index']} {last_event['kind']} scan={last_event['scan']} "
            f"at +{last_event['elapsed']:.2f}s {last_event['context']}",
            file=sys.stderr,
        )
    if last_visible is not None:
        print(
            "[watchdog] last DAP-visible progress: "
            f"{last_visible['visible']!r} at +{last_visible['elapsed']:.2f}s",
            file=sys.stderr,
        )
    if recent:
        print("[watchdog] recent events:", file=sys.stderr)
        for receipt in recent:
            print(
                f"  #{receipt['index']} {receipt['kind']} scan={receipt['scan']} "
                f"+{receipt['elapsed']:.2f}s {receipt['context']}",
                file=sys.stderr,
            )
    sys.stderr.flush()


def watch_worker(
    process: Any,
    messages: Any,
    dump_request: Any,
    dump_ready: Any,
    *,
    wall_budget_s: float,
    stall_budget_s: float,
    output_budget_s: float,
    memory_budget_mb: int | None = None,
    history: int = 8,
    dump_grace_s: float = 1.0,
) -> int:
    """Stream one worker and enforce clocks outside the computation under test."""

    started = time.monotonic()
    last_event_at = started
    last_visible_at = started
    previous_event_elapsed = 0.0
    max_event_gap = 0.0
    last_event: Mapping[str, Any] | None = None
    last_visible: Mapping[str, Any] | None = None
    recent: deque[Mapping[str, Any]] = deque(maxlen=max(1, history))
    dead_since: float | None = None
    worker_ready = False
    current_rss_mb: float | None = None
    peak_rss_mb = 0.0
    next_memory_sample = started

    while True:
        now = time.monotonic()
        wall_elapsed = now - started
        wall_remaining = wall_budget_s - wall_elapsed
        stall_remaining = stall_budget_s - (now - last_event_at) if worker_ready else float("inf")
        output_remaining = (
            output_budget_s - (now - last_visible_at) if worker_ready else float("inf")
        )
        wait_s = max(0.001, min(0.1, wall_remaining, stall_remaining, output_remaining))
        try:
            message = messages.get(timeout=wait_s)
        except queue_module.Empty:
            message = None

        now = time.monotonic()
        if message is not None:
            kind = message.get("type")
            if kind == "started":
                worker_ready = True
                last_event_at = now
                last_visible_at = now
                print(
                    f"[watch] worker ready at scan {message['scan']} (+{message['elapsed']:.2f}s)",
                    flush=True,
                )
            elif kind == "event":
                gap = max(0.0, float(message["elapsed"]) - previous_event_elapsed)
                previous_event_elapsed = float(message["elapsed"])
                max_event_gap = max(max_event_gap, gap)
                last_event_at = now
                last_event = message
                recent.append(message)
                rendered = message.get("rendered")
                if rendered:
                    print(rendered, end="", flush=True)
                    last_visible_at = now
                    last_visible = {
                        "visible": str(rendered).strip(),
                        "elapsed": message["elapsed"],
                    }
                for line in message.get("decision_lines") or ():
                    print(line, flush=True)
                    last_visible_at = now
                    last_visible = {"visible": line, "elapsed": message["elapsed"]}
            elif kind == "interpretation":
                last_event_at = now
                line = _interpretation_line(message)
                print(line, flush=True)
                last_visible_at = now
                last_visible = {"visible": line, "elapsed": message["elapsed"]}
                if message.get("stop"):
                    _stop_worker(process)
                    print(
                        f"[watch] stopped at {message['kind']} after "
                        f"{message['elapsed']:.2f}s; scan={message['scan']}",
                        flush=True,
                    )
                    return EXIT_STOPPED
            elif kind == "conductivity":
                last_event_at = now
                line = _conductivity_line(message)
                print(line, flush=True)
                last_visible_at = now
                last_visible = {"visible": line, "elapsed": message["elapsed"]}
            elif kind == "error":
                print(message["traceback"], file=sys.stderr, flush=True)
                process.join(timeout=1.0)
                if process.is_alive():
                    _stop_worker(process)
                return EXIT_FAILED
            elif kind == "result":
                current_rss_mb = _worker_rss_mb(process)
                if current_rss_mb is not None:
                    peak_rss_mb = max(peak_rss_mb, current_rss_mb)
                process.join(timeout=2.0)
                if process.is_alive():
                    _stop_worker(process)
                status = message["status"]
                summary = (
                    f"[watch] {status} after {message['elapsed']:.2f}s; "
                    f"events={message['events']} scan={message['scan']} "
                    f"max_event_gap={max_event_gap:.2f}s peak_rss={peak_rss_mb:.1f}MB"
                )
                if message.get("reason"):
                    summary += f" reason={message['reason']!r}"
                print(summary, flush=True)
                if status == "reached":
                    return 0
                if status == "stopped":
                    return EXIT_STOPPED
                return EXIT_FAILED

        now = time.monotonic()
        wall_elapsed = now - started
        if now >= next_memory_sample:
            current_rss_mb = _worker_rss_mb(process)
            if current_rss_mb is not None:
                peak_rss_mb = max(peak_rss_mb, current_rss_mb)
            next_memory_sample = now + 0.25
        timeout_reason = None
        if wall_elapsed >= wall_budget_s:
            timeout_reason = f"drive exceeded the {wall_budget_s:g}s wall budget"
        elif (
            memory_budget_mb is not None
            and current_rss_mb is not None
            and (current_rss_mb >= memory_budget_mb)
        ):
            timeout_reason = (
                f"worker RSS {current_rss_mb:.1f} MB exceeded the "
                f"{memory_budget_mb} MB memory budget"
            )
        elif worker_ready and now - last_event_at >= stall_budget_s:
            timeout_reason = (
                f"no structured PILOT event for {stall_budget_s:g}s "
                "(the current operation stopped yielding DAP progress)"
            )
        elif worker_ready and now - last_visible_at >= output_budget_s:
            timeout_reason = (
                f"no DAP-visible progress for {output_budget_s:g}s "
                "despite any internal PILOT events"
            )
        if timeout_reason is not None:
            _timeout_report(
                timeout_reason,
                elapsed=wall_elapsed,
                last_event=last_event,
                last_visible=last_visible,
                recent=tuple(recent),
                current_rss_mb=current_rss_mb,
                peak_rss_mb=peak_rss_mb,
            )
            dump_request.set()
            dump_ready.wait(timeout=max(0.0, dump_grace_s))
            _stop_worker(process)
            return EXIT_TIMEOUT

        if not process.is_alive():
            if dead_since is None:
                dead_since = now
            elif now - dead_since >= 0.5:
                print(
                    f"[watchdog] worker exited with code {process.exitcode} "
                    "without a terminal receipt",
                    file=sys.stderr,
                    flush=True,
                )
                return EXIT_FAILED


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream one Tumbler PILOT drive with a killable performance watchdog."
    )
    parser.add_argument(
        "--fixture",
        default="tests.fixtures.tumbler",
        help="module exposing the Program as `logic` (default: tests.fixtures.tumbler)",
    )
    parser.add_argument(
        "--target",
        type=_target,
        default=("Sts_State_Completed", True),
        help="Boolean TAG or TAG=VALUE (default: Sts_State_Completed)",
    )
    parser.add_argument(
        "--avoid",
        action="append",
        help="tag to avoid; repeat for a union (default: Cmd_State_Complete)",
    )
    parser.add_argument(
        "--no-avoid",
        action="store_true",
        help="disable the default Cmd_State_Complete avoidance",
    )
    parser.add_argument("--max-scans", type=int, default=1_000_000)
    parser.add_argument(
        "--entry-steps",
        type=_nonnegative_int,
        default=1,
        help="ordinary scans to execute before how() (default: 1)",
    )
    parser.add_argument(
        "--decision-budget",
        type=_positive_int,
        help=(
            "terminate immediately after this many action/composition decisions; "
            "the parent memory and time budgets still guard work between decisions"
        ),
    )
    parser.add_argument("--wall-budget", type=_positive_seconds, default=240.0)
    parser.add_argument(
        "--stall-budget",
        type=_positive_seconds,
        default=30.0,
        help="maximum silence between structured PILOT events (default: 30s)",
    )
    parser.add_argument(
        "--output-budget",
        type=_positive_seconds,
        default=30.0,
        help="maximum silence between DAP-visible progress fragments (default: 30s)",
    )
    parser.add_argument(
        "--memory-budget-mb",
        type=_positive_int,
        default=4096,
        help="maximum worker-tree RSS before termination (default: 4096 MB)",
    )
    parser.add_argument("--dt", type=float, default=0.010)
    parser.add_argument(
        "--stop-action",
        type=_target,
        default=("Cmd_Reset2FactoryDefault", True),
        help="fail fast when candidate construction surfaces TAG or TAG=VALUE",
    )
    parser.add_argument(
        "--stop-interpretation",
        choices=(
            "keep_and_reread",
            "coast_to_boundary",
            "setup_first",
            "retry_together",
            "retry_through_deadline",
            "unresolved",
        ),
        help="stop after printing the first matching temporal interpretation",
    )
    parser.add_argument(
        "--history",
        type=int,
        default=8,
        help="recent event receipts printed on timeout (default: 8)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    avoid = () if args.no_avoid else tuple(args.avoid or ("Cmd_State_Complete",))
    config = {
        "fixture": args.fixture,
        "target": args.target,
        "avoid": avoid,
        "max_scans": args.max_scans,
        "entry_steps": args.entry_steps,
        "decision_budget": args.decision_budget,
        "dt": args.dt,
        "stop_action": args.stop_action,
        "stop_interpretation": args.stop_interpretation,
    }
    context = mp.get_context("spawn")
    messages = context.Queue()
    dump_request = context.Event()
    dump_ready = context.Event()
    process = context.Process(
        target=_drive_worker,
        args=(config, messages, dump_request, dump_ready),
        name="pyrung-pilot-watch",
    )
    print(
        f"[watch] how({args.target[0]}={args.target[1]!r})"
        + (f" avoid {', '.join(avoid)}" if avoid else "")
        + (
            f"; wall={args.wall_budget:g}s stall={args.stall_budget:g}s "
            f"output={args.output_budget:g}s memory={args.memory_budget_mb}MB"
        )
        + f" entry_steps={args.entry_steps}"
        + (
            f" decisions={args.decision_budget}"
            if args.decision_budget is not None
            else ""
        ),
        flush=True,
    )
    process.start()
    try:
        return watch_worker(
            process,
            messages,
            dump_request,
            dump_ready,
            wall_budget_s=args.wall_budget,
            stall_budget_s=args.stall_budget,
            output_budget_s=args.output_budget,
            memory_budget_mb=args.memory_budget_mb,
            history=max(1, args.history),
        )
    except KeyboardInterrupt:
        print("\n[watch] interrupted; stopping worker", file=sys.stderr, flush=True)
        _stop_worker(process)
        return 130
    finally:
        messages.close()
        messages.join_thread()


if __name__ == "__main__":
    sys.exit(main())
