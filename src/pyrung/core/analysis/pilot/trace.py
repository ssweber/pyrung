"""Backward trace engine for PILOT — the transparent static reader of the compass.

Walks writer conditions / copy / calc backward to steerable inputs
(``trace_back``).  The opaque-but-constant value-graph reader lives in
``compass.py``.  See ``pilot/CLAUDE.md``.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import TagRole, resolve_rung
from pyrung.core.analysis.prove.expr import _eval_expr_from_state
from pyrung.core.analysis.simplified import And, Atom, Const, Or, _negate, _sp_to_expr
from pyrung.core.analysis.sp_values import (
    _FLIP_FORM,
    _chase_inequality_source,
    _expr_tag_names,
    _extract_condition_values,
    _invert_affine,
    _required_from_atom,
    _SnapshotView,
    _values_match,
    _writer_projection,
    _written_value_for_tag,
    copy_source_binding,
    projected_writer_overlay,
)
from pyrung.core.crossing import Affine, Aggregate, Literal

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph, RungNode


@dataclass(frozen=True)
class DomainPrior:
    """Prover-derived domain prior for resolving inequality atoms.

    ``nd_domains`` maps a free/steerable input to its value domain
    (``_ExploreContext.nondeterministic_dims``); ``func_deps`` is the affine
    projection map ``{tag: (source, scale, offset)}`` for derived scratch
    (``_ExploreContext.functional_dep_projections``).  Both feed
    :func:`_resolve_inequality_target` so an inequality (``PV >= Lower``,
    ``ModeSel >= 1``) resolves to a *reachable* satisfying value instead of a
    blind arithmetic boundary.  ``None`` everywhere reproduces the pre-domain
    snapshot-boundary behavior — the prior is a completeness aid, never
    correctness-bearing (the interpreted fork verifies every plan).
    """

    nd_domains: dict[str, tuple[Any, ...]] | None = None
    func_deps: dict[str, tuple[str, int, Any]] | None = None


@dataclass(frozen=True)
class _TraceEnv:
    """Invariant context threaded through one backward trace.

    Everything here is constant for the whole trace — only ``tag``/``value`` (or
    ``expr``), ``provenance``, ``_visited``, ``_ancestry`` and ``_depth`` change
    between recursive calls.  Bundling the constants into one frozen value
    replaces the ten-kwarg wall that used to thread through every ``trace_back``
    / ``_trace_expression`` call.  ``avoid_pred`` biases OR-arm selection away
    from arms that force the avoided condition; ``via_pred`` is its dual — it
    biases selection *toward* an arm that forces the named condition (``None``
    each for an unconstrained trace).
    """

    snapshot: dict[str, Any]
    pdg: ProgramGraph
    program: Any
    steerable: frozenset[str]
    # Clear-only (ack-cleared momentary) command tags — off-path maintenance levers
    # that ``_rank_writers`` must not treat as a *preferred* init/reset writer gate.
    clear_only: frozenset[str] = frozenset()
    opaque_loop: frozenset[str] = frozenset()
    pipeline_internal_tags: frozenset[str] = frozenset()
    writer_locks: dict[tuple[str, Any], int] | None = None
    or_locks: dict[tuple[str, str], int] | None = None
    prior: DomainPrior | None = None
    avoid_pred: Any = None
    via_pred: Any = None
    max_depth: int = 15
    # Installed Harness, when tracing on a fork that has one.  Lets the coast
    # disposition attach a harness-linked sensor's *driver* (the input that makes
    # it ramp) as a steerable sibling of the coast leaf.  ``None`` off-fork.
    harness: Any = None
    # Per-trace memo for the writer-guard rejection arm (:func:`_writer_guard_verdict`).
    # Pure over the trace-invariant env (frozen snapshot + constant domains), so a
    # verdict is deterministic in ``(rung id, fire-pins, guard route key)`` and can
    # be cached for the whole recursion.  Fresh dict per :func:`_env_for` call.
    guard_memo: dict[Any, str] = field(default_factory=dict)


def _env_for(
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    *,
    clear_only: frozenset[str] = frozenset(),
    opaque_loop: frozenset[str] = frozenset(),
    pipeline_internal_tags: frozenset[str] = frozenset(),
    route: TraceChoice | None = None,
    writer_locks: dict[tuple[str, Any], int] | None = None,
    or_locks: dict[tuple[str, str], int] | None = None,
    prior: DomainPrior | None = None,
    avoid_pred: Any = None,
    via_pred: Any = None,
    max_depth: int = 15,
    harness: Any = None,
) -> _TraceEnv:
    """Build a trace env, resolving a ``TraceChoice`` route to its lock maps once."""
    if route is not None:
        writer_locks = route.writer_lock_map()
        or_locks = route.or_lock_map()
    return _TraceEnv(
        snapshot=snapshot,
        pdg=pdg,
        program=program,
        steerable=steerable,
        clear_only=clear_only,
        opaque_loop=opaque_loop,
        pipeline_internal_tags=pipeline_internal_tags,
        writer_locks=writer_locks,
        or_locks=or_locks,
        prior=prior,
        avoid_pred=avoid_pred,
        via_pred=via_pred,
        max_depth=max_depth,
        harness=harness,
    )


@dataclass(frozen=True)
class TraceChoice:
    """One enumerated route through a multi-writer / OR-over-coils Bool trace.

    Internal: ``enumerate_trace_choices`` produces these so ``_prepare_route``
    can pick a deterministic default and record the rest as redirectable pivots
    on :class:`~pyrung.core.analysis.graph.RouteTaken`.  ``via_hint`` is the
    concrete ``(tag, value)`` the engineer names to redirect onto this route
    (``via=``) or off it (``avoid=``) — the committed writer's gating coil, or a
    representative leaf of the chosen OR arm.
    """

    id: str
    label: str
    route: tuple[str, ...]
    writer_locks: tuple[tuple[str, Any, int], ...] = ()
    or_locks: tuple[tuple[str, str, int], ...] = ()
    via_hint: tuple[str, Any] | None = None

    def __str__(self) -> str:
        detail = " -> ".join(_compact_route(self.route))
        return f"route={self.id}: {self.label}" + (f" ({detail})" if detail else "")

    def writer_lock_map(self) -> dict[tuple[str, Any], int]:
        return {(tag, value): rung for tag, value, rung in self.writer_locks}

    def or_lock_map(self) -> dict[tuple[str, str], int]:
        return {(tag, key): index for tag, key, index in self.or_locks}


@dataclass(frozen=True)
class TraceAction:
    """A steerable action discovered by backward trace, with source context."""

    tag: str
    value: Any
    provenance: tuple[str, ...] = ()
    blast_radius: int | None = None
    # True when this action drives an edge-gated accumulator: a steady hold fires
    # the edge only once, so the action must *oscillate* (toggle each scan) to keep
    # the accumulator advancing.  candidates.py turns it into a ``ConditionalHold``.
    oscillate: bool = False
    # True when this action sits under an unsatisfied ``data_flow=="enable"`` node —
    # it *establishes* a table-enablement precondition (e.g. the mode that unblocks
    # a mask-disabled state) whose effect is a settled cross-register recompute.  It
    # cannot fire in the same scan as the command it gates, so candidates.py makes
    # it the sole bearing (stage 0) and defers the gated commands.
    establish: bool = False
    # Stage-3 heuristic boundary proposal on a steerable free word: the value is
    # an example that satisfies a relation, not a sound derivation.  ``note`` is
    # the relational report threaded to ``PlanStep.notes``.
    heuristic: bool = False
    note: str = ""

    @property
    def pair(self) -> tuple[str, Any]:
        return (self.tag, self.value)


@dataclass(frozen=True)
class _RouteDraft:
    """Accumulated OR-arm selections for one enumerated route.

    Root-only: a route records which arm of each OR in the output writer's
    condition it took.  The writer choice itself is tracked separately and
    applied at ``TraceChoice`` construction.
    """

    route: tuple[str, ...] = ()
    or_locks: tuple[tuple[str, str, int], ...] = ()
    # Concrete ``(tag, value)`` the engineer names to redirect onto this route;
    # the outermost OR arm's representative leaf (first set wins).
    via_hint: tuple[str, Any] | None = None

    def extend(
        self,
        *,
        route: str | None = None,
        or_lock: tuple[str, str, int] | None = None,
        via_hint: tuple[str, Any] | None = None,
    ) -> _RouteDraft:
        return _RouteDraft(
            route=self.route + ((route,) if route else ()),
            or_locks=self.or_locks + ((or_lock,) if or_lock else ()),
            via_hint=self.via_hint if self.via_hint is not None else via_hint,
        )


def _expr_route_key(expr: Any) -> str:
    return repr(expr)


# ---------------------------------------------------------------------------
# TraceNode — the backward-trace tree
# ---------------------------------------------------------------------------


@dataclass
class TraceNode:
    """One node in the backward-trace tree.

    Leaves are steerable inputs (actions to take), satisfied conditions
    (nothing to do), or depth/cycle terminations.
    """

    tag: str
    value: Any
    satisfied: bool = False
    is_steerable: bool = False
    writer_rung: int | None = None
    children: list[TraceNode] = field(default_factory=list)
    data_flow: str | None = None  # "copy" | "calc" | None
    provenance: tuple[str, ...] = ()
    pipeline_internal: bool = False
    self_advancing: bool = False  # threshold on a timer/counter Acc — coast, don't steer
    # Edge-gated accumulator driver: a steerable leaf that must *toggle* each scan
    # (not hold steady) to keep firing the rise/fall that advances the counter.
    oscillate: bool = False
    # Relational frontier: a live predicate (``A op B``) carried past the trace
    # boundary instead of collapsed to ``A == k``.  ``predicate`` is the source
    # ``Atom`` (evaluable via ``_eval_expr_from_state``).  The single-lever
    # resolution rides as the child subtree so steering is unchanged; distance
    # counts the predicate once and does not recurse into the lever (means, not
    # a separate goal).  See ``pilot/CLAUDE.md`` and the relational-goals plan.
    relational: bool = False
    predicate: Any = None
    lever: str | None = None  # "left"/"right" — which operand this subtree steers
    # Lever provenance (set alongside ``lever``): a stage-3 heuristic boundary
    # proposal and its relational report (see ``_Lever`` / ``_lever_note``).
    heuristic: bool = False
    note: str = ""
    # Set when the writer chosen for this (tag, value) frontier is gated by a
    # guard the table oracle could only *punt* on (a genuinely-live word or an
    # undecidable term) — not proved satisfiable, not proved dead.  Purely
    # informational today: it marks "this frontier is gated by an unreadable
    # guard" so the future sandbox skiff can see where to send an experiment.
    # No drive-loop behavior keys on it yet.
    live_guard: bool = False

    def leaves(self) -> list[TraceNode]:
        if not self.children:
            return [self]
        result: list[TraceNode] = []
        for child in self.children:
            result.extend(child.leaves())
        return result

    def steerable_leaves(self) -> list[tuple[str, Any]]:
        return [(n.tag, n.value) for n in self.leaves() if n.is_steerable]

    def same_tag_chains(self) -> list[list[TraceNode]]:
        """Find ancestor-descendant pairs with the same tag but different values
        where the prerequisite (descendant) is NOT already satisfied.

        These encode temporal ordering: reaching tag=v2 requires first
        reaching tag=v1 (the ancestor).  Chains where the prerequisite
        is already satisfied are not real blocking dependencies.
        """
        chains: list[list[TraceNode]] = []
        self._collect_chains([], chains)
        return chains

    def _collect_chains(self, ancestors: list[TraceNode], out: list[list[TraceNode]]) -> None:
        for anc in ancestors:
            if (
                anc.tag == self.tag
                and not _values_match(anc.value, self.value)
                and not self.satisfied
            ):
                out.append([anc, self])
        for child in self.children:
            child._collect_chains([*ancestors, self], out)

    def ordered_actions(self) -> list[tuple[str, Any]]:
        """Depth-first action list with same-tag prerequisites first.

        Returns deduplicated ``(tag, value)`` pairs, deepest first — the
        natural temporal ordering for state-machine programs.
        """
        return [action.pair for action in self.ordered_action_details()]

    def ordered_action_details(self) -> list[TraceAction]:
        """Depth-first action list with provenance for diagnostics/scoring."""
        actions: list[TraceAction] = []
        seen: set[tuple[str, Any]] = set()
        self._collect_ordered(actions, seen)
        return actions

    def _collect_ordered(
        self,
        out: list[TraceAction],
        seen: set[tuple[str, Any]],
        under_enable: bool = False,
    ) -> None:
        # A steerable leaf inherits ``establish`` from the nearest unsatisfied
        # ``enable`` ancestor: it stands in stage 0 (precondition), not stage 1
        # (the command it gates).  Once the gate is satisfied the node drops out
        # of the tree, so the flag is re-derived every trace.
        child_enable = under_enable or (self.data_flow == "enable" and not self.satisfied)
        for child in self.children:
            child._collect_ordered(out, seen, child_enable)
        if self.is_steerable:
            key = (self.tag, self.value)
            if key not in seen:
                seen.add(key)
                out.append(
                    TraceAction(
                        tag=self.tag,
                        value=self.value,
                        provenance=self.provenance,
                        oscillate=self.oscillate,
                        establish=under_enable,
                        heuristic=self.heuristic,
                        note=self.note,
                    )
                )

    def pivot_tags(self) -> set[str]:
        """Tags in the trace tree that are gate conditions — the pivots.

        These are the tags PILOT should monitor for progress/regression:
        non-leaf, non-steerable nodes that have children (meaning the
        trace walked through them as intermediate conditions).
        """
        tags: set[str] = set()
        self._collect_pivots(tags)
        return tags

    def _collect_pivots(self, out: set[str]) -> None:
        if (
            not self.satisfied
            and not self.is_steerable
            and not self.pipeline_internal
            and not self.relational
            and self.children
        ):
            out.add(self.tag)
        for child in self.children:
            child._collect_pivots(out)

    def unsatisfied_count(self) -> int:
        """Number of *distinct* unsatisfied, non-steerable conditions.

        This is the "distance to target" — fewer = closer; an action that
        increases it moved further from the goal.  Deduplicated by
        ``(tag, value)`` so a register that recurs across many branches (the
        same need reached by several paths) counts once.  Without this the
        count tracks tree *size*, which the cyclic state machine inflates
        (~2x on the burner), drowning the Layer 4 trend signal.
        """
        seen: set[tuple[str, Any]] = set()
        self._collect_unsatisfied(seen)
        return len(seen)

    def _collect_unsatisfied(self, seen: set[tuple[str, Any]]) -> None:
        if self.relational:
            # A relational frontier is one logical unmet goal — count it once;
            # its lever child(ren) are alternatives (means), not separate goals,
            # so do not recurse.  ``satisfied`` is set by reconciliation when a
            # sibling concrete demand already covers the predicate (a guard
            # whose value comes from elsewhere); such a frontier is not a goal.
            if not self.satisfied:
                seen.add(self._relational_key())
            return
        if (
            not self.satisfied
            and not self.is_steerable
            and not self.pipeline_internal
            and self.children
        ):
            seen.add(_visit_key(self.tag, self.value))
        for child in self.children:
            child._collect_unsatisfied(seen)

    def _relational_key(self) -> tuple[str, Any]:
        """Dedup key for a relational frontier: tag + (form, operand)."""
        p = self.predicate
        return (self.tag, (getattr(p, "form", None), getattr(p, "operand", self.value)))

    def dead_end_parent_tags(self) -> set[str]:
        """Tags of nodes whose children include a dead-end leaf.

        A dead-end leaf is not satisfied, not steerable, and has no children.
        The parent's tag broadens the upstream candidate cone so command
        buttons that write through an opaque pipeline can be discovered.
        """
        result: set[str] = set()
        self._collect_dead_end_parents(result)
        return result

    def _collect_dead_end_parents(self, out: set[str]) -> None:
        for child in self.children:
            if (
                not child.children
                and not child.satisfied
                and not child.is_steerable
                and not child.pipeline_internal
            ):
                out.add(self.tag)
            child._collect_dead_end_parents(out)


def _all_nodes(tree: TraceNode) -> list[TraceNode]:
    """Collect all nodes in a TraceNode tree (breadth-first)."""
    result: list[TraceNode] = [tree]
    i = 0
    while i < len(result):
        result.extend(result[i].children)
        i += 1
    return result


def frontier_pairs(tree: TraceNode, snap: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    """The tree's outstanding non-steerable frontier as ``(tag, value)`` pairs.

    The registers the target still *needs*: unsatisfied, non-steerable,
    non-pipeline-internal interior nodes whose snapshot value differs from the
    needed one (``Heat_CurStep = 3``-shaped progress registers, never steerable
    buttons).  The single definition shared by the iteration payload's
    ``still_need`` display and the checkpoint ``frontier`` capture that feeds
    ``hold_defeats_needed`` — the two must not drift.
    """
    pairs: list[tuple[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for n in _all_nodes(tree):
        if (
            not n.satisfied
            and not n.is_steerable
            and not getattr(n, "pipeline_internal", False)
            and n.children
        ):
            if n.relational and n.predicate is not None:
                # The need is the *relation* — emit the Atom, never the operand
                # tag-name posing as an equality value (a string a downstream
                # ``_values_match`` would garbage-compare against numbers).
                # Consumers treat pairs as opaque membership except
                # ``hold_defeats_needed``, which isinstance-skips Atoms.
                if _eval_expr_from_state(n.predicate, snap) is not True:
                    key = (n.tag, repr(n.predicate))
                    if key not in seen:
                        seen.add(key)
                        pairs.append((n.tag, n.predicate))
                continue
            cur = snap.get(n.tag)
            if cur != n.value:
                key = (n.tag, repr(n.value))
                if key not in seen:
                    seen.add(key)
                    pairs.append((n.tag, n.value))
    return tuple(pairs)


# ---------------------------------------------------------------------------
# trace_back — recursive backward trace
# ---------------------------------------------------------------------------

# Feedback-loop guard: how many *distinct* values of one opaque-loop register
# may be expanded along a single ancestor path before further occurrences are
# treated as a data-flow cycle.  Only tags in ``opaque_loop`` (jump-table
# state registers — see ``detect_opaque_loop``) are guarded; simple direct-copy
# state machines are never cut.  Budget 1 keeps the direct prerequisite chain
# (StateCurrent=6 <- StateRequested=6 <- command) but cuts the cross-state
# wandering (StateCurrent=6 -> ... -> StateCurrent=7 -> ...), emitting a leaf
# so Layer 6 owns the transition by observation.
_SAME_TAG_VALUE_BUDGET = 1


def _atom_target(atom: Atom) -> tuple[str, Any] | None:
    """Convert an Atom to the ``(tag, value)`` needed to satisfy it.

    ``rise``/``fall`` need the tag at the transition target value.
    ``truthy`` needs a truthy value — use ``True`` as proxy.
    """
    form = atom.form
    if form == "xic":
        return (atom.tag, True)
    if form == "xio":
        return (atom.tag, False)
    if form == "eq":
        return (atom.tag, atom.operand)
    if form == "rise":
        return (atom.tag, True)
    if form == "fall":
        return (atom.tag, False)
    if form == "truthy":
        return (atom.tag, True)
    if form in {"ne", "lt", "le", "gt", "ge"}:
        return None
    return None


#: strict-inequality nudge for a Real tag with no domain grid — small and
#: positive so the fork-verified fallback lands just past the threshold rather
#: than a whole integer beyond it (the ``+1`` unit assumption).
_REAL_STRICT_EPSILON = 1e-6


def _domain_granularity(domain: tuple[Any, ...]) -> Any:
    """The spacing of a finite numeric *domain* — its smallest positive step.

    ``(0, 2, 4, 6)`` → ``2``; ``(0, 5, 10)`` → ``5``.  ``None`` when the domain
    holds fewer than two numeric values (no step to infer)."""
    nums = sorted(v for v in domain if isinstance(v, (int, float)) and not isinstance(v, bool))
    diffs = [b - a for a, b in zip(nums, nums[1:], strict=False) if b > a]
    return min(diffs) if diffs else None


def _strict_inequality_step(tag: str, prior: DomainPrior | None, pdg: ProgramGraph | None) -> Any:
    """The amount to step past a strict-inequality threshold for *tag*.

    A Real tag steps by :data:`_REAL_STRICT_EPSILON` (a whole ``+1`` overshoots
    an analog boundary); a tag with a non-unit prover domain steps by the
    domain's granularity; everything else keeps the integer unit ``1``."""
    from pyrung.core.tag import TagType

    if pdg is not None:
        tag_ref = pdg.tags.get(tag)
        if tag_ref is not None and getattr(tag_ref, "type", None) is TagType.REAL:
            return _REAL_STRICT_EPSILON
    domain = prior.nd_domains.get(tag) if (prior is not None and prior.nd_domains) else None
    if domain:
        step = _domain_granularity(domain)
        if step is not None:
            return step
    return 1


def _resolve_inequality_target(
    atom: Atom,
    snapshot: dict[str, Any],
    prior: DomainPrior | None = None,
    pdg: ProgramGraph | None = None,
) -> tuple[str, Any] | None:
    """Resolve an inequality atom to a ``(tag, satisfying_value)`` target.

    Two-stage, mirroring the walk engine (``sp_values._chase_inequality_source``):

    1. *Domain-aware* — when the prover gives the compare tag (or its affine
       source) a pipeline domain, chase to that source and pick the nearest
       **in-domain** satisfying value.  Reachable by construction, and this is
       what re-enables literal-operand inequalities on steerable analog/word
       inputs (``ModeSel >= 1``) that the pre-domain code dropped.
    2. *Snapshot-boundary fallback* — no domain.  A tag-name operand
       (``PV >= Lower``) is a computed-threshold comparison: resolve the
       operand from *snapshot* and steer toward ``operand`` (``ge``/``le``) or
       one step past it (strict ``gt``/``lt`` — an epsilon for a Real tag, the
       domain granularity for a non-unit domain, else the integer unit; see
       ``_strict_inequality_step``, which reads the tag type off *pdg*).  A literal operand on a
       domain-less (logic-written) tag is a static guard whose satisfying
       value comes from a writer/binding elsewhere in the trace — return
       ``None`` (drop), the original punt's safe direction.
    """
    operand = atom.operand
    operand_is_tag = isinstance(operand, str)
    if operand_is_tag:
        resolved = snapshot.get(operand)
        if resolved is None:
            return None
        threshold = resolved
    else:
        threshold = operand

    if prior is not None and prior.nd_domains:
        hit = _chase_inequality_source(
            atom.tag, atom.form, threshold, prior.nd_domains, prior.func_deps
        )
        if hit is not None:
            return hit
        # Monotone fallback (joint two-input steering): the compare tag has its
        # own domain but the partner-frozen threshold is unsatisfiable within it
        # (``A > 8`` over ``A ∈ 0..5`` while ``B`` is still 0).  Steer to the domain
        # extreme in the form's direction — each operand ratchets toward its bound
        # and the partner re-points next scan, so a sum/difference that no single
        # move can satisfy converges across scans rather than dead-ending.
        domain = prior.nd_domains.get(atom.tag)
        if domain and atom.form in _FLIP_FORM:
            try:
                extreme = max(domain) if atom.form in ("gt", "ge") else min(domain)
            except (TypeError, ValueError):
                extreme = None
            if extreme is not None and not _values_match(snapshot.get(atom.tag), extreme):
                return (atom.tag, extreme)

    if atom.form == "ne":
        # A disequality has no single satisfying value; with a finite domain, steer
        # to any in-domain value other than the excluded one (preferring one we are
        # not already at).  No domain → drop (the safe punt), same as an
        # unresolvable literal-operand guard.
        domain = (
            prior.nd_domains.get(atom.tag) if (prior is not None and prior.nd_domains) else None
        )
        if domain:
            cur = snapshot.get(atom.tag)
            alts = [v for v in domain if not _values_match(v, threshold)]
            pick = next((v for v in alts if not _values_match(v, cur)), alts[0] if alts else None)
            if pick is not None:
                return (atom.tag, pick)
        return None

    if not operand_is_tag:
        return None
    if atom.form in ("ge", "le"):
        return (atom.tag, threshold)
    if (
        atom.form in ("gt", "lt")
        and isinstance(threshold, (int, float))
        and not isinstance(threshold, bool)
    ):
        step = _strict_inequality_step(atom.tag, prior, pdg)
        return (atom.tag, threshold + step if atom.form == "gt" else threshold - step)
    return None


def _declared_float_bounds(tag: str, pdg: ProgramGraph | None) -> tuple[Any, Any]:
    """Raw declared ``(min, max)`` for *tag* off the program graph, else ``(None, None)``.

    Reads the tag declaration's numeric bounds directly — deliberately NOT
    ``_declared_domain`` (which is the sound int-enumeration used for
    rejection/probing and stays untouched).  These bounds only *clamp* a
    heuristic proposal; they never reject anything.
    """
    if pdg is None:
        return (None, None)
    ref = pdg.tags.get(tag)
    if ref is None:
        return (None, None)
    return (getattr(ref, "min", None), getattr(ref, "max", None))


def _heuristic_inequality_target(
    atom: Atom,
    snapshot: dict[str, Any],
    steerable: frozenset[str],
    pdg: ProgramGraph | None,
) -> tuple[Any, str] | None:
    """Stage-3 heuristic value proposal for an ordered comparison on a
    **steerable** free numeric word — fired only after
    :func:`_resolve_inequality_target` returned ``None``.

    The value is not guessed: it solves the boundary exactly (``threshold`` for
    ``ge``/``le``; ``threshold ± _strict_inequality_step`` for strict forms)
    against the snapshot-frozen partner.  A declared ``min``/``max`` clamps the
    proposal; when the clamped value no longer satisfies the relation, propose
    the bound extreme in the form's direction only if that is an actual move
    (the ratchet — the partner lever re-points against the new snapshot next
    trace), else no lever.

    Returns ``(value, marker)`` where *marker* is the honesty sentence appended
    to the lever note, or ``None``.  The proposal is a trial like any other —
    replay-verified via Act→Verify, never used for rejection/DEAD/domain
    fabrication (see ``pilot/CLAUDE.md``).
    """
    if atom.form not in ("lt", "le", "gt", "ge"):
        return None
    if atom.tag not in steerable:
        return None
    operand = atom.operand
    threshold = snapshot.get(operand) if isinstance(operand, str) else operand
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        return None

    if atom.form in ("ge", "le"):
        value = threshold
    else:
        step = _strict_inequality_step(atom.tag, None, pdg)
        value = threshold + step if atom.form == "gt" else threshold - step

    lo, hi = _declared_float_bounds(atom.tag, pdg)
    clamped = value
    if lo is not None and clamped < lo:
        clamped = lo
    if hi is not None and clamped > hi:
        clamped = hi
    if clamped != value:
        satisfies = {
            "lt": clamped < threshold,
            "le": clamped <= threshold,
            "gt": clamped > threshold,
            "ge": clamped >= threshold,
        }[atom.form]
        if not satisfies and _values_match(snapshot.get(atom.tag), clamped):
            return None  # already at the extreme — no move left, no lever
        value = clamped

    return (value, "heuristic value — relation is the requirement, not this number")


#: form -> operator symbol, for rendering lever notes.
_FORM_SYMBOL = {"lt": "<", "le": "<=", "gt": ">", "ge": ">=", "eq": "==", "ne": "!="}


def _atom_text(atom: Atom) -> str:
    """Render an inequality atom for a lever note (``PV < Lower``)."""
    op = _FORM_SYMBOL.get(atom.form, atom.form)
    operand = atom.operand
    rhs = operand if isinstance(operand, str) else repr(operand)
    return f"{atom.tag} {op} {rhs}"


@dataclass(frozen=True)
class _Lever:
    """One actionable lever for an inequality atom, with provenance.

    ``heuristic`` marks a stage-3 boundary proposal (no sound derivation —
    the value is an example, the relation is the requirement); ``note`` is the
    human-readable relational report threaded to ``PlanStep.notes``.
    """

    label: str  # "left" | "right" — which operand this lever steers
    tag: str
    value: Any
    heuristic: bool = False
    note: str = ""


def _sole_write_instr(tag: str, pdg: ProgramGraph, program: Any) -> Any:
    """The sole instruction writing *tag*, or ``None`` (multi/zero writer or
    unresolved rung) — the structural reader's narrow entry."""
    writers = pdg.writers_of.get(tag, frozenset())
    if len(writers) != 1:
        return None
    ro = resolve_rung(program, pdg.rung_nodes[next(iter(writers))])
    if ro is None:
        return None
    for instr in ro._instructions:
        if getattr(getattr(instr, "dest", None), "name", None) == tag:
            return instr
    return None


#: simplified comparison form <-> Crossings ``Cmp`` operator symbol.
_FORM_TO_OP = {"gt": ">", "ge": ">=", "lt": "<", "le": "<="}
_OP_TO_FORM = {op: form for form, op in _FORM_TO_OP.items()}


def _rewrite_internal_compare(
    atom: Atom,
    steerable: frozenset[str],
    pdg: ProgramGraph,
    program: Any,
    snapshot: dict[str, Any],
    *,
    _depth: int = 0,
) -> list[Atom]:
    """Rewrite an inequality on an internal copy/calc register onto input-level
    atoms by reversing through its writer via the Crossings registry.

    The prover *dissolves* transparent pass-throughs (``copy(Temp, TempCopy)``,
    ``calc(Sensor + 10, Adjusted)``, ``calc(A + B, Sum)``) — back-propagating the
    boundary into the source domains and dropping the intermediate — so an
    inequality guard on the intermediate (``TempCopy > 50``, ``Sum > 8``) has no
    domain and would dead-end.  Reverse the guard through the writer instead
    (``crossings.reverse`` on a ``Cmp`` target):

    - single-source affine (copy / ``scale*src + offset``): one rewritten atom on
      ``src`` with the threshold shifted (form flipped on a negative scale).
      Recurses, so copy/calc chains collapse to the steerable source.
    - two-tag ``A ± B`` against a threshold: the calc crossing freezes each
      partner at its *snapshot* value and returns one branch per operand
      (``A op bound-B_now`` ∨ ``B op bound-A_now``); each becomes an alternative
      atom whose lever re-points against the live partner next scan.  Subsumes the
      old subtraction-at-zero special case (it is the ``bound == 0`` instance).

    Returns ``[atom]`` unchanged when the tag is steerable or the registry falls
    through — honest: the caller dead-ends, it never fabricates a lever.  The
    per-instruction inversion lives in ``core/analysis/crossings/``; this is the
    consumer that drives it and recurses for multi-hop chains.
    """
    if _depth > 6 or atom.tag in steerable:
        return [atom]
    op = _FORM_TO_OP.get(atom.form)
    if op is None:
        return [atom]  # not an inequality form
    instr = _sole_write_instr(atom.tag, pdg, program)
    if instr is None:
        return [atom]

    from pyrung.core.analysis import crossings
    from pyrung.core.crossing import Cmp, CrossingContext

    target = Cmp(atom.tag, op, atom.operand, bound_is_tag=isinstance(atom.operand, str))
    result = crossings.reverse(instr, None, target, CrossingContext(snapshot=snapshot))
    if result.fallthrough or not result.branches:
        return [atom]

    rewritten: list[Atom] = []
    for branch in result.branches:
        cmps = [c for c in branch if isinstance(c, Cmp)]
        if not cmps:
            return [atom]  # no inequality atom (Eq / unsat) -> stay honest
        if len(cmps) > 1 and len(cmps) != len(branch):
            # A conjunctive branch mixed with a non-Cmp atom (an Eq we cannot
            # represent as a lever) -> stay honest rather than drop the conjunct.
            return [atom]
        # A single Cmp is the affine/two-tag case unchanged; a branch of two or
        # more Cmps is a conjunction (both source atoms must hold), so surface
        # every conjunct as its own rewritten lever.
        for c in cmps:
            form = _OP_TO_FORM.get(c.op)
            if form is None:
                return [atom]
            rewritten.extend(
                _rewrite_internal_compare(
                    Atom(tag=c.tag, form=form, operand=c.bound),
                    steerable,
                    pdg,
                    program,
                    snapshot,
                    _depth=_depth + 1,
                )
            )
    return rewritten or [atom]


def _lever_note(req: Atom, orig: Atom, tag: str, value: Any, marker: str = "") -> str:
    """The relational report for one lever — the requirement, with the proposed
    value as an *example* (``held Band < -100.0 to satisfy PV < Lower (e.g.,
    Band = -100.000001; heuristic …)``)."""
    req_txt = _atom_text(req)
    orig_txt = _atom_text(orig)
    body = req_txt if req._key() == orig._key() else f"{req_txt} to satisfy {orig_txt}"
    note = f"held {body} (e.g., {tag} = {value!r}"
    if marker:
        note += f"; {marker}"
    return note + ")"


def _inequality_levers(
    atom: Atom,
    snapshot: dict[str, Any],
    steerable: frozenset[str],
    pdg: ProgramGraph,
    prior: DomainPrior | None,
    program: Any = None,
) -> list[_Lever]:
    """Actionable levers for ``A op B``, as :class:`_Lever` values.

    The **left** lever steers the LHS (``A`` toward the boundary set by the
    current ``B``); the **right** lever — only when the operand is itself a tag —
    steers the RHS (``B`` toward the boundary set by the current ``A``), via the
    flipped comparison (``A > B`` ⟺ ``B < A``).  Both are reactive *candidates*:
    the loop tries one, verifies, and the existing ranker/nogood learning
    switches to the other if it was a no-op — nothing is pre-committed.

    A lever is kept only when its tag is **actionable** — steerable, or
    logic-written so trace can chase it.  A free, non-actionable operand (a
    harness-linked sensor) yields no lever; that is the converging/coast
    disposition's job (handled by the self-advancing branch upstream).

    When *program* is given, an inequality on an internal copy/calc register is
    first rewritten onto its input-level source(s) (``_rewrite_internal_compare``)
    so the levers land on steerable inputs the prover dissolved.  A tag-bound
    compare (``PV < Lower``) cannot cross the registry as-is (the calc crossing
    defers on a tag bound), so each side is *also* rewritten with the partner
    frozen at its snapshot value — the same freeze the two-tag calc branch
    applies one level down — landing literal-operand atoms on the sources
    (``Band < -100.0``, ``Level > 100.0``) that the resolver, or failing it the
    stage-3 heuristic (:func:`_heuristic_inequality_target`, steerable free
    words only), can turn into levers.
    """
    levers: list[_Lever] = []
    seen: set[str] = set()

    def _actionable(tag: str) -> bool:
        return tag in steerable or bool(pdg.writers_of.get(tag))

    def _add(label: str, req: Atom) -> None:
        heuristic = False
        marker = ""
        target = _resolve_inequality_target(req, snapshot, prior, pdg)
        if target is None:
            hit = _heuristic_inequality_target(req, snapshot, steerable, pdg)
            if hit is None:
                return
            value, marker = hit
            target = (req.tag, value)
            heuristic = True
        tag, value = target
        if tag in seen or not _actionable(tag):
            return
        seen.add(tag)
        note = _lever_note(req, atom, tag, value, marker)
        levers.append(_Lever(label, tag, value, heuristic=heuristic, note=note))

    def _rewrite(a: Atom) -> list[Atom]:
        if program is None:
            return [a]
        return _rewrite_internal_compare(a, steerable, pdg, program, snapshot)

    base = _rewrite(atom)
    operand = atom.operand
    if isinstance(operand, str) and program is not None:
        # Snapshot-frozen variants of both sides (see docstring).  Dedup by
        # atom key so an unchanged rewrite adds nothing new.
        seen_atoms = {a._key() for a in base}
        frozen: list[Atom] = []
        threshold = snapshot.get(operand)
        if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
            frozen.extend(_rewrite(Atom(tag=atom.tag, form=atom.form, operand=threshold)))
        lhs_now = snapshot.get(atom.tag)
        if (
            atom.form in _FLIP_FORM
            and isinstance(lhs_now, (int, float))
            and not isinstance(lhs_now, bool)
        ):
            frozen.extend(_rewrite(Atom(tag=operand, form=_FLIP_FORM[atom.form], operand=lhs_now)))
        for a in frozen:
            if a._key() not in seen_atoms:
                seen_atoms.add(a._key())
                base.append(a)

    for a in base:
        _add("left", a)
        if isinstance(a.operand, str) and a.form in _FLIP_FORM:
            _add("right", Atom(tag=a.operand, form=_FLIP_FORM[a.form], operand=a.tag))

    return levers


@functools.lru_cache(maxsize=16)
def _progress_kinds(program: Any) -> dict[str, str]:
    """Self-advancing accumulators (timer/counter Acc) → kind, cached per program.

    Reused from the prover so the trace can surface a threshold on such an
    accumulator (``Acc > 2``) as a coast leaf instead of dropping it.
    """
    from pyrung.core.analysis.prove.absorb import _collect_progress_source_kinds

    return _collect_progress_source_kinds(program)


def _expr_satisfied(expr: Any, snapshot: dict[str, Any]) -> bool:
    """Whether *expr* is definitely satisfied in *snapshot*.

    Delegates to the prover's ``_eval_expr_from_state`` which returns
    ``None`` for undecidable terms (rise/fall, missing tags).  Treat
    ``None`` as not-satisfied — conservative for backward tracing.
    """
    return _eval_expr_from_state(expr, snapshot) is True


def target_reached(
    snapshot: dict[str, Any],
    target_tag: str,
    target_value: Any,
    target_predicate: Any = None,
) -> bool:
    """Whether the (possibly relational) target holds in *snapshot*.

    A relational target (``A op B``) is judged by evaluating its live predicate
    — the goal is the relation, not a frozen value.  An equality / Bool target
    falls back to ``_values_match`` on ``(target_tag, target_value)``.
    """
    if target_predicate is not None:
        return _eval_expr_from_state(target_predicate, snapshot) is True
    return _values_match(snapshot.get(target_tag), target_value)


def _coupling_driver_leaf(
    env: _TraceEnv, tag: str, provenance: tuple[str, ...]
) -> TraceNode | None:
    """The steerable driver hold that advances an analog harness sensor.

    When *tag* is an analog coupling's feedback register, return the input PILOT
    must hold for it to ramp (e.g. ``Enable=True``) as a steerable ``TraceNode``.
    Surfaced as a *sibling* of the coast leaf (never a child — a child would hide
    the ``self_advancing`` leaf from ``leaves()`` and suppress the let-run
    escalation), so let-run holds the driver while coasting the sensor.  ``None``
    when *tag* is not a coupling sensor, or its driver is not steerable.
    """
    if env.harness is None:
        return None
    from pyrung.core.analysis.pilot.accumulators import resolve_profile
    from pyrung.core.condition import BitCondition, CompareEq

    match = resolve_profile(tag, env.program, env.harness)
    if match is None or match.via_done:
        return None
    advance = match.profile.advance
    if isinstance(advance, BitCondition):
        driver_tag, driver_val = advance._resolved_tag.name, True
    elif isinstance(advance, CompareEq):
        driver_tag, driver_val = advance.tag.name, advance.value
    else:
        return None
    if driver_tag not in env.steerable:
        return None
    return TraceNode(tag=driver_tag, value=driver_val, is_steerable=True, provenance=provenance)


def _resolve_preset_value(preset: Any, snapshot: dict[str, Any]) -> int | None:
    """Resolve a counter preset (``Tag`` or literal) to an int over a snapshot."""
    name = getattr(preset, "name", None)
    if name is not None:
        preset = snapshot.get(name, getattr(preset, "default", None))
    try:
        return int(preset)
    except (TypeError, ValueError):
        return None


def _declared_finite_domain(tag: str, pdg: ProgramGraph) -> tuple[Any, ...] | None:
    """Complete finite value domain of *tag* from its declaration alone.

    A Bool → ``(True, False)`` (True first, so a Bool advance resolves exactly as
    the historical ``(True, False)`` scan did); a ``choices=`` tag → its declared
    keys.  Anything else — an unbounded Int/Real, or a tag absent from
    ``pdg.tags`` — has no provably-complete finite domain, so this returns
    ``None`` and the caller punts rather than enumerate a plausible-value set
    (the same completeness discipline as ``table_oracle._is_complete_domain``).
    """
    from pyrung.core.tag import TagType

    tag_ref = pdg.tags.get(tag)
    if tag_ref is None:
        return None
    if getattr(tag_ref, "type", None) is TagType.BOOL:
        return (True, False)
    choices = getattr(tag_ref, "choices", None)
    if choices:
        return tuple(sorted(choices))
    return None


def _counter_driver_leaf(
    env: _TraceEnv, profile: Any, provenance: tuple[str, ...]
) -> TraceNode | None:
    """The steerable input that advances a counter, as a sibling driver leaf.

    A counter advances once per scan its ``advance`` condition holds.  A plain
    level (``BitCondition``) driver is held steady; an edge (``rise``/``fall``)
    driver fires only once when held, so the leaf is flagged ``oscillate`` and
    candidates.py turns it into a toggling ``ConditionalHold``.  ``None`` when the
    advance has no single steerable read.
    """
    from pyrung.core.analysis.pdg import _extract_reads_from_condition
    from pyrung.core.condition import FallingEdgeCondition, RisingEdgeCondition

    advance = profile.advance
    if advance is None:
        return None
    reads = _extract_reads_from_condition(advance, {})
    if len(reads) != 1:
        return None
    read_tag = next(iter(reads))
    if read_tag not in env.steerable:
        return None
    if isinstance(advance, (RisingEdgeCondition, FallingEdgeCondition)):
        return TraceNode(
            tag=read_tag, value=True, is_steerable=True, oscillate=True, provenance=provenance
        )
    # Level advance: hold the read at a value that makes advance == advance_value.
    # A Bool read enumerates the historical ``(True, False)`` pair; an int/word read
    # (``Sel == 3``) enumerates its COMPLETE declared finite domain (Bool or
    # ``choices=``).  A read with no complete finite domain — an unbounded live
    # word — has no sound candidate set, so punt rather than guess.
    domain = _declared_finite_domain(read_tag, env.pdg)
    if domain is None:
        return None
    for candidate in domain:
        try:
            evaluated = advance.evaluate(_SnapshotView(env.snapshot, {read_tag: candidate}))
        except Exception:  # noqa: BLE001 — any eval failure → no resolvable driver
            return None
        if evaluated == profile.advance_value:
            return TraceNode(
                tag=read_tag, value=candidate, is_steerable=True, provenance=provenance
            )
    return None


def _counter_done_frontier(
    env: _TraceEnv, done_tag: str, provenance: tuple[str, ...]
) -> TraceNode | None:
    """Recognize a counter ``Done`` bit as a self-advancing accumulator frontier.

    Reaching ``Done == True`` means driving the accumulator to ``preset`` — not
    firing the writer rung once (the naive backward walk surfaces the rung
    condition as a single steerable leaf and loses the count entirely).  Mirrors
    the ``Acc > N`` branch (a ``self_advancing`` coast leaf) plus the analog
    ``_coupling_driver_leaf`` sibling: the coast leaf rides let-run; the driver
    leaf is held (level) or oscillated (edge).  Scoped to counters — timer ``Done``
    bits already work via their enable-condition coast, so this leaves them
    untouched.  ``None`` when *done_tag* is not a counter ``Done`` bit, or its
    preset / driver can't be resolved.
    """
    from pyrung.core.analysis.pilot.accumulators import resolve_profile
    from pyrung.core.instruction.accumulating import KIND_COUNT_DOWN, KIND_COUNT_UP

    match = resolve_profile(done_tag, env.program)
    if match is None or not match.via_done:
        return None
    profile = match.profile
    if profile.kind not in (KIND_COUNT_UP, KIND_COUNT_DOWN):
        return None
    preset = _resolve_preset_value(profile.preset, env.snapshot)
    if preset is None:
        return None
    driver = _counter_driver_leaf(env, profile, provenance)
    if driver is None:
        return None
    coast = TraceNode(
        tag=profile.accumulator.name,
        value=profile.done_target(preset),
        self_advancing=True,
        provenance=provenance,
    )
    return TraceNode(tag=done_tag, value=True, provenance=provenance, children=[coast, driver])


def trace_relational(
    predicate: Atom,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    *,
    clear_only: frozenset[str] = frozenset(),
    opaque_loop: frozenset[str] = frozenset(),
    pipeline_internal_tags: frozenset[str] = frozenset(),
    route: TraceChoice | None = None,
    prior: DomainPrior | None = None,
    avoid_pred: Any = None,
    via_pred: Any = None,
    max_depth: int = 15,
    harness: Any = None,
) -> TraceNode:
    """Backward trace for a relational *target* predicate (``A op B``).

    Routes the target through the same atom branch as a relational prerequisite,
    so a target inequality gets the live-predicate node, the up-to-two reactive
    levers, and the converging/coast disposition for free.  Returns the
    relational node (or a coast leaf / dead-end) as the tree root; a satisfied
    predicate yields a ``satisfied`` leaf (the drive loop's early-exit owns it).
    """
    env = _env_for(
        snapshot,
        pdg,
        program,
        steerable,
        clear_only=clear_only,
        opaque_loop=opaque_loop,
        pipeline_internal_tags=pipeline_internal_tags,
        route=route,
        prior=prior,
        avoid_pred=avoid_pred,
        via_pred=via_pred,
        max_depth=max_depth,
        harness=harness,
    )
    nodes = _trace_expression(env, predicate, predicate.tag, _visited=set(), _depth=0)
    if nodes:
        root = nodes[0]
        _reconcile_relational(root, snapshot)
        return root
    return TraceNode(
        tag=predicate.tag,
        value=getattr(predicate, "operand", None),
        satisfied=_expr_satisfied(predicate, snapshot),
    )


def _reconcile_relational(root: TraceNode, snapshot: dict[str, Any]) -> None:
    """Prune relational levers subsumed by a sibling concrete demand.

    A guard inequality (``ModeSel >= 1``) whose tag is *also* driven to a
    concrete value elsewhere in the tree (``ModeSel == 2``, from a copy-source)
    is already satisfied by that value — steering it to its own boundary (1)
    would conflict with the value the other goal needs.  Mark such a frontier
    ``satisfied`` and drop its lever so only the concrete demand survives as a
    candidate.  This is the conjunction/subsumption reconciliation: pure
    cleanup, re-run every iteration, never manufacturing a steer.

    Concrete demands are non-lever steerable leaves; lever leaves (a relational
    node's own boundary picks) are excluded so two levers never subsume each
    other.  The predicate is evaluated against the snapshot overlaid with the
    candidate value, so tag-operand thresholds (``PV >= Lower``) resolve too.
    """
    nodes = _all_nodes(root)
    concrete: dict[str, set[Any]] = {}
    for n in nodes:
        if n.is_steerable and n.lever is None:
            concrete.setdefault(n.tag, set()).add(n.value)
    for n in nodes:
        if not n.relational or n.predicate is None or n.satisfied:
            continue
        vals = concrete.get(n.tag)
        if not vals:
            continue
        if any(_eval_expr_from_state(n.predicate, {**snapshot, n.tag: v}) is True for v in vals):
            n.satisfied = True
            n.children = []


def _scope_ref(rung_index: int, rung_node: Any) -> str:
    scope = rung_node.subroutine or rung_node.scope or "Main"
    if str(scope).lower() == "main":
        scope = "Main"
    return f"{scope}:R{rung_index}"


def _is_dead_end_leaf(leaf: TraceNode) -> bool:
    """A terminal the backward walk could not resolve to any action.

    Not satisfied (does not hold now), not steerable (PILOT can't set it), not
    self-advancing (no timer/counter to coast), not pipeline-internal, not a
    relational frontier — and childless (no further writer to chase).  The
    canonical case is ``InitDone == False`` once init has latched it True: no
    writer produces ``False``, so a route that needs it is dead.
    """
    return (
        not leaf.children
        and not leaf.satisfied
        and not leaf.is_steerable
        and not leaf.self_advancing
        and not leaf.pipeline_internal
        and not leaf.relational
    )


def _route_pilotable(nodes: list[TraceNode]) -> bool:
    """Whether a route bottoms out at things PILOT can act on — a binary property.

    A route is an AND of prerequisites; one dead-end leaf (see
    :func:`_is_dead_end_leaf`) makes the whole route undriveable.  This is the
    filter to apply *before* :func:`_trace_score`, which only ranks: a dead route
    has no steerable leaves and therefore the *cheapest* (zero) blast radius, so
    scoring alone would always prefer it over a live one.
    """
    return not any(_is_dead_end_leaf(leaf) for node in nodes for leaf in node.leaves())


def _trace_score(nodes: list[TraceNode], pdg: ProgramGraph) -> tuple[int, int, int]:
    """Rank alternative trace routes: low blast radius, few pivots, few leaves."""
    steerable = [leaf for node in nodes for leaf in node.leaves() if leaf.is_steerable]
    blast = sum(len(pdg.downstream_slice(leaf.tag, follow_calls=True)) for leaf in steerable)
    pivots = sum(node.unsatisfied_count() for node in nodes)
    return blast, pivots, len(steerable)


def _route_forces(nodes: list[TraceNode], snapshot: dict[str, Any], pred: Any) -> bool:
    """Whether the concrete demands across *nodes* satisfy *pred*.

    Overlays each node's ``(tag, value)`` (skipping relational representatives
    and valueless nodes) onto the snapshot and evaluates the predicate.  Shared
    by the OR-arm avoid skip (an arm whose assignment forces the avoided
    condition is dropped from selection) and route avoid-pruning (a choice whose
    route forces it is pruned before the ambiguity check) — ``Or(Manual, Auto)``
    with ``avoid=Manual`` picks the ``Auto``/``Start`` route instead of only
    vetoing ``Manual`` at verify time.  Caller guards ``pred is not None``.
    """
    overlay = dict(snapshot)
    for root in nodes:
        for n in _all_nodes(root):
            if n.relational or n.value is None:
                continue
            overlay[n.tag] = n.value
    try:
        return bool(pred(overlay))
    except Exception:
        return False


def _value_sets_intersect(a: Any, b: Any) -> bool:
    """Whether any value in *a* loosely matches any value in *b* (``_values_match``).

    Small operands (governing-value sets, singleton pins), so the pairwise sweep
    is cheap and preserves ``1 == True`` semantics that a raw set intersection
    would only get by luck of Python hashing.
    """
    return any(_values_match(x, y) for x in a for y in b)


def _equality_gated_coil(
    tag: str, value: Any, pdg: ProgramGraph, program: Any
) -> tuple[str, frozenset[Any]] | None:
    """The governing-register value SET a Bool mode-flag stands for, else ``None``.

    ``out(S_ManualMode)`` under ``rung(S_UnitModeCurrent == 3)`` means
    ``S_ManualMode=True`` is *equivalent to* ``S_UnitModeCurrent=3`` — return
    ``("S_UnitModeCurrent", {3})``.  Generalized past the single-equality case by
    inverting each writer's guard into the *set* of governing values it implies,
    via the And-narrows/Or-widens value lattice (:func:`_governing_constraint`):

    - a flag gated ``Or(Reg==3, Reg==5)`` aliases to ``("Reg", {3, 5})``;
    - a flag with several plain ``out`` writers that all gate the *same*
      governing register aliases to the union of their value sets (the flag is
      ``True`` only if some writer fired, and each writer pins the register).

    Fires only for a Bool driven ``True`` by plain ``out`` coils (``ote_writes``)
    whose guards each constrain exactly one governing register (never ``tag``
    itself) to a finite value set.  A writer that constrains a *different*
    register, more than one register, or nothing invertible (an inequality- or
    live-word-only gate — :func:`_governing_constraint` returns ``None``) makes
    the whole flag un-aliasable: return ``None`` and never fabricate a governing
    constraint.  Lets :func:`_route_conflict_tags` catch a caller-gate mode that
    contradicts the mode the body requires, even across differently named tags.
    """
    from pyrung.core.analysis.pilot.evidence import _governing_constraint

    if value is not True:
        return None
    writers = pdg.writers_of.get(tag, frozenset())
    if not writers:
        return None

    governing: str | None = None
    value_union: set[Any] = set()
    for wi in writers:
        node = pdg.rung_nodes[wi]
        if tag not in node.ote_writes:
            return None
        ro = resolve_rung(program, node)
        if ro is None:
            return None
        sp = ro.sp_tree()
        if sp is None:
            return None
        expr = _sp_to_expr(sp)
        others = [t for t in _extract_condition_values(expr) if t != tag]
        if len(others) != 1:
            return None  # not a clean single-register discriminator
        other = others[0]
        if governing is None:
            governing = other
        elif governing != other:
            return None  # writers disagree on the governing register
        constraint = _governing_constraint(expr, other, {})
        if not constraint:
            return None  # inequality / live-word gate — no finite value set
        value_union |= set(constraint)

    if governing is None or not value_union:
        return None
    return (governing, frozenset(value_union))


def _route_conflict_tags(tree: TraceNode, pdg: ProgramGraph, program: Any) -> frozenset[str]:
    """Tags *tree* pins to value sets with empty intersection that must hold together.

    Every node in a resolved trace tree is a required condition (Or-arms are
    already chosen), so two nodes pinning the same tag to disjoint value sets
    clash **unless** one is an ancestor of the other — that is temporal
    sequencing (``same_tag_chains``: reach ``v1`` first, then ``v2``), not a
    simultaneous contradiction.  A plain node pins its scalar value (a singleton
    set); a mode flag is normalized through :func:`_equality_gated_coil` into the
    governing-register value *set* it implies, so a manual-mode caller gate
    (``S_ManualMode=True`` → ``S_UnitModeCurrent ∈ {3}``) clashes with a body that
    needs ``S_UnitModeCurrent=1``, while a set-valued alias (``Reg ∈ {3, 5}``)
    only clashes when the needed value falls *outside* the set.

    This is a *relative* signal, not an absolute feasibility verdict: sibling
    flags can also encode an SFC that legitimately sequences one register
    (``S_StateCurrent`` 3→6 appears here as ``S_Starting`` beside ``S_Execute``).
    The ranker discounts any conflict tag shared by **every** route as inherent
    to the goal and penalizes only the conflicts unique to a route — the ones
    that are genuinely that route's own contradiction.
    """
    entries: list[tuple[str, Any, int, frozenset[int]]] = []

    def walk(node: TraceNode, anc: frozenset[int]) -> None:
        if not (node.relational or node.value is None):
            alias = _equality_gated_coil(node.tag, node.value, pdg, program)
            if alias is not None:
                demand_tag, demand_vals = alias
            else:
                demand_tag, demand_vals = node.tag, (node.value,)
            entries.append((demand_tag, demand_vals, id(node), anc))
        child_anc = anc | {id(node)}
        for child in node.children:
            walk(child, child_anc)

    walk(tree, frozenset())

    by_tag: dict[str, list[tuple[Any, int, frozenset[int]]]] = {}
    for tag, vals, nid, anc in entries:
        by_tag.setdefault(tag, []).append((vals, nid, anc))

    conflicts: set[str] = set()
    for tag, pins in by_tag.items():
        if len(pins) < 2:
            continue  # single pin — no clash possible
        for i in range(len(pins)):
            vi, ni, ai = pins[i]
            for j in range(i + 1, len(pins)):
                vj, nj, aj = pins[j]
                if _value_sets_intersect(vi, vj):
                    continue  # a shared value satisfies both pins — compatible
                if nj in ai or ni in aj:
                    continue  # ancestor/descendant → temporal, not a clash
                conflicts.add(tag)
                break
            else:
                continue
            break
    return frozenset(conflicts)


def _trace_expression(
    env: _TraceEnv,
    expr: Any,
    self_tag: str,
    *,
    provenance: tuple[str, ...] = (),
    _visited: set[tuple[str, Any]],
    _ancestry: tuple[tuple[str, Any], ...] = (),
    _depth: int,
) -> list[TraceNode]:
    """Walk an expression tree, returning trace children.

    And: trace all terms (all must be satisfied).
    Or: if any branch is already satisfied, skip. Otherwise pick the
        best unsatisfied branch (fewest non-steerable unsatisfied nodes),
        skipping any arm whose assignment forces ``env.avoid_pred``.
    Atom: convert to (tag, value) and recurse via _trace_back.
    """
    if isinstance(expr, And):
        children: list[TraceNode] = []
        for term in expr.terms:
            children.extend(
                _trace_expression(
                    env,
                    term,
                    self_tag,
                    provenance=provenance,
                    _visited=_visited,
                    _ancestry=_ancestry,
                    _depth=_depth,
                )
            )
        return children

    if isinstance(expr, Or):
        # Any satisfied branch means the Or doesn't block — skip it.
        if _expr_satisfied(expr, env.snapshot):
            return []

        lock_key = (self_tag, _expr_route_key(expr))
        locked_index = env.or_locks.get(lock_key) if env.or_locks is not None else None
        if locked_index is not None and 0 <= locked_index < len(expr.terms):
            term = expr.terms[locked_index]
            if not (isinstance(term, Atom) and term.tag == self_tag):
                return _trace_expression(
                    env,
                    term,
                    self_tag,
                    provenance=provenance,
                    _visited=set(_visited),
                    _ancestry=_ancestry,
                    _depth=_depth,
                )

        # Pick the cheapest unsatisfied branch. Skip self-referencing
        # branches — Or(rise(Input), SealIn) where SealIn is the tag
        # we're already tracing (the engineer knows the seal-in path
        # is circular and looks at the trigger instead).
        best: list[TraceNode] | None = None
        best_score: float = float("inf")
        # via= dual of avoid=: among the surviving arms, prefer one that *forces*
        # the requested condition.  Tracked separately so it only redirects when
        # some arm actually touches the predicate — an Or that does not mention it
        # falls back to the cheapest arm (no over-pruning).
        best_via: list[TraceNode] | None = None
        best_via_score: float = float("inf")
        for term in expr.terms:
            if isinstance(term, Atom) and term.tag == self_tag:
                continue
            candidate = _trace_expression(
                env,
                term,
                self_tag,
                provenance=provenance,
                _visited=set(_visited),
                _ancestry=_ancestry,
                _depth=_depth,
            )
            # Steering this arm would land in the avoided region — skip it so a
            # non-avoided arm wins the bearing (not just a verify-time veto).
            if (
                env.avoid_pred is not None
                and candidate
                and _route_forces(candidate, env.snapshot, env.avoid_pred)
            ):
                continue
            if not candidate:
                return []
            score = sum(
                1
                for c in candidate
                if (not c.satisfied and not c.is_steerable and not c.pipeline_internal)
            )
            if score < best_score:
                best_score = score
                best = candidate
            if (
                env.via_pred is not None
                and _route_forces(candidate, env.snapshot, env.via_pred)
                and score < best_via_score
            ):
                best_via_score = score
                best_via = candidate
        if best_via is not None:
            return best_via
        return best if best is not None else []

    if isinstance(expr, Atom):
        target = _atom_target(expr)
        if target is None:
            if expr.form in ("lt", "le", "gt", "ge", "ne"):
                # A threshold (Acc > N) on a self-advancing accumulator (timer
                # or counter) is a coast leaf: wait for it to cross on its own.
                if expr.tag in _progress_kinds(env.program):
                    return [
                        TraceNode(
                            tag=expr.tag,
                            value=expr.operand,
                            self_advancing=True,
                            satisfied=_expr_satisfied(expr, env.snapshot),
                            provenance=provenance,
                        )
                    ]
                if _expr_satisfied(expr, env.snapshot):
                    return []
                # Carry the predicate live as a relational frontier (Stage A)
                # and surface up-to-two reactive levers (Stage B): steer the LHS
                # toward B, or steer the RHS toward A.  Both ride as children so
                # both surface as candidates; the ranker + try-verify-learn loop
                # picks one and switches if it was a no-op.  Distance counts the
                # predicate once (the relational node stops recursion), so the
                # levers do not double-count as separate goals.
                levers = _inequality_levers(
                    expr, env.snapshot, env.steerable, env.pdg, env.prior, env.program
                )
                lever_children: list[TraceNode] = []
                for lever in levers:
                    child = _trace_back(
                        env,
                        lever.tag,
                        lever.value,
                        _visited=set(_visited),
                        _ancestry=_ancestry,
                        _depth=_depth + 1,
                    )
                    if child.is_steerable and not child.provenance:
                        child.provenance = provenance
                    child.lever = lever.label
                    child.heuristic = lever.heuristic
                    child.note = lever.note
                    lever_children.append(child)
                # Converging disposition (Stage B1): when the LHS advances on its
                # own and PILOT cannot steer it — a free input with no writers
                # (a harness-linked sensor that ramps under the held state) —
                # add a coast leaf so let-run can carry the frontier across the
                # boundary even when no lever is productive (e.g. Temp >= Limit
                # with Limit pinned: lowering the bar dead-ends, but Temp ramps
                # under the held Enable and crosses on its own).  Without this the
                # frontier looks like an opaque dead-end and the loop bails before
                # the coast.
                if expr.tag not in env.steerable and not env.pdg.writers_of.get(expr.tag):
                    lever_children.append(
                        TraceNode(
                            tag=expr.tag,
                            value=expr.operand,
                            self_advancing=True,
                            provenance=provenance,
                        )
                    )
                    # If this is a harness-linked sensor, attach its driver hold
                    # (e.g. Enable=True) as a *sibling* so let-run holds the
                    # input that makes it ramp while coasting the sensor.  The
                    # bare coast leaf above stays a leaf, so the let-run
                    # escalation is unchanged.
                    driver = _coupling_driver_leaf(env, expr.tag, provenance)
                    if driver is not None:
                        lever_children.append(driver)
                if lever_children:
                    return [
                        TraceNode(
                            tag=expr.tag,
                            value=expr.operand,
                            relational=True,
                            predicate=expr,
                            provenance=provenance,
                            children=lever_children,
                        )
                    ]
            return []
        tag, val = target
        # Rise/fall need a transition — if the tag is already at the
        # target value, the edge won't fire.  Mark it steerable so
        # PILOT knows to re-pulse it.
        if (
            expr.form in ("rise", "fall")
            and tag in env.steerable
            and _values_match(env.snapshot.get(tag), val)
        ):
            return [TraceNode(tag=tag, value=val, is_steerable=True, provenance=provenance)]
        if (
            expr.form in ("rise", "fall")
            and not env.pdg.writers_of.get(tag)
            and tag not in env.steerable
        ):
            return [
                TraceNode(
                    tag=tag,
                    value=val,
                    self_advancing=True,
                    satisfied=_expr_satisfied(expr, env.snapshot),
                    provenance=provenance,
                )
            ]
        child = _trace_back(
            env,
            tag,
            val,
            _visited=_visited,
            _ancestry=_ancestry,
            _depth=_depth + 1,
        )
        if child.is_steerable and not child.provenance:
            child.provenance = provenance
        return [child]

    return []


def _return_early_guard_exprs(program: Any, rung_node: RungNode) -> list[Any]:
    """Negated conditions of the ``return_early()`` rungs that gate a writer.

    Everything past a ``return_early()`` in a subroutine is effectively a split
    sub-function: if the guard fired, none of it executes at all (the writes
    don't run, the tags just retain their prior values).  So a writer downstream
    of a return guard carries that guard as an implicit prerequisite — for
    *either* polarity, since the coil only drives its value when it executes,
    which requires control to have reached it.  The PDG records the guard's tag
    *names* (``rung_node.guard_reads``, via ``_augment_return_early_guards``) but
    not the expression; recover it here — negated — so the trace resolves the
    polarity (``Enable == True``) instead of merely flagging the tag.
    """
    if not rung_node.guard_reads or rung_node.subroutine is None or rung_node.branch_path != ():
        return []
    sub_rungs = program.subroutines.get(rung_node.subroutine)
    if sub_rungs is None:
        return []

    from pyrung.core.instruction.control import ReturnInstruction

    guards: list[Any] = []
    for rung in sub_rungs[: rung_node.rung_index]:
        if any(isinstance(instr, ReturnInstruction) for instr in rung._instructions):
            sp = rung.sp_tree()
            if sp is not None:
                guards.append(_negate(_sp_to_expr(sp)))
    return guards


def trace_back(
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    *,
    clear_only: frozenset[str] = frozenset(),
    opaque_loop: frozenset[str] = frozenset(),
    pipeline_internal_tags: frozenset[str] = frozenset(),
    route: TraceChoice | None = None,
    writer_locks: dict[tuple[str, Any], int] | None = None,
    or_locks: dict[tuple[str, str], int] | None = None,
    prior: DomainPrior | None = None,
    avoid_pred: Any = None,
    via_pred: Any = None,
    max_depth: int = 15,
    harness: Any = None,
    _visited: set[tuple[str, Any]] | None = None,
    _ancestry: tuple[tuple[str, Any], ...] = (),
    _depth: int = 0,
) -> TraceNode:
    """Recursive backward trace from ``(tag, value)``.

    Public entry point: bundles the invariant trace context (graph, steerable
    set, locks, domain prior, avoid predicate, ...) into a :class:`_TraceEnv`
    and delegates to :func:`_trace_back`, which threads that one value down the
    recursion instead of a dozen kwargs.  A ``TraceChoice`` resolves to its lock
    maps here, once.
    """
    env = _env_for(
        snapshot,
        pdg,
        program,
        steerable,
        clear_only=clear_only,
        opaque_loop=opaque_loop,
        pipeline_internal_tags=pipeline_internal_tags,
        route=route,
        writer_locks=writer_locks,
        or_locks=or_locks,
        prior=prior,
        avoid_pred=avoid_pred,
        via_pred=via_pred,
        max_depth=max_depth,
        harness=harness,
    )
    return _trace_back(env, tag, value, _visited=_visited, _ancestry=_ancestry, _depth=_depth)


def _trace_back(
    env: _TraceEnv,
    tag: str,
    value: Any,
    *,
    _visited: set[tuple[str, Any]] | None = None,
    _ancestry: tuple[tuple[str, Any], ...] = (),
    _depth: int = 0,
) -> TraceNode:
    """Backward-trace worker over a fixed :class:`_TraceEnv`.

    Returns a ``TraceNode`` tree.  Leaves are steerable inputs (actions),
    already-satisfied conditions, or cycle/depth terminations.  Uses
    ``(tag, value)`` visited keys so the same tag at different values
    (e.g. ``StateCurrent==1`` then ``StateCurrent==2``) can be traced
    independently.  ``_ancestry`` is the per-path chain of expanded
    ``(tag, value)`` nodes; it powers the feedback-loop guard below.
    """
    if _visited is None:
        _visited = set()

    vkey = _visit_key(tag, value)

    if _values_match(env.snapshot.get(tag), value):
        return TraceNode(tag=tag, value=value, satisfied=True)

    if vkey in _visited:
        return TraceNode(tag=tag, value=value)

    # Feedback-loop guard: a jump-table state register (``opaque_loop``) that
    # already appears at another value along the ancestor path means we are
    # inverting the state-machine feedback cycle, not a finite prerequisite
    # chain.  Stop and emit a dead-end leaf so Layer 6 owns the transition.
    if tag in env.opaque_loop and tag not in env.steerable:
        # Key values via _visit_key so an unhashable expression value (a
        # relational sub-goal on a state register) is counted without crashing
        # the set membership; identical to raw values for the scalar case.
        prior_keys = {_visit_key(t, v) for (t, v) in _ancestry if t == tag}
        if _visit_key(tag, value) not in prior_keys and len(prior_keys) >= _SAME_TAG_VALUE_BUDGET:
            return TraceNode(tag=tag, value=value)

    _visited.add(vkey)

    if tag in env.steerable:
        return TraceNode(tag=tag, value=value, is_steerable=True)

    if tag in env.pipeline_internal_tags:
        return TraceNode(tag=tag, value=value, pipeline_internal=True)

    # Counter Done bit: reaching Done==True means driving the accumulator to
    # preset (a coast), not firing the writer rung once.  Surface the accumulator
    # frontier + its advance driver instead of the naive rung-condition walk.
    if _values_match(value, True):
        frontier = _counter_done_frontier(env, tag, ())
        if frontier is not None:
            return frontier

    _child_ancestry = (*_ancestry, (tag, value))

    if env.pdg.tag_roles.get(tag) == TagRole.INPUT:
        return TraceNode(tag=tag, value=value)

    writers = env.pdg.writers_of.get(tag, frozenset())
    if not writers:
        return TraceNode(tag=tag, value=value)

    node = TraceNode(tag=tag, value=value)

    ranked_writers = _rank_writers(
        writers,
        env.pdg,
        env.program,
        tag,
        value,
        env.snapshot,
        env.opaque_loop,
        env.clear_only,
        steerable=env.steerable,
        ancestry=_ancestry,
    )
    locked_writer = env.writer_locks.get(vkey) if env.writer_locks is not None else None
    if locked_writer is not None and locked_writer in ranked_writers:
        ranked_writers = [locked_writer]

    for ri in ranked_writers:
        rung_node = env.pdg.rung_nodes[ri]
        ro = resolve_rung(env.program, rung_node)
        if ro is None:
            continue

        wv = _written_value_for_tag(ro, tag)
        if not _can_produce(wv, value):
            continue

        csb = copy_source_binding(ro, tag, value)

        sp = ro.sp_tree()
        guard_expr = _sp_to_expr(sp) if sp is not None else None
        # OTE deactivation: tracing tag=False through out(tag) means the rung
        # must NOT fire — negate the expression.
        if guard_expr is not None and _values_match(value, False) and tag in rung_node.ote_writes:
            guard_expr = _negate(guard_expr)

        # Reduce the guard by the copy-source pin: reject the writer if the pin
        # violates a source-only guard atom (it can never emit this value), else
        # drop the atoms the pin already satisfies so a redundant ``src`` guard
        # (``UnitModeCmd != 0`` beside source ``== 2``) does not surface as a second
        # frontier fighting the source pin.
        if csb is not None and guard_expr is not None:
            guard_expr = _reduce_guard_by_pin(guard_expr, csb[0], csb[1], env.snapshot)
            if guard_expr is _GUARD_CONTRADICTION:
                continue

        if guard_expr is not None:
            guard_expr = _reduce_guard_by_fire_pins(
                guard_expr, ro, tag, value, env.snapshot, env.pdg, env.program
            )
            if guard_expr is _GUARD_CONTRADICTION:
                continue

        # Rejection arm (table_oracle.guard_verdict): a writer whose guard is
        # *provably unsatisfiable* over complete finite free-tag domains — under
        # the fire-time pins the writer itself forces to produce ``value`` — can
        # never fire to produce it.  Skip it exactly as a False ``_can_produce``
        # would, so a provably-dead writer never burns drive-loop trials.
        # Punt-biased and sound: ONLY a definite ``GUARD_DEAD`` rejects; ``SAT``
        # and ``PUNT`` keep today's behavior untouched.
        guard_punted = False
        if guard_expr is not None:
            from pyrung.core.analysis.pilot.table_oracle import GUARD_DEAD, GUARD_PUNT

            verdict = _writer_guard_verdict(env, ri, ro, tag, value, csb, guard_expr)
            if verdict == GUARD_DEAD:
                continue
            guard_punted = verdict == GUARD_PUNT

        node.writer_rung = ri

        if guard_expr is not None:
            node.children.extend(
                _trace_expression(
                    env,
                    guard_expr,
                    tag,
                    provenance=(_scope_ref(ri, rung_node),),
                    _visited=_visited,
                    _ancestry=_child_ancestry,
                    _depth=_depth,
                )
            )

        # Reaching this writer at all requires no upstream return_early() to have
        # fired — its negated guard is a prerequisite of the rung executing.
        for guard_expr in _return_early_guard_exprs(env.program, rung_node):
            node.children.extend(
                _trace_expression(
                    env,
                    guard_expr,
                    tag,
                    provenance=(_scope_ref(ri, rung_node),),
                    _visited=_visited,
                    _ancestry=_child_ancestry,
                    _depth=_depth,
                )
            )

        if rung_node.subroutine:
            caller_routes: list[tuple[tuple[int, int, int], list[TraceNode]]] = []
            for ci, cn in enumerate(env.pdg.rung_nodes):
                if rung_node.subroutine in cn.calls:
                    call_ro = resolve_rung(env.program, cn)
                    if call_ro is None:
                        continue
                    call_sp = call_ro.sp_tree()
                    if call_sp is None:
                        caller_routes.append(((0, 0, 0), []))
                        continue
                    children = _trace_expression(
                        env,
                        _sp_to_expr(call_sp),
                        tag,
                        provenance=(_scope_ref(ci, cn),),
                        _visited=set(_visited),
                        _ancestry=_child_ancestry,
                        _depth=_depth + 1,
                    )
                    caller_routes.append((_trace_score(children, env.pdg), children))
            if caller_routes:
                # Choose the caller by *pilotability*, not by score.
                # ``mode_change`` is called from both ``~InitDone`` (spent after
                # init) and ``ModeChgRequestBool==1`` (the live trigger).  Scoring
                # alone picks the dead ~InitDone route — its lone leaf
                # ``InitDone == False`` is a dead end (childless, unsatisfiable),
                # so it has no steerable leaves and scores cheapest (zero blast).
                # Filter to routes PILOT can actually drive first; score only to
                # break ties among those.
                pilotable = [r for r in caller_routes if _route_pilotable(r[1])]
                pool = pilotable or caller_routes
                _score, call_children = min(pool, key=lambda item: item[0])
                node.children.extend(call_children)

        if csb is not None:
            src_tag, src_val = csb
            child = _trace_back(
                env,
                src_tag,
                src_val,
                _visited=_visited,
                _ancestry=_child_ancestry,
                _depth=_depth + 1,
            )
            child.data_flow = "copy"
            node.children.append(child)

        # Enablement gate decided by a constant-table predicate (PackML
        # state-enable / cmd-valid mask): the flag on this transition is a
        # dh[...] & dh[...] over the target state and a steerable index (mode),
        # whose snapshot value is stale w.r.t. the planned transition — so trace
        # would wrongly read the gate as satisfied.  Keys on the semantic shape
        # (fire-time pins derivable from the writer's data flow or its guard),
        # not the identity-copy silhouette: identity/converting copies, affine
        # calc transitions, and guard-pinned decodes all reach the oracle, which
        # is asked which mode makes the gate hold under those pins.
        node.children.extend(
            _table_enablement_prereqs(
                env,
                ro,
                tag,
                value,
                csb,
                _visited=_visited,
                _ancestry=_child_ancestry,
                _depth=_depth,
            )
        )

        # An identity copy (``copy(src, dest)``) forward-classifies as an identity
        # Affine *and* binds via ``copy_source_binding`` — inverting both re-traces
        # the same source, and the second call dead-ends on the visited set
        # (childless), a pure duplicate that (when the source is steerable) shadows
        # the real leaf and poisons pilotability.  The copy binding already handled
        # the source; the Affine branch is only for genuine affine ``calc`` writers
        # (``calc(X + 1, dest)``), which never bind a copy source.
        if isinstance(wv, Affine) and csb is None:
            src_val = _invert_affine(wv, value)
            # Self-referential affine (``calc(CurStep+1, CurStep)``) is a
            # value-step: invert one hop (``CurStep==2`` <- ``CurStep==1``) and
            # let the ``(tag, value)`` visited set + a different writer for the
            # source value terminate the chain.  Skip only the degenerate
            # self-map (``src_val == value``), which would not advance.
            if src_val is not None and not (wv.source == tag and _values_match(src_val, value)):
                child = _trace_back(
                    env,
                    wv.source,
                    src_val,
                    _visited=_visited,
                    _ancestry=_child_ancestry,
                    _depth=_depth + 1,
                )
                child.data_flow = "calc"
                node.children.append(child)

        if isinstance(wv, Aggregate) and wv.operation == "sum" and not node.children:
            for child_node in _decompose_sum(
                env,
                wv,
                tag,
                value,
                _visited=_visited,
                _ancestry=_child_ancestry,
                _depth=_depth,
            ):
                node.children.append(child_node)

        # Indirect copy: block[pointer] → invert the lookup table.
        if not node.children:
            inv = _invert_indirect(ro, tag, value, env.snapshot, env.pdg, env.program)
            if inv is not None:
                idx_tag, idx_vals = inv
                for iv in idx_vals:
                    child = _trace_back(
                        env,
                        idx_tag,
                        iv,
                        _visited=_visited,
                        _ancestry=_child_ancestry,
                        _depth=_depth + 1,
                    )
                    child.data_flow = "lookup"
                    node.children.append(child)

        # Preserve: the writer above *establishes* the value; a retentive target
        # must also be kept from being clobbered by a competing writer.
        node.children.extend(
            _preserve_children(
                env,
                tag,
                value,
                ri,
                _visited=_visited,
                _ancestry=_child_ancestry,
                _depth=_depth,
            )
        )

        # Punt signal for the future sandbox skiff: the oracle could not decide
        # this writer's guard (a genuinely-live word / undecidable term) AND the
        # backward walk found no drivable path for this frontier.  That is exactly
        # the skiff's territory — "this frontier is gated by an unreadable guard".
        # Gating on non-pilotability keeps the flag off ordinary frontiers whose
        # guard merely lacks an ``nd_domains`` entry but still resolves to a
        # steerable input.  Purely informational: no drive-loop behavior keys on it.
        node.live_guard = guard_punted and not _route_pilotable([node])

        break  # use first viable writer

    if _depth == 0:
        # Reconcile relational guards against concrete demands once, on the full
        # tree (a relational guard satisfied by a sibling's needed value should
        # not steer to its own boundary and conflict).
        _reconcile_relational(node, env.snapshot)
    return node


def _preserve_children(
    env: _TraceEnv,
    tag: str,
    value: Any,
    establish_ri: int,
    *,
    _visited: set[tuple[str, Any]],
    _ancestry: tuple[tuple[str, Any], ...],
    _depth: int,
) -> list[TraceNode]:
    """Preserve prerequisites: keep a retentive ``tag=value`` from being clobbered.

    The writer walk *establishes* the value (finds the writer that produces it).
    For a **retentive** target — a latch/SET coil or a copy/calc into a held
    register — the value also has to *persist*: any competing writer that
    provably drives the tag to a different value overwrites it on a later scan
    unless its guard is suppressed.  An engineer reading ``latch(Running)``
    immediately asks "where's the reset, and is it active?"; this surfaces that
    half of the latch's boolean semantics, which the establish walk omits.

    For each clobbering writer, emit the **negation** of its guard as ordinary
    prerequisite children (``reset gated ~StopBtn`` -> ``StopBtn=True``).  They
    ride the same candidate / widening / hold pipeline as the establish leaves,
    and ``_trace_back`` marks the already-healthy ones ``satisfied`` so they drop
    out.  De Morgan turns a compound reset guard into an ``Or`` of suppression
    options, which ``_trace_expression`` resolves like any route choice.

    Honesty boundary: a competing writer whose written value *could* be the
    target (``_can_produce`` True — affine / aggregate / unknown) is **not**
    suppressed.  Trace never fabricates a hold it cannot statically read; that is
    sandbox territory.
    """
    establish_node = env.pdg.rung_nodes[establish_ri]
    # Only retentive targets need preserving — an OTE coil is recomputed every
    # scan from its own condition, so there is no held value to clobber.
    if tag in establish_node.ote_writes:
        return []

    children: list[TraceNode] = []
    seen_guards: set[str] = set()
    for ri in sorted(env.pdg.writers_of.get(tag, frozenset())):
        if ri == establish_ri:
            continue
        rung_node = env.pdg.rung_nodes[ri]
        ro = resolve_rung(env.program, rung_node)
        if ro is None:
            continue
        # A clobber is a writer that *provably* drives the tag away from value.
        if _can_produce(_written_value_for_tag(ro, tag), value):
            continue
        sp = ro.sp_tree()
        if sp is None:
            continue
        suppress = _negate(_sp_to_expr(sp))
        key = _expr_route_key(suppress)
        if key in seen_guards:
            continue
        seen_guards.add(key)
        children.extend(
            _trace_expression(
                env,
                suppress,
                tag,
                provenance=(_scope_ref(ri, rung_node),),
                _visited=_visited,
                _ancestry=_ancestry,
                _depth=_depth,
            )
        )
    return children


def _arm_fully_steerable(e: Any, self_tag: str, steerable: frozenset[str]) -> bool:
    """True when *e* is reachable by directly-steerable inputs alone.

    An OR arm qualifies when PILOT can assert it with inputs only, recursively:

    * ``And`` — *every* term must be steerable (``And(Manual, DiverterBtn)``).
    * ``Or`` — *any* term suffices, since asserting one satisfies it
      (``And(Manual, Or(BtnA, BtnB))``).
    * ``Atom`` — a steerable input the trace can drive: a bit/equality whose
      ``_atom_target`` tag is steerable, **or** an inequality (``Size > 100``)
      whose LHS tag is steerable (the trace's ``_inequality_levers`` drives it).

    Disqualified — and so kept as a surfaced choice — are a non-input /
    coil-backed tag (``ProdMode``), an inequality on a non-steerable computed
    tag, and the self-referencing seal-in atom (taking it commits the machine to
    an internal configuration, a real engineer decision).
    """
    if isinstance(e, And):
        return bool(e.terms) and all(
            _arm_fully_steerable(term, self_tag, steerable) for term in e.terms
        )
    if isinstance(e, Or):
        return any(_arm_fully_steerable(term, self_tag, steerable) for term in e.terms)
    if isinstance(e, Atom):
        if e.tag == self_tag:
            return False  # self-referencing seal-in arm — not a real route
        target = _atom_target(e)
        if target is not None:
            return target[0] in steerable
        # No single target value (an inequality): a steerable LHS is still a
        # lever the trace can drive to satisfy the threshold.
        if e.form in {"lt", "le", "gt", "ge", "ne"}:
            return e.tag in steerable
        return False
    return False  # unknown node — not directly steerable


def _or_ambiguity_over_inputs(
    ri: int,
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
) -> bool:
    """True when one writer's unsatisfied OR(s) each offer a directly-steerable arm.

    The route-choice surface exists so the engineer commits the machine to a
    materially different configuration.  When an OR has *any* arm reachable by
    directly-steerable inputs alone — ``Or(Auto, Manual)``, or the manual-jog
    branch ``And(Manual, DiverterBtn)`` beside an internal auto-sort branch —
    PILOT can take that arm without an internal commitment, so it collapses
    rather than reporting ambiguous (the internal arms stay available via
    ``choice=``/``via=``).  Returns False when there is no choice-bearing OR
    (nothing to collapse) or any choosing OR offers *no* steerable arm
    (``Or(ProdMode, MaintMode)`` — both coil-backed), which must stay surfaced.
    """
    ro = resolve_rung(program, pdg.rung_nodes[ri])
    if ro is None:
        return False
    sp = ro.sp_tree()
    if sp is None:
        return False
    expr = _sp_to_expr(sp)
    if _values_match(value, False) and tag in pdg.rung_nodes[ri].ote_writes:
        expr = _negate(expr)

    found_choice = False

    def walk(e: Any) -> bool:
        nonlocal found_choice
        if isinstance(e, And):
            return all(walk(term) for term in e.terms)
        if isinstance(e, Or):
            if _expr_satisfied(e, snapshot):
                return True  # already satisfied — contributes no choice
            found_choice = True
            # Collapse when at least one arm is fully steerable: PILOT takes it,
            # no engineer choice needed.  The trace's own Or-scorer then lands on
            # the cheapest (fewest non-steerable) arm, which is that steerable one.
            return any(_arm_fully_steerable(term, tag, steerable) for term in e.terms)
        return True  # atom / leaf — no choice here

    return walk(expr) and found_choice


def enumerate_trace_choices(
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    *,
    steerable: frozenset[str] = frozenset(),
    clear_only: frozenset[str] = frozenset(),
    max_choices: int = 16,
) -> tuple[TraceChoice, ...]:
    """Enumerate route choices for an ambiguous ``tag == value`` trace.

    General over the target value — ``Bool == True``, ``Bool == False`` (the
    writer guard is negated for an ``out`` coil, or only reset writers are
    viable for a retentive coil), or a word ``tag == value`` (only writers whose
    ``_can_produce`` admits *value* are viable).  The route/OR-arm derivation,
    Or-scorer collapse, and rank/tie-break rules are all target-agnostic.

    A "route" is a top-level decision in how *tag* reaches *value*: which
    writer rung drives it, and which arm of each OR in that writer's
    condition is taken.  Choices are **root-only** — each locks just this
    decision; ``trace_back`` re-traces everything below it from current
    state.  Deeper ambiguity (an OR in a downstream tag's writer) is not
    enumerated, by design: the engineer picks the output route, PILOT plans
    the rest.  This reuses ``trace_back``'s lock mechanism rather than
    re-walking the trace.

    A single writer whose *only* ambiguity is an OR among directly-steerable
    inputs (``Or(Auto, Manual)``) is **not** surfaced: those arms are inputs
    PILOT can assert directly, so it satisfies the cheapest and plans the rest.
    Multi-writer ambiguity, or an OR over internal coils (``Or(ProdMode,
    MaintMode)`` — materially different machine states), stays a real choice.
    """
    viable: list[int] = []
    for ri in _rank_writers(
        pdg.writers_of.get(tag, frozenset()),
        pdg,
        program,
        tag,
        value,
        snapshot,
        clear_only=clear_only,
    ):
        ro = resolve_rung(program, pdg.rung_nodes[ri])
        if ro is not None and _can_produce(_written_value_for_tag(ro, tag), value):
            viable.append(ri)

    multi_writer = len(viable) > 1
    if (
        not multi_writer
        and viable
        and _or_ambiguity_over_inputs(viable[0], tag, value, snapshot, pdg, program, steerable)
    ):
        return ()
    options: list[tuple[int | None, _RouteDraft]] = []
    for ri in viable:
        for draft in _writer_route_drafts(
            ri, tag, value, snapshot, pdg, program, max_choices=max_choices
        ):
            options.append((ri if multi_writer else None, draft))
            if len(options) >= max_choices:
                break
        if len(options) >= max_choices:
            break

    if len(options) <= 1:
        return ()

    choices: list[TraceChoice] = []
    for i, (writer_ri, draft) in enumerate(options[:max_choices], 1):
        route = draft.route
        writer_locks: tuple[tuple[str, Any, int], ...] = ()
        via_hint = draft.via_hint
        if writer_ri is not None:
            route = (_writer_label(tag, value, writer_ri, pdg.rung_nodes[writer_ri]), *route)
            writer_locks = ((tag, value, writer_ri),)
            # Multi-writer: the discriminator is the writer's own guard; the
            # OR-arm hint (if any) only refines it.
            via_hint = _writer_via_hint(writer_ri, tag, value, pdg, program) or via_hint
        choices.append(
            TraceChoice(
                id=str(i),
                label=_choice_label(route, tag, value),
                route=route,
                writer_locks=writer_locks,
                or_locks=draft.or_locks,
                via_hint=via_hint,
            )
        )
    return tuple(choices)


def writer_route_eligible(
    ri: int, tag: str, pdg: ProgramGraph, program: Any, steerable: frozenset[str]
) -> bool:
    """Is multi-writer route *ri* a sensible *default* (vs a material pivot)?

    Two gates, mirroring the OR-arm collapse:

    1. **Retentive** (``tag not in ote_writes``) — a latch/SET or copy/calc into a
       held register, so establishing via this writer is not clobbered by a
       later last-wins ``out``.  A non-retentive multi-out is a ``duplicate_out``
       conflict, never a safe default.
    2. **Input-gated** (``_arm_fully_steerable``) — the writer's guard is
       reachable by directly-steerable inputs alone (``Manual``), not an internal
       coil (``ProdMode``).  This is what keeps the Burner from auto-defaulting to
       a configuration the engineer should pick deliberately.

    Routes that pass both are preferred as the default; when none do (Burner) the
    default falls to the cheapest by trace score, rung order breaking ties.
    """
    if tag in pdg.rung_nodes[ri].ote_writes:
        return False
    ro = resolve_rung(program, pdg.rung_nodes[ri])
    if ro is None:
        return False
    sp = ro.sp_tree()
    if sp is None:
        return False
    return _arm_fully_steerable(_sp_to_expr(sp), tag, steerable)


def route_rung_order(choice: TraceChoice) -> tuple[int, ...]:
    """Deterministic rung-order tiebreak key for a route (lowest wins).

    The committed writer's rung index for a multi-writer route, else the chosen
    OR-arm indices — so ``Or(ProdMode, MaintMode)`` defaults to the first arm."""
    if choice.writer_locks:
        return (choice.writer_locks[0][2],)
    if choice.or_locks:
        return tuple(index for _, _, index in choice.or_locks)
    return ()


def _writer_route_drafts(
    ri: int,
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    *,
    max_choices: int,
) -> list[_RouteDraft]:
    """OR-arm route drafts for one writer rung's condition(s)."""
    rn = pdg.rung_nodes[ri]
    ro = resolve_rung(program, rn)
    if ro is None:
        return [_RouteDraft()]
    exprs: list[Any] = []
    sp = ro.sp_tree()
    if sp is not None:
        expr = _sp_to_expr(sp)
        if _values_match(value, False) and tag in rn.ote_writes:
            expr = _negate(expr)
        exprs.append(expr)
    if rn.subroutine:
        for cn in pdg.rung_nodes:
            if rn.subroutine in cn.calls:
                call_ro = resolve_rung(program, cn)
                call_sp = call_ro.sp_tree() if call_ro is not None else None
                if call_sp is not None:
                    exprs.append(_sp_to_expr(call_sp))
    if not exprs:
        return [_RouteDraft()]
    groups = [_enumerate_expr_routes(e, tag, snapshot, max_choices=max_choices) for e in exprs]
    return _combine_route_options(groups, max_choices=max_choices)


def _choice_label(route: tuple[str, ...], tag: str, value: Any) -> str:
    if len(route) >= 2:
        return route[-2]
    if route:
        return route[-1]
    return f"{tag}={value!r}"


def _compact_route(route: tuple[str, ...], *, max_items: int = 8) -> tuple[str, ...]:
    compact: list[str] = []
    seen: set[str] = set()
    for item in route:
        if item in seen:
            continue
        seen.add(item)
        compact.append(item)
    if len(compact) <= max_items:
        return tuple(compact)
    head = compact[: max_items - 2]
    return (*head, "...", compact[-1])


def _combine_route_options(
    groups: list[list[_RouteDraft]],
    *,
    max_choices: int,
) -> list[_RouteDraft]:
    drafts = [_RouteDraft()]
    for group in groups:
        if not group:
            continue
        combined: list[_RouteDraft] = []
        for left in drafts:
            for right in group:
                combined.append(
                    _RouteDraft(
                        route=left.route + right.route,
                        or_locks=left.or_locks + right.or_locks,
                        via_hint=left.via_hint if left.via_hint is not None else right.via_hint,
                    )
                )
                if len(combined) >= max_choices:
                    break
            if len(combined) >= max_choices:
                break
        drafts = combined
    return drafts[:max_choices]


def _enumerate_expr_routes(
    expr: Any,
    self_tag: str,
    snapshot: dict[str, Any],
    *,
    max_choices: int,
) -> list[_RouteDraft]:
    """Enumerate OR-arm selections within one writer's condition.

    Walks only the boolean structure (And/Or/Atom) of the condition — never
    into downstream writers — so the only decisions recorded are the OR arms
    of *this* condition.  That is the root-only contract: choices distinguish
    output routes, not the full downstream plan.
    """
    if isinstance(expr, And):
        groups = [
            _enumerate_expr_routes(term, self_tag, snapshot, max_choices=max_choices)
            for term in expr.terms
        ]
        return _combine_route_options(groups, max_choices=max_choices)

    if isinstance(expr, Or):
        if _expr_satisfied(expr, snapshot):
            return [_RouteDraft()]
        key = _expr_route_key(expr)
        result: list[_RouteDraft] = []
        for index, term in enumerate(expr.terms):
            if isinstance(term, Atom) and term.tag == self_tag:
                continue  # self-referencing seal-in arm
            label = _route_label_for_expr(term)
            hint = _expr_via_hint(term)
            for route in _enumerate_expr_routes(term, self_tag, snapshot, max_choices=max_choices):
                result.append(
                    route.extend(route=label, or_lock=(self_tag, key, index), via_hint=hint)
                )
                if len(result) >= max_choices:
                    return result
        return result or [_RouteDraft()]

    return [_RouteDraft()]


def _route_label_for_expr(expr: Any) -> str:
    if isinstance(expr, Atom):
        target = _atom_target(expr)
        if target is not None:
            tag, value = target
            return f"{tag}={value!r}"
    return str(expr)


def _expr_via_hint(expr: Any) -> tuple[str, Any] | None:
    """A concrete ``(tag, value)`` an engineer can name to select *expr*'s route.

    The first equality/bit atom found walking the And/Or/Atom structure — the
    OR arm's representative leaf (``ProdMode`` / ``Manual``).  Inequalities and
    other non-targetable atoms yield ``None`` (the renderer falls back to the
    route label).  Display + redirect-suggestion only; ``via_pred`` itself comes
    from the user, so a ``None`` hint never weakens correctness.
    """
    if isinstance(expr, Atom):
        return _atom_target(expr)
    if isinstance(expr, (And, Or)):
        for term in expr.terms:
            hint = _expr_via_hint(term)
            if hint is not None:
                return hint
    return None


def _writer_via_hint(
    ri: int, tag: str, value: Any, pdg: ProgramGraph, program: Any
) -> tuple[str, Any] | None:
    """The gating-condition discriminator for multi-writer route *ri*.

    A multi-writer Bool surfaces because two rungs drive it under different
    guards; the guard is what the engineer names to pick a writer (``via=Manual``
    vs ``via=State==5``).  Returns the writer condition's representative atom."""
    rn = pdg.rung_nodes[ri]
    ro = resolve_rung(program, rn)
    if ro is None:
        return None
    sp = ro.sp_tree()
    if sp is None:
        return None
    expr = _sp_to_expr(sp)
    if _values_match(value, False) and tag in rn.ote_writes:
        expr = _negate(expr)
    return _expr_via_hint(expr)


def _writer_label(tag: str, value: Any, rung_index: int, rung_node: Any) -> str:
    scope = rung_node.subroutine or rung_node.scope
    return f"{tag}={value!r} via {scope} rung {rung_index}"


# ---------------------------------------------------------------------------
# Steerable-input detection (copied from walk/priors.py)
# ---------------------------------------------------------------------------


def _operator_interface(
    tag: str,
    t: Any,
    pdg: ProgramGraph,
    program: Any,
) -> bool:
    """Whether *tag* is an operator/field-chosen interface the program only nudges.

    The type-independent core of steerability — identical terms for
    bool/int/dint/word/real/char.  A tag qualifies when the program is **not** its
    authoritative source of value, one of three ways:

    * **never program-written** — a pure input; its value comes from outside the
      program (operator / field / patch / force).  Steerable in any type, provided
      it is read somewhere (a wholly-unused declaration is not a lever).
    * **clear-only (ack-cleared)** — every writer merely *resets it to its
      rest/default* value (``reset()`` on a Bool, ``copy(0, flag)`` on an Int/Word).
      The program never asserts the active value, so that value must come from
      outside — the acknowledge pattern (PackML command bits like ``C_Clear`` /
      ``C_UnitModeChgRequest``).  Steerable in any type regardless of ``external``,
      and even when the clear is unconditional every scan (a momentary command).
    * **externally declared and only nudged** — ``external=True`` and every writer
      stamps a literal (any value) under a condition, so the operator's value
      persists between the program's nudges.

    A writer that derives the value from live state (a non-literal write) or drives
    the tag through an ``out`` coil means the program authors it — the interface is
    upstream, not here.  For the external-nudge arm an *unconditional* clobber also
    disqualifies (the program owns the rest state); the clear-only arm is exempt,
    since resetting to rest is precisely what an ack-cleared command expects.

    This one predicate subsumes the former per-type rules — never-written INPUT,
    ack-cleared Bool, external Int/Dint/Word command register — and by construction
    extends them to Real and Char, which no per-type rule covered.
    """
    writers = pdg.writers_of.get(tag, frozenset())
    if not writers:
        # Pure input: chosen entirely outside the program.  Require a reader so a
        # wholly-unused declaration is not surfaced as a phantom lever.
        return bool(pdg.readers_of.get(tag, frozenset()))
    if not pdg.readers_of.get(tag, frozenset()):
        return False
    if _clear_only_command(tag, t, pdg, program):
        # Program only ever resets it to rest; the operator/field supplies the
        # active value.  An ack-cleared command interface — steerable in any type,
        # external or not, unconditional clear or not.
        return True
    # External-nudge arm: every writer stamps a literal (not necessarily the
    # default) under a condition, so the operator's value persists between the
    # program's nudges.  An out coil / non-literal write (program authors the
    # value) disqualifies, and only an externally declared register with no
    # unconditional every-scan clobber qualifies.
    for ri in writers:
        rung_node = pdg.rung_nodes[ri]
        if tag in rung_node.ote_writes:
            return False  # out-coil driven: a computed output, not a nudge
        ro = resolve_rung(program, rung_node)
        if ro is None or _literal_write(ro, tag) is None:
            return False  # derives from live state — the program authors it
    if not getattr(t, "external", False):
        return False
    return not any(_rung_unconditional(pdg.rung_nodes[ri], pdg, program) for ri in writers)


def _clear_only_command(tag: str, t: Any, pdg: ProgramGraph, program: Any) -> bool:
    """Every writer merely resets *tag* to its rest/default — the ack-cleared idiom.

    ``reset()`` on a Bool, ``copy(0, flag)`` / ``fill(0, ...)`` on an Int/Word: the
    program never asserts the active value, so it must come from outside — a
    *momentary* operator/field command (``C_Clear``, ``C_UnitModeChgRequest``,
    ``Heat_xInit``).  Requires the tag be program-written *and* read; an out coil or
    non-literal (live-state) write means the program authors it, so not clear-only.
    Sound without ``external`` and even under an unconditional clear every scan.

    The structural fact behind two drive-layer decisions (:func:`compute_clear_only`
    threads it): such a tag's idiom is pulse-and-release, so it is never a
    prerequisite *hold* (holding it steady asserts the command forever) and never a
    *preferred* init/reset writer gate (:func:`_rank_writers`).
    """
    writers = pdg.writers_of.get(tag, frozenset())
    if not writers or not pdg.readers_of.get(tag, frozenset()):
        return False
    default = getattr(t, "default", None)
    for ri in writers:
        rung_node = pdg.rung_nodes[ri]
        if tag in rung_node.ote_writes:
            return False
        ro = resolve_rung(program, rung_node)
        lw = _literal_write(ro, tag) if ro is not None else None
        if lw is None or not _values_match(lw, default):
            return False
    return True


def compute_steerable(
    pdg: ProgramGraph,
    known: dict[str, Any],
    program: Any,
) -> frozenset[str]:
    """Tags PILOT may command directly, by intrinsic characteristics — any type.

    A tag is steerable when its value is an operator/field-chosen interface the
    program does not author each scan (see :func:`_operator_interface`).  Read-only
    and system tags (``rtc.*``, ``sys.*``) are never steerable.  Genuine program
    constants that merely seed lookup-table pointers are removed separately, at the
    drive layer, via :func:`compute_reference_constants`.
    """
    from pyrung.core.system_points import READ_ONLY_SYSTEM_TAG_NAMES

    out: set[str] = set()
    for tag in set(pdg.readers_of) | set(pdg.writers_of):
        if tag in READ_ONLY_SYSTEM_TAG_NAMES:
            continue
        if getattr(known.get(tag), "readonly", False):
            continue
        if _operator_interface(tag, known.get(tag), pdg, program):
            out.add(tag)
    return frozenset(out)


def compute_clear_only(
    pdg: ProgramGraph,
    known: dict[str, Any],
    program: Any,
) -> frozenset[str]:
    """Clear-only (ack-cleared momentary) command tags — the pulse-treatment set.

    A subset of :func:`compute_steerable`: every writer only ever resets the tag to
    rest (:func:`_clear_only_command`).  The program's own clear declares the idiom
    is pulse-and-release, so the drive layer keeps these off prerequisite *holds*
    (candidates.py) and off *preferred* init/reset writer selection
    (:func:`_rank_writers`) — holding one steady, or routing through it, asserts a
    momentary command forever.  Read-only / system tags are excluded, mirroring
    :func:`compute_steerable`.
    """
    from pyrung.core.system_points import READ_ONLY_SYSTEM_TAG_NAMES

    out: set[str] = set()
    for tag in set(pdg.readers_of) | set(pdg.writers_of):
        if tag in READ_ONLY_SYSTEM_TAG_NAMES:
            continue
        if getattr(known.get(tag), "readonly", False):
            continue
        if _clear_only_command(tag, known.get(tag), pdg, program):
            out.add(tag)
    return frozenset(out)


def compute_reference_constants(
    pdg: ProgramGraph, program: Any, known: dict[str, Any] | None = None
) -> frozenset[str]:
    """Never-written copy sources feeding into lookup-table pointer chains.

    Four conditions, all must hold:
    1. Tag has no writers (initial-value only)
    2. Used as a copy/fill source feeding some destination D
    3. D participates in a lookup-table pipeline — either D is a direct
       indirect-copy pointer, or D is the representative of a pointer
       via functional dependency (``calc(D + offset, ptr)``)
    4. Tag is **not** ``external`` — a declared program constant, not an
       operator/field interface (given *known*; unchecked when *known* is None).

    The functional dep collapse is key: ``sm__jump_target_ds_idx =
    S_StateRequested + 150`` means S_StateRequested is the representative
    of the pointer.  So ``copy(sm__STATESTARTINGREF, S_StateRequested)``
    makes sm__STATESTARTINGREF a reference constant — it feeds into the
    lookup-table machinery through the collapsed pointer chain.

    Condition 4 is what keeps an *operator command* that happens to index a
    table (``copy(ToolReqCmd, ToolReq)`` with ``calc(ToolReq + k, idx)``) out of
    the constant set: the operator chooses the value, so it stays steerable.
    """
    from pyrung.core.instruction.data_transfer import CopyInstruction, FillInstruction
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef

    # Step 1: find direct pointer tags from indirect copies.
    pointer_tags: set[str] = set()

    def _scan_pointers(rungs: Any) -> None:
        for r in rungs:
            for instr in getattr(r, "_instructions", ()):
                if isinstance(instr, CopyInstruction):
                    src = instr.source
                    if isinstance(src, IndirectRef):
                        name = getattr(src.pointer, "name", None)
                        if name:
                            pointer_tags.add(name)
                    elif isinstance(src, IndirectExprRef):
                        names = _expr_tag_names(src.expr)
                        if names:
                            pointer_tags.update(names)
            _scan_pointers(getattr(r, "_branches", ()))

    _scan_pointers(program.rungs)
    for sub_rungs in getattr(program, "subroutines", {}).values():
        _scan_pointers(sub_rungs)

    if not pointer_tags:
        return frozenset()

    # Step 2: follow functional deps (calc-defined scratch) to find
    # representative tags.  ptr = calc(rep + offset) → rep drives ptr.
    pipeline_tags = set(pointer_tags)
    for ptr in list(pointer_tags):
        tag = ptr
        for _ in range(3):
            defn = _single_calc_source(tag, pdg, program)
            if defn is None:
                break
            _expr, rep = defn
            pipeline_tags.add(rep)
            tag = rep

    # Step 3: find never-written tags used as copy sources into pipeline tags.
    candidates: set[str] = set()

    def _scan_sources(rungs: Any) -> None:
        for r in rungs:
            for instr in getattr(r, "_instructions", ()):
                if isinstance(instr, CopyInstruction):
                    src_name = getattr(instr.source, "name", None)
                    dest_name = getattr(instr.dest, "name", None)
                    if src_name and dest_name and dest_name in pipeline_tags:
                        candidates.add(src_name)
                elif isinstance(instr, FillInstruction):
                    src_name = getattr(instr.value, "name", None)
                    dest_name = getattr(instr.dest, "name", None)
                    if src_name and dest_name and dest_name in pipeline_tags:
                        candidates.add(src_name)
            _scan_sources(getattr(r, "_branches", ()))

    _scan_sources(program.rungs)
    for sub_rungs in getattr(program, "subroutines", {}).values():
        _scan_sources(sub_rungs)

    # Step 3b: the table *contents*.  A never-written ds/dh slot reached ONLY
    # through ``ds[computed]`` (a computed-index read, never a plain copy source)
    # is data, not a lever — but when the pointer is bounded (choices / min-max)
    # the PDG registers those slots as readers, so ``_operator_interface`` would
    # otherwise classify each as steerable and the skiff would waste probes on a
    # data-only constant.  Add the slots an indirect read can land on; the
    # ``_is_program_constant`` filter below keeps a written or ``external`` slot
    # (an operator-indexed table) out, exactly as condition 4 does for sources.
    from pyrung.core.analysis.pdg import _indirect_expr_base_tag, _indirect_ref_tags

    def _indirect_read_slots(src: Any) -> list[str]:
        if isinstance(src, IndirectRef):
            tags = _indirect_ref_tags(src.block, src.pointer)
        elif isinstance(src, IndirectExprRef):
            base = _indirect_expr_base_tag(src.expr)
            tags = _indirect_ref_tags(src.block, base) if base is not None else None
        else:
            return []
        return [t.name for t in tags] if tags is not None else []

    def _scan_indirect_reads(rungs: Any) -> None:
        for r in rungs:
            for instr in getattr(r, "_instructions", ()):
                if isinstance(instr, CopyInstruction):
                    candidates.update(_indirect_read_slots(instr.source))
            _scan_indirect_reads(getattr(r, "_branches", ()))

    _scan_indirect_reads(program.rungs)
    for sub_rungs in getattr(program, "subroutines", {}).values():
        _scan_indirect_reads(sub_rungs)

    def _is_program_constant(n: str) -> bool:
        if pdg.writers_of.get(n, frozenset()):
            return False  # written by the program — a pipeline tag, not a constant
        if known is not None and getattr(known.get(n), "external", False):
            return False  # an operator/field interface, not a program constant
        return True

    return frozenset(n for n in candidates if _is_program_constant(n))


def compute_edge_tags(pdg: ProgramGraph, program: Any) -> set[str]:
    """Tag names read through ``rise()``/``fall()`` anywhere in the program."""
    from pyrung.core.analysis.simplified import And, Atom, Or

    result: set[str] = set()

    def visit(e: Any) -> None:
        if isinstance(e, Atom):
            if e.form in ("rise", "fall"):
                result.add(e.tag)
        elif isinstance(e, (And, Or)):
            for term in e.terms:
                visit(term)

    seen: set[int] = set()
    for rung_node in pdg.rung_nodes:
        ro = resolve_rung(program, rung_node)
        if ro is None or id(ro) in seen:
            continue
        seen.add(id(ro))
        sp = ro.sp_tree()
        if sp is not None:
            visit(_sp_to_expr(sp))
    return result


def compute_resting_values(
    steerable: frozenset[str],
    known: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
) -> dict[str, Any]:
    """Map each steerable input to its resting value.

    For pure INPUTs: the tag's declared default.
    For ack-cleared Bools: the program's clearing value via
    ``_scan_transient_rest``.
    """
    resting: dict[str, Any] = {}
    for name in steerable:
        t = known.get(name)
        if t is None:
            resting[name] = False
            continue
        transient, rest_val = _scan_transient_rest(name, pdg, program)
        if transient and rest_val is not None:
            resting[name] = rest_val
        else:
            resting[name] = getattr(t, "default", False)
    return resting


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _decompose_sum(
    env: _TraceEnv,
    wv: Aggregate,
    tag: str,
    value: Any,
    *,
    _visited: Any,
    _ancestry: Any,
    _depth: int,
) -> list[TraceNode]:
    """Decompose a sum-aggregate writer into per-element children.

    For ``value != 0``: the sum is non-zero because at least one element
    is non-zero.  Find contributing elements, trace each one.
    For ``value == 0``: every element must be zero.  Trace each non-zero
    element as a prerequisite to clear.
    """
    if _values_match(value, 0):
        element_tags = [t for t in wv.tags if not _values_match(env.snapshot.get(t), 0)]
        target_value: Any = 0
    elif isinstance(value, (int, float)) and value != 0:
        element_tags = [t for t in wv.tags if not _values_match(env.snapshot.get(t), 0)]
        if not element_tags:
            return []
        target_value = None
    else:
        return []

    children: list[TraceNode] = []
    for elem_tag in element_tags:
        elem_value = env.snapshot.get(elem_tag, 0) if target_value is None else target_value
        child = _trace_back(
            env,
            elem_tag,
            elem_value,
            _visited=_visited,
            _ancestry=_ancestry,
            _depth=_depth + 1,
        )
        child.data_flow = "aggregate"
        children.append(child)
    return children


_IDX_CHASE_CAP = 32


_FORM_TO_CMP_OP = {"eq": "==", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}


def _atom_comparison(atom: Any) -> tuple[str, str, Any] | None:
    """``(tag, op, literal_bound)`` for a comparison atom against a constant."""
    form = getattr(atom, "form", None)
    op = _FORM_TO_CMP_OP.get(form) if isinstance(form, str) else None
    if op is None:
        return None
    operand = getattr(atom, "operand", None)
    if not isinstance(operand, (int, float)) or isinstance(operand, bool):
        return None
    return (atom.tag, op, operand)


def _condition_required_values(expr: Any) -> list[tuple[str, Any]]:
    """``(tag, value)`` conjuncts a condition requires (``And`` of xic/xio/eq)."""
    from pyrung.core.analysis.simplified import And, Atom
    from pyrung.core.analysis.sp_values import _required_from_atom

    out: list[tuple[str, Any]] = []

    def visit(e: Any) -> None:
        if isinstance(e, Atom):
            pairs = _required_from_atom(e)
            if pairs:
                out.extend(pairs)
        elif isinstance(e, And):
            for t in e.terms:
                visit(t)

    visit(expr)
    return out


def _flag_gate_comparisons(
    env: _TraceEnv, flag_tag: str, flag_val: Any
) -> list[tuple[str, str, Any]]:
    """Comparison atoms gating a writer that sets *flag_tag* to *flag_val*."""
    from pyrung.core.analysis.simplified import And, Atom, Or

    out: list[tuple[str, str, Any]] = []
    for ri in sorted(env.pdg.writers_of.get(flag_tag, frozenset())):
        ro = resolve_rung(env.program, env.pdg.rung_nodes[ri])
        if ro is None:
            continue
        if not _can_produce(_written_value_for_tag(ro, flag_tag), flag_val):
            continue
        sp = ro.sp_tree()
        if sp is None:
            continue

        def visit(e: Any) -> None:
            if isinstance(e, Atom):
                cmp = _atom_comparison(e)
                if cmp is not None:
                    out.append(cmp)
            elif isinstance(e, (And, Or)):
                for t in e.terms:
                    visit(t)

        visit(_sp_to_expr(sp))
    return out


def _transition_fire_pins(
    env: _TraceEnv, ro: Any, tag: str, value: Any, csb: tuple[str, Any] | None
) -> dict[str, Any]:
    """Data-flow pins a transition writer imposes the scan it produces *value*.

    The semantic key for the table-oracle trigger: an enablement gate recomputed
    each scan from constant-table lookups is indexed by the transition's own
    pins, so evaluating it needs the *fire-time* source values, not the
    snapshot's.  Soundly derivable in three writer shapes, none guessed:

    - a copy binding — ``copy(src, tag)`` forces ``src == inverse(value)``
      (:func:`~pyrung.core.analysis.sp_values.copy_source_binding`; the identity
      copy gives ``src == value``, a converting copy its exact preimage);
    - an affine calc — ``calc(src + k, tag)`` forces ``src == value - k``
      (:func:`~pyrung.core.analysis.sp_values.calc_source_binding`), inverted
      through the crossings registry;
    - a **non-affine calc** — ``calc(A * B, tag)``, ``calc(A & mask, tag)``,
      ``calc((A << 2) | B, tag)`` — that the crossing can't invert symbolically,
      solved by enumerate-and-evaluate over the sources' *complete* finite
      domains (:func:`~pyrung.core.analysis.pilot.table_oracle.solve_calc_preimage`),
      pinning only the FORCED source values (those shared by every satisfying
      assignment).

    A decode transition (literal write gated on ``src == v``) carries its pin in
    its *guard*, which the caller layers on separately.  Empty dict when no
    data-flow pin is derivable — never a fabricated binding.
    """
    from pyrung.core.analysis.sp_values import calc_source_binding

    if csb is not None:
        src_tag, src_val = csb
        return {src_tag: src_val}
    ccb = calc_source_binding(ro, tag, value)
    if ccb is not None:
        return {ccb[0]: ccb[1]}
    # Non-affine calc decode: no symbolic inverse, so solve the expression over
    # the sources' complete finite domains and pin only the forced values.
    from pyrung.core.analysis.pilot.table_oracle import solve_calc_preimage

    domains = env.prior.nd_domains if env.prior is not None else None
    pins = solve_calc_preimage(tag, value, env.snapshot, env.pdg, env.program, domains=domains)
    return pins or {}


def _writer_guard_verdict(
    env: _TraceEnv,
    ri: int,
    ro: Any,
    tag: str,
    value: Any,
    csb: tuple[str, Any] | None,
    guard_expr: Any,
) -> str:
    """Table-oracle verdict for a candidate writer's guard under its own fire pins.

    The rejection arm of ``table_oracle`` (``pilot/CLAUDE.md``: "tries first — and
    punts — and the sandbox is its escalation").  Fixes the pins the writer
    *itself* forces to produce ``value`` (:func:`_transition_fire_pins` — the
    inverted copy/affine source, never a borrowed pin) and enumerates the
    remaining guard operands over the ``DomainPrior``'s ``nd_domains`` (the
    prover-derived complete domains; a Bool resolves to ``(False, True)``, a
    missing domain punts inside the oracle).  Returns one of
    ``GUARD_DEAD``/``GUARD_SAT``/``GUARD_PUNT``.

    Memoized on ``(rung id, fire-pins, guard route key)``: the verdict is a pure
    function of those plus the trace-invariant snapshot/domains, so one enumeration
    per distinct writer/pin/guard suffices for the whole ``trace_back`` recursion.

    Soundness gate: a ``GUARD_DEAD`` proof is only valid over *complete* free-tag
    domains.  The prover's ``nd_domains`` are complete by construction and a Bool
    is trivially ``(False, True)``, but the oracle's softer fallbacks
    (``_index_values`` / producible-literal chains) are only *plausible* value
    sets — enumerating a guard over an incomplete domain would fabricate a
    rejection.  So we punt unless every free guard operand is either Bool-typed or
    carries an ``nd_domains`` entry; only then is the enumeration sound.
    """
    from pyrung.core.analysis.pilot.table_oracle import GUARD_PUNT, guard_verdict
    from pyrung.core.tag import TagType

    pins = _transition_fire_pins(env, ro, tag, value, csb)
    key = (ri, tuple(sorted(pins.items(), key=lambda kv: kv[0])), _expr_route_key(guard_expr))
    cached = env.guard_memo.get(key)
    if cached is not None:
        return cached

    nd_domains = env.prior.nd_domains if env.prior is not None else None
    free = _simplified_expr_tags(guard_expr) - set(pins)

    def _complete_domain(t: str) -> bool:
        if nd_domains is not None and t in nd_domains:
            return True
        tag_ref = env.pdg.tags.get(t)
        return tag_ref is not None and getattr(tag_ref, "type", None) is TagType.BOOL

    if not all(_complete_domain(t) for t in free):
        # A free operand lacks a provably-complete domain (a live word, or a tag
        # the prover left unconstrained) — never fabricate a rejection.
        env.guard_memo[key] = GUARD_PUNT
        return GUARD_PUNT

    verdict = guard_verdict(
        guard_expr,
        fixed=pins,
        snapshot=env.snapshot,
        pdg=env.pdg,
        program=env.program,
        domains=nd_domains,
    )
    env.guard_memo[key] = verdict
    return verdict


def _table_enablement_prereqs(
    env: _TraceEnv,
    ro: Any,
    tag: str,
    value: Any,
    csb: tuple[str, Any] | None,
    *,
    _visited: set[tuple[str, Any]],
    _ancestry: tuple[tuple[str, Any], ...],
    _depth: int,
) -> list[TraceNode]:
    """Prerequisites for an enablement flag decided by a constant-table predicate.

    A PackML transition (``copy(StateReq, StateCur)``, an affine
    ``calc(StateReq + k, StateCur)``, or a decode ``copy(10, StateCur)`` gated on
    ``StateReq == 10``) is gated by ``isStateEnbl_Yes==1``, whose own writer is
    gated by ``stateMask[StateReq] & disabledMask[Mode] == 0``.  That predicate
    register is recomputed from ``StateReq`` every scan, so its snapshot value is
    stale w.r.t. the planned transition and trace would otherwise read the gate
    as satisfied.  The trigger is *semantic*, not idiom-shaped: whenever the
    transition's fire-time pins are soundly derivable
    (:func:`_transition_fire_pins` — the inverted data-flow source and/or the
    guard's own required conjuncts), consult the table oracle with those pins
    fixed and surface the steerable index — the mode — that makes the gate hold,
    as an ``Or`` whose cheapest arm trace drives.  No derivable pin ⇒ punt
    (never enumerate an unpinned predicate — that would surface prerequisites
    unconditioned on the actual transition).
    """
    sp = ro.sp_tree()
    if sp is None:
        return []
    pins = _transition_fire_pins(env, ro, tag, value, csb)

    from pyrung.core.analysis.pilot.table_oracle import solve_table_predicate

    domains = env.prior.nd_domains if env.prior is not None else None
    required = _condition_required_values(_sp_to_expr(sp))
    prereqs: list[TraceNode] = []
    seen_idx: set[str] = set()
    for flag_tag, flag_val in required:
        if flag_tag == tag or flag_tag in pins:
            continue
        # Fire-time pins for this flag's gate: the data-flow pin plus the
        # guard's *other* required conjuncts (a decode transition carries its
        # source pin in its own guard, not in data flow — those conjuncts hold
        # the scan the writer fires, so they are sound pins for the recomputed
        # predicate).  Nothing pinned means the planned transition constrains
        # nothing the oracle could key on — punt, exactly as before.
        fixed = dict(pins)
        for other_tag, other_val in required:
            if other_tag not in (tag, flag_tag) and other_tag not in fixed:
                fixed[other_tag] = other_val
        if not fixed:
            continue
        for pred_tag, op, bound in _flag_gate_comparisons(env, flag_tag, flag_val):
            sol = solve_table_predicate(
                pred_tag,
                bound,
                op,
                env.snapshot,
                env.pdg,
                env.program,
                fixed=fixed,
                domains=domains,
            )
            if sol is None:
                continue
            for idx_tag, idx_vals in sol.per_tag.items():
                if idx_tag in seen_idx or not idx_vals:
                    continue
                if any(_values_match(env.snapshot.get(idx_tag), v) for v in idx_vals):
                    continue  # already in a satisfying mode — gate genuinely holds
                seen_idx.add(idx_tag)
                arms = [
                    _trace_back(
                        env,
                        idx_tag,
                        v,
                        _visited=set(_visited),
                        _ancestry=_ancestry,
                        _depth=_depth + 1,
                    )
                    for v in idx_vals
                ]
                # Respect avoid=/via= the same way OR-arm selection does: drop an
                # arm whose assignment forces the avoided condition (so
                # ``avoid=(UnitModeCurrent == 0)`` steers off the degenerate
                # Undefined slot), and prefer one that forces via=.  Never
                # over-prune — if every arm is avoided, keep them all.
                if env.avoid_pred is not None:
                    kept = [n for n in arms if not _route_forces([n], env.snapshot, env.avoid_pred)]
                    if kept:
                        arms = kept
                if env.via_pred is not None:
                    preferred = [n for n in arms if _route_forces([n], env.snapshot, env.via_pred)]
                    if preferred:
                        arms = preferred
                # Keep only the arms PILOT can actually drive.  The oracle admits
                # every table-satisfying index, but some are dead: the degenerate
                # mode 0 is producible only via the reset ``copy(0, UnitModeCmd)``,
                # and a mode whose sole writer is a spent ``~InitDone`` init rung
                # drives nothing.  ``_trace_score`` alone would *prefer* those (no
                # steerable leaves ⇒ zero blast ⇒ sorts first), so PILOT would
                # surface a mode it cannot command.  Filtering to pilotable arms
                # lets it see, without a hard-coded filter, that mode 0 leads
                # nowhere; score only breaks ties among the drivable ones.
                pilotable = [n for n in arms if _route_pilotable([n])]
                if pilotable:
                    arms = pilotable
                best = min(arms, key=lambda n: _trace_score([n], env.pdg))
                best.data_flow = "enable"
                prereqs.append(best)
    return prereqs


def _invert_indirect(
    ro: Any,
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
) -> tuple[str, list[Any]] | None:
    """Invert an indirect copy: find which index values produce *value*.

    For ``copy(block[ptr], tag)`` or ``copy(block[expr], tag)``, read the
    block from the snapshot and find which pointer values land on a slot
    holding *value*.  Hops through calc-defined scratch pointers
    (e.g. ``calc(S_StateRequested + 150, idx)``).

    Returns ``(index_tag, [matching_values])`` or ``None``.

    This is the single-table / identity-predicate slice of the constant-table
    inversion generalized by ``table_oracle.solve_table_predicate`` (N tables, an
    arbitrary predicate).  Both share ``table_from_indirect_src`` (operand model)
    and ``_read_table`` (slot read).
    """
    from pyrung.core.analysis.pilot.table_oracle import _read_table, table_from_indirect_src
    from pyrung.core.instruction.data_transfer import CopyInstruction
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef

    # Find the indirect copy instruction writing our tag.
    src = None
    for instr in ro._instructions:
        if not isinstance(instr, CopyInstruction):
            continue
        if getattr(instr.dest, "name", None) != tag:
            continue
        if isinstance(instr.source, (IndirectRef, IndirectExprRef)):
            src = instr.source
        break
    if src is None:
        return None

    # Model the source as ``table[eval_addr(index)]``, then keep the plausible
    # index values whose slot holds our target.
    table = table_from_indirect_src(src, snapshot, pdg, program)
    if table is None or table.index_tag == tag:
        return None
    inverting = [
        v
        for v in _index_values(table.index_tag, snapshot, pdg, program)
        if _values_match(_read_table(table, v, snapshot), value)
    ]
    if not inverting:
        return None
    return table.index_tag, inverting


def _single_calc_source(idx_tag: str, pdg: ProgramGraph, program: Any) -> tuple[Any, str] | None:
    """``(expression, source_tag)`` when *idx_tag* has a single calc writer.

    Handles ``calc(S_StateRequested + 150, sm__jump_target_ds_idx)`` —
    the pointer register is computed from one other tag.
    """
    from pyrung.core.instruction.calc import CalcInstruction

    writers = pdg.writers_of.get(idx_tag, frozenset())
    if len(writers) != 1:
        return None
    ro = resolve_rung(program, pdg.rung_nodes[next(iter(writers))])
    if ro is None:
        return None
    for instr in ro._instructions:
        if isinstance(instr, CalcInstruction) and getattr(instr.dest, "name", None) == idx_tag:
            names = _expr_tag_names(instr.expression)
            if not names:
                return None
            mutable = {n for n in names if pdg.writers_of.get(n)} - {idx_tag}
            if len(mutable) != 1:
                return None
            src = next(iter(mutable))
            return instr.expression, src
    return None


def _index_values(
    idx_tag: str,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
) -> list[int]:
    """Plausible values for an index register, current value first."""
    from pyrung.core.crossing import Literal as _Literal

    rest: set[int] = set()
    current = snapshot.get(idx_tag)
    for ri in sorted(pdg.writers_of.get(idx_tag, frozenset())):
        ro = resolve_rung(program, pdg.rung_nodes[ri])
        if ro is None:
            continue
        wv = _written_value_for_tag(ro, idx_tag)
        if isinstance(wv, _Literal):
            v = wv.value
            if isinstance(v, int) and not isinstance(v, bool):
                rest.add(v)
        else:
            from pyrung.core.analysis.sp_values import _named_copy_source, _writer_for_tag

            _instr = _writer_for_tag(ro, idx_tag)
            src_name = _named_copy_source(_instr) if _instr is not None else None
            if src_name is not None and src_name != idx_tag:
                v = snapshot.get(src_name)
                if isinstance(v, int) and not isinstance(v, bool):
                    rest.add(v)
    out: list[int] = []
    if isinstance(current, int) and not isinstance(current, bool):
        out.append(current)
        rest.discard(current)
    out.extend(sorted(rest))
    return out[:_IDX_CHASE_CAP]


def _visit_key(tag: str, value: Any) -> tuple[str, Any]:
    if isinstance(value, (bool, int, float, str, type(None))):
        return (tag, value)
    return (tag, id(value))


def _can_produce(wv: Any, value: Any) -> bool:
    if isinstance(wv, Literal):
        return _values_match(wv.value, value)
    if isinstance(wv, Affine):
        return True
    if isinstance(wv, Aggregate):
        return True
    return True  # UNKNOWN — assume it could


def _simplified_expr_tags(e: Any) -> set[str]:
    """Tag names referenced by a simplified ``Atom``/``And``/``Or`` expression."""
    if isinstance(e, Atom):
        tags = {e.tag}
        if isinstance(e.operand, str):
            tags.add(e.operand)
        return tags
    if isinstance(e, (And, Or)):
        out: set[str] = set()
        for term in e.terms:
            out |= _simplified_expr_tags(term)
        return out
    return set()


_GUARD_CONTRADICTION = object()


class _WriterAvailability(IntEnum):
    AVAILABLE_NOW = 0
    AFTER_PREREQ = 1
    UNKNOWN = 2
    UNAVAILABLE_FROM_HERE = 3


def _partial_eval_guard(expr: Any, known: dict[str, Any]) -> Any:
    """Partial-evaluate a simplified guard using exact fire-time pins only."""
    if not known or isinstance(expr, Const):
        return expr
    if isinstance(expr, Atom):
        tags = {expr.tag}
        if isinstance(expr.operand, str):
            tags.add(expr.operand)
        if tags <= known.keys():
            result = _eval_expr_from_state(expr, known)
            if result is not None:
                return Const(result)
        return expr
    if isinstance(expr, And):
        terms: list[Any] = []
        for term in expr.terms:
            reduced = _partial_eval_guard(term, known)
            if isinstance(reduced, Const):
                if not reduced.value:
                    return Const(False)
                continue
            terms.append(reduced)
        if not terms:
            return Const(True)
        return terms[0] if len(terms) == 1 else And(terms=tuple(terms))
    if isinstance(expr, Or):
        terms = []
        for term in expr.terms:
            reduced = _partial_eval_guard(term, known)
            if isinstance(reduced, Const):
                if reduced.value:
                    return Const(True)
                continue
            terms.append(reduced)
        if not terms:
            return Const(False)
        return terms[0] if len(terms) == 1 else Or(terms=tuple(terms))
    return expr


def _reduce_guard_by_fire_pins(
    guard_expr: Any,
    ro: Any,
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
) -> Any:
    """Drop guard arms decided by a writer's own exact fire-time pins.

    For a self-referential affine writer, producing the target value fixes the
    source value for the firing scan and any one-hop derived tags.  Reduce only
    against those exact pins, not the live snapshot, so an OR arm contradicted
    by the source pin does not hide a sibling frontier.
    """
    built = projected_writer_overlay(ro, tag, value, snapshot, pdg, program, {})
    if built is None:
        return guard_expr
    overlay, _local_pinned = built
    reduced = _partial_eval_guard(guard_expr, overlay)
    if isinstance(reduced, Const):
        return None if reduced.value else _GUARD_CONTRADICTION
    return reduced


def _expr_availability(
    expr: Any,
    snapshot: dict[str, Any],
    steerable: frozenset[str],
    current_tags: frozenset[str],
    pdg: ProgramGraph,
    program: Any,
) -> _WriterAvailability:
    """Current-frame availability of a guard expression.

    False steerable leaves are still available tools; false current-state leaves
    are unavailable here; other false leaves are prerequisites trace may pursue.
    """
    if isinstance(expr, Const):
        return (
            _WriterAvailability.AVAILABLE_NOW
            if expr.value
            else _WriterAvailability.UNAVAILABLE_FROM_HERE
        )
    if isinstance(expr, Atom):
        result = _eval_expr_from_state(expr, snapshot)
        if result is True:
            return _WriterAvailability.AVAILABLE_NOW
        if result is None:
            return _WriterAvailability.UNKNOWN
        pairs = _required_from_atom(expr)
        if pairs:
            alias_states: list[_WriterAvailability] = []
            for req_tag, req_value in pairs:
                alias = _equality_gated_coil(req_tag, req_value, pdg, program)
                if alias is None:
                    continue
                governing, values = alias
                if governing not in snapshot:
                    alias_states.append(_WriterAvailability.UNKNOWN)
                elif any(_values_match(snapshot.get(governing), v) for v in values):
                    alias_states.append(_WriterAvailability.AVAILABLE_NOW)
                else:
                    alias_states.append(_WriterAvailability.UNAVAILABLE_FROM_HERE)
            if alias_states:
                if any(s == _WriterAvailability.AVAILABLE_NOW for s in alias_states):
                    return _WriterAvailability.AVAILABLE_NOW
                if any(s == _WriterAvailability.UNKNOWN for s in alias_states):
                    return _WriterAvailability.UNKNOWN
                return _WriterAvailability.UNAVAILABLE_FROM_HERE
        if expr.tag in current_tags:
            return _WriterAvailability.UNAVAILABLE_FROM_HERE
        if expr.tag in steerable:
            return _WriterAvailability.AVAILABLE_NOW
        return _WriterAvailability.AFTER_PREREQ
    if isinstance(expr, And):
        states = [
            _expr_availability(term, snapshot, steerable, current_tags, pdg, program)
            for term in expr.terms
        ]
        if any(s == _WriterAvailability.UNAVAILABLE_FROM_HERE for s in states):
            return _WriterAvailability.UNAVAILABLE_FROM_HERE
        if any(s == _WriterAvailability.UNKNOWN for s in states):
            return _WriterAvailability.UNKNOWN
        if any(s == _WriterAvailability.AFTER_PREREQ for s in states):
            return _WriterAvailability.AFTER_PREREQ
        return _WriterAvailability.AVAILABLE_NOW
    if isinstance(expr, Or):
        states = [
            _expr_availability(term, snapshot, steerable, current_tags, pdg, program)
            for term in expr.terms
        ]
        if any(s == _WriterAvailability.AVAILABLE_NOW for s in states):
            return _WriterAvailability.AVAILABLE_NOW
        if any(s == _WriterAvailability.AFTER_PREREQ for s in states):
            return _WriterAvailability.AFTER_PREREQ
        if any(s == _WriterAvailability.UNKNOWN for s in states):
            return _WriterAvailability.UNKNOWN
        return _WriterAvailability.UNAVAILABLE_FROM_HERE
    return _WriterAvailability.UNKNOWN


def _or_availability(states: list[_WriterAvailability]) -> _WriterAvailability:
    """Availability for a disjunction of alternative paths."""
    if any(s == _WriterAvailability.AVAILABLE_NOW for s in states):
        return _WriterAvailability.AVAILABLE_NOW
    if any(s == _WriterAvailability.AFTER_PREREQ for s in states):
        return _WriterAvailability.AFTER_PREREQ
    if any(s == _WriterAvailability.UNKNOWN for s in states):
        return _WriterAvailability.UNKNOWN
    return _WriterAvailability.UNAVAILABLE_FROM_HERE


def _caller_availability(
    rung_node: Any,
    tag: str,
    snapshot: dict[str, Any],
    steerable: frozenset[str],
    current_tags: frozenset[str],
    pdg: ProgramGraph,
    program: Any,
    *,
    _seen: frozenset[str] = frozenset(),
) -> _WriterAvailability:
    """Availability of the subroutine call path for ``rung_node``.

    A body rung with an unconditionally-true local guard is not available when
    its subroutine is only called from a contradictory state.  Treat call sites
    as OR alternatives, and each call site's own guard plus outer call path as
    an AND.
    """
    subroutine = getattr(rung_node, "subroutine", None)
    if not subroutine:
        return _WriterAvailability.AVAILABLE_NOW
    if subroutine in _seen:
        return _WriterAvailability.UNKNOWN

    states: list[_WriterAvailability] = []
    for caller in pdg.rung_nodes:
        if subroutine not in caller.calls:
            continue
        call_ro = resolve_rung(program, caller)
        if call_ro is None:
            states.append(_WriterAvailability.UNKNOWN)
            continue
        call_sp = call_ro.sp_tree()
        gate = (
            _WriterAvailability.AVAILABLE_NOW
            if call_sp is None
            else _expr_availability(
                _sp_to_expr(call_sp), snapshot, steerable, current_tags, pdg, program
            )
        )
        outer = _caller_availability(
            caller,
            tag,
            snapshot,
            steerable,
            current_tags,
            pdg,
            program,
            _seen=_seen | {subroutine},
        )
        states.append(max(gate, outer))

    if not states:
        return _WriterAvailability.UNKNOWN
    return _or_availability(states)


def _writer_availability(
    ro: Any,
    rung_node: Any,
    wv: Any,
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    is_counterfactual: bool,
) -> _WriterAvailability:
    """State-indexed availability for a candidate writer."""
    if is_counterfactual:
        return _WriterAvailability.UNAVAILABLE_FROM_HERE

    current_tags = frozenset((tag,)) | opaque_loop
    availability = _caller_availability(
        rung_node, tag, snapshot, steerable, current_tags, pdg, program
    )
    if isinstance(wv, Affine) and wv.source == tag:
        src_val = _invert_affine(wv, value)
        if src_val is None:
            return _WriterAvailability.UNAVAILABLE_FROM_HERE
        if not _values_match(snapshot.get(tag), src_val):
            availability = _WriterAvailability.AFTER_PREREQ

    sp = ro.sp_tree()
    if sp is None:
        return availability
    guard_expr = _sp_to_expr(sp)
    guard_availability = _expr_availability(
        guard_expr, snapshot, steerable, current_tags, pdg, program
    )
    return max(availability, guard_availability)


def _reduce_guard_by_pin(
    guard_expr: Any, src_tag: str, src_val: Any, snapshot: dict[str, Any]
) -> Any:
    """Reduce a copy writer's guard by its own source pin.

    A copy ``copy(src, dst)`` forces ``src == src_val`` to produce the target, so a
    guard conjunct that constrains *only* ``src`` is fully decided by that pin:

    - the pin **violates** it (``UnitModeCmd != 0`` beside source ``== 0``) → the
      writer can never emit this value; return :data:`_GUARD_CONTRADICTION` so the
      caller drops the writer (producibility);
    - the pin **satisfies** it (``UnitModeCmd != 0`` beside source ``== 2``) → it is
      redundant; drop it so it does not surface as a second, conflicting frontier
      on ``src`` (which would fight the source pin).

    Conjuncts on other tags are left untouched for normal tracing.  Returns the
    reduced expression, ``None`` when nothing remains, or the contradiction
    sentinel.  Only source-*only* conjuncts are decided; a multi-tag conjunct
    (whose other operands may yet be steered) is never dropped or rejected.
    """
    overlay = {**snapshot, src_tag: src_val}
    terms = list(guard_expr.terms) if isinstance(guard_expr, And) else [guard_expr]
    kept: list[Any] = []
    for term in terms:
        if _simplified_expr_tags(term) == {src_tag}:
            decided = _eval_expr_from_state(term, overlay)
            if decided is False:
                return _GUARD_CONTRADICTION
            if decided is True:
                continue  # satisfied by the pin — redundant, drop
        kept.append(term)
    if not kept:
        return None
    if len(kept) == 1:
        return kept[0]
    return And(terms=tuple(kept))


def _is_self_gated(rn: Any, pdg: ProgramGraph, tag: str) -> bool:
    """A writer is self-gated if its condition or call-gate reads the tag.

    ``with rung(State == 1): copy(1, State)`` is a hold/latch — it can
    never *cause* a transition, only sustain one.  The trace should prefer
    transition writers that can actually move the tag to the target value.
    """
    if tag in rn.condition_reads:
        return True
    if rn.subroutine:
        for cn in pdg.rung_nodes:
            if rn.subroutine in cn.calls and tag in cn.condition_reads:
                return True
    return False


def _rank_writers(
    writers: frozenset[int],
    pdg: ProgramGraph,
    program: Any,
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    opaque_loop: frozenset[str] = frozenset(),
    clear_only: frozenset[str] = frozenset(),
    *,
    steerable: frozenset[str] = frozenset(),
    ancestry: tuple[tuple[str, Any], ...] = (),
) -> list[int]:
    """Rank viable writers by current-state availability, then writer role.

    - Prevents dead-ending on a latch writer (``if State == 1: copy(1, State)``)
      when a transition writer (``copy(C_UnitMode, State)``) exists.
    - Prevents selecting a *counterfactual* writer — one whose guard, evaluated in
      the projected prerequisite state, has a false leaf on a pinned tag.  Covers
      both the one-hot case (``copy(1, S_StateCompleteBool)`` under ``S_Clearing``
      while we hold ``S_Starting``) and the self-referential affine step counter
      (the even-step rung gated ``valstepisodd != 1`` cannot produce
      ``CurStep == 2``, because at the source state ``CurStep == 1`` the parity is
      odd; the transition rung gated ``Trans == 1`` stays live).  See
      ``_writer_projection``.
    - Demotes a *maintenance* writer — a literal init/reset rung fireable only by
      pressing a clear-only (ack-cleared momentary) lever off the natural path
      (``fill(1, CurStep)`` gated ``Or(xInit, xReset)``).  Its guard is not
      consistent with the state the plan drives toward; a self-advancing value-step
      writer whose guard the pipeline establishes (``CurStep+1`` under the caller
      gate) is the state-consistent choice, so the maintenance writer ranks below
      it — kept as a fallback, never the default route.
    """
    pinned_overlay = {t: snapshot.get(t) for t in opaque_loop}
    pinned = frozenset(opaque_loop)
    ranked: list[tuple[_WriterAvailability, int, int]] = []
    prior_same_tag_values = tuple(v for t, v in ancestry if t == tag)
    for ri in sorted(writers):
        rn = pdg.rung_nodes[ri]
        ro = resolve_rung(program, rn)
        if ro is None:
            continue
        wv = _written_value_for_tag(ro, tag)
        if not _can_produce(wv, value):
            continue
        proj = _writer_projection(ro, tag, value, snapshot, pdg, program, pinned_overlay, pinned)
        is_counterfactual = proj is not None and proj[0]
        availability = _writer_availability(
            ro,
            rn,
            wv,
            tag,
            value,
            snapshot,
            pdg,
            program,
            steerable,
            opaque_loop,
            is_counterfactual,
        )
        if isinstance(wv, Affine) and wv.source == tag:
            src_val = _invert_affine(wv, value)
            if src_val is not None and any(
                _values_match(src_val, prior) for prior in prior_same_tag_values
            ):
                availability = _WriterAvailability.UNAVAILABLE_FROM_HERE

        bucket = 1  # ordinary non-literal / affine writer
        if isinstance(wv, Literal) and _values_match(wv.value, value):
            if _is_self_gated(rn, pdg, tag):
                bucket = 4
            elif is_counterfactual:
                bucket = 3
            elif proj is not None and any(t in clear_only for t in proj[1]):
                # Fireable only by pressing a clear-only maintenance lever off the
                # natural path — rank below any self-advancing value-step writer.
                bucket = 2
            else:
                bucket = 0
        else:
            csb = copy_source_binding(ro, tag, value)
            if csb is not None:
                src_tag, src_val = csb
                if _values_match(snapshot.get(src_tag), src_val):
                    bucket = 0
            if is_counterfactual:
                bucket = 3

        ranked.append((availability, bucket, ri))
    return [ri for _availability, _bucket, ri in sorted(ranked)]


# ---------------------------------------------------------------------------
# Copied from walk/priors.py — zero walk-specific dependencies
# ---------------------------------------------------------------------------


def _rung_unconditional(rung_node: Any, pdg: ProgramGraph, program: Any, _depth: int = 0) -> bool:
    """Whether *rung_node* fires on every scan — no gate, no upstream return_early,
    and (if in a subroutine) reached only through unconditional callers."""
    if rung_node.condition_reads or _return_early_guard_exprs(program, rung_node):
        return False
    if rung_node.subroutine is None:
        return True
    if _depth > 3:
        return False  # give up conservatively on deep call chains
    callers = [cn for cn in pdg.rung_nodes if rung_node.subroutine in cn.calls]
    return bool(callers) and all(
        _rung_unconditional(cn, pdg, program, _depth + 1) for cn in callers
    )


def _literal_write(ro: Any, tag: str) -> Any | None:
    """The literal value rung *ro* writes to *tag*, or ``None``."""
    from pyrung.core.instruction.coils import LatchInstruction, ResetInstruction
    from pyrung.core.instruction.data_transfer import CopyInstruction, FillInstruction

    for instr in ro._instructions:
        target = getattr(instr, "target", None)
        if target is None:
            target = getattr(instr, "dest", None)
        if target is None:
            continue
        name = getattr(target, "name", None)
        if name is not None:
            names = {name}
        elif hasattr(target, "tags"):
            names = {getattr(t, "name", None) for t in target.tags()}
        else:
            continue
        if tag not in names:
            continue
        if isinstance(instr, ResetInstruction):
            return False
        if isinstance(instr, LatchInstruction):
            return True
        if isinstance(instr, CopyInstruction):
            src = instr.source
            if hasattr(src, "name"):
                return getattr(src, "default", None) if getattr(src, "readonly", False) else None
            return src if isinstance(src, (bool, int, float, str)) else None
        if isinstance(instr, FillInstruction):
            val = instr.value
            if hasattr(val, "name"):
                return None
            return val if isinstance(val, (bool, int, float, str)) else None
        return None
    return None


def _scan_transient_rest(
    tag: str,
    pdg: ProgramGraph,
    program: Any,
    known: dict[str, Any] | None = None,
) -> tuple[bool, Any]:
    """Whether *tag* provably rests at one value at every scan boundary."""
    from pyrung.core.analysis.simplified import Atom, Or

    del known
    if pdg.tag_roles.get(tag) == TagRole.INPUT:
        return False, None

    writer_idxs = pdg.writers_of.get(tag, frozenset())
    if not writer_idxs:
        return False, None
    writes: list[tuple[Any, Any, Any]] = []
    for ri in writer_idxs:
        rung_node = pdg.rung_nodes[ri]
        ro = resolve_rung(program, rung_node)
        if ro is None:
            return False, None
        if tag in rung_node.ote_writes:
            return False, None
        lw = _literal_write(ro, tag)
        if lw is None:
            return False, None
        writes.append((rung_node, ro, lw))

    candidate_rests: list[Any] = []
    for _n, _r, v in writes:
        if not any(_values_match(v, c) for c in candidate_rests):
            candidate_rests.append(v)

    def _main_exec_pos(node: Any) -> int | None:
        """*node*'s position in the flattened per-scan execution order, as a
        main-program rung index.  A main-scope node is its own ``rung_index``; a
        subroutine node runs at its call site — resolvable to a single position
        only when that subroutine has exactly one main call site.  ``None`` means
        the node cannot be placed in a single scan order (multiple / zero call
        sites, or a subroutine called only from another subroutine)."""
        if node.subroutine is None:
            return node.rung_index
        sites = pdg.call_site_rung_indices().get(node.subroutine, frozenset())
        return next(iter(sites)) if len(sites) == 1 else None

    for rest in candidate_rests:
        producers = [(n, v) for n, _r, v in writes if not _values_match(v, rest)]
        clearers = [(n, r) for n, r, v in writes if _values_match(v, rest)]
        if not producers or not clearers:
            continue
        prod_scopes = {n.subroutine for n, _v in producers}
        produced_vals = [v for _n, v in producers]

        def _fires_when_set(e: Any, produced: tuple[Any, ...] = tuple(produced_vals)) -> bool:
            if isinstance(e, Atom):
                if e.tag != tag:
                    return False
                if e.form in ("xic", "truthy"):
                    return all(bool(v) for v in produced)
                return e.form == "eq" and all(_values_match(e.operand, v) for v in produced)
            if isinstance(e, Or):
                return any(_fires_when_set(term, produced) for term in e.terms)
            return False

        if len(prod_scopes) == 1:
            pscope = next(iter(prod_scopes))
            last_producer = max(n.rung_index for n, _v in producers)
            for c_node, c_ro in clearers:
                sp = c_ro.sp_tree()
                if sp is not None and not _fires_when_set(_sp_to_expr(sp)):
                    continue
                if c_node.subroutine == pscope:
                    if c_node.rung_index > last_producer:
                        return True, rest
                    continue
                if c_node.subroutine is None:
                    continue
                for cnode in pdg.rung_nodes:
                    if (
                        c_node.subroutine in cnode.calls
                        and cnode.subroutine == pscope
                        and cnode.rung_index > last_producer
                    ):
                        cro = resolve_rung(program, cnode)
                        if cro is None:
                            continue
                        csp = cro.sp_tree()
                        if csp is not None and _fires_when_set(_sp_to_expr(csp)):
                            return True, rest
            continue

        # Multi-scope producers: rest is provable only when the producers can be
        # linearized in the per-scan execution order *and* a self-gated main-scope
        # clearer runs strictly after all of them — then it clears whatever any
        # producer set, every scan.  Each producer is placed at its main-program
        # execution position (its call site, for a subroutine producer); if any
        # cannot be placed (a producer scope with multiple/zero main call sites)
        # the ordering is ambiguous and we punt.  The clearer is required to be in
        # the main scope so it provably executes after cross-scope producers; a
        # clearer nested in a subroutine, or sitting between producer call sites,
        # is not proven and falls through to the punt — never a fabricated rest.
        prod_positions = [_main_exec_pos(n) for n, _v in producers]
        if any(p is None for p in prod_positions):
            continue
        last_prod_pos = max(p for p in prod_positions if p is not None)
        for c_node, c_ro in clearers:
            if c_node.subroutine is not None:
                continue
            sp = c_ro.sp_tree()
            if sp is not None and not _fires_when_set(_sp_to_expr(sp)):
                continue
            if c_node.rung_index > last_prod_pos:
                return True, rest
    return False, None
