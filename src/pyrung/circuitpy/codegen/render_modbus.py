"""CircuitPython Modbus rendering helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pyclickplc.addresses import format_address_display
from pyclickplc.banks import BANKS, DataType
from pyclickplc.modbus import MODBUS_MAPPINGS, plc_to_modbus

from pyrung.circuitpy.codegen.context import CodegenContext
from pyrung.click.system_mappings import SYSTEM_CLICK_SLOTS
from pyrung.core.system_points import system

_SYSTEM_SLOT_BY_HARDWARE = {slot.hardware.name: slot for slot in SYSTEM_CLICK_SLOTS}
_WORD_BANKS = {"DS", "DD", "DH", "DF", "TD", "CTD", "TXT", "XD", "YD", "SD"}
_COIL_BANKS = {"X", "Y", "C", "T", "CT", "SC"}

_BANK_DESCRIPTIONS: dict[str, str] = {
    "X": "digital inputs",
    "Y": "digital outputs",
    "C": "bit memory",
    "T": "timer done bits",
    "CT": "counter done bits",
    "SC": "system control bits",
    "DS": "int memory",
    "DD": "double-int memory",
    "DH": "hex/word memory",
    "DF": "float memory",
    "TXT": "text memory",
    "TD": "timer accumulators",
    "CTD": "counter accumulators",
    "XD": "input words",
    "YD": "output words",
    "SD": "system data",
}


@dataclass(frozen=True)
class _ModbusBackedSlot:
    bank: str
    index: int
    logical_name: str
    symbol: str
    data_type: DataType
    read_only: bool
    source: str
    default: object


def _ip_tuple_literal(value: str) -> str:
    parts = [part.strip() for part in value.split(".")]
    if len(parts) != 4:
        raise ValueError(f"Invalid IPv4 address {value!r}")
    octets: list[int] = []
    for part in parts:
        try:
            octet = int(part, 10)
        except ValueError as exc:
            raise ValueError(f"Invalid IPv4 address {value!r}") from exc
        if octet < 0 or octet > 255:
            raise ValueError(f"Invalid IPv4 address {value!r}")
        octets.append(octet)
    return repr(tuple(octets))


def _modbus_backed_slots(ctx: CodegenContext) -> tuple[_ModbusBackedSlot, ...]:
    if ctx.tag_map is None:
        return ()
    slots: list[_ModbusBackedSlot] = []
    for slot in ctx.tag_map.mapped_slots():
        tag = ctx.referenced_tags.get(slot.logical_name)
        if tag is None:
            continue
        symbol = ctx.symbol_for_tag(tag)
        slots.append(
            _ModbusBackedSlot(
                bank=slot.memory_type,
                index=slot.address,
                logical_name=slot.logical_name,
                symbol=symbol,
                data_type=BANKS[slot.memory_type].data_type,
                read_only=slot.read_only,
                source=slot.source,
                default=tag.default,
            )
        )
    return tuple(sorted(slots, key=lambda item: (item.bank, item.index, item.logical_name)))


def _backing_global_for_slot(ctx: CodegenContext, slot: _ModbusBackedSlot) -> str:
    block_info = ctx.tag_block_addresses.get(slot.logical_name)
    if block_info is None:
        return slot.symbol
    block_id, _ = block_info
    return ctx.block_symbols[block_id]


def _modbus_valid_ranges(bank: str) -> tuple[tuple[int, int], ...]:
    cfg = BANKS[bank]
    addresses: set[int] = set()
    if cfg.valid_ranges is not None:
        for lo, hi in cfg.valid_ranges:
            for idx in range(lo, hi + 1):
                addr, _ = plc_to_modbus(bank, idx)
                addresses.add(addr)
    else:
        for idx in range(cfg.min_addr, cfg.max_addr + 1):
            addr, _ = plc_to_modbus(bank, idx)
            addresses.add(addr)
    ordered = sorted(addresses)
    if not ordered:
        return ()
    ranges: list[tuple[int, int]] = []
    lo = hi = ordered[0]
    for addr in ordered[1:]:
        if addr == hi + 1:
            hi = addr
            continue
        ranges.append((lo, hi))
        lo = hi = addr
    ranges.append((lo, hi))
    return tuple(ranges)


def _word_start_expr(word_index_expr: str) -> str:
    return (
        f"(1 if int({word_index_expr}) == 0 else "
        f"21 if int({word_index_expr}) == 1 else "
        f"(((int({word_index_expr}) // 2) * 100) + 1))"
    )


def _render_sparse_reverse_coil(bank: str, base: int) -> list[str]:
    return [
        f"    if {base} <= addr <= {base + 31}:",
        f"        _offset = addr - {base}",
        "        if _offset < 16:",
        f'            return ("{bank}", _offset + 1)',
        f'        return ("{bank}", 21 + (_offset - 16))',
        f"    if {base + 32} <= addr <= {base + 47}:",
        f'        return ("{bank}", 101 + (addr - {base + 32}))',
        f"    if {base + 64} <= addr <= {base + 79}:",
        f'        return ("{bank}", 201 + (addr - {base + 64}))',
        f"    if {base + 96} <= addr <= {base + 111}:",
        f'        return ("{bank}", 301 + (addr - {base + 96}))',
        f"    if {base + 128} <= addr <= {base + 143}:",
        f'        return ("{bank}", 401 + (addr - {base + 128}))',
        f"    if {base + 160} <= addr <= {base + 175}:",
        f'        return ("{bank}", 501 + (addr - {base + 160}))',
        f"    if {base + 192} <= addr <= {base + 207}:",
        f'        return ("{bank}", 601 + (addr - {base + 192}))',
        f"    if {base + 224} <= addr <= {base + 239}:",
        f'        return ("{bank}", 701 + (addr - {base + 224}))',
        f"    if {base + 256} <= addr <= {base + 271}:",
        f'        return ("{bank}", 801 + (addr - {base + 256}))',
    ]


def _render_reverse_register_case(bank: str) -> list[str]:
    mapping = MODBUS_MAPPINGS[bank]
    cfg = BANKS[bank]
    if bank == "TXT":
        max_reg = mapping.base + ((cfg.max_addr + 1) // 2) - 1
        return [
            f"    if {mapping.base} <= addr <= {max_reg}:",
            f'        return ("TXT", ((addr - {mapping.base}) * 2) + 1, 0)',
        ]
    if cfg.min_addr == 0:
        max_reg = mapping.base + cfg.max_addr
        return [
            f"    if {mapping.base} <= addr <= {max_reg}:",
            f'        return ("{bank}", addr - {mapping.base}, 0)',
        ]
    max_reg = mapping.base + (mapping.width * cfg.max_addr) - 1
    return [
        f"    if {mapping.base} <= addr <= {max_reg}:",
        f"        _offset = addr - {mapping.base}",
        f'        return ("{bank}", (_offset // {mapping.width}) + 1, _offset % {mapping.width})',
    ]


def _render_ethernet_setup(ctx: CodegenContext) -> list[str]:
    if ctx.modbus_server is None and ctx.modbus_client is None:
        return []
    server = ctx.modbus_server
    lines = [
        "_mb_cs = digitalio.DigitalInOut(board.D5)",
        "_mb_spi = busio.SPI(board.SCK, MOSI=board.MOSI, MISO=board.MISO)",
        "_mb_eth = WIZNET5K(_mb_spi, _mb_cs)",
    ]
    if server is not None:
        lines.extend(
            [
                f"_mb_eth.ifconfig = ({_ip_tuple_literal(server.ip)}, {_ip_tuple_literal(server.subnet)}, {_ip_tuple_literal(server.gateway)}, {_ip_tuple_literal(server.dns)})",
            ]
        )
    lines.extend(
        [
            "_mb_socket.set_interface(_mb_eth)",
            "",
        ]
    )
    return lines


def _render_modbus_accessors(ctx: CodegenContext) -> list[str]:
    backed = _modbus_backed_slots(ctx)
    coil_slots = [slot for slot in backed if slot.bank in _COIL_BANKS]
    reg_slots = [slot for slot in backed if slot.bank in _WORD_BANKS]
    coil_write_globals = sorted({_backing_global_for_slot(ctx, slot) for slot in coil_slots})
    reg_write_globals = sorted({_backing_global_for_slot(ctx, slot) for slot in reg_slots})
    if ctx.runstop is not None:
        coil_write_globals = sorted({*coil_write_globals, "_mode_run"})
    lines: list[str] = [
        "def _mb_reverse_coil(addr):",
    ]
    for bank in ("X", "Y"):
        lines.append(f"    # {bank} ({_BANK_DESCRIPTIONS[bank]})")
        lines.extend(_render_sparse_reverse_coil(bank, MODBUS_MAPPINGS[bank].base))
    for bank in ("C", "T", "CT", "SC"):
        mapping = MODBUS_MAPPINGS[bank]
        cfg = BANKS[bank]
        max_addr = mapping.base + cfg.max_addr - 1
        lines.append(f"    # {bank} ({_BANK_DESCRIPTIONS[bank]})")
        lines.extend(
            [
                f"    if {mapping.base} <= addr <= {max_addr}:",
                f'        return ("{bank}", (addr - {mapping.base}) + 1)',
            ]
        )
    lines.extend(
        [
            "    return None",
            "",
            "def _mb_reverse_register(addr):",
        ]
    )
    for bank in ("DS", "DD", "DH", "DF", "TXT", "TD", "CTD", "XD", "YD", "SD"):
        lines.append(f"    # {bank} ({_BANK_DESCRIPTIONS[bank]})")
        lines.extend(_render_reverse_register_case(bank))
    lines.extend(
        [
            "    return None",
            "",
            "def _mb_xy_word_start(word_index):",
            "    _idx = int(word_index)",
            "    if _idx < 0 or _idx > 16:",
            "        return None",
            f"    return {_word_start_expr('word_index')}",
            "",
            "def _mb_read_coil_plc(bank, index):",
        ]
    )
    if coil_slots:
        for slot in coil_slots:
            if slot.source == "system" and slot.logical_name == system.sys.mode_run.name:
                if ctx.runstop is not None:
                    lines.extend(
                        [
                            f'    if bank == "{slot.bank}" and index == {slot.index}:',
                            "        return bool(_mode_run)",
                        ]
                    )
                else:
                    lines.extend(
                        [
                            f'    if bank == "{slot.bank}" and index == {slot.index}:',
                            f"        return bool({slot.symbol})",
                        ]
                    )
                continue
            lines.extend(
                [
                    f'    if bank == "{slot.bank}" and index == {slot.index}:',
                    f"        return bool({slot.symbol})",
                ]
            )
    lines.extend(
        [
            "    return False",
            "",
            "def _mb_write_coil_plc(bank, index, val):",
        ]
    )
    if coil_write_globals:
        lines.append(f"    global {', '.join(coil_write_globals)}")
    lines.extend(
        [
            "    _value = bool(val)",
        ]
    )
    for slot in coil_slots:
        if slot.source == "system" and slot.logical_name == system.sys.cmd_mode_stop.name:
            lines.extend(
                [
                    f'    if bank == "{slot.bank}" and index == {slot.index}:',
                ]
            )
            if ctx.runstop is not None:
                lines.extend(
                    [
                        "        if _value:",
                        "            _mode_run = False",
                    ]
                )
            if slot.symbol != "":
                lines.append(f"        {slot.symbol} = _value")
            lines.append("        return True")
            continue
        if slot.read_only:
            lines.extend(
                [
                    f'    if bank == "{slot.bank}" and index == {slot.index}:',
                    "        return True",
                ]
            )
            continue
        lines.extend(
            [
                f'    if bank == "{slot.bank}" and index == {slot.index}:',
                f"        {slot.symbol} = _value",
                "        return True",
            ]
        )
    lines.extend(
        [
            '    if bank in ("Y", "C"):',
            "        return True",
            '    if bank == "SC":',
            f"        return index in {sorted(MODBUS_MAPPINGS['SC'].writable or [])!r}",
            "    return False",
            "",
            "def _mb_read_mirrored_word(bank, word_index):",
            "    _start = _mb_xy_word_start(word_index)",
            "    if _start is None:",
            "        return 0",
            "    _word = 0",
            "    for _bit_index in range(16):",
            "        if _mb_read_coil_plc(bank, _start + _bit_index):",
            "            _word |= (1 << _bit_index)",
            "    return _word",
            "",
            "def _mb_write_mirrored_word(word_index, value):",
            "    _start = _mb_xy_word_start(word_index)",
            "    if _start is None:",
            "        return False",
            "    _word = int(value) & 0xFFFF",
            "    for _bit_index in range(16):",
            '        _mb_write_coil_plc("Y", _start + _bit_index, bool((_word >> _bit_index) & 0x1))',
            "    return True",
            "",
            "def _mb_read_reg_plc(bank, index, reg_pos):",
            '    if bank == "XD":',
            '        return _mb_read_mirrored_word("X", index)',
            '    if bank == "YD":',
            '        return _mb_read_mirrored_word("Y", index)',
        ]
    )
    for slot in reg_slots:
        if slot.data_type == DataType.INT:
            pack_expr = f"struct.unpack('<H', struct.pack('<h', int({slot.symbol})))[0]"
            read_lines = [pack_expr]
        elif slot.data_type == DataType.HEX:
            read_lines = [f"(int({slot.symbol}) & 0xFFFF)"]
        elif slot.data_type == DataType.INT2:
            read_lines = [
                f"struct.unpack('<HH', struct.pack('<i', int({slot.symbol})))[int(reg_pos)]"
            ]
        elif slot.data_type == DataType.FLOAT:
            read_lines = [
                f"struct.unpack('<HH', struct.pack('<f', float({slot.symbol})))[int(reg_pos)]"
            ]
        elif slot.data_type == DataType.TXT:
            pair_index = (slot.index - 1) // 2 * 2 + 1
            # Skip even slots whose odd pair is already in reg_slots (avoids
            # duplicate accessor entries — the odd slot's entry handles both).
            if slot.index != pair_index and any(
                item.bank == slot.bank and item.index == pair_index for item in reg_slots
            ):
                continue
            low_slot = next(
                (item for item in reg_slots if item.bank == slot.bank and item.index == pair_index),
                None,
            )
            high_slot = next(
                (
                    item
                    for item in reg_slots
                    if item.bank == slot.bank and item.index == pair_index + 1
                ),
                None,
            )
            if low_slot is not None:
                low_expr = f"ord({low_slot.symbol}) if {low_slot.symbol} else 0"
            else:
                low_expr = "0"
            if high_slot is not None:
                high_expr = f"ord({high_slot.symbol}) if {high_slot.symbol} else 0"
            else:
                high_expr = "0"
            read_lines = [f"(({low_expr}) & 0xFF) | ((({high_expr}) & 0xFF) << 8)"]
        else:
            continue
        lines.extend(
            [
                f'    if bank == "{slot.bank}" and index == {pair_index if slot.data_type == DataType.TXT else slot.index}:',
                f"        return {read_lines[0]}",
            ]
        )
    lines.extend(
        [
            "    return 0",
            "",
            "def _mb_write_reg_plc(bank, index, reg_pos, value):",
        ]
    )
    if reg_write_globals:
        lines.append(f"    global {', '.join(reg_write_globals)}")
    lines.extend(
        [
            "    _word = int(value) & 0xFFFF",
            '    if bank == "XD":',
            "        return False",
            '    if bank == "YD":',
            "        return _mb_write_mirrored_word(index, _word)",
        ]
    )
    for slot in reg_slots:
        if slot.read_only and slot.logical_name != system.sys.cmd_mode_stop.name:
            lines.extend(
                [
                    f'    if bank == "{slot.bank}" and index == {slot.index}:',
                    "        return True",
                ]
            )
            continue
        if slot.data_type == DataType.INT:
            store_expr = "struct.unpack('<h', struct.pack('<H', _word))[0]"
        elif slot.data_type == DataType.HEX:
            store_expr = "(_word & 0xFFFF)"
        elif slot.data_type == DataType.INT2:
            store_expr = (
                f"struct.unpack('<i', struct.pack('<HH', _word if int(reg_pos) == 0 else "
                f"struct.unpack('<HH', struct.pack('<i', int({slot.symbol})))[0], "
                f"_word if int(reg_pos) == 1 else struct.unpack('<HH', struct.pack('<i', int({slot.symbol})))[1]))[0]"
            )
        elif slot.data_type == DataType.FLOAT:
            store_expr = (
                f"struct.unpack('<f', struct.pack('<HH', _word if int(reg_pos) == 0 else "
                f"struct.unpack('<HH', struct.pack('<f', float({slot.symbol})))[0], "
                f"_word if int(reg_pos) == 1 else struct.unpack('<HH', struct.pack('<f', float({slot.symbol})))[1]))[0]"
            )
        elif slot.data_type == DataType.TXT:
            pair_index = (slot.index - 1) // 2 * 2 + 1
            if slot.index != pair_index and any(
                item.bank == slot.bank and item.index == pair_index for item in reg_slots
            ):
                continue
            low_slot = next(
                (item for item in reg_slots if item.bank == slot.bank and item.index == pair_index),
                None,
            )
            high_slot = next(
                (
                    item
                    for item in reg_slots
                    if item.bank == slot.bank and item.index == pair_index + 1
                ),
                None,
            )
            lines.extend(
                [
                    f'    if bank == "{slot.bank}" and index == {pair_index}:',
                ]
            )
            if low_slot is not None:
                lines.append(f"        {low_slot.symbol} = chr(_word & 0xFF)")
            if high_slot is not None:
                lines.append(f"        {high_slot.symbol} = chr((_word >> 8) & 0xFF)")
            lines.append("        return True")
            continue
        else:
            continue
        lines.extend(
            [
                f'    if bank == "{slot.bank}" and index == {slot.index}:',
                f"        {slot.symbol} = {store_expr}",
                "        return True",
            ]
        )
    lines.extend(
        [
            '    if bank in ("DS", "DD", "DH", "DF", "TXT", "TD", "CTD"):',
            "        return True",
            '    if bank == "SD":',
            f"        return index in {sorted(MODBUS_MAPPINGS['SD'].writable or [])!r}",
            "    return False",
            "",
            "def _mb_read_coil(addr):",
            "    _mapped = _mb_reverse_coil(int(addr))",
            "    if _mapped is None:",
            "        return None",
            "    _bank, _index = _mapped",
            "    return _mb_read_coil_plc(_bank, _index)",
            "",
            "def _mb_write_coil(addr, val):",
            "    _mapped = _mb_reverse_coil(int(addr))",
            "    if _mapped is None:",
            "        return False",
            "    _bank, _index = _mapped",
            "    return _mb_write_coil_plc(_bank, _index, val)",
            "",
            "def _mb_read_reg(addr):",
            "    _mapped = _mb_reverse_register(int(addr))",
            "    if _mapped is None:",
            "        return None",
            "    _bank, _index, _reg_pos = _mapped",
            "    return _mb_read_reg_plc(_bank, _index, _reg_pos)",
            "",
            "def _mb_write_reg(addr, val):",
            "    _mapped = _mb_reverse_register(int(addr))",
            "    if _mapped is None:",
            "        return False",
            "    _bank, _index, _reg_pos = _mapped",
            "    return _mb_write_reg_plc(_bank, _index, _reg_pos, val)",
            "",
        ]
    )
    return lines


def _render_modbus_protocol(ctx: CodegenContext) -> list[str]:
    if ctx.modbus_server is None:
        return []
    lines = [
        "def _mb_err(tid, uid, fc, code):",
        "    return struct.pack('>HHHBB', int(tid) & 0xFFFF, 0, 3, int(uid) & 0xFF, (int(fc) & 0x7F) | 0x80) + bytes([int(code) & 0xFF])",
        "",
        "def _mb_handle(data, n):",
        "    if n < 8:",
        "        return None",
        "    try:",
        "        tid, pid, length, uid = struct.unpack('>HHHB', bytes(data[:7]))",
        "    except Exception:",
        "        return None",
        "    if pid != 0:",
        "        return None",
        "    if length < 2 or (length + 6) > int(n):",
        "        return None",
        "    fc = int(data[7])",
        "    pdu_end = 6 + length",
        "    if fc in (1, 2):",
        "        if pdu_end < 12:",
        "            return _mb_err(tid, uid, fc, 3)",
        "        start, count = struct.unpack('>HH', bytes(data[8:12]))",
        "        if count < 1 or count > 2000:",
        "            return _mb_err(tid, uid, fc, 3)",
        "        bits = []",
        "        for _offset in range(count):",
        "            _bit = _mb_read_coil(start + _offset)",
        "            if _bit is None:",
        "                return _mb_err(tid, uid, fc, 2)",
        "            bits.append(bool(_bit))",
        "        byte_count = (count + 7) // 8",
        "        payload = bytearray(byte_count)",
        "        for _offset, _bit in enumerate(bits):",
        "            if _bit:",
        "                payload[_offset // 8] |= 1 << (_offset % 8)",
        "        return struct.pack('>HHHBBB', tid, 0, len(payload) + 3, uid, fc, len(payload)) + bytes(payload)",
        "    if fc in (3, 4):",
        "        if pdu_end < 12:",
        "            return _mb_err(tid, uid, fc, 3)",
        "        start, count = struct.unpack('>HH', bytes(data[8:12]))",
        "        if count < 1 or count > 125:",
        "            return _mb_err(tid, uid, fc, 3)",
        "        regs = []",
        "        for _offset in range(count):",
        "            _reg = _mb_read_reg(start + _offset)",
        "            if _reg is None:",
        "                return _mb_err(tid, uid, fc, 2)",
        "            regs.append(int(_reg) & 0xFFFF)",
        "        payload = bytearray()",
        "        for _reg in regs:",
        "            payload.extend(struct.pack('>H', _reg))",
        "        return struct.pack('>HHHBBB', tid, 0, len(payload) + 3, uid, fc, len(payload)) + bytes(payload)",
        "    if fc == 5:",
        "        if pdu_end < 12:",
        "            return _mb_err(tid, uid, fc, 3)",
        "        addr, raw = struct.unpack('>HH', bytes(data[8:12]))",
        "        if raw not in (0x0000, 0xFF00):",
        "            return _mb_err(tid, uid, fc, 3)",
        "        if not _mb_write_coil(addr, raw == 0xFF00):",
        "            return _mb_err(tid, uid, fc, 2)",
        "        return bytes(data[:12])",
        "    if fc == 6:",
        "        if pdu_end < 12:",
        "            return _mb_err(tid, uid, fc, 3)",
        "        addr, raw = struct.unpack('>HH', bytes(data[8:12]))",
        "        if not _mb_write_reg(addr, raw):",
        "            return _mb_err(tid, uid, fc, 2)",
        "        return bytes(data[:12])",
        "    if fc == 15:",
        "        if pdu_end < 13:",
        "            return _mb_err(tid, uid, fc, 3)",
        "        start, count, byte_count = struct.unpack('>HHB', bytes(data[8:13]))",
        "        if count < 1 or count > 1968 or byte_count != ((count + 7) // 8):",
        "            return _mb_err(tid, uid, fc, 3)",
        "        if pdu_end < 13 + byte_count:",
        "            return _mb_err(tid, uid, fc, 3)",
        "        payload = data[13:13 + byte_count]",
        "        for _offset in range(count):",
        "            _bit = bool((payload[_offset // 8] >> (_offset % 8)) & 0x1)",
        "            if not _mb_write_coil(start + _offset, _bit):",
        "                return _mb_err(tid, uid, fc, 2)",
        "        return struct.pack('>HHHBBHH', tid, 0, 6, uid, fc, start, count)",
        "    if fc == 16:",
        "        if pdu_end < 13:",
        "            return _mb_err(tid, uid, fc, 3)",
        "        start, count, byte_count = struct.unpack('>HHB', bytes(data[8:13]))",
        "        if count < 1 or count > 123 or byte_count != (count * 2):",
        "            return _mb_err(tid, uid, fc, 3)",
        "        if pdu_end < 13 + byte_count:",
        "            return _mb_err(tid, uid, fc, 3)",
        "        for _offset in range(count):",
        "            _base = 13 + (_offset * 2)",
        "            _reg = struct.unpack('>H', bytes(data[_base:_base + 2]))[0]",
        "            if not _mb_write_reg(start + _offset, _reg):",
        "                return _mb_err(tid, uid, fc, 2)",
        "        return struct.pack('>HHHBBHH', tid, 0, 6, uid, fc, start, count)",
        "    return _mb_err(tid, uid, fc, 1)",
        "",
    ]
    return lines


def _render_modbus_server(ctx: CodegenContext) -> list[str]:
    if ctx.modbus_server is None:
        return []
    server = ctx.modbus_server
    return [
        "_mb_server = _mb_socket.socket(_mb_socket.AF_INET, _mb_socket.SOCK_STREAM)",
        f"_mb_server.bind(('', {server.port}))",
        f"_mb_server.listen({server.max_clients})",
        "_mb_server.settimeout(0)",
        f"_mb_clients = [None] * {server.max_clients}",
        "_mb_buf = bytearray(260)",
        "",
        "def service_modbus_server():",
        "    try:",
        "        _client, _addr = _mb_server.accept()",
        "    except OSError:",
        "        _client = None",
        "    if _client is not None:",
        "        _client.settimeout(0)",
        "        for _idx in range(len(_mb_clients)):",
        "            if _mb_clients[_idx] is None:",
        "                _mb_clients[_idx] = _client",
        "                _client = None",
        "                break",
        "        if _client is not None:",
        "            _client.close()",
        "    for _idx in range(len(_mb_clients)):",
        "        _sock = _mb_clients[_idx]",
        "        if _sock is None:",
        "            continue",
        "        try:",
        "            _n = _sock.recv_into(_mb_buf)",
        "        except OSError:",
        "            _sock.close()",
        "            _mb_clients[_idx] = None",
        "            continue",
        "        if not _n:",
        "            _sock.close()",
        "            _mb_clients[_idx] = None",
        "            continue",
        "        _resp = _mb_handle(_mb_buf, int(_n))",
        "        if _resp is None:",
        "            continue",
        "        try:",
        "            _sock.send(_resp)",
        "        except OSError:",
        "            _sock.close()",
        "            _mb_clients[_idx] = None",
        "",
    ]


def _modbus_client_target_lookup(ctx: CodegenContext) -> dict[str, Any]:
    if ctx.modbus_client is None:
        return {}
    return {target.name: target for target in ctx.modbus_client.targets}


def _render_client_status_helper(spec: Any) -> list[str]:
    globals_needed = sorted(
        {
            spec.busy.owner,
            spec.success.owner,
            spec.error.owner,
            spec.exception_response.owner,
        }
    )
    lines = [f"def {spec.var_name}_set_status(busy, success, error, exception_response):"]
    if globals_needed:
        lines.append(f"    global {', '.join(globals_needed)}")
    lines.extend(
        [
            f"    {spec.busy.symbol} = bool(busy)",
            f"    {spec.success.symbol} = bool(success)",
            f"    {spec.error.symbol} = bool(error)",
            f"    {spec.exception_response.symbol} = int(exception_response)",
            "",
        ]
    )
    return lines


def _render_client_values_helper(spec: Any) -> list[str]:
    globals_needed = sorted({item.owner for item in spec.items})
    lines = [f"def {spec.var_name}_values():"]
    if globals_needed:
        lines.append(f"    global {', '.join(globals_needed)}")
    if spec.items:
        lines.append(f"    return [{', '.join(item.symbol for item in spec.items)}]")
    else:
        lines.append("    return []")
    lines.append("")
    return lines


_FC_NAMES: dict[int, str] = {
    1: "read coils",
    2: "read discrete inputs",
    3: "read holding registers",
    4: "read input registers",
    5: "write single coil",
    6: "write single register",
    15: "write multiple coils",
    16: "write multiple registers",
}


def _render_client_request_helper(spec: Any, target: Any) -> list[str]:
    if spec.bank is not None:
        plc_addr = format_address_display(spec.bank, spec.plc_start)
    else:
        kind = "coil" if spec.is_coil else "register"
        plc_addr = f"{kind} 0x{spec.modbus_start:04X}"
    fc_desc = _FC_NAMES.get(spec.function_code, f"FC {spec.function_code}")
    if spec.kind == "send":
        if spec.function_code == 5:
            pdu_lines = [
                f"    _values = {spec.var_name}_values()",
                "    _raw = 0xFF00 if bool(_values[0]) else 0x0000",
                f"    _pdu = struct.pack('>BHH', {spec.function_code}, {spec.modbus_start}, _raw)",
            ]
        elif spec.function_code == 15:
            pdu_lines = [
                f"    _values = [bool(_value) for _value in {spec.var_name}_values()]",
                "    _byte_count = (len(_values) + 7) // 8",
                "    _payload = bytearray(_byte_count)",
                "    for _offset, _value in enumerate(_values):",
                "        if _value:",
                "            _payload[_offset // 8] |= 1 << (_offset % 8)",
                f"    _pdu = struct.pack('>BHHB', {spec.function_code}, {spec.modbus_start}, {spec.modbus_quantity}, _byte_count) + bytes(_payload)",
            ]
        elif spec.bank is not None:
            pdu_lines = [
                f"    _regs = _mb_client_pack_register_values('{spec.bank}', {spec.var_name}_values())",
            ]
            if spec.function_code == 6:
                pdu_lines.append(
                    f"    _pdu = struct.pack('>BHH', {spec.function_code}, {spec.modbus_start}, int(_regs[0]) & 0xFFFF)"
                )
            else:
                pdu_lines.append(
                    f"    _pdu = struct.pack('>BHHB', {spec.function_code}, {spec.modbus_start}, {spec.modbus_quantity}, len(_regs) * 2)"
                )
                pdu_lines.append("    for _reg in _regs:")
                pdu_lines.append("        _pdu += struct.pack('>H', int(_reg) & 0xFFFF)")
        else:
            tag_types_lit = repr([item.tag_type for item in spec.items])
            pdu_lines = [
                f"    _regs = _mb_client_raw_pack_register_values({spec.var_name}_values(), {tag_types_lit}, '{spec.word_order}')",
            ]
            if spec.function_code == 6:
                pdu_lines.append(
                    f"    _pdu = struct.pack('>BHH', {spec.function_code}, {spec.modbus_start}, int(_regs[0]) & 0xFFFF)"
                )
            else:
                pdu_lines.append(
                    f"    _pdu = struct.pack('>BHHB', {spec.function_code}, {spec.modbus_start}, {spec.modbus_quantity}, len(_regs) * 2)"
                )
                pdu_lines.append("    for _reg in _regs:")
                pdu_lines.append("        _pdu += struct.pack('>H', int(_reg) & 0xFFFF)")
    else:
        pdu_lines = [
            f"    _pdu = struct.pack('>BHH', {spec.function_code}, {spec.modbus_start}, {spec.modbus_quantity})"
        ]

    count_desc = f"{spec.item_count} {'coil' if spec.function_code in (1, 2, 5, 15) else 'register'}{'s' if spec.item_count != 1 else ''}"
    return [
        f"def {spec.var_name}_build_request(tid):  # {fc_desc}: {plc_addr} ({count_desc}) on {spec.target_name}",
        *pdu_lines,
        f"    return struct.pack('>HHHB', int(tid) & 0xFFFF, 0, len(_pdu) + 1, {target.device_id}) + _pdu",
        "",
    ]


def _render_client_apply_helper(spec: Any, target: Any) -> list[str]:
    globals_needed = sorted({item.owner for item in spec.items})
    lines = [f"def {spec.var_name}_apply_response(data, n):"]
    if globals_needed:
        lines.append(f"    global {', '.join(globals_needed)}")
    lines.extend(
        [
            "    if int(n) < 8:",
            "        return (False, 0)",
            "    try:",
            "        _tid, _pid, _length, _uid = struct.unpack('>HHHB', bytes(data[:7]))",
            "    except Exception:",
            "        return (False, 0)",
            f"    if _pid != 0 or _uid != {target.device_id}:",
            "        return (False, 0)",
            f"    if _tid != int({spec.var_name}['tid']):",
            "        return (False, 0)",
            "    _frame_len = 6 + int(_length)",
            "    if _frame_len > int(n) or _frame_len < 8:",
            "        return (False, 0)",
            "    _fc = int(data[7])",
            "    if _fc & 0x80:",
            "        if _frame_len < 9:",
            "            return (False, 0)",
            "        return (False, int(data[8]))",
            f"    if _fc != {spec.function_code}:",
            "        return (False, 0)",
        ]
    )
    if spec.kind == "send":
        lines.extend(
            [
                "    return (True, 0)",
                "",
            ]
        )
        return lines

    if spec.is_coil:
        lines.extend(
            [
                "    if _frame_len < 9:",
                "        return (False, 0)",
                "    _byte_count = int(data[8])",
                "    if _frame_len < 9 + _byte_count:",
                "        return (False, 0)",
                "    _values = []",
                f"    for _offset in range({spec.item_count}):",
                "        _byte = int(data[9 + (_offset // 8)])",
                "        _values.append(bool((_byte >> (_offset % 8)) & 0x1))",
            ]
        )
    elif spec.bank is not None:
        lines.extend(
            [
                "    if _frame_len < 9:",
                "        return (False, 0)",
                "    _byte_count = int(data[8])",
                f"    if _byte_count != ({spec.modbus_quantity} * 2):",
                "        return (False, 0)",
                "    if _frame_len < 9 + _byte_count:",
                "        return (False, 0)",
                "    _regs = []",
                "    for _offset in range(_byte_count // 2):",
                "        _base = 9 + (_offset * 2)",
                "        _regs.append(struct.unpack('>H', bytes(data[_base:_base + 2]))[0])",
                f"    _values = _mb_client_unpack_register_values('{spec.bank}', _regs, {spec.item_count}, {spec.plc_start})",
                f"    if len(_values) < {spec.item_count}:",
                "        return (False, 0)",
            ]
        )
    else:
        tag_types_lit = repr([item.tag_type for item in spec.items])
        lines.extend(
            [
                "    if _frame_len < 9:",
                "        return (False, 0)",
                "    _byte_count = int(data[8])",
                f"    if _byte_count != ({spec.modbus_quantity} * 2):",
                "        return (False, 0)",
                "    if _frame_len < 9 + _byte_count:",
                "        return (False, 0)",
                "    _regs = []",
                "    for _offset in range(_byte_count // 2):",
                "        _base = 9 + (_offset * 2)",
                "        _regs.append(struct.unpack('>H', bytes(data[_base:_base + 2]))[0])",
                f"    _values = _mb_client_raw_unpack_register_values(_regs, {tag_types_lit}, '{spec.word_order}')",
                f"    if len(_values) < {spec.item_count}:",
                "        return (False, 0)",
            ]
        )

    for index, item in enumerate(spec.items):
        lines.append(
            f"    {item.symbol} = _store_copy_value_to_type(_values[{index}], '{item.tag_type}')"
        )
    lines.extend(
        [
            "    return (True, 0)",
            "",
        ]
    )
    return lines


def _render_modbus_client(ctx: CodegenContext) -> list[str]:
    if ctx.modbus_client is None:
        return []
    specs = list(ctx.modbus_client_specs)
    if not specs:
        return [
            "_mb_client_jobs = []",
            "",
            "def service_modbus_client():",
            "    pass",
            "",
        ]

    targets = _modbus_client_target_lookup(ctx)
    lines = [
        "_MB_CLIENT_IDLE = 0",
        "_MB_CLIENT_CONNECTING = 1",
        "_MB_CLIENT_SENDING = 2",
        "_MB_CLIENT_WAITING = 3",
        "_MB_CLIENT_DONE = 4",
        "_MB_CLIENT_ERROR = 5",
        "",
        "def _mb_client_close(job):",
        "    _sock = job.get('socket')",
        "    if _sock is not None:",
        "        try:",
        "            _sock.close()",
        "        except Exception:",
        "            pass",
        "    job['socket'] = None",
        "",
        "def _mb_client_reset_runtime(job):",
        "    _mb_client_close(job)",
        "    job['request'] = b''",
        "    job['sent_offset'] = 0",
        "    job['rx_len'] = 0",
        "    job['state'] = _MB_CLIENT_IDLE",
        "",
        "def _mb_client_frame_length(data, n):",
        "    if int(n) < 7:",
        "        return None",
        "    try:",
        "        _length = struct.unpack('>H', bytes(data[4:6]))[0]",
        "    except Exception:",
        "        return None",
        "    return 6 + int(_length)",
        "",
        "def _mb_client_pack_register_values(bank, values):",
        "    if bank in ('DS', 'TD', 'SD'):",
        "        return [struct.unpack('<H', struct.pack('<h', int(_value)))[0] for _value in values]",
        "    if bank in ('DD', 'CTD'):",
        "        _regs = []",
        "        for _value in values:",
        "            _regs.extend(struct.unpack('<HH', struct.pack('<i', int(_value))))",
        "        return _regs",
        "    if bank == 'DF':",
        "        _regs = []",
        "        for _value in values:",
        "            _regs.extend(struct.unpack('<HH', struct.pack('<f', float(_value))))",
        "        return _regs",
        "    if bank in ('DH', 'XD', 'YD'):",
        "        return [int(_value) & 0xFFFF for _value in values]",
        "    if bank == 'TXT':",
        "        _regs = []",
        "        _index = 0",
        "        while _index < len(values):",
        "            _lo_raw = values[_index]",
        "            _hi_raw = values[_index + 1] if (_index + 1) < len(values) else ''",
        "            _lo = ord(_lo_raw[0]) if isinstance(_lo_raw, str) and _lo_raw else 0",
        "            _hi = ord(_hi_raw[0]) if isinstance(_hi_raw, str) and _hi_raw else 0",
        "            _regs.append((_lo & 0xFF) | ((_hi & 0xFF) << 8))",
        "            _index += 2",
        "        return _regs",
        "    return [int(_value) & 0xFFFF for _value in values]",
        "",
        "def _mb_client_unpack_register_values(bank, regs, logical_count, plc_start=1):",
        "    if bank in ('DS', 'TD', 'SD'):",
        "        return [struct.unpack('<h', struct.pack('<H', int(_reg) & 0xFFFF))[0] for _reg in regs[:logical_count]]",
        "    if bank in ('DD', 'CTD'):",
        "        _values = []",
        "        for _index in range(0, len(regs), 2):",
        "            if (_index + 1) >= len(regs):",
        "                break",
        "            _values.append(struct.unpack('<i', struct.pack('<HH', int(regs[_index]) & 0xFFFF, int(regs[_index + 1]) & 0xFFFF))[0])",
        "        return _values[:logical_count]",
        "    if bank == 'DF':",
        "        _values = []",
        "        for _index in range(0, len(regs), 2):",
        "            if (_index + 1) >= len(regs):",
        "                break",
        "            _values.append(struct.unpack('<f', struct.pack('<HH', int(regs[_index]) & 0xFFFF, int(regs[_index + 1]) & 0xFFFF))[0])",
        "        return _values[:logical_count]",
        "    if bank in ('DH', 'XD', 'YD'):",
        "        return [(int(_reg) & 0xFFFF) for _reg in regs[:logical_count]]",
        "    if bank == 'TXT':",
        "        _values = []",
        "        for _reg in regs:",
        "            _lo = int(_reg) & 0xFF",
        "            _hi = (int(_reg) >> 8) & 0xFF",
        "            _values.append('' if _lo == 0 else chr(_lo))",
        "            _values.append('' if _hi == 0 else chr(_hi))",
        "        _offset = 0 if (int(plc_start) % 2) == 1 else 1",
        "        return _values[_offset:_offset + int(logical_count)]",
        "    return [(int(_reg) & 0xFFFF) for _reg in regs[:logical_count]]",
        "",
    ]

    if any(s.bank is None for s in specs):
        lines.extend(
            [
                "def _mb_client_raw_pack_register_values(values, tag_types, word_order):",
                "    _regs = []",
                "    for _vi in range(len(values)):",
                "        _tt = tag_types[_vi]",
                "        _v = values[_vi]",
                "        if _tt == 'DINT':",
                "            _bo = '>HH' if word_order == 'high_low' else '<HH'",
                "            _bi = '>i' if word_order == 'high_low' else '<i'",
                "            _regs.extend(struct.unpack(_bo, struct.pack(_bi, int(_v))))",
                "        elif _tt == 'REAL':",
                "            _bo = '>HH' if word_order == 'high_low' else '<HH'",
                "            _bf = '>f' if word_order == 'high_low' else '<f'",
                "            _regs.extend(struct.unpack(_bo, struct.pack(_bf, float(_v))))",
                "        elif _tt == 'INT':",
                "            _regs.append(struct.unpack('<H', struct.pack('<h', int(_v)))[0])",
                "        else:",
                "            _regs.append(int(_v) & 0xFFFF)",
                "    return _regs",
                "",
                "def _mb_client_raw_unpack_register_values(regs, tag_types, word_order):",
                "    _values = []",
                "    _ri = 0",
                "    for _tt in tag_types:",
                "        if _tt == 'DINT':",
                "            _bo = '>HH' if word_order == 'high_low' else '<HH'",
                "            _bi = '>i' if word_order == 'high_low' else '<i'",
                "            _values.append(struct.unpack(_bi, struct.pack(_bo, int(regs[_ri]) & 0xFFFF, int(regs[_ri + 1]) & 0xFFFF))[0])",
                "            _ri += 2",
                "        elif _tt == 'REAL':",
                "            _bo = '>HH' if word_order == 'high_low' else '<HH'",
                "            _bf = '>f' if word_order == 'high_low' else '<f'",
                "            _values.append(struct.unpack(_bf, struct.pack(_bo, int(regs[_ri]) & 0xFFFF, int(regs[_ri + 1]) & 0xFFFF))[0])",
                "            _ri += 2",
                "        elif _tt == 'INT':",
                "            _values.append(struct.unpack('<h', struct.pack('<H', int(regs[_ri]) & 0xFFFF))[0])",
                "            _ri += 1",
                "        else:",
                "            _values.append(int(regs[_ri]) & 0xFFFF)",
                "            _ri += 1",
                "    return _values",
                "",
            ]
        )

    for spec in specs:
        target = targets[spec.target_name]
        lines.extend(_render_client_status_helper(spec))
        lines.extend(_render_client_values_helper(spec))
        lines.extend(_render_client_request_helper(spec, target))
        lines.extend(_render_client_apply_helper(spec, target))
        lines.extend(
            [
                f"{spec.var_name} = {{",
                f"    'name': '{spec.var_name}',",
                "    'enabled': False,",
                "    'state': _MB_CLIENT_IDLE,",
                "    'socket': None,",
                "    'request': b'',",
                "    'sent_offset': 0,",
                "    'rx_buf': bytearray(260),",
                "    'rx_len': 0,",
                "    'deadline': 0.0,",
                "    'tid': 0,",
                f"    'host': '{target.ip}',",
                f"    'port': {target.port},",
                f"    'timeout_s': {target.timeout_ms / 1000.0!r},",
                f"    'build': {spec.var_name}_build_request,",
                f"    'apply': {spec.var_name}_apply_response,",
                f"    'set_status': {spec.var_name}_set_status,",
                "}",
                "",
            ]
        )

    lines.append(f"_mb_client_jobs = [{', '.join(spec.var_name for spec in specs)}]")
    lines.extend(
        [
            "",
            "def service_modbus_client():",
            "    _now = time.monotonic()",
            "    for _job in _mb_client_jobs:",
            "        if not bool(_job['enabled']):",
            "            _mb_client_reset_runtime(_job)",
            "            _job['set_status'](False, False, False, 0)",
            "            continue",
            "        if _job['state'] in (_MB_CLIENT_DONE, _MB_CLIENT_ERROR):",
            "            _mb_client_reset_runtime(_job)",
            "        if _job['state'] == _MB_CLIENT_IDLE:",
            "            _job['tid'] = (int(_job['tid']) + 1) & 0xFFFF",
            "            if _job['tid'] == 0:",
            "                _job['tid'] = 1",
            "            _job['request'] = _job['build'](_job['tid'])",
            "            _job['sent_offset'] = 0",
            "            _job['rx_len'] = 0",
            "            _job['deadline'] = _now + float(_job['timeout_s'])",
            "            _job['set_status'](True, False, False, 0)",
            "            _job['state'] = _MB_CLIENT_CONNECTING",
            "            continue",
            "        if _job['state'] == _MB_CLIENT_CONNECTING:",
            "            if _job['socket'] is None:",
            "                try:",
            "                    _job['socket'] = _mb_socket.socket(_mb_socket.AF_INET, _mb_socket.SOCK_STREAM)",
            "                    _job['socket'].settimeout(0)",
            "                except OSError:",
            "                    _job['set_status'](False, False, True, 0)",
            "                    _job['state'] = _MB_CLIENT_ERROR",
            "                    continue",
            "            try:",
            "                _job['socket'].connect((_job['host'], int(_job['port'])))",
            "            except OSError:",
            "                if _now >= float(_job['deadline']):",
            "                    _job['set_status'](False, False, True, 0)",
            "                    _job['state'] = _MB_CLIENT_ERROR",
            "                    _mb_client_close(_job)",
            "                else:",
            "                    _job['state'] = _MB_CLIENT_CONNECTING",
            "                continue",
            "            _job['state'] = _MB_CLIENT_SENDING",
            "            continue",
            "        if _job['state'] == _MB_CLIENT_SENDING:",
            "            try:",
            "                _sent = int(_job['socket'].send(_job['request'][int(_job['sent_offset']):]))",
            "            except OSError:",
            "                _job['set_status'](False, False, True, 0)",
            "                _job['state'] = _MB_CLIENT_ERROR",
            "                _mb_client_close(_job)",
            "                continue",
            "            if _sent < 0:",
            "                _sent = 0",
            "            _job['sent_offset'] = int(_job['sent_offset']) + _sent",
            "            if int(_job['sent_offset']) >= len(_job['request']):",
            "                _job['state'] = _MB_CLIENT_WAITING",
            "            elif _now >= float(_job['deadline']):",
            "                _job['set_status'](False, False, True, 0)",
            "                _job['state'] = _MB_CLIENT_ERROR",
            "                _mb_client_close(_job)",
            "            continue",
            "        if _job['state'] != _MB_CLIENT_WAITING:",
            "            continue",
            "        try:",
            "            _view = memoryview(_job['rx_buf'])[int(_job['rx_len']):]",
            "            _n = int(_job['socket'].recv_into(_view))",
            "        except OSError:",
            "            _n = 0",
            "        if _n > 0:",
            "            _job['rx_len'] = int(_job['rx_len']) + _n",
            "            _frame_len = _mb_client_frame_length(_job['rx_buf'], _job['rx_len'])",
            "            if _frame_len is not None and int(_job['rx_len']) >= int(_frame_len):",
            "                _ok, _exception = _job['apply'](_job['rx_buf'], int(_frame_len))",
            "                if _ok:",
            "                    _job['set_status'](False, True, False, 0)",
            "                    _job['state'] = _MB_CLIENT_DONE",
            "                else:",
            "                    _job['set_status'](False, False, True, int(_exception))",
            "                    _job['state'] = _MB_CLIENT_ERROR",
            "                _mb_client_close(_job)",
            "                continue",
            "        if _now >= float(_job['deadline']):",
            "            _job['set_status'](False, False, True, 0)",
            "            _job['state'] = _MB_CLIENT_ERROR",
            "            _mb_client_close(_job)",
            "",
        ]
    )
    return lines
