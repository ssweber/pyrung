"""Generate a small pyclickplc-backed Modbus TCP fixture corpus."""

from __future__ import annotations

import json
import struct
from pathlib import Path

from pyclickplc.server import MemoryDataProvider, _ClickDeviceContext
from pymodbus.constants import ExcCodes


def _mbap(tid: int, pdu: bytes, uid: int = 1) -> bytes:
    return struct.pack(">HHHB", tid, 0, len(pdu) + 1, uid) + pdu


def _err(tid: int, uid: int, fc: int, code: int) -> bytes:
    return struct.pack(">HHHBB", tid, 0, 3, uid, (fc & 0x7F) | 0x80) + bytes([code & 0xFF])


def _handle(ctx: _ClickDeviceContext, request: bytes) -> bytes:
    tid, pid, length, uid = struct.unpack(">HHHB", request[:7])
    if pid != 0:
        raise ValueError("fixture generator only supports pid=0")
    fc = int(request[7])

    if fc in (1, 2):
        start, count = struct.unpack(">HH", request[8:12])
        values = ctx.getValues(fc, start, count)
        payload = bytearray((count + 7) // 8)
        for offset, bit in enumerate(values):
            if bit:
                payload[offset // 8] |= 1 << (offset % 8)
        return struct.pack(">HHHBBB", tid, 0, len(payload) + 3, uid, fc, len(payload)) + bytes(
            payload
        )

    if fc in (3, 4):
        start, count = struct.unpack(">HH", request[8:12])
        values = ctx.getValues(fc, start, count)
        payload = bytearray()
        for reg in values:
            payload.extend(struct.pack(">H", int(reg) & 0xFFFF))
        return struct.pack(">HHHBBB", tid, 0, len(payload) + 3, uid, fc, len(payload)) + bytes(
            payload
        )

    if fc == 5:
        address, raw = struct.unpack(">HH", request[8:12])
        result = ctx.setValues(fc, address, [raw == 0xFF00])
        if result is not None:
            return _err(tid, uid, fc, int(result))
        return request[:12]

    if fc == 6:
        address, raw = struct.unpack(">HH", request[8:12])
        result = ctx.setValues(fc, address, [raw])
        if result is not None:
            return _err(tid, uid, fc, int(result))
        return request[:12]

    if fc == 15:
        start, count, byte_count = struct.unpack(">HHB", request[8:13])
        payload = request[13 : 13 + byte_count]
        values = [bool((payload[offset // 8] >> (offset % 8)) & 0x1) for offset in range(count)]
        result = ctx.setValues(fc, start, values)
        if result is not None:
            return _err(tid, uid, fc, int(result))
        return struct.pack(">HHHBBHH", tid, 0, 6, uid, fc, start, count)

    if fc == 16:
        start, count, byte_count = struct.unpack(">HHB", request[8:13])
        payload = request[13 : 13 + byte_count]
        values = [
            struct.unpack(">H", payload[offset : offset + 2])[0]
            for offset in range(0, len(payload), 2)
        ]
        result = ctx.setValues(fc, start, values)
        if result is not None:
            return _err(tid, uid, fc, int(result))
        return struct.pack(">HHHBBHH", tid, 0, 6, uid, fc, start, count)

    return _err(tid, uid, fc, int(ExcCodes.ILLEGAL_FUNCTION))


def main() -> None:
    provider = MemoryDataProvider()
    provider.bulk_set(
        {
            "C1": True,
            "DS1": 7,
            "Y1": True,
            "YD0": 0x1234,
        }
    )
    ctx = _ClickDeviceContext(provider)

    cases = [
        ("read_c1", _mbap(1, struct.pack(">BHH", 1, 16384, 1))),
        ("read_ds1", _mbap(2, struct.pack(">BHH", 3, 0, 1))),
        ("read_valid_unmapped_ds2", _mbap(3, struct.pack(">BHH", 3, 1, 1))),
        ("write_c1_false", _mbap(4, struct.pack(">BHH", 5, 16384, 0x0000))),
        ("write_ds1_42", _mbap(5, struct.pack(">BHH", 6, 0, 42))),
        ("write_x1_illegal", _mbap(6, struct.pack(">BHH", 5, 0, 0xFF00))),
        ("read_yd0", _mbap(7, struct.pack(">BHH", 3, 57856, 1))),
    ]

    fixtures = [
        {
            "name": name,
            "request_hex": request.hex(),
            "response_hex": _handle(ctx, request).hex(),
        }
        for name, request in cases
    ]

    out_path = Path(__file__).with_name("modbus_fixtures.json")
    out_path.write_text(json.dumps(fixtures, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
