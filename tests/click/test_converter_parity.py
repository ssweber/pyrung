"""Check that copy() refuses at build the converter pairs that laddercodec refuses.

pyrung keeps its own converter rule, so that a program fails when you build it, not when you
export it.  laddercodec's ``Copy`` judges each pair as CLICK's Copy dialog offers it.  For
every source, target and converter below, the test builds laddercodec's ``Copy`` and notes
whether it raises a ``FieldError`` with the code ``copy-converter-bank``.  Then it builds the
same ``copy()`` in pyrung and notes whether it raises a converter refusal.  The two answers
must agree, except for the pairs that the sets at the end of this module list.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from laddercodec import Code, Copy, FieldError

from pyrung import Program, copy, rung
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


def _bank(text: str) -> str | None:
    """Return the bank of an operand's text, or None for a constant."""
    bank = text.split("[")[0].rstrip("0123456789")
    return bank or None


def _pyrung_builds_what_laddercodec_refuses(source: str, target: str, converter: str) -> bool:
    """Return whether the pair is one that pyrung's rule does not check yet.

    pyrung checks a converter against its target, and to_binary into a number against its
    source too.  laddercodec also refuses to_value and to_ascii from a source that is not
    TXT, and to_text and to_binary from TXT into TXT.
    """
    from_text = _bank(source) == "TXT"
    if converter in ("to_value", "to_ascii"):
        # pyrung refuses both into TXT and DF whatever the source.
        return not from_text and _bank(target) not in ("TXT", "DF")
    return from_text and _bank(target) == "TXT"


#: The pairs that laddercodec refuses and pyrung builds.
ONLY_LADDERCODEC_REFUSES = frozenset(
    (source, target, converter)
    for source, _ in SOURCES
    for target, _ in TARGETS
    for converter in CONVERTERS
    if _pyrung_builds_what_laddercodec_refuses(source, target, converter)
)

#: The pairs that pyrung refuses and the installed laddercodec (0.5.1) accepts.  Empty this at
#: the next laddercodec pin.  It holds no pair at 0.5.1, so the test fails if one appears.
ONLY_PYRUNG_REFUSES: frozenset[tuple[str, str, str]] = frozenset()


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
