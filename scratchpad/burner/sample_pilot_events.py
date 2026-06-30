"""Sample the structured PILOT event stream on the burner project."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

CLICK_PROJECT = Path(
    os.environ.get(
        "PYRUNG_CLICK_PROJECT",
        r"C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project",
    )
)
sys.path.insert(0, str(CLICK_PROJECT))

from main import logic  # noqa: E402

from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.pilot import pilot_events  # noqa: E402


def _interesting(event_kind: str) -> bool:
    return event_kind in {
        "started",
        "iteration",
        "candidates_built",
        "candidate_try",
        "candidate_rejected",
        "candidate_accepted",
        "trial_committed",
        "trend_checkpoint",
        "trend_regression",
        "zoom",
        "zoom_accepted",
        "zoom_rejected",
        "finished",
    }


def _pair_text(pair: tuple[str, object]) -> str:
    tag, value = pair
    return f"{tag}={value!r}"


def _action_detail_text(detail: object) -> str:
    bits = [_pair_text(detail.pair)]
    if detail.provenance:
        bits.append(f"via {', '.join(detail.provenance)}")
    if detail.blast_radius is not None:
        bits.append(f"blast={detail.blast_radius}")
    return "  ".join(bits)


def _candidate_text(candidate: dict[str, object]) -> str:
    bits = [_pair_text(candidate["pair"])]
    if candidate.get("route_prescribed"):
        bits.append("route")
    if candidate.get("influence_prescribed"):
        bits.append("influence")
    provenance = candidate.get("provenance")
    if provenance:
        bits.append(f"via {', '.join(provenance)}")
    blast_radius = candidate.get("blast_radius")
    if blast_radius is not None:
        bits.append(f"blast={blast_radius}")
    return "  ".join(bits)


def _print_pairs(label: str, pairs: object) -> None:
    print(f"  {label}:")
    if not pairs:
        print("    (none)")
        return
    for pair in pairs:
        print(f"    - {_pair_text(pair)}")


def _print_pipeline_roles(roles: Any, internal_tags: Any) -> None:
    print(f"  pipeline_internal_tags: {len(internal_tags)}")
    if internal_tags:
        for tag in sorted(internal_tags):
            print(f"    - {tag}")
    else:
        print("    (none)")
    print(f"  pipeline_roles: {len(roles)}")
    if not roles:
        print("    (none)")
        return
    for role in roles:
        print(f"    - governing: {role.governing_tag}")
        print(f"      request: {sorted(role.request_tags)}")
        print(f"      guards: {sorted(role.guard_internal_tags)}")
        print(f"      scratch: {sorted(role.scratch_internal_tags)}")


def _print_action_details(label: str, details: object) -> None:
    print(f"  {label}:")
    if not details:
        print("    (none)")
        return
    for detail in details:
        print(f"    - {_action_detail_text(detail)}")


def _print_route_plan(plan: object) -> None:
    print("  route_plan:")
    if not plan:
        print("    (none)")
        return
    needed_tag, needed_value = plan["needed"]
    print(
        f"    need: {needed_tag}={needed_value!r} "
        f"via {plan['governing_tag']} -> {plan['target_value']!r}"
    )
    for step in plan["path"]:
        action = step["action"]
        action_text = _pair_text(action) if action else "subgoal"
        request = step["request"]
        request_text = f" request={_pair_text(request)}" if request else ""
        print(f"    - {step['from']!r} -> {step['to']!r}: {action_text}{request_text}")


def _change_text(change: object) -> str:
    return f"{change.tag}: {change.before!r} -> {change.after!r}"


def _print_changes(label: str, changes: object) -> None:
    print(f"  {label}:")
    if not changes:
        print("    (none)")
        return
    for change in changes:
        print(f"    - {_change_text(change)}")


def _print_change_group(label: str, grouped_changes: object) -> None:
    print(f"  {label}:")
    for group_name, changes in grouped_changes.items():
        print(f"    {group_name}:")
        if not changes:
            print("      (none)")
            continue
        for change in changes:
            print(f"      - {_change_text(change)}")


def _print_scan_header(scan: int, last_scan: int | None) -> int:
    if scan != last_scan:
        if last_scan is not None:
            print()
        print("=" * 72)
        print(f"scan {scan}")
        print("=" * 72)
    return scan


def _print_event(event) -> None:
    data = event.data
    print(event.kind)
    if event.kind == "started":
        target_tag, target_value = data["target"]
        print(f"  target: {target_tag}={target_value!r}")
        print(f"  steerable_count: {data['steerable_count']}")
        print(f"  opaque_loop_count: {len(data['opaque_loop'])}")
        print(f"  choice: {getattr(data['choice'], 'label', None)}")
        _print_pipeline_roles(data["pipeline_roles"], data["pipeline_internal_tags"])
        _print_pairs("blocked_choice_actions", data["blocked_choice_actions"])
    elif event.kind == "iteration":
        print(f"  distance: {data['distance']}")
        print(f"  seen_key_count: {data['seen_key_count']}")
        print(f"  checkpoint_count: {data['checkpoint_count']}")
        print("  still_need:")
        for need in data["still_need"]:
            print(f"    - {need}")
        _print_action_details("raw_trace_actions", data["raw_trace_action_details"])
        _print_pairs("nogoods", sorted(data["nogoods"]))
    elif event.kind == "candidates_built":
        print(f"  candidate_count: {len(data['candidates'])}")
        print(f"  blast_cap: {data['blast_cap']}")
        if data.get("wait_prescribed"):
            print(f"  wait_prescribed: {data.get('wait_reason')}")
        if data.get("stuck_reason"):
            print(f"  stuck_reason: {data['stuck_reason']}")
        _print_action_details("trace_actions", data["trace_action_details"])
        _print_pairs("route_candidates", data["route_candidates"])
        _print_route_plan(data["route_plan"])
        if data.get("prerequisite_holds"):
            _print_pairs("prerequisite_holds", data["prerequisite_holds"])
        print("  candidates:")
        for candidate in data["candidates"]:
            print(f"    - {_candidate_text(candidate)}")
    elif event.kind in {"candidate_try", "candidate_rejected", "candidate_accepted"}:
        print(f"  candidate: {_candidate_text(data['candidate'])}")
        if "index" in data and "total" in data:
            print(f"  index: {data['index'] + 1}/{data['total']}")
        _print_pairs("pulse_actions", data.get("pulse_actions", ()))
        _print_pairs("context_actions", data.get("context_actions", ()))
        gates = data.get("gates", ())
        print("  gates:")
        if gates:
            for gate in gates:
                suffix = f" ({gate.detail})" if gate.detail else ""
                print(f"    - {gate.event}{suffix}")
        else:
            print("    (none)")
        if "trend" in data:
            print(f"  trend: {data['trend']}")
        if event.kind == "candidate_accepted":
            why = data["accepted_because"]
            print("  accepted_because:")
            print(f"    trend: {why['trend_before']} -> {why['trend_after']}")
            print(f"    state_key_changed: {why['state_key_changed']}")
            print(f"    novel_key: {why['novel_key']}")
            print(f"    target_reached: {why['target_reached']}")
            _print_change_group("changes", data["changes"])
    elif event.kind == "trial_committed":
        print(f"  decision: {data['decision']}")
        _print_pairs("pulse_actions", data.get("pulse_actions", ()))
    elif event.kind == "trend_checkpoint":
        print(f"  trend: {data['trend']}")
        print(f"  checkpoint_count: {data['checkpoint_count']}")
    elif event.kind == "trend_regression":
        print(f"  from_trend: {data['from_trend']}")
        print(f"  to_trend: {data['to_trend']}")
        _print_pairs("regression_nogoods", sorted(data["regression_nogoods"]))
        inv = data.get("investigation", {})
        if inv:
            print(f"  investigation:")
            print(f"    hypotheses: {inv.get('hypotheses', 0)}")
            print(f"    confirmed: {inv.get('confirmed', 0)}")
            print(f"    rejected: {inv.get('rejected', 0)}")
            unresolved = inv.get("unresolved", ())
            if unresolved:
                print(f"    unresolved: {list(unresolved)[:10]}")
            for h in inv.get("hypothesis_detail", ()):
                holds_str = ", ".join(f"{t}={v!r}" for t, v in h["holds"])
                print(f"    [{h['kind']}] {holds_str}")
                if h.get("detail"):
                    print(f"      {h['detail']}")
    elif event.kind == "wait":
        print(f"  prescribed: {data.get('prescribed', False)}")
        if data.get("reason"):
            print(f"  reason: {data['reason']}")
        if data.get("holds"):
            _print_pairs("holds", data["holds"])
        print("  watch_tags:")
        for tag in data["watch_tags"]:
            print(f"    - {tag}")
    elif event.kind == "zoom":
        print(f"  prescribed: {data.get('prescribed', False)}")
        if data.get("reason"):
            print(f"  reason: {data['reason']}")
        if data.get("governing_tag"):
            print(f"  governing_tag: {data['governing_tag']}")
        if data.get("prerequisite_holds"):
            _print_pairs("prerequisite_holds", data["prerequisite_holds"])
    elif event.kind == "zoom_accepted":
        print(f"  trend: {data.get('trend')}")
        print(f"  outcome: {data.get('outcome')}")
        print(f"  scan_before: {data.get('scan_before')}")
        print(f"  scan_after: {data.get('scan_after')}")
        snap = data.get("snapshot", {})
        if snap:
            gov = snap.get("S_StateCurrent")
            if gov is not None:
                print(f"  S_StateCurrent: {gov}")
            for key in sorted(snap):
                if "Alm" in key and snap[key]:
                    print(f"  {key}: {snap[key]}")
    elif event.kind == "zoom_rejected":
        gates = data.get("gates", ())
        print("  gates:")
        if gates:
            for gate in gates:
                suffix = f" ({gate.detail})" if gate.detail else ""
                print(f"    - {gate.event}{suffix}")
        else:
            print("    (none)")
    elif event.kind == "finished":
        print(f"  reached: {data['reached']}")
        print(f"  reason: {data['reason']}")
        print(f"  steps: {len(data['steps'])} (clean path)")
        print(f"  journey: {len(data.get('journey', ()))} (attempts incl. reverted)")


def main() -> int:
    plc = PLC(logic)
#    for name, value in {
#        "x_DoorClosed": True,
#        "x_LintDoorClosed": True,
#        "x_BlowerFB": True,
#        "x_RotateFB": True,
#        "x_RotateSensor": False,
#        "x_SailRelay": True,
#    }.items():
#        plc.force(name, value)
    plc.step()

    target = plc._known_tags_by_name["y_BurnerLoop"]
    max_events = int(os.environ.get("PILOT_MAX_EVENTS", "100"))
    # Starting->Execute is a genuinely ~1400-scan completion (timer-gated SFC
    # step-counters); folding saves wall-clock, not scan budget, so the real-
    # scan allowance must cover it.
    max_scans = int(os.environ.get("PILOT_MAX_SCANS", "100000"))
    kept = 0
    last_scan: int | None = None
    for event in pilot_events(plc, target, choice=1, max_scans=max_scans):
        if not _interesting(event.kind):
            continue
        last_scan = _print_scan_header(event.scan, last_scan)
        _print_event(event)
        kept += 1
        if event.kind == "finished":
            break
        if kept >= max_events:
            print(f"\n[stopped after {kept} events at scan {event.scan}]")
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
