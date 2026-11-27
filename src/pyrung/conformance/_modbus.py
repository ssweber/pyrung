"""Conformance runner over Modbus TCP.

The harness sees the implementation only through DS registers: it reads the
program's layout hash and each selected group's hash, zeroes the slots of the
groups that match, waits for each case's reset to raise ``Ready``, writes each
ready slot's ``Cmd``, waits for every ``Fired``, and reads the slots back
twice.  A group whose hash does not match is stale: its cases are reported
without touching its registers.  The same client code talks
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
from dataclasses import dataclass, replace
from typing import Any

from pyclickplc.server import PlcValue

from pyrung.conformance._build import ConformanceProgram, GroupBuild, build_program
from pyrung.conformance._case import CaseResult, Group, expectation_met, judge, timed_out_step
from pyrung.conformance._slot import LONGEST_SCAN_REGISTER, PROGRAM_HASH_REGISTER

#: Default Modbus TCP port of a CLICK.
DEFAULT_PORT = 502
#: Registers per Modbus read/write request (the protocol allows 123..125).
_CHUNK = 100
#: Wall-clock seconds per scan the settle time assumes (pyrung's nominal dt).
NOMINAL_SCAN_S = 0.010
#: Most seconds of PLC time one sim scan advances (4 nominal scans).  A longer
#: wall-clock gap between scans is this machine stalling (other processes, the
#: GC), which a CLICK scanning in milliseconds never sees, so the sim's PLC does
#: not see it either.
MAX_SCAN_DT_S = 0.040
#: Seconds between polls while waiting on the header, the zeroing, or ``Fired``.
POLL_S = 0.02
#: Seconds between the two final reads; both must meet the expectation.
CONFIRM_GAP_S = 0.1
#: Seconds the sim's scan and server threads get to start or stop.
_THREAD_TIMEOUT_S = 5.0
#: CLICK's ``_PLC_Mode`` bit: ON in RUN.
_MODE_RUN_BIT = 11
#: SD9, CLICK's scan counter.
_SCAN_COUNTER = 9
#: SD9 counts 0..32767 and then starts again.
_SCAN_COUNTER_WRAP = 32768
#: Scans to wait after the harness writes a reset, before it trusts ``Ready``.
_SCANS_AFTER_WRITE = 2
#: Seconds between SD9 reads while waiting for it to wrap.
_WRAP_POLL_S = 0.2
#: Seconds of SD9 reads that size the wait for it to wrap.
_WRAP_RATE_S = 1.0


class ConformanceError(RuntimeError):
    """The harness could not run the protocol against the endpoint."""


class BuildMismatchError(ConformanceError):
    """The main program on the endpoint does not have this build's group layout."""


class ScanTimeError(ConformanceError):
    """A scan took longer than the program's cases assume, so the run is void."""


def scan_limit_note(part: str | None, limit: int | None, longest_ms: int) -> str | None:
    """Return the line that says *longest_ms* passed the scan limit, or ``None`` if it did not."""
    if limit is None or longest_ms <= limit:
        return None
    name = f"part {part}" if part else "this program"
    return f"longest scan {longest_ms} ms passed the {limit} ms limit of {name}"


def check_scan_time(build: ConformanceProgram, longest_ms: int) -> None:
    """Raise :class:`ScanTimeError` when *longest_ms* is past *build*'s scan limit.

    The cases of a part assume every scan is at most its limit, so a longer one
    voids a run on a real PLC.  No Result is judged.
    """
    if scan_limit_note(build.part, build.scan_limit_ms, longest_ms) is None:
        return
    name = f"part {build.part}" if build.part else "this program"
    raise ScanTimeError(
        f"Run void: {name} assumes scans of at most {build.scan_limit_ms} ms, but the longest "
        f"scan while its cases ran took {longest_ms} ms. No Result was judged. Find what made "
        "the scan long. A For loop that runs more times than its case expects is one cause. "
        "If the part needs longer scans, raise its limit in tests/conformance/suite.py."
    )


def note_failures(results: list[CaseResult], note: str) -> list[CaseResult]:
    """Return *results* with *note* added to every failed case.

    On pyrung's own targets a slow host can make a long scan. That is no reason to void the
    run, so the note tells you why a case may have failed.
    """
    return [
        r
        if r.passed or r.skipped is not None or r.stale
        else replace(r, note=f"{r.note}; {note}" if r.note else note)
        for r in results
    ]


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
    *scan_period* seconds of wall clock.  Timers see wall-clock time as on a
    CLICK, except that one scan advances them by at most *max_scan_dt* seconds.
    If this machine stalls (the scan thread waits longer than that for the CPU),
    the stall does not become a scan longer than any CLICK's, which would step
    over a case's timing windows.  ``None`` lets the whole gap through.

    Modbus writes become patches that the next scan applies at its start.  The
    Modbus server refuses a write to SC50 (the STOP command) with exception code
    2, so only a case's rung can stop the PLC.  A PLC in STOP does not scan.  The
    sim keeps serving Modbus and stays in STOP.  Use it as a context manager.
    :attr:`endpoint` is where to connect.
    """

    #: The name a case's ``pending_on`` gives this target.
    target = "pyrung"

    def __init__(
        self,
        build: ConformanceProgram,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        scan_period: float = NOMINAL_SCAN_S,
        max_scan_dt: float | None = MAX_SCAN_DT_S,
    ) -> None:
        self.build = build
        self._host = host
        self._port = port
        self._scan_period = scan_period
        self._max_scan_dt = max_scan_dt
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

        from pyrung.click import ClickBlocks, ClickDataProvider
        from pyrung.core import PLC

        # Bounded history: the run is short, but a stuck harness must not grow memory.
        self._plc = PLC(self.build.program, realtime=True, history=2000)
        # The TagMap maps no raw memory (DF101, C1999).  The banks serve it from the PLC, not
        # from a separate memory the server keeps.
        provider = _LockedProvider(
            ClickDataProvider(self._plc, tag_map=self.build.tag_map, blocks=ClickBlocks()),
            self._lock,
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
        cap = self._max_scan_dt
        last_scan = next_tick = time.perf_counter()
        while not self._stop.is_set():
            try:
                with self._lock:
                    # step() would put a stopped PLC back in RUN; a CLICK in
                    # STOP stays there until someone switches it.
                    if self._plc.mode != "stop":
                        if cap is not None:
                            # The scan's dt, as realtime mode measures it (the
                            # wall clock since the last scan), but capped.
                            now = time.perf_counter()
                            self._plc._dt_override_for_next_scan = min(now - last_scan, cap)
                            last_scan = now
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
    sim: Callable[[ConformanceProgram], Any] | None = None,
    pending: bool | None = None,
    local: bool | None = None,
    target: str | None = None,
) -> list[CaseResult]:
    """Run cases over Modbus TCP and return one result per case, in group order.

    Args:
        program: A program built with :func:`build_program`, or groups to build
            one from (packed layout, fine for the local sim).
        endpoint: ``host[:port]`` of a CLICK running the exported program.
            ``None`` starts a local target on a free localhost port: *sim*.
        groups: Names of the groups to run; ``None`` runs every group.  The
            other groups' registers are never touched.
        timeout: Seconds to wait for each phase (header, zeroing, ``Fired``).
        sim: The local target to start when *endpoint* is ``None``, called
            with the build: :class:`SimServer` (pyrung's soft PLC, the
            default) or :class:`~pyrung.conformance.CircuitPySim` (the
            program's generated P1AM-200 code).
        pending: Judge ``pending`` cases as strict xfails, as for pyrung's
            own targets.  ``None`` (the default) follows *local*.
        local: The endpoint is one of pyrung's own targets, not a real PLC.
            ``None`` (the default) means *endpoint* is ``None``.  Pass ``True``
            for a local target served on an endpoint of its own.
        target: The local target's name.  A case whose ``pending_on`` names it is
            a strict xfail.  ``None`` (the default) takes the name from *sim* when
            *endpoint* is ``None``.

    A selected group whose subroutine hash does not match is stale: its cases
    come back with ``stale=True`` without running, and the other groups still run.

    The main program keeps the longest scan in ``DS2``.  The harness writes 0
    there before it zeroes the slots and reads it after the last read.  On a
    real PLC a scan past the build's ``scan_limit_ms`` voids the run.  On a
    local target the run prints a line instead and adds it to every failed case.

    Raises:
        BuildMismatchError: ``DS1`` does not hold this build's layout hash.
        ScanTimeError: A scan on a real PLC took longer than the build's
            ``scan_limit_ms``.
        ConformanceError: The endpoint is unreachable or the phases time out
            before any case can be judged.
        ValueError: *groups* names a group the program does not have.
    """
    build = program if isinstance(program, ConformanceProgram) else build_program(program)
    selected = _select(build, groups)
    wire = [g for g in build.groups if g.name in selected]
    if isinstance(endpoint, str):
        endpoint = parse_endpoint(endpoint)
    if local is None:
        local = endpoint is None
    if pending is None:
        pending = local
    if target is None and endpoint is None:
        target = getattr(sim or SimServer, "target", None)

    by_group: dict[str, list[CaseResult]] = {}
    if wire:
        if endpoint is None:
            with (sim or SimServer)(build) as server:
                assert server.endpoint is not None
                try:
                    by_group = asyncio.run(
                        _drive(build, wire, server.endpoint, timeout, pending, local, target)
                    )
                finally:
                    server.raise_if_failed()
        else:
            by_group = asyncio.run(_drive(build, wire, endpoint, timeout, pending, local, target))

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


def read_program_hash(endpoint: Endpoint | str, *, timeout: float = 10.0) -> int:
    """Return the layout hash in ``DS1`` of the conformance program running at *endpoint*.

    Programs built from different groups have different hashes. The hash tells you
    which build is loaded before you choose one to run. A suite too big for one
    CLICK project loads in parts, so more than one build can exist.

    Raises:
        ConformanceError: The endpoint is unreachable or ``DS1`` stays 0.
    """
    from pyclickplc.client import ClickClient

    if isinstance(endpoint, str):
        endpoint = parse_endpoint(endpoint)

    async def read(endpoint: Endpoint) -> int:
        client = ClickClient(endpoint.host, endpoint.port, timeout=max(1, int(timeout)))
        async with client:
            if not client._client.connected:
                raise ConformanceError(f"Cannot connect to Modbus endpoint {endpoint}")
            return await _await_program_hash(client, endpoint, timeout)

    return asyncio.run(read(endpoint))


def wait_for_scan_counter_wrap(
    endpoint: Endpoint | str,
    *,
    timeout: float | None = None,
    report: Callable[[str], None] = print,
) -> int:
    """Wait until the scan counter SD9 at *endpoint* has passed 32767.

    A probe of what SD9 does after 32767 can only give an answer once SD9 has got there.
    SD9 has passed 32767 when a read is lower than the one before it (it wrapped,
    to 0 or negative) or is outside 0..32767 (it counts on, read signed or
    unsigned).  A PLC that passed 32767 long ago wraps again within one lap, so
    this waits at most one lap: 32768 scans, or 5.5 minutes at 10 ms.

    Args:
        timeout: Seconds to wait.  ``None`` waits half again the time that the
            lap should take at the scan rate measured over the first second,
            plus 30 s.
        report: Called with one line saying how long the wait should take.

    Returns:
        The first SD9 read past 32767.

    Raises:
        ConformanceError: The endpoint is unreachable, SD9 does not move, or
            it does not wrap in time.
    """
    from pyclickplc.client import ClickClient

    if isinstance(endpoint, str):
        endpoint = parse_endpoint(endpoint)

    async def wait(endpoint: Endpoint) -> int:
        client = ClickClient(endpoint.host, endpoint.port, timeout=10)
        async with client:
            if not client._client.connected:
                raise ConformanceError(f"Cannot connect to Modbus endpoint {endpoint}")
            first = int(await client.sd[_SCAN_COUNTER])
            if scan_counter_wrapped(first, first):
                return first
            await asyncio.sleep(_WRAP_RATE_S)
            now = int(await client.sd[_SCAN_COUNTER])
            if scan_counter_wrapped(first, now):
                return now
            rate = (now - first) / _WRAP_RATE_S
            if rate <= 0:
                raise ConformanceError(f"SD9 on {endpoint} does not count (reads {now})")
            eta = (32768 - now) / rate
            limit = timeout if timeout is not None else eta * 1.5 + 30
            report(
                f"Waiting ~{eta:.0f} s for SD9 on {endpoint} to pass 32767 "
                f"(reads {now}, {rate:.0f} scans/s)"
            )
            deadline = time.monotonic() + limit
            while True:
                await asyncio.sleep(_WRAP_POLL_S)
                prev, now = now, int(await client.sd[_SCAN_COUNTER])
                if scan_counter_wrapped(prev, now):
                    return now
                if time.monotonic() >= deadline:
                    raise ConformanceError(
                        f"SD9 on {endpoint} did not pass 32767 within {limit:.0f} s (reads {now})"
                    )

    return asyncio.run(wait(endpoint))


def scan_counter_wrapped(previous: int, current: int) -> bool:
    """Return True when two SD9 reads, *previous* then *current*, show it passed 32767."""
    return current < previous or not 0 <= current <= 32767


async def _await_program_hash(client: Any, endpoint: Endpoint, timeout: float) -> int:
    (found,) = await _poll(
        lambda: _read_ds(client, PROGRAM_HASH_REGISTER, 1),
        lambda regs: regs[0] != 0,
        timeout,
        f"DS{PROGRAM_HASH_REGISTER} on {endpoint} stayed 0, so the conformance program is not "
        "running. Load the exported project and put the PLC in RUN.",
    )
    return found


async def wait_scans_after_write(client: Any, deadline: float) -> tuple[bool, int]:
    """Wait until SD9 has moved on two scans since this call began, or until *deadline*.

    A write reaches the ladder at the start of a later scan, so a read just after a write can
    still return a scan from before it. Call this right after the write. Return whether SD9
    moved on two scans, and how many scans it moved. It moved fewer only when *deadline* passed
    first.
    """
    start = int(await client.sd[_SCAN_COUNTER])
    while True:
        moved = (int(await client.sd[_SCAN_COUNTER]) - start) % _SCAN_COUNTER_WRAP
        if moved >= _SCANS_AFTER_WRITE or time.monotonic() >= deadline:
            return moved >= _SCANS_AFTER_WRITE, moved
        await asyncio.sleep(POLL_S)


async def _drive(
    build: ConformanceProgram,
    wire: list[GroupBuild],
    endpoint: Endpoint,
    timeout: float,
    pending: bool,
    local: bool,
    target: str | None,
) -> dict[str, list[CaseResult]]:
    from pyclickplc.client import ClickClient

    results: dict[str, list[CaseResult]] = {}
    client = ClickClient(endpoint.host, endpoint.port, timeout=max(1, int(timeout)))
    async with client:
        if not client._client.connected:
            raise ConformanceError(f"Cannot connect to Modbus endpoint {endpoint}")

        # 0. Header: is a program with this group layout running?
        found = await _await_program_hash(client, endpoint, timeout)
        # A PLC in STOP still serves its registers, but runs no ladder: no
        # reset would ever complete.  A case that stops the PLC leaves it so.
        if not await client.sc[_MODE_RUN_BIT]:
            raise ConformanceError(
                f"{endpoint} is in STOP mode (SC{_MODE_RUN_BIT} reads 0): put the PLC in "
                "RUN, then run again"
            )
        if found != build.program_hash:
            raise BuildMismatchError(
                f"{endpoint} runs a program with layout hash {found}, but these groups build "
                f"layout hash {build.program_hash}: "
                f"{', '.join(g.subroutine for g in build.groups)}. "
                "Export the project again and load it."
            )

        # Each selected group's own hash: a mismatch marks the group stale.
        live: list[GroupBuild] = []
        for g in wire:
            (group_hash,) = await _read_ds(client, g.hash_register, 1)
            if group_hash == g.group_hash:
                live.append(g)
                continue
            where = "main and its helper subroutines" if g.group.in_main else g.subroutine
            note = (
                f"{where} is stale. Copy this build's {where} to CLICK and write it to "
                f"the PLC. DS{g.hash_register} reads {group_hash}, and this build writes "
                f"{g.group_hash}."
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

        # The ladder keeps the longest scan from here on.
        await client.ds.write(LONGEST_SCAN_REGISTER, 0)

        # 1-3. Zero the live groups' slots, then wait for every case's reset:
        # Ready, with Cmd and Fired still 0.  A case whose reset never
        # completes is judged, not fired.
        for g in live:
            start, width = g.span
            await _write_ds(client, start, [0] * width)
        slots = [(g, i) for g in live for i, _ in g.cases]
        deadline = time.monotonic() + timeout
        waited, moved = await wait_scans_after_write(client, deadline)
        while True:
            regs = _by_register(live, await _read_spans(client, live))
            unready = {(g.name, i) for g, i in slots if not (waited and _reset_done(g, i, regs))}
            if not unready or time.monotonic() >= deadline:
                break
            await asyncio.sleep(POLL_S)
        reset_regs = regs
        ready = [(g, i) for g, i in slots if (g.name, i) not in unready]

        # 4. Cmd = 1, one single-register write per slot so a slot's other
        # registers are never rewritten after its case has fired.
        for g, i in ready:
            await client.ds.write(g.register(i, "Cmd"), 1)

        # 5. Wait for every Fired (a case that never fires is judged, not raised).
        # A case that fires last may take longer, and leaves nothing to settle.
        fired_registers = [g.register(i, "Fired") for g, i in ready]
        cases = [c for g in live for _, c in g.cases]
        deadline = time.monotonic() + max([timeout, *(c.fires_within or 0.0 for c in cases)])
        while True:
            regs = _by_register(live, await _read_spans(client, live))
            if all(regs[r] != 0 for r in fired_registers) or time.monotonic() >= deadline:
                break
            await asyncio.sleep(POLL_S)
        scans = max((c.scans for c in cases if c.fires_within is None), default=1)
        settle = scans * NOMINAL_SCAN_S
        await asyncio.sleep(settle)

        # 6. Read every slot (and every system bit a case expects) twice; both
        # reads must meet the expectation.
        first = _by_register(live, await _read_spans(client, live))
        first_bits = await _read_system_bits(client, live)
        await asyncio.sleep(max(CONFIRM_GAP_S, settle))
        second = _by_register(live, await _read_spans(client, live))
        second_bits = await _read_system_bits(client, live)
        (longest,) = await _read_ds(client, LONGEST_SCAN_REGISTER, 1)

    # On a real PLC a scan past the cases' limit voids the run before anything is judged.
    if not local:
        check_scan_time(build, longest)

    # 7. Compare.
    for g in live:
        group_results: list[CaseResult] = []
        for i, c in g.cases:
            actual_first = {f: first[g.register(i, f)] for f in c.expect}
            actual_first.update({a: first_bits[a] for a in c.expect_system})
            actual = {f: second[g.register(i, f)] for f in c.expect}
            actual.update({a: second_bits[a] for a in c.expect_system})
            fired = second[g.register(i, "Fired")] != 0
            met_first = fired and expectation_met(c.checks, actual_first)
            met_second = fired and expectation_met(c.checks, actual)
            notes: list[str] = []
            if (g.name, i) in unready:
                state = {f: reset_regs[g.register(i, f)] for f in ("Cmd", "Fired", "Ready")}
                notes.append(
                    f"reset did not complete within {timeout:g} s, SD9 moved {moved} scans: {state}"
                )
            elif met_first != met_second:
                notes.append(f"unstable: first read {actual_first}, second read {actual}")
            passed, xfail, pending_note = judge(
                c,
                [met_first, met_second],
                (g.name, i) not in unready,
                actual,
                pending=pending,
                target=target,
            )
            if pending_note:
                notes.insert(0, pending_note)
            step = timed_out_step(second[g.register(i, "Fired")])
            if step is not None:
                passed, xfail = False, False
                notes.insert(0, f"timed out at step {step}")
            group_results.append(
                CaseResult(
                    case=c,
                    passed=passed,
                    actual=actual,
                    slot_index=i,
                    fired=fired,
                    note="; ".join(notes),
                    group=g.name,
                    xfail=xfail,
                    case_hash=g.case_hashes.get(c.id, ""),
                )
            )
        results[g.name] = group_results
    note = scan_limit_note(build.part, build.scan_limit_ms, longest) if local else None
    if note is not None:
        print(note)
        results = {name: note_failures(rs, note) for name, rs in results.items()}
    return results


def _reset_done(g: GroupBuild, index: int, regs: dict[int, int]) -> bool:
    """Return True when the case's reset ran: ``Ready`` is up and ``Cmd`` and ``Fired`` read 0."""
    return (
        regs[g.register(index, "Ready")] != 0
        and regs[g.register(index, "Cmd")] == 0
        and regs[g.register(index, "Fired")] == 0
    )


async def _read_system_bits(client: Any, groups: Sequence[GroupBuild]) -> dict[str, int]:
    """Read every system bit (``"SC46"``) that a case of *groups* expects, as 0 or 1."""
    addresses = sorted({a for g in groups for _, c in g.cases for a in c.expect_system})
    return {a: int(bool(await client.sc[int(a[2:])])) for a in addresses}


async def _read_spans(client: Any, groups: Sequence[GroupBuild]) -> list[int]:
    """Read every group's used slots, concatenated in group order."""
    values: list[int] = []
    for g in groups:
        start, width = g.span
        values.extend(await _read_ds(client, start, width))
    return values


def _by_register(groups: Sequence[GroupBuild], values: list[int]) -> dict[int, int]:
    """Return a :func:`_read_spans` result keyed by DS register."""
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
