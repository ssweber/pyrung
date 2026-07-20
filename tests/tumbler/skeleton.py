"""Decision-skeleton extraction from the PILOT event stream.

A "decision skeleton" is the ordered list of pilot events reduced to the
fields that record *reasoning* — which candidates were built, in what order,
which were accepted/rejected and on what grounds, route choices, provisional
lifecycle outcomes, investigation slugs, correction kinds, and zoom
requested-vs-landed pairs.  Everything run-variable is dropped: scan ids,
timestamps, dwell/duration counts, fork ids, memory/perf numbers.

The output is JSON-serializable and deterministic: every set/frozenset is
sorted (frozenset iteration order is PYTHONHASHSEED-salted — the #1
cold-process determinism hazard), run-specific scan numbers are normalized,
and object addresses become stable encounter-ordered tokens.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

# ---------------------------------------------------------------------------
# Scrubbing
# ---------------------------------------------------------------------------

#: Strings that embed scan numbers get them normalized, never dropped —
#: "coasted 799 scans" and "at scan 2011" both carry decision *shape* worth
#: keeping, but the count itself is run-variable.
_SCAN_NUMBER_PATTERNS = (
    re.compile(r"\bscan\s+\d+", re.IGNORECASE),
    re.compile(r"\b\d+\s+scans?\b", re.IGNORECASE),
)

#: Default object reprs include a process-specific address.  Conditions can
#: appear inside kept ``PilotRung.guard`` values; after canonicalizing set-like
#: fields, these addresses become stable encounter-ordered identity tokens.
_OBJECT_ADDRESS_RE = re.compile(r"(?<= at )0x[0-9a-fA-F]+(?=>)")
_ACTIVE_LATCH_DETAIL_RE = re.compile(r"^(clear \d+ active latches: )(.+)$")

#: Payload keys dropped everywhere, even for unknown event kinds — a
#: defensive layer so a new emitter can't smuggle a scan id or a perf
#: number into the skeleton through the generic fallback.
_DROP_KEY_RE = re.compile(
    r"scan|snapshot|state_key|new_key|checkpoint_key|seen_key|_count$|"
    r"coast_span|settle_scans|dwell|wake|memory|elapsed|duration|"
    r"^key$|^work$|^steps$|^journey$|^tree$",
)


def _scrub(text: str) -> str:
    for pat in _SCAN_NUMBER_PATTERNS:
        text = pat.sub(lambda m: re.sub(r"\d+", "<N>", m.group(0)), text)
    match = _ACTIVE_LATCH_DETAIL_RE.match(text)
    if match:
        names = sorted(part.strip() for part in match.group(2).split(","))
        return match.group(1) + ", ".join(names)
    return text


def _canonicalize_object_addresses(value: Any) -> Any:
    """Replace repr addresses with stable tokens while preserving aliases."""
    tokens: dict[str, str] = {}

    def walk(item: Any) -> Any:
        if isinstance(item, str):

            def replace(match: re.Match[str]) -> str:
                address = match.group(0)
                token = tokens.get(address)
                if token is None:
                    token = f"<ADDR:{len(tokens) + 1}>"
                    tokens[address] = token
                return token

            return _OBJECT_ADDRESS_RE.sub(replace, item)
        if isinstance(item, list):
            return [walk(child) for child in item]
        if isinstance(item, Mapping):
            return {key: walk(item[key]) for key in sorted(item, key=str)}
        return item

    return walk(value)


def _sort_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _address_neutral_sort_key(value: Any) -> str:
    """Stable ordering for values whose repr contains process-local addresses.

    Encounter-ordered address tokens are assigned only after the complete
    skeleton is assembled.  Sorting first on raw addresses makes equivalent
    PilotRungs swap order across processes, so erase only the address component
    for this comparison.
    """

    def neutralize(item: Any) -> Any:
        if isinstance(item, str):
            return _OBJECT_ADDRESS_RE.sub("<ADDR>", item)
        if isinstance(item, list):
            return [neutralize(child) for child in item]
        if isinstance(item, Mapping):
            return {key: neutralize(item[key]) for key in sorted(item, key=str)}
        return item

    return _sort_key(neutralize(value))


# ---------------------------------------------------------------------------
# JSON-ification of payload values
# ---------------------------------------------------------------------------


def _jsonify(value: Any) -> Any:
    """Convert a payload value to a deterministic JSON-safe form."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _scrub(value)
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonify(v) for v in value), key=_sort_key)
    if isinstance(value, Mapping):
        return {str(k): _jsonify(v) for k, v in value.items() if not _DROP_KEY_RE.search(str(k))}
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonify_dataclass(value)
    # Route objects and other labeled values: keep the label, not the guts.
    label = getattr(value, "label", None)
    if isinstance(label, str):
        return {"label": _scrub(label)}
    # Expressions / conditions render deterministically via repr.
    return _scrub(repr(value))


#: Per-dataclass keep-lists.  Anything not named here falls back to a
#: type-name marker rather than a full asdict (which could drag in forks,
#: snapshots, or scan ids).
_DATACLASS_KEEP: dict[str, tuple[str, ...]] = {
    "PilotGateEvent": ("event", "detail"),
    "TagChange": ("tag", "before", "after"),
    "PipelineRoles": ("channel_tag", "request_tags"),
    "PilotRung": ("dest", "value", "guard"),
    "_HoldLogEntry": ("source", "tags"),
    "PlanStep": (
        "kind",
        "label",
        "transition",
        "waiting_for",
        "inputs",
        "steady_holds",
        "pulsing_holds",
        "accelerators",
        "notes",
    ),
    "_Step": ("inputs",),
    # Receipt vocabulary (pilot/coast.py): decision-shaped fields only — scan
    # ids, budgets, and fold counters are run-variable and stay out.
    "CoastReceipt": ("kind", "stop_reason", "fired"),
    "BumpEvent": ("name", "kind", "transitions"),
}


def _jsonify_dataclass(value: Any) -> Any:
    name = type(value).__name__
    keep = _DATACLASS_KEEP.get(name)
    if keep is None:
        return {"__type__": name}
    out: dict[str, Any] = {"__type__": name}
    for field in keep:
        if hasattr(value, field):
            out[field] = _jsonify(getattr(value, field))
    if name == "_HoldLogEntry" and isinstance(out.get("tags"), list):
        out["tags"] = sorted(out["tags"], key=_sort_key)
    elif name == "PlanStep":
        for field in ("steady_holds", "pulsing_holds", "accelerators"):
            if isinstance(out.get(field), list):
                out[field] = sorted(out[field], key=_sort_key)
        if out.get("kind") == "force" and isinstance(out.get("inputs"), list):
            out["inputs"] = sorted(out["inputs"], key=_sort_key)
            out["label"] = ", ".join(str(pair[0]) for pair in out["inputs"])
    return out


# ---------------------------------------------------------------------------
# Per-event keep-lists
# ---------------------------------------------------------------------------

#: Candidate payload fields kept (see ``_candidate_payload`` in recording.py).
#: ``wake`` (downstream fan-out magnitude) is deliberately dropped.
_CANDIDATE_KEEP = (
    "tag",
    "value",
    "pair",
    "influence_prescribed",
    "route_prescribed",
    "bearing_channel_tag",
    "bearing_channel_value",
    "current_prescribed",
    "current_note",
    "provenance",
    "prescribed",
    "scored",
    "avail_tier",
    "over_wake",
    "compass_score",
)

_ROUTE_PLAN_KEEP = ("needed", "channel_tag", "target_value", "path")

#: Investigation payload: decision grounds only (slugs + ground text landed
#: for exactly this skeleton; see progress.py ``_rejection_detail``).
_HYPOTHESIS_KEEP = ("kind", "holds", "sources", "detail", "slug", "ground")

#: kind -> payload keys kept.  Conservative: more decision fields rather
#: than fewer, but absolutely no scan numbers.
_EVENT_KEEP: dict[str, tuple[str, ...]] = {
    "started": (
        "target",
        "route",
        "blocked_route_actions",
        "pipeline_roles",
        "steerable_count",
        "opaque_loop",
    ),
    "iteration": (
        "target",
        "distance",
        "still_need",
        "nogoods",
        "raw_trace_actions",
        "watch_tags",
    ),
    "candidates_built": (
        "candidates",
        "trace_actions",
        "active_trace_actions",
        "route_candidates",
        "route_plan",
        "wait_prescribed",
        "wait_reason",
        "stuck_reason",
    ),
    "candidate_try": ("index", "total", "candidate", "applied", "co_actions"),
    "candidate_rejected": ("index", "candidate", "applied", "co_actions", "gates"),
    "candidate_accepted": (
        "index",
        "candidate_detail",
        "applied",
        "co_actions",
        "gates",
        "accepted_because",
    ),
    "trial_committed": ("candidate", "applied"),
    "batch_accepted": ("candidate", "applied", "gates", "trend"),
    "widening_accepted": ("candidate", "applied", "gates", "trend"),
    "letrun_ejection": (
        "channel_tag",
        "from_value",
        "requested_value",
        "to_value",
        "observe_label",
        "investigated",
        "reason",
    ),
    "provisional_started": (
        "channel_tag",
        "from_value",
        "requested_value",
        "settled_value",
        "reason",
        "route",
        "gauge_at_source",
        "classification",
    ),
    "provisional_promoted": (
        "channel_tag",
        "from_value",
        "gauge_at_source",
        "landing_mark",
        "trend",
        "terminal",
    ),
    "provisional_regressed": (
        "channel_tag",
        "from_value",
        "reason",
        "classification",
        "gauge_at_source",
        "landing_mark",
        "trend",
        "terminal",
    ),
    "provisional_expired": (
        "channel_tag",
        "from_value",
        "reason",
        "classification",
        "gauge_at_source",
        "landing_mark",
        "trend",
        "terminal",
    ),
    "trend_checkpoint": ("trend", "channel", "channel_value", "baseline_trend", "provisional"),
    "trend_regression": (
        "from_trend",
        "to_trend",
        "channel_transitions",
        "regression_nogoods",
        "investigation",
    ),
    "zoom": ("prescribed", "reason", "channel_tag"),
    "zoom_accepted": (
        "observe_label",
        "outcome",
        "zoom_channel_tag",
        "zoom_target_value",
        "zoom_actual_value",
        "bearing_stop_reason",
        "ejected",
        "trend",
    ),
    "zoom_rejected": ("gates",),
    "skiff": ("reason", "observations"),
    "stuck": ("reason", "distance", "candidate_count", "nogoods_at_key", "terminal"),
    "finished": ("reached", "reason", "knowledge", "plan_journal"),
}

#: Fallback for event kinds unknown to this extractor: keep only fields with
#: clearly decision-shaped names, so a new emitter degrades to a partial
#: record instead of crashing or leaking run-variable data.
_GENERIC_KEEP = (
    "reason",
    "channel_tag",
    "tag",
    "value",
    "pair",
    "outcome",
    "observe_label",
    "classification",
    "label",
    "detail",
    "slug",
    "gates",
    "candidate",
    "applied",
    "from_value",
    "to_value",
    "requested_value",
    "settled_value",
    "trend",
    "distance",
    "terminal",
    "reached",
)


def _extract_candidate(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {k: _jsonify(payload.get(k)) for k in _CANDIDATE_KEEP if k in payload}


def _extract_route_plan(plan: Mapping[str, Any] | None) -> Any:
    if plan is None:
        return None
    out: dict[str, Any] = {}
    for k in _ROUTE_PLAN_KEEP:
        if k != "path" and k in plan:
            out[k] = _jsonify(plan[k])
    steps = []
    for step in plan.get("path", ()):
        steps.append(
            {
                k: _jsonify(step.get(k))
                for k in ("from", "to", "action", "request", "enablers")
                if k in step
            }
        )
    out["path"] = steps
    return out


def _extract_hypothesis(detail: Mapping[str, Any]) -> dict[str, Any]:
    out = {k: _jsonify(detail.get(k)) for k in _HYPOTHESIS_KEEP if k in detail}
    for field in ("holds", "sources"):
        if isinstance(out.get(field), list):
            out[field] = sorted(out[field], key=_address_neutral_sort_key)
    return out


def _extract_investigation(inv: Mapping[str, Any] | None) -> Any:
    if inv is None:
        return None
    return {
        "hypotheses": inv.get("hypotheses"),
        "confirmed": inv.get("confirmed"),
        "rejected": inv.get("rejected"),
        "unresolved": _jsonify(inv.get("unresolved")),
        "hypothesis_detail": [_extract_hypothesis(h) for h in inv.get("hypothesis_detail", ())],
        "confirmed_detail": [_extract_hypothesis(h) for h in inv.get("confirmed_detail", ())],
        "rejected_detail": [_extract_hypothesis(h) for h in inv.get("rejected_detail", ())],
    }


def _extract_accepted_because(because: Mapping[str, Any] | None) -> Any:
    if because is None:
        return None
    return {
        "gate_events": _jsonify(because.get("gate_events")),
        "trend_before": because.get("trend_before"),
        "trend_after": because.get("trend_after"),
        "state_key_changed": because.get("state_key_changed"),
        "novel_key": because.get("novel_key"),
        "target_reached": because.get("target_reached"),
    }


def _extract_knowledge(knowledge: Mapping[str, Any] | None) -> Any:
    if knowledge is None:
        return None
    return {
        "hold_log": _jsonify(knowledge.get("hold_log", ())),
        "lever_notes": _jsonify(knowledge.get("lever_notes", {})),
        "skiff_decline": _jsonify(knowledge.get("skiff_decline")),
        "avoid_names": _jsonify(knowledge.get("avoid_names", ())),
    }


def extract_skeleton(events: Iterable[Any]) -> list[dict[str, Any]]:
    """Reduce a pilot event stream to its JSON-serializable decision skeleton."""
    skeleton: list[dict[str, Any]] = []
    for event in events:
        kind = event.kind
        data = event.data if isinstance(event.data, Mapping) else {}
        entry: dict[str, Any] = {"kind": kind}

        keep = _EVENT_KEEP.get(kind)
        if keep is None:
            keep = tuple(k for k in _GENERIC_KEEP if k in data)

        for key in keep:
            if key not in data:
                continue
            value = data[key]
            if key in ("candidate", "candidate_detail") and isinstance(value, Mapping):
                # ``candidate`` is the full rank-rationale dict on try/reject
                # events but a bare ``{tag: value}`` action map on committed /
                # batch / widening events — keep the action map verbatim.
                if "tag" in value and "pair" in value:
                    entry[key] = _extract_candidate(value)
                else:
                    entry[key] = _jsonify(value)
            elif key == "candidates":
                entry[key] = [_extract_candidate(c) for c in value]
            elif key == "route_plan":
                entry[key] = _extract_route_plan(value)
            elif key == "investigation":
                entry[key] = _extract_investigation(value)
            elif key == "accepted_because":
                entry[key] = _extract_accepted_because(value)
            elif key == "knowledge":
                entry[key] = _extract_knowledge(value)
            else:
                entry[key] = _jsonify(value)

        # The watched diff on an acceptance (target-channel movement) is a
        # decision record — what the press actually moved on the bearing.
        if kind == "candidate_accepted":
            changes = data.get("changes")
            if isinstance(changes, Mapping):
                entry["watched_changes"] = _jsonify(changes.get("watched", ()))

        skeleton.append(entry)
        if kind == "finished":
            break
    return _canonicalize_object_addresses(skeleton)


def dump_skeleton(skeleton: list[dict[str, Any]]) -> str:
    """Pretty, sorted, reviewable rendering (one event per block)."""
    return json.dumps(skeleton, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def first_divergence(
    a: list[dict[str, Any]], b: list[dict[str, Any]]
) -> tuple[int, Any, Any] | None:
    """Index and both entries at the first structural difference, or None."""
    for i in range(max(len(a), len(b))):
        ea = a[i] if i < len(a) else "<missing: stream A ended>"
        eb = b[i] if i < len(b) else "<missing: stream B ended>"
        if ea != eb:
            return i, ea, eb
    return None


def divergence_message(
    a: list[dict[str, Any]], b: list[dict[str, Any]], a_name: str, b_name: str
) -> str:
    """Readable structural diff: first divergent index + both events."""
    div = first_divergence(a, b)
    if div is None:
        return f"{a_name} and {b_name} are identical"
    i, ea, eb = div
    return (
        f"decision skeletons diverge at event index {i} "
        f"({a_name}: {len(a)} events, {b_name}: {len(b)} events)\n"
        f"--- {a_name}[{i}] ---\n{json.dumps(ea, indent=1, sort_keys=True, default=str)}\n"
        f"--- {b_name}[{i}] ---\n{json.dumps(eb, indent=1, sort_keys=True, default=str)}"
    )
