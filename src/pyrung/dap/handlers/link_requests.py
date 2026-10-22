"""Bench-link custom requests for DAP.

Owns pyrungLink, pyrungUnlink, pyrungRestoreLink, pyrungSetLinks and
pyrungListLinks: structured entry points parallel to the ``link`` /
``unlink`` / ``restore`` / ``links`` console verbs (see ``pyrung.dap.links``).
Tags are tag names or Click addresses.  Each change is recorded in an active
capture as its console command, so a replay rebuilds the same wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pyrung.dap import links
from pyrung.dap.capture import capture_hook

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]


@dataclass(frozen=True)
class _LinkRequestArgs:
    feedback: Any = None
    enable: Any = None
    trigger: Any = None
    onDelay: Any = None
    offDelay: Any = None
    profile: Any = None
    physical: Any = None
    system: Any = None


@dataclass(frozen=True)
class _FeedbackRequestArgs:
    feedback: Any = None


@dataclass(frozen=True)
class _SetLinksRequestArgs:
    bench: Any = None
    enabled: Any = None


def _require_text(adapter: Any, value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise adapter.DAPAdapterError(f"{field} is required")
    return value.strip()


def _optional_text(adapter: Any, value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise adapter.DAPAdapterError(f"{field} must be a non-empty string")
    return value.strip()


def on_pyrung_link(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_LinkRequestArgs, args)
    feedback = _require_text(adapter, parsed.feedback, "pyrungLink.feedback")
    enable = _require_text(adapter, parsed.enable, "pyrungLink.enable")
    trigger = parsed.trigger
    if trigger is not None:
        if isinstance(trigger, bool) or not isinstance(trigger, (int, str)):
            raise adapter.DAPAdapterError("pyrungLink.trigger must be an integer or a string")
        enable = f"{enable}:{trigger}"
    keys: dict[str, str] = {}
    for key, value in (
        ("on_delay", parsed.onDelay),
        ("off_delay", parsed.offDelay),
        ("profile", parsed.profile),
        ("physical", parsed.physical),
        ("system", parsed.system),
    ):
        text = _optional_text(adapter, value, f"pyrungLink.{key}")
        if text is not None:
            keys[key] = text

    with adapter._state_lock:
        coupling = links.build_coupling_locked(adapter, feedback, enable, keys)
        text = links.set_link_locked(adapter, coupling)
        command = links.link_command(adapter, coupling)
        capture_hook(adapter, "link", command)
    return {"text": text, "command": command}, []


def on_pyrung_unlink(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_FeedbackRequestArgs, args)
    feedback = _require_text(adapter, parsed.feedback, "pyrungUnlink.feedback")
    with adapter._state_lock:
        adapter._require_runner_locked()
        text = links.unlink_locked(adapter, feedback)
        capture_hook(adapter, "unlink", f"unlink {feedback}")
    return {"text": text}, []


def on_pyrung_restore_link(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_FeedbackRequestArgs, args)
    feedback = _require_text(adapter, parsed.feedback, "pyrungRestoreLink.feedback")
    with adapter._state_lock:
        adapter._require_runner_locked()
        text = links.restore_locked(adapter, feedback)
        capture_hook(adapter, "restore", f"restore {feedback}")
    return {"text": text}, []


def on_pyrung_set_links(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_SetLinksRequestArgs, args)
    if parsed.bench is not None and not isinstance(parsed.bench, bool):
        raise adapter.DAPAdapterError("pyrungSetLinks.bench must be a boolean")
    enabled = parsed.enabled if parsed.enabled is not None else {}
    if not isinstance(enabled, dict) or not all(
        isinstance(k, str) and k.strip() and isinstance(v, bool) for k, v in enabled.items()
    ):
        raise adapter.DAPAdapterError("pyrungSetLinks.enabled must map feedback tags to booleans")
    with adapter._state_lock:
        adapter._require_runner_locked()
        # Check every entry first, so a bad one changes nothing.
        for feedback in enabled:
            if adapter._resolve_target_locked(feedback.strip()) not in adapter._bench_links:
                raise adapter.DAPAdapterError(f"{feedback.strip()} has no bench change")
        if parsed.bench is not None:
            links.set_bench_locked(adapter, parsed.bench)
            capture_hook(adapter, "links", f"links bench {'on' if parsed.bench else 'off'}")
        for feedback, on in enabled.items():
            links.set_enabled_locked(adapter, feedback.strip(), on)
            verb = "enable" if on else "disable"
            capture_hook(adapter, "links", f"links {verb} {feedback.strip()}")
        body = _links_body_locked(adapter)
    return body, []


def on_pyrung_list_links(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    with adapter._state_lock:
        adapter._require_runner_locked()
        body = _links_body_locked(adapter)
    return body, []


def _links_body_locked(adapter: Any) -> dict[str, Any]:
    return {
        "bench": adapter._bench_links_on,
        "revision": adapter._links_revision,
        "links": links.link_rows_locked(adapter),
    }
