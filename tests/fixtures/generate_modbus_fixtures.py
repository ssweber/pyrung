"""Generate a small pyclickplc-backed Modbus TCP fixture corpus."""

from __future__ import annotations

import json
import struct
from pathlib import Path

from pyclickplc.server import MemoryDataProvider, _answer, _ClickDeviceContext


def _mbap(tid: int, pdu: bytes, uid: int = 1) -> bytes:
    return struct.pack(">HHHB", tid, 0, len(pdu) + 1, uid) + pdu


def _handle(ctx: _ClickDeviceContext, request: bytes) -> bytes:
    """Return the frame that pyclickplc's ``ClickServer`` sends back for *request*."""
    tid, _pid, _length, uid = struct.unpack(">HHHB", request[:7])
    pdu = _answer(ctx, request[7:])
    return struct.pack(">HHHB", tid, 0, len(pdu) + 1, uid) + pdu


def _fc15(start: int, bits: list[bool]) -> bytes:
    payload = bytearray((len(bits) + 7) // 8)
    for i, bit in enumerate(bits):
        if bit:
            payload[i // 8] |= 1 << (i % 8)
    return struct.pack(">BHHB", 15, start, len(bits), len(payload)) + bytes(payload)


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
        ("write_x1", _mbap(6, struct.pack(">BHH", 5, 0, 0xFF00))),
        ("read_yd0", _mbap(7, struct.pack(">BHH", 3, 57856, 1))),
        ("read_x1", _mbap(8, struct.pack(">BHH", 2, 0, 1))),
        ("write_t1", _mbap(9, struct.pack(">BHH", 5, 45056, 0xFF00))),
        ("read_t1", _mbap(10, struct.pack(">BHH", 1, 45056, 1))),
        ("write_ct1", _mbap(11, _fc15(49152, [True]))),
        ("read_ct1", _mbap(12, struct.pack(">BHH", 2, 49152, 1))),
        # Y115 and Y116, the 16 gap coils after them, then Y201 and Y202.
        ("write_y_gap", _mbap(13, _fc15(8238, [True] * 20))),
        ("read_y_gap", _mbap(14, struct.pack(">BHH", 1, 8238, 20))),
        # Unit 17 of slot 1, after X116.
        ("write_x_gap", _mbap(15, struct.pack(">BHH", 5, 48, 0xFF00))),
        ("read_x_gap", _mbap(16, struct.pack(">BHH", 2, 48, 1))),
        ("read_past_xd8", _mbap(17, struct.pack(">BHH", 4, 57361, 1))),
        ("read_xd8_and_past", _mbap(18, struct.pack(">BHH", 4, 57360, 2))),
        ("read_past_yd8", _mbap(19, struct.pack(">BHH", 3, 57873, 1))),
        # XD1 through the register after XD8.
        ("read_xd1_to_past_xd8", _mbap(20, struct.pack(">BHH", 4, 57346, 16))),
        # DF1 as NaN (0x0000:0x7FC0), then 2.5 written low word first, one word at a time.
        ("write_df1_high_nan", _mbap(21, struct.pack(">BHH", 6, 28673, 0x7FC0))),
        ("read_df1_nan", _mbap(22, struct.pack(">BHH", 3, 28672, 2))),
        ("write_df1_low", _mbap(23, struct.pack(">BHH", 6, 28672, 0x0000))),
        ("read_df1_still_nan", _mbap(24, struct.pack(">BHH", 3, 28672, 2))),
        ("write_df1_high", _mbap(25, struct.pack(">BHH", 6, 28673, 0x4020))),
        ("read_df1_2_5", _mbap(26, struct.pack(">BHH", 3, 28672, 2))),
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
