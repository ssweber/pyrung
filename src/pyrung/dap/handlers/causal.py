"""Causal-chain request handling for the DAP adapter.

Owns the ``pyrungCausal`` custom request.  Accepts a single query string
and dispatches to ``runner.cause`` / ``runner.effect`` / ``runner.recovers``.

Query grammar:

- ``cause:Tag``           — latest recorded transition
- ``cause:Tag@N``         — transition at scan N
- ``cause:Tag:value``     — projected ("how could this reach value")
- ``effect:Tag@N``        — recorded forward walk from scan N
- ``effect:Tag:value``    — projected what-if
- ``recovers:Tag``        — bool + witness/blockers chain

Response envelope::

    {
        "query": "<echoed query>",
        "command": "cause"|"effect"|"recovers",
        "ok":   <bool — chain found / path reachable>,
        "chain": <CausalChain.to_dict() or null>,
    }
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]

_COMMANDS = ("cause", "effect", "recovers")


@dataclass(frozen=True)
class _CausalRequestArgs:
    query: Any = None


def _parse_value(raw: str) -> Any:
    """Parse the RHS of ``:value`` into bool/int/float/str.

    Booleans win over int ("true"/"false"), then numeric, else the
    original string.  ``null``/``none`` map to Python ``None``.
    """
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


@dataclass(frozen=True)
class _ParsedQuery:
    command: str  # "cause" | "effect" | "recovers"
    tag: str
    scan: int | None
    has_value: bool
    value: Any


def _parse_query(query: str) -> _ParsedQuery:
    q = query.strip()
    if ":" not in q:
        raise ValueError(
            f"pyrungCausal.query missing ':' — expected cause:Tag, effect:Tag, or recovers:Tag (got {query!r})"
        )
    cmd, _, rest = q.partition(":")
    cmd_lower = cmd.lower().strip()
    if cmd_lower not in _COMMANDS:
        raise ValueError(
            f"pyrungCausal.query unknown command {cmd!r} — expected one of {_COMMANDS}"
        )
    rest = rest.strip()
    if not rest:
        raise ValueError(f"pyrungCausal.query missing tag name (got {query!r})")

    if "@" in rest:
        tag, _, scan_s = rest.partition("@")
        tag = tag.strip()
        try:
            scan = int(scan_s.strip())
        except ValueError as e:
            raise ValueError(
                f"pyrungCausal.query scan after '@' must be an integer (got {scan_s!r})"
            ) from e
        if not tag:
            raise ValueError(f"pyrungCausal.query missing tag name before '@' (got {query!r})")
        if cmd_lower == "recovers":
            raise ValueError("pyrungCausal recovers:Tag does not accept @scan")
        return _ParsedQuery(cmd_lower, tag, scan, has_value=False, value=None)

    if ":" in rest:
        tag, _, value_s = rest.partition(":")
        tag = tag.strip()
        if not tag:
            raise ValueError(f"pyrungCausal.query missing tag name before ':' (got {query!r})")
        if cmd_lower == "recovers":
            raise ValueError("pyrungCausal recovers:Tag does not accept :value")
        return _ParsedQuery(cmd_lower, tag, None, has_value=True, value=_parse_value(value_s))

    return _ParsedQuery(cmd_lower, rest, None, has_value=False, value=None)


def on_pyrung_causal(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    """Dispatch a causal query to the runner and serialize the chain."""
    parsed_args = adapter._parse_request_args(_CausalRequestArgs, args)

    if not isinstance(parsed_args.query, str) or not parsed_args.query.strip():
        raise adapter.DAPAdapterError("pyrungCausal.query must be a non-empty string")

    try:
        pq = _parse_query(parsed_args.query)
    except ValueError as e:
        raise adapter.DAPAdapterError(str(e)) from e

    with adapter._state_lock:
        runner = adapter._require_runner_locked()

    try:
        if pq.command == "cause":
            if pq.has_value:
                chain = runner.cause(pq.tag, to=pq.value)
            else:
                chain = runner.cause(pq.tag, scan=pq.scan)
        elif pq.command == "effect":
            if pq.has_value:
                chain = runner.effect(pq.tag, from_=pq.value)
            else:
                chain = runner.effect(pq.tag, scan=pq.scan)
        else:  # recovers
            ok = runner.recovers(pq.tag)
            # Attach the witness/blockers chain so the UI can render either case.
            from pyrung.core.tag import Tag as TagClass

            if isinstance(pq.tag, TagClass):
                resting = pq.tag.default
            else:
                resting = runner._resolve_resting_value(pq.tag)
            witness = runner.cause(pq.tag, to=resting)
            return {
                "query": parsed_args.query,
                "command": "recovers",
                "ok": bool(ok),
                "chain": witness.to_dict() if witness is not None else None,
            }, []
    except ValueError as e:
        raise adapter.DAPAdapterError(str(e)) from e

    chain_dict = chain.to_dict() if chain is not None else None
    ok = chain is not None and getattr(chain, "mode", None) != "unreachable"
    return {
        "query": parsed_args.query,
        "command": pq.command,
        "ok": bool(ok),
        "chain": chain_dict,
    }, []
