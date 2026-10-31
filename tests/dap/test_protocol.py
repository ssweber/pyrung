"""Tests for DAP framing helpers."""

from __future__ import annotations

import io

import pytest

from pyrung.dap.protocol import read_message, write_message


def test_read_write_message_roundtrip():
    stream = io.BytesIO()
    message = {"seq": 1, "type": "request", "command": "initialize", "arguments": {"x": 1}}

    write_message(stream, message)

    stream.seek(0)
    parsed = read_message(stream)
    assert parsed == message


def test_read_write_message_multiple_frames():
    stream = io.BytesIO()
    first = {"seq": 1, "type": "event", "event": "initialized"}
    second = {"seq": 2, "type": "response", "success": True, "command": "launch"}

    write_message(stream, first)
    write_message(stream, second)

    stream.seek(0)
    assert read_message(stream) == first
    assert read_message(stream) == second
    assert read_message(stream) is None


def test_write_message_spells_inf_and_nan_as_strings():
    """A REAL can hold Inf or NaN, which JSON cannot; the frame stays valid JSON."""
    import json

    stream = io.BytesIO()
    message = {
        "seq": 1,
        "body": {"values": [1.5, float("inf"), (float("-inf"),)], "x": float("nan")},
    }

    write_message(stream, message)

    raw = stream.getvalue().partition(b"\r\n\r\n")[2].decode("utf-8")
    json.loads(raw, parse_constant=lambda name: pytest.fail(f"non-JSON constant {name}"))
    stream.seek(0)
    assert read_message(stream) == {
        "seq": 1,
        "body": {"values": [1.5, "inf", ["-inf"]], "x": "nan"},
    }
