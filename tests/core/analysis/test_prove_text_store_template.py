"""A text store keeps the blockless kernel's template to the tags the program names.

``to_text()`` into TXT1 writes TXT2 and on, by name.  The prover takes its fixed
keys from the blockless template, so an element there that the store writes and
no PDG write site lists would be a constant the search never restores: a false
Proven.  The board code keeps such a block whole; the blockless kernel must not.
"""

from __future__ import annotations

import pytest

from pyrung.circuitpy.codegen import compile_kernel
from pyrung.click import ClickBlocks
from pyrung.core import Block, Bool, Or, Program, Rung, TagType, copy, latch, out, to_text
from pyrung.core.analysis.prove import Proven, always
from pyrung.core.analysis.prove import kernel as prove_kernel


@pytest.fixture(autouse=True)
def _verify_the_write_set(monkeypatch):
    """Every scan checks the kernel wrote no tag outside the mutable set."""
    monkeypatch.setattr(prove_kernel, "_VERIFY_MUTABLE_SET", True)


def _program(read: str, *, latched: bool = False) -> Program:
    """B picks DS1 as 12 or 3; its text goes into TXT1 and on; *read* in TXT1 sets Y."""
    blocks = ClickBlocks()
    ds, txt = blocks.ds, blocks.txt
    b, latch_bit, y = Bool("B"), Bool("L"), Bool("Y")
    with Program(strict=False) as prog:
        with Rung(b):
            if latched:
                latch(latch_bit)
            copy(12, ds[1])
        with Rung(~b):
            copy(3, ds[1])
        with Rung():
            copy(ds[1], txt[1], convert=to_text())
        with Rung(txt[1] == read):
            out(y)
    return prog


def test_the_blockless_template_holds_only_the_named_elements():
    compiled = compile_kernel(_program("3"), blockless=True)
    assert "TXT1" in compiled._tag_template
    assert "TXT2" not in compiled._tag_template
    assert not any(name.startswith("TXT") and name != "TXT1" for name in compiled._tag_template)


def test_an_element_only_the_store_writes_is_no_proof():
    """B=1 then B=0 leaves TXT2 "2" from "12" and TXT1 "3": the claim is false."""
    txt = ClickBlocks().txt
    claim = Or(txt[2] != "2", txt[1] != "3")
    result = always(_program("3"), claim, max_states=20_000, depth_budget=20)
    assert not isinstance(result, Proven)

    def callable_claim(state):
        return not (state.get("TXT2", "\x00") == "2" and state.get("TXT1") == "3")

    result = always(_program("3"), callable_claim, max_states=20_000, depth_budget=20)
    assert not isinstance(result, Proven)


def test_a_true_claim_over_the_store_still_proves():
    """Y reads TXT1 "1", from "12" only, so only after B, which latches L."""
    y, latch_bit = Bool("Y"), Bool("L")
    result = always(
        _program("1", latched=True), Or(~y, latch_bit), max_states=20_000, depth_budget=20
    )
    assert isinstance(result, Proven)


@pytest.mark.parametrize("name", ["Q{0}", r"Q\x", 'Q"x'], ids=["braces", "backslash", "dquote"])
def test_a_store_into_a_block_of_any_name_is_no_proof(name):
    """The blockless kernel writes ``Q{0}2`` as named, so Seen is reachable."""
    blk = Block(name, TagType.CHAR, 1, 10)
    go, seen = Bool("Go"), Bool("Seen")
    with Program(strict=False) as prog:
        with Rung(go):
            copy("ABC", blk[1])
        with Rung(blk[2] == "B"):
            out(seen)
    result = always(prog, ~seen, max_states=5_000, depth_budget=10)
    assert not isinstance(result, Proven)
