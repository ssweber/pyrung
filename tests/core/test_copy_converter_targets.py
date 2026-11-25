"""copy() refuses a converter that cannot store into the type of its target.

The refused pairs are the ones that the CLICK export refuses for every source.  copy() also
refuses a to_binary into a Dint or a Real from a source that CLICK's Copy dialog was read
offering no Copy Binary for.  fill() refuses a number register into Char tags, which
CLICK's Fill dialog was read refusing with OBJ-024.  fill() also refuses a Real constant
into Char tags, which a CLICK was read writing past the end of the range.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from pyrung import Char, Int, Program, copy, fill, rung
from pyrung.click import ClickBlocks
from pyrung.core import to_ascii, to_binary, to_text, to_value

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

CONVERTERS = {
    "to_value": to_value,
    "to_ascii": to_ascii,
    "to_text()": to_text(),
    "to_binary": to_binary,
}

TARGETS: dict[str, Callable[[], Any]] = {
    "Bool": lambda: c[2],
    "Int": lambda: ds[2],
    "Dint": lambda: dd[2],
    "Word": lambda: dh[2],
    "Real": lambda: df[2],
    "Char": lambda: txt[2],
}

# The source that each converter reads, so that an accepted pair can build.  to_binary into
# a Real reads a Dint, and into any other target it reads a Real.
SOURCES = {"to_value": txt[5], "to_ascii": txt[5], "to_text()": ds[1], "to_binary": df[1]}


def _source(converter: str, target: str) -> Any:
    return dd[1] if (converter, target) == ("to_binary", "Real") else SOURCES[converter]


ACCEPTED = {
    "to_value": {"Int", "Dint", "Word"},
    "to_ascii": {"Int", "Dint", "Word"},
    "to_text()": {"Char"},
    "to_binary": {"Char", "Dint", "Real"},
}


def _build(source: Any, target: Any, converter: Any) -> Program:
    with Program(strict=False) as program:
        with rung(x[1]):
            copy(source, target, convert=converter)
    return program


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("converter", CONVERTERS)
def test_a_converter_builds_only_into_a_type_that_it_stores_into(
    converter: str, target: str
) -> None:
    def build() -> Program:
        return _build(_source(converter, target), TARGETS[target](), CONVERTERS[converter])

    if target in ACCEPTED[converter]:
        build()
        return
    with pytest.raises(ValueError, match=r"^copy\(\) got convert=") as refused:
        build()
    message = str(refused.value)
    assert message.isascii()
    assert f"convert={converter} with the {target} tag " in message
    assert "Leave convert out" in message


def test_the_refusal_names_the_target_and_what_to_write() -> None:
    with pytest.raises(ValueError) as refused:
        _build(5, txt[1], to_value)
    assert str(refused.value) == (
        "copy() got convert=to_value with the Char tag TXT1 as the target. to_value stores "
        "only into an Int, Dint or Word tag. Leave convert out, or copy into a tag of one "
        "of those types."
    )


def test_a_pointer_target_is_refused_by_its_block_type() -> None:
    with pytest.raises(ValueError, match=r"the Char pointer TXT\[DS100\] as the target"):
        _build(ds[1], txt[ds[100]], to_ascii)


def test_a_txt_range_target_is_named_as_written() -> None:
    with pytest.raises(ValueError, match=r"with the range TXT2\.\.TXT3 of Char tags as the target"):
        _build(txt[1], txt.select(2, 3), to_value)


_DINT_TARGETS = {
    "DD1": (lambda: dd[1], "the Dint tag DD1"),
    "DD[DS5]": (lambda: dd[ds[5]], "the Dint pointer DD[DS5]"),
}
_REAL_TARGETS = {
    "DF1": (lambda: df[1], "the Real tag DF1"),
    "DF[DS5]": (lambda: df[ds[5]], "the Real pointer DF[DS5]"),
}

# Each source that CLICK's Copy dialog offers no Copy Binary for, into a DD and into a DF.
_REFUSED_SOURCES: dict[str, tuple[Callable[[], Any], str, set[str]]] = {
    "55": (lambda: 55, "the constant 55", {"Dint", "Real"}),
    "-2": (lambda: -2, "the constant -2", {"Dint", "Real"}),
    "32767": (lambda: 32767, "the constant 32767", {"Real"}),
    "-32768": (lambda: -32768, "the constant -32768", {"Real"}),
    "40000": (lambda: 40000, "the constant 40000", {"Dint"}),
    "1.5": (lambda: 1.5, "the constant 1.5", {"Real"}),
    "DS1": (lambda: ds[1], "the Int tag DS1", {"Dint", "Real"}),
    "DS[DS2]": (lambda: ds[ds[2]], "the Int pointer DS[DS2]", {"Dint", "Real"}),
    "TD1": (lambda: td[1], "the Int tag TD1", {"Dint", "Real"}),
    "DH1": (lambda: dh[1], "the Word tag DH1", {"Dint", "Real"}),
    "DD2": (lambda: dd[2], "the Dint tag DD2", {"Dint"}),
    "DF2": (lambda: df[2], "the Real tag DF2", {"Real"}),
}


_REFUSED_PAIRS = [
    pytest.param(source, kind, address, id=f"{source} -> {address}")
    for source, (_, _, refused_into) in _REFUSED_SOURCES.items()
    for kind, targets in (("Dint", _DINT_TARGETS), ("Real", _REAL_TARGETS))
    if kind in refused_into
    for address in targets
]


@pytest.mark.parametrize("source,kind,address", _REFUSED_PAIRS)
def test_to_binary_into_a_number_from_a_source_that_clicks_dialog_refuses(
    source: str, kind: str, address: str
) -> None:
    build_source, shown, _ = _REFUSED_SOURCES[source]
    build_target, named = (_DINT_TARGETS if kind == "Dint" else _REAL_TARGETS)[address]
    with pytest.raises(ValueError) as refused:
        _build(build_source(), build_target(), to_binary)
    assert str(refused.value) == (
        f"copy() got convert=to_binary with {shown} as the source and {named} as the target. "
        "to_binary stores into a Dint only from a Real tag or a constant with a fraction, and "
        "into a Real only from a Dint tag or an integer constant outside -32768..32767. "
        "Leave convert out."
    )


@pytest.mark.parametrize(
    "source,target",
    [
        (lambda: 1.5, lambda: dd[1]),
        (lambda: -0.5, lambda: dd[ds[5]]),
        (lambda: df[1], lambda: dd[1]),
        (lambda: df[ds[2]], lambda: dd[ds[5]]),
        (lambda: 40000, lambda: df[1]),
        (lambda: -32769, lambda: df[ds[5]]),
        (lambda: dd[1], lambda: df[1]),
        (lambda: ctd[1], lambda: df[1]),
    ],
)
def test_to_binary_into_a_number_builds_from_a_source_that_clicks_dialog_offers(
    source, target
) -> None:
    _build(source(), target(), to_binary)


# Each of these forms makes a TXT hold a number.  check() and the how() prior raise
# TypeError on such a TXT, so copy() must refuse the form.
_SWEEP_SOURCES: dict[str, Callable[[], Any]] = {
    "5": lambda: 5,
    "12": lambda: 12,
    "Int tag": lambda: Int("SrcI"),
    "DS1": lambda: ds[1],
    "TXT10": lambda: txt[10],
    "Char tag": lambda: Char("SrcC"),
}
_SWEEP_TARGETS: dict[str, Callable[[], Any]] = {
    "TXT1": lambda: txt[1],
    "TXT1..TXT1": lambda: txt.select(1, 1),
    "TXT1..TXT3": lambda: txt.select(1, 3),
}


@pytest.mark.parametrize("target", _SWEEP_TARGETS)
@pytest.mark.parametrize("converter", ["to_value", "to_ascii"])
@pytest.mark.parametrize("source", _SWEEP_SOURCES)
def test_a_text_to_number_converter_into_a_txt_is_refused_before_check(
    source: str, converter: str, target: str
) -> None:
    with pytest.raises(ValueError, match=rf"^copy\(\) got convert={converter} with the "):
        _build(_SWEEP_SOURCES[source](), _SWEEP_TARGETS[target](), CONVERTERS[converter])


@pytest.mark.parametrize(
    "value,shown",
    [
        (lambda: ds[1], "the Int tag DS1"),
        (lambda: ds[ds[2]], "the Int pointer DS[DS2]"),
        (lambda: dd[1], "the Dint tag DD1"),
        (lambda: dh[1], "the Word tag DH1"),
        (lambda: df[1], "the Real tag DF1"),
        (lambda: Int("Count"), "the Int tag Count"),
        (lambda: 1.5, "the constant 1.5"),
        (lambda: 5.0, "the constant 5.0"),
    ],
)
def test_fill_of_a_number_register_or_a_real_constant_into_char_tags_is_refused(
    value, shown
) -> None:
    with pytest.raises(ValueError) as refused:
        with Program(strict=False):
            with rung(x[1]):
                fill(value(), txt.select(1, 3))
    assert str(refused.value) == (
        f"fill() got {shown} as the value and the range TXT1..TXT3 of Char tags as the "
        "target. fill() writes only an integer or a text constant into Char tags. Fill one "
        "of those, or copy() the value into one Char tag."
    )


@pytest.mark.parametrize("value", [5, 12, "A", txt[10]], ids=["5", "12", "'A'", "TXT10"])
def test_fill_of_a_constant_or_a_char_into_char_tags_builds(value) -> None:
    with Program(strict=False):
        with rung(x[1]):
            fill(value, txt.select(1, 3))
