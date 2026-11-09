"""Raw Modbus requests, and what the ladder saw of each write: part ``modbus_raw``.

The rest of the suite reads and writes slot DS only, through pyclickplc's client.  This part
asks what a CLICK answers to the requests that client never sends or refuses to send.  Its
ladder stages values for the harness to read raw.  After each raw write the ladder echoes what
it then holds, so a reading tells "the write was stored" apart from "the server answered OK".
``test_raw_requests`` sends the requests with ``pymodbus`` and records every answer: the
registers or bits, the exception code, or ``timeout``.  It reads each value twice, raw and
through pyclickplc's ``ClickClient``, which decodes it or raises.  Nothing here is asserted
against an answer.  On a CLICK each probe goes into ``readings.toml``.  On a local target it goes
into ``build/conformance-modbus_raw/<target>.toml``, for setting the two side by side.

The ladder (case ``raw_values``), on the scan ``Cmd`` arrives:

- DF102 = +Inf, DF103 = -Inf, DF104 = NaN (+Inf - +Inf), DF105 = 2.5, DD201 = 0x00010002.
- TXT401 = 'A', TXT402 = 'B' and TXT403 = the byte 200 (a Copy Binary).
- The write targets: DF111..DF113 = 2.5, DF114 and DF115 = NaN, DD203 and DD205 = 0x00010002,
  DD206 = 0x00030004, TXT405 = 'Y' and TXT406 = 'Z'.

Every scan while ``Cmd`` is up, it copies each DF and DD above into two DH (an Unpack Copy, low
word first) and each TXT into a DS (a Copy to ASCII).  It scores each write-target DF into
DS4071..DS4075: 1 if it equals itself (not NaN), 2 if it is above 3.0E38, 4 if it equals 2.5.
Last, it copies ``Cmd`` into ``Scratch``.  The harness writes, then writes a new ``Cmd``, and
reads the echo once ``Scratch`` holds it.

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
  applies) to today's date from SD19, SD21 and SD22.  If they do not read back as today, it
  writes SC53 off, and the probe can only show the exception.
- ``fc03_past_ds``, ``fc03_across_ds_end``, ``fc01_past_c2000``, ``fc01_y_gap``: reads past a bank
  or in a gap (the register past DS4500, DS4500 and that register, C2001, between Y116 and Y201).
  ``fc15_across_c_end``: FC15 of four ON over C1999..C2002.
- ``fc03_at_xd``, ``fc04_at_ds1``, ``fc01_at_x001``, ``fc02_at_c1``: a bank read with the other
  function code.
- ``xd_x17``, ``yd_x17``: FC04 of 17 registers from XD0, FC03 of 17 from YD0, the odd registers
  between the documented ones included.  ``yd_odd_write``: FC06 of 0x8000 to YD's register 3,
  then YD1 read back.  If it lands on YD1 it turns Y116 on until the harness writes it back.
- ``sd29_write``: FC06 to SD29 of a year one past what it holds, then FC04 back, then the old
  value written back.
- ``fc03_x126``, ``fc03_x200``, ``fc15_x1969``: requests past Modbus's limits (125 registers,
  1968 coils), which a server should refuse with exception 03.  The FC15 writes C1..C1969 with
  what they read just before.
- ``sc51_watchdog_reset``, ``sc50_stop``: only with ``PYRUNG_CONFORMANCE_STOP=1``, last.  SC50 puts
  the PLC in STOP: the probe records the exception, then SC11 after.  Put the PLC back in RUN.

Each probe writes only the part's own memory, or restores what it wrote, except where a CLICK
should refuse the write.  The reset (``Cmd = 0``) clears the part's memory at the end.
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
from pyrung.core import Rung, copy, fill, math, reset, to_ascii, to_binary, unpack_to_words
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


def _word_source(name: str) -> Any:
    bank = {"DF": df, "DD": dd}[name[:2]]
    return bank[int(name[2:])]


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
    names = list(ECHO_WORDS)
    for start in range(0, len(names), 5):
        with Rung(slot.Cmd != 0):
            for name in names[start : start + 5]:
                first = ECHO_WORDS[name]
                unpack_to_words(_word_source(name), dh.select(first, first + 1))
    with Rung(slot.Cmd != 0):
        for n, into in ECHO_TEXT.items():
            copy(txt[n], ds[into], convert=to_ascii)
        fill(0, ds.select(SCORE_DS, SCORE_DS + len(SCORED) - 1))
    for k, n in enumerate(SCORED):
        score = ds[SCORE_DS + k]
        with Rung(slot.Cmd != 0, df[n] == df[n]):
            math(score + 1, score)
        with Rung(slot.Cmd != 0, df[n] > 3.0e38):
            math(score + 2, score)
        with Rung(slot.Cmd != 0, df[n] == 2.5):
            math(score + 4, score)
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
    fill(0, dd.select(201, 206))
    fill(0, dh.select(101, 126))
    fill("\x00", txt.select(401, 406))
    fill(0, ds.select(4061, 4075))
    copy(0, ds[LAST_DS])
    reset(c.select(1999, 2000))


group = Group(
    GROUP,
    [
        case(
            "Stages values for raw Modbus reads and echoes what each raw write left: TXT401 "
            "'A' as ASCII (Result1), TXT403's byte 200 (Result2), DD201's high word in the "
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
    """A decoded value as TOML can hold it: text as ``ascii()``, floats as ``repr()``."""
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
        """The response, or what stood in for one: ``timeout``, an exception code, an error."""
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
        """FC03 or FC04, with no check on *count*: pymodbus refuses more than 125."""
        from pymodbus.pdu.register_message import (
            ReadHoldingRegistersRequest,
            ReadInputRegistersRequest,
        )

        base = ReadHoldingRegistersRequest if fc == 3 else ReadInputRegistersRequest

        class Unchecked(base):  # type: ignore[valid-type,misc]
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
        """What pyclickplc's client reads, or the error it raised."""
        try:
            return _plain(await asyncio.wait_for(make(self.click), PROBE_TIMEOUT_S))
        except TimeoutError:
            return "timeout"
        except Exception as exc:
            return _error(exc)


def _word(read: Any) -> int | None:
    """The first register of a raw read, or ``None`` if the read failed."""
    return int(read[0], 16) if isinstance(read, list) and read else None


def _count(read: Any) -> Any:
    """A long read as its length and first word, not every register."""
    if isinstance(read, list):
        return {"count": len(read), "first": read[0] if read else "none"}
    return read


class _Session:
    """One run of the probes on one PLC: the wire, the slot, what was read and what broke."""

    def __init__(self, wire: _Wire, slot: Mapping[str, int], *, local: bool) -> None:
        self.w = wire
        self.slot = slot
        self.local = local
        self.phase = 1
        self.readings: dict[str, dict[str, Any]] = {}
        self.broken: list[str] = []

    async def reg(self, field: str) -> int:
        return int(await self.w.click.ds[self.slot[field]])

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
        await self.w.click.ds.write(self.slot["Cmd"], phase)

        async def echoed() -> bool:
            return await self.reg("Scratch") == phase

        if await self.until(echoed):
            return True
        self.broken.append(f"{probe}: no echo of Cmd = {phase} within {ACK_TIMEOUT_S:g} s")
        return False

    async def both(
        self, raw: Awaitable[Any], decoded: Callable[[Any], Awaitable[Any]]
    ) -> dict[str, Any]:
        """A raw read and pyclickplc's read of the same thing."""
        return {"raw": await raw, "decoded": await self.w.decoded(decoded)}

    async def df(self, n: int) -> dict[str, Any]:
        return await self.both(self.w.registers(3, _df(n), 2), lambda cl: cl.df[n])

    async def dd(self, n: int) -> dict[str, Any]:
        return await self.both(self.w.registers(3, _dd(n), 2), lambda cl: cl.dd[n])

    async def ds(self, n: int) -> dict[str, Any]:
        return await self.both(self.w.registers(3, _ds(n), 1), lambda cl: cl.ds[n])

    async def words(self, name: str) -> dict[str, Any]:
        """The two DH the ladder unpacked *name* into."""
        first = ECHO_WORDS[name]
        return await self.both(
            self.w.registers(3, _dh(first), 2), lambda cl: cl.dh.read(first, first + 1)
        )

    async def text(self, first: int, last: int) -> dict[str, Any]:
        count = (last - first) // 2 + 1
        return await self.both(
            self.w.registers(3, _txt(first), count), lambda cl: cl.txt.read(first, last)
        )

    async def sc(self, first: int, last: int) -> dict[str, Any]:
        if first == last:
            return await self.both(self.w.bits(2, _sc(first), 1), lambda cl: cl.sc[first])
        return await self.both(
            self.w.bits(2, _sc(first), last - first + 1), lambda cl: cl.sc.read(first, last)
        )

    async def sd(self, n: int) -> dict[str, Any]:
        return await self.both(self.w.registers(4, _sd(n), 1), lambda cl: cl.sd[n])


async def _stage(s: _Session) -> bool:
    """Reset the slot, then ``Cmd = 1`` fires the staging rungs once."""
    await s.w.click.ds.write(s.slot["Cmd"], [0] * 9)

    async def reset_done() -> bool:
        return (await s.reg("Ready"), await s.reg("Cmd"), await s.reg("Fired")) == (1, 0, 0)

    if not await s.until(reset_done, timeout=10.0):
        s.broken.append("the reset did not raise Ready within 10 s")
        return False
    await s.w.click.ds.write(s.slot["Cmd"], 1)

    async def staged() -> bool:
        return (await s.reg("Fired"), await s.reg("Scratch")) == (1, 1)

    if not await s.until(staged):
        s.broken.append(f"the staging did not fire within {ACK_TIMEOUT_S:g} s")
        return False
    return True


async def _staged_reads(s: _Session) -> None:
    for name, n in (("staged_inf", 102), ("staged_minus_inf", 103), ("staged_nan", 104)):
        s.readings[name] = {"df": await s.df(n), "echo": await s.words(f"DF{n}")}
    s.readings["staged_df"] = {"df": await s.df(105), "echo": await s.words("DF105")}
    s.readings["staged_dd"] = {"dd": await s.dd(201), "echo": await s.words("DD201")}
    s.readings["staged_txt"] = {
        "txt": await s.text(401, 404),
        "echo": [await s.ds(ECHO_TEXT[n]) for n in (401, 402, 403)],
    }


async def _df_write(
    s: _Session, probe: str, n: int, request: str, send: Callable[[], Awaitable[str]]
) -> None:
    before = await s.df(n)
    answer = await send()
    settled = await s.settle(probe)
    s.readings[probe] = {
        "request": request,
        "before": before,
        "answer": answer,
        "after": await s.df(n),
        "echo": await s.words(f"DF{n}"),
        "score": await s.ds(SCORE_DS + n - SCORED.start),
        "settled": int(settled),
    }


async def _value_writes(s: _Session) -> None:
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

    before = await s.text(405, 406)
    answer = await w.write_register(_txt(405), 0xC841)
    settled = await s.settle("txt_high_byte")
    s.readings["txt_high_byte"] = {
        "request": "FC06 TXT405/TXT406 register 0xC841",
        "before": before,
        "answer": answer,
        "after": await s.text(405, 406),
        "echo": [await s.ds(ECHO_TEXT[n]) for n in (405, 406)],
        "settled": int(settled),
    }

    before = await s.dd(203)
    answer = await w.write_register(_dd(203), 0x0005)
    settled = await s.settle("dd_half")
    s.readings["dd_half"] = {
        "request": "FC06 DD203 first register 0x0005",
        "before": before,
        "answer": answer,
        "after": await s.dd(203),
        "echo": await s.words("DD203"),
        "settled": int(settled),
    }

    before_pair = [await s.dd(205), await s.dd(206)]
    answer = await w.write_registers(_dd(205) + 1, [0x0009, 0x0008])
    settled = await s.settle("dd_straddle")
    s.readings["dd_straddle"] = {
        "request": "FC16 [0x0009, 0x0008] at the second register of DD205",
        "before": before_pair,
        "answer": answer,
        "after": [await s.dd(205), await s.dd(206)],
        "echo": [await s.words("DD205"), await s.words("DD206")],
        "settled": int(settled),
    }


async def _all_or_nothing(s: _Session) -> None:
    # DS4500 is the part's, and the closing reset clears it.
    before = await s.ds(LAST_DS)
    answer = await s.w.write_registers(_ds(LAST_DS), [7, 8])
    settled = await s.settle("fc16_past_ds")
    s.readings["fc16_past_ds"] = {
        "request": "FC16 [7, 8] at DS4500 and the register past it",
        "before": before,
        "answer": answer,
        "after": await s.ds(LAST_DS),
        "settled": int(settled),
    }
    s.readings["fc15_sc53_sc54"] = await _sc53_sc54(s)


async def _sc53_sc54(s: _Session) -> dict[str, Any]:
    """FC15 over SC53..SC54, with SC53 on only once SD29, SD31 and SD32 hold today's date.

    SC53 sets the PLC's date from those three, so a stale date there would move the clock.
    """
    w = s.w
    before = await s.sc(53, 54)
    today = [_word(await w.registers(4, _sd(n), 1)) for n in (19, 21, 22)]
    old = [_word(await w.registers(4, _sd(n), 1)) for n in (29, 31, 32)]
    armed = None not in today
    if armed:
        for n, value in zip((29, 31, 32), today, strict=True):
            await w.write_register(_sd(n), int(value or 0))
        await s.settle("fc15_sc53_sc54")
        back = [_word(await w.registers(4, _sd(n), 1)) for n in (29, 31, 32)]
        armed = back == today
    answer = await w.write_coils(_sc(53), [armed, False])
    settled = await s.settle("fc15_sc53_sc54")
    after = await s.sc(53, 54)
    cleared = await w.write_coil(_sc(53), False)
    for n, value in zip((29, 31, 32), old, strict=True):
        if value is not None:
            await w.write_register(_sd(n), value)
    await s.settle("fc15_sc53_sc54")
    return {
        "request": f"FC15 [{int(armed)}, 0] at SC53..SC54",
        "date_set_to_today": int(armed),
        "before": before,
        "answer": answer,
        "after": after,
        "sc53_off": cleared,
        "settled": int(settled),
    }


async def _past_banks(s: _Session) -> None:
    w = s.w
    s.readings["fc03_past_ds"] = await s.both(
        w.registers(3, _ds(LAST_DS) + 1, 1), lambda cl: cl.ds[LAST_DS + 1]
    )
    s.readings["fc03_across_ds_end"] = await s.both(
        w.registers(3, _ds(LAST_DS), 2), lambda cl: cl.ds.read(LAST_DS, LAST_DS + 1)
    )
    s.readings["fc01_past_c2000"] = await s.both(w.bits(1, _c(2001), 1), lambda cl: cl.c[2001])
    s.readings["fc01_y_gap"] = await s.both(w.bits(1, Y_GAP, 1), lambda cl: cl.y[117])
    # C1999 and C2000 are the part's, and the closing reset clears them.
    before = await w.bits(1, _c(1999), 2)
    answer = await w.write_coils(_c(1999), [True] * 4)
    settled = await s.settle("fc15_across_c_end")
    s.readings["fc15_across_c_end"] = {
        "request": "FC15 four ON at C1999..C2002",
        "before": before,
        "answer": answer,
        "after": await s.both(w.bits(1, _c(1999), 2), lambda cl: cl.c.read(1999, 2000)),
        "settled": int(settled),
    }


async def _function_codes(s: _Session) -> None:
    """Each bank read with the other function code, beside pyclickplc's read of it."""
    w = s.w
    s.readings["fc03_at_xd"] = await s.both(w.registers(3, XD0, 1), lambda cl: cl.xd[0])
    s.readings["fc04_at_ds1"] = await s.both(w.registers(4, _ds(1), 1), lambda cl: cl.ds[1])
    s.readings["fc01_at_x001"] = await s.both(w.bits(1, 0, 1), lambda cl: cl.x[1])
    s.readings["fc02_at_c1"] = await s.both(w.bits(2, _c(1), 1), lambda cl: cl.c[1])


async def _xd_yd(s: _Session) -> None:
    w = s.w
    s.readings["xd_x17"] = {
        **await s.both(w.registers(4, XD0, 17), lambda cl: cl.xd.read(0, 8)),
        "decoded_xd0u": await w.decoded(lambda cl: cl.xd0u.read()),
    }
    s.readings["yd_x17"] = {
        **await s.both(w.registers(3, YD0, 17), lambda cl: cl.yd.read(0, 8)),
        "decoded_yd0u": await w.decoded(lambda cl: cl.yd0u.read()),
    }
    # YD's register 2 is YD1, and register 3 lies between YD1 and YD2.
    old_yd1 = _word(await w.registers(3, YD0 + 2, 1))
    old_odd = _word(await w.registers(3, YD0 + 3, 1))
    answer = await w.write_register(YD0 + 3, 0x8000)
    settled = await s.settle("yd_odd_write")
    after = await s.both(w.registers(3, YD0 + 2, 2), lambda cl: cl.yd[1])
    restored = [
        await w.write_register(address, value)
        for address, value in ((YD0 + 3, old_odd), (YD0 + 2, old_yd1))
        if value is not None
    ]
    await s.settle("yd_odd_write")
    s.readings["yd_odd_write"] = {
        "request": "FC06 0x8000 to YD register 3",
        "before": {
            "yd1": "not read" if old_yd1 is None else _hex([old_yd1])[0],
            "register_3": "not read" if old_odd is None else _hex([old_odd])[0],
        },
        "answer": answer,
        "after": after,
        "restored": restored,
        "settled": int(settled),
    }


async def _sd29_write(s: _Session) -> None:
    """FC06 to SD29 of another year, FC04 back, then the old value written back."""
    before = await s.sd(29)
    old = _word(before["raw"])
    new = 2030 if old is None else (old + 1 if old < 9999 else old - 1)
    answer = await s.w.write_register(_sd(29), new)
    settled = await s.settle("sd29_write")
    after = await s.sd(29)
    restored = await s.w.write_register(_sd(29), old) if old is not None else "not read"
    await s.settle("sd29_write")
    s.readings["sd29_write"] = {
        "request": f"FC06 SD29 = {new}",
        "before": before,
        "answer": answer,
        "after": after,
        "restored": restored,
        "settled": int(settled),
    }


async def _limits(s: _Session) -> None:
    w = s.w
    s.readings["fc03_x126"] = {"raw": _count(await w.registers(3, _ds(1), 126))}
    bits = await w.bits(1, _c(1), 1969)
    if isinstance(bits, list) and len(bits) == 1969:
        values, note = [bool(b) for b in bits], "C1..C1969 as read"
    elif s.local:
        values, note = [False] * 1969, "1969 OFF: the read of C1..C1969 failed"
    else:
        values, note = [], "not sent: the read of C1..C1969 failed"
    answer = await w.write_coils(_c(1), values) if values else "not sent"
    await s.settle("fc15_x1969")
    s.readings["fc15_x1969"] = {"read": _count(bits), "values": note, "answer": answer}
    # Last before the reset: a server may never answer it.
    s.readings["fc03_x200"] = {"raw": _count(await w.registers(3, _ds(1), 200))}


async def _stop_probes(s: _Session) -> None:
    """SC51 (watchdog reset), then SC50 (STOP): the exception, then SC11."""
    w = s.w
    answer = await w.write_coil(_sc(51), True)
    s.readings["sc51_watchdog_reset"] = {
        "answer": answer,
        "off": await w.write_coil(_sc(51), False),
        "sc11": await s.sc(11, 11),
    }
    answer = await w.write_coil(_sc(50), True)
    deadline = time.monotonic() + 2.0
    sc11 = await w.bits(2, _sc(11), 1)
    while sc11 == [1] and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        sc11 = await w.bits(2, _sc(11), 1)
    s.readings["sc50_stop"] = {
        "answer": answer,
        "sc11": {"raw": sc11, "decoded": await w.decoded(lambda cl: cl.sc[11])},
        "off": await w.write_coil(_sc(50), False),
    }


async def _probe_all(s: _Session) -> None:
    """Every probe in order, then the reset, then the probes that stop the PLC."""
    if not await _stage(s):
        return
    await _staged_reads(s)
    await _value_writes(s)
    await _all_or_nothing(s)
    await _past_banks(s)
    await _function_codes(s)
    await _xd_yd(s)
    await _sd29_write(s)
    await _limits(s)
    # The reset clears the part's memory: DF, DD, DH, TXT, DS4500, C1999..C2000.
    await s.w.click.ds.write(s.slot["Cmd"], 0)

    async def ready() -> bool:
        return await s.reg("Ready") == 1

    if not await s.until(ready, timeout=10.0):
        s.broken.append("the closing reset did not raise Ready within 10 s")
    if os.environ.get(STOP_ENV) == "1":
        await _stop_probes(s)


def _target_name(sim: type) -> str:
    return {"SimServer": "pyrung", "CircuitPySim": "circuitpy"}.get(sim.__name__, sim.__name__)


#: Where a local target's answers go, one file per target, rewritten on each run.
LOCAL_READINGS = Path(__file__).parents[2] / "build" / "conformance-modbus_raw"

_LOCAL_HEADER = """\
# What a local target answered to the raw requests of test_modbus_raw.py, rewritten each run.
"""


@pytest.mark.conformance
def test_raw_requests(
    conformance_endpoint: Endpoint | None,
    conformance_groups: list[str] | None,
    conformance_sim: type,
) -> None:
    # Imported here: suite imports this module for its group.
    from tests.conformance import suite

    if conformance_groups is not None and GROUP not in conformance_groups:
        pytest.skip(f"GROUP= leaves out {GROUP}")
    build = suite.build(GROUP)
    g = build.group(GROUP)
    ((index, _),) = g.cases
    slot = {f: g.register(index, f) for f in ("Cmd", "Scratch", "Fired", "Ready")}

    async def probe(endpoint: Endpoint, *, local: bool) -> _Session:
        async with _Wire(endpoint) as wire:
            s = _Session(wire, slot, local=local)
            found = await wire.click.ds[g.hash_register]
            if found != g.group_hash:
                s.broken.append(
                    f"{g.subroutine} is stale: repush (DS{g.hash_register} reads {found}, "
                    f"this build writes {g.group_hash})"
                )
            else:
                await _probe_all(s)
            return s

    if conformance_endpoint is None:
        with conformance_sim(build) as sim:
            s = asyncio.run(probe(sim.endpoint, local=True))
            sim.raise_if_failed()
        target = _target_name(conformance_sim)
        path, header = LOCAL_READINGS / f"{target}.toml", _LOCAL_HEADER
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        # What the ladder staged is pyrung's to get right on its own targets.
        echo = s.readings.get("staged_txt", {}).get("echo", [])
        if [e.get("decoded") for e in echo] != [65, 66, 200]:
            s.broken.append(f"the staged TXT echo reads {echo}, not 65, 66, 200")
    else:
        if read_program_hash(conformance_endpoint) != build.program_hash:
            pytest.skip(f"part {GROUP} is not loaded (make conformance-export PART={GROUP})")
        s = asyncio.run(probe(conformance_endpoint, local=False))
        target = str(conformance_endpoint)
        path, header = suite.READINGS_PATH, None
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
    if os.environ.get(STOP_ENV) == "1":
        print(f"SC50 was sent: put the PLC at {target} back in RUN.")
    assert not s.broken, "\n".join(s.broken)
