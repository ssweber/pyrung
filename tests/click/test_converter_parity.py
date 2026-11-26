"""Check that copy() refuses at build the converter pairs that laddercodec refuses.

pyrung keeps its own converter rule, so that a program fails when you build it, not when you
export it.  laddercodec's ``Copy`` judges each pair as CLICK's Copy dialog offers it.  For
every source, target and converter below, the test builds laddercodec's ``Copy`` and notes
whether it raises a ``FieldError`` with the code ``copy-converter-bank`` or
``copy-bank-incompatible``.  laddercodec gives the second code for a text constant.  Then
it builds the same ``copy()`` in pyrung and notes whether it raises a converter refusal.
The two answers must agree, except for the pairs that ``ONLY_LADDERCODEC_REFUSES`` and
``ONLY_PYRUNG_REFUSES`` list.

A second test checks a Bool source into TXT the same way, for Copy and for Fill.  The last
three tests check which Block Copy, Fill and Receive into TXT each side refuses.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from laddercodec import BlockCopy, Code, Copy, FieldError, Fill, Receive, Send
from laddercodec.instructions import send_receive as laddercodec_modbus

from pyrung import Program, blockcopy, copy, fill, rung
from pyrung.click import ClickBlocks
from pyrung.core import to_ascii, to_binary, to_text, to_value
from pyrung.core.instruction.send_receive import ModbusAddress, RegisterType, receive, send

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

_BANKS = {
    "DS": ds,
    "DD": dd,
    "DH": dh,
    "DF": df,
    "TXT": txt,
    "XD": xd,
    "YD": yd,
    "TD": td,
    "CTD": ctd,
    "SD": sd,
}
#: The banks that CLICK lets a pointer address.
_POINTER_BANKS = ("DS", "DD", "DH", "DF", "TXT")
_CONSTANTS = (55, -2, 32767, -32768, 40000, -32769, 1.5, -0.5, 2.0)
_TARGET_BANKS = ("DS", "DD", "DH", "DF", "TXT", "YD")

#: Each operand as laddercodec's text and as a function that returns pyrung's operand.
Operand = tuple[str, Callable[[], Any]]


def _operands(banks: tuple[str, ...], number: int) -> list[Operand]:
    operands: list[Operand] = []
    for bank in banks:
        block = _BANKS[bank]
        operands.append((f"{bank}{number}", lambda block=block: block[number]))
        if bank in _POINTER_BANKS:
            operands.append(
                (f"{bank}[DS{number}]", lambda block=block: block[ds[number]]),
            )
    return operands


SOURCES = (
    _operands(tuple(_BANKS), 1)
    + [(str(value), lambda value=value: value) for value in _CONSTANTS]
    + [('"5"', lambda: "5")]
)
TARGETS = _operands(_TARGET_BANKS, 2)
CONVERTERS = {
    "to_value": to_value,
    "to_ascii": to_ascii,
    "to_text": to_text(),
    "to_binary": to_binary,
}


#: The codes of a laddercodec refusal that the converter and the source decide.
_CONVERTER_CODES = (Code.COPY_CONVERTER_BANK, Code.COPY_BANK_INCOMPATIBLE)


def _laddercodec_refuses(source: str, target: str, converter: str) -> bool:
    try:
        Copy(source, target, convert=converter)
    except FieldError as refused:
        return any(error.code in _CONVERTER_CODES for error in refused.errors)
    return False


def _pyrung_refuses(source: Any, target: Any, converter: Any) -> bool:
    try:
        with Program(strict=False):
            with rung(x[1]):
                copy(source, target, convert=converter)
    except ValueError as refused:
        if str(refused).startswith("copy() got convert="):
            return True
        raise
    return False


#: The pairs that laddercodec refuses and pyrung builds.
ONLY_LADDERCODEC_REFUSES: frozenset[tuple[str, str, str]] = frozenset()

#: The pairs that pyrung refuses and the installed laddercodec (0.5.1) accepts.  Empty this at
#: the next laddercodec pin.  CLICK 4.00's Copy dialog hides Copy Binary from a DF into a TXT.
#: laddercodec main (#182) refuses Copy Binary into a TXT from every Real source.
ONLY_PYRUNG_REFUSES = frozenset(
    (source, target, "to_binary")
    for source in ("DF1", "DF[DS1]", "1.5", "-0.5", "2.0")
    for target in ("TXT2", "TXT[DS2]")
)


def test_copy_refuses_the_converter_pairs_that_laddercodec_refuses() -> None:
    only_laddercodec: set[tuple[str, str, str]] = set()
    only_pyrung: set[tuple[str, str, str]] = set()
    for source_text, source in SOURCES:
        for target_text, target in TARGETS:
            for name, converter in CONVERTERS.items():
                pair = (source_text, target_text, name)
                theirs = _laddercodec_refuses(source_text, target_text, name)
                ours = _pyrung_refuses(source(), target(), converter)
                if theirs and not ours:
                    only_laddercodec.add(pair)
                elif ours and not theirs:
                    only_pyrung.add(pair)
    assert only_pyrung == ONLY_PYRUNG_REFUSES
    assert only_laddercodec == ONLY_LADDERCODEC_REFUSES


#: Each Bool source as laddercodec's text and as pyrung's operand.  pyrung's export writes
#: True and False as 1 and 0.
_BOOL_SOURCES: list[Operand] = [
    ("C1", lambda: c[1]),
    ("X1", lambda: x[1]),
    ("Y1", lambda: y[1]),
    ("T1", lambda: t[1]),
    ("CT1", lambda: ct[1]),
    ("SC1", lambda: sc[1]),
    ("1", lambda: True),
    ("0", lambda: False),
]


def _refuses(build: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
    try:
        build(*args, **kwargs)
    except ValueError:
        return True
    return False


def _refuses_a_bool(build: Callable[..., Any], *args: Any) -> bool:
    """Return whether pyrung refuses the form for its Bool source, and raise any other refusal."""
    try:
        build(*args)
    except ValueError as refused:
        message = str(refused)
        if (
            message.startswith(("copy() got the ", "fill() got the "))
            and "writes no Bool" in message
        ):
            return True
        raise
    return False


def _pyrung_copy(source: Any, target: Any, convert: Any) -> None:
    with Program(strict=False):
        with rung(x[2]):
            copy(source, target, convert=convert)


def _pyrung_fill(value: Any) -> None:
    with Program(strict=False):
        with rung(x[2]):
            fill(value, txt.select(2, 4))


def test_copy_and_fill_refuse_a_bool_source_into_txt_as_laddercodec_does() -> None:
    """A Bool tag is refused into TXT with any converter, and so is a Copy of a constant.

    laddercodec accepts a Fill of 1 into TXT, and pyrung builds the ``fill(True, ...)`` that
    exports as that Fill.  copy() also refuses a Bool constant with ``to_text()`` or
    ``to_binary``, which laddercodec accepts as a Copy of 1.
    """
    differ: list[tuple[str, str, str]] = []
    for source_text, source in _BOOL_SOURCES:
        constant = source_text in ("1", "0")
        for target_text, target in _operands(("TXT",), 2):
            for name in ("none", *CONVERTERS):
                convert = None if name == "none" else name
                theirs = _refuses(Copy, source_text, target_text, convert=convert)
                ours = _refuses_a_bool(_pyrung_copy, source(), target(), CONVERTERS.get(name))
                if ours != (theirs or (constant and convert is not None)):
                    differ.append((source_text, target_text, name))
        theirs = _refuses(Fill, source_text, "TXT2", "TXT4")
        if _refuses_a_bool(_pyrung_fill, source()) != theirs:
            differ.append((source_text, "TXT2..TXT4", "fill"))
    assert differ == []


# --- Block Copy, Fill and Receive into TXT ----------------------------------------------

#: Every CLICK bank as laddercodec's first two addresses and pyrung's block.
_ALL_BANKS = {
    "X": (("X1", "X2"), x, 1),
    "Y": (("Y1", "Y2"), y, 1),
    "C": (("C1", "C2"), c, 1),
    "T": (("T1", "T2"), t, 1),
    "CT": (("CT1", "CT2"), ct, 1),
    "SC": (("SC1", "SC2"), sc, 1),
    "DS": (("DS1", "DS2"), ds, 1),
    "DD": (("DD1", "DD2"), dd, 1),
    "DH": (("DH1", "DH2"), dh, 1),
    "DF": (("DF1", "DF2"), df, 1),
    "XD": (("XD0", "XD1"), xd, 0),
    "YD": (("YD0", "YD1"), yd, 0),
    "TD": (("TD1", "TD2"), td, 1),
    "CTD": (("CTD1", "CTD2"), ctd, 1),
    "SD": (("SD1", "SD2"), sd, 1),
    "TXT": (("TXT1", "TXT2"), txt, 1),
}


def _laddercodec_refuses_any(build: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
    try:
        build(*args, **kwargs)
    except FieldError:
        return True
    return False


def _pyrung_refuses_any(write: Callable[[], Any]) -> bool:
    try:
        with Program(strict=False):
            with rung(x[3]):
                write()
    except (TypeError, ValueError):
        return True
    return False


def test_blockcopy_into_txt_refuses_what_laddercodec_refuses() -> None:
    differ: list[tuple[str, str]] = []
    for bank, ((first, second), block, start) in _ALL_BANKS.items():
        for name, convert in (("none", None), ("to_value", to_value), ("to_ascii", to_ascii)):
            theirs = _laddercodec_refuses_any(
                BlockCopy,
                first,
                second,
                "TXT11",
                "TXT12",
                convert=None if convert is None else name,
            )
            ours = _pyrung_refuses_any(
                lambda block=block, start=start, convert=convert: blockcopy(
                    block.select(start, start + 1), txt.select(11, 12), convert=convert
                )
            )
            if ours != theirs:
                differ.append((bank, name))
    assert differ == []


#: The values of a Fill into TXT that only pyrung refuses.  A CLICK writes a Real
#: constant's text past the end of the range.
ONLY_PYRUNG_REFUSES_FILL = frozenset({"1.5"})


def test_fill_into_txt_refuses_what_laddercodec_refuses() -> None:
    values: list[tuple[str, Any]] = [
        (first, lambda block=block, start=start: block[start])
        for (first, _), block, start in _ALL_BANKS.values()
    ]
    values += [
        ("5", lambda: 5),
        ("12", lambda: 12),
        ("1.5", lambda: 1.5),
        ('"A"', lambda: "A"),
        ('"AB"', lambda: "AB"),
        ("1", lambda: True),
        ("0", lambda: False),
    ]
    only_laddercodec: set[str] = set()
    only_pyrung: set[str] = set()
    for text, value in values:
        theirs = _laddercodec_refuses_any(Fill, text, "TXT11", "TXT13")
        ours = _pyrung_refuses_any(lambda value=value: fill(value(), txt.select(11, 13)))
        if theirs and not ours:
            only_laddercodec.add(text)
        elif ours and not theirs:
            only_pyrung.add(text)
    assert only_pyrung == ONLY_PYRUNG_REFUSES_FILL
    assert only_laddercodec == set()


_LADDERCODEC_PEER = laddercodec_modbus.ModbusTcpTarget(
    name="peer", ip="192.168.1.5", port=502, device_id=1
)
#: Each raw register type as laddercodec's 984 address and pyrung's ``ModbusAddress``.
_RAW_STARTS = {
    "holding": ("400001", ModbusAddress(0, RegisterType.HOLDING)),
    "input": ("300001", ModbusAddress(0, RegisterType.INPUT)),
    "coil": ("000001", ModbusAddress(0, RegisterType.COIL)),
    "discrete input": ("100001", ModbusAddress(0, RegisterType.DISCRETE_INPUT)),
}


@pytest.mark.parametrize(
    "remote", [*(pair[0] for pair in _ALL_BANKS.values()), *_RAW_STARTS], ids=str
)
def test_receive_into_txt_refuses_what_laddercodec_refuses(remote: Any) -> None:
    if isinstance(remote, tuple):
        theirs_start, ours_start = remote[0], remote[0]
    else:
        theirs_start = laddercodec_modbus.ModbusAddress(_RAW_STARTS[remote][0])
        ours_start = _RAW_STARTS[remote][1]
    theirs = _laddercodec_refuses_any(
        Receive, _LADDERCODEC_PEER, theirs_start, "TXT11", 2, "C1", "C2", "C3", "DS1"
    )
    ours = _pyrung_refuses_any(
        lambda: receive(target="peer", remote_start=ours_start, dest=txt.select(11, 12))
    )
    assert ours == theirs


@pytest.mark.parametrize("remote", ["holding", "coil"])
def test_send_of_txt_to_a_raw_address_refuses_what_laddercodec_refuses(remote: str) -> None:
    theirs = _laddercodec_refuses_any(
        Send,
        _LADDERCODEC_PEER,
        laddercodec_modbus.ModbusAddress(_RAW_STARTS[remote][0]),
        "TXT11",
        2,
        "C1",
        "C2",
        "C3",
        "DS1",
    )
    ours = _pyrung_refuses_any(
        lambda: send(target="peer", remote_start=_RAW_STARTS[remote][1], source=txt.select(11, 12))
    )
    assert ours == theirs
