"""Backward trace engine for PILOT — the transparent static reader of the compass.

Walks writer conditions / copy / calc backward to steerable inputs
(``trace_back``).  The opaque-but-constant value-graph reader lives in
``compass.py``.  See ``pilot/CLAUDE.md``.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import TagRole, resolve_rung
from pyrung.core.analysis.prove.expr import _eval_expr_from_state
from pyrung.core.analysis.simplified import And, Atom, Or, _negate, _sp_to_expr
from pyrung.core.analysis.sp_values import (
    _FLIP_FORM,
    _chase_inequality_source,
    _expr_tag_names,
    _extract_condition_values,
    _invert_affine,
    _SnapshotView,
    _values_match,
    _writer_projection,
    _written_value_for_tag,
    copy_source_binding,
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


def _env_for(
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    *,
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

    def _collect_ordered(self, out: list[TraceAction], seen: set[tuple[str, Any]]) -> None:
        for child in self.children:
            child._collect_ordered(out, seen)
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


def _resolve_inequality_target(
    atom: Atom,
    snapshot: dict[str, Any],
    prior: DomainPrior | None = None,
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
       ``operand ± 1`` (strict ``gt``/``lt``).  A literal operand on a
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

    if not operand_is_tag:
        return None
    if atom.form in ("ge", "le"):
        return (atom.tag, threshold)
    if (
        atom.form in ("gt", "lt")
        and isinstance(threshold, (int, float))
        and not isinstance(threshold, bool)
    ):
        return (atom.tag, threshold + 1 if atom.form == "gt" else threshold - 1)
    return None


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
        if len(cmps) != 1:
            return [atom]  # unexpected shape (conjunction / Eq / unsat) -> stay honest
        c = cmps[0]
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


def _inequality_levers(
    atom: Atom,
    snapshot: dict[str, Any],
    steerable: frozenset[str],
    pdg: ProgramGraph,
    prior: DomainPrior | None,
    program: Any = None,
) -> list[tuple[str, str, Any]]:
    """Actionable levers for ``A op B``, as ``(label, tag, satisfying_value)``.

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
    so the levers land on steerable inputs the prover dissolved.
    """
    levers: list[tuple[str, str, Any]] = []
    seen: set[str] = set()

    def _actionable(tag: str) -> bool:
        return tag in steerable or bool(pdg.writers_of.get(tag))

    base = (
        _rewrite_internal_compare(atom, steerable, pdg, program, snapshot)
        if program is not None
        else [atom]
    )
    for a in base:
        left = _resolve_inequality_target(a, snapshot, prior)
        if left is not None and left[0] not in seen and _actionable(left[0]):
            levers.append(("left", left[0], left[1]))
            seen.add(left[0])

        operand = a.operand
        if isinstance(operand, str) and a.form in _FLIP_FORM:
            flipped = Atom(tag=operand, form=_FLIP_FORM[a.form], operand=a.tag)
            right = _resolve_inequality_target(flipped, snapshot, prior)
            if right is not None and right[0] not in seen and _actionable(right[0]):
                levers.append(("right", right[0], right[1]))
                seen.add(right[0])

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
    # Level advance: hold the read at the value that makes advance == advance_value.
    for candidate in (True, False):
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


def _equality_gated_coil(
    tag: str, value: Any, pdg: ProgramGraph, program: Any
) -> tuple[str, Any] | None:
    """The discriminator a Bool mode-flag stands for, else ``None``.

    ``out(S_ManualMode)`` under ``rung(S_UnitModeCurrent == 3)`` means
    ``S_ManualMode=True`` is *equivalent to* ``S_UnitModeCurrent=3`` — return
    ``("S_UnitModeCurrent", 3)``.  Only fires for a Bool driven ``True`` by a
    single plain ``out`` whose guard is one equality on one other tag, so the
    rewrite is exact (never widens a flag that has multiple writers or a compound
    gate).  Lets :func:`_route_conflict_tags` catch a caller-gate mode that
    contradicts the mode the body requires, even though they name different tags.
    """
    if value is not True:
        return None
    writers = pdg.writers_of.get(tag, frozenset())
    if len(writers) != 1:
        return None
    node = pdg.rung_nodes[next(iter(writers))]
    if tag not in node.ote_writes:
        return None
    ro = resolve_rung(program, node)
    if ro is None:
        return None
    sp = ro.sp_tree()
    if sp is None:
        return None
    conds = _extract_condition_values(_sp_to_expr(sp))
    if len(conds) != 1:
        return None
    ((other, vals),) = conds.items()
    if other == tag or len(vals) != 1:
        return None
    return (other, next(iter(vals)))


def _route_conflict_tags(tree: TraceNode, pdg: ProgramGraph, program: Any) -> frozenset[str]:
    """Tags *tree* pins to two incompatible values that must hold **together**.

    Every node in a resolved trace tree is a required condition (Or-arms are
    already chosen), so two nodes demanding ``tag=v1`` and ``tag=v2`` clash
    **unless** one is an ancestor of the other — that is temporal sequencing
    (``same_tag_chains``: reach ``v1`` first, then ``v2``), not a simultaneous
    contradiction.  Mode flags are normalized through :func:`_equality_gated_coil`
    so a manual-mode caller gate (``S_ManualMode=True`` → ``S_UnitModeCurrent=3``)
    clashes with a body that needs ``S_UnitModeCurrent=1``.

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
            demand = _equality_gated_coil(node.tag, node.value, pdg, program) or (
                node.tag,
                node.value,
            )
            entries.append((demand[0], demand[1], id(node), anc))
        child_anc = anc | {id(node)}
        for child in node.children:
            walk(child, child_anc)

    walk(tree, frozenset())

    by_tag: dict[str, list[tuple[Any, int, frozenset[int]]]] = {}
    for tag, val, nid, anc in entries:
        by_tag.setdefault(tag, []).append((val, nid, anc))

    conflicts: set[str] = set()
    for tag, pins in by_tag.items():
        distinct: list[Any] = []
        for val, _, _ in pins:
            if not any(_values_match(val, d) for d in distinct):
                distinct.append(val)
        if len(distinct) < 2:
            continue  # single-valued tag — no clash possible
        for i in range(len(pins)):
            vi, ni, ai = pins[i]
            for j in range(i + 1, len(pins)):
                vj, nj, aj = pins[j]
                if _values_match(vi, vj):
                    continue
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
            if expr.form in ("lt", "le", "gt", "ge"):
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
                for label, ltag, lval in levers:
                    child = _trace_back(
                        env,
                        ltag,
                        lval,
                        _visited=set(_visited),
                        _ancestry=_ancestry,
                        _depth=_depth + 1,
                    )
                    if child.is_steerable and not child.provenance:
                        child.provenance = provenance
                    child.lever = label
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
        writers, env.pdg, env.program, tag, value, env.snapshot, env.opaque_loop
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

        node.writer_rung = ri

        sp = ro.sp_tree()
        if sp is not None:
            expr = _sp_to_expr(sp)
            # OTE deactivation: tracing tag=False through out(tag)
            # means the rung must NOT fire — negate the expression.
            if _values_match(value, False) and tag in rung_node.ote_writes:
                expr = _negate(expr)
            node.children.extend(
                _trace_expression(
                    env,
                    expr,
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
                _score, call_children = min(caller_routes, key=lambda item: item[0])
                node.children.extend(call_children)

        csb = copy_source_binding(ro, tag, value)
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

        if isinstance(wv, Affine):
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
    max_choices: int = 16,
) -> tuple[TraceChoice, ...]:
    """Enumerate route choices for an ambiguous Bool-output trace.

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
        pdg.writers_of.get(tag, frozenset()), pdg, program, tag, value, snapshot
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


def compute_steerable(
    pdg: ProgramGraph,
    known: dict[str, Any],
    program: Any,
) -> frozenset[str]:
    """All steerable inputs: INPUT-role tags + ack-cleared Bools.

    Excludes read-only and system tags (``rtc.*``, ``sys.*``).
    """
    from pyrung.core.system_points import READ_ONLY_SYSTEM_TAG_NAMES

    inputs = set(_external_bool_inputs(pdg, known, program))
    for tag, role in pdg.tag_roles.items():
        if role == TagRole.INPUT:
            inputs.add(tag)
    inputs -= READ_ONLY_SYSTEM_TAG_NAMES
    return frozenset(t for t in inputs if not getattr(known.get(t), "readonly", False))


def compute_reference_constants(pdg: ProgramGraph, program: Any) -> frozenset[str]:
    """Never-written copy sources feeding into lookup-table pointer chains.

    Three conditions, all must hold:
    1. Tag has no writers (initial-value only)
    2. Used as a copy/fill source feeding some destination D
    3. D participates in a lookup-table pipeline — either D is a direct
       indirect-copy pointer, or D is the representative of a pointer
       via functional dependency (``calc(D + offset, ptr)``)

    The functional dep collapse is key: ``sm__jump_target_ds_idx =
    S_StateRequested + 150`` means S_StateRequested is the representative
    of the pointer.  So ``copy(sm__STATESTARTINGREF, S_StateRequested)``
    makes sm__STATESTARTINGREF a reference constant — it feeds into the
    lookup-table machinery through the collapsed pointer chain.
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

    return frozenset(n for n in candidates if not pdg.writers_of.get(n, frozenset()))


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
    """
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

    # Determine the index tag and address evaluator.
    if isinstance(src, IndirectRef):
        idx_tag = src.pointer.name
        eval_addr: Any = lambda v: int(v)
    else:
        names = _expr_tag_names(src.expr)
        if not names:
            return None
        mutable = {n for n in names if pdg.writers_of.get(n)}
        if len(mutable) != 1:
            return None
        idx_tag = next(iter(mutable))
        iexpr = src.expr
        itag = idx_tag
        eval_addr = lambda v: int(iexpr.evaluate(_SnapshotView(snapshot, {itag: v})))

    # Hop through calc-defined scratch (e.g. calc(X + 150, idx_tag)).
    for _ in range(3):
        defn = _single_calc_source(idx_tag, pdg, program)
        if defn is None:
            break
        cexpr, hop_src = defn

        def _hopped(
            v: int, _prev: Any = eval_addr, _cexpr: Any = cexpr, _src: str = hop_src
        ) -> int:
            mid = int(_cexpr.evaluate(_SnapshotView(snapshot, {_src: v})))
            return _prev(mid)

        eval_addr = _hopped
        idx_tag = hop_src

    if idx_tag == tag:
        return None

    # Enumerate plausible index values and find which ones produce our target.
    block = src.block
    candidates = _index_values(idx_tag, snapshot, pdg, program)
    inverting: list[Any] = []
    for v in candidates:
        try:
            addr = eval_addr(v)
            block._validate_address(addr)
        except (IndexError, TypeError, ValueError, ZeroDivisionError):
            continue
        slot_name = block._effective_slot_name(addr)
        if slot_name in snapshot:
            slot_val = snapshot[slot_name]
        else:
            _retentive, slot_val = block._effective_slot_policy(addr)
        if _values_match(slot_val, value):
            inverting.append(v)
    if not inverting:
        return None
    return idx_tag, inverting


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
) -> list[int]:
    """Rank viable writers: state-consistent first, counterfactual late, latches last.

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
    """
    pinned_overlay = {t: snapshot.get(t) for t in opaque_loop}
    pinned = frozenset(opaque_loop)
    preferred: list[int] = []
    counterfactual: list[int] = []
    rest: list[int] = []
    latches: list[int] = []
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
        if isinstance(wv, Literal) and _values_match(wv.value, value):
            if _is_self_gated(rn, pdg, tag):
                latches.append(ri)
            elif is_counterfactual:
                counterfactual.append(ri)
            else:
                preferred.append(ri)
            continue
        csb = copy_source_binding(ro, tag, value)
        if csb is not None:
            src_tag, src_val = csb
            if _values_match(snapshot.get(src_tag), src_val):
                preferred.append(ri)
                continue
        if is_counterfactual:
            counterfactual.append(ri)
        else:
            rest.append(ri)
    return [*preferred, *rest, *counterfactual, *latches]


# ---------------------------------------------------------------------------
# Copied from walk/priors.py — zero walk-specific dependencies
# ---------------------------------------------------------------------------


def _external_bool_inputs(
    pdg: ProgramGraph,
    known: dict[str, Any],
    program: Any | None = None,
) -> list[str]:
    """External Bool inputs: never-written + ack-cleared Bools."""
    from pyrung.core.tag import TagType

    out: list[str] = []
    for tag, role in pdg.tag_roles.items():
        if role != TagRole.INPUT:
            continue
        t = known.get(tag)
        if t is not None and t.type is TagType.BOOL:
            out.append(tag)
    if program is not None:
        out.extend(_ack_cleared_bool_inputs(pdg, known, program))
    return sorted(out)


def _ack_cleared_bool_inputs(
    pdg: ProgramGraph,
    known: dict[str, Any],
    program: Any,
) -> list[str]:
    """Operator-driven Bools the program only ever clears (acknowledge pattern)."""
    from pyrung.core.tag import TagType

    result: list[str] = []
    for tag, t in known.items():
        if getattr(t, "type", None) is not TagType.BOOL:
            continue
        if pdg.tag_roles.get(tag) == TagRole.INPUT:
            continue
        writers = pdg.writers_of.get(tag, frozenset())
        if not writers or not pdg.readers_of.get(tag, frozenset()):
            continue
        default = t.default
        ok = True
        for ri in writers:
            rung_node = pdg.rung_nodes[ri]
            if tag in rung_node.ote_writes:
                ok = False
                break
            ro = resolve_rung(program, rung_node)
            lw = _literal_write(ro, tag) if ro is not None else None
            if lw is None or not _values_match(lw, default):
                ok = False
                break
        if ok:
            result.append(tag)
    return sorted(result)


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

    for rest in candidate_rests:
        producers = [(n, v) for n, _r, v in writes if not _values_match(v, rest)]
        clearers = [(n, r) for n, r, v in writes if _values_match(v, rest)]
        if not producers or not clearers:
            continue
        prod_scopes = {n.subroutine for n, _v in producers}
        if len(prod_scopes) != 1:
            continue
        pscope = next(iter(prod_scopes))
        produced_vals = [v for _n, v in producers]
        last_producer = max(n.rung_index for n, _v in producers)

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
    return False, None
