"""Modbus TCP soft-PLC server embedded in a DAP session.

A ``pyclickplc.ClickServer`` serves the session's runner through a
``ClickDataProvider``, so ordinary Modbus clients read and write the
simulated PLC as they would a real CLICK.  The server's asyncio loop runs on
a background thread owned by this module.  Every provider call takes the
adapter's ``_state_lock`` and goes to whichever runner the session holds at
that moment, so the server keeps working across reloads.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from pyclickplc.server import PlcValue

#: Seconds to wait for the server to start or stop on its loop thread.
_LOOP_TIMEOUT = 5.0


@dataclass(frozen=True)
class ModbusConfig:
    host: str = "127.0.0.1"
    port: int = 502
    allow_input_writes: bool = False


def parse_modbus_config(adapter: Any, raw: Any) -> ModbusConfig | None:
    """Parse the ``modbus`` launch argument; ``None`` when absent."""
    if raw is None or raw is False:
        return None
    if raw is True:
        return ModbusConfig()
    if not isinstance(raw, dict):
        raise adapter.DAPAdapterError("launch.modbus must be an object")
    host = raw.get("host", ModbusConfig.host)
    port = raw.get("port", ModbusConfig.port)
    allow = raw.get("allowInputWrites", ModbusConfig.allow_input_writes)
    if not isinstance(host, str) or not host.strip():
        raise adapter.DAPAdapterError("launch.modbus.host must be a non-empty string")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise adapter.DAPAdapterError("launch.modbus.port must be an integer 0..65535")
    if not isinstance(allow, bool):
        raise adapter.DAPAdapterError("launch.modbus.allowInputWrites must be a boolean")
    return ModbusConfig(host=host.strip(), port=port, allow_input_writes=allow)


class SessionDataProvider:
    """``DataProvider`` that serves the adapter's current runner under its lock."""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    def _provider_locked(self) -> Any:
        adapter = self._adapter
        if adapter._runner is None or adapter._click_provider is None:
            raise RuntimeError("No program running")
        return adapter._click_provider

    def read(self, address: str) -> PlcValue:
        with self._adapter._state_lock:
            return self._provider_locked().read(address)

    def write(self, address: str, value: PlcValue) -> None:
        from pyrung.dap import execution_flow

        adapter = self._adapter
        with adapter._state_lock:
            self._provider_locked().write(address, value)
            # Paused mid-scan, the patch would otherwise miss the scan already
            # prepared.  While continue runs, the next scan picks it up.
            if not adapter._executor_running_locked():
                execution_flow.invalidate_mid_scan(adapter)
        self._adapter._request_state_frame({"origin": "modbus", "command": f"write {address}"})


class ModbusSimServer:
    """Owns a ``ClickServer`` and the event-loop thread it runs on."""

    def __init__(self, adapter: Any, config: ModbusConfig) -> None:
        self.config = config
        self.error: str | None = None
        self.port: int = config.port
        self._provider = SessionDataProvider(adapter)
        self._server: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """Start serving; on failure record ``error`` and return False."""
        from pyclickplc.server import ClickServer

        config = self.config
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True, name="pyrung-dap-modbus")
        thread.start()
        self._loop = loop
        self._thread = thread
        # Passed only when asked for, so pyclickplc releases without the
        # option still serve the default (real-PLC) behaviour.
        options = {"allow_input_writes": True} if config.allow_input_writes else {}
        try:
            server = ClickServer(self._provider, host=config.host, port=config.port, **options)
            self._run(server.start())
        except Exception as exc:
            self.error = _bind_error(config, exc)
            self._shutdown_loop()
            return False
        self._server = server
        self.port = _bound_port(server, config.port)
        return True

    def stop(self) -> None:
        """Stop the server and its loop thread.  Never call with the state lock held."""
        server = self._server
        self._server = None
        if server is not None and self._loop is not None:
            try:
                self._run(server.stop())
            except Exception:
                pass
        self._shutdown_loop()

    @property
    def running(self) -> bool:
        server = self._server
        return server is not None and bool(server.is_running())

    def client_count(self) -> int:
        server = self._server
        loop = self._loop
        if server is None or loop is None:
            return 0

        async def _count() -> int:
            return len(server.list_clients())

        try:
            return self._run(_count(), timeout=1.0)
        except Exception:
            return 0

    def _run(self, coro: Coroutine[Any, Any, Any], *, timeout: float = _LOOP_TIMEOUT) -> Any:
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)

    def _shutdown_loop(self) -> None:
        loop = self._loop
        thread = self._thread
        self._loop = None
        self._thread = None
        if loop is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=_LOOP_TIMEOUT)
        if not loop.is_running():
            loop.close()


def _bound_port(server: Any, fallback: int) -> int:
    """Return the port actually bound (differs from the request for port 0)."""
    try:
        sockets = server._server.transport.sockets
        return int(sockets[0].getsockname()[1])
    except Exception:
        return fallback


def _bind_error(config: ModbusConfig, exc: Exception) -> str:
    """Describe a start failure, preferring the OS reason over pymodbus' generic one."""
    reason = str(exc) or type(exc).__name__
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((config.host, config.port))
    except OSError as os_exc:
        reason = os_exc.strerror or str(os_exc)
    except Exception:
        pass
    finally:
        probe.close()
    return f"Cannot listen on {config.host}:{config.port}: {reason}"


def start_modbus(adapter: Any, config: ModbusConfig) -> None:
    """Start the session's Modbus server, reporting failure without raising."""
    stop_modbus(adapter)
    server = ModbusSimServer(adapter, config)
    adapter._modbus = server
    if server.start():
        text = f"Modbus TCP server listening on {config.host}:{server.port}"
        if config.allow_input_writes:
            text += " (X inputs writable)"
    else:
        text = f"Modbus TCP server not started. {server.error}"
    adapter._enqueue_internal_event("output", {"category": "console", "output": f"{text}\n"})


def stop_modbus(adapter: Any) -> None:
    server = adapter._modbus
    adapter._modbus = None
    if server is not None:
        server.stop()


def modbus_info(server: ModbusSimServer | None) -> dict[str, Any]:
    """Body of the ``pyrungModbusInfo`` response."""
    if server is None:
        return {
            "running": False,
            "host": None,
            "port": None,
            "allowInputWrites": False,
            "clients": 0,
            "error": None,
        }
    running = server.running
    return {
        "running": running,
        "host": server.config.host,
        "port": server.port,
        "allowInputWrites": server.config.allow_input_writes,
        "clients": server.client_count() if running else 0,
        "error": server.error,
    }
