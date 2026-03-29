"""Click ladder CSV export helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._exporter import LadderBundle, LadderExportError, build_ladder_bundle

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap
    from pyrung.core.program import Program


def pyrung_to_ladder(program: Program, tag_map: TagMap) -> LadderBundle:
    """Render a Program into Click ladder CSV row matrices.

    Args:
        program: The Program to export.
        tag_map: TagMap mapping logical tags to Click hardware addresses.

    Returns:
        A LadderBundle containing main and subroutine row matrices.
    """
    return build_ladder_bundle(tag_map, program)


__all__ = ["LadderBundle", "LadderExportError", "build_ladder_bundle", "pyrung_to_ladder"]
