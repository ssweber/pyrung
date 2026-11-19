"""The instruction families that laddercodec checks.

Each module here declares one family as ``FAMILY``, a
:class:`~pyrung.click.validation.fields.Family`. Validation imports every module here, so
a family branch adds its module and edits no list. Validation finds an instruction's family
with :func:`family_of`.
"""

from __future__ import annotations

from functools import cache
from importlib import import_module
from pkgutil import iter_modules
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..fields import Family

FAMILIES: list[Family] = []

for _module in sorted(info.name for info in iter_modules(__path__)):
    FAMILIES.append(import_module(f"{__name__}.{_module}").FAMILY)


@cache
def _by_class() -> dict[str, Family]:
    found: dict[str, Family] = {}
    for family in FAMILIES:
        for name in family.classes:
            other = found.setdefault(name, family)
            if other is not family:
                raise RuntimeError(f"{name} is in the {other.name} and {family.name} families.")
    return found


def family_of(instruction: Any) -> Family | None:
    """Return the family of *instruction*, or None if laddercodec checks no family of it."""
    return _by_class().get(type(instruction).__name__)


@cache
def replaced_codes() -> dict[str, str]:
    """Return each CLK_ code that a family's move removed, with the codes that replace it."""
    return {code: new for family in FAMILIES for code, new in family.replaced_codes.items()}


__all__ = ["FAMILIES", "family_of", "replaced_codes"]
