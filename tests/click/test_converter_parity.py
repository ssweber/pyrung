"""Check that copy() refuses at build the converter pairs that laddercodec refuses.

pyrung keeps its own converter rule, so that a program fails when you build it, not when you
export it.  laddercodec's ``Copy`` judges each pair as CLICK's Copy dialog offers it.  For
every source, target and converter below, the test builds laddercodec's ``Copy`` and notes
whether it raises a ``FieldError`` with the code ``copy-converter-bank``.  Then it builds the
same ``copy()`` in pyrung and notes whether it raises a converter refusal.  The two answers
must agree, except for the pairs that ``ONLY_LADDERCODEC_REFUSES`` and
``ONLY_PYRUNG_REFUSES`` list.

A second test checks a Bool source into TXT the same way, for Copy and for Fill.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from laddercodec import Code, Copy, FieldError, Fill

from pyrung import Program, copy, fill, rung
from pyrung.click import ClickBlocks
from pyrung.core import to_ascii, to_binary, to_text, to_value

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
_CONSTANTS = (55, -2, 32767, -32768, 40000, -32769, 1.5, -0.5)
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


SOURCES = _operands(tuple(_BANKS), 1) + [
    (str(value), lambda value=value: value) for value in _CONSTANTS
]
TARGETS = _operands(_TARGET_BANKS, 2)
CONVERTERS = {
    "to_value": to_value,
    "to_ascii": to_ascii,
    "to_text": to_text(),
    "to_binary": to_binary,
}


def _laddercodec_refuses(source: str, target: str, converter: str) -> bool:
    try:
        Copy(source, target, convert=converter)
    except FieldError as refused:
        return any(error.code is Code.COPY_CONVERTER_BANK for error in refused.errors)
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
ONLY_PYRUNG_REFUSES = frozenset(
    (source, target, "to_binary")
    for source in ("DF1", "DF[DS1]")
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
                ours = _refuses(_pyrung_copy, source(), target(), CONVERTERS.get(name))
                if ours != (theirs or (constant and convert is not None)):
                    differ.append((source_text, target_text, name))
        theirs = _refuses(Fill, source_text, "TXT2", "TXT4")
        if _refuses(_pyrung_fill, source()) != theirs:
            differ.append((source_text, "TXT2..TXT4", "fill"))
    assert differ == []
