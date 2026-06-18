"""Crossings — the reverse contract (low module).

A *crossing* answers one question in one language: given a constraint on a tag a
writer produces, what constraint follows on its inputs?  This module holds only
the data carried across that boundary — the :class:`Constraint` algebra a target
and a result are expressed in, the DNF :class:`ReverseResult` a crossing returns,
and the immutable :class:`CrossingContext` a consumer fills with what it knows.
The per-instruction reverse *logic* lives one layer up, in
``core/analysis/crossings/`` (the registry), keyed by instruction class — it
cannot live here (instructions sit below analysis; an evidence-bearing handler
would be an import cycle).

The same contract serves both reverse mechanisms; they differ only in how a
:class:`Prior` constraint is *resolved*, not in how a crossing is *expressed*:

- **Recorded** — a :class:`Prior` is read out of an observed prior scan
  (``causal/`` resolves it against history).
- **Projected** — a :class:`Prior` is chased by recursing the planner on the
  prior-scan value (walk / prover seeding).

Soundness (``prove/CLAUDE.md``): a reverse may **over**-approximate the allowed
input domain (a superset is safe) but never **under**-approximate.  A crossing
that cannot invert returns :data:`REVERSE_FALLTHROUGH` — "add no constraint,
defer to the caller" — which is the sound direction.  A crossing that *can*
invert but only to a superset says so with ``exact=False`` (the caller verifies);
it must never narrow to a singleton it cannot guarantee (the clamp-rail trap).

This module runtime-imports nothing from ``analysis/``; it depends only on the
standard library so it can sit below every consumer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: Sentinel for "no forward value is known".  The forward protocol is locked but
#: reverse-first — the interpreted fork is the forward oracle.
UNKNOWN: Any = object()

#: Comparison operators a :class:`Cmp` may carry.
CMP_OPS = frozenset({"==", "!=", "<", "<=", ">", ">="})


# --- the constraint algebra ---------------------------------------------------
#
# Every crossing speaks in these.  A target handed to ``reverse`` is one of
# them; a result is a DNF of them.  Each is a frozen, hashable value — no
# behaviour, just the shape of a constraint — so consumers dispatch on type.


@dataclass(frozen=True)
class Constraint:
    """Base for the constraint algebra.  Never instantiated directly."""


@dataclass(frozen=True)
class Eq(Constraint):
    """``tag`` holds one of ``values``.

    The empty set is the **unsatisfiable** encoding: ``Eq(dest, frozenset())``
    means "no value works" (a structural blocker) — every crossing agrees on it.
    """

    tag: str
    values: frozenset[Any]


@dataclass(frozen=True)
class Cmp(Constraint):
    """``tag <op> bound`` — an inequality/equality against a literal or a tag.

    ``bound_is_tag`` distinguishes ``acc >= 100`` (literal preset) from
    ``acc >= Preset`` (a preset tag whose own value must be chased).  This is the
    shape a counter/timer done-bit, a numeric search, and a clamp rail invert to.
    """

    tag: str
    op: str
    bound: Any
    bound_is_tag: bool = False


@dataclass(frozen=True)
class Mask(Constraint):
    """``tag & mask == bits`` — a partial constraint on a wide register.

    The exact-but-not-enumerable shape a single-bit/single-word unpack inverts
    to (the other bits stay free), where an :class:`Eq` set would be 2**31 wide.
    """

    tag: str
    mask: int
    bits: int


@dataclass(frozen=True)
class Prior(Constraint):
    """``tag@N == scale * source@(N-1) + offset`` — a prior-scan reference.

    The bridge between the recorded and projected mechanisms: a shift register
    cell (``scale=1, offset=0``), an affine counter predecessor
    (``offset=±1``), a held value.  The *consumer* resolves it — recorded reads
    ``source`` from the previous scan; projected recurses the planner on the
    inverted prior value ``(value - offset) / scale``.
    """

    tag: str
    source: str
    scale: int = 1
    offset: int = 0


@dataclass(frozen=True)
class CondAttr(Constraint):
    """``tag``'s value this scan is decided by the writer's rung *condition*.

    ``expected`` is the truth value the rung condition must have for the target
    to hold (a coil ``== True`` needs ``expected=True``; ``== False`` needs
    ``expected=False``).  The consumer attributes through the rung SP-tree
    (``attribute()``) — which is why ``reverse`` receives the ``rung``.
    """

    expected: bool


@dataclass(frozen=True)
class External(Constraint):
    """``tag`` is written from outside the program (a Modbus receive, an input).

    Not a gap — a *stop*: the chase ends here because the value is an input, not
    a derived quantity.  Distinct from :data:`REVERSE_FALLTHROUGH` (could not
    invert) — this asserts there is nothing upstream to chase.
    """

    tag: str


@dataclass(frozen=True)
class Quant(Constraint):
    """A quantified constraint over a block: ``∃``/``∀`` element ``<op> value``.

    ``kind`` is ``"exists"`` or ``"forall"``.  The shape a block search inverts
    to — the consumer either enumerates the block or defers to the fork.  Kept
    explicit so the search frontier is a named cell, not a silent fallthrough.
    """

    kind: str
    block: tuple[str, ...]
    op: str
    value: Any
    value_is_tag: bool = False


# --- the result ---------------------------------------------------------------


@dataclass(frozen=True)
class ReverseResult:
    """The input constraints implied by a target on a writer, in DNF.

    - ``branches`` — disjunction of conjunctions: the outer tuple is OR, each
      inner tuple is AND.  A deterministic crossing returns one branch; a
      stateful writer (count vs reset, shift vs hold) returns one branch per
      mutually-exclusive path.  An empty inner tuple is the trivially-true
      branch ("the target holds with no input constraint").
    - ``exact`` — every branch is necessary *and* sufficient.  ``False`` is a
      sound superset the caller must still verify; it must remain a superset
      (over-approximation), never a guessed singleton.
    - ``fallthrough`` — the crossing could not invert; the caller keeps its
      existing behaviour.  A fallthrough carries no branches.
    """

    branches: tuple[tuple[Constraint, ...], ...] = ()
    exact: bool = False
    fallthrough: bool = False


#: The "could not invert" result.  Behaviourally inert — add no constraint.
REVERSE_FALLTHROUGH = ReverseResult(fallthrough=True)


# --- constructors (the common shapes, so handlers read declaratively) ---------


def single(*constraints: Constraint, exact: bool = False) -> ReverseResult:
    """One conjunctive branch of *constraints*."""
    return ReverseResult(branches=(tuple(constraints),), exact=exact)


def disjoint(*branches: tuple[Constraint, ...], exact: bool = False) -> ReverseResult:
    """A DNF result — one inner tuple per mutually-exclusive path."""
    return ReverseResult(branches=tuple(branches), exact=exact)


def satisfied() -> ReverseResult:
    """The target holds unconditionally — one empty (trivially-true) branch."""
    return ReverseResult(branches=((),), exact=True)


def unsatisfiable(dest: str) -> ReverseResult:
    """The pinned structural-blocker encoding: no value of *dest* works."""
    return single(Eq(dest, frozenset()), exact=True)


def eq_target(tag: str, value: Any) -> Eq:
    """The everyday target a consumer builds: ``tag == value``."""
    return Eq(tag, frozenset({value}))


# --- the context bundle -------------------------------------------------------


@dataclass(frozen=True)
class CrossingContext:
    """What a consumer knows when it asks a crossing to reverse.

    Each consumer fills only the fields it has.  ``value_at_scan`` carries
    *recorded* evidence (a callable ``(tag, scan_id) -> value``); **projected /
    prover-path contexts must leave it ``None``** so recorded evidence cannot
    leak into seeding (asserted by tests).
    """

    snapshot: Mapping[str, Any] = field(default_factory=dict)
    tags_by_name: Mapping[str, Any] = field(default_factory=dict)
    nondeterministic_dims: frozenset[str] = frozenset()
    nd_domains: Mapping[str, tuple[Any, ...]] | None = None
    value_at_scan: Callable[[str, int], Any] | None = None
    scan_id: int | None = None
    bounds_index: Any | None = None  # reserved; no producer yet, unread
