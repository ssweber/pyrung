"""pyrung's own bookkeeping tags, which the debugger leaves out.

pyrung adds tags of its own to a running program:

- ``__on{n}__{dest}``, ``__off{n}__{dest}``, ``__set{n}__{dest}``: the bit
  of one rule of a synthesis bracket (``pyrung.core.synthesis``).
- ``__cpl_ond__``, ``__cpl_on__``, ``__cpl_offd__``, ``__cpl_off__``,
  ``__cpl_armed__`` ``{feedback}``: the debounce timers of a harness coupling,
  and a ramp's arm bit (``pyrung.core.harness``).
- ``__pls_ond__``, ``__pls_on__``, ``__pls_offd__``, ``__pls_off__``
  ``{feedback}``: the flasher timers of a harness pulse (``pyrung.core.harness``).
- ``__tmd_ond__``, ``__tmd_on__`` ``{feedback}``: the timer of a harness timed coil
  (``pyrung.core.harness``).
- ``on`` and ``off`` above with a unit (``__cpl_on_Ts__{feedback}``): the same
  timer, for a delay too long to count in milliseconds.

They are not the program's, so the DAP does not list them. Only these exact
prefixes are matched: a user tag such as ``__init__`` or ``__spare__1`` is shown.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import TypeVar

from pyrung.core.synthesis import RULE_BIT_PREFIX

_V = TypeVar("_V")

_INTERNAL_TAG_PREFIX = re.compile(
    rf"(?:{RULE_BIT_PREFIX}"
    r"|__(?:cpl_(?:ond|offd|armed|(?:on|off)(?:_T[smhd])?)"
    r"|pls_(?:ond|offd|(?:on|off)(?:_T[smhd])?)"
    r"|tmd_(?:ond|on(?:_T[smhd])?))__)(?=.)",
    re.DOTALL,
)


def is_internal_tag_name(name: str) -> bool:
    """True for a tag pyrung adds itself (a bracket's rule bit, a harness timer)."""
    return _INTERNAL_TAG_PREFIX.match(name) is not None


def visible_tag_names(names: Iterable[str]) -> list[str]:
    """The names in ``names`` that are the program's, in their order."""
    return [name for name in names if not is_internal_tag_name(name)]


def visible_tags(tags: Mapping[str, _V]) -> dict[str, _V]:
    """``tags`` without pyrung's internal ones."""
    return {name: value for name, value in tags.items() if not is_internal_tag_name(name)}
