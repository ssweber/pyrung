"""Run generated P1AM-200 code on a desktop Python, no board attached.

``generate_circuitpy()`` writes a ``code.py`` (and ``pyrung_rt.py``) for a
P1AM-200.  This module runs that same file under CPython: it installs stand-in
modules for the board's hardware (``board``, ``digitalio``, ``busio``,
``P1AM``, the SD card, ``microcontroller``, the WIZnet Ethernet stack) and
then runs ``code.py`` unchanged, scan loop and all::

    python path/to/host.py DIR [--host 127.0.0.1] [--port 5020]

What the stand-ins do:

- The WIZnet socket pool hands out ordinary TCP sockets, so the generated
  Modbus server answers on *--host*.  *--port* overrides the port baked into
  ``code.py``.
- ``board.SWITCH`` reads high: the RUN/STOP switch is in RUN.
- Every P1AM module input reads 0; outputs go nowhere.
- There is no SD card, so retentive memory starts fresh on every run.

It catches what the generated logic and the generated Modbus server do; it
does not catch what only the board does (REAL math in single precision, the
CircuitPython language subset, scan timing, WIZnet quirks).

Standard library only: run it by path (``python -I host.py``) so it starts
without importing pyrung.
"""

from __future__ import annotations

import argparse
import importlib.util
import socket
import sys
import types
from pathlib import Path
from typing import Any


class _Pin:
    """A named board pin."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"board.{self.name}"


class _DigitalInOut:
    def __init__(self, pin: Any) -> None:
        self.pin = pin
        self.direction = None
        self.pull = None
        # The RUN/STOP switch is in RUN; every other pin reads low.
        self.value = getattr(pin, "name", None) == "SWITCH"


class _NeoPixel:
    def __init__(self, pin: Any, count: int, **_kwargs: Any) -> None:
        self._pixels = [(0, 0, 0)] * count

    def __setitem__(self, index: int, value: Any) -> None:
        self._pixels[index] = value

    def __getitem__(self, index: int) -> Any:
        return self._pixels[index]


class _P1AMBase:
    """``P1AM.Base()``: no modules answer; every read is 0, every write is dropped."""

    def __getattr__(self, name: str) -> Any:
        return lambda *_args, **_kwargs: 0


def _no_sd_card(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError("no SD card on the host")


class _Wiznet5k:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.ifconfig = None


class HostSocket:
    """A WIZnet socket, played by a CPython TCP socket.

    Two differences matter to the generated server.  With timeout 0, a WIZnet
    ``recv_into`` with nothing to read returns 0 where CPython raises
    ``BlockingIOError`` (which the server would take as a dead client); and
    the server asks ``_connected`` to tell an idle client from a closed one.
    """

    def __init__(
        self, sock: socket.socket | None = None, *, host: str = "127.0.0.1", port: int = 0
    ) -> None:
        self._sock = sock if sock is not None else socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._host = host
        self._port = port
        self._connected = True

    def bind(self, address: tuple[str, int]) -> None:
        self._sock.bind((self._host, self._port or address[1]))

    def listen(self, backlog: int) -> None:
        self._sock.listen(backlog)

    def settimeout(self, timeout: float | None) -> None:
        self._sock.settimeout(timeout)

    def setblocking(self, flag: bool) -> None:
        self._sock.setblocking(flag)

    def accept(self) -> tuple[HostSocket, Any]:
        conn, addr = self._sock.accept()
        return HostSocket(conn, host=self._host), addr

    def connect(self, address: tuple[str, int]) -> None:
        try:
            self._sock.connect(address)
        except BlockingIOError:
            pass

    def recv_into(self, buffer: Any, nbytes: int = 0) -> int:
        try:
            n = self._sock.recv_into(buffer, nbytes)
        except BlockingIOError:
            return 0
        if n == 0:
            self._connected = False
        return n

    def send(self, data: Any) -> int:
        return self._sock.send(data)

    def close(self) -> None:
        self._connected = False
        self._sock.close()


def install(host: str = "127.0.0.1", port: int = 0) -> None:
    """Put the board's stand-in modules in ``sys.modules``."""

    def module(name: str, **attrs: Any) -> types.ModuleType:
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        sys.modules[name] = mod
        return mod

    pins: dict[str, _Pin] = {}

    def pin(name: str) -> _Pin:
        if name.startswith("__"):
            raise AttributeError(name)
        return pins.setdefault(name, _Pin(name))

    module("board", __getattr__=pin)
    module("busio", SPI=lambda *_args, **_kwargs: object())
    module(
        "digitalio",
        DigitalInOut=_DigitalInOut,
        Direction=types.SimpleNamespace(INPUT="INPUT", OUTPUT="OUTPUT"),
        Pull=types.SimpleNamespace(UP="UP", DOWN="DOWN"),
    )
    module("neopixel", NeoPixel=_NeoPixel)
    module("sdcardio", SDCard=_no_sd_card)
    module("storage", VfsFat=_no_sd_card, mount=_no_sd_card, umount=lambda *_args: None)
    module("microcontroller", nvm=bytearray(1))
    module("P1AM", Base=_P1AMBase)

    class SocketPool:
        AF_INET = socket.AF_INET
        SOCK_STREAM = socket.SOCK_STREAM

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def socket(self, *_args: Any, **_kwargs: Any) -> HostSocket:
            return HostSocket(host=host, port=port)

    module(
        "adafruit_wiznet5k",
        adafruit_wiznet5k=module("adafruit_wiznet5k.adafruit_wiznet5k", WIZNET5K=_Wiznet5k),
        adafruit_wiznet5k_socketpool=module(
            "adafruit_wiznet5k.adafruit_wiznet5k_socketpool", SocketPool=SocketPool
        ),
    )


def run(directory: str | Path, *, host: str = "127.0.0.1", port: int = 0) -> None:
    """Run ``code.py`` from *directory* (with its ``pyrung_rt.py``); never returns."""
    directory = Path(directory).resolve()
    install(host, port)
    sys.path.insert(0, str(directory))
    # Loaded as a module (not "code", which is a stdlib name) so its bytecode is cached.
    spec = importlib.util.spec_from_file_location("__main__", directory / "code.py")
    if spec is None or spec.loader is None:
        raise FileNotFoundError(directory / "code.py")
    main = importlib.util.module_from_spec(spec)
    sys.modules["__main__"] = main
    spec.loader.exec_module(main)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run generated P1AM-200 code on this machine.")
    parser.add_argument("directory", help="folder holding code.py and pyrung_rt.py")
    parser.add_argument("--host", default="127.0.0.1", help="address the Modbus server binds")
    parser.add_argument("--port", type=int, default=0, help="override the Modbus server port")
    args = parser.parse_args(argv)
    run(args.directory, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
