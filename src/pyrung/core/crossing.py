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

Soundness (``prove/AGENTS.md``): a reverse may **over**-approximate the allowed
input domain (a superset is safe) but never **under**-approximate.  A crossing
that cannot invert returns :data:`REVERSE_FALLTHROUGH` — "add no constraint,
defer to the caller" — which is the sound direction.  A crossing that *can*
invert but only to a superset says so with ``exact=False`` (the caller verifies);
it must never narrow to a singleton it cannot guarantee (the clamp-rail trap).

This module runtime-imports nothing from ``analysis/``; it depends only on the
neutral core storage primitive and the standard library, so it can sit below
every consumer.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pyrung.core.storage import IDENTITY_STORE, StoreTransform, store_value

#: Sentinel for "no forward value is known".  The forward protocol is locked but
#: reverse-first — the interpreted fork is the forward oracle.
UNKNOWN: Any = object()


@dataclass(frozen=True)
class Literal:
    """Writer unconditionally produces this fixed value."""

    value: Any


@dataclass(frozen=True)
class Affine:
    """Writer produces and stores ``source * scale + offset`` this scan.

    Self-referential when *source* equals the destination tag
    (the increment/decrement pattern). ``storage`` describes the concrete
    destination conversion after the raw affine expression.
    """

    source: str
    scale: int | float = 1
    offset: int | float = 0
    storage: StoreTransform = IDENTITY_STORE


@dataclass(frozen=True)
class Aggregate:
    """Writer produces and stores an aggregate (sum/count) over a block range."""

    tags: tuple[str, ...]
    operation: str = "sum"
    storage: StoreTransform = IDENTITY_STORE


def _apply_store(value: Any, storage: StoreTransform) -> Any:
    """Apply a forward claim's concrete destination-store transform.

    Returns :data:`UNKNOWN` when the descriptor or conversion is not
    representable.  The numeric behavior mirrors the interpreter's copy/calc
    stores, including non-finite-to-zero handling and integer coercion before
    clamp/wrap.
    """

    try:
        return store_value(value, storage)
    except (OverflowError, TypeError, ValueError):
        return UNKNOWN


def evaluate_forward(
    claim: Literal | Affine | Aggregate,
    values: Mapping[str, Any],
) -> Any:
    """Evaluate a forward claim against concrete source values.

    This is the single place consumers should combine a relationship with its
    destination storage.  A missing source, unsupported aggregate, or failed
    conversion returns :data:`UNKNOWN`.
    """

    if isinstance(claim, Literal):
        return claim.value
    if isinstance(claim, Affine):
        if claim.source not in values:
            return UNKNOWN
        try:
            # A named copy is represented by the affine identity even for
            # non-numeric types. Preserve the raw source before applying CHAR /
            # BOOL destination storage instead of attempting string arithmetic.
            raw = (
                values[claim.source]
                if claim.scale == 1 and claim.offset == 0
                else values[claim.source] * claim.scale + claim.offset
            )
        except (OverflowError, TypeError, ValueError):
            return UNKNOWN
        return _apply_store(raw, claim.storage)
    if isinstance(claim, Aggregate):
        if claim.operation != "sum" or any(tag not in values for tag in claim.tags):
            return UNKNOWN
        try:
            raw = sum(values[tag] for tag in claim.tags)
        except (OverflowError, TypeError, ValueError):
            return UNKNOWN
        return _apply_store(raw, claim.storage)
    return UNKNOWN


def invert_affine_candidate(claim: Affine, output: Any) -> Any:
    """Return one source candidate that may make *claim* produce *output*.

    This is a proposal helper, not a complete reverse preimage: clamp rails and
    modular stores can have many producers.  Callers must verify the candidate
    in the interpreted fork. :data:`UNKNOWN` means no useful representative
    could be derived.
    """

    scale = claim.scale
    offset = claim.offset
    if not isinstance(scale, (int, float)) or isinstance(scale, bool) or scale == 0:
        return UNKNOWN

    storage = claim.storage
    if storage.kind == "wrap":
        if (
            not isinstance(scale, int)
            or isinstance(scale, bool)
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or not isinstance(output, int)
            or isinstance(output, bool)
            or storage.lower is None
            or storage.upper is None
        ):
            return UNKNOWN
        modulus = storage.upper - storage.lower + 1
        divisor = math.gcd(abs(scale), modulus)
        residue = output - offset
        if modulus <= 0 or residue % divisor != 0:
            return UNKNOWN
        reduced_modulus = modulus // divisor
        if reduced_modulus == 1:
            candidate = 0
        else:
            inverse = pow(scale // divisor, -1, reduced_modulus)
            candidate = (residue // divisor * inverse) % reduced_modulus
        candidate = _apply_store(candidate, storage)
        if candidate is UNKNOWN:
            return UNKNOWN
        produced = evaluate_forward(claim, {claim.source: candidate})
        return candidate if produced is not UNKNOWN and produced == output else UNKNOWN

    if storage.kind in {"bool", "char"}:
        return UNKNOWN
    try:
        candidate = (output - offset) / scale
    except (OverflowError, TypeError, ValueError, ZeroDivisionError):
        return UNKNOWN
    if isinstance(output, int) and isinstance(scale, int) and isinstance(offset, int):
        if (output - offset) % scale == 0:
            candidate = (output - offset) // scale
    produced = evaluate_forward(claim, {claim.source: candidate})
    return candidate if produced is not UNKNOWN and produced == output else UNKNOWN


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
class AffineCmp(Constraint):
    """``tag <op> scale * bound_tag + offset``.

    This is distinct from :class:`Cmp` so consumers that only understand plain
    scalar bounds cannot silently erase the transform. Counter completion uses
    it for dynamic presets at the same-scan frontier.
    """

    tag: str
    op: str
    bound_tag: str
    scale: int | float = 1
    offset: int | float = 0


def complement_scalar_constraint(constraint: Constraint) -> Cmp | AffineCmp | None:
    """Return the exact logical complement of a supported scalar relation.

    Only relations whose complement remains one scalar constraint are
    supported. Consumers must fail closed for every other algebra member.
    """

    complements = {
        "==": "!=",
        "!=": "==",
        "<": ">=",
        "<=": ">",
        ">": "<=",
        ">=": "<",
    }
    if isinstance(constraint, Cmp):
        op = complements.get(constraint.op)
        return (
            None
            if op is None
            else Cmp(
                constraint.tag,
                op,
                constraint.bound,
                bound_is_tag=constraint.bound_is_tag,
            )
        )
    if isinstance(constraint, AffineCmp):
        op = complements.get(constraint.op)
        return (
            None
            if op is None
            else AffineCmp(
                constraint.tag,
                op,
                constraint.bound_tag,
                scale=constraint.scale,
                offset=constraint.offset,
            )
        )
    return None


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


@dataclass(frozen=True)
class CrossingProposal:
    """Verify-required predecessor candidates, expressed as constraint DNF.

    A proposal is deliberately separate from :class:`ReverseResult`: its
    branches may omit concrete preimages and therefore make no sound reverse
    claim. ``reason`` records the heuristic used, and ``verify_required`` makes
    the consumer obligation explicit.
    """

    branches: tuple[tuple[Constraint, ...], ...] = ()
    reason: str = ""
    verify_required: bool = True

    @property
    def empty(self) -> bool:
        return not self.branches


NO_CROSSING_PROPOSAL = CrossingProposal(verify_required=False)


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
    """Concrete values and tag declarations available to a crossing."""

    snapshot: Mapping[str, Any] = field(default_factory=dict)
    tags_by_name: Mapping[str, Any] = field(default_factory=dict)
