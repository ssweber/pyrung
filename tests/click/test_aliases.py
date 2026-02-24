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

    for name in (
        "x",
        "y",
        "c",
        "t",
        "ct",
        "sc",
        "ds",
        "dd",
        "dh",
        "df",
        "xd",
        "yd",
        "xd0u",
        "yd0u",
        "td",
        "ctd",
        "sd",
        "txt",
    ):
        assert hasattr(click, name)


def test_click_send_receive_are_exported():
    click = importlib.import_module("pyrung.click")
    assert hasattr(click, "send")
    assert hasattr(click, "receive")


def test_click_prebuilt_block_classes_and_identity_names():
    from pyrung.click import (
        c,
        ct,
        ctd,
        dd,
        df,
        dh,
        ds,
        sc,
        sd,
        t,
        td,
        txt,
        x,
        xd,
        xd0u,
        y,
        yd,
        yd0u,
    )

    assert isinstance(x, InputBlock)
    assert isinstance(xd, InputBlock)
    assert isinstance(y, OutputBlock)
    assert isinstance(yd, OutputBlock)
    assert isinstance(xd0u, InputTag)
    assert isinstance(yd0u, OutputTag)
    for block in (c, t, ct, sc, ds, dd, dh, df, td, ctd, sd, txt):
        assert isinstance(block, Block)

    assert x.name == "X"
    assert y.name == "Y"
    assert xd.name == "XD"
    assert yd.name == "YD"
    assert ds.name == "DS"
    assert txt.name == "TXT"


def test_click_prebuilt_canonical_tag_names():
    from pyrung.click import c, ds, x, xd, xd0u, y, yd, yd0u

    assert x[1].name == "X001"
    assert y[1].name == "Y001"
    assert c[1].name == "C1"
    assert ds[1].name == "DS1"
    assert xd[0].name == "XD0"
    assert xd[1].name == "XD1"
    assert xd[3].name == "XD3"
    assert xd0u.name == "XD0u"
    assert yd[0].name == "YD0"
    assert yd[1].name == "YD1"
    assert yd[3].name == "YD3"
    assert yd0u.name == "YD0u"


def test_click_sparse_x_select_and_gap_rejection():
    from pyrung.click import x

    expected_addresses = tuple(range(1, 17)) + (21,)
    block = x.select(1, 21)

    assert tuple(block.addresses) == expected_addresses
    assert [tag.name for tag in block] == [f"X{addr:03d}" for addr in expected_addresses]
    with pytest.raises(IndexError):
        x[17]


def test_click_xd_yd_display_indexed_select():
    from pyrung.click import xd, yd

    assert tuple(xd.select(0, 4).addresses) == (0, 1, 2, 3, 4)
    assert [tag.name for tag in xd.select(0, 4)] == ["XD0", "XD1", "XD2", "XD3", "XD4"]
    assert tuple(yd.select(0, 4).addresses) == (0, 1, 2, 3, 4)
    assert [tag.name for tag in yd.select(0, 4)] == ["YD0", "YD1", "YD2", "YD3", "YD4"]
    assert xd[8].name == "XD8"
    assert yd[8].name == "YD8"
    with pytest.raises(IndexError):
        xd[9]
    with pytest.raises(IndexError):
        yd[9]


def test_click_prebuilt_type_and_retentive_defaults():
    from pyrung.click import (
        c,
        ct,
        ctd,
        dd,
        df,
        dh,
        ds,
        sc,
        sd,
        t,
        td,
        txt,
        x,
        xd,
        xd0u,
        y,
        yd,
        yd0u,
    )

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
        "xd": (TagType.WORD, False),
        "yd": (TagType.WORD, False),
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
        "xd": xd,
        "yd": yd,
        "td": td,
        "ctd": ctd,
        "sd": sd,
        "txt": txt,
    }

    for name, block in blocks.items():
        expected_type, expected_retentive = expected[name]
        assert block.type == expected_type
        assert block.retentive is expected_retentive

    assert xd0u.type == TagType.WORD
    assert yd0u.type == TagType.WORD
    assert xd0u.retentive is False
    assert yd0u.retentive is False


def test_click_prebuilt_tag_classes():
    from pyrung.click import ds, x, xd, xd0u, y, yd, yd0u

    assert isinstance(x[1], InputTag)
    assert isinstance(xd[0], InputTag)
    assert isinstance(xd0u, InputTag)
    assert isinstance(y[1], OutputTag)
    assert isinstance(yd[0], OutputTag)
    assert isinstance(yd0u, OutputTag)
    assert isinstance(ds[1], Tag)


def test_click_prebuilt_block_allows_in_place_slot_policy_before_materialization():
    from pyrung.click import ds

    candidate = next(
        (addr for addr in range(ds.end, ds.start - 1, -1) if addr not in ds._tag_cache), None
    )
    if candidate is None:
        pytest.skip("No unmaterialized DS slot available for in-place policy test.")

    baseline = ds.slot_config(candidate)
    ds.configure_slot(candidate, retentive=not baseline.retentive, default=1234)
    configured = ds.slot_config(candidate)
    assert configured.retentive is (not baseline.retentive)
    assert configured.default == 1234

    ds.clear_slot_config(candidate)
    restored = ds.slot_config(candidate)
    assert restored.retentive == baseline.retentive
    assert restored.default == baseline.default
