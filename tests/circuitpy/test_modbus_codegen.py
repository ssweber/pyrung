"""Tests for CircuitPython Modbus code generation."""

from __future__ import annotations

import json
import struct
from pathlib import Path

from pyrung import Block, Bool, Int, Program, Rung, TagType
from pyrung.circuitpy import (
    P1AM,
    ModbusClientConfig,
    ModbusServerConfig,
    generate_circuitpy,
)
from pyrung.click import ModbusTarget, TagMap, c, ds, receive, send, x, y
from tests.circuitpy.test_codegen import _run_single_scan_source


def _mbap(tid: int, pdu: bytes, uid: int = 1) -> bytes:
    return struct.pack(">HHHB", tid, 0, len(pdu) + 1, uid) + pdu


def _ctx_and_source(program: Program, mapping: TagMap) -> str:
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    source = generate_circuitpy(
        program,
        hw,
        target_scan_ms=10.0,
        modbus_server=ModbusServerConfig(ip="192.168.1.200"),
        tag_map=mapping,
    )
    return source


def _client_source(program: Program, mapping: TagMap | None = None) -> str:
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    return generate_circuitpy(
        program,
        hw,
        target_scan_ms=10.0,
        modbus_client=ModbusClientConfig(
            targets=(ModbusTarget(name="peer", ip="192.168.1.50", port=1502, device_id=17),)
        ),
        tag_map=TagMap() if mapping is None else mapping,
    )


def test_modbus_server_source_emits_imports_and_scan_service():
    with Program(strict=False) as prog:
        pass

    source = generate_circuitpy(
        prog,
        _hw_with_slot(),
        target_scan_ms=10.0,
        modbus_server=ModbusServerConfig(ip="192.168.1.200"),
        tag_map=TagMap(),
    )
    assert "from adafruit_wiznet5k.adafruit_wiznet5k import WIZNET5K" in source
    assert "import adafruit_wiznet5k.adafruit_wiznet5k_socket as _mb_socket" in source
    assert "_mb_cs = digitalio.DigitalInOut(board.D5)" in source
    assert "_mb_server.listen(2)" in source
    assert "service_modbus_server()" in source


def _hw_with_slot() -> P1AM:
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    return hw


def test_read_and_write_backed_coil_and_register(monkeypatch):
    coil = Bool("Coil", default=True)
    reg = Int("Reg", default=7)
    mapping = TagMap({coil: c[1], reg: ds[1]})
    with Program(strict=False) as prog:
        pass

    source = _ctx_and_source(prog, mapping)

    class StubBase:
        def rollCall(self, modules):
            return None

        def readDiscrete(self, slot):
            return 0

        def writeDiscrete(self, value, slot):
            return None

        def readAnalog(self, slot, ch):
            return 0

        def writeAnalog(self, value, slot, ch):
            return None

        def readTemperature(self, slot, ch):
            return 0.0

    namespace = _run_single_scan_source(source, monkeypatch, StubBase())
    coil_symbol = "_t_Coil"
    reg_symbol = "_t_Reg"

    resp = namespace["_mb_handle"](_mbap(1, struct.pack(">BHH", 1, 16384, 1)), 12)
    assert resp is not None
    assert resp[-2:] == bytes([1, 1])

    resp = namespace["_mb_handle"](_mbap(2, struct.pack(">BHH", 3, 0, 1)), 12)
    assert resp is not None
    assert struct.unpack(">H", resp[-2:])[0] == 7

    resp = namespace["_mb_handle"](_mbap(3, struct.pack(">BHH", 5, 16384, 0)), 12)
    assert resp == _mbap(3, struct.pack(">BHH", 5, 16384, 0))
    assert namespace[coil_symbol] is False

    resp = namespace["_mb_handle"](_mbap(4, struct.pack(">BHH", 6, 0, 42)), 12)
    assert resp == _mbap(4, struct.pack(">BHH", 6, 0, 42))
    assert namespace[reg_symbol] == 42


def test_valid_unmapped_register_reads_zero_and_invalid_address_errors(monkeypatch):
    mapped = Int("Mapped", default=5)
    mapping = TagMap({mapped: ds[1]})
    with Program(strict=False) as prog:
        pass
    source = _ctx_and_source(prog, mapping)

    class StubBase:
        def rollCall(self, modules):
            return None

        def readDiscrete(self, slot):
            return 0

        def writeDiscrete(self, value, slot):
            return None

        def readAnalog(self, slot, ch):
            return 0

        def writeAnalog(self, value, slot, ch):
            return None

        def readTemperature(self, slot, ch):
            return 0.0

    namespace = _run_single_scan_source(source, monkeypatch, StubBase())

    resp = namespace["_mb_handle"](_mbap(10, struct.pack(">BHH", 3, 1, 1)), 12)
    assert resp is not None
    assert struct.unpack(">H", resp[-2:])[0] == 0

    resp = namespace["_mb_handle"](_mbap(11, struct.pack(">BHH", 3, 5000, 1)), 12)
    assert resp is not None
    assert resp[-2:] == bytes([0x83, 0x02])


def test_read_only_system_writes_acknowledge_without_exception(monkeypatch):
    with Program(strict=False) as prog:
        pass

    source = generate_circuitpy(
        prog,
        _hw_with_slot(),
        target_scan_ms=10.0,
        modbus_server=ModbusServerConfig(ip="192.168.1.200"),
        tag_map=TagMap(),
        mapped_tag_scope="all_mapped",
    )

    class StubBase:
        def rollCall(self, modules):
            return None

        def readDiscrete(self, slot):
            return 0

        def writeDiscrete(self, value, slot):
            return None

        def readAnalog(self, slot, ch):
            return 0

        def writeAnalog(self, value, slot, ch):
            return None

        def readTemperature(self, slot, ch):
            return 0.0

    namespace = _run_single_scan_source(source, monkeypatch, StubBase())

    coil_resp = namespace["_mb_handle"](_mbap(12, struct.pack(">BHH", 5, 61441, 0xFF00)), 12)
    assert coil_resp == _mbap(12, struct.pack(">BHH", 5, 61441, 0xFF00))

    reg_resp = namespace["_mb_handle"](_mbap(13, struct.pack(">BHH", 6, 61440, 99)), 12)
    assert reg_resp == _mbap(13, struct.pack(">BHH", 6, 61440, 99))


def test_yd_mirror_read_and_write(monkeypatch):
    outputs = Block("OutBits", TagType.BOOL, 1, 16)
    for idx in range(1, 17):
        outputs.configure_slot(idx, default=True)
    mapping = TagMap({outputs: y.select(1, 16)})
    with Program(strict=False) as prog:
        pass
    source = _ctx_and_source(prog, mapping)

    class StubBase:
        def rollCall(self, modules):
            return None

        def readDiscrete(self, slot):
            return 0

        def writeDiscrete(self, value, slot):
            return None

        def readAnalog(self, slot, ch):
            return 0

        def writeAnalog(self, value, slot, ch):
            return None

        def readTemperature(self, slot, ch):
            return 0.0

    namespace = _run_single_scan_source(source, monkeypatch, StubBase())
    block_symbol = "_b_OutBits"
    out_bits = namespace[block_symbol]
    assert isinstance(out_bits, list)
    out_bits[0] = True
    out_bits[2] = True
    out_bits[15] = True

    resp = namespace["_mb_handle"](_mbap(20, struct.pack(">BHH", 3, 57856, 1)), 12)
    assert resp is not None
    assert struct.unpack(">H", resp[-2:])[0] == 0x8005

    resp = namespace["_mb_handle"](_mbap(21, struct.pack(">BHH", 6, 57856, 0xA55A)), 12)
    assert resp == _mbap(21, struct.pack(">BHH", 6, 57856, 0xA55A))
    assert out_bits[0] is False
    assert out_bits[1] is True
    assert out_bits[3] is True
    assert out_bits[15] is True


def test_xd_mirror_read(monkeypatch):
    inputs = Block("InBits", TagType.BOOL, 1, 16)
    for idx in range(1, 17):
        inputs.configure_slot(idx, default=True)
    mapping = TagMap({inputs: x.select(1, 16)})
    with Program(strict=False) as prog:
        pass
    source = _ctx_and_source(prog, mapping)

    class StubBase:
        def rollCall(self, modules):
            return None

        def readDiscrete(self, slot):
            return 0

        def writeDiscrete(self, value, slot):
            return None

        def readAnalog(self, slot, ch):
            return 0

        def writeAnalog(self, value, slot, ch):
            return None

        def readTemperature(self, slot, ch):
            return 0.0

    namespace = _run_single_scan_source(source, monkeypatch, StubBase())
    block_symbol = "_b_InBits"
    in_bits = namespace[block_symbol]
    assert isinstance(in_bits, list)
    in_bits[0] = True
    in_bits[7] = True
    in_bits[15] = True

    resp = namespace["_mb_handle"](_mbap(30, struct.pack(">BHH", 4, 57344, 1)), 12)
    assert resp is not None
    assert struct.unpack(">H", resp[-2:])[0] == 0x8081


def test_service_modbus_server_closes_client_on_clean_disconnect(monkeypatch):
    with Program(strict=False) as prog:
        pass
    source = _ctx_and_source(prog, TagMap())

    class StubBase:
        def rollCall(self, modules):
            return None

        def readDiscrete(self, slot):
            return 0

        def writeDiscrete(self, value, slot):
            return None

        def readAnalog(self, slot, ch):
            return 0

        def writeAnalog(self, value, slot, ch):
            return None

        def readTemperature(self, slot, ch):
            return 0.0

    namespace = _run_single_scan_source(source, monkeypatch, StubBase())

    class ClosingClient:
        def __init__(self):
            self.closed = False

        def recv_into(self, buf):
            return 0

        def close(self):
            self.closed = True

    client = ClosingClient()
    clients = namespace["_mb_clients"]
    assert isinstance(clients, list)
    clients[0] = client

    namespace["service_modbus_server"]()

    assert client.closed is True
    assert clients[0] is None


def test_pyclickplc_fixture_subset_matches_generated_server(monkeypatch):
    coil = Bool("Coil", default=True)
    reg = Int("Reg", default=7)
    outputs = Block("OutBits", TagType.BOOL, 1, 16)
    yd_word = 0x1234
    for idx in range(1, 17):
        outputs.configure_slot(idx, default=bool((yd_word >> (idx - 1)) & 0x1))
    mapping = TagMap({coil: c[1], reg: ds[1], outputs: y.select(1, 16)})
    with Program(strict=False) as prog:
        pass

    source = _ctx_and_source(prog, mapping)

    class StubBase:
        def rollCall(self, modules):
            return None

        def readDiscrete(self, slot):
            return 0

        def writeDiscrete(self, value, slot):
            return None

        def readAnalog(self, slot, ch):
            return 0

        def writeAnalog(self, value, slot, ch):
            return None

        def readTemperature(self, slot, ch):
            return 0.0

    namespace = _run_single_scan_source(source, monkeypatch, StubBase())
    out_bits = namespace["_b_OutBits"]
    assert isinstance(out_bits, list)
    for idx in range(16):
        out_bits[idx] = bool((yd_word >> idx) & 0x1)
    fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "modbus_fixtures.json"
    fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))

    for fixture in fixtures:
        request = bytes.fromhex(fixture["request_hex"])
        response = namespace["_mb_handle"](request, len(request))
        assert response is not None, fixture["name"]
        assert response.hex() == fixture["response_hex"], fixture["name"]


def test_modbus_client_send_codegen_builds_expected_request_and_states(monkeypatch):
    enable = Bool("Enable", default=True)
    source = Int("Source", default=123)
    sending = Bool("Sending")
    success = Bool("Success")
    error = Bool("Error")
    ex_code = Int("ExCode")

    with Program(strict=False) as prog:
        with Rung(enable):
            send(
                target="peer",
                remote_start="DS1",
                source=source,
                sending=sending,
                success=success,
                error=error,
                exception_response=ex_code,
            )

    class StubBase:
        def rollCall(self, modules):
            return None

        def readDiscrete(self, slot):
            return 0

        def writeDiscrete(self, value, slot):
            return None

        def readAnalog(self, slot, ch):
            return 0

        def writeAnalog(self, value, slot, ch):
            return None

        def readTemperature(self, slot, ch):
            return 0.0

    created_sockets: list[object] = []

    class ScriptedSocket:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.timeout = None
            self.connected_to = None
            self.sent_packets: list[bytes] = []
            self.recv_chunks: list[bytes] = []
            created_sockets.append(self)

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, address):
            self.connected_to = address

        def send(self, payload):
            data = bytes(payload)
            self.sent_packets.append(data)
            return len(data)

        def recv_into(self, buf):
            if not self.recv_chunks:
                return 0
            chunk = self.recv_chunks.pop(0)
            buf[: len(chunk)] = chunk
            return len(chunk)

        def close(self):
            return None

    namespace = _run_single_scan_source(
        _client_source(prog),
        monkeypatch,
        StubBase(),
        socket_factory=lambda *args, **kwargs: ScriptedSocket(*args, **kwargs),
    )
    job_name = next(
        name
        for name, value in namespace.items()
        if name.startswith("_mb_client_i") and isinstance(value, dict)
    )
    job = namespace[job_name]
    assert isinstance(job, dict)
    assert job["state"] == namespace["_MB_CLIENT_CONNECTING"]
    assert job["request"] == _mbap(1, struct.pack(">BHH", 6, 0, 123), uid=17)
    assert namespace["_t_Sending"] is True
    assert namespace["_t_Success"] is False

    namespace["service_modbus_client"]()
    assert job["state"] == namespace["_MB_CLIENT_SENDING"]

    namespace["service_modbus_client"]()
    assert created_sockets
    assert created_sockets[0].sent_packets == [_mbap(1, struct.pack(">BHH", 6, 0, 123), uid=17)]
    assert job["state"] == namespace["_MB_CLIENT_WAITING"]

    created_sockets[0].recv_chunks.append(_mbap(1, struct.pack(">BHH", 6, 0, 123), uid=17))
    namespace["service_modbus_client"]()
    assert job["state"] == namespace["_MB_CLIENT_DONE"]
    assert namespace["_t_Sending"] is False
    assert namespace["_t_Success"] is True
    assert namespace["_t_Error"] is False
    assert namespace["_t_ExCode"] == 0


def test_modbus_client_connect_would_block_keeps_request_in_flight(monkeypatch):
    enable = Bool("Enable", default=True)
    source = Int("Source", default=123)
    sending = Bool("Sending")
    success = Bool("Success")
    error = Bool("Error")
    ex_code = Int("ExCode")

    with Program(strict=False) as prog:
        with Rung(enable):
            send(
                target="peer",
                remote_start="DS1",
                source=source,
                sending=sending,
                success=success,
                error=error,
                exception_response=ex_code,
            )

    class StubBase:
        def rollCall(self, modules):
            return None

        def readDiscrete(self, slot):
            return 0

        def writeDiscrete(self, value, slot):
            return None

        def readAnalog(self, slot, ch):
            return 0

        def writeAnalog(self, value, slot, ch):
            return None

        def readTemperature(self, slot, ch):
            return 0.0

    class ScriptedSocket:
        def __init__(self, *args, **kwargs):
            self.connect_calls = 0

        def settimeout(self, timeout):
            return None

        def connect(self, address):
            self.connect_calls += 1
            if self.connect_calls == 1:
                raise OSError("would block")
            return None

        def send(self, payload):
            return len(payload)

        def recv_into(self, buf):
            return 0

        def close(self):
            return None

    namespace = _run_single_scan_source(
        _client_source(prog),
        monkeypatch,
        StubBase(),
        socket_factory=lambda *args, **kwargs: ScriptedSocket(*args, **kwargs),
    )
    job_name = next(
        name
        for name, value in namespace.items()
        if name.startswith("_mb_client_i") and isinstance(value, dict)
    )
    job = namespace[job_name]
    assert isinstance(job, dict)

    namespace["service_modbus_client"]()
    assert job["state"] == namespace["_MB_CLIENT_CONNECTING"]
    assert namespace["_t_Sending"] is True
    assert namespace["_t_Error"] is False

    namespace["service_modbus_client"]()
    assert job["state"] == namespace["_MB_CLIENT_SENDING"]


def test_modbus_client_receive_codegen_applies_response(monkeypatch):
    enable = Bool("Enable", default=True)
    dest = Int("Dest")
    receiving = Bool("Receiving")
    success = Bool("Success")
    error = Bool("Error")
    ex_code = Int("ExCode")

    with Program(strict=False) as prog:
        with Rung(enable):
            receive(
                target="peer",
                remote_start="DS1",
                dest=dest,
                receiving=receiving,
                success=success,
                error=error,
                exception_response=ex_code,
            )

    class StubBase:
        def rollCall(self, modules):
            return None

        def readDiscrete(self, slot):
            return 0

        def writeDiscrete(self, value, slot):
            return None

        def readAnalog(self, slot, ch):
            return 0

        def writeAnalog(self, value, slot, ch):
            return None

        def readTemperature(self, slot, ch):
            return 0.0

    created_sockets: list[object] = []

    class ScriptedSocket:
        def __init__(self, *args, **kwargs):
            self.sent_packets: list[bytes] = []
            self.recv_chunks: list[bytes] = []
            created_sockets.append(self)

        def settimeout(self, timeout):
            return None

        def connect(self, address):
            return None

        def send(self, payload):
            data = bytes(payload)
            self.sent_packets.append(data)
            return len(data)

        def recv_into(self, buf):
            if not self.recv_chunks:
                return 0
            chunk = self.recv_chunks.pop(0)
            buf[: len(chunk)] = chunk
            return len(chunk)

        def close(self):
            return None

    namespace = _run_single_scan_source(
        _client_source(prog),
        monkeypatch,
        StubBase(),
        socket_factory=lambda *args, **kwargs: ScriptedSocket(*args, **kwargs),
    )
    job_name = next(
        name
        for name, value in namespace.items()
        if name.startswith("_mb_client_i") and isinstance(value, dict)
    )
    job = namespace[job_name]
    assert isinstance(job, dict)
    assert job["request"] == _mbap(1, struct.pack(">BHH", 3, 0, 1), uid=17)

    namespace["service_modbus_client"]()
    namespace["service_modbus_client"]()
    assert created_sockets[0].sent_packets == [_mbap(1, struct.pack(">BHH", 3, 0, 1), uid=17)]
    created_sockets[0].recv_chunks.append(_mbap(1, struct.pack(">BBH", 3, 2, 456), uid=17))
    namespace["service_modbus_client"]()

    assert job["state"] == namespace["_MB_CLIENT_DONE"]
    assert namespace["_t_Dest"] == 456
    assert namespace["_t_Receiving"] is False
    assert namespace["_t_Success"] is True
    assert namespace["_t_Error"] is False
    assert namespace["_t_ExCode"] == 0
