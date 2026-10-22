"""Conformance runner over Modbus TCP.

The harness sees the implementation only through DS registers: it reads the
program's layout hash and each selected group's hash, zeroes the slots of the
groups that match, writes each slot's ``Cmd``, waits for every ``Fired``, and
reads the slots back twice.  A group whose hash does not match is stale: its
cases are reported without touching its registers.  The same client code talks
to pyrung's soft PLC (:class:`SimServer`, the default) or to a real CLICK
running the exported program; only the endpoint differs.

Pacing is wall-clock.  See ``tests/conformance/AGENTS.md`` for the protocol.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from pyclickplc.server import PlcValue

from pyrung.conformance._build import ConformanceProgram, GroupBuild, build_program
from pyrung.conformance._case import CaseResult, Group, expectation_met
from pyrung.conformance._slot import PROGRAM_HASH_REGISTER

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
    """The main program on the endpoint does not have this build's group layout."""


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

    @property
    def plc(self) -> Any:
        """The running ``PLC``.  It scans on its own thread; read it for checks only."""
        return self._plc

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
    program: ConformanceProgram | Group | Sequence[Group],
    endpoint: Endpoint | str | None = None,
    *,
    groups: Iterable[str] | None = None,
    timeout: float = 10.0,
) -> list[CaseResult]:
    """Run cases over Modbus TCP and return one result per case, in group order.

    Args:
        program: A program built with :func:`build_program`, or groups to build
            one from (packed layout, fine for the local sim).
        endpoint: ``host[:port]`` of a CLICK running the exported program.
            ``None`` starts pyrung's soft PLC on a free localhost port.
        groups: Names of the groups to run; ``None`` runs every group.  The
            other groups' registers are never touched.
        timeout: Seconds to wait for each phase (header, zeroing, ``Fired``).

    A selected group whose subroutine hash does not match is stale: its cases
    come back with ``stale=True`` without running, and the other groups still run.

    Raises:
        BuildMismatchError: ``DS1`` does not hold this build's layout hash.
        ConformanceError: The endpoint is unreachable or the phases time out
            before any case can be judged.
        ValueError: *groups* names a group the program does not have.
    """
    build = program if isinstance(program, ConformanceProgram) else build_program(program)
    selected = _select(build, groups)
    wire = [g for g in build.groups if g.name in selected]
    if isinstance(endpoint, str):
        endpoint = parse_endpoint(endpoint)

    by_group: dict[str, list[CaseResult]] = {}
    if wire:
        if endpoint is None:
            with SimServer(build) as sim:
                assert sim.endpoint is not None
                try:
                    by_group = asyncio.run(_drive(build, wire, sim.endpoint, timeout))
                finally:
                    sim.raise_if_failed()
        else:
            by_group = asyncio.run(_drive(build, wire, endpoint, timeout))

    results: list[CaseResult] = []
    for name in build.names:
        if name not in selected:
            continue
        results.extend(by_group.get(name, []))
        for group_name, c in build.skipped:
            if group_name == name:
                results.append(
                    CaseResult(
                        case=c,
                        passed=False,
                        actual={},
                        slot_index=0,
                        skipped=f"in-process only: {c.in_process_only}",
                        group=name,
                    )
                )
    return results


def _select(build: ConformanceProgram, groups: Iterable[str] | None) -> set[str]:
    if groups is None:
        return set(build.names)
    wanted = {name.strip() for name in groups if name.strip()}
    unknown = sorted(wanted - set(build.names))
    if unknown:
        raise ValueError(
            f"Unknown conformance group(s) {', '.join(unknown)}; known: {', '.join(build.names)}"
        )
    return wanted


async def _drive(
    build: ConformanceProgram, wire: list[GroupBuild], endpoint: Endpoint, timeout: float
) -> dict[str, list[CaseResult]]:
    from pyclickplc.client import ClickClient

    results: dict[str, list[CaseResult]] = {}
    client = ClickClient(endpoint.host, endpoint.port, timeout=max(1, int(timeout)))
    async with client:
        if not client._client.connected:
            raise ConformanceError(f"Cannot connect to Modbus endpoint {endpoint}")

        # 0. Header: is a program with this group layout running?
        (found,) = await _poll(
            lambda: _read_ds(client, PROGRAM_HASH_REGISTER, 1),
            lambda regs: regs[0] != 0,
            timeout,
            f"DS{PROGRAM_HASH_REGISTER} stayed 0 on {endpoint}: "
            "the conformance program is not running",
        )
        if found != build.program_hash:
            raise BuildMismatchError(
                f"{endpoint} runs program layout {found}; these groups are layout "
                f"{build.program_hash} ({', '.join(g.subroutine for g in build.groups)}). "
                "Export the whole project again and load it."
            )

        # Each selected group's own hash: a mismatch marks the group stale.
        live: list[GroupBuild] = []
        for g in wire:
            (group_hash,) = await _read_ds(client, g.hash_register, 1)
            if group_hash == g.group_hash:
                live.append(g)
                continue
            note = (
                f"{g.subroutine} is stale: repush (DS{g.hash_register} reads {group_hash}, "
                f"this build writes {g.group_hash})"
            )
            results[g.name] = [
                CaseResult(
                    case=c,
                    passed=False,
                    actual={},
                    slot_index=i,
                    note=note,
                    group=g.name,
                    stale=True,
                )
                for i, c in g.cases
            ]
        if not live:
            return results

        # 1-3. Zero the live groups' slots (no case stages inputs yet), then read back.
        for g in live:
            start, width = g.span
            await _write_ds(client, start, [0] * width)
        await _poll(
            lambda: _read_spans(client, live),
            lambda regs: not any(regs),
            timeout,
            f"Slots on {endpoint} did not read back as zero: writes were dropped",
        )

        # 4. Cmd = 1, one single-register write per slot so a slot's other
        # registers are never rewritten after its case has fired.
        for g in live:
            for i, _ in g.cases:
                await client.ds.write(g.register(i, "Cmd"), 1)

        # 5. Wait for every Fired (a case that never fires is judged, not raised).
        fired_registers = [g.register(i, "Fired") for g in live for i, _ in g.cases]
        deadline = time.monotonic() + timeout
        while True:
            regs = _by_register(live, await _read_spans(client, live))
            if all(regs[r] != 0 for r in fired_registers) or time.monotonic() >= deadline:
                break
            await asyncio.sleep(POLL_S)
        settle = max(c.scans for g in live for _, c in g.cases) * NOMINAL_SCAN_S
        await asyncio.sleep(settle)

        # 6. Read every slot twice; both reads must meet the expectation.
        first = _by_register(live, await _read_spans(client, live))
        await asyncio.sleep(max(CONFIRM_GAP_S, settle))
        second = _by_register(live, await _read_spans(client, live))

    # 7. Compare.
    for g in live:
        group_results: list[CaseResult] = []
        for i, c in g.cases:
            actual_first = {f: first[g.register(i, f)] for f in c.expect}
            actual = {f: second[g.register(i, f)] for f in c.expect}
            fired = second[g.register(i, "Fired")] != 0
            met_first = expectation_met(c.expect, actual_first)
            met_second = expectation_met(c.expect, actual)
            note = ""
            if met_first != met_second:
                note = f"unstable: first read {actual_first}, second read {actual}"
            group_results.append(
                CaseResult(
                    case=c,
                    passed=fired and met_first and met_second,
                    actual=actual,
                    slot_index=i,
                    fired=fired,
                    note=note,
                    group=g.name,
                )
            )
        results[g.name] = group_results
    return results


async def _read_spans(client: Any, groups: Sequence[GroupBuild]) -> list[int]:
    """Read every group's used slots, concatenated in group order."""
    values: list[int] = []
    for g in groups:
        start, width = g.span
        values.extend(await _read_ds(client, start, width))
    return values


def _by_register(groups: Sequence[GroupBuild], values: list[int]) -> dict[int, int]:
    """Key a :func:`_read_spans` result by DS register."""
    out: dict[int, int] = {}
    pos = 0
    for g in groups:
        start, width = g.span
        for offset in range(width):
            out[start + offset] = values[pos + offset]
        pos += width
    return out


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
