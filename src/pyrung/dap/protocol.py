"""Minimal Debug Adapter Protocol framing helpers."""

from __future__ import annotations

import json
import math
from collections.abc import MutableMapping
from typing import Any, BinaryIO


class MessageSequencer:
    """Monotonic sequence number generator for DAP envelopes."""

    def __init__(self, start: int = 1) -> None:
        self._next = start

    def next(self) -> int:
        seq = self._next
        self._next += 1
        return seq


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    """Read one DAP message from a binary stream."""
    headers: dict[str, str] = {}

    while True:
        line = stream.readline()
        if line == b"":
            return None
        if line in (b"\r\n", b"\n"):
            break
        if b":" not in line:
            continue
        key, value = line.split(b":", 1)
        headers[key.decode("ascii").strip().lower()] = value.decode("ascii").strip()

    raw_length = headers.get("content-length")
    if raw_length is None:
        raise ValueError("Missing Content-Length header")

    length = int(raw_length)
    payload = stream.read(length)
    if len(payload) != length:
        return None
    return json.loads(payload.decode("utf-8"))


def _finite_json(value: Any) -> Any:
    """*value* with every Inf or NaN float as its repr (``"inf"``, ``"nan"``).

    A REAL can hold +/-Inf or NaN (a Math overflow stores Inf); JSON has no
    such number, and ``json.dumps`` would write ``Infinity``, which a DAP
    client's JSON parser refuses.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, dict):
        return {key: _finite_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_json(item) for item in value]
    return value


def write_message(stream: BinaryIO, message: MutableMapping[str, Any]) -> None:
    """Write one DAP message to a binary stream."""
    try:
        text = json.dumps(message, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except ValueError:  # an Inf or NaN float somewhere in the message
        text = json.dumps(_finite_json(message), separators=(",", ":"), ensure_ascii=False)
    payload = text.encode("utf-8")
    header = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
    stream.write(header)
    stream.write(payload)
    stream.flush()


def make_response(
    *,
    seq: int,
    request: dict[str, Any],
    success: bool,
    body: dict[str, Any] | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """Build a DAP response envelope."""
    response: dict[str, Any] = {
        "seq": seq,
        "type": "response",
        "request_seq": request.get("seq", 0),
        "success": success,
        "command": request.get("command", ""),
    }
    if body is not None:
        response["body"] = body
    if message is not None:
        response["message"] = message
    return response


def make_event(*, seq: int, event: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a DAP event envelope."""
    message: dict[str, Any] = {"seq": seq, "type": "event", "event": event}
    if body is not None:
        message["body"] = body
    return message
