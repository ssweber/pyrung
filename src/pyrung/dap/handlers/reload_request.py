"""The pyrungReload custom request, a reload that applies the caller's plan.

A plain ``reload`` moves each force and bench change by its tag name.  A client
that knows the new program's tag map (ClickNick) sends a plan instead.  It says
where each force, bench change and declared link goes, keyed by the old tag name
(see ``pyrung.dap.reload_console.ReloadPlan``).  The response body holds the console
reload's ``result`` text and ``carried``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pyrung.dap.console import record_command
from pyrung.dap.handlers.simulation import pause_executor
from pyrung.dap.reload_console import ReloadPlan, _reload_locked

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]


@dataclass(frozen=True)
class _ReloadRequestArgs:
    forces: Any = None
    links: Any = None
    declared: Any = None


def _plan_table(adapter: Any, raw: Any, field: str) -> dict[str, Any]:
    """The plan's *field*, keyed by tag name.  A bad entry is reported, not refused."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise adapter.DAPAdapterError(f"pyrungReload.{field} must be an object")
    for key in raw:
        if not isinstance(key, str) or not key.strip():
            raise adapter.DAPAdapterError(f"pyrungReload.{field} keys must be tag names")
    return {key.strip(): value for key, value in raw.items()}


def on_pyrung_reload(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_ReloadRequestArgs, args)
    plan = ReloadPlan(
        forces=_plan_table(adapter, parsed.forces, "forces"),
        links=_plan_table(adapter, parsed.links, "links"),
        declared=_plan_table(adapter, parsed.declared, "declared"),
    )
    with adapter._state_lock:
        adapter._require_runner_locked()
    # As with the console's reload, a running continue pauses first, so the last command wins.
    pause_executor(adapter)
    with adapter._state_lock:
        summary, events, carried = _reload_locked(adapter, plan)
        # The history logs this request as a console reload.
        record_command(adapter, "reload", "reload", provenance="console")
    return {"result": summary, "carried": carried}, events
