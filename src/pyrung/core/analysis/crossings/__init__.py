"""Projected Crossings registry (Phase 2).

Maps instruction classes to *crossings* — per-instruction projected reverse
handlers.  :func:`reverse` looks up the crossing for an instruction (exact
class, then an MRO walk so a subclass inherits a base crossing) and returns its
:class:`~pyrung.core.crossing.ReverseResult`, or
:data:`~pyrung.core.crossing.REVERSE_FALLTHROUGH` when nothing is registered.

This is the projected counterpart to the recorded read-diff
(``causal/crossings_recorded.py``).  It is the neutral analysis layer: it may
import instruction classes and ``sp_values`` one-way, but **must not import from
``walk/``** (walk is a consumer above it).

The registry is populated by importing the submodules — each registers its
class(es) at import time — so importing this package is enough.  Those imports
sit at the bottom of this module (after the API is defined) to break the
register-at-import cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.crossing import REVERSE_FALLTHROUGH, UNKNOWN, ReverseResult

if TYPE_CHECKING:
    from pyrung.core.crossing import CrossingContext


class BaseCrossing:
    """A per-instruction projected reverse handler.

    Subclasses override :meth:`reverse`.  The default reverse is a fallthrough
    and the default forward is ``UNKNOWN``, so a class registered before its
    semantics are filled in is always sound.
    """

    def reverse(
        self, instr: Any, target_tag: str, target_value: Any, ctx: CrossingContext
    ) -> ReverseResult:
        return REVERSE_FALLTHROUGH

    def forward(self, instr: Any, ctx: CrossingContext) -> Any:
        return UNKNOWN


_REGISTRY: dict[type, BaseCrossing] = {}


def register(cls: type, crossing: BaseCrossing) -> None:
    """Register *crossing* as the handler for instruction class *cls*."""
    _REGISTRY[cls] = crossing


def crossing_for(instr: Any) -> BaseCrossing | None:
    """The crossing for *instr* — exact class first, then an MRO walk."""
    cls = type(instr)
    crossing = _REGISTRY.get(cls)
    if crossing is not None:
        return crossing
    for base in cls.__mro__[1:]:
        crossing = _REGISTRY.get(base)
        if crossing is not None:
            return crossing
    return None


def reverse(instr: Any, target_tag: str, target_value: Any, ctx: CrossingContext) -> ReverseResult:
    """Reverse *instr* for ``target_tag == target_value``; FALLTHROUGH if unregistered."""
    crossing = crossing_for(instr)
    if crossing is None:
        return REVERSE_FALLTHROUGH
    return crossing.reverse(instr, target_tag, target_value, ctx)


def forward(instr: Any, ctx: CrossingContext) -> Any:
    """Forward-evaluate *instr* — locked protocol, reverse-first; returns ``UNKNOWN``."""
    crossing = crossing_for(instr)
    if crossing is None:
        return UNKNOWN
    return crossing.forward(instr, ctx)


def registered_classes() -> frozenset[type]:
    """The instruction classes with a registered crossing (the coverage test)."""
    return frozenset(_REGISTRY)


__all__ = [
    "BaseCrossing",
    "crossing_for",
    "forward",
    "register",
    "registered_classes",
    "reverse",
]

# Submodule imports populate the registry at import time.  Keep them last so the
# API above is bound when each submodule does ``from . import register``.
# (Added per commit as each crossing lands: copy, calc, boolean, pack.)
