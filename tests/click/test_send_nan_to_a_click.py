"""``send()`` to a CLICK DF sends an Inf or a NaN as a CLICK holds it.

A CLICK holds +Inf and -Inf as their own bits and every NaN as 0xFFFF:0xFFFF.
A local soft PLC stands in for the peer.  Its provider keeps each value it is given, so the
test reads the bits that reached it.
"""

from __future__ import annotations

import asyncio
import socket
import struct
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from pyclickplc.server import ClickServer, MemoryDataProvider

from pyrung.core.instruction.send_receive.backends import _run_click_send_request


class _KeepEverything(MemoryDataProvider):
    """Stores each value as given, so the test reads the bits that reached it."""

    def __init__(self) -> None:
        super().__init__()
        self.kept: dict[str, Any] = {}

    def write(self, address: str, value: Any) -> None:
        self.kept[address] = value


@pytest.fixture
def peer() -> Iterator[tuple[int, _KeepEverything]]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    provider = _KeepEverything()
    ready, stop = threading.Event(), threading.Event()

    async def serve() -> None:
        async with ClickServer(provider, host="127.0.0.1", port=port):
            ready.set()
            while not stop.is_set():
                await asyncio.sleep(0.02)

    thread = threading.Thread(target=lambda: asyncio.run(serve()), daemon=True)
    thread.start()
    assert ready.wait(10)
    yield port, provider
    stop.set()
    thread.join(timeout=5)


def _bits(value: float) -> int:
    return struct.unpack("<I", struct.pack("<f", value))[0]


def _float(bits: int) -> float:
    return struct.unpack("<f", struct.pack("<I", bits))[0]


@pytest.mark.parametrize(
    ("bits", "sent"),
    [
        (0x7FC00000, 0xFFFFFFFF),
        (0xFFC00000, 0xFFFFFFFF),
        (0x7F800001, 0xFFFFFFFF),
        (0x7F800000, 0x7F800000),
        (0xFF800000, 0xFF800000),
    ],
)
def test_an_inf_or_a_nan_reaches_the_peer_as_a_click_holds_it(peer, bits, sent):
    port, provider = peer
    values = (2.5, _float(bits), -7.25)
    result = _run_click_send_request("127.0.0.1", port, 1, "DF", (1, 2, 3), values)
    assert result.ok
    assert _bits(provider.kept["DF2"]) == sent
    assert (provider.kept["DF1"], provider.kept["DF3"]) == (2.5, -7.25)


def test_a_value_past_float32_is_refused_beside_a_nan(peer):
    port, provider = peer
    values = (float("nan"), 3.5e38)
    result = _run_click_send_request("127.0.0.1", port, 1, "DF", (1, 2), values)
    assert not result.ok
    assert provider.kept == {}
