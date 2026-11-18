"""Tests for raw Modbus (non-Click) send/receive support."""

from __future__ import annotations

import struct
from concurrent.futures import Future

import pytest

from pyrung.core import PLC, Block, Bool, Dint, Int, Program, Rung, TagType
from pyrung.core.instruction.send_receive import (
    ModbusAddress,
    ModbusRtuTarget,
    ModbusTcpTarget,
    RegisterType,
    WordOrder,
)
from pyrung.core.instruction.send_receive._core import _RequestResult
from pyrung.core.instruction.send_receive.helpers import (
    _calculate_register_count,
    _pack_values_to_registers,
    _preview_operand_tag_types,
    _unpack_registers_to_values,
)
from pyrung.core.tag import Tag

# ---------------------------------------------------------------------------
# Type construction
# ---------------------------------------------------------------------------


class TestModbusAddress:
    def test_int_address(self):
        addr = ModbusAddress(100)
        assert addr.address == 100
        assert addr.register_type == RegisterType.HOLDING

    def test_hex_h_suffix(self):
        addr = ModbusAddress("64h")
        assert addr.address == 0x64

    def test_hex_h_suffix_upper(self):
        addr = ModbusAddress("FFFEh")
        assert addr.address == 0xFFFE

    def test_hex_zero_h(self):
        addr = ModbusAddress("0h")
        assert addr.address == 0

    def test_984_holding(self):
        addr = ModbusAddress(400001)
        assert addr.address == 0
        assert addr.register_type == RegisterType.HOLDING

    def test_984_holding_high(self):
        addr = ModbusAddress(465535)
        assert addr.address == 65534
        assert addr.register_type == RegisterType.HOLDING

    def test_984_input(self):
        addr = ModbusAddress(300001)
        assert addr.address == 0
        assert addr.register_type == RegisterType.INPUT

    def test_984_discrete_input(self):
        addr = ModbusAddress(100001)
        assert addr.address == 0
        assert addr.register_type == RegisterType.DISCRETE_INPUT

    def test_984_register_type_conflict(self):
        with pytest.raises(ValueError, match="is a HOLDING address, but you passed"):
            ModbusAddress(400001, RegisterType.INPUT)

    def test_max_valid(self):
        addr = ModbusAddress(0xFFFE)
        assert addr.address == 0xFFFE

    def test_too_large(self):
        with pytest.raises(ValueError, match="0..0xFFFE"):
            ModbusAddress(0xFFFF)

    def test_negative(self):
        with pytest.raises(ValueError, match="0..0xFFFE"):
            ModbusAddress(-1)

    def test_invalid_hex_string(self):
        with pytest.raises(ValueError, match="valid hex"):
            ModbusAddress("xyz")

    def test_register_type(self):
        addr = ModbusAddress(0, RegisterType.INPUT)
        assert addr.register_type == RegisterType.INPUT

    def test_bad_register_type(self):
        with pytest.raises(TypeError, match="RegisterType"):
            ModbusAddress(0, "holding")

    def test_frozen(self):
        addr = ModbusAddress(100)
        with pytest.raises(AttributeError):
            addr.address = 200  # ty: ignore[invalid-assignment]

    def test_notation_records_the_written_form_without_changing_identity(self):
        assert ModbusAddress("64h") == ModbusAddress(400101) == ModbusAddress(100)
        assert hash(ModbusAddress("64h")) == hash(ModbusAddress(100))
        assert ModbusAddress("64h").notation == "hex"
        assert ModbusAddress(400101).notation == "984"
        assert ModbusAddress(100).notation == "raw"


class TestModbusRtuTarget:
    def test_defaults(self):
        t = ModbusRtuTarget("meter", "/dev/ttyUSB0")
        assert t.device_id == 1
        assert t.baudrate == 9600
        assert t.bytesize == 8
        assert t.parity == "N"
        assert t.stopbits == 1
        assert t.timeout_ms == 1000

    def test_custom_fields(self):
        t = ModbusRtuTarget(
            "meter",
            "/dev/ttyUSB0",
            device_id=3,
            baudrate=19200,
            parity="E",
        )
        assert t.device_id == 3
        assert t.baudrate == 19200
        assert t.parity == "E"

    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="name must not be empty"):
            ModbusRtuTarget("", "/dev/ttyUSB0")

    def test_empty_serial_port_allowed(self):
        """Empty serial_port is valid (codegen-only / non-simulation targets)."""
        t = ModbusRtuTarget("meter")
        assert t.serial_port == ""

    def test_bad_bytesize(self):
        with pytest.raises(ValueError, match="bytesize"):
            ModbusRtuTarget("meter", "/dev/ttyUSB0", bytesize=9)

    def test_bad_parity(self):
        with pytest.raises(ValueError, match="parity"):
            ModbusRtuTarget("meter", "/dev/ttyUSB0", parity="X")

    def test_bad_stopbits(self):
        with pytest.raises(ValueError, match="stopbits"):
            ModbusRtuTarget("meter", "/dev/ttyUSB0", stopbits=3)

    def test_bad_baudrate(self):
        with pytest.raises(ValueError, match="baudrate"):
            ModbusRtuTarget("meter", "/dev/ttyUSB0", baudrate=0)


class TestCLICKV4TargetForms:
    """Covers CLICK v4's port and Slave ID forms.

    Where CLICK or laddercodec refuses a form, pyrung refuses it too.
    """

    @pytest.mark.parametrize(
        "make",
        [
            lambda device_id: ModbusTcpTarget("peer", "10.0.0.2", device_id=device_id),
            lambda device_id: ModbusRtuTarget("drive", device_id=device_id),
        ],
        ids=["tcp", "rtu"],
    )
    def test_targets_with_a_tag_device_id_compare_by_tag_type_and_name(self, make):
        same = make(Int("Id"))
        assert same == make(Int("Id"))
        assert hash(same) == hash(make(Int("Id")))
        assert same != make(Int("Other"))
        assert same != make(1)
        assert same in [make(1), make(Int("Id"))]
        assert len({same, make(Int("Id")), make(1), make(1)}) == 2

    def test_tcp_com_port_defaults_to_ethernet_and_takes_wlan(self):
        assert ModbusTcpTarget("peer", "10.0.0.2").com_port == "ethernet"
        assert ModbusTcpTarget("peer", "10.0.0.2", com_port="wlan").com_port == "wlan"

    def test_tcp_com_port_refuses_a_serial_port(self):
        with pytest.raises(ValueError, match="com_port must be one of"):
            ModbusTcpTarget("peer", "10.0.0.2", com_port="cpu2")

    def test_rtu_takes_cpu3(self):
        assert ModbusRtuTarget("vfd", com_port="cpu3").com_port == "cpu3"

    @pytest.mark.parametrize("target", [ModbusTcpTarget, ModbusRtuTarget])
    def test_device_id_takes_an_int_tag(self, target):
        slave = Int("Slave")
        args = ("peer", "10.0.0.2") if target is ModbusTcpTarget else ("vfd",)
        assert target(*args, device_id=slave).device_id is slave

    @pytest.mark.parametrize("make_tag", [Dint, Bool, lambda name: Tag(name, TagType.WORD)])
    @pytest.mark.parametrize("target", [ModbusTcpTarget, ModbusRtuTarget])
    def test_device_id_refuses_a_dd_dh_or_c_tag(self, target, make_tag):
        args = ("peer", "10.0.0.2") if target is ModbusTcpTarget else ("vfd",)
        with pytest.raises(TypeError, match=r"device_id must be an int 0\.\.\d+ or a DS tag"):
            target(*args, device_id=make_tag("Slave"))

    def test_device_id_refuses_a_bool_value(self):
        with pytest.raises(TypeError, match="device_id must be an int"):
            ModbusTcpTarget("peer", "10.0.0.2", device_id=True)

    def test_tcp_device_id_runs_to_255(self):
        assert ModbusTcpTarget("peer", "10.0.0.2", device_id=255).device_id == 255
        with pytest.raises(ValueError, match=r"0\.\.255"):
            ModbusTcpTarget("peer", "10.0.0.2", device_id=256)

    def test_rtu_device_id_runs_to_247(self):
        assert ModbusRtuTarget("vfd", device_id=247).device_id == 247
        with pytest.raises(ValueError, match=r"0\.\.247"):
            ModbusRtuTarget("vfd", device_id=248)

    def test_rtu_receive_refuses_device_id_0(self):
        from pyrung.core.instruction.send_receive import receive, send

        target = ModbusRtuTarget("vfd", device_id=0)
        with Program():
            with Rung():
                send(target=target, remote_start=ModbusAddress(400001), source=Int("Out"))
                with pytest.raises(
                    ValueError, match="cannot read from device_id 0 on an RTU target"
                ):
                    receive(target=target, remote_start=ModbusAddress(400001), dest=Int("In"))


# ---------------------------------------------------------------------------
# Register count calculation
# ---------------------------------------------------------------------------


class TestRegisterCount:
    def test_single_int(self):
        assert _calculate_register_count([TagType.INT], RegisterType.HOLDING) == 1

    def test_single_dint(self):
        assert _calculate_register_count([TagType.DINT], RegisterType.HOLDING) == 2

    def test_single_real(self):
        assert _calculate_register_count([TagType.REAL], RegisterType.HOLDING) == 2

    def test_multiple_int(self):
        assert _calculate_register_count([TagType.INT] * 3, RegisterType.HOLDING) == 3

    def test_mixed(self):
        types = [TagType.INT, TagType.DINT, TagType.REAL]
        assert _calculate_register_count(types, RegisterType.HOLDING) == 5

    def test_coil_ignores_type_width(self):
        assert _calculate_register_count([TagType.DINT], RegisterType.COIL) == 1
        assert _calculate_register_count([TagType.INT] * 3, RegisterType.COIL) == 3

    def test_preview_single_tag(self):
        tag = Int("X")
        assert _preview_operand_tag_types(tag, 1) == [TagType.INT]

    def test_preview_block_range(self):
        b = Block("B", TagType.INT, 1, 4)
        br = b.select(1, 3)
        assert _preview_operand_tag_types(br, 3) == [TagType.INT] * 3


# ---------------------------------------------------------------------------
# Value packing / unpacking
# ---------------------------------------------------------------------------


class TestPacking:
    def test_pack_int_values(self):
        tags = [Tag("a", TagType.INT), Tag("b", TagType.INT)]
        result = _pack_values_to_registers([10, 20], tags, WordOrder.HIGH_LOW, RegisterType.HOLDING)
        assert result == [10, 20]

    def test_pack_dint_high_low(self):
        tags = [Tag("a", TagType.DINT)]
        val = 0x00010002  # 65538
        result = _pack_values_to_registers([val], tags, WordOrder.HIGH_LOW, RegisterType.HOLDING)
        hi, lo = struct.unpack(">HH", struct.pack(">i", val))
        assert result == [hi, lo]
        assert result == [1, 2]

    def test_pack_dint_low_high(self):
        tags = [Tag("a", TagType.DINT)]
        val = 0x00010002
        result = _pack_values_to_registers([val], tags, WordOrder.LOW_HIGH, RegisterType.HOLDING)
        hi, lo = struct.unpack(">HH", struct.pack(">i", val))
        assert result == [lo, hi]

    def test_pack_real_high_low(self):
        tags = [Tag("a", TagType.REAL)]
        val = 3.14
        result = _pack_values_to_registers([val], tags, WordOrder.HIGH_LOW, RegisterType.HOLDING)
        hi, lo = struct.unpack(">HH", struct.pack(">f", val))
        assert result == [hi, lo]

    def test_pack_real_low_high(self):
        tags = [Tag("a", TagType.REAL)]
        val = 3.14
        result = _pack_values_to_registers([val], tags, WordOrder.LOW_HIGH, RegisterType.HOLDING)
        hi, lo = struct.unpack(">HH", struct.pack(">f", val))
        assert result == [lo, hi]

    @pytest.mark.parametrize("bits", [0x7FC00000, 0xFFC00000, 0x7F800001])
    @pytest.mark.parametrize("order", [WordOrder.HIGH_LOW, WordOrder.LOW_HIGH])
    def test_pack_any_nan_as_its_own_bits(self, bits, order):
        tags = [Tag("a", TagType.REAL)]
        nan = struct.unpack(">f", struct.pack(">I", bits))[0]
        result = _pack_values_to_registers([nan], tags, order, RegisterType.HOLDING)
        hi, lo = struct.unpack(">HH", struct.pack(">f", nan))
        assert result == ([hi, lo] if order == WordOrder.HIGH_LOW else [lo, hi])

    def test_pack_coils(self):
        tags = [Tag("a", TagType.BOOL), Tag("b", TagType.BOOL)]
        result = _pack_values_to_registers(
            [True, False], tags, WordOrder.HIGH_LOW, RegisterType.COIL
        )
        assert result == [True, False]

    def test_roundtrip_int(self):
        tags = [Tag("a", TagType.INT), Tag("b", TagType.INT)]
        values = [42, 99]
        packed = _pack_values_to_registers(values, tags, WordOrder.HIGH_LOW, RegisterType.HOLDING)
        unpacked = _unpack_registers_to_values(
            packed, tags, WordOrder.HIGH_LOW, RegisterType.HOLDING
        )
        assert unpacked == (42, 99)

    def test_roundtrip_dint_high_low(self):
        tags = [Tag("a", TagType.DINT)]
        values = [123456]
        packed = _pack_values_to_registers(values, tags, WordOrder.HIGH_LOW, RegisterType.HOLDING)
        unpacked = _unpack_registers_to_values(
            packed, tags, WordOrder.HIGH_LOW, RegisterType.HOLDING
        )
        assert unpacked == (123456,)

    def test_roundtrip_dint_low_high(self):
        tags = [Tag("a", TagType.DINT)]
        values = [-12345]
        packed = _pack_values_to_registers(values, tags, WordOrder.LOW_HIGH, RegisterType.HOLDING)
        unpacked = _unpack_registers_to_values(
            packed, tags, WordOrder.LOW_HIGH, RegisterType.HOLDING
        )
        assert unpacked == (-12345,)

    def test_roundtrip_real_high_low(self):
        tags = [Tag("a", TagType.REAL)]
        values = [3.14]
        packed = _pack_values_to_registers(values, tags, WordOrder.HIGH_LOW, RegisterType.HOLDING)
        unpacked = _unpack_registers_to_values(
            packed, tags, WordOrder.HIGH_LOW, RegisterType.HOLDING
        )
        assert abs(unpacked[0] - 3.14) < 1e-5

    def test_roundtrip_real_low_high(self):
        tags = [Tag("a", TagType.REAL)]
        values = [-99.5]
        packed = _pack_values_to_registers(values, tags, WordOrder.LOW_HIGH, RegisterType.HOLDING)
        unpacked = _unpack_registers_to_values(
            packed, tags, WordOrder.LOW_HIGH, RegisterType.HOLDING
        )
        assert abs(unpacked[0] - (-99.5)) < 1e-5

    def test_roundtrip_coil(self):
        tags = [Tag("a", TagType.BOOL), Tag("b", TagType.BOOL)]
        packed = _pack_values_to_registers(
            [True, False], tags, WordOrder.HIGH_LOW, RegisterType.COIL
        )
        unpacked = _unpack_registers_to_values(packed, tags, WordOrder.HIGH_LOW, RegisterType.COIL)
        assert unpacked == (True, False)

    def test_roundtrip_mixed(self):
        tags = [Tag("a", TagType.INT), Tag("b", TagType.DINT), Tag("c", TagType.REAL)]
        values = [100, 200000, 1.5]
        packed = _pack_values_to_registers(values, tags, WordOrder.HIGH_LOW, RegisterType.HOLDING)
        assert len(packed) == 5  # 1 + 2 + 2
        unpacked = _unpack_registers_to_values(
            packed, tags, WordOrder.HIGH_LOW, RegisterType.HOLDING
        )
        assert unpacked[0] == 100
        assert unpacked[1] == 200000
        assert abs(unpacked[2] - 1.5) < 1e-5


# ---------------------------------------------------------------------------
# DSL construction validation
# ---------------------------------------------------------------------------

_TCP_TARGET = ModbusTcpTarget("vfd", "192.168.1.20")
_RTU_TARGET = ModbusRtuTarget("meter", "/dev/ttyUSB0", device_id=3)


class TestDslConstruction:
    def test_raw_tcp_send(self, monkeypatch: pytest.MonkeyPatch):
        from pyrung.core.instruction.send_receive import _core as mod

        submissions: list[dict[str, object]] = []

        def fake_submit(**kwargs: object) -> Future[_RequestResult]:
            fut: Future[_RequestResult] = Future()
            submissions.append(dict(kwargs))
            return fut

        monkeypatch.setattr(mod, "_submit_raw_send_request", fake_submit)

        Source = Int("Source")
        Enable = Bool("Enable")
        Sending = Bool("Sending")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")

        with Program() as logic:
            with Rung(Enable):
                mod.send(
                    target=_TCP_TARGET,
                    remote_start=ModbusAddress(0x100),
                    source=Source,
                    sending=Sending,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )

        runner = PLC(logic=logic)
        runner.patch({"Enable": True, "Source": 42})
        runner.step()

        assert len(submissions) == 1
        assert submissions[0]["address"] == 0x100
        assert submissions[0]["register_type"] == RegisterType.HOLDING
        assert runner.current_state.tags["Sending"] is True

    def test_rtu_with_click_address_accepted(self):
        """RTU targets may use Click address strings (Click-to-Click serial)."""
        from pyrung.core.instruction.send_receive import _core as mod

        Source = Int("Source")
        Enable = Bool("Enable")
        Sending = Bool("Sending")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")

        with Program() as logic:
            with Rung(Enable):
                mod.send(
                    target=_RTU_TARGET,
                    remote_start="DS1",
                    source=Source,
                    sending=Sending,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )

        # Should create the instruction without error
        assert len(logic.rungs) == 1

    def test_send_to_input_register_raises(self):
        from pyrung.core.instruction.send_receive import _core as mod

        Source = Int("Source")
        Enable = Bool("Enable")
        Sending = Bool("Sending")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")

        with pytest.raises(ValueError, match="cannot write to"):
            with Program():
                with Rung(Enable):
                    mod.send(
                        target=_TCP_TARGET,
                        remote_start=ModbusAddress(0x100, RegisterType.INPUT),
                        source=Source,
                        sending=Sending,
                        success=Success,
                        error=Error,
                        exception_response=ExCode,
                    )

    def test_send_to_discrete_input_raises(self):
        from pyrung.core.instruction.send_receive import _core as mod

        Source = Int("Source")
        Enable = Bool("Enable")
        Sending = Bool("Sending")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")

        with pytest.raises(ValueError, match="cannot write to"):
            with Program():
                with Rung(Enable):
                    mod.send(
                        target=_TCP_TARGET,
                        remote_start=ModbusAddress(0x100, RegisterType.DISCRETE_INPUT),
                        source=Source,
                        sending=Sending,
                        success=Success,
                        error=Error,
                        exception_response=ExCode,
                    )

    def test_receive_from_input_register_ok(self, monkeypatch: pytest.MonkeyPatch):
        from pyrung.core.instruction.send_receive import _core as mod

        def fake_submit(**kw: object) -> Future[_RequestResult]:
            return Future()

        monkeypatch.setattr(mod, "_submit_raw_receive_request", fake_submit)

        Dest = Int("Dest")
        Enable = Bool("Enable")
        Receiving = Bool("Receiving")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")

        with Program() as logic:
            with Rung(Enable):
                mod.receive(
                    target=_TCP_TARGET,
                    remote_start=ModbusAddress(0x100, RegisterType.INPUT),
                    dest=Dest,
                    receiving=Receiving,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )

        runner = PLC(logic=logic)
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["Receiving"] is True

    def test_inert_mode_with_string_target(self):
        from pyrung.core.instruction.send_receive import _core as mod

        Dest = Int("Dest")
        Enable = Bool("Enable")
        Receiving = Bool("Receiving")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")

        with Program() as logic:
            with Rung(Enable):
                mod.receive(
                    target="my_plc",
                    remote_start=ModbusAddress(0x100),
                    dest=Dest,
                    receiving=Receiving,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )

        # Inert mode: no I/O, no status changes
        runner = PLC(logic=logic)
        runner.patch({"Enable": True})
        runner.step()
        # Receiving should not be set since instruction is inert
        assert runner.current_state.tags.get("Receiving", False) is False

    def test_dint_register_count(self, monkeypatch: pytest.MonkeyPatch):
        """DINT source should produce 2 registers in addresses tuple."""
        from pyrung.core.instruction.send_receive import _core as mod

        submissions: list[dict[str, object]] = []

        def fake_submit(**kw: object) -> Future[_RequestResult]:
            submissions.append(dict(kw))
            return Future()

        monkeypatch.setattr(mod, "_submit_raw_send_request", fake_submit)

        Source = Dint("Source")
        Enable = Bool("Enable")
        Sending = Bool("Sending")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")

        with Program() as logic:
            with Rung(Enable):
                mod.send(
                    target=_TCP_TARGET,
                    remote_start=ModbusAddress(0x100),
                    source=Source,
                    sending=Sending,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )

        runner = PLC(logic=logic)
        runner.patch({"Enable": True, "Source": 65538})
        runner.step()

        assert len(submissions) == 1
        # DINT should pack into 2 registers
        regs = submissions[0]["registers"]
        assert isinstance(regs, list)
        assert len(regs) == 2


# ---------------------------------------------------------------------------
# Execute state machine (monkeypatched futures)
# ---------------------------------------------------------------------------


class TestRawExecuteStateMachine:
    def test_send_submit_busy_success_restart(self, monkeypatch: pytest.MonkeyPatch):
        from pyrung.core.instruction.send_receive import _core as mod

        submissions: list[tuple[dict, Future[_RequestResult]]] = []

        def fake_submit(**kwargs: object) -> Future[_RequestResult]:
            fut: Future[_RequestResult] = Future()
            submissions.append((dict(kwargs), fut))
            return fut

        monkeypatch.setattr(mod, "_submit_raw_send_request", fake_submit)

        Source = Int("Source")
        Enable = Bool("Enable")
        Sending = Bool("Sending")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")

        with Program() as logic:
            with Rung(Enable):
                mod.send(
                    target=_TCP_TARGET,
                    remote_start=ModbusAddress(0x100, RegisterType.HOLDING),
                    source=Source,
                    sending=Sending,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )

        runner = PLC(logic=logic)
        runner.patch({"Enable": True, "Source": 42})
        runner.step()

        assert len(submissions) == 1
        assert runner.current_state.tags["Sending"] is True
        assert runner.current_state.tags["Success"] is False

        # Complete the request
        submissions[0][1].set_result(_RequestResult(ok=True, exception_code=0))
        runner.patch({"Enable": True})
        runner.step()

        assert runner.current_state.tags["Sending"] is False
        assert runner.current_state.tags["Success"] is True
        assert runner.current_state.tags["Error"] is False

        # Auto-restart
        runner.patch({"Enable": True})
        runner.step()
        assert len(submissions) == 2
        assert runner.current_state.tags["Sending"] is True

    def test_receive_submit_busy_values_success(self, monkeypatch: pytest.MonkeyPatch):
        from pyrung.core.instruction.send_receive import _core as mod

        future: Future[_RequestResult] = Future()

        def fake_submit(**kwargs: object) -> Future[_RequestResult]:
            return future

        monkeypatch.setattr(mod, "_submit_raw_receive_request", fake_submit)

        Enable = Bool("Enable")
        Receiving = Bool("Receiving")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")
        Local = Block("Local", TagType.INT, 1, 2)

        with Program() as logic:
            with Rung(Enable):
                mod.receive(
                    target=_TCP_TARGET,
                    remote_start=ModbusAddress(0x100, RegisterType.HOLDING),
                    dest=Local.select(1, 2),
                    receiving=Receiving,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )

        runner = PLC(logic=logic)
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["Receiving"] is True

        # Return raw register values (2 INT registers)
        future.set_result(_RequestResult(ok=True, exception_code=0, values=(11, 22)))
        runner.patch({"Enable": True})
        runner.step()

        assert runner.current_state.tags["Receiving"] is False
        assert runner.current_state.tags["Success"] is True
        assert runner.current_state.tags["Local1"] == 11
        assert runner.current_state.tags["Local2"] == 22

    def test_receive_dint_with_word_swap(self, monkeypatch: pytest.MonkeyPatch):
        from pyrung.core.instruction.send_receive import _core as mod

        future: Future[_RequestResult] = Future()

        def fake_submit(**kwargs: object) -> Future[_RequestResult]:
            return future

        monkeypatch.setattr(mod, "_submit_raw_receive_request", fake_submit)

        target = ModbusTcpTarget("vfd", "192.168.1.20")
        Enable = Bool("Enable")
        Receiving = Bool("Receiving")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")
        Dest = Dint("Dest")

        with Program() as logic:
            with Rung(Enable):
                mod.receive(
                    target=target,
                    remote_start=ModbusAddress(0x100),
                    dest=Dest,
                    receiving=Receiving,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                    word_swap=True,
                )

        runner = PLC(logic=logic)
        runner.patch({"Enable": True})
        runner.step()

        # Simulate LOW_HIGH register response: lo word first, then hi word
        val = 123456
        hi, lo = struct.unpack(">HH", struct.pack(">i", val))
        future.set_result(_RequestResult(ok=True, exception_code=0, values=(lo, hi)))
        runner.patch({"Enable": True})
        runner.step()

        assert runner.current_state.tags["Dest"] == 123456
        assert runner.current_state.tags["Success"] is True

    def test_error_sets_exception_code(self, monkeypatch: pytest.MonkeyPatch):
        from pyrung.core.instruction.send_receive import _core as mod

        future: Future[_RequestResult] = Future()

        def fake_submit(**kwargs: object) -> Future[_RequestResult]:
            return future

        monkeypatch.setattr(mod, "_submit_raw_send_request", fake_submit)

        Source = Int("Source")
        Enable = Bool("Enable")
        Sending = Bool("Sending")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")

        with Program() as logic:
            with Rung(Enable):
                mod.send(
                    target=_TCP_TARGET,
                    remote_start=ModbusAddress(0x100),
                    source=Source,
                    sending=Sending,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )

        runner = PLC(logic=logic)
        runner.patch({"Enable": True, "Source": 1})
        runner.step()

        future.set_result(_RequestResult(ok=False, exception_code=6))
        runner.patch({"Enable": True})
        runner.step()

        assert runner.current_state.tags["Sending"] is False
        assert runner.current_state.tags["Success"] is False
        assert runner.current_state.tags["Error"] is True
        assert runner.current_state.tags["ExCode"] == 6

    def test_disabled_rung_keeps_pending_alive(self, monkeypatch: pytest.MonkeyPatch):
        """Click semantics: dropping Enable mid-transaction does not cancel
        the in-flight request; busy stays True until completion, then
        success/error latch across the disabled state."""
        from pyrung.core.instruction.send_receive import _core as mod

        futures: list[Future[_RequestResult]] = []

        def fake_submit(**kwargs: object) -> Future[_RequestResult]:
            fut: Future[_RequestResult] = Future()
            futures.append(fut)
            return fut

        monkeypatch.setattr(mod, "_submit_raw_send_request", fake_submit)

        Source = Int("Source")
        Enable = Bool("Enable")
        Sending = Bool("Sending")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")

        with Program() as logic:
            with Rung(Enable):
                mod.send(
                    target=_TCP_TARGET,
                    remote_start=ModbusAddress(0x100),
                    source=Source,
                    sending=Sending,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )

        runner = PLC(logic=logic)
        runner.patch({"Enable": True, "Source": 1})
        runner.step()
        assert runner.current_state.tags["Sending"] is True

        runner.patch({"Enable": False})
        runner.step()
        assert runner.current_state.tags["Sending"] is True

        futures[0].set_result(_RequestResult(ok=True, exception_code=0))
        runner.patch({"Enable": False})
        runner.step()
        assert runner.current_state.tags["Sending"] is False
        assert runner.current_state.tags["Success"] is True
        assert runner.current_state.tags["Error"] is False

    def test_rtu_send_uses_raw_backend(self, monkeypatch: pytest.MonkeyPatch):
        from pyrung.core.instruction.send_receive import _core as mod

        submissions: list[dict] = []

        def fake_submit(**kwargs: object) -> Future[_RequestResult]:
            submissions.append(dict(kwargs))
            fut: Future[_RequestResult] = Future()
            return fut

        monkeypatch.setattr(mod, "_submit_raw_send_request", fake_submit)

        Source = Int("Source")
        Enable = Bool("Enable")
        Sending = Bool("Sending")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")

        with Program() as logic:
            with Rung(Enable):
                mod.send(
                    target=_RTU_TARGET,
                    remote_start=ModbusAddress(0x2000, RegisterType.HOLDING),
                    source=Source,
                    sending=Sending,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )

        runner = PLC(logic=logic)
        runner.patch({"Enable": True, "Source": 99})
        runner.step()

        assert len(submissions) == 1
        assert submissions[0]["address"] == 0x2000
        assert submissions[0]["register_type"] == RegisterType.HOLDING
        target_obj = submissions[0]["target"]
        assert isinstance(target_obj, ModbusRtuTarget)
        assert target_obj.serial_port == "/dev/ttyUSB0"
