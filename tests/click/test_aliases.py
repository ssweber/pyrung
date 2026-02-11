"""Tests for Click-style constructor aliases."""

import importlib

import pytest

from pyrung.core import Block, InputBlock, InputTag, OutputBlock, OutputTag, Tag, TagType


def test_click_aliases_map_to_core_tag_types():
    from pyrung.click import Bit, Float, Hex, Int2, Txt

    assert Bit("B1").type == TagType.BOOL
    assert Int2("D1").type == TagType.DINT
    assert Float("R1").type == TagType.REAL
    assert Hex("W1").type == TagType.WORD
    assert Txt("C1").type == TagType.CHAR


def test_click_aliases_are_exported():
    click = importlib.import_module("pyrung.click")

    for alias in ("Bit", "Int2", "Float", "Hex", "Txt"):
        assert hasattr(click, alias)


def test_click_aliases_not_exported_from_core():
    core = importlib.import_module("pyrung.core")

    for alias in ("Bit", "Int2", "Float", "Hex", "Txt"):
        assert not hasattr(core, alias)


def test_click_prebuilt_blocks_are_exported():
    click = importlib.import_module("pyrung.click")

    for name in ("x", "y", "c", "t", "ct", "sc", "ds", "dd", "dh", "df", "td", "ctd", "sd", "txt"):
        assert hasattr(click, name)


def test_click_send_receive_are_exported():
    click = importlib.import_module("pyrung.click")
    assert hasattr(click, "send")
    assert hasattr(click, "receive")


def test_click_prebuilt_block_classes_and_identity_names():
    from pyrung.click import c, ct, ctd, dd, df, dh, ds, sc, sd, t, td, txt, x, y

    assert isinstance(x, InputBlock)
    assert isinstance(y, OutputBlock)
    for block in (c, t, ct, sc, ds, dd, dh, df, td, ctd, sd, txt):
        assert isinstance(block, Block)

    assert x.name == "X"
    assert y.name == "Y"
    assert ds.name == "DS"
    assert txt.name == "TXT"


def test_click_prebuilt_canonical_tag_names():
    from pyrung.click import c, ds, x, y

    assert x[1].name == "X001"
    assert y[1].name == "Y001"
    assert c[1].name == "C1"
    assert ds[1].name == "DS1"


def test_click_sparse_x_select_and_gap_rejection():
    from pyrung.click import x

    expected_addresses = tuple(range(1, 17)) + (21,)
    block = x.select(1, 21)

    assert tuple(block.addresses) == expected_addresses
    assert [tag.name for tag in block] == [f"X{addr:03d}" for addr in expected_addresses]
    with pytest.raises(IndexError):
        x[17]


def test_click_prebuilt_type_and_retentive_defaults():
    from pyrung.click import c, ct, ctd, dd, df, dh, ds, sc, sd, t, td, txt, x, y

    expected: dict[str, tuple[TagType, bool]] = {
        "x": (TagType.BOOL, False),
        "y": (TagType.BOOL, False),
        "c": (TagType.BOOL, False),
        "t": (TagType.BOOL, False),
        "ct": (TagType.BOOL, True),
        "sc": (TagType.BOOL, False),
        "ds": (TagType.INT, True),
        "dd": (TagType.DINT, True),
        "dh": (TagType.WORD, True),
        "df": (TagType.REAL, True),
        "td": (TagType.INT, False),
        "ctd": (TagType.DINT, True),
        "sd": (TagType.INT, False),
        "txt": (TagType.CHAR, True),
    }
    blocks = {
        "x": x,
        "y": y,
        "c": c,
        "t": t,
        "ct": ct,
        "sc": sc,
        "ds": ds,
        "dd": dd,
        "dh": dh,
        "df": df,
        "td": td,
        "ctd": ctd,
        "sd": sd,
        "txt": txt,
    }

    for name, block in blocks.items():
        expected_type, expected_retentive = expected[name]
        assert block.type == expected_type
        assert block.retentive is expected_retentive


def test_click_prebuilt_tag_classes():
    from pyrung.click import ds, x, y

    assert isinstance(x[1], InputTag)
    assert isinstance(y[1], OutputTag)
    assert isinstance(ds[1], Tag)
