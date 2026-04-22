"""Review console verbs for the invariant miner."""

from __future__ import annotations

from typing import Any

from pyrung.dap.console import ConsoleResult, register


@register("candidates", usage="candidates [list]", group="review")
def _cmd_candidates(adapter: Any, expression: str) -> ConsoleResult:
    candidates = getattr(adapter, "_miner_candidates", [])
    if not candidates:
        return ConsoleResult("No pending candidates.")
    lines: list[str] = []
    for c in candidates:
        lines.append(f"  [?] {c.id}  {c.description}")
    header = f"{len(candidates)} candidate(s):"
    return ConsoleResult(f"{header}\n" + "\n".join(lines))


@register("accept", usage="accept <id>", group="review")
def _cmd_accept(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: accept <id>")
    target_id = parts[1]
    candidates: list[Any] = getattr(adapter, "_miner_candidates", [])
    matched = [c for c in candidates if c.id == target_id]
    if not matched:
        raise adapter.DAPAdapterError(f"No candidate with id '{target_id}'")
    candidate = matched[0]
    accepted: list[Any] = getattr(adapter, "_miner_accepted", [])
    accepted.append(candidate)
    adapter._miner_candidates = [c for c in candidates if c.id != target_id]
    return ConsoleResult(f"Accepted: {candidate.description}")


@register("deny", usage="deny <id>", group="review")
def _cmd_deny(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: deny <id>")
    target_id = parts[1]
    candidates: list[Any] = getattr(adapter, "_miner_candidates", [])
    matched = [c for c in candidates if c.id == target_id]
    if not matched:
        raise adapter.DAPAdapterError(f"No candidate with id '{target_id}'")
    adapter._miner_candidates = [c for c in candidates if c.id != target_id]
    return ConsoleResult(f"Denied: {matched[0].description}")


@register("suppress", usage="suppress <id>", group="review")
def _cmd_suppress(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: suppress <id>")
    target_id = parts[1]
    candidates: list[Any] = getattr(adapter, "_miner_candidates", [])
    matched = [c for c in candidates if c.id == target_id]
    if not matched:
        raise adapter.DAPAdapterError(f"No candidate with id '{target_id}'")
    candidate = matched[0]
    suppressed: set[str] = getattr(adapter, "_miner_suppressed", set())
    suppressed.add(candidate.formula)
    adapter._miner_candidates = [c for c in candidates if c.id != target_id]
    return ConsoleResult(f"Suppressed: {candidate.description}")
