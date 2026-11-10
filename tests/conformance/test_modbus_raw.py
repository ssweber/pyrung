"""Raw Modbus requests, and what the ladder saw of each write: part ``modbus_raw``.

**It writes into a real CLICK outside the part's memory.**  Do not run it on a CLICK whose
memory matters.  These are the writes outside the part's memory:

- DD1531 (past the DD bank) through a pointer, and Modbus 19444, which may be the same memory.
  The run ends by writing 0 to DD1531, whatever it held before the run.
  ``faults/pointer_past_bank_lands`` (part ``system``) expects to read 0 there.  A run that dies
  between steps 2 and 4 of ``past_bank_alias`` may leave 1234 there.  The next run, or
  ``past_clean``, writes 0 again.
- SD29, SD31 and SD32 get today's date.  Then SC53 goes on and sets the PLC's date from them.
  SD29 also gets the year after the one it holds.  Each is written back.
- C1..C1969, written with the values they read just before.
- YD register 3, only with ``PYRUNG_CONFORMANCE_OUTPUTS=1``.  It may turn Y116 on until it is
  written back.
- SC51 and SC50, only with ``PYRUNG_CONFORMANCE_STOP=1``.  The SC50 write asks whether a CLICK
  takes it and stops.  The simulator and the P1AM code refuse both writes with exception
  code 2.

The simulator and the P1AM code have no memory past the DD bank.

The rest of the suite reads and writes slot DS only, through pyclickplc's client.  This part
asks what a CLICK answers to the requests that client never sends or refuses to send.  Its
ladder stages values for the harness to read raw.  After each raw write the ladder echoes what
it then holds, so a reading tells "the write was stored" apart from "the server answered OK".
``test_raw_requests`` sends the requests with ``pymodbus`` and records every answer.  An
answer is the registers or bits, an exception code, or ``timeout``.  It reads each value twice,
raw and through pyclickplc's ``ClickClient``, which decodes it or raises.  The entry
``modbus_raw/disagree`` lists each read where pyclickplc's value is not the raw bits read with
CLICK's documented word and byte order (``_as_click_stores``).  Nothing here is asserted against an answer.  On a
CLICK each probe goes into ``readings.toml``.  On a local target it goes into
``build/modbus_raw-readings/<target>.toml``, for setting the two side by side.

The ladder (case ``raw_values``), on the scan ``Cmd`` arrives:

- DF102 = +Inf, DF103 = -Inf, DF104 = NaN (+Inf - +Inf), DF105 = 2.5, DD201 = 0x00010002.
- TXT401 = 'A', TXT402 = 'B' and TXT403 = the byte 200 (a Copy Binary).
- The write targets: DF111..DF113 = 2.5, DF114 and DF115 = NaN, DD203 and DD205 = 0x00010002,
  DD206 = 0x00030004, TXT405 = 'Y' and TXT406 = 'Z'.

The ladder echoes three things on the staging scan, and on every scan while ``Cmd`` is above 1.

- It unpacks each DF and DD above into two DH, low word first (an Unpack Copy).
- It copies each TXT into a DS (a Copy to ASCII).
- It scores each write-target DF into DS4071..DS4075.  The score is 1 if the DF equals itself
  (not NaN), plus 2 if it is above 3.0E38, plus 4 if it equals 2.5.

On every scan, the last rung copies ``Cmd`` into ``Scratch``.  The harness writes, then writes
a new ``Cmd``, and reads the echo once ``Scratch`` holds it.

The probes, each one entry (``modbus_raw/<probe>``):

- ``staged_*``: the staged DF, DD and TXT registers, raw.  They show NaN's and Inf's words, the
  word order of a DD and a DF, and which byte of a register holds the lower TXT.
- ``df_all_ones``, ``df_quiet_nan``, ``df_inf``: FC16 of 0xFFFF:0xFFFF, 0x0000:0x7FC0 and
  0x0000:0x7F80 into DF111..DF113 (the low word first, as a CLICK's word order would have it).
- ``df_half_first``, ``df_half_second``: FC06 of one word over a stored NaN, each half.
- ``txt_high_byte``: FC06 of 0xC841 into the TXT405/TXT406 register.
- ``dd_half``: FC06 of 0x0005 to DD203's first register.
- ``dd_straddle``: FC16 of 0x0009, 0x0008 over DD205's second register and DD206's first.
- ``fc16_past_ds``: FC16 of 7, 8 at DS4500 and the register past it.  ``fc15_sc53_sc54``: FC15
  over SC53 (writable) and SC54 (not).  Each records the exception and whether the first
  target changed.  Before SC53 goes on, the harness sets SD29, SD31 and SD32 (the date SC53
  applies) to today's date from SD19, SD21 and SD22.  It does so only if SC53 reads 0 and the
  old date reads, so it can write the old date back.  Otherwise, or if the date does not read
  back as today, it writes SC53 off, and the probe can only show the exception.
- ``fc03_past_ds``, ``fc03_across_ds_end``, ``fc01_past_c2000``, ``fc01_y_gap``: reads past a bank
  or in a gap (the register past DS4500, DS4500 and that register, C2001, between Y116 and Y201).
  ``fc15_across_c_end``: FC15 of four ON over C1999..C2002.
- ``fc03_at_xd``, ``fc04_at_ds1``, ``fc01_at_x001``, ``fc02_at_c1``: a bank read with the other
  function code.
- ``xd_x17``, ``yd_x17``: FC04 of 17 registers from XD0, FC03 of 17 from YD0, the odd registers
  between the documented ones included.  ``yd_odd_write``: FC06 of 0x8000 to YD's register 3,
  then YD1 read back, only with ``PYRUNG_CONFORMANCE_OUTPUTS=1``.  If it lands on YD1 it turns
  Y116 on until the harness writes it back.
- ``sd29_write``: FC06 to SD29 of a year one past what it holds, then FC04 back, then the old
  value written back.  It is sent only while SC53 reads 0.
- ``past_bank_alias``: whether DD1531, which a pointer reaches past the bank, is the memory at
  Modbus 19444 (DD1 is 16384, two registers each).  FC03 x2 at 18384 (DD1001) and 19444 first.
  Then the ladder reads DD1531 through a pointer (DS10 = 1531) and writes 1234 there only if it
  read 0.  FC03 at 18384 and 19444 again.  The ladder stops writing, then the harness sends
  FC16 of 4321 to 19444, and the ladder reads DD1531 back.  If a server took the FC16, the
  harness writes the old registers back.  Last, the ladder writes 0 to DD1531 whatever
  happened.  The probe also asks whether DH follows DD in memory, two DH to a DD (so DD1064
  would be DH127 and DH128).  The ladder sets DH127 = 0x1357
  and DH128 = 0x2468, then reads DD1064 through the same pointer into DD207.  DD207 reads
  0x24681357 if DH127 is DD1064's low word, and 0x13572468 if it is the high word.
- ``fc03_x126``, ``fc03_x200``, ``fc15_x1969``: requests past Modbus's limits (125 registers,
  1968 coils), which a server should refuse with exception 03.  The FC15 writes C1..C1969 with
  what they read just before.
- ``sc51_watchdog_reset``, ``sc50_stop``: only with ``PYRUNG_CONFORMANCE_STOP=1``, last.
  ``sc50_stop`` asks whether a CLICK takes the SC50 write and stops.  The simulator and the P1AM
  code refuse it with exception code 2.  The probe records the exception, then SC11.  If the
  PLC stopped, put it back in RUN.

Each probe writes only the part's own memory, or restores what it wrote in a ``finally``.  The
exceptions are the writes a CLICK should refuse, and DD1531.  The reset (``Cmd = 0``) clears the
part's memory at the end, even after a probe fails.  The readings are recorded even if the run
dies.
"""

from __future__ import annotations

import asyncio
import os
import struct
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from pyrung.conformance import Endpoint, Group, assert_all_passed, case, read_program_hash, run
from pyrung.core import Or, Rung, copy, fill, math, reset, to_ascii, to_binary, unpack_to_words
from tests.conformance.copy.test_past_bank_write import ADDRESS
from tests.conformance.memory import c, dd, df, dh, ds, txt
from tests.conformance.probe_kit import between, class_of, finish, once, record_raw_readings

GROUP = "modbus_raw"
CASE = "raw_values"

#: Each staged or written DF and DD, and the first of the two DH its Unpack Copy fills.
ECHO_WORDS = {
    "DF102": 101,
    "DF103": 103,
    "DF104": 105,
    "DF105": 107,
    "DD201": 109,
    "DF111": 111,
    "DF112": 113,
    "DF113": 115,
    "DF114": 117,
    "DF115": 119,
    "DD203": 121,
    "DD205": 123,
    "DD206": 125,
}
#: Each TXT, and the DS its Copy to ASCII fills.
ECHO_TEXT = {401: 4061, 402: 4062, 403: 4063, 405: 4065, 406: 4066}
#: The write-target DF scored into DS4071..DS4075.
SCORED = range(111, 116)
#: Where the scores start.
SCORE_DS = 4071
#: The DS that holds 200 for the Copy Binary into TXT403.
BYTE_DS = 4067
LAST_DS = 4500
#: The DS of ``past_bank_alias``.  They hold the step the harness asks of the ladder, DD1531 as
#: the ladder found it, and DD1531 as the ladder reads it back.
PAST_OP_DS, PAST_FOUND_DS, PAST_BACK_DS = 4068, 4069, 4070
#: What the ladder writes through the pointer, and what the harness writes over Modbus.
LADDER_VALUE, MODBUS_VALUE = 1234, 4321
#: If DH follows DD in a CLICK's memory, two DH to a DD, then DD1064 is DH127 and DH128.
#: The ladder sets that pair to a pattern and reads DD1064 through the pointer into DD207.
AFTER_DD, PATTERN = 1064, (0x1357, 0x2468)
DH_PAIR = (2 * (AFTER_DD - 1000) - 1, 2 * (AFTER_DD - 1000))
AFTER_INTO_DD = 207
AFTER_INTO = dd[AFTER_INTO_DD]
# The pointer's choices bound it, or the build would take a write through it for a write to the
# whole DD bank.  Choices hold for every build, so the pointer is DS10, which no group writes.
POINTER_DS = 10
POINTER = ds[POINTER_DS]
ds.slot(
    POINTER_DS,
    choices={0: "unset", AFTER_DD: "DH127 if DH follows DD", ADDRESS: "past the DD bank"},
)


def _word_source(name: str) -> Any:
    bank = {"DF": df, "DD": dd}[name[:2]]
    return bank[int(name[2:])]


def _past_bank_steps(slot: Any) -> None:
    """The ladder's half of ``past_bank_alias``, one step for each value of DS4068.

    Each step does the same on every scan it is held, so the harness waits for one scan and
    reads.  Step 1 reads DD1531 as found, and DD1064 after the DH pattern.  Step 2 writes 1234
    to DD1531 only if step 1 found 0, on every scan it is held.  Step 3 only reads.  Step 4
    writes 0 there whatever it holds.
    """
    step, found, back = ds[PAST_OP_DS], ds[PAST_FOUND_DS], ds[PAST_BACK_DS]
    with Rung(slot.Cmd != 0, step != 0):
        copy(PATTERN[0], dh[DH_PAIR[0]])
        copy(PATTERN[1], dh[DH_PAIR[1]])
        copy(AFTER_DD, POINTER)
    with Rung(slot.Cmd != 0, step == 1):
        copy(dd[POINTER], AFTER_INTO)
    with Rung(slot.Cmd != 0, step != 0):
        copy(ADDRESS, POINTER)
    with Rung(slot.Cmd != 0, step == 1):
        copy(dd[POINTER], found)
    with Rung(slot.Cmd != 0, step == 2, found == 0):
        copy(LADDER_VALUE, dd[POINTER])
    with Rung(slot.Cmd != 0, step == 4):
        copy(0, dd[POINTER])
    with Rung(slot.Cmd != 0, step >= 2):
        copy(dd[POINTER], back)


def prove_raw_values(slot: Any) -> None:
    nan = df[102] - df[102]
    with Rung(*once(slot)):
        copy(3.0e38, df[101])
        math(df[101] * 10, df[102])
        math(df[101] * -10, df[103])
        math(nan, df[104])
        copy(2.5, df[105])
        copy(65538, dd[201])
    with Rung(*once(slot)):
        copy("A", txt[401])
        copy("B", txt[402])
        copy(200, ds[BYTE_DS])
        copy(ds[BYTE_DS], txt[403], convert=to_binary)
        copy("Y", txt[405])
        copy("Z", txt[406])
    with Rung(*once(slot)):
        fill(2.5, df.select(111, 113))
        math(nan, df[114])
        math(nan, df[115])
        copy(65538, dd[203])
        copy(65538, dd[205])
        copy(196612, dd[206])
    # The echo runs on the staging scan and while the harness holds a Cmd above 1, not on
    # every scan.  The compiled run can't take a scan with an Unpack Copy of a Real.
    echo = (slot.Cmd != 0, Or(slot.Fired == 0, slot.Cmd > 1))
    names = list(ECHO_WORDS)
    for start in range(0, len(names), 5):
        with Rung(*echo):
            for name in names[start : start + 5]:
                first = ECHO_WORDS[name]
                unpack_to_words(_word_source(name), dh.select(first, first + 1))
    with Rung(*echo):
        for n, into in ECHO_TEXT.items():
            copy(txt[n], ds[into], convert=to_ascii)
        fill(0, ds.select(SCORE_DS, SCORE_DS + len(SCORED) - 1))
    for k, n in enumerate(SCORED):
        score = ds[SCORE_DS + k]
        with Rung(*echo, df[n] == df[n]):
            math(score + 1, score)
        with Rung(*echo, df[n] > 3.0e38):
            math(score + 2, score)
        with Rung(*echo, df[n] == 2.5):
            math(score + 4, score)
    _past_bank_steps(slot)
    with Rung(*once(slot)):
        copy(ds[ECHO_TEXT[401]], slot.Result1)
        copy(ds[ECHO_TEXT[403]], slot.Result2)
    class_of(slot, dh[110], [1, 2], slot.Result3)
    class_of(slot, dh[108], [0x4020, 0], slot.Result4)
    with Rung(slot.Cmd != 0):
        copy(slot.Cmd, slot.Scratch)
    finish(slot)


def init_raw_values(slot: Any) -> None:
    fill(0.0, df.select(101, 115))
    fill(0, dd.select(201, 207))
    fill(0, dh.select(101, 128))
    fill("\x00", txt.select(401, 406))
    fill(0, ds.select(4061, 4075))
    copy(0, ds[LAST_DS])
    copy(0, POINTER)
    reset(c.select(1999, 2000))


group = Group(
    GROUP,
    [
        case(
            "WRITES PAST THE DD BANK on a CLICK at DD1531 when the harness asks, and clears "
            "it last.  Stages values for raw Modbus reads and echoes what each raw write left: "
            "TXT401 'A' as ASCII (Result1), TXT403's byte 200 (Result2), DD201's high word in the "
            "second DH (Result3 = 1) and DF 2.5's high word 0x4020 there too (Result4 = 1)",
            ladder=prove_raw_values,
            init=init_raw_values,
            expect={
                "Result1": between(0, 255),
                "Result2": between(0, 255),
                "Result3": between(0, 2),
                "Result4": between(0, 2),
            },
            probe="what a CLICK answers to raw Modbus requests (test_raw_requests)",
            id=CASE,
        ),
    ],
)


def test_modbus_raw() -> None:
    assert_all_passed(run(group))


# --- the raw harness ----------------------------------------------------------------------

#: Seconds one request may take before it is recorded as ``timeout``.
PROBE_TIMEOUT_S = 3.0
#: Seconds the ladder may take to echo a new ``Cmd``.
ACK_TIMEOUT_S = 5.0
POLL_S = 0.02
#: Set to 1 to send SC51 and SC50 (STOP) last.
STOP_ENV = "PYRUNG_CONFORMANCE_STOP"
#: Set to 1 to send ``yd_odd_write``, which may turn Y116 on until it is written back.
OUTPUTS_ENV = "PYRUNG_CONFORMANCE_OUTPUTS"
#: The decoded column of a register pyclickplc has no name for.
NO_NAME = "none: pyclickplc names no register here"


# CLICK's Modbus map, zero-based (CLICK's help, "Modbus addressing").
def _ds(n: int) -> int:
    return n - 1


def _dd(n: int) -> int:
    return 16384 + 2 * (n - 1)


def _dh(n: int) -> int:
    return 24576 + n - 1


def _df(n: int) -> int:
    return 28672 + 2 * (n - 1)


def _txt(n: int) -> int:
    return 36864 + (n - 1) // 2


def _c(n: int) -> int:
    return 16384 + n - 1


def _sc(n: int) -> int:
    return 61440 + n - 1


def _sd(n: int) -> int:
    return 61440 + n - 1


XD0 = 57344
YD0 = 57856
#: Coil past Y116, before Y201: unit 17 of expansion slot 1.
Y_GAP = 8192 + 32 + 16


def _hex(words: list[int]) -> list[str]:
    return [f"0x{w:04X}" for w in words]


def _plain(value: Any) -> Any:
    """A decoded value in a form TOML holds.

    Text goes through ``ascii()`` and a float through ``repr()``.
    """
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return ascii(value)
    return value


def _error(exc: BaseException) -> str:
    return f"error {type(exc).__name__}: {exc}"


def _signed(word: int, bits: int = 16) -> int:
    return word - (1 << bits) if word >> (bits - 1) else word


def _as_click_stores(bank: str, raw: list[Any]) -> list[Any]:
    """Raw registers or bits read as CLICK stores *bank*, in the form of :func:`_plain`.

    DD and DF take two registers, low word first.  A TXT register holds the odd TXT in its low
    byte.  XD and YD name every other register (XD1 is register 2).
    """
    if bank in ("C", "SC", "X", "Y"):
        return [int(b) for b in raw]
    words = [int(w, 16) for w in raw]
    if bank in ("DS", "SD"):
        return [_signed(w) for w in words]
    if bank == "DH":
        return words
    if bank in ("XD", "YD"):
        return words[0::2]
    pairs = list(zip(words[0::2], words[1::2], strict=False))
    if bank == "DD":
        return [_signed(low | high << 16, 32) for low, high in pairs]
    if bank == "DF":
        return [repr(struct.unpack("<f", struct.pack("<HH", *pair))[0]) for pair in pairs]
    if bank == "TXT":
        return [ascii(chr(byte)) for w in words for byte in (w & 0xFF, w >> 8)]
    raise ValueError(f"no rule for bank {bank}")


def _failed(answer: Any) -> bool:
    return isinstance(answer, str) and answer.startswith(("error ", "exception ", "timeout"))


def _disagreement(bank: str, raw: Any, decoded: Any) -> str | None:
    """What sets pyclickplc's value apart from the raw bits read as CLICK stores them, if anything.

    A read both refuse agrees.  A read only one refuses does not.
    """
    if _failed(raw) and _failed(decoded):
        return None
    if _failed(raw) or _failed(decoded):
        return f"raw {raw}, pyclickplc {decoded}"
    values = list(decoded.values()) if isinstance(decoded, dict) else [decoded]
    stored = _as_click_stores(bank, raw)
    if stored != values:
        return f"raw {raw} is {stored} as CLICK stores {bank}, pyclickplc {decoded}"
    return None


def _disagreements(readings: Mapping[str, Any]) -> list[str]:
    """One line for each raw read whose decoded column differs from its bits."""
    lines: list[str] = []

    def walk(path: str, node: Any) -> None:
        if isinstance(node, dict):
            if {"bank", "raw", "decoded"} <= node.keys():
                line = _disagreement(node["bank"], node["raw"], node["decoded"])
                if line:
                    lines.append(f"{path}: {line}")
            for key, value in node.items():
                walk(f"{path}.{key}", value)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(f"{path}[{i}]", value)

    for name, reading in readings.items():
        walk(name, reading)
    return lines


def _word(read: Any) -> int | None:
    """The first register of a raw read, or ``None`` if the read failed."""
    return int(read[0], 16) if isinstance(read, list) and read else None


def _count(read: Any) -> Any:
    """A long read as its length and first value, not every one."""
    if isinstance(read, dict):
        read = list(read.values())
    if isinstance(read, list):
        return {"count": len(read), "first": read[0] if read else "none"}
    return read


class _Wire:
    """A raw pymodbus connection and a pyclickplc one to the same endpoint."""

    def __init__(self, endpoint: Endpoint) -> None:
        self.endpoint = endpoint
        self.raw: Any = None
        self.click: Any = None

    async def __aenter__(self) -> _Wire:
        from pyclickplc.client import ClickClient

        await self._connect_raw()
        self.click = ClickClient(self.endpoint.host, self.endpoint.port, timeout=2)
        await self.click.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.raw.close()
        await self.click.__aexit__()

    async def _connect_raw(self) -> None:
        from pymodbus.client import AsyncModbusTcpClient

        if self.raw is not None:
            self.raw.close()
        self.raw = AsyncModbusTcpClient(
            self.endpoint.host, port=self.endpoint.port, timeout=1.0, retries=0, reconnect_delay=0
        )
        await self.raw.connect()

    async def _send(self, make: Callable[[], Awaitable[Any]]) -> Any:
        """The response, or what stood in for one.

        That is ``timeout``, an exception code or an error.
        """
        from pymodbus.exceptions import ModbusIOException

        try:
            response = await asyncio.wait_for(make(), PROBE_TIMEOUT_S)
        except (TimeoutError, ModbusIOException):
            # A late answer must not be taken for the next request's.
            await self._connect_raw()
            return "timeout"
        except Exception as exc:
            if not self.raw.connected:
                await self._connect_raw()
            return _error(exc)
        if response.isError():
            return f"exception {response.exception_code:02d}"
        return response

    async def registers(self, fc: int, address: int, count: int) -> Any:
        """FC03 or FC04, with no check on *count*.  pymodbus refuses more than 125."""
        from pymodbus.pdu.register_message import (
            ReadHoldingRegistersRequest,
            ReadInputRegistersRequest,
        )

        base: type[Any] = ReadHoldingRegistersRequest if fc == 3 else ReadInputRegistersRequest

        class Unchecked(base):
            def encode(self) -> bytes:
                return struct.pack(">HH", self.address, self.count)

        request = Unchecked(address=address, count=count, dev_id=1)
        response = await self._send(lambda: self.raw.execute(False, request))
        return response if isinstance(response, str) else _hex(list(response.registers))

    async def bits(self, fc: int, address: int, count: int) -> Any:
        read = self.raw.read_coils if fc == 1 else self.raw.read_discrete_inputs
        response = await self._send(lambda: read(address, count=count))
        return response if isinstance(response, str) else [int(b) for b in response.bits[:count]]

    async def write_register(self, address: int, value: int) -> str:
        response = await self._send(lambda: self.raw.write_register(address, value))
        return response if isinstance(response, str) else "ok"

    async def write_registers(self, address: int, values: list[int]) -> str:
        response = await self._send(lambda: self.raw.write_registers(address, values))
        return response if isinstance(response, str) else "ok"

    async def write_coil(self, address: int, value: bool) -> str:
        response = await self._send(lambda: self.raw.write_coil(address, value))
        return response if isinstance(response, str) else "ok"

    async def write_coils(self, address: int, values: list[bool]) -> str:
        response = await self._send(lambda: self.raw.write_coils(address, values))
        return response if isinstance(response, str) else "ok"

    async def decoded(self, make: Callable[[Any], Awaitable[Any]]) -> Any:
        """What pyclickplc's client reads, or the error it raised (an ``OSError`` too)."""
        try:
            return _plain(await asyncio.wait_for(make(self.click), PROBE_TIMEOUT_S))
        except TimeoutError:
            return "timeout"
        except Exception as exc:
            return _error(exc)


class _Session:
    """One run of the probes on one PLC.

    It holds the wire, the slot, what was read and what broke.
    """

    def __init__(self, slot: Mapping[str, int], *, local: bool) -> None:
        self.w: Any = None
        self.slot = slot
        self.local = local
        self.phase = 1
        self.readings: dict[str, dict[str, Any]] = {}
        self.broken: list[str] = []
        #: Lines for the long reads, which keep only a count and their first value.
        self.disagree: list[str] = []

    def entry(self, name: str) -> dict[str, Any]:
        """The reading of probe *name*, kept from the start so a failed probe keeps its part."""
        reading: dict[str, Any] = {}
        self.readings[name] = reading
        return reading

    async def reg(self, field: str) -> int | None:
        try:
            return int(await self.w.click.ds[self.slot[field]])
        except Exception:
            return None

    async def write_ds(self, n: int, value: int | list[int], what: str) -> bool:
        try:
            await self.w.click.ds.write(n, value)
        except Exception as exc:
            self.broken.append(f"{what}: the write of DS{n} raised {_error(exc)}")
            return False
        return True

    async def until(
        self, check: Callable[[], Awaitable[bool]], timeout: float = ACK_TIMEOUT_S
    ) -> bool:
        deadline = time.monotonic() + timeout
        while not await check():
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(POLL_S)
        return True

    async def settle(self, probe: str) -> bool:
        """A new ``Cmd``, then wait for the ladder to copy it into ``Scratch``.

        A scan has then run since the raw write, so the echo shows it.  The sim applies a write
        at the start of the next scan, so a read with no scan between would miss it.
        """
        self.phase += 1
        phase = self.phase
        if not await self.write_ds(self.slot["Cmd"], phase, probe):
            return False

        async def echoed() -> bool:
            return await self.reg("Scratch") == phase

        if await self.until(echoed):
            return True
        self.broken.append(f"{probe}: no echo of Cmd = {phase} within {ACK_TIMEOUT_S:g} s")
        return False

    async def pair(
        self, bank: str, raw: Awaitable[Any], decoded: Callable[[Any], Awaitable[Any]]
    ) -> dict[str, Any]:
        """A raw read and pyclickplc's read of the same thing."""
        return {"bank": bank, "raw": await raw, "decoded": await self.w.decoded(decoded)}

    async def long_pair(
        self, name: str, bank: str, raw: Awaitable[Any], decoded: Callable[[Any], Awaitable[Any]]
    ) -> dict[str, Any]:
        """:meth:`pair` for a long read, compared in full and kept as a count."""
        both = await self.pair(bank, raw, decoded)
        return self.keep_long(name, bank, both["raw"], both["decoded"])

    def keep_long(self, name: str, bank: str, raw: Any, decoded: Any) -> dict[str, Any]:
        """A long read compared in full, then kept as a count and its first value."""
        kept = {"raw": _count(raw), "decoded": _count(decoded)}
        if _disagreement(bank, raw, decoded):
            self.disagree.append(f"{name}: raw {kept['raw']}, pyclickplc {kept['decoded']}")
        return kept

    async def df(self, n: int) -> dict[str, Any]:
        return await self.pair("DF", self.w.registers(3, _df(n), 2), lambda cl: cl.df[n])

    async def dd(self, n: int) -> dict[str, Any]:
        return await self.pair("DD", self.w.registers(3, _dd(n), 2), lambda cl: cl.dd[n])

    async def ds(self, n: int) -> dict[str, Any]:
        return await self.pair("DS", self.w.registers(3, _ds(n), 1), lambda cl: cl.ds[n])

    async def dh(self, first: int, last: int) -> dict[str, Any]:
        return await self.pair(
            "DH",
            self.w.registers(3, _dh(first), last - first + 1),
            lambda cl: cl.dh.read(first, last),
        )

    async def words(self, name: str) -> dict[str, Any]:
        """The two DH the ladder unpacked *name* into."""
        first = ECHO_WORDS[name]
        return await self.dh(first, first + 1)

    async def text(self, first: int, last: int) -> dict[str, Any]:
        count = (last - first) // 2 + 1
        return await self.pair(
            "TXT", self.w.registers(3, _txt(first), count), lambda cl: cl.txt.read(first, last)
        )

    async def c(self, first: int, last: int) -> dict[str, Any]:
        return await self.pair(
            "C", self.w.bits(1, _c(first), last - first + 1), lambda cl: cl.c.read(first, last)
        )

    async def sc(self, first: int, last: int) -> dict[str, Any]:
        if first == last:
            return await self.pair("SC", self.w.bits(2, _sc(first), 1), lambda cl: cl.sc[first])
        return await self.pair(
            "SC", self.w.bits(2, _sc(first), last - first + 1), lambda cl: cl.sc.read(first, last)
        )

    async def sd(self, n: int) -> dict[str, Any]:
        return await self.pair("SD", self.w.registers(4, _sd(n), 1), lambda cl: cl.sd[n])


async def _stage(s: _Session) -> bool:
    """Reset the slot, then ``Cmd = 1`` fires the staging rungs once."""
    if not await s.write_ds(s.slot["Cmd"], [0] * 9, "the reset"):
        return False

    async def reset_done() -> bool:
        return (await s.reg("Ready"), await s.reg("Cmd"), await s.reg("Fired")) == (1, 0, 0)

    if not await s.until(reset_done, timeout=10.0):
        s.broken.append("the reset did not raise Ready within 10 s")
        return False
    if not await s.write_ds(s.slot["Cmd"], 1, "the staging"):
        return False

    async def staged() -> bool:
        return (await s.reg("Fired"), await s.reg("Scratch")) == (1, 1)

    if not await s.until(staged):
        s.broken.append(f"the staging did not fire within {ACK_TIMEOUT_S:g} s")
        return False
    return True


async def _staged_reads(s: _Session) -> None:
    for name, n in (("staged_inf", 102), ("staged_minus_inf", 103), ("staged_nan", 104)):
        r = s.entry(name)
        r["df"] = await s.df(n)
        r["echo"] = await s.words(f"DF{n}")
    r = s.entry("staged_df")
    r["df"] = await s.df(105)
    r["echo"] = await s.words("DF105")
    r = s.entry("staged_dd")
    r["dd"] = await s.dd(201)
    r["echo"] = await s.words("DD201")
    r = s.entry("staged_txt")
    r["txt"] = await s.text(401, 404)
    r["echo"] = [await s.ds(ECHO_TEXT[n]) for n in (401, 402, 403)]


async def _df_write(
    s: _Session, probe: str, n: int, request: str, send: Callable[[], Awaitable[str]]
) -> None:
    r = s.entry(probe)
    r["request"] = request
    r["before"] = await s.df(n)
    r["answer"] = await send()
    r["settled"] = int(await s.settle(probe))
    r["after"] = await s.df(n)
    r["echo"] = await s.words(f"DF{n}")
    r["score"] = await s.ds(SCORE_DS + n - SCORED.start)


async def _value_writes(s: _Session) -> None:
    """Writes into the part's own DF, DD and TXT, which the closing reset clears."""
    w = s.w
    for probe, n, words in (
        ("df_all_ones", 111, [0xFFFF, 0xFFFF]),
        ("df_quiet_nan", 112, [0x0000, 0x7FC0]),
        ("df_inf", 113, [0x0000, 0x7F80]),
    ):
        request = f"FC16 DF{n} [{', '.join(_hex(words))}]"
        await _df_write(s, probe, n, request, lambda n=n, v=words: w.write_registers(_df(n), v))
    await _df_write(
        s,
        "df_half_first",
        114,
        "FC06 DF114 first register 0x0000, over NaN",
        lambda: w.write_register(_df(114), 0x0000),
    )
    await _df_write(
        s,
        "df_half_second",
        115,
        "FC06 DF115 second register 0x4020, over NaN",
        lambda: w.write_register(_df(115) + 1, 0x4020),
    )

    r = s.entry("txt_high_byte")
    r["request"] = "FC06 TXT405/TXT406 register 0xC841"
    r["before"] = await s.text(405, 406)
    r["answer"] = await w.write_register(_txt(405), 0xC841)
    r["settled"] = int(await s.settle("txt_high_byte"))
    r["after"] = await s.text(405, 406)
    r["echo"] = [await s.ds(ECHO_TEXT[n]) for n in (405, 406)]

    r = s.entry("dd_half")
    r["request"] = "FC06 DD203 first register 0x0005"
    r["before"] = await s.dd(203)
    r["answer"] = await w.write_register(_dd(203), 0x0005)
    r["settled"] = int(await s.settle("dd_half"))
    r["after"] = await s.dd(203)
    r["echo"] = await s.words("DD203")

    r = s.entry("dd_straddle")
    r["request"] = "FC16 [0x0009, 0x0008] at the second register of DD205"
    r["before"] = [await s.dd(205), await s.dd(206)]
    r["answer"] = await w.write_registers(_dd(205) + 1, [0x0009, 0x0008])
    r["settled"] = int(await s.settle("dd_straddle"))
    r["after"] = [await s.dd(205), await s.dd(206)]
    r["echo"] = [await s.words("DD205"), await s.words("DD206")]


async def _fc16_past_ds(s: _Session) -> None:
    # DS4500 is the part's, and the closing reset clears it.
    r = s.entry("fc16_past_ds")
    r["request"] = "FC16 [7, 8] at DS4500 and the register past it"
    r["before"] = await s.ds(LAST_DS)
    r["answer"] = await s.w.write_registers(_ds(LAST_DS), [7, 8])
    r["settled"] = int(await s.settle("fc16_past_ds"))
    r["after"] = await s.ds(LAST_DS)


async def _sc53_sc54(s: _Session) -> None:
    """FC15 over SC53..SC54, with SC53 on only once SD29, SD31 and SD32 hold today's date.

    SC53 sets the PLC's date from those three, so a stale date there would move the clock.
    SC53 goes on only if it reads 0 first and the old date read, so it can be written back.
    """
    w = s.w
    r = s.entry("fc15_sc53_sc54")
    r["before"] = await s.sc(53, 54)
    r["today"] = {f"SD{n}": await s.sd(n) for n in (19, 21, 22)}
    r["date_before"] = {f"SD{n}": await s.sd(n) for n in (29, 31, 32)}
    today = [_word(r["today"][f"SD{n}"]["raw"]) for n in (19, 21, 22)]
    old = [_word(r["date_before"][f"SD{n}"]["raw"]) for n in (29, 31, 32)]
    armed = False
    try:
        sc53_off = isinstance(r["before"]["raw"], list) and r["before"]["raw"][:1] == [0]
        if sc53_off and None not in today and None not in old:
            r["date_set"] = [
                await w.write_register(_sd(n), int(value or 0))
                for n, value in zip((29, 31, 32), today, strict=True)
            ]
            await s.settle("fc15_sc53_sc54")
            back = [_word(await w.registers(4, _sd(n), 1)) for n in (29, 31, 32)]
            armed = back == today
        r["date_set_to_today"] = int(armed)
        r["request"] = f"FC15 [{int(armed)}, 0] at SC53..SC54"
        r["answer"] = await w.write_coils(_sc(53), [armed, False])
        r["settled"] = int(await s.settle("fc15_sc53_sc54"))
        r["after"] = await s.sc(53, 54)
    finally:
        r["sc53_off"] = await w.write_coil(_sc(53), False)
        if "date_set" in r:
            r["date_restored"] = [
                await w.write_register(_sd(n), int(value or 0))
                for n, value in zip((29, 31, 32), old, strict=True)
            ]
        await s.settle("fc15_sc53_sc54")


async def _past_banks(s: _Session) -> None:
    w = s.w
    s.readings["fc03_past_ds"] = await s.pair(
        "DS", w.registers(3, _ds(LAST_DS) + 1, 1), lambda cl: cl.ds[LAST_DS + 1]
    )
    s.readings["fc03_across_ds_end"] = await s.pair(
        "DS", w.registers(3, _ds(LAST_DS), 2), lambda cl: cl.ds.read(LAST_DS, LAST_DS + 1)
    )
    s.readings["fc01_past_c2000"] = await s.pair("C", w.bits(1, _c(2001), 1), lambda cl: cl.c[2001])
    s.readings["fc01_y_gap"] = await s.pair("Y", w.bits(1, Y_GAP, 1), lambda cl: cl.y[117])
    # C1999 and C2000 are the part's, and the closing reset clears them.
    r = s.entry("fc15_across_c_end")
    r["request"] = "FC15 four ON at C1999..C2002"
    r["before"] = await s.c(1999, 2000)
    r["answer"] = await w.write_coils(_c(1999), [True] * 4)
    r["settled"] = int(await s.settle("fc15_across_c_end"))
    r["after"] = await s.c(1999, 2000)


async def _function_codes(s: _Session) -> None:
    """Each bank read with the other function code, beside pyclickplc's read of it."""
    w = s.w
    s.readings["fc03_at_xd"] = await s.pair("XD", w.registers(3, XD0, 1), lambda cl: cl.xd[0])
    s.readings["fc04_at_ds1"] = await s.pair("DS", w.registers(4, _ds(1), 1), lambda cl: cl.ds[1])
    s.readings["fc01_at_x001"] = await s.pair("X", w.bits(1, 0, 1), lambda cl: cl.x[1])
    s.readings["fc02_at_c1"] = await s.pair("C", w.bits(2, _c(1), 1), lambda cl: cl.c[1])


async def _xd_yd(s: _Session) -> None:
    w = s.w
    for name, bank, fc, first in (("xd_x17", "XD", 4, XD0), ("yd_x17", "YD", 3, YD0)):
        r = s.entry(name)
        r.update(await s.pair(bank, w.registers(fc, first, 17), lambda cl, b=bank: _block(cl, b)))
        # XD0u is the register after XD0.
        r["upper"] = await s.pair(
            bank, w.registers(fc, first + 1, 1), lambda cl, b=bank: _upper(cl, b)
        )


def _block(cl: Any, bank: str) -> Awaitable[Any]:
    return (cl.xd if bank == "XD" else cl.yd).read(0, 8)


def _upper(cl: Any, bank: str) -> Awaitable[Any]:
    return (cl.xd0u if bank == "XD" else cl.yd0u).read()


async def _yd_odd_write(s: _Session) -> None:
    """FC06 of 0x8000 to YD's register 3, between YD1 and YD2, then YD1 read back.

    If it lands on YD1 it turns Y116 on until both registers are written back, so it is sent
    only with ``PYRUNG_CONFORMANCE_OUTPUTS=1``.
    """
    w = s.w
    r = s.entry("yd_odd_write")
    if os.environ.get(OUTPUTS_ENV) != "1":
        r["skipped"] = f"not sent: set {OUTPUTS_ENV}=1 to send it"
        return
    r["request"] = "FC06 0x8000 to YD register 3"
    r["before"] = {
        "yd1": await s.pair("YD", w.registers(3, YD0 + 2, 1), lambda cl: cl.yd[1]),
        "register_3": {"raw": await w.registers(3, YD0 + 3, 1), "decoded": NO_NAME},
    }
    old_yd1 = _word(r["before"]["yd1"]["raw"])
    old_odd = _word(r["before"]["register_3"]["raw"])
    if old_yd1 is None or old_odd is None:
        r["skipped"] = "not sent: YD1 or register 3 did not read, so neither could be written back"
        return
    try:
        r["answer"] = await w.write_register(YD0 + 3, 0x8000)
        r["settled"] = int(await s.settle("yd_odd_write"))
        r["after"] = await s.pair("YD", w.registers(3, YD0 + 2, 2), lambda cl: cl.yd[1])
    finally:
        r["restored"] = [
            await w.write_register(YD0 + 3, old_odd),
            await w.write_register(YD0 + 2, old_yd1),
        ]
        await s.settle("yd_odd_write")


async def _sd29_write(s: _Session) -> None:
    """FC06 to SD29 of another year, FC04 back, then the old value written back.

    It is sent only while SC53 reads 0, since SC53 on would set the clock from SD29.
    """
    r = s.entry("sd29_write")
    r["sc53"] = await s.sc(53, 53)
    if r["sc53"]["raw"] != [0]:
        r["skipped"] = "not sent: SC53 does not read 0"
        return
    r["before"] = await s.sd(29)
    old = _word(r["before"]["raw"])
    if old is None:
        r["skipped"] = "not sent: SD29 did not read, so it could not be written back"
        return
    new = old + 1 if old < 9999 else old - 1
    r["request"] = f"FC06 SD29 = {new}"
    try:
        r["answer"] = await s.w.write_register(_sd(29), new)
        r["settled"] = int(await s.settle("sd29_write"))
        r["after"] = await s.sd(29)
    finally:
        r["restored"] = await s.w.write_register(_sd(29), old)
        await s.settle("sd29_write")


async def _past_step(s: _Session, step: int) -> bool:
    if not await s.write_ds(PAST_OP_DS, step, f"past_bank_alias step {step}"):
        return False
    return await s.settle(f"past_bank_alias step {step}")


async def _past_bank_alias(s: _Session) -> None:
    """Whether the memory a pointer reaches past the DD bank is the memory Modbus finds there.

    If the Modbus map runs on past DD1000 at two registers each, DD1001 is 18384 and DD1531 is
    19444, in the gap before DH.  Step 2 rewrites DD1531 on every scan it is held, so the FC16
    goes in at step 3.  Step 4 zeroes DD1531, whatever it held, even if a step fails.
    """
    w = s.w
    here = _dd(ADDRESS)
    r = s.entry("past_bank_alias")
    r["dd1001_before"] = await s.dd(1001)
    r["dd1531_before"] = await s.dd(ADDRESS)
    old = r["dd1531_before"]["raw"]
    settled = True
    try:
        settled &= await _past_step(s, 1)
        r["ladder_found"] = await s.ds(PAST_FOUND_DS)
        low, high = PATTERN
        r["after_dd"] = {
            "dh_pattern": await s.dh(*DH_PAIR),
            f"dd{AFTER_DD}_by_pointer": await s.dd(AFTER_INTO_DD),
            "if_dh_low_word_first": high << 16 | low,
            "if_dh_high_word_first": low << 16 | high,
        }
        settled &= await _past_step(s, 2)
        r["dd1001_after_ladder_write"] = await s.dd(1001)
        r["after_ladder_write"] = await s.dd(ADDRESS)
        r["ladder_back"] = await s.ds(PAST_BACK_DS)
        settled &= await _past_step(s, 3)
        r["fc16"] = await w.write_registers(here, [MODBUS_VALUE, 0])
        settled &= await s.settle("past_bank_alias FC16")
        r["after_fc16"] = await s.dd(ADDRESS)
        r["ladder_back_after_fc16"] = await s.ds(PAST_BACK_DS)
    finally:
        if r.get("fc16") == "ok" and isinstance(old, list) and len(old) == 2:
            r["fc16_restored"] = await w.write_registers(here, [int(x, 16) for x in old])
        settled &= await _past_step(s, 4)
        r["after_clear"] = await s.dd(ADDRESS)
        r["ladder_back_after_clear"] = await s.ds(PAST_BACK_DS)
        await s.write_ds(PAST_OP_DS, 0, "past_bank_alias")
        r["settled"] = int(settled)


async def _limits(s: _Session) -> None:
    w = s.w
    s.readings["fc03_x126"] = await s.long_pair(
        "fc03_x126", "DS", w.registers(3, _ds(1), 126), lambda cl: cl.ds.read(1, 126)
    )
    r = s.entry("fc15_x1969")
    bits = await w.bits(1, _c(1), 1969)
    decoded = await w.decoded(lambda cl: cl.c.read(1, 1969))
    r["read"] = s.keep_long("fc15_x1969.read", "C", bits, decoded)
    if isinstance(bits, list) and len(bits) == 1969:
        values, r["values"] = [bool(b) for b in bits], "C1..C1969 as read"
    elif s.local:
        values, r["values"] = [False] * 1969, "1969 OFF: the read of C1..C1969 failed"
    else:
        values, r["values"] = [], "not sent: the read of C1..C1969 failed"
    r["answer"] = await w.write_coils(_c(1), values) if values else "not sent"
    await s.settle("fc15_x1969")
    # Last before the reset, since a server may never answer it.
    s.readings["fc03_x200"] = await s.long_pair(
        "fc03_x200", "DS", w.registers(3, _ds(1), 200), lambda cl: cl.ds.read(1, 200)
    )


async def _stop_probes(s: _Session) -> None:
    """SC51 (watchdog reset), then SC50 (STOP).  Each records the exception, then SC11."""
    w = s.w
    r = s.entry("sc51_watchdog_reset")
    r["answer"] = await w.write_coil(_sc(51), True)
    r["off"] = await w.write_coil(_sc(51), False)
    r["sc11"] = await s.sc(11, 11)
    r = s.entry("sc50_stop")
    r["answer"] = await w.write_coil(_sc(50), True)
    deadline = time.monotonic() + 2.0
    sc11 = await w.bits(2, _sc(11), 1)
    while sc11 == [1] and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        sc11 = await w.bits(2, _sc(11), 1)
    r["sc11"] = {"bank": "SC", "raw": sc11, "decoded": await w.decoded(lambda cl: cl.sc[11])}
    r["off"] = await w.write_coil(_sc(50), False)


_PROBES = (
    _staged_reads,
    _value_writes,
    _fc16_past_ds,
    _sc53_sc54,
    _past_banks,
    _function_codes,
    _xd_yd,
    _yd_odd_write,
    _sd29_write,
    _past_bank_alias,
    _limits,
)


async def _probe_all(s: _Session) -> None:
    """Every probe in order, then the reset, then the probes that stop the PLC.

    A probe that raises ends the run, and the reset still clears the part's memory.
    """
    if not await _stage(s):
        return
    try:
        for probe in _PROBES:
            try:
                await probe(s)
            except Exception as exc:
                s.broken.append(f"{probe.__name__.lstrip('_')}: {_error(exc)}")
                return
    finally:
        # The reset clears all of the part's memory.
        if await s.write_ds(s.slot["Cmd"], 0, "the closing reset"):

            async def ready() -> bool:
                return await s.reg("Ready") == 1

            if not await s.until(ready, timeout=10.0):
                s.broken.append("the closing reset did not raise Ready within 10 s")
    if os.environ.get(STOP_ENV) == "1":
        await _stop_probes(s)


def _target_name(sim: type) -> str:
    return {"SimServer": "pyrung", "CircuitPySim": "circuitpy"}.get(sim.__name__, sim.__name__)


#: Where a local target's answers go, one file per target, rewritten on each run.  Not the
#: export's folder, which a person loads into the CLICK.
LOCAL_READINGS = Path(__file__).parents[2] / "build" / "modbus_raw-readings"

_LOCAL_HEADER = """\
# What a local target answered to the raw requests of test_modbus_raw.py, rewritten each run.
"""


@pytest.mark.conformance
def test_raw_requests(
    conformance_endpoint: Endpoint | None,
    conformance_groups: list[str] | None,
    conformance_sim: type,
) -> None:
    # Imported here, since suite imports this module for its group.
    from tests.conformance import suite

    if conformance_groups is not None and GROUP not in conformance_groups:
        pytest.skip(f"GROUP= leaves out {GROUP}")
    build = suite.build(GROUP)
    g = build.group(GROUP)
    ((index, _),) = g.cases
    slot = {f: g.register(index, f) for f in ("Cmd", "Scratch", "Fired", "Ready")}
    s = _Session(slot, local=conformance_endpoint is None)

    async def probe(endpoint: Endpoint) -> None:
        async with _Wire(endpoint) as wire:
            s.w = wire
            found = await wire.click.ds[g.hash_register]
            if found != g.group_hash:
                s.broken.append(
                    f"{g.subroutine} is stale: repush (DS{g.hash_register} reads {found}, "
                    f"this build writes {g.group_hash})"
                )
            else:
                await _probe_all(s)

    if conformance_endpoint is None:
        target = _target_name(conformance_sim)
        path, header = LOCAL_READINGS / f"{target}.toml", _LOCAL_HEADER
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
    else:
        if read_program_hash(conformance_endpoint) != build.program_hash:
            pytest.skip(f"part {GROUP} is not loaded (make conformance-export PART={GROUP})")
        target, path, header = str(conformance_endpoint), suite.READINGS_PATH, None
    disagree: list[str] = []
    try:
        if conformance_endpoint is None:
            with conformance_sim(build) as sim:
                asyncio.run(probe(sim.endpoint))
                sim.raise_if_failed()
            # What the ladder staged is pyrung's to get right on its own targets.
            echo = s.readings.get("staged_txt", {}).get("echo", [])
            if [e.get("decoded") for e in echo] != [65, 66, 200]:
                s.broken.append(f"the staged TXT echo reads {echo}, not 65, 66, 200")
        else:
            asyncio.run(probe(conformance_endpoint))
    finally:
        # Whatever the run got through is recorded, even if it died.
        if s.readings:
            disagree = _disagreements(s.readings) + s.disagree
            s.readings["disagree"] = {"lines": disagree}
            record_raw_readings(
                path,
                s.readings,
                group=GROUP,
                case_hash=g.case_hashes[CASE],
                on=date.today().isoformat(),
                **({"header": header} if header else {}),
            )
        for name, reading in s.readings.items():
            print(f"read {GROUP}/{name} on {target}: {reading}")
        for line in disagree:
            print(f"disagree {GROUP}/{line}")
        if os.environ.get(STOP_ENV) == "1":
            print(f"SC50 was sent. Put the PLC at {target} back in RUN.")
    assert not s.broken, "\n".join(s.broken)


def test_a_decoded_value_is_checked_against_the_bits_as_click_stores_them() -> None:
    assert _disagreement("DF", ["0x0000", "0x4020"], "2.5") is None
    assert _disagreement("DF", ["0x0000", "0xFFC0"], "nan") is None
    assert _disagreement("DD", ["0xFFFE", "0xFFFF"], -2) is None
    assert _disagreement("TXT", ["0x4241"], {"TXT401": "'A'", "TXT402": "'B'"}) is None
    assert _disagreement("XD", ["0x0001", "0x0009", "0x0002"], {"XD0": 1, "XD1": 2}) is None
    assert _disagreement("SC", "exception 02", "error ValueError: no") is None
    assert _disagreement("DS", ["0xFFFF"], 65535) == (
        "raw ['0xFFFF'] is [-1] as CLICK stores DS, pyclickplc 65535"
    )
    assert (
        _disagreement("C", [0], "error ValueError: no")
        == "raw [0], pyclickplc error ValueError: no"
    )
    readings = {"p": {"a": [{"bank": "DH", "raw": ["0x0001"], "decoded": 2}], "n": 1}}
    assert _disagreements(readings) == [
        "p.a[0]: raw ['0x0001'] is [1] as CLICK stores DH, pyclickplc 2"
    ]
