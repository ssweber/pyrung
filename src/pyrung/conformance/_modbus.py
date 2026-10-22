"""Conformance runner over Modbus TCP.

The harness sees the implementation only through DS registers: it reads the
build-hash header, zeroes the slots, writes each slot's ``Cmd``, waits for
every ``Fired``, and reads the slots back twice.  The same client code talks
to pyrung's soft PLC (:class:`SimServer`, the default) or to a real CLICK
running the exported program; only the endpoint differs.

Pacing is wall-clock.  See ``tests/conformance/AGENTS.md`` for the protocol.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any

from pyclickplc.server import PlcValue

from pyrung.conformance._build import ConformanceProgram, build_program
from pyrung.conformance._case import Case, CaseResult, expectation_met
from pyrung.conformance._slot import (
    COUNT_REGISTER,
    HASH_REGISTER,
    SLOT_BASE,
    SLOT_FIELDS,
    SLOT_STRIDE,
)

#: Default Modbus TCP port of a CLICK.
DEFAULT_PORT = 502
#: Registers per Modbus read/write request (the protocol allows 123..125).
_CHUNK = 100
#: Wall-clock seconds per scan the settle time assumes (pyrung's nominal dt).
NOMINAL_SCAN_S = 0.010
#: Seconds between polls while waiting on the header, the zeroing, or ``Fired``.
POLL_S = 0.02
#: Seconds between the two final reads; both must meet the expectation.
CONFIRM_GAP_S = 0.1
#: Seconds the sim's scan and server threads get to start or stop.
_THREAD_TIMEOUT_S = 5.0


class ConformanceError(RuntimeError):
    """The harness could not run the protocol against the endpoint."""


class BuildMismatchError(ConformanceError):
    """The program on the endpoint is not the program built from these cases."""


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int = DEFAULT_PORT

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


def parse_endpoint(text: str) -> Endpoint:
    """Parse ``host`` or ``host:port`` (port defaults to 502)."""
    raw = text.strip()
    if not raw:
        raise ValueError("Endpoint must be 'host' or 'host:port'.")
    host, sep, port_text = raw.rpartition(":")
    if not sep:
        return Endpoint(raw)
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError(f"Endpoint port must be an integer: {text!r}") from None
    if not host or not 1 <= port <= 65535:
        raise ValueError(f"Endpoint must be 'host' or 'host:port' with port 1..65535: {text!r}")
    return Endpoint(host, port)


# ---------------------------------------------------------------------------
# Local sim: ClickDataProvider behind ClickServer, scanning in real time
# ---------------------------------------------------------------------------


class _LockedProvider:
    """Serializes Modbus reads and writes against the scan thread."""

    def __init__(self, provider: Any, lock: threading.Lock) -> None:
        self._provider = provider
        self._lock = lock

    def read(self, address: str) -> PlcValue:
        with self._lock:
            return self._provider.read(address)

    def write(self, address: str, value: PlcValue) -> None:
        with self._lock:
            self._provider.write(address, value)


class SimServer:
    """pyrung's soft PLC running a conformance program, served over Modbus TCP.

    The PLC free-runs in ``realtime`` mode on its own thread, one scan every
    *scan_period* seconds of wall clock, so timers see wall-clock time as on a
    CLICK.  Modbus writes become patches applied at the start of the next
    scan.  Use as a context manager; :attr:`endpoint` is where to connect.
    """

    def __init__(
        self,
        build: ConformanceProgram,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        scan_period: float = NOMINAL_SCAN_S,
    ) -> None:
        self.build = build
        self._host = host
        self._port = port
        self._scan_period = scan_period
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._scan_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._server: Any = None
        self._plc: Any = None
        self.endpoint: Endpoint | None = None
        #: First exception raised by a scan, if any; the scan thread stops on it.
        self.error: BaseException | None = None

    def __enter__(self) -> SimServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> Endpoint:
        from pyclickplc.server import ClickServer

        from pyrung.click import ClickDataProvider
        from pyrung.core import PLC

        # Bounded history: the run is short, but a stuck harness must not grow memory.
        self._plc = PLC(self.build.program, realtime=True, history=2000)
        provider = _LockedProvider(
            ClickDataProvider(self._plc, tag_map=self.build.tag_map), self._lock
        )
        port = self._port or _free_port(self._host)

        loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(
            target=loop.run_forever, daemon=True, name="pyrung-conformance-modbus"
        )
        loop_thread.start()
        self._loop = loop
        self._loop_thread = loop_thread
        server = ClickServer(provider, host=self._host, port=port)
        try:
            self._call(server.start())
        except Exception as exc:
            self._shutdown_loop()
            raise ConformanceError(f"Cannot serve Modbus on {self._host}:{port}: {exc}") from exc
        self._server = server

        self._stop.clear()
        scan_thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="pyrung-conformance-scan"
        )
        scan_thread.start()
        self._scan_thread = scan_thread
        self.endpoint = Endpoint(self._host, port)
        return self.endpoint

    def stop(self) -> None:
        self._stop.set()
        if self._scan_thread is not None:
            self._scan_thread.join(timeout=_THREAD_TIMEOUT_S)
            self._scan_thread = None
        server = self._server
        self._server = None
        if server is not None and self._loop is not None:
            try:
                self._call(server.stop())
            except Exception:
                pass
        self._shutdown_loop()

    def raise_if_failed(self) -> None:
        if self.error is not None:
            raise ConformanceError(f"Sim scan failed: {self.error!r}") from self.error

    def _scan_loop(self) -> None:
        period = self._scan_period
        next_tick = time.perf_counter()
        while not self._stop.is_set():
            try:
                with self._lock:
                    self._plc.step()
            except BaseException as exc:  # surfaced through raise_if_failed()
                self.error = exc
                return
            next_tick += period
            delay = next_tick - time.perf_counter()
            if delay > 0:
                self._stop.wait(delay)
            else:
                # Running late: do not burst scans to catch up, as a PLC would not.
                next_tick = time.perf_counter()

    def _call(self, coro: Coroutine[Any, Any, Any]) -> Any:
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=_THREAD_TIMEOUT_S)

    def _shutdown_loop(self) -> None:
        loop = self._loop
        thread = self._loop_thread
        self._loop = None
        self._loop_thread = None
        if loop is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=_THREAD_TIMEOUT_S)
        if not loop.is_running():
            loop.close()


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# Harness: the 8-step protocol over the wire
# ---------------------------------------------------------------------------


def run_modbus(
    cases: Sequence[Case] | ConformanceProgram,
    endpoint: Endpoint | str | None = None,
    *,
    timeout: float = 10.0,
) -> list[CaseResult]:
    """Run *cases* over Modbus TCP and return one result per case.

    Args:
        cases: The cases, or a program already built with :func:`build_program`.
        endpoint: ``host[:port]`` of a CLICK running the exported program.
            ``None`` starts pyrung's soft PLC on a free localhost port.
        timeout: Seconds to wait for each phase (header, zeroing, ``Fired``).

    Raises:
        BuildMismatchError: ``DS1``/``DS2`` do not hold this build's hash and
            slot count.
        ConformanceError: The endpoint is unreachable or the phases time out
            before any case can be judged.
    """
    build = cases if isinstance(cases, ConformanceProgram) else build_program(cases)
    if isinstance(endpoint, str):
        endpoint = parse_endpoint(endpoint)

    if endpoint is None:
        with SimServer(build) as sim:
            assert sim.endpoint is not None
            try:
                results = asyncio.run(_drive(build, sim.endpoint, timeout))
            finally:
                sim.raise_if_failed()
    else:
        results = asyncio.run(_drive(build, endpoint, timeout))

    for c in build.skipped:
        results.append(
            CaseResult(
                case=c,
                passed=False,
                actual={},
                slot_index=0,
                skipped=f"in-process only: {c.in_process_only}",
            )
        )
    return results


async def _drive(build: ConformanceProgram, endpoint: Endpoint, timeout: float) -> list[CaseResult]:
    from pyclickplc.client import ClickClient

    n = build.slot_count
    width = n * SLOT_STRIDE
    fired_offsets = [_offset(i, "Fired") for i in range(1, n + 1)]

    client = ClickClient(endpoint.host, endpoint.port, timeout=max(1, int(timeout)))
    async with client:
        if not client._client.connected:
            raise ConformanceError(f"Cannot connect to Modbus endpoint {endpoint}")

        # 0. Header: is the right program running?
        header = await _poll(
            lambda: _read_ds(client, HASH_REGISTER, COUNT_REGISTER - HASH_REGISTER + 1),
            lambda regs: regs[0] != 0,
            timeout,
            f"DS{HASH_REGISTER} stayed 0 on {endpoint}: the conformance program is not running",
        )
        found = (header[0], header[1])
        wanted = (build.build_hash, n)
        if found != wanted:
            raise BuildMismatchError(
                f"{endpoint} runs build {found[0]} with {found[1]} slots; "
                f"these cases are build {wanted[0]} with {wanted[1]} slots. "
                "Export the program again and load it."
            )

        # 1-3. Zero every slot (no case stages inputs yet), then read back.
        await _write_ds(client, SLOT_BASE, [0] * width)
        await _poll(
            lambda: _read_ds(client, SLOT_BASE, width),
            lambda regs: not any(regs),
            timeout,
            f"Slots on {endpoint} did not read back as zero: writes were dropped",
        )

        # 4. Cmd = 1, one single-register write per slot so a slot's other
        # registers are never rewritten after its case has fired.
        for i in range(1, n + 1):
            await client.ds.write(SLOT_BASE + _offset(i, "Cmd"), 1)

        # 5. Wait for every Fired (a case that never fires is judged, not raised).
        deadline = time.monotonic() + timeout
        while True:
            regs = await _read_ds(client, SLOT_BASE, width)
            if all(regs[o] != 0 for o in fired_offsets) or time.monotonic() >= deadline:
                break
            await asyncio.sleep(POLL_S)
        settle = max(c.scans for c in build.cases) * NOMINAL_SCAN_S
        await asyncio.sleep(settle)

        # 6. Read every slot twice; both reads must meet the expectation.
        first = await _read_ds(client, SLOT_BASE, width)
        await asyncio.sleep(max(CONFIRM_GAP_S, settle))
        second = await _read_ds(client, SLOT_BASE, width)

    # 7. Compare.
    results: list[CaseResult] = []
    for i, c in enumerate(build.cases, start=1):
        actual_first = {f: first[_offset(i, f)] for f in c.expect}
        actual = {f: second[_offset(i, f)] for f in c.expect}
        fired = second[_offset(i, "Fired")] != 0
        met_first = expectation_met(c.expect, actual_first)
        met_second = expectation_met(c.expect, actual)
        note = ""
        if met_first != met_second:
            note = f"unstable: first read {actual_first}, second read {actual}"
        results.append(
            CaseResult(
                case=c,
                passed=fired and met_first and met_second,
                actual=actual,
                slot_index=i,
                fired=fired,
                note=note,
            )
        )
    return results


def _offset(index: int, field: str) -> int:
    """Offset of *field* in slot *index* from ``SLOT_BASE``."""
    return (index - 1) * SLOT_STRIDE + SLOT_FIELDS.index(field)


async def _poll(
    read: Callable[[], Coroutine[Any, Any, list[int]]],
    done: Callable[[list[int]], bool],
    timeout: float,
    message: str,
) -> list[int]:
    deadline = time.monotonic() + timeout
    while True:
        regs = await read()
        if done(regs):
            return regs
        if time.monotonic() >= deadline:
            raise ConformanceError(message)
        await asyncio.sleep(POLL_S)


async def _read_ds(client: Any, start: int, count: int) -> list[int]:
    values: list[int] = []
    for chunk_start in range(start, start + count, _CHUNK):
        chunk_end = min(chunk_start + _CHUNK, start + count) - 1
        if chunk_end == chunk_start:
            values.append(int(await client.ds[chunk_start]))
            continue
        response = await client.ds.read(chunk_start, chunk_end)
        values.extend(int(v) for v in response.values())
    return values


async def _write_ds(client: Any, start: int, values: list[int]) -> None:
    for i in range(0, len(values), _CHUNK):
        chunk = values[i : i + _CHUNK]
        if len(chunk) == 1:
            await client.ds.write(start + i, chunk[0])
        else:
            await client.ds.write(start + i, chunk)
