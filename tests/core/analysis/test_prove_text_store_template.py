"""A text store keeps the blockless kernel's template to the tags the program names.

``to_text()`` into TXT1 writes TXT2 and on, by name.  The board code keeps such a
block whole, and the blockless kernel must not.
"""

from __future__ import annotations

from pyrung.circuitpy.codegen import compile_kernel
from pyrung.click import ClickBlocks
from pyrung.core import Bool, Program, Rung, copy, out, to_text


def _program(read: str) -> Program:
    """B picks DS1 as 12 or 3; its text goes into TXT1 and on; *read* in TXT1 sets Y."""
    blocks = ClickBlocks()
    ds, txt = blocks.ds, blocks.txt
    b, y = Bool("B"), Bool("Y")
    with Program(strict=False) as prog:
        with Rung(b):
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
