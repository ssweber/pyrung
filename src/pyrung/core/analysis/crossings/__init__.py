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

from pyrung.core.crossing import (
    NO_CROSSING_PROPOSAL,
    REVERSE_FALLTHROUGH,
    UNKNOWN,
    Affine,
    CrossingProposal,
    Literal,
    ReverseResult,
)

if TYPE_CHECKING:
    from pyrung.core.crossing import Constraint, CrossingContext


class BaseCrossing:
    """A per-instruction reverse handler.

    Subclasses override :meth:`reverse`.  The default reverse is a fallthrough
    and the default forward is ``UNKNOWN``, so a class registered before its
    semantics are filled in is always sound.

    ``reverse`` receives the writer's *rung* as well as the instruction so a
    condition-level crossing (a coil, a done bit) can reach the rung SP-tree;
    handlers that don't need it ignore it.  ``target`` is a
    :class:`~pyrung.core.crossing.Constraint` (usually ``Eq(tag, {value})``),
    not a bare value, so an inequality target composes through the registry.
    """

    def reverse(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> ReverseResult:
        return REVERSE_FALLTHROUGH

    def forward(self, instr: Any, target_tag: str, ctx: CrossingContext) -> Any:
        """Forward-classify the value written to one concrete destination."""
        return UNKNOWN

    def propose(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> CrossingProposal:
        """Return verify-required candidates without making a reverse claim."""
        return NO_CROSSING_PROPOSAL


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


def reverse(instr: Any, rung: Any, target: Constraint, ctx: CrossingContext) -> ReverseResult:
    """Reverse *instr* for *target*; FALLTHROUGH if unregistered.

    *target* is a :class:`~pyrung.core.crossing.Constraint`; the everyday case is
    ``Eq(tag, {value})`` (see ``eq_target``).
    """
    crossing = crossing_for(instr)
    if crossing is None:
        return REVERSE_FALLTHROUGH
    return crossing.reverse(instr, rung, target, ctx)


def forward(instr: Any, target_tag: str, ctx: CrossingContext) -> Any:
    """Forward-evaluate *instr* for one concrete destination tag.

    Destination identity is part of the crossing contract because sequential
    fan-out and heterogeneous co-write instructions can produce different
    values at different write sites. Unregistered instructions remain
    ``UNKNOWN``.
    """
    crossing = crossing_for(instr)
    if crossing is None:
        return UNKNOWN
    return crossing.forward(instr, target_tag, ctx)


def propose(
    instr: Any,
    rung: Any,
    target: Constraint,
    ctx: CrossingContext,
) -> CrossingProposal:
    """Propose predecessor candidates; empty when unregistered/unsupported."""
    crossing = crossing_for(instr)
    if crossing is None:
        return NO_CROSSING_PROPOSAL
    return crossing.propose(instr, rung, target, ctx)


def registered_classes() -> frozenset[type]:
    """The instruction classes with a registered crossing (the coverage test)."""
    return frozenset(_REGISTRY)


__all__ = [
    "Affine",
    "BaseCrossing",
    "CrossingProposal",
    "Literal",
    "crossing_for",
    "forward",
    "propose",
    "register",
    "registered_classes",
    "reverse",
]

# Submodule imports populate the registry at import time.  Keep them last so the
# API above is bound when each submodule does ``from . import register``.
from pyrung.core.analysis.crossings import accumulating as _accumulating  # noqa: E402, F401
from pyrung.core.analysis.crossings import boolean as _boolean  # noqa: E402, F401
from pyrung.core.analysis.crossings import calc as _calc  # noqa: E402, F401
from pyrung.core.analysis.crossings import copy as _copy  # noqa: E402, F401
from pyrung.core.analysis.crossings import drums as _drums  # noqa: E402, F401
from pyrung.core.analysis.crossings import external as _external  # noqa: E402, F401
from pyrung.core.analysis.crossings import pack as _pack  # noqa: E402, F401
from pyrung.core.analysis.crossings import search as _search  # noqa: E402, F401
from pyrung.core.analysis.crossings import shift as _shift  # noqa: E402, F401
