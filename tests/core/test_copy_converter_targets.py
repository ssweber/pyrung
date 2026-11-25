"""copy() refuses a converter that cannot store into the type of its target.

The refused pairs are the ones that the CLICK export refuses for every source.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from pyrung import Char, Int, Program, copy, rung
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

# The source that each converter reads, so that an accepted pair can build.
SOURCES = {"to_value": txt[5], "to_ascii": txt[5], "to_text()": ds[1], "to_binary": ds[1]}

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
        return _build(SOURCES[converter], TARGETS[target](), CONVERTERS[converter])

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
