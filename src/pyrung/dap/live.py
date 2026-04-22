"""pyrung-live: out-of-process console attachment.

Server side (``LiveServer``) runs inside the DAP adapter process,
accepting connections on a TCP socket bound to localhost.
Client side (``main()``) is the ``pyrung-live`` CLI entry point.

Protocol: plain text over a length-prefixed TCP connection.
  - Client sends a command (UTF-8 text via ``send_bytes``).
  - Server sends the result text back, then closes the connection.
  - Error responses are prefixed with ``ERROR: ``.

Session discovery uses port files in a well-known directory:
  ``<session_dir>/pyrung-<name>.port`` containing the TCP port number.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Any

_SESSION_DIR = Path(
    os.environ.get("PYRUNG_SESSION_DIR", str(Path(tempfile.gettempdir()) / "pyrung"))
)


def _port_file(session_name: str) -> Path:
    return _SESSION_DIR / f"pyrung-{session_name}.port"


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class LiveServer:
    """TCP server embedded in the DAP adapter."""

    def __init__(self, adapter: Any, session_name: str) -> None:
        self._adapter = adapter
        self._session_name = session_name
        self._listener: Listener | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._port: int | None = None

    @property
    def session_name(self) -> str:
        return self._session_name

    @property
    def port(self) -> int | None:
        return self._port

    def start(self) -> None:
        self._listener = Listener(("localhost", 0), family="AF_INET")
        self._port = int(self._listener.address[1])
        _SESSION_DIR.mkdir(parents=True, exist_ok=True)
        _port_file(self._session_name).write_text(str(self._port), encoding="utf-8")
        self._thread = threading.Thread(
            target=self._accept_loop, daemon=True, name=f"pyrung-live-{self._session_name}"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        pf = _port_file(self._session_name)
        if pf.exists():
            pf.unlink(missing_ok=True)

    def _accept_loop(self) -> None:
        listener = self._listener
        assert listener is not None
        while not self._stop.is_set():
            try:
                conn = listener.accept()
            except OSError:
                break
            try:
                self._handle(conn)
            except Exception:
                pass
            finally:
                conn.close()

    def _handle(self, conn: Any) -> None:
        raw = conn.recv_bytes()
        command = raw.decode("utf-8").strip()
        if not command:
            conn.send_bytes(b"ERROR: empty command")
            return

        from pyrung.dap.console import dispatch

        try:
            with self._adapter._state_lock:
                result = dispatch(self._adapter, command)
            conn.send_bytes(result.text.encode("utf-8"))
        except Exception as exc:
            conn.send_bytes(f"ERROR: {exc}".encode())


# ---------------------------------------------------------------------------
# Client helpers
# ---------------------------------------------------------------------------


def _resolve_address(session_name: str) -> tuple[str, int]:
    """Read the port file for *session_name* and return ``('localhost', port)``."""
    pf = _port_file(session_name)
    if not pf.exists():
        raise FileNotFoundError(f"Session '{session_name}' not found (no port file)")
    port = int(pf.read_text(encoding="utf-8").strip())
    return ("localhost", port)


def send_command(session_name: str, command: str) -> tuple[bool, str]:
    """Connect, send *command*, return ``(ok, text)``."""
    address = _resolve_address(session_name)
    conn = Client(address, family="AF_INET")
    try:
        conn.send_bytes(command.encode("utf-8"))
        raw = conn.recv_bytes()
        text = raw.decode("utf-8")
        if text.startswith("ERROR: "):
            return False, text[7:]
        return True, text
    finally:
        conn.close()


def list_sessions() -> list[str]:
    """Return names of sessions that have port files."""
    if not _SESSION_DIR.is_dir():
        return []
    prefix = "pyrung-"
    suffix = ".port"
    return sorted(
        p.name[len(prefix) : -len(suffix)]
        for p in _SESSION_DIR.glob(f"{prefix}*{suffix}")
        if p.is_file()
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """``pyrung-live`` command-line entry point."""
    parser = argparse.ArgumentParser(
        prog="pyrung-live",
        description="Attach to a running pyrung DAP session",
    )
    parser.add_argument("--session", "-s", help="Session name to connect to")
    parser.add_argument("command", nargs="*", help="Console command to send")
    args = parser.parse_args()

    if not args.session and args.command and args.command[0] == "list":
        sessions = list_sessions()
        if sessions:
            for name in sessions:
                print(name)
        else:
            print("No active sessions")
        return

    if not args.session:
        parser.error("--session is required (or use 'pyrung-live list')")

    if not args.command:
        parser.error("No command given")

    command = " ".join(args.command)
    try:
        ok, text = send_command(args.session, command)
    except ConnectionRefusedError:
        print(f"Cannot connect to session '{args.session}'", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print(f"Session '{args.session}' not found", file=sys.stderr)
        sys.exit(1)

    print(text)
    sys.exit(0 if ok else 1)
