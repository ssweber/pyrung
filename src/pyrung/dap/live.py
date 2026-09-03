"""pyrung live: out-of-process console attachment.

Server side (``LiveServer``) runs inside the DAP adapter process,
accepting connections on a TCP socket bound to localhost.
Client side (``main()``) is the ``pyrung live`` CLI entry point.

Protocol: plain text over a length-prefixed TCP connection.
  - Client sends a command (UTF-8 text via ``send_bytes``).
  - Server sends zero or more progress frames (``\\x01`` prefix + UTF-8 text),
    then the final result text (no prefix), then closes the connection.
  - Error responses are prefixed with ``ERROR: ``.

Session discovery uses port files in a well-known directory:
  ``<session_dir>/pyrung-<name>.port`` containing the TCP port number.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import tempfile
import threading
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Any

_SESSION_DIR = Path(
    os.environ.get(
        "PYRUNG_SESSION_DIR",
        str(Path(os.environ.get("TEMP", os.environ.get("TMP", tempfile.gettempdir()))) / "pyrung"),
    )
)


#: Verbs answered by the server itself, ahead of the adapter's state lock.
_CANCEL_VERBS = frozenset({"stop", "cancel"})


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
        self._inflight_lock = threading.Lock()
        self._inflight: str | None = None

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
            # One thread per connection. A serial loop could not accept a
            # `stop` until the command it is meant to cancel had finished.
            threading.Thread(
                target=self._serve,
                args=(conn,),
                daemon=True,
                name=f"pyrung-live-conn-{self._session_name}",
            ).start()

    def _serve(self, conn: Any) -> None:
        try:
            self._handle(conn)
        except Exception:
            pass
        finally:
            conn.close()

    def _request_cancel(self) -> str:
        """Flag the in-flight command for cancellation. Never takes the state lock."""
        with self._inflight_lock:
            inflight = self._inflight
        if inflight is None:
            return "Nothing running."
        cancel = getattr(self._adapter, "_cancel", None)
        if cancel is None:
            return "ERROR: this adapter does not support stop"
        cancel.request()
        return f"Stop requested ({inflight})."

    def _handle(self, conn: Any) -> None:
        raw = conn.recv_bytes()
        command = raw.decode("utf-8").strip()
        if not command:
            conn.send_bytes(b"ERROR: empty command")
            return

        # Answered ahead of ``_state_lock`` -- the entire point of `stop` is to
        # reach the adapter while a long command still holds that lock.
        if command.split()[0].lower() in _CANCEL_VERBS:
            conn.send_bytes(self._request_cancel().encode("utf-8"))
            return

        from pyrung.dap.console import dispatch

        try:
            with self._adapter._state_lock:
                # Patch _send_event under the lock: two concurrent connections
                # would otherwise capture each other's wrapper as "original"
                # and leak progress onto the wrong socket.
                cancel = getattr(self._adapter, "_cancel", None)
                if cancel is not None:
                    cancel.reset()
                with self._inflight_lock:
                    self._inflight = command
                original_send = self._adapter._send_event

                def _send_with_progress(event: str, body: dict[str, Any] | None = None) -> None:
                    original_send(event, body)
                    if event == "output" and body and body.get("category") == "console":
                        text = body.get("output", "")
                        conn.send_bytes(b"\x01" + text.encode("utf-8"))

                self._adapter._send_event = _send_with_progress  # type: ignore[assignment]
                try:
                    result = dispatch(self._adapter, command, provenance="live")
                finally:
                    self._adapter._send_event = original_send  # type: ignore[assignment]
                    with self._inflight_lock:
                        self._inflight = None
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


def send_command(
    session_name: str,
    command: str,
    *,
    on_progress: Any = None,
) -> tuple[bool, str]:
    """Connect, send *command*, return ``(ok, text)``.

    If *on_progress* is provided it is called with each progress line
    (str) the server emits during long-running commands like ``how``.
    """
    address = _resolve_address(session_name)
    conn = Client(address, family="AF_INET")
    try:
        conn.send_bytes(command.encode("utf-8"))
        while True:
            raw = conn.recv_bytes()
            if raw[:1] == b"\x01":
                if on_progress is not None:
                    on_progress(raw[1:].decode("utf-8"))
                continue
            text = raw.decode("utf-8")
            if text.startswith("ERROR: "):
                return False, text[7:]
            return True, text
    finally:
        conn.close()


def _is_port_alive(port: int) -> bool:
    """Return True if something is listening on localhost:*port*."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.1)
    try:
        sock.connect(("localhost", port))
        return True
    except (ConnectionRefusedError, OSError):
        return False
    finally:
        sock.close()


def list_sessions() -> list[str]:
    """Return names of sessions whose servers are still reachable.

    Stale port files (left by crashed or exited processes) are removed
    automatically.
    """
    if not _SESSION_DIR.is_dir():
        return []
    prefix = "pyrung-"
    suffix = ".port"
    alive: list[str] = []
    for p in _SESSION_DIR.glob(f"{prefix}*{suffix}"):
        if not p.is_file():
            continue
        name = p.name[len(prefix) : -len(suffix)]
        try:
            port = int(p.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            p.unlink(missing_ok=True)
            continue
        if _is_port_alive(port):
            alive.append(name)
        else:
            p.unlink(missing_ok=True)
    return sorted(alive)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_command_epilog() -> str:
    import pyrung.dap.bounds_console  # noqa: F401
    import pyrung.dap.capture  # noqa: F401
    import pyrung.dap.harness_console  # noqa: F401
    import pyrung.dap.miner_console  # noqa: F401
    import pyrung.dap.reload_console  # noqa: F401
    import pyrung.dap.spec_console  # noqa: F401
    from pyrung.dap.console import _format_grouped_help

    return _format_grouped_help()


def main() -> None:
    """``pyrung live`` command-line entry point."""
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
    parser = argparse.ArgumentParser(
        prog="pyrung live",
        description="Attach to a running pyrung DAP session",
        epilog=_build_command_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--session", "-s", help="Session name to connect to")
    parser.add_argument("command", nargs="*", help="Console command (use ; to chain)")
    args = parser.parse_args()

    show_list = (not args.session and args.command and args.command[0] == "list") or (
        not args.session and not args.command
    )
    if show_list:
        sessions = list_sessions()
        if not sessions:
            print("No active sessions")
        elif len(sessions) == 1:
            print(f"Active session: {sessions[0]}")
            print("Usage: pyrung live <command>  (e.g. pyrung live help)")
        else:
            print("Live sessions:")
            for name in sessions:
                print(f"  {name}")
            print("Usage: pyrung live -s <session> <command>")
        return

    if not args.session:
        sessions = list_sessions()
        if len(sessions) == 1:
            args.session = sessions[0]
        elif not sessions:
            parser.error("no active sessions")
        else:
            parser.error(
                f"multiple sessions active ({', '.join(sessions)}), use --session to pick one"
            )

    if not args.command:
        parser.error("No command given")

    raw = " ".join(args.command)
    commands = [c.strip() for c in raw.split(";") if c.strip()]

    def _print_progress(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    all_ok = True
    for _i, command in enumerate(commands):
        try:
            ok, text = send_command(args.session, command, on_progress=_print_progress)
        except ConnectionRefusedError:
            print(f"Cannot connect to session '{args.session}'", file=sys.stderr)
            sys.exit(1)
        except FileNotFoundError:
            print(f"Session '{args.session}' not found", file=sys.stderr)
            sys.exit(1)
        except KeyboardInterrupt:
            # Ask the session to cancel rather than just dropping the socket:
            # the server would otherwise keep planning, holding its state lock
            # and locking out every later command.
            print()
            try:
                _, note = send_command(args.session, "stop")
                print(note)
            except (ConnectionRefusedError, FileNotFoundError, OSError) as exc:
                print(f"Cancel request failed: {exc}", file=sys.stderr)
            sys.exit(130)
        print(text)
        if not ok:
            all_ok = False
            break
    sys.exit(0 if all_ok else 1)
