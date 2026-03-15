"""Tests for CircuitPython Modbus code generation."""

from __future__ import annotations

import json
import struct
from pathlib import Path

from pyclickplc.modbus import plc_to_modbus

from pyrung import Block, Bool, Char, Dint, Int, Program, Real, Rung, TagType, Word
from pyrung.circuitpy import (
    P1AM,
    ModbusClientConfig,
    ModbusServerConfig,
    generate_circuitpy,
)
from pyrung.click import (
    ModbusTarget,
    TagMap,
    c,
    ctd,
    dd,
    df,
    dh,
    ds,
    receive,
    send,
    td,
    txt,
    x,
    y,
)
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

    created_sockets: list[ScriptedSocket] = []

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

    created_sockets: list[ScriptedSocket] = []

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


def test_txt_single_odd_index_read_and_write(monkeypatch):
    """Regression: mapping a single odd TXT address without its pair must not
    crash codegen, and the read accessor should pack the low byte correctly
    with 0 in the high byte."""
    state = Char("State", default="A")
    mapping = TagMap({state: txt[1]})
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

    # TXT base=36864; register 36864 holds TXT1 (low) + TXT2 (high)
    # Only TXT1 is mapped, so high byte should be 0
    resp = namespace["_mb_handle"](_mbap(1, struct.pack(">BHH", 3, 36864, 1)), 12)
    assert resp is not None
    assert struct.unpack(">H", resp[-2:])[0] == ord("A")

    # Write: set low byte to 'Z' (0x5A), high byte ignored (only TXT1 mapped)
    resp = namespace["_mb_handle"](_mbap(2, struct.pack(">BHH", 6, 36864, ord("Z"))), 12)
    assert resp == _mbap(2, struct.pack(">BHH", 6, 36864, ord("Z")))
    assert namespace["_t_State"] == "Z"


# ---------------------------------------------------------------------------
# Boundary integration: codegen server — first & last of every bank
# ---------------------------------------------------------------------------


def test_codegen_server_first_and_last_of_every_bank(monkeypatch):
    """Map first and last address of every bank, generate Modbus server, then
    verify reads return correct defaults, writes update tags, and read-back
    returns the written value.  Addresses are computed via plc_to_modbus so
    the test validates codegen addressing matches pyclickplc."""

    # --- Tags for every bank boundary ---
    c_1 = Bool("C1", default=True)
    c_2000 = Bool("C2000", default=False)
    y_1 = Bool("Y1", default=True)
    y_816 = Bool("Y816", default=False)
    ds_1 = Int("DS1", default=123)
    ds_4500 = Int("DS4500", default=-456)
    dd_1 = Dint("DD1", default=100000)
    dd_1000 = Dint("DD1000", default=-200000)
    dh_1 = Word("DH1", default=0xBEEF)
    dh_500 = Word("DH500", default=0xDEAD)
    df_1 = Real("DF1", default=3.14)
    df_500 = Real("DF500", default=-2.71)
    txt_1 = Char("TXT1", default="A")
    txt_1000 = Char("TXT1000", default="Z")
    td_1 = Int("TD1", default=100)
    td_500 = Int("TD500", default=-200)
    ctd_1 = Dint("CTD1", default=50000)
    ctd_250 = Dint("CTD250", default=-60000)

    mapping = TagMap(
        {
            c_1: c[1],
            c_2000: c[2000],
            y_1: y[1],
            y_816: y[816],
            ds_1: ds[1],
            ds_4500: ds[4500],
            dd_1: dd[1],
            dd_1000: dd[1000],
            dh_1: dh[1],
            dh_500: dh[500],
            df_1: df[1],
            df_500: df[500],
            txt_1: txt[1],
            txt_1000: txt[1000],
            td_1: td[1],
            td_500: td[500],
            ctd_1: ctd[1],
            ctd_250: ctd[250],
        }
    )

    with Program(strict=False) as prog:
        pass
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    source = generate_circuitpy(
        prog,
        hw,
        target_scan_ms=10.0,
        modbus_server=ModbusServerConfig(ip="192.168.1.200"),
        tag_map=mapping,
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

    ns = _run_single_scan_source(source, monkeypatch, StubBase())
    handle = ns["_mb_handle"]
    _tid = [0]

    def tid():
        _tid[0] += 1
        return _tid[0]

    def _i16_raw(v):
        return struct.unpack("<H", struct.pack("<h", v))[0]

    def _i32_regs(v):
        return struct.unpack("<HH", struct.pack("<i", v))

    def _f32_regs(v):
        return struct.unpack("<HH", struct.pack("<f", v))

    # --- Coils (FC1 read, FC5 write) ---
    coil_cases = [
        ("C", 1, True, "_t_C1"),
        ("C", 2000, False, "_t_C2000"),
        ("Y", 1, True, "_t_Y1"),
        ("Y", 816, False, "_t_Y816"),
    ]
    for bank, index, default, sym in coil_cases:
        addr = plc_to_modbus(bank, index)[0]
        label = f"{bank}[{index}]"
        # Read default
        t = tid()
        resp = handle(_mbap(t, struct.pack(">BHH", 1, addr, 1)), 12)
        assert resp is not None, f"{label} read"
        assert bool(resp[-1] & 0x01) == default, f"{label} default"
        # Write opposite
        flipped = not default
        t = tid()
        req = _mbap(t, struct.pack(">BHH", 5, addr, 0xFF00 if flipped else 0x0000))
        resp = handle(req, len(req))
        assert resp == req, f"{label} write echo"
        assert ns[sym] is flipped, f"{label} tag"
        # Read back
        t = tid()
        resp = handle(_mbap(t, struct.pack(">BHH", 1, addr, 1)), 12)
        assert bool(resp[-1] & 0x01) == flipped, f"{label} read-back"

    # --- Signed 16-bit registers (DS, TD): FC3 read, FC6 write ---
    sint16_cases = [
        ("DS", 1, 123, "_t_DS1", 999),
        ("DS", 4500, -456, "_t_DS4500", -1000),
        ("TD", 1, 100, "_t_TD1", 300),
        ("TD", 500, -200, "_t_TD500", -500),
    ]
    for bank, index, default, sym, write_val in sint16_cases:
        addr = plc_to_modbus(bank, index)[0]
        label = f"{bank}[{index}]"
        t = tid()
        resp = handle(_mbap(t, struct.pack(">BHH", 3, addr, 1)), 12)
        assert resp is not None, f"{label} read"
        assert struct.unpack(">H", resp[-2:])[0] == _i16_raw(default), f"{label} default"
        t = tid()
        raw = _i16_raw(write_val)
        req = _mbap(t, struct.pack(">BHH", 6, addr, raw))
        resp = handle(req, len(req))
        assert resp == req, f"{label} write echo"
        assert ns[sym] == write_val, f"{label} tag"

    # --- Unsigned 16-bit registers (DH): FC3 read, FC6 write ---
    uint16_cases = [
        ("DH", 1, 0xBEEF, "_t_DH1", 0x1234),
        ("DH", 500, 0xDEAD, "_t_DH500", 0xCAFE),
    ]
    for bank, index, default, sym, write_val in uint16_cases:
        addr = plc_to_modbus(bank, index)[0]
        label = f"{bank}[{index}]"
        t = tid()
        resp = handle(_mbap(t, struct.pack(">BHH", 3, addr, 1)), 12)
        assert struct.unpack(">H", resp[-2:])[0] == default, f"{label} default"
        t = tid()
        req = _mbap(t, struct.pack(">BHH", 6, addr, write_val))
        resp = handle(req, len(req))
        assert resp == req, f"{label} write echo"
        assert ns[sym] == write_val, f"{label} tag"

    # --- 32-bit signed registers (DD, CTD): FC3 read 2 regs, FC16 write ---
    dint_cases = [
        ("DD", 1, 100000, "_t_DD1", 999999),
        ("DD", 1000, -200000, "_t_DD1000", -888888),
        ("CTD", 1, 50000, "_t_CTD1", 123456),
        ("CTD", 250, -60000, "_t_CTD250", -654321),
    ]
    for bank, index, default, sym, write_val in dint_cases:
        addr = plc_to_modbus(bank, index)[0]
        label = f"{bank}[{index}]"
        lo, hi = _i32_regs(default)
        t = tid()
        resp = handle(_mbap(t, struct.pack(">BHH", 3, addr, 2)), 12)
        assert resp is not None, f"{label} read"
        assert struct.unpack(">HH", resp[-4:]) == (lo, hi), f"{label} default"
        wlo, whi = _i32_regs(write_val)
        t = tid()
        pdu = struct.pack(">BHHB", 16, addr, 2, 4) + struct.pack(">HH", wlo, whi)
        req = _mbap(t, pdu)
        resp = handle(req, len(req))
        assert resp == struct.pack(">HHHBBHH", t, 0, 6, 1, 16, addr, 2), f"{label} FC16 resp"
        assert ns[sym] == write_val, f"{label} tag"

    # --- Float32 registers (DF): FC3 read 2 regs, FC16 write ---
    float_cases = [
        ("DF", 1, 3.14, "_t_DF1", 1.23),
        ("DF", 500, -2.71, "_t_DF500", -9.99),
    ]
    for bank, index, default, sym, write_val in float_cases:
        addr = plc_to_modbus(bank, index)[0]
        label = f"{bank}[{index}]"
        lo, hi = _f32_regs(default)
        t = tid()
        resp = handle(_mbap(t, struct.pack(">BHH", 3, addr, 2)), 12)
        assert struct.unpack(">HH", resp[-4:]) == (lo, hi), f"{label} default"
        wlo, whi = _f32_regs(write_val)
        t = tid()
        pdu = struct.pack(">BHHB", 16, addr, 2, 4) + struct.pack(">HH", wlo, whi)
        req = _mbap(t, pdu)
        resp = handle(req, len(req))
        assert resp == struct.pack(">HHHBBHH", t, 0, 6, 1, 16, addr, 2), f"{label} FC16 resp"
        expected = struct.unpack("<f", struct.pack("<f", write_val))[0]
        assert abs(ns[sym] - expected) < 1e-6, f"{label} tag"

    # --- TXT (char, packed pairs) ---
    # TXT[1] (odd, low byte, no TXT2 pair)
    txt1_addr = plc_to_modbus("TXT", 1)[0]
    t = tid()
    resp = handle(_mbap(t, struct.pack(">BHH", 3, txt1_addr, 1)), 12)
    assert struct.unpack(">H", resp[-2:])[0] == ord("A"), "TXT[1] default"
    t = tid()
    req = _mbap(t, struct.pack(">BHH", 6, txt1_addr, ord("W")))
    resp = handle(req, len(req))
    assert resp == req, "TXT[1] write echo"
    assert ns["_t_TXT1"] == "W", "TXT[1] tag"

    # TXT[1000] (even, high byte, no TXT999 pair)
    txt1000_addr = plc_to_modbus("TXT", 1000)[0]
    t = tid()
    resp = handle(_mbap(t, struct.pack(">BHH", 3, txt1000_addr, 1)), 12)
    assert struct.unpack(">H", resp[-2:])[0] == (ord("Z") << 8), "TXT[1000] default"
    t = tid()
    req = _mbap(t, struct.pack(">BHH", 6, txt1000_addr, ord("W") << 8))
    resp = handle(req, len(req))
    assert resp == req, "TXT[1000] write echo"
    assert ns["_t_TXT1000"] == "W", "TXT[1000] tag"


# ---------------------------------------------------------------------------
# Boundary integration: codegen client receive — first & last of every bank
# ---------------------------------------------------------------------------


def _run_client_job(monkeypatch, prog):
    """Run a single-instruction client program.  Returns (namespace, sockets)."""
    sockets: list[object] = []

    class _Sock:
        def __init__(self, *args, **kwargs):
            self.sent_packets: list[bytes] = []
            self.recv_chunks: list[bytes] = []
            sockets.append(self)

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

    class _Stub:
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

    ns = _run_single_scan_source(
        _client_source(prog),
        monkeypatch,
        _Stub(),
        socket_factory=lambda *a, **kw: _Sock(*a, **kw),
    )
    return ns, sockets


def test_codegen_client_receive_first_and_last_of_every_bank(monkeypatch):
    """For each bank boundary, generate a receive() instruction, feed a valid
    Modbus response, and verify the dest tag is populated correctly."""

    def _i16_raw(v):
        return struct.unpack("<H", struct.pack("<h", v))[0]

    def _i32_regs(v):
        return struct.unpack("<HH", struct.pack("<i", v))

    def _f32_regs(v):
        return struct.unpack("<HH", struct.pack("<f", v))

    def _f32(v):
        return struct.unpack("<f", struct.pack("<f", v))[0]

    # (remote_start, tag_cls, response_pdu, expected_value)
    cases = [
        # Coils — FC1 response: fc(1) + byte_count(1) + data(1)
        ("C1", Bool, struct.pack(">BBB", 1, 1, 0x01), True),
        ("C2000", Bool, struct.pack(">BBB", 1, 1, 0x00), False),
        ("Y1", Bool, struct.pack(">BBB", 1, 1, 0x01), True),
        ("Y816", Bool, struct.pack(">BBB", 1, 1, 0x00), False),
        # Signed 16-bit — FC3 response: fc(1) + byte_count(1) + reg(2)
        ("DS1", Int, struct.pack(">BBH", 3, 2, 123), 123),
        ("DS4500", Int, struct.pack(">BBH", 3, 2, _i16_raw(-456)), -456),
        ("TD1", Int, struct.pack(">BBH", 3, 2, 100), 100),
        ("TD500", Int, struct.pack(">BBH", 3, 2, _i16_raw(-200)), -200),
        # Unsigned 16-bit
        ("DH1", Word, struct.pack(">BBH", 3, 2, 0xBEEF), 0xBEEF),
        ("DH500", Word, struct.pack(">BBH", 3, 2, 0xDEAD), 0xDEAD),
        # TXT (odd/even single-char)
        ("TXT1", Char, struct.pack(">BBH", 3, 2, ord("A")), "A"),
        ("TXT999", Char, struct.pack(">BBH", 3, 2, ord("Z")), "Z"),
        ("TXT1000", Char, struct.pack(">BBH", 3, 2, ord("Z") << 8), "Z"),
        # 32-bit signed — FC3 response with 2 registers
        ("DD1", Dint, struct.pack(">BB", 3, 4) + struct.pack(">HH", *_i32_regs(100000)), 100000),
        (
            "DD1000",
            Dint,
            struct.pack(">BB", 3, 4) + struct.pack(">HH", *_i32_regs(-200000)),
            -200000,
        ),
        ("CTD1", Dint, struct.pack(">BB", 3, 4) + struct.pack(">HH", *_i32_regs(50000)), 50000),
        ("CTD250", Dint, struct.pack(">BB", 3, 4) + struct.pack(">HH", *_i32_regs(-60000)), -60000),
        # Float32 — FC3 response with 2 registers
        ("DF1", Real, struct.pack(">BB", 3, 4) + struct.pack(">HH", *_f32_regs(3.14)), _f32(3.14)),
        (
            "DF500",
            Real,
            struct.pack(">BB", 3, 4) + struct.pack(">HH", *_f32_regs(-2.71)),
            _f32(-2.71),
        ),
    ]

    for remote_start, tag_cls, response_pdu, expected in cases:
        label = f"receive({remote_start})"
        enable = Bool("Enable", default=True)
        dest = tag_cls("Dest")
        receiving = Bool("Receiving")
        success = Bool("Success")
        error = Bool("Error")
        ex_code = Int("ExCode")

        with Program(strict=False) as prog:
            with Rung(enable):
                receive(
                    target="peer",
                    remote_start=remote_start,
                    dest=dest,
                    receiving=receiving,
                    success=success,
                    error=error,
                    exception_response=ex_code,
                )

        ns, sockets = _run_client_job(monkeypatch, prog)

        # Progress: CONNECTING → SENDING → WAITING
        ns["service_modbus_client"]()
        ns["service_modbus_client"]()
        assert sockets, f"{label} no socket created"

        # Feed response (TID=1, uid=17 matches the "peer" target)
        sockets[-1].recv_chunks.append(_mbap(1, response_pdu, uid=17))
        ns["service_modbus_client"]()

        assert ns["_t_Success"] is True, f"{label} success"
        actual = ns["_t_Dest"]
        if isinstance(expected, float):
            assert abs(actual - expected) < 1e-6, f"{label}: {actual}"
        else:
            assert actual == expected, f"{label}: {actual}"


# ---------------------------------------------------------------------------
# Boundary integration: codegen client send — first & last of every bank
# ---------------------------------------------------------------------------


def test_codegen_client_send_first_and_last_of_every_bank(monkeypatch):
    """For each bank boundary, generate a send() instruction and verify the
    request PDU encodes the value at the correct Modbus address."""

    def _i16_raw(v):
        return struct.unpack("<H", struct.pack("<h", v))[0]

    def _i32_regs(v):
        return struct.unpack("<HH", struct.pack("<i", v))

    def _f32_regs(v):
        return struct.unpack("<HH", struct.pack("<f", v))

    # (remote_start, tag_cls, default, expected_request_pdu)
    # Coil sends use FC5: struct.pack(">BHH", 5, addr, 0xFF00|0x0000)
    # Single-register sends use FC6: struct.pack(">BHH", 6, addr, raw)
    # Double-register sends use FC16: struct.pack(">BHHB", 16, addr, 2, 4) + regs
    cases = [
        # Coils — FC5
        ("C1", Bool, True, struct.pack(">BHH", 5, plc_to_modbus("C", 1)[0], 0xFF00)),
        ("C2000", Bool, False, struct.pack(">BHH", 5, plc_to_modbus("C", 2000)[0], 0x0000)),
        ("Y1", Bool, True, struct.pack(">BHH", 5, plc_to_modbus("Y", 1)[0], 0xFF00)),
        ("Y816", Bool, False, struct.pack(">BHH", 5, plc_to_modbus("Y", 816)[0], 0x0000)),
        # Signed 16-bit — FC6
        ("DS1", Int, 123, struct.pack(">BHH", 6, plc_to_modbus("DS", 1)[0], _i16_raw(123))),
        ("DS4500", Int, -456, struct.pack(">BHH", 6, plc_to_modbus("DS", 4500)[0], _i16_raw(-456))),
        ("TD1", Int, 100, struct.pack(">BHH", 6, plc_to_modbus("TD", 1)[0], _i16_raw(100))),
        ("TD500", Int, -200, struct.pack(">BHH", 6, plc_to_modbus("TD", 500)[0], _i16_raw(-200))),
        # Unsigned 16-bit — FC6
        ("DH1", Word, 0xBEEF, struct.pack(">BHH", 6, plc_to_modbus("DH", 1)[0], 0xBEEF)),
        ("DH500", Word, 0xDEAD, struct.pack(">BHH", 6, plc_to_modbus("DH", 500)[0], 0xDEAD)),
        # TXT (odd only) — FC6
        ("TXT1", Char, "A", struct.pack(">BHH", 6, plc_to_modbus("TXT", 1)[0], ord("A"))),
        ("TXT999", Char, "Z", struct.pack(">BHH", 6, plc_to_modbus("TXT", 999)[0], ord("Z"))),
        # 32-bit signed — FC16
        (
            "DD1",
            Dint,
            100000,
            struct.pack(">BHHB", 16, plc_to_modbus("DD", 1)[0], 2, 4)
            + struct.pack(">HH", *_i32_regs(100000)),
        ),
        (
            "DD1000",
            Dint,
            -200000,
            struct.pack(">BHHB", 16, plc_to_modbus("DD", 1000)[0], 2, 4)
            + struct.pack(">HH", *_i32_regs(-200000)),
        ),
        (
            "CTD1",
            Dint,
            50000,
            struct.pack(">BHHB", 16, plc_to_modbus("CTD", 1)[0], 2, 4)
            + struct.pack(">HH", *_i32_regs(50000)),
        ),
        (
            "CTD250",
            Dint,
            -60000,
            struct.pack(">BHHB", 16, plc_to_modbus("CTD", 250)[0], 2, 4)
            + struct.pack(">HH", *_i32_regs(-60000)),
        ),
        # Float32 — FC16
        (
            "DF1",
            Real,
            3.14,
            struct.pack(">BHHB", 16, plc_to_modbus("DF", 1)[0], 2, 4)
            + struct.pack(">HH", *_f32_regs(3.14)),
        ),
        (
            "DF500",
            Real,
            -2.71,
            struct.pack(">BHHB", 16, plc_to_modbus("DF", 500)[0], 2, 4)
            + struct.pack(">HH", *_f32_regs(-2.71)),
        ),
    ]

    for remote_start, tag_cls, default, expected_pdu in cases:
        label = f"send({remote_start})"
        enable = Bool("Enable", default=True)
        source = tag_cls("Source", default=default)
        sending = Bool("Sending")
        success = Bool("Success")
        error = Bool("Error")
        ex_code = Int("ExCode")

        with Program(strict=False) as prog:
            with Rung(enable):
                send(
                    target="peer",
                    remote_start=remote_start,
                    source=source,
                    sending=sending,
                    success=success,
                    error=error,
                    exception_response=ex_code,
                )

        ns, sockets = _run_client_job(monkeypatch, prog)

        # Find the job and verify the request PDU
        job = next(v for k, v in ns.items() if k.startswith("_mb_client_i") and isinstance(v, dict))
        request = job["request"]
        # Request = MBAP(7) + PDU; MBAP uses uid=17 (peer device_id)
        assert request == _mbap(1, expected_pdu, uid=17), f"{label} request"

        # Feed an echo/ack response so the job completes
        ns["service_modbus_client"]()  # connect
        ns["service_modbus_client"]()  # send
        assert sockets, f"{label} no socket created"
        sockets[-1].recv_chunks.append(_mbap(1, expected_pdu, uid=17))
        ns["service_modbus_client"]()  # receive

        assert ns["_t_Success"] is True, f"{label} success"
