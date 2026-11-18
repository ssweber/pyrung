"""The generated Modbus server answers as a CLICK did (``tests/conformance/test_modbus_raw.py``).

Each test sends one of that part's raw requests to the generated code and checks the CLICK's
answer.  Most answers are recorded in ``tests/conformance/readings.toml`` on 2026-11-10.  A
CLICK C2-03CPU with firmware 4.00 gave the answers to the writes of X, T, CT and gap coils, the
reads past XD8 and YD8, and the DF written one word at a time.
"""

from __future__ import annotations

import struct

import pytest
from pyclickplc.modbus import plc_to_modbus

from pyrung import (
    Block,
    Bool,
    Dint,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    copy,
    fill,
    math,
    out,
    unpack_to_words,
)
from pyrung.circuitpy import P1AM, generate_circuitpy
from pyrung.click import ClickBlocks, TagMap
from tests.circuitpy.test_codegen import _run_single_scan_source
from tests.circuitpy.test_modbus_codegen import _client_source, _ctx_and_source, _IdleBase, _mbap

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

XD0, YD0 = 57344, 57856


def _addr(bank: str, index: int) -> int:
    return plc_to_modbus(bank, index)[0]


def _server(monkeypatch, mapping: TagMap, prog: Program | None = None):
    if prog is None:
        with Program(strict=False) as prog:
            pass
    result = _ctx_and_source(prog, mapping)
    ns = _run_single_scan_source(
        result.code, monkeypatch, _IdleBase(), runtime_source=result.runtime
    )
    tid = [0]

    def send(pdu: bytes) -> bytes:
        tid[0] += 1
        request = _mbap(tid[0], pdu)
        return ns["_mb_handle"](request, len(request))

    return ns, send


def _exception(answer: bytes) -> int | None:
    """Return the exception code of *answer*, or ``None`` for a normal answer."""
    return answer[8] if answer[7] & 0x80 else None


def _read_bits(send, fc: int, start: int, count: int) -> list[int] | int:
    answer = send(struct.pack(">BHH", fc, start, count))
    if _exception(answer) is not None:
        return _exception(answer)
    payload = answer[9:]
    return [(payload[i // 8] >> (i % 8)) & 1 for i in range(count)]


def _read_regs(send, fc: int, start: int, count: int) -> list[int] | int:
    answer = send(struct.pack(">BHH", fc, start, count))
    if _exception(answer) is not None:
        return _exception(answer)
    return list(struct.unpack(f">{count}H", answer[9:]))


def _fc15(start: int, bits: list[bool]) -> bytes:
    payload = bytearray((len(bits) + 7) // 8)
    for i, bit in enumerate(bits):
        if bit:
            payload[i // 8] |= 1 << (i % 8)
    return struct.pack(">BHHB", 15, start, len(bits), len(payload)) + bytes(payload)


def _fc16(start: int, words: list[int]) -> bytes:
    return struct.pack(f">BHHB{len(words)}H", 16, start, len(words), 2 * len(words), *words)


# -- A write the CLICK refuses anywhere in its range stores nothing -----------------------


def test_fc16_past_the_ds_bank_stores_nothing(monkeypatch):
    last = Int("Last")
    _ns, send = _server(monkeypatch, TagMap({last: ds[4500]}))
    assert _exception(send(_fc16(_addr("DS", 4500), [7, 8]))) == 2
    assert _read_regs(send, 3, _addr("DS", 4500), 1) == [0]


def test_fc16_over_a_cell_no_register_can_hold_stores_nothing(monkeypatch):
    ptr = Int("Ptr", default=1)
    with Program(strict=False) as prog:
        with Rung():
            copy(5, dd[ptr])
    ns, send = _server(monkeypatch, TagMap({ptr: ds[1]}), prog)
    # A Dint past 32 bits in DD2, an array cell.
    ns["_b_DD"][1] = 2**40
    assert _exception(send(_fc16(_addr("DD", 1), [1, 0, 2, 0]))) == 2
    assert ns["_b_DD"][0] == 5


def test_fc16_over_a_tag_no_register_can_hold_stores_nothing(monkeypatch):
    low, wide = Dint("Low"), Dint("Wide")
    ns, send = _server(monkeypatch, TagMap({low: dd[2], wide: dd[3]}))
    ns["_t_Wide"] = 2**40
    assert _exception(send(_fc16(_addr("DD", 2), [1, 0, 2, 0]))) == 2
    assert ns["_t_Low"] == 0


def test_fc15_past_the_c_bank_stores_nothing(monkeypatch):
    bits = Block("Bits", TagType.BOOL, 1, 2)
    _ns, send = _server(monkeypatch, TagMap({bits: c.select(1999, 2000)}))
    assert _exception(send(_fc15(_addr("C", 1999), [True] * 4))) == 2
    assert _read_bits(send, 1, _addr("C", 1999), 2) == [0, 0]


def test_fc15_over_a_writable_and_a_read_only_sc_stores_nothing(monkeypatch):
    _ns, send = _server(monkeypatch, TagMap())
    assert _exception(send(_fc15(_addr("SC", 53), [True, False]))) == 2
    assert _read_bits(send, 2, _addr("SC", 53), 2) == [0, 0]


# -- A write to a read-only SC or SD answers 02 --------------------------------------------


@pytest.mark.parametrize(
    "pdu",
    [
        struct.pack(">BHH", 5, _addr("SC", 1), 0x0000),
        struct.pack(">BHH", 5, _addr("SC", 54), 0xFF00),
        _fc15(_addr("SC", 54), [True]),
        struct.pack(">BHH", 6, _addr("SD", 5), 1),
        _fc16(_addr("SD", 1), [99]),
    ],
    ids=["fc05-sc1", "fc05-sc54", "fc15-sc54", "fc06-sd5", "fc16-sd1"],
)
def test_a_write_to_a_read_only_system_point_answers_02(monkeypatch, pdu):
    _ns, send = _server(monkeypatch, TagMap())
    assert _exception(send(pdu)) == 2


def test_a_write_to_a_writable_system_point_is_stored(monkeypatch):
    _ns, send = _server(monkeypatch, TagMap())
    assert _exception(send(struct.pack(">BHH", 6, _addr("SD", 29), 2027))) is None
    assert _read_regs(send, 4, _addr("SD", 29), 1) == [2027]
    assert _exception(send(struct.pack(">BHH", 5, _addr("SC", 55), 0xFF00))) is None
    assert _read_bits(send, 2, _addr("SC", 55), 1) == [1]


# -- XD and YD, in CLICK's layout ----------------------------------------------------------


def test_xd_and_yd_serve_17_registers_with_no_x_or_y_mapped(monkeypatch):
    _ns, send = _server(monkeypatch, TagMap())
    assert _read_regs(send, 4, XD0, 17) == [0] * 17
    assert _read_regs(send, 3, YD0, 17) == [0] * 17


def test_the_register_after_xd8_or_yd8_reads_0(monkeypatch):
    _ns, send = _server(monkeypatch, TagMap())
    for fc, first in ((4, XD0), (3, YD0)):
        assert _read_regs(send, fc, first + 17, 1) == [0]
        assert _read_regs(send, fc, first + 16, 2) == [0, 0]
        assert _read_regs(send, fc, first, 18) == [0] * 18
    assert _exception(send(struct.pack(">BHH", 6, YD0 + 17, 1))) == 2


def test_xd_words_follow_their_x_slots(monkeypatch):
    lower = Block("Lower", TagType.BOOL, 1, 16)
    upper = Block("Upper", TagType.BOOL, 1, 16)
    slot1 = Block("Exp1", TagType.BOOL, 1, 16)
    for block in (lower, upper, slot1):
        block.slot(1, default=True)
    mapping = TagMap({lower: x.select(1, 16), upper: x.select(21, 36), slot1: x.select(101, 116)})
    _ns, send = _server(monkeypatch, mapping)
    # XD0, XD0u, XD1, the spare word after XD1, XD2.
    assert _read_regs(send, 4, XD0, 5) == [1, 1, 1, 0, 0]
    assert _exception(send(struct.pack(">BHH", 6, XD0, 1))) == 2


def test_a_spare_yd_register_keeps_its_own_word(monkeypatch):
    _ns, send = _server(monkeypatch, TagMap())
    assert _exception(send(struct.pack(">BHH", 6, YD0 + 3, 0x8000))) is None
    assert _read_regs(send, 3, YD0 + 2, 2) == [0x0000, 0x8000]


def test_a_yd_word_with_no_y_mapped_keeps_its_value(monkeypatch):
    _ns, send = _server(monkeypatch, TagMap())
    assert _exception(send(_fc16(YD0 + 2, [0x1234, 0, 0x00FF]))) is None
    assert _read_regs(send, 3, YD0, 5) == [0, 0, 0x1234, 0, 0x00FF]


def test_a_yd_write_sets_the_mapped_y_bits_of_its_slot(monkeypatch):
    slot1 = Block("Exp1", TagType.BOOL, 1, 16)
    ns, send = _server(monkeypatch, TagMap({slot1: y.select(101, 116)}))
    assert _exception(send(struct.pack(">BHH", 6, YD0 + 2, 0x8001))) is None
    bits = ns["_b_Exp1"]
    assert [i for i, bit in enumerate(bits) if bit] == [0, 15]
    bits[0] = False
    assert _read_regs(send, 3, YD0 + 2, 1) == [0x8000]


# -- A read with the other function code, and the Y gap ------------------------------------


def test_reads_with_the_other_function_code_answer(monkeypatch):
    _ns, send = _server(monkeypatch, TagMap())
    assert _read_regs(send, 3, XD0, 1) == [0]
    assert _read_bits(send, 1, _addr("X", 1), 1) == [0]
    assert _read_bits(send, 2, _addr("C", 1), 1) == [0]


def test_a_df_word_written_alone_keeps_the_other_word(monkeypatch):
    ns, send = _server(monkeypatch, TagMap({Real("Level"): df[1]}))
    first = _addr("DF", 1)
    # The high word of a NaN, then 2.5 one word at a time, low word first.
    for offset, word, after in (
        (1, 0x7FC0, [0x0000, 0x7FC0]),
        (0, 0x0000, [0x0000, 0x7FC0]),
        (1, 0x4020, [0x0000, 0x4020]),
    ):
        request = struct.pack(">BHH", 6, first + offset, word)
        assert send(request)[7:] == request
        assert _read_regs(send, 3, first, 2) == after
    assert ns["_t_Level"] == 2.5


def test_a_nan_that_a_math_computes_reads_all_ones(monkeypatch):
    # A CLICK reads 0xFFFF:0xFFFF from a DF that Inf - Inf filled (modbus_raw/staged_nan).
    big = Real("Big", default=3.0e38)
    inf = Real("Inf")
    level = Real("Level")
    with Program(strict=False) as prog:
        with Rung():
            math(big * 10, inf)
            math(inf - inf, level)
            copy(inf - inf, df[2])
    _ns, send = _server(monkeypatch, TagMap({level: df[1]}), prog)
    assert _read_regs(send, 3, _addr("DF", 1), 4) == [0xFFFF] * 4


def test_an_unpack_copy_of_a_nan_that_a_client_wrote_keeps_its_bits(monkeypatch):
    # A CLICK unpacks the NaN 0x0000:0x7FC0 to those words (modbus_raw/df_quiet_nan).
    level = Real("Level")
    with Program(strict=False) as prog:
        with Rung():
            unpack_to_words(level, Block("W", TagType.WORD, 1, 2).select(1, 2))
            copy(level, df[2])
    result = _ctx_and_source(prog, TagMap({level: df[1]}))
    request = _mbap(1, _fc16(_addr("DF", 1), [0x0000, 0x7FC0]))
    # The request arrives after the first scan.  The second scan unpacks and copies it.
    serve = f"if __scan_once == 1: _t_answer = _rt._mb_handle({request!r}, {len(request)})"
    code = result.code.replace("while True:\n", f"while True:\n    {serve}\n", 1)
    ns = _run_single_scan_source(
        code, monkeypatch, _IdleBase(), runtime_source=result.runtime, scans=2
    )
    assert ns["_b_W"][:2] == [0x0000, 0x7FC0]
    read = _mbap(2, struct.pack(">BHH", 3, _addr("DF", 1), 4))
    assert list(struct.unpack(">4H", ns["_mb_handle"](read, len(read))[9:])) == [0, 0x7FC0] * 2


def test_a_read_in_the_y_gap_answers_0(monkeypatch):
    _ns, send = _server(monkeypatch, TagMap())
    # Unit 17 of expansion slot 1, between Y116 and Y201.
    assert _read_bits(send, 1, 8192 + 32 + 16, 1) == [0]


def test_a_read_past_a_bank_end_answers_02(monkeypatch):
    last = Int("Last")
    bit = Bool("Bit")
    _ns, send = _server(monkeypatch, TagMap({last: ds[4500], bit: c[2000]}))
    assert _read_regs(send, 3, _addr("DS", 4500) + 1, 1) == 2
    assert _read_regs(send, 3, _addr("DS", 4500), 2) == 2
    assert _read_bits(send, 1, _addr("C", 2000) + 1, 1) == 2


def test_a_cell_a_rung_reaches_by_pointer_is_served(monkeypatch):
    ptr = Int("Ptr", default=7)
    with Program(strict=False) as prog:
        with Rung():
            copy(99, ds[ptr])
            copy(70000, dd[ptr])
    ns, send = _server(monkeypatch, TagMap({ptr: ds[1]}), prog)
    assert _read_regs(send, 3, _addr("DS", 7), 1) == [99]
    assert _read_regs(send, 3, _addr("DD", 7), 2) == [70000 & 0xFFFF, 70000 >> 16]
    assert _exception(send(_fc16(_addr("DS", 8), [5, 0xFFFF]))) is None
    assert ns["_b_DS"][7:9] == [5, -1]
    assert _exception(send(_fc16(_addr("DD", 8), [1, 1]))) is None
    assert ns["_b_DD"][7] == 65537


def test_a_float_and_a_text_cell_reached_by_pointer_are_served(monkeypatch):
    ptr = Int("Ptr", default=3)
    with Program(strict=False) as prog:
        with Rung():
            copy(1.5, df[ptr])
            copy("A", txt[ptr])
    ns, send = _server(monkeypatch, TagMap({ptr: ds[1]}), prog)
    assert _read_regs(send, 3, _addr("DF", 3), 2) == list(
        struct.unpack("<HH", struct.pack("<f", 1.5))
    )
    # TXT3 is the low byte of the register it shares with TXT4.
    assert _read_regs(send, 3, _addr("TXT", 3), 1) == [ord("A")]
    assert _exception(send(_fc16(_addr("TXT", 5), [ord("B") | ord("C") << 8]))) is None
    assert ns["_b_TXT"][4:6] == ["B", "C"]


def test_a_cell_a_block_instruction_reaches_is_served(monkeypatch):
    with Program(strict=False) as prog:
        with Rung():
            fill(True, c.select(1, 3))
    ns, send = _server(monkeypatch, TagMap(), prog)
    assert _read_bits(send, 2, _addr("C", 1), 4) == [1, 1, 1, 0]
    assert _exception(send(_fc15(_addr("C", 1999), [True, True]))) is None
    assert ns["_b_C"][1998:2000] == [True, True]
    # Past C2000, the whole write is refused.
    assert _exception(send(_fc15(_addr("C", 1), [False] * 2001))) == 3
    assert _exception(send(_fc15(_addr("C", 1999), [False] * 3))) == 2
    assert ns["_b_C"][1998:2000] == [True, True]


# -- X and Y gaps, and writes of X, T and CT -----------------------------------------------


def test_a_read_between_x_or_y_slots_answers_0(monkeypatch):
    _ns, send = _server(monkeypatch, TagMap())
    # Units 17..32 of slot 1, and of slot 8, which ends the bank.
    for base in (0, 8192):
        assert _read_bits(send, 1 if base else 2, base + 48, 16) == [0] * 16
        assert _read_bits(send, 1 if base else 2, base + 272, 16) == [0] * 16
        assert _read_bits(send, 1 if base else 2, base + 288, 1) == 2


def test_a_write_between_x_or_y_slots_is_stored(monkeypatch):
    _ns, send = _server(monkeypatch, TagMap())
    # Y115 and Y116, the 16 gap coils after them, then Y201 and Y202.
    start = _addr("Y", 115)
    assert _exception(send(_fc15(start, [True] * 20))) is None
    assert _read_bits(send, 1, start, 20) == [1] * 20
    # X021, then a gap coil of X slots 1 and 8 and of Y slots 2 and 8.
    for coil in (16, 48, 272, 8272, 8464):
        request = struct.pack(">BHH", 5, coil, 0xFF00)
        assert send(request)[7:] == request
        assert _read_bits(send, 1, coil, 1) == [1]
        assert _read_bits(send, 2, coil, 1) == [1]
        assert _exception(send(struct.pack(">BHH", 5, coil, 0x0000))) is None
        assert _read_bits(send, 1, coil, 1) == [0]


def test_an_x_write_is_stored_with_or_without_a_tag(monkeypatch):
    start = Bool("Start")
    _ns, send = _server(monkeypatch, TagMap({start: x[1]}))
    # X101..X132 runs across slot 1's gap coils.
    assert _exception(send(_fc15(_addr("X", 101), [True] * 32))) is None
    assert _read_bits(send, 2, _addr("X", 101), 32) == [1] * 32
    request = struct.pack(">BHH", 5, _addr("X", 1), 0xFF00)
    assert send(request)[7:] == request
    assert _read_bits(send, 1, _addr("X", 1), 1) == [1]


@pytest.mark.parametrize("bank", ["T", "CT"])
def test_a_rung_reads_a_t_or_ct_bit_that_a_client_wrote(monkeypatch, bank):
    hardware = {"T": t, "CT": ct}[bank]
    with Program(strict=False) as prog:
        with Rung(hardware[1]):
            out(c[1])
    result = _ctx_and_source(prog, TagMap())
    request = _mbap(1, struct.pack(">BHH", 5, _addr(bank, 1), 0xFF00))
    # The request arrives after the first scan.  The second scan reads the bit.
    serve = f"if __scan_once == 1: _t_answer = _rt._mb_handle({request!r}, {len(request)})"
    code = result.code.replace("while True:\n", f"while True:\n    {serve}\n", 1)
    ns = _run_single_scan_source(
        code, monkeypatch, _IdleBase(), runtime_source=result.runtime, scans=2
    )
    assert ns["_t_answer"] == request
    assert ns["_t_C1"] is True


# -- Code size -----------------------------------------------------------------------------


def test_a_program_with_no_server_has_no_server_code():
    with Program(strict=False) as prog:
        pass
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    plain = generate_circuitpy(prog, hw, target_scan_ms=10.0)
    client = _client_source(prog)
    for code in (plain if isinstance(plain, str) else plain.code, client.code):
        assert "_MB_COIL_MAP" not in code
        assert "def _mb_write_regs" not in code
