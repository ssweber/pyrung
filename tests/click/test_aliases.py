"""Tests for Click-style constructor aliases."""

import importlib

from pyrung.core import TagType


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
