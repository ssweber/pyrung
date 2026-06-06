"""Corridor walker for how().

A sequential-simulation planner that runs on the PLC runner instead of the
BFS infrastructure.

The principle: the target reduces to driving one **governing** stateful tag
to a value.  The walker discovers that tag's value-transition graph by
*interpreted simulation* — from a state at value ``from``, it applies a
candidate *steer* on a forked runner and observes the value ``to`` that
results.  Every edge is therefore something the real interpreter produced
(sound by construction; immune to copy/calc/indirect-addressing blindness
that defeats static writer inversion).

Static analysis is only a **prior**, never correctness-bearing:
  1. it picks the governing tag (a derived coil delegates to the state tag
     that gates it),
  2. it narrows the steer alphabet to the governing tag's input cone, and
  3. it sets the search horizon (short for command machines, long when a
     timer/counter gates the tag — a held wait advances time to the crossing,
     so "advance time" is just an empty steer with a longer horizon).

Best-first search runs over the governing tag's value space (tiny — mode
values or a counter's range), not the full state space, so it stays "mostly
no search".  Anything the walker cannot reach returns ``None`` and the caller
falls back to the existing waypoint/BFS planner.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pyrung.core.analysis.graph import Path
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.runner import PLC

# Horizon (scans) for an empty steer to produce a value change.
_HORIZON_SHORT = 6  # command machines: auto-completes settle in 1-2 scans
_HORIZON_LONG = 1500  # timer/counter-gated: tick to the crossing (early-outs)
# Caps on the interpreted value-graph search.
_MAX_NODES = 64
_MAX_CORRIDOR = 40


@dataclass(frozen=True)
class _Steer:
    """A candidate move: empty (just step) or pulse an input edge high."""

    kind: str  # "empty" | "pulse"
    input: str | None = None


# A realized action step: ``patch(action)`` then ``scans`` steps.
_Action = tuple[dict[str, Any], int]


# ---------------------------------------------------------------------------
# Target extraction
# ---------------------------------------------------------------------------


def _target_tag_value(expr: Any) -> tuple[str, Any] | None:
    """Reduce a target expression to a single ``(tag, value)`` goal."""
    from pyrung.core.analysis.simplified import Atom

    if isinstance(expr, Atom):
        if expr.form == "eq":
            return (expr.tag, expr.operand)
        if expr.form == "xic":
            return (expr.tag, True)
        if expr.form == "xio":
            return (expr.tag, False)
    return None


# ---------------------------------------------------------------------------
# Static priors: governing tag, steer alphabet, horizon
# ---------------------------------------------------------------------------


def _copy_source(tag: str, pdg: ProgramGraph, program: Any) -> str | None:
    """Return ``U`` when *tag* is written ``copy(U, tag)`` (copy-from-tag)."""
    from pyrung.core.analysis.prove.waypoints import _resolve_rung, _written_value_for_tag

    for ri in pdg.writers_of.get(tag, frozenset()):
        ro = _resolve_rung(program, pdg.rung_nodes[ri])
        if ro is None:
            continue
        wv = _written_value_for_tag(ro, tag)
        if wv is not None and wv[0] == "tag":
            return wv[1]
    return None


def _value_richness(tag: str, pdg: ProgramGraph, program: Any) -> int:
    """How many distinct values *tag* plausibly steps through.

    Counts distinct literal write values of *tag* and (when copy-coupled) of
    its copy source; an arithmetic (counter) writer counts as rich.  Used to
    decide whether *tag* is itself the governing corridor tag or merely a
    derived view of one.
    """
    from pyrung.core.analysis.prove.waypoints import (
        _has_arithmetic_writer,
        _resolve_rung,
        _written_value_for_tag,
    )

    if _has_arithmetic_writer(tag, pdg, program):
        return 99
    values: set[Any] = set()
    sources = [tag]
    src = _copy_source(tag, pdg, program)
    if src is not None:
        sources.append(src)
        if _has_arithmetic_writer(src, pdg, program):
            return 99
    for s in sources:
        for ri in pdg.writers_of.get(s, frozenset()):
            ro = _resolve_rung(program, pdg.rung_nodes[ri])
            if ro is None:
                continue
            wv = _written_value_for_tag(ro, s)
            if wv is not None and wv[0] == "literal":
                values.add(wv[1])
    return len(values)


def _governing(
    target_tag: str,
    target_value: Any,
    pdg: ProgramGraph,
    program: Any,
) -> tuple[str, Any]:
    """Pick the governing tag/value for the corridor.

    If *target_tag* steps through multiple values, it governs itself.
    Otherwise it is a derived view (e.g. an ``out`` coil); delegate to the
    richest stateful tag that gates the writer producing *target_value*.
    """
    from pyrung.core.analysis.pdg import TagRole
    from pyrung.core.analysis.prove.waypoints import (
        _extract_condition_values,
        _resolve_rung,
        _written_value_for_tag,
    )
    from pyrung.core.analysis.simplified import _sp_to_expr

    if _value_richness(target_tag, pdg, program) >= 2:
        return target_tag, target_value

    best: tuple[str, Any] | None = None
    best_rich = 1
    for ri in pdg.writers_of.get(target_tag, frozenset()):
        ro = _resolve_rung(program, pdg.rung_nodes[ri])
        if ro is None:
            continue
        wv = _written_value_for_tag(ro, target_tag)
        if wv is not None and wv[0] == "literal" and wv[1] != target_value:
            continue
        sp = ro.sp_tree()
        if sp is None:
            continue
        for gt, gvals in _extract_condition_values(_sp_to_expr(sp)).items():
            if gt == target_tag or pdg.tag_roles.get(gt) == TagRole.INPUT:
                continue
            rich = _value_richness(gt, pdg, program)
            if rich > best_rich:
                best = (gt, next(iter(gvals)))
                best_rich = rich
    return best if best is not None else (target_tag, target_value)


def _external_bool_inputs(pdg: ProgramGraph, known: dict[str, Any]) -> list[str]:
    from pyrung.core.analysis.pdg import TagRole
    from pyrung.core.tag import TagType

    out: list[str] = []
    for tag, role in pdg.tag_roles.items():
        if role != TagRole.INPUT:
            continue
        t = known.get(tag)
        if t is not None and t.type is TagType.BOOL:
            out.append(tag)
    return sorted(out)


def _edge_tags(pdg: ProgramGraph, program: Any) -> set[str]:
    """Tag names read through ``rise()``/``fall()`` anywhere in the program."""
    from pyrung.core.analysis.prove.waypoints import _resolve_rung
    from pyrung.core.analysis.simplified import And, Atom, Or, _sp_to_expr

    result: set[str] = set()

    def visit(e: Any) -> None:
        if isinstance(e, Atom):
            if e.form in ("rise", "fall"):
                result.add(e.tag)
        elif isinstance(e, (And, Or)):
            for term in e.terms:
                visit(term)

    seen: set[int] = set()
    for node in pdg.rung_nodes:
        ro = _resolve_rung(program, node)
        if ro is None or id(ro) in seen:
            continue
        seen.add(id(ro))
        sp = ro.sp_tree()
        if sp is not None:
            visit(_sp_to_expr(sp))
    return result


def _steer_alphabet(
    governing: str,
    pdg: ProgramGraph,
    known: dict[str, Any],
) -> list[_Steer]:
    """Empty plus a pulse for each external Bool input in the governing cone.

    The cone narrows branching; when static cone tracing finds nothing (e.g.
    indirect addressing) it falls back to every external Bool input.
    """
    ext = _external_bool_inputs(pdg, known)
    cone = pdg.upstream_slice(governing)
    cone_inputs = [c for c in ext if c in cone]
    candidates = cone_inputs if len(cone_inputs) >= 1 else ext
    return [_Steer("empty")] + [_Steer("pulse", c) for c in candidates]


def _timer_done_bits(pdg: ProgramGraph, program: Any) -> set[str]:
    """Done-bit tag names of all timer/counter instructions in the program."""
    from pyrung.core.analysis.prove.waypoints import _resolve_rung

    bits: set[str] = set()
    seen: set[int] = set()
    for node in pdg.rung_nodes:
        ro = _resolve_rung(program, node)
        if ro is None or id(ro) in seen:
            continue
        seen.add(id(ro))
        for instr in ro._instructions:
            db = getattr(instr, "done_bit", None)
            name = getattr(db, "name", None)
            if name is not None:
                bits.add(name)
    return bits


def _governing_reads(governing: str, pdg: ProgramGraph, program: Any) -> set[str]:
    """Tags read (condition or data) by writers of *governing* and its copy source."""
    reads: set[str] = set()
    tags = [governing]
    src = _copy_source(governing, pdg, program)
    if src is not None:
        tags.append(src)
    for tag in tags:
        for ri in pdg.writers_of.get(tag, frozenset()):
            node = pdg.rung_nodes[ri]
            reads |= set(getattr(node, "condition_reads", ()))
            reads |= set(getattr(node, "data_reads", ()))
    return reads


def _horizon(governing: str, pdg: ProgramGraph, program: Any) -> int:
    """Long horizon when a timer/counter gates the governing tag, else short.

    A timer crossing is reached by *holding inputs and letting time pass*, so
    a timer-gated transition needs a horizon long enough to tick to the
    accumulator preset (the step still early-outs at the crossing).
    Detection reads the governing tag's writer gates directly — ``done`` bits
    are condition reads, which an upstream data-flow slice can miss.
    """
    done_bits = _timer_done_bits(pdg, program)
    if not done_bits:
        return _HORIZON_SHORT
    return (
        _HORIZON_LONG if (_governing_reads(governing, pdg, program) & done_bits) else _HORIZON_SHORT
    )


# ---------------------------------------------------------------------------
# Interpreted steer application
# ---------------------------------------------------------------------------


def _steer_prefix(
    steer: _Steer,
    work_tags: dict[str, Any],
    ext_inputs: list[str],
    edge_ext: set[str],
) -> list[_Action]:
    """Action prefix for *steer*: empty → none; pulse → release then drive high."""
    if steer.kind == "empty" or steer.input is None:
        return []
    release: dict[str, Any] = {c: False for c in ext_inputs if work_tags.get(c)}
    for e in edge_ext:
        if work_tags.get(e):
            release[e] = False
    pulse: dict[str, Any] = {steer.input: True}
    for e in edge_ext:
        pulse[e] = True
    prefix: list[_Action] = []
    if release:
        prefix.append((release, 1))
    prefix.append((pulse, 1))
    return prefix


def _apply_steer(
    runner: PLC,
    steer: _Steer,
    governing: str,
    from_value: Any,
    ext_inputs: list[str],
    edge_ext: set[str],
    horizon: int,
) -> list[_Action] | None:
    """Apply *steer* on *runner* and step until the governing value changes.

    Returns the realized ``(action, scans)`` list (leaving *runner* at the new
    value), or ``None`` if no change occurs within *horizon* scans.
    """
    realized: list[_Action] = []
    for action, scans in _steer_prefix(steer, dict(runner.state.tags), ext_inputs, edge_ext):
        if action:
            runner.patch(action)
        for _ in range(scans):
            runner.step()
        realized.append((action, scans))
        if runner.state.tags.get(governing) != from_value:
            return realized

    auto = 0
    while auto < horizon and runner.state.tags.get(governing) == from_value:
        runner.step()
        auto += 1
    if runner.state.tags.get(governing) == from_value:
        return None
    if auto:
        realized.append(({}, auto))
    return realized


# ---------------------------------------------------------------------------
# Best-first interpreted search over the governing value graph
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    value: Any
    plc: PLC
    path: list[_Action]


def _explore(
    start_plc: PLC,
    governing: str,
    target_value: Any,
    alphabet: list[_Steer],
    ext_inputs: list[str],
    edge_ext: set[str],
    horizon: int,
) -> list[_Action] | None:
    """BFS over governing values; edges discovered by interpreted stepping.

    Returns the realized action sequence reaching *target_value*, or ``None``.
    """
    start_val = start_plc.state.tags.get(governing)
    if start_val == target_value:
        return []
    seen: set[Any] = {start_val}
    frontier: deque[_Node] = deque([_Node(start_val, start_plc.fork(), [])])
    nodes = 0

    while frontier and nodes < _MAX_NODES:
        node = frontier.popleft()
        nodes += 1
        for steer in alphabet:
            trial = node.plc.fork()
            # Only the empty steer needs the long (timer-wait) horizon — a held
            # wait advances timers to their crossing on its own.  Input steers
            # act promptly, so probing them past a few scans only burns time.
            steer_horizon = horizon if steer.kind == "empty" else _HORIZON_SHORT
            realized = _apply_steer(
                trial, steer, governing, node.value, ext_inputs, edge_ext, steer_horizon
            )
            if realized is None:
                continue
            nv = trial.state.tags.get(governing)
            if nv in seen:
                continue
            new_path = node.path + realized
            if nv == target_value:
                return new_path
            if len(new_path) > _MAX_CORRIDOR:
                continue
            seen.add(nv)
            frontier.append(_Node(nv, trial, new_path))
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def plan_walk(plc: PLC, snapshot: dict[str, Any], expr: Any, max_steps: int) -> Path | None:
    """Try to reach the target by walking a governing-tag value corridor.

    Returns a :class:`~pyrung.core.analysis.graph.Path` on success, or
    ``None`` to fall back to the existing planner.
    """
    from pyrung.core.analysis.graph import Path, ReachabilityStep
    from pyrung.core.analysis.pdg import build_program_graph

    program = getattr(plc, "_program", None)
    if program is None:
        return None

    tv = _target_tag_value(expr)
    if tv is None:
        return None
    target_tag, target_value = tv

    known = plc._known_tags_by_name
    tag_defaults = {t.name: t.default for t in known.values()}

    # Resolve a choice name ("IDLE") to its underlying value.
    if isinstance(target_value, str):
        t = known.get(target_tag)
        choices = getattr(t, "choices", None) if t is not None else None
        if choices:
            inv = {name: val for val, name in choices.items()}
            if target_value in inv:
                target_value = inv[target_value]

    if snapshot.get(target_tag) == target_value:
        return Path(
            reachable=True, steps=(), total_changes=0, total_scans=0, tag_defaults=tag_defaults
        )

    pdg = build_program_graph(program)
    governing, gov_value = _governing(target_tag, target_value, pdg, program)

    # The corridor drives `governing` to `gov_value`; if governing is the
    # target tag they coincide.  A derived target (governing != target_tag)
    # is confirmed by replay against the original `expr` value below.
    ext_inputs = _external_bool_inputs(pdg, known)
    edge_ext = _edge_tags(pdg, program) & set(ext_inputs)
    alphabet = _steer_alphabet(governing, pdg, known)
    horizon = _horizon(governing, pdg, program)

    work = plc.fork()
    steps = _explore(work, governing, gov_value, alphabet, ext_inputs, edge_ext, horizon)
    if steps is None or not steps:
        return None
    if len(steps) > max_steps:
        return None

    logger.info("walk: corridor on %s reached %s in %d action(s)", governing, gov_value, len(steps))

    # Verify on a fresh fork against the *original* target.
    verify = plc.fork()
    for action, scans in steps:
        if action:
            verify.patch(action)
        for _ in range(scans):
            verify.step()
    if verify.state.tags.get(target_tag) != target_value:
        logger.info("walk: replay verification failed (target %s)", target_tag)
        return None

    rsteps = [
        ReachabilityStep(action=action, source_key=(), dest_key=(), scans=scans)
        for action, scans in steps
    ]
    from pyrung.core.runner import _count_visible_changes

    total_changes = _count_visible_changes(rsteps, tag_defaults)
    total_scans = sum(scans for _action, scans in steps)
    logger.info("walk: reached %s=%s in %d step(s)", target_tag, target_value, len(rsteps))
    return Path(
        reachable=True,
        steps=tuple(rsteps),
        total_changes=total_changes,
        total_scans=total_scans,
        tag_defaults=tag_defaults,
    )
