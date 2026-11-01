"""pyrung's own bookkeeping tags, which the debugger leaves out.

pyrung adds tags of its own to a running program:

- ``__frz{n}__{dest}``: the freeze bit of a synthesis bracket
  (``pyrung.core.synthesis``).
- ``__cpl_ond__``, ``__cpl_on__``, ``__cpl_off__``, ``__cpl_armed__``
  ``{feedback}``: the delay timers of a harness coupling
  (``pyrung.core.harness``).
- ``__pls_ond__``, ``__pls_on__``, ``__pls_offd__``, ``__pls_off__``
  ``{feedback}``: the timers of a harness pulse (``pyrung.core.harness``).

They are not the program's, so the DAP does not list them. Only these exact
prefixes are matched: a user tag such as ``__init__`` or ``__spare__1`` is shown.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import TypeVar

_V = TypeVar("_V")

_INTERNAL_TAG_PREFIX = re.compile(
    r"__(?:frz\d+|cpl_(?:ond|on|off|armed)|pls_(?:ond|on|offd|off))__(?=.)",
    re.DOTALL,
)


def is_internal_tag_name(name: str) -> bool:
    """True for a tag pyrung adds itself (a bracket freeze bit, a harness timer)."""
    return _INTERNAL_TAG_PREFIX.match(name) is not None


def visible_tag_names(names: Iterable[str]) -> list[str]:
    """The names in ``names`` that are the program's, in their order."""
    return [name for name in names if not is_internal_tag_name(name)]


def visible_tags(tags: Mapping[str, _V]) -> dict[str, _V]:
    """``tags`` without pyrung's internal ones."""
    return {name: value for name, value in tags.items() if not is_internal_tag_name(name)}
