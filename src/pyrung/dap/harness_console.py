"""Harness integration for the DAP debug session.

Auto-installs the autoharness when Physical+link= annotations are detected
at launch. Console verbs: ``harness status``, ``harness install``,
``harness remove``.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.harness import Harness
from pyrung.dap.console import ConsoleResult, register


def try_auto_install(adapter: Any) -> str | None:
    """If the runner has Physical+link= couplings, install the harness.

    Returns a banner string for the debug console, or ``None`` if no
    couplings were found.
    """
    runner = adapter._runner
    if runner is None:
        return None

    harness = Harness(runner)
    harness.install()

    summary = harness.coupling_summary()
    n_bool = len(summary["bool_couplings"])
    n_profile = len(summary["profile_couplings"])
    total = n_bool + n_profile

    if total == 0:
        harness.uninstall()
        return None

    harness.on_patches_applied = _make_patch_listener(adapter)
    adapter._harness = harness

    parts = []
    if n_bool:
        parts.append(f"{n_bool} bool")
    if n_profile:
        parts.append(f"{n_profile} profile")
    return f"Harness: {total} feedback loop(s) ({', '.join(parts)}) — `harness status` for details"


def uninstall_harness(adapter: Any) -> None:
    """Uninstall the harness if present."""
    harness: Harness | None = adapter._harness
    if harness is not None:
        harness.uninstall()
        adapter._harness = None


def _make_patch_listener(adapter: Any) -> Any:
    """Build the callback that fires when the harness applies patches."""

    def on_patches(notifications: list[tuple[str, Any, str]]) -> None:
        if not notifications:
            return

        parts = [f"{tag}={value!r}" for tag, value, _prov in notifications]
        text = f"[harness] {', '.join(parts)}\n"
        adapter._enqueue_internal_event("output", {"category": "console", "output": text})

        capture = getattr(adapter, "_capture", None)
        if capture is not None and capture.recording:
            runner = adapter._runner
            timestamp = runner.current_state.timestamp if runner else 0.0
            scan_id = (
                runner.current_state.scan_id
                if runner
                else getattr(adapter, "_current_scan_id", None)
            )
            for tag, value, provenance in notifications:
                capture.append(
                    f"patch {tag} {value!r}",
                    scan_id,
                    timestamp,
                    provenance=provenance,
                )

    return on_patches


# ---------------------------------------------------------------------------
# Console verb
# ---------------------------------------------------------------------------


@register("harness", usage="harness <install|remove|status>", group="capture")
def _cmd_harness(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: harness install | harness remove | harness status")

    sub = parts[1].lower()

    if sub == "status":
        return _harness_status(adapter)
    if sub == "install":
        return _harness_install(adapter)
    if sub == "remove":
        return _harness_remove(adapter)

    raise adapter.DAPAdapterError(
        f"Unknown harness subcommand '{sub}'. Use: install, remove, status"
    )


def _harness_status(adapter: Any) -> ConsoleResult:
    harness: Harness | None = adapter._harness
    if harness is None:
        return ConsoleResult("Harness: not installed")

    summary = harness.coupling_summary()
    lines: list[str] = [f"Harness: {'active' if summary['installed'] else 'inactive'}"]

    for bc in summary["bool_couplings"]:
        tv = bc.get("trigger_value")
        en_label = f"{bc['en']}=={tv}" if tv is not None else bc["en"]
        lines.append(
            f"  bool  {en_label} -> {bc['fb']}  "
            f"(on={bc['on_delay_ms']}ms, off={bc['off_delay_ms']}ms)"
        )
    for ac in summary["profile_couplings"]:
        active = " [active]" if ac["active"] else ""
        tv = ac.get("trigger_value")
        en_label = f"{ac['en']}=={tv}" if tv is not None else ac["en"]
        lines.append(f"  analog  {en_label} -> {ac['fb']}  profile={ac['profile']}{active}")

    pending = summary["pending_patches"]
    if pending:
        lines.append(f"  {pending} pending patch(es)")

    return ConsoleResult("\n".join(lines))


def _harness_install(adapter: Any) -> ConsoleResult:
    if adapter._harness is not None:
        return ConsoleResult("Harness already installed — use `harness remove` first")

    adapter._require_runner_locked()
    banner = try_auto_install(adapter)
    if banner is None:
        return ConsoleResult("No Physical+link= couplings found — nothing to install")
    return ConsoleResult(banner)


def _harness_remove(adapter: Any) -> ConsoleResult:
    if adapter._harness is None:
        return ConsoleResult("Harness not installed")
    uninstall_harness(adapter)
    return ConsoleResult("Harness removed")
