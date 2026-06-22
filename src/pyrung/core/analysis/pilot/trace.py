"""Backward trace engine and steerable-input detection for PILOT."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import TagRole, resolve_rung
from pyrung.core.analysis.prove.expr import _eval_expr_from_state
from pyrung.core.analysis.simplified import And, Atom, Or, _negate, _sp_to_expr
from pyrung.core.analysis.sp_values import (
    _values_match,
    _written_value_for_tag,
    copy_source_binding,
)
from pyrung.core.crossing import Affine, Literal

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph


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
        actions: list[tuple[str, Any]] = []
        seen: set[tuple[str, Any]] = set()
        self._collect_ordered(actions, seen)
        return actions

    def _collect_ordered(self, out: list[tuple[str, Any]], seen: set[tuple[str, Any]]) -> None:
        for child in self.children:
            child._collect_ordered(out, seen)
        if self.is_steerable:
            key = (self.tag, self.value)
            if key not in seen:
                seen.add(key)
                out.append(key)

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
        if not self.satisfied and not self.is_steerable and self.children:
            out.add(self.tag)
        for child in self.children:
            child._collect_pivots(out)

    def unsatisfied_count(self) -> int:
        """Number of unsatisfied, non-steerable conditions in the tree.

        This is the "distance to target" — fewer = closer. An action
        that increases this count moved us further from the goal.
        """
        count = 0
        if not self.satisfied and not self.is_steerable and self.children:
            count = 1
        for child in self.children:
            count += child.unsatisfied_count()
        return count


# ---------------------------------------------------------------------------
# trace_back — recursive backward trace
# ---------------------------------------------------------------------------


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


def _expr_satisfied(expr: Any, snapshot: dict[str, Any]) -> bool:
    """Whether *expr* is definitely satisfied in *snapshot*.

    Delegates to the prover's ``_eval_expr_from_state`` which returns
    ``None`` for undecidable terms (rise/fall, missing tags).  Treat
    ``None`` as not-satisfied — conservative for backward tracing.
    """
    return _eval_expr_from_state(expr, snapshot) is True


def _trace_expression(
    expr: Any,
    self_tag: str,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    *,
    max_depth: int,
    _visited: set[tuple[str, Any]],
    _depth: int,
) -> list[TraceNode]:
    """Walk an expression tree, returning trace children.

    And: trace all terms (all must be satisfied).
    Or: if any branch is already satisfied, skip. Otherwise pick the
        best unsatisfied branch (fewest non-steerable unsatisfied nodes).
    Atom: convert to (tag, value) and recurse via trace_back.
    """
    if isinstance(expr, And):
        children: list[TraceNode] = []
        for term in expr.terms:
            children.extend(
                _trace_expression(
                    term,
                    self_tag,
                    snapshot,
                    pdg,
                    program,
                    steerable,
                    max_depth=max_depth,
                    _visited=_visited,
                    _depth=_depth,
                )
            )
        return children

    if isinstance(expr, Or):
        # Any satisfied branch means the Or doesn't block — skip it.
        if _expr_satisfied(expr, snapshot):
            return []

        # Pick the cheapest unsatisfied branch. Skip self-referencing
        # branches — Or(rise(Input), SealIn) where SealIn is the tag
        # we're already tracing (the engineer knows the seal-in path
        # is circular and looks at the trigger instead).
        best: list[TraceNode] | None = None
        best_score: float = float("inf")
        for term in expr.terms:
            if isinstance(term, Atom) and term.tag == self_tag:
                continue
            candidate = _trace_expression(
                term,
                self_tag,
                snapshot,
                pdg,
                program,
                steerable,
                max_depth=max_depth,
                _visited=set(_visited),
                _depth=_depth,
            )
            if not candidate:
                return []
            score = sum(1 for c in candidate if not c.satisfied and not c.is_steerable)
            if score < best_score:
                best_score = score
                best = candidate
        return best if best is not None else []

    if isinstance(expr, Atom):
        target = _atom_target(expr)
        if target is None:
            return []
        tag, val = target
        # Rise/fall need a transition — if the tag is already at the
        # target value, the edge won't fire.  Mark it steerable so
        # PILOT knows to re-pulse it.
        if (
            expr.form in ("rise", "fall")
            and tag in steerable
            and _values_match(snapshot.get(tag), val)
        ):
            return [TraceNode(tag=tag, value=val, is_steerable=True)]
        child = trace_back(
            tag,
            val,
            snapshot,
            pdg,
            program,
            steerable,
            max_depth=max_depth,
            _visited=_visited,
            _depth=_depth + 1,
        )
        return [child]

    return []


def trace_back(
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    *,
    max_depth: int = 15,
    _visited: set[tuple[str, Any]] | None = None,
    _depth: int = 0,
) -> TraceNode:
    """Recursive backward trace from ``(tag, value)``.

    Returns a ``TraceNode`` tree.  Leaves are steerable inputs (actions),
    already-satisfied conditions, or cycle/depth terminations.

    Uses ``(tag, value)`` visited keys so the same tag at different values
    (e.g. ``StateCurrent==1`` then ``StateCurrent==2``) can be traced
    independently.
    """
    if _visited is None:
        _visited = set()

    vkey = _visit_key(tag, value)

    if _values_match(snapshot.get(tag), value):
        return TraceNode(tag=tag, value=value, satisfied=True)

    if vkey in _visited:
        return TraceNode(tag=tag, value=value)

    _visited.add(vkey)

    if tag in steerable:
        return TraceNode(tag=tag, value=value, is_steerable=True)

    if pdg.tag_roles.get(tag) == TagRole.INPUT:
        return TraceNode(tag=tag, value=value)

    writers = pdg.writers_of.get(tag, frozenset())
    if not writers:
        return TraceNode(tag=tag, value=value)

    node = TraceNode(tag=tag, value=value)

    for ri in sorted(writers):
        rung_node = pdg.rung_nodes[ri]
        ro = resolve_rung(program, rung_node)
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
                    expr,
                    tag,
                    snapshot,
                    pdg,
                    program,
                    steerable,
                    max_depth=max_depth,
                    _visited=_visited,
                    _depth=_depth,
                )
            )

        if rung_node.subroutine:
            for cn in pdg.rung_nodes:
                if rung_node.subroutine in cn.calls:
                    call_ro = resolve_rung(program, cn)
                    if call_ro is None:
                        continue
                    call_sp = call_ro.sp_tree()
                    if call_sp is not None:
                        node.children.extend(
                            _trace_expression(
                                _sp_to_expr(call_sp),
                                tag,
                                snapshot,
                                pdg,
                                program,
                                steerable,
                                max_depth=max_depth,
                                _visited=_visited,
                                _depth=_depth + 1,
                            )
                        )

        csb = copy_source_binding(ro, tag, value)
        if csb is not None:
            src_tag, src_val = csb
            child = trace_back(
                src_tag,
                src_val,
                snapshot,
                pdg,
                program,
                steerable,
                max_depth=max_depth,
                _visited=_visited,
                _depth=_depth + 1,
            )
            child.data_flow = "copy"
            node.children.append(child)

        if isinstance(wv, Affine) and wv.source != tag:
            src_val = _invert_affine(wv, value)
            if src_val is not None:
                child = trace_back(
                    wv.source,
                    src_val,
                    snapshot,
                    pdg,
                    program,
                    steerable,
                    max_depth=max_depth,
                    _visited=_visited,
                    _depth=_depth + 1,
                )
                child.data_flow = "calc"
                node.children.append(child)

        break  # use first viable writer

    return node


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


def _visit_key(tag: str, value: Any) -> tuple[str, Any]:
    if isinstance(value, (bool, int, float, str, type(None))):
        return (tag, value)
    return (tag, id(value))


def _can_produce(wv: Any, value: Any) -> bool:
    if isinstance(wv, Literal):
        return _values_match(wv.value, value)
    if isinstance(wv, Affine):
        return True
    return True  # UNKNOWN — assume it could


def _invert_affine(wv: Affine, value: Any) -> Any | None:
    try:
        if wv.scale == 0:
            return None
        src_val = (value - wv.offset) / wv.scale
        if isinstance(value, int) and isinstance(wv.offset, (int, float)):
            src_val_int = int(src_val)
            if float(src_val) == src_val_int:
                return src_val_int
            return None
        return src_val
    except (TypeError, ValueError, ZeroDivisionError):
        return None


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
