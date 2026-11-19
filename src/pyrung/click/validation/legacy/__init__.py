"""pyrung's own checks of each instruction family that laddercodec does not check yet.

Each module here is one family. It holds:

- ``CODES``, the codes that only its checks report;
- ``CHECKS``, its checks, each a :class:`~pyrung.click.validation.checks.Check`;
- ``TABLES``, its rows of each table that a shared check or a test reads, keyed by table
  name.

Validation imports every module here and merges their checks, codes and tables. When
laddercodec takes over a family, its branch deletes the family's module here and adds one in
``families/``. It edits no list, so two family branches merge without a conflict.
"""

from __future__ import annotations

from importlib import import_module
from pkgutil import iter_modules
from typing import Any

from ..checks import Check

CHECKS: list[Check] = []
CODES: list[str] = []
TABLES: dict[str, dict[Any, Any]] = {}

for _module in sorted(info.name for info in iter_modules(__path__)):
    _family = import_module(f"{__name__}.{_module}")
    CHECKS.extend(_family.CHECKS)
    CODES.extend(_family.CODES)
    for _name, _rows in _family.TABLES.items():
        _merged = TABLES.setdefault(_name, {})
        for _key, _row in _rows.items():
            if _key in _merged:
                raise RuntimeError(f"{_module} repeats the row {_key!r} of {_name}")
            _merged[_key] = _row


def table(name: str) -> dict[Any, Any]:
    """Return the rows of the shared table *name*, or no rows once every family has left."""
    return TABLES.get(name, {})
