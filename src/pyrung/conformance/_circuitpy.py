"""The conformance program as P1AM-200 code, run on this machine.

:class:`CircuitPySim` stands where :class:`~pyrung.conformance.SimServer`
does, but what it serves is not pyrung's engine: it is the ``code.py`` that
``generate_circuitpy()`` writes for a P1AM-200, run unchanged under this
machine's Python by :mod:`pyrung.circuitpy.host`, answering Modbus through its
own generated server.  The harness talks to it exactly as it talks to a CLICK.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from pyrung.conformance._build import ConformanceProgram
from pyrung.conformance._modbus import (
    NOMINAL_SCAN_S,
    ConformanceError,
    Endpoint,
    _free_port,
)

#: Seconds the child gets to compile ``code.py`` and listen.
_START_TIMEOUT_S = 60.0
#: Seconds the child gets to exit once told to.
_STOP_TIMEOUT_S = 5.0
#: Characters of the child's output quoted when it dies.
_LOG_TAIL = 4000


def generate(build: ConformanceProgram, *, ip: str = "127.0.0.1", port: int = 502) -> Any:
    """``generate_circuitpy()`` output for *build*: the P1AM-200 project to load.

    The generated server serves every register *build*'s TagMap maps (slots,
    their spares, the hashes) and every raw register a case uses.
    """
    from pyrung.circuitpy import P1AM, ModbusServerConfig, generate_circuitpy

    hw = P1AM()
    hw.slot(1, "P1-08SIM")  # the generator wants one module; the program reads none
    return generate_circuitpy(
        build.program,
        hw,
        target_scan_ms=NOMINAL_SCAN_S * 1000.0,
        modbus_server=ModbusServerConfig(ip=ip, port=port),
        tag_map=build.tag_map,
    )


class CircuitPySim:
    """The conformance program's P1AM-200 code, run on this machine and served over Modbus.

    ``code.py`` runs in a child Python with stand-ins for the board's hardware
    (:mod:`pyrung.circuitpy.host`): its own scan loop, wall-clock timers, and
    its own generated Modbus server between scans.  The RUN/STOP switch reads
    RUN; a math error puts the code in STOP, where it keeps serving Modbus.
    Same interface as :class:`~pyrung.conformance.SimServer`: a context
    manager with :attr:`endpoint` and :meth:`raise_if_failed`.
    """

    def __init__(self, build: ConformanceProgram, *, host: str = "127.0.0.1", port: int = 0):
        self.build = build
        self._host = host
        self._port = port
        self._dir: tempfile.TemporaryDirectory[str] | None = None
        self._log: Any = None
        self._proc: subprocess.Popen[bytes] | None = None
        self.endpoint: Endpoint | None = None

    def __enter__(self) -> CircuitPySim:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> Endpoint:
        from pyrung.circuitpy import host

        output = generate(self.build, ip=self._host)
        self._dir = tempfile.TemporaryDirectory(prefix="pyrung-circuitpy-")
        directory = Path(self._dir.name)
        (directory / "code.py").write_text(output.code, encoding="utf-8")
        (directory / "pyrung_rt.py").write_text(output.runtime, encoding="utf-8")
        port = self._port or _free_port(self._host)
        self._log = (directory / "host.log").open("wb")
        # -I: the child imports only the stdlib, the stand-ins and code.py's folder.
        self._proc = subprocess.Popen(
            [sys.executable, "-I", host.__file__, str(directory), "--host", self._host]
            + ["--port", str(port)],
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + _START_TIMEOUT_S
        while True:
            self.raise_if_failed()
            try:
                socket.create_connection((self._host, port), timeout=0.2).close()
                break
            except OSError:
                if time.monotonic() >= deadline:
                    self.stop()
                    raise ConformanceError(
                        f"CircuitPython host did not listen on {self._host}:{port} "
                        f"within {_START_TIMEOUT_S:g} s"
                    ) from None
                time.sleep(0.05)
        self.endpoint = Endpoint(self._host, port)
        return self.endpoint

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=_STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=_STOP_TIMEOUT_S)
        if self._log is not None:
            self._log.close()
            self._log = None
        if self._dir is not None:
            self._dir.cleanup()
            self._dir = None

    def raise_if_failed(self) -> None:
        """Raise if ``code.py`` has exited: on a board it never returns."""
        proc = self._proc
        if proc is None or proc.poll() is None:
            return
        self._log.flush()
        tail = (Path(self._log.name).read_bytes().decode("utf-8", "replace"))[-_LOG_TAIL:]
        raise ConformanceError(
            f"CircuitPython host exited with code {proc.returncode}:\n{tail.rstrip()}"
        )
