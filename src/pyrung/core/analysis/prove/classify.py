"""Dimension classification and domain discovery for prove.

Tags partition into three roles:

- **Stateful** — latch/reset, timer/counter, copy, calc; tracked in the
  BFS visited set.
- **Nondeterministic** — external inputs; enumerated at each BFS state.
- **Combinational** — OTE-only writes with no cross-scan readers; ignored.
  A read above the first write (the scan-entry version in the def-use
  chain) is a cross-scan reader.

``run_function`` / ``run_enabled_function`` outputs are classified as
stateful writes, but the function body is opaque.  Domain inference
falls through to tag metadata (``choices=``, ``min=/max=``).  Unannotated
outputs trigger ``_detect_function_escape_hatches`` → ``Intractable``.

Domain inference stack (most to least specific):

1. Bool → ``{False, True}``
2. ``choices=`` metadata → explicit finite set
3. ``min=`` / ``max=`` metadata → integer range (Int/Dint/Word; capped at 1000)
   or partition seed (Real with non-integer bounds)
4. Literal-write mining (``_collect_literal_write_domains``) → values
   from ``copy(literal, tag)``
5. Structural propagation (``_collect_structural_domains``) → fixed-point
   over write graph
6. Expression partition (``_extract_value_domain``) → comparison literals
   ± 1 + tag default
7. eq/ne enum closure → ``{literals..., OTHER}`` for tags only tested for
   equality

A nondeterministic input compared with another changing value (an
accumulator, a ``calc`` result, another input, an expression, an indirect
element, a ``search()``), directly or through data flow, skips 4-7: no
partition covers it, so it gets every value (``_tag_compared_names``,
``_every_value_domain``), or is refused.  Compared with a tag whose every
value is known (``_exact_value_sets``), it keeps the partition with those
values among its split points.

No domain → ``Intractable`` with hints.
"""

from __future__ import annotations

import itertools
import math
import os
import sys
import time
from collections import ChainMap
from typing import TYPE_CHECKING, Any, NamedTuple

from pyrung.core.analysis.affine import (
    extract_forward_affine as _extract_forward_affine,
)
from pyrung.core.analysis.affine import (
    extract_forward_offset as _extract_forward_offset,
)
from pyrung.core.analysis.pdg import TagRole, build_program_graph
from pyrung.core.analysis.return_guards import return_early_guard_exprs
from pyrung.core.analysis.reverse_edges import (
    IDENTITY as _IDENTITY,
)
from pyrung.core.analysis.reverse_edges import (
    InvertFn as _InvertFn,
)
from pyrung.core.analysis.reverse_edges import (
    calc_reverse_edge as _calc_reverse_edge,
)
from pyrung.core.analysis.reverse_edges import (
    compose_invert as _compose_invert,
)
from pyrung.core.analysis.reverse_edges import (
    literal_value_from_value as _literal_value_from_value,
)
from pyrung.core.analysis.reverse_edges import (
    tag_name_from_value as _tag_name_from_value,
)
from pyrung.core.analysis.simplified import Expr, _condition_to_expr, simplified_forms
from pyrung.core.expression import is_randint_tag, randint_sites
from pyrung.core.kernel import CompiledKernel
from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME
from pyrung.core.tag import TagType

from .absorb import (
    _DONE_KIND_COUNT_DOWN,
    _DONE_KIND_COUNT_UP,
    _DONE_KIND_OFF_DELAY,
    _DONE_KIND_ON_DELAY,
    _DONE_KIND_TIME_DRUM,
    _PROGRESS_KIND_INT_DOWN,
    _PROGRESS_KIND_INT_UP,
    _PROGRESS_KIND_REAL_DOWN,
    _PROGRESS_KIND_REAL_UP,
    _all_write_targets,
    _collect_done_acc_pairs,
    _collect_progress_source_kinds,
    _drum_times_under_its_flag,
    _find_comparison_absorptions,
    _find_redundant_acc_absorptions,
    _find_threshold_absorptions,
    _has_forbidden_data_read,
    _merge_threshold_absorptions,
    _owner_reset_condition_refs,
    _RedundantAccAbsorptions,
    _threshold_atom_for_progress,
    _ThresholdAbsorptions,
    _ThresholdBlocker,
)
from .expr import _build_atom_index, _collect_atoms_for_tag, _referenced_tags
from .kernel import _restore_kernel, _snapshot_kernel, _step_compiled_kernel
from .results import PENDING, Intractable
from .system_points import (
    FAULT_FLAGS,
    FIRST_SCAN,
    FIRST_SCAN_DEPENDENTS,
    SCAN_START_RESETS,
    bounded_system_tag,
    clip_to_range,
    free_system_domain,
    free_system_tags,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from pyrung.core.analysis.pdg import ProgramGraph, RungNode
    from pyrung.core.analysis.simplified import Atom
    from pyrung.core.program import Program
    from pyrung.core.tag import Tag

    from . import _ExploreContext

_OTE_INSTRUCTION = "OutInstruction"

_STATEFUL_INSTRUCTIONS = frozenset(
    {
        "LatchInstruction",
        "ResetInstruction",
        "OnDelayInstruction",
        "OffDelayInstruction",
        "CountUpInstruction",
        "CountDownInstruction",
        "CopyInstruction",
        "CalcInstruction",
        "ShiftInstruction",
        "EventDrumInstruction",
        "TimeDrumInstruction",
        "TimeEventDrumInstruction",
        "FunctionCallInstruction",
        "EnabledFunctionCallInstruction",
        "BlockCopyInstruction",
        "FillInstruction",
        "PackBitsInstruction",
        "PackWordsInstruction",
        "PackTextInstruction",
        "UnpackToBitsInstruction",
        "UnpackToWordsInstruction",
        "ModbusSendInstruction",
        "ModbusReceiveInstruction",
        "SearchInstruction",
        "ForLoopInstruction",
    }
)

_FUNCTION_INSTRUCTIONS = frozenset(
    {
        "FunctionCallInstruction",
        "EnabledFunctionCallInstruction",
    }
)

_TIMER_COUNTER_INSTRUCTIONS = frozenset(
    {
        "OnDelayInstruction",
        "OffDelayInstruction",
        "CountUpInstruction",
        "CountDownInstruction",
    }
)


def _classify_profile_enabled() -> bool:
    return bool(os.environ.get("PYRUNG_PROFILE_CLASSIFY"))


def _classify_profile_format(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _classify_profile_mark(
    timings: list[tuple[str, float]],
    label: str,
    start: float,
) -> float:
    now = time.perf_counter()
    timings.append((label, now - start))
    return now


def _classify_profile_emit(
    label: str,
    timings: list[tuple[str, float]],
    total: float,
    **meta: Any,
) -> None:
    meta_text = " ".join(f"{key}={_classify_profile_format(value)}" for key, value in meta.items())
    prefix = f"[pyrung classify] {label}: total={total:.3f}s"
    if meta_text:
        prefix = f"{prefix} {meta_text}"
    print(prefix, file=sys.stderr)
    for timing_label, elapsed in timings:
        print(
            f"[pyrung classify]   {timing_label:<34s} {elapsed:8.3f}s",
            file=sys.stderr,
        )


def _unread_timer_done_tags(
    program: Program,
    graph: ProgramGraph,
    done_acc: dict[str, str],
    *,
    scope: list[str] | None,
    project: tuple[str, ...] | None,
) -> frozenset[str]:
    """Done tags of timers and counters whose every output is read by nothing.

    The Done bit, the accumulator and any status bits are read by no rung,
    branch, instruction condition or data read (an owner's exclusive read of
    its own accumulator aside), and none is in *scope* or *project*.  Such a
    timer runs, but no other tag depends on it, so its state never decides
    what else is reachable.
    """
    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction
    from pyrung.core.validation._common import walk_instructions

    observed = set(scope or ()) | set(project or ())
    outputs: dict[str, set[str]] = {}
    for instr in walk_instructions(program):
        if not isinstance(
            instr,
            (OnDelayInstruction, OffDelayInstruction, CountUpInstruction, CountDownInstruction),
        ):
            continue
        done_name = instr.done_bit.name
        names = outputs.setdefault(done_name, {done_name, instr.accumulator.name})
        for status in ("en_bit", "tt_bit"):
            tag = getattr(instr, status, None)
            if tag is not None:
                names.add(tag.name)
    unread: set[str] = set()
    for done_name, acc_name in done_acc.items():
        names = outputs.get(done_name)
        if names is None or acc_name not in names:
            continue
        if any(name in observed or graph.readers_of.get(name) for name in names):
            continue
        unread.add(done_name)
    return frozenset(unread)


def _tag_is_observable(
    tag_name: str,
    *,
    scope: list[str] | None,
    project: tuple[str, ...] | None,
) -> bool:
    return tag_name in set(scope or ()) or tag_name in set(project or ())


def _has_inert_writer(
    program: Program, tag_name: str, reach: Mapping[int, tuple[str, ...]] | None = None
) -> bool:
    from pyrung.core.validation._common import walk_instructions

    for instr in walk_instructions(program):
        if tag_name not in {name for name, _itype in _all_write_targets(instr, reach)}:
            continue
        if getattr(instr, "is_inert_when_disabled", lambda: True)():
            return True
    return False


def _has_out_oneshot_writer(program: Program, tag_name: str) -> bool:
    from pyrung.core.instruction.coils import OutInstruction
    from pyrung.core.validation._common import walk_instructions

    for instr in walk_instructions(program):
        if not isinstance(instr, OutInstruction):
            continue
        if not getattr(instr, "_oneshot", False):
            continue
        if tag_name in {name for name, _itype in _all_write_targets(instr)}:
            return True
    return False


def _has_skippable_coil_writer(program: Program, tag_name: str, graph: ProgramGraph) -> bool:
    """Whether an ``out()`` coil of *tag_name* sits where some scans skip it.

    An ``out()`` writes False when its rung is off, but only on a scan that
    runs it.  One behind a ``return_early()``, in a subroutine not called on
    every scan, or in a forloop body (a false For rung skips the body) holds
    its value on the scans that skip it, so the tag carries state even when
    nothing in the program reads it.
    """
    from pyrung.core.instruction.coils import OutInstruction
    from pyrung.core.instruction.control import ForLoopInstruction
    from pyrung.core.validation._common import walk_instructions

    writes_coil = False
    for instr in walk_instructions(program):
        if isinstance(instr, ForLoopInstruction):
            for body_instr in instr.instructions:
                if isinstance(body_instr, OutInstruction) and tag_name in {
                    name for name, _itype in _all_write_targets(body_instr, graph.indirect_reach)
                }:
                    return True
        elif isinstance(instr, OutInstruction) and tag_name in {
            name for name, _itype in _all_write_targets(instr, graph.indirect_reach)
        }:
            writes_coil = True
    return writes_coil and not _is_ote_unconditionally_reachable(tag_name, graph)


def _collect_all_exprs(
    program: Program,
    graph: ProgramGraph,
    scope: list[str] | None = None,
) -> list[Expr]:
    """Collect all expression trees from simplified forms and write-site conditions.

    When *scope* is given, restricts to expressions in the upstream cone
    of the scoped tags.  This improves don't-care pruning without
    affecting soundness — cone-of-influence reduction and abstraction
    commute for safety properties, so absorption decisions made on the
    scoped set are sound even though out-of-cone reads are not visible.
    """
    forms = simplified_forms(program)

    upstream: frozenset[str] | None = None
    if scope is not None:
        upstream_tags: set[str] = set(scope)
        for tag_name in scope:
            upstream_tags.update(graph.upstream_slice(tag_name, follow_calls=False))
        upstream = frozenset(upstream_tags)
        forms = {k: v for k, v in forms.items() if k in upstream}

    exprs: list[Expr] = [tf.expr for tf in forms.values()]

    from pyrung.core.analysis.pdg import _implicit_fault_writes
    from pyrung.core.validation._common import (
        _build_caller_map,
        _collect_write_sites,
        _transitive_caller_conditions,
    )

    tag_refs = dict(graph.tags)

    def _write_targets_with_faults(instr: Any) -> list[tuple[str, str]]:
        seen: set[tuple[str, str]] = set(_all_write_targets(instr, graph.indirect_reach))
        instr_type = type(instr).__name__
        for fault_name in sorted(_implicit_fault_writes(instr, tag_refs)):
            seen.add((fault_name, instr_type))
        return sorted(seen)

    sites = _collect_write_sites(program, target_extractor=_write_targets_with_faults)
    # Whether a subroutine's writes happen at all is decided by every call on
    # a path into it: a call in a forloop body, or one from another
    # subroutine (whose own callers then gate it too).
    caller_map = _build_caller_map(program)
    gated_subs: set[str] = set()
    for site in sites:
        if upstream is not None and site.target_name not in upstream:
            continue
        if site.conditions:
            for cond in site.conditions:
                exprs.append(_condition_to_expr(cond))
        if site.subroutine is not None and site.subroutine not in gated_subs:
            gated_subs.add(site.subroutine)
            for caller_chain in _transitive_caller_conditions(site.subroutine, caller_map):
                for cond in caller_chain:
                    exprs.append(_condition_to_expr(cond))

    # A return guard that can skip a write, or a call (whose callee's writes
    # it skips too), decides which inputs are live.  ``include_current`` adds
    # a return earlier in the node's own rung (a branch's).
    for node in graph.rung_nodes:
        node_writes = graph.node_writes(node)
        if not node.guard_reads or not (node_writes or node.calls):
            continue
        if upstream is not None and not node.calls and not (node_writes & upstream):
            continue
        exprs.extend(return_early_guard_exprs(program, node, include_current=True))

    _collect_implicit_edge_condition_exprs(program, upstream, exprs)
    return exprs


def _collect_implicit_edge_condition_exprs(
    program: Program,
    upstream: frozenset[str] | None,
    exprs: list[Expr],
) -> None:
    """Append expressions from implicit-edge instruction conditions.

    Drum events, shift clocks, and drum jog/jump conditions use internal
    rising-edge detection rather than rise()/fall() atoms.  Their condition
    expressions must be in all_exprs so _live_inputs can see the ND inputs
    they reference.  A time & event drum's events are levels, but they sit in
    no rung expression either.
    """
    from pyrung.core.instruction.advanced import ShiftInstruction
    from pyrung.core.instruction.control import ForLoopInstruction
    from pyrung.core.instruction.drums import (
        EventDrumInstruction,
        TimeDrumInstruction,
        TimeEventDrumInstruction,
    )

    def _add(cond: Any) -> None:
        if cond is not None:
            exprs.append(_condition_to_expr(cond))

    def _in_scope(instr: Any) -> bool:
        if upstream is None:
            return True
        for field_name in getattr(type(instr), "_writes", ()):
            targets = getattr(instr, field_name, None)
            if targets is None:
                continue
            if hasattr(targets, "__iter__") and not isinstance(targets, str):
                for t in targets:
                    if hasattr(t, "name") and t.name in upstream:
                        return True
            elif hasattr(targets, "name") and targets.name in upstream:
                return True
        return False

    def _walk(instructions: list[Any]) -> None:
        for instr in instructions:
            if isinstance(instr, ShiftInstruction) and _in_scope(instr):
                _add(instr.clock_condition)
            elif isinstance(
                instr, (EventDrumInstruction, TimeDrumInstruction, TimeEventDrumInstruction)
            ) and _in_scope(instr):
                _add(instr.jog_condition)
                _add(instr.jump_condition)
                _add(instr.reset_condition)
                if isinstance(instr, (EventDrumInstruction, TimeEventDrumInstruction)):
                    for event_cond in instr.events:
                        _add(event_cond)
            if isinstance(instr, ForLoopInstruction) and hasattr(instr, "instructions"):
                _walk(instr.instructions)

    def _walk_rung(rung: Any) -> None:
        _walk(rung._instructions)
        for branch in rung._branches:
            _walk_rung(branch)

    for rung in program.rungs:
        _walk_rung(rung)
    for sub_name in program.subroutines:
        for rung in program.subroutines[sub_name]:
            _walk_rung(rung)


def _boundary_values_for_tag(other_tag: Tag) -> list[Any]:
    """Extract boundary-representative values from a tag's metadata.

    For tag-vs-tag comparisons like ``A > B``, we only need values that
    distinguish both comparison outcomes — not the full numeric range.
    """
    if other_tag.choices is not None:
        return sorted(other_tag.choices.keys())
    if other_tag.min is not None and other_tag.max is not None:
        vals: list[Any] = [other_tag.min]
        if other_tag.max != other_tag.min:
            vals.append(other_tag.max)
        return vals
    return []


_NO_LITERAL_WRITE = object()


def _eq_ne_other_value(tag: Tag, literals: set[Any]) -> Any:
    """A value of *tag* that no literal in *literals* names.

    An input read only through ``==``/``!=`` against literals needs one more
    value standing for all the others.  It is a real value of the tag's type,
    so rungs outside the property's cone (which may compare it with ``>``)
    and a replayed counterexample both get something the PLC can hold.  The
    power-on default is preferred; otherwise the nearest unused value.
    """
    from pyrung.core.analysis.crossings._ranges import type_bounds

    lo: Any = None
    hi: Any = None
    if tag.type == TagType.CHAR:
        candidates: Iterable[Any] = itertools.chain(
            (tag.default,), (chr(code) for code in range(32, 127))
        )
    else:
        bounds = type_bounds(tag.type)
        if bounds is not None:
            lo, hi = bounds
        if tag.min is not None:
            lo = tag.min if lo is None else max(lo, tag.min)
        if tag.max is not None:
            hi = tag.max if hi is None else min(hi, tag.max)
        start = tag.default
        if lo is not None and start < lo:
            start = lo
        if hi is not None and start > hi:
            start = hi
        step = 1.0 if tag.type == TagType.REAL else 1
        candidates = (
            start + sign * offset * step
            for offset in itertools.count()
            for sign in ((1,) if offset == 0 else (1, -1))
        )
    for value in itertools.islice(candidates, len(literals) * 2 + 200):
        if value in literals:
            continue
        if not isinstance(value, str):
            if lo is not None and value < lo:
                continue
            if hi is not None and value > hi:
                continue
        return value
    raise AssertionError(f"no value of {tag.name} outside {sorted(literals, key=repr)}")


def _has_non_condition_data_read(tag_name: str, graph: ProgramGraph | None) -> bool:
    """True when a tag participates in non-condition data flow."""
    if graph is None:
        return False
    return any(tag_name in node.data_reads for node in graph.rung_nodes)


def _comparison_influencing_tags(
    graph: ProgramGraph,
    atom_idx: dict[str, list[Any]],
    program: Program | None = None,
) -> frozenset[str]:
    """Tags whose value can reach a comparison atom via invertible copy/calc.

    A free input that only feeds arithmetic — ``calc(100 - level, pv)`` — still
    influences a downstream comparison such as ``pv < band``.  The direct
    ``atom_idx`` membership test misses this transitive case and would
    misclassify the input as constant storage.

    Back-propagates from direct comparison participants through *invertible*
    reverse edges (copy, fill, single-source calc expressions that
    ``_calc_reverse_edge`` can invert).  Multi-source expressions like
    ``DS.select(1, 2).sum()`` are NOT invertible, so genuinely constant block
    slots that only feed such expressions stay pinned to their default.

    Every tag an index reads counts as well: ``copy(DS[Ptr], N)`` makes ``N``
    depend on ``Ptr``, and the copy edge above sees no tag source there.  The
    block slots behind the index stay constant storage.
    """
    influential: set[str] = {name for name in graph.tags if atom_idx.get(name)}
    influential.update(graph.index_tags)
    reach = graph.indirect_reach
    if program is None:
        return frozenset(influential)

    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.data_transfer import (
        BlockCopyInstruction,
        CopyInstruction,
        FillInstruction,
    )
    from pyrung.core.validation._common import _resolve_tag_names, walk_instructions

    target_to_sources: dict[str, set[str]] = {}
    for instr in walk_instructions(program):
        if isinstance(instr, CopyInstruction):
            if instr.convert is not None:
                continue
            source_name = _tag_name_from_value(instr.source)
            if source_name is None:
                continue
            target_names = _resolve_tag_names(instr.dest)
            if not target_names:
                target_names = _expand_indirect_tag_names(instr.dest, reach)
            for tn in target_names:
                target_to_sources.setdefault(tn, set()).add(source_name)
        elif isinstance(instr, FillInstruction):
            source_name = _tag_name_from_value(instr.value)
            if source_name is None:
                continue
            target_names = _resolve_tag_names(instr.dest)
            if not target_names:
                target_names = _expand_indirect_tag_names(instr.dest, reach)
            for tn in target_names:
                target_to_sources.setdefault(tn, set()).add(source_name)
        elif isinstance(instr, BlockCopyInstruction):
            if instr.convert is not None:
                continue
            source_names = _resolve_tag_names(instr.source)
            dest_names = _resolve_tag_names(instr.dest)
            if not source_names:
                source_names = _expand_indirect_tag_names(instr.source, reach)
            if not dest_names:
                dest_names = _expand_indirect_tag_names(instr.dest, reach)
            if source_names and dest_names and len(source_names) == len(dest_names):
                for src, dst in zip(source_names, dest_names, strict=True):
                    target_to_sources.setdefault(dst, set()).add(src)
        elif isinstance(instr, CalcInstruction):
            target_name = _tag_name_from_value(instr.dest)
            if target_name is None:
                continue
            edge = _calc_reverse_edge(instr.expression)
            if edge is not None:
                source_name, _ = edge
                target_to_sources.setdefault(target_name, set()).add(source_name)

    changed = True
    while changed:
        changed = False
        for target, sources in target_to_sources.items():
            if target in influential:
                for source in sources:
                    if source not in influential:
                        influential.add(source)
                        changed = True
    return frozenset(influential)


def _compared_value_names(
    value: Any, graph: ProgramGraph, index_out: set[str] | None = None
) -> set[str]:
    """Names of the tags whose *values* a comparison operand reads.

    A literal or a readonly tag (a constant) reads none.  An indirect access
    reads the block elements it can reach; the tags its index reads (the
    pointer picks the element) go to *index_out*, or are returned with the
    elements when it is None.
    """
    from enum import Enum

    from pyrung.core.analysis.pdg import _extract_tag_names
    from pyrung.core.expression import Expression
    from pyrung.core.memory_block import (
        BlockRange,
        IndirectBlockRange,
        IndirectExprRef,
        IndirectRef,
    )
    from pyrung.core.tag import ImmediateRef, Tag

    found: set[str] = set()
    seen: set[int] = set()

    def walk(current: Any) -> None:
        if current is None or isinstance(current, (bool, int, float, str, bytes, Enum)):
            return
        if id(current) in seen:
            return
        seen.add(id(current))
        if isinstance(current, ImmediateRef):
            walk(current.value)
        elif isinstance(current, Tag):
            if not current.readonly:
                found.add(current.name)
        elif isinstance(current, (IndirectRef, IndirectExprRef, IndirectBlockRange)):
            found.update(_expand_indirect_tag_names(current, graph.indirect_read_reach))
            if isinstance(current, IndirectRef):
                index_parts: tuple[Any, ...] = (current.pointer,)
            elif isinstance(current, IndirectExprRef):
                index_parts = (current.expr,)
            else:
                index_parts = (current.start_expr, current.end_expr)
            index_names = _compared_value_names(index_parts, graph)
            if index_out is None:
                found.update(index_names)
            else:
                index_out.update(index_names)
        elif isinstance(current, BlockRange):
            found.update(tag.name for tag in current.tags() if not tag.readonly)
        elif isinstance(current, Expression):
            for key in sorted(vars(current)):
                if not key.startswith("_"):
                    walk(getattr(current, key))
        elif isinstance(current, (list, tuple)):
            for item in current:
                walk(item)
        elif isinstance(current, dict):
            for item in current.values():
                walk(item)
        else:
            # Anything else (a converter, a custom operand): every tag in it.
            found.update(_extract_tag_names(current, dict(graph.tags)))

    walk(value)
    return found


def _iter_program_conditions(program: Program) -> Iterable[Any]:
    """Every condition the program evaluates: rung contacts and instruction conditions."""
    from pyrung.core.condition import Condition
    from pyrung.core.instruction.control import ForLoopInstruction
    from pyrung.core.rung import Rung as RungClass

    def from_rung(rung: Any) -> Iterable[Any]:
        yield from rung._conditions
        for instr in rung._instructions:
            yield from from_instruction(instr)
        for branch in rung._branches:
            yield from from_rung(branch)

    def from_instruction(instr: Any) -> Iterable[Any]:
        for field_name in getattr(type(instr), "_conditions", ()):
            value = getattr(instr, field_name, None)
            items = value if isinstance(value, (list, tuple)) else (value,)
            for item in items:
                if isinstance(item, Condition):
                    yield item
        if isinstance(instr, ForLoopInstruction):
            for item in instr.body:
                if isinstance(item, RungClass):
                    yield from from_rung(item)
                else:
                    yield from from_instruction(item)

    for rung in program.rungs:
        yield from from_rung(rung)
    for sub_rungs in program.subroutines.values():
        for rung in sub_rungs:
            yield from from_rung(rung)


def _exact_value_sets(
    program: Program,
    graph: ProgramGraph,
    receive_dest_names: frozenset[str],
    constant_storage: frozenset[str] = frozenset(),
) -> dict[str, frozenset[Any]]:
    """Tags with a known finite set that holds every value they can take.

    A constant (readonly, or *constant_storage*: unwritten internal tags the
    search holds at their default) holds its default, a Bool both values, and a free
    input (external, physical, a receive destination or unwritten) what its
    ``choices=`` or integer ``min=``/``max=`` allow.  A tag only the program
    writes (an ``external`` one too: the search changes a written tag through
    its writers only) holds its power-on value and what its writers store: literals, and
    the values of exact tags copied in (``copy``/``fill``/``blockcopy``, with
    the copy's store conversion).  A tag with any other writer (``calc``, a
    timer, a function, ...) is not exact.  Sets are capped at
    ``_TAG_COMPARED_DOMAIN_CAP`` values.
    """
    from pyrung.core.instruction.conversions import _store_copy_value_to_tag_type
    from pyrung.core.instruction.data_transfer import (
        BlockCopyInstruction,
        CopyInstruction,
        FillInstruction,
    )
    from pyrung.core.validation._common import _resolve_tag_names, walk_instructions

    def is_free(name: str, tag: Tag) -> bool:
        return (
            tag.external
            or name not in graph.writers_of
            or graph.is_physical_input(name)
            or name in receive_dest_names
            or graph.tag_roles.get(name) is TagRole.INPUT
        )

    def program_driven(name: str) -> bool:
        return (
            name in graph.writers_of
            and not graph.is_physical_input(name)
            and name not in receive_dest_names
            and graph.tag_roles.get(name) is not TagRole.INPUT
        )

    exact: dict[str, frozenset[Any]] = {}
    for name, tag in graph.tags.items():
        if tag.readonly or name in constant_storage:
            exact[name] = frozenset({tag.default})
        elif tag.type == TagType.BOOL:
            exact[name] = frozenset({False, True})
        elif is_free(name, tag) and not program_driven(name):
            values = _every_value_domain(tag)
            if values is not None:
                exact[name] = frozenset(values)

    # target -> ("literal", stored value) | ("copy", source name)
    feeds: dict[str, list[tuple[str, Any]]] = {}
    opaque: set[str] = set()
    for instr in walk_instructions(program):
        targets = [name for name, _itype in _all_write_targets(instr, graph.indirect_reach)]
        if not targets:
            continue
        literal = _literal_write_values(instr, graph.tags, graph.indirect_reach)
        source_of: dict[str, str] = {}
        if literal is None:
            if isinstance(instr, CopyInstruction) and instr.convert is None:
                source = _tag_name_from_value(instr.source)
                if source is not None:
                    source_of = dict.fromkeys(targets, source)
            elif isinstance(instr, FillInstruction):
                source = _tag_name_from_value(instr.value)
                if source is not None:
                    source_of = dict.fromkeys(targets, source)
            elif isinstance(instr, BlockCopyInstruction) and instr.convert is None:
                sources = _resolve_tag_names(instr.source)
                dests = _resolve_tag_names(instr.dest)
                if sources and len(sources) == len(dests):
                    source_of = dict(zip(dests, sources, strict=True))
        for target in targets:
            if literal is not None and target in literal:
                feeds.setdefault(target, []).append(("literal", literal[target]))
            elif target in source_of:
                feeds.setdefault(target, []).append(("copy", source_of[target]))
            else:
                opaque.add(target)

    written = {
        name: graph.tags[name]
        for name in feeds
        if name not in opaque and name in graph.tags and name not in exact and program_driven(name)
    }
    values: dict[str, set[Any]] = {name: {tag.default} for name, tag in written.items()}
    changed = True
    while changed:
        changed = False
        for name, tag in written.items():
            if name not in values:
                continue
            grown = set(values[name])
            for kind, item in feeds[name]:
                if kind == "literal":
                    grown.add(item)
                    continue
                source_values = exact.get(item)
                if source_values is None:
                    source_values = values.get(item)
                if source_values is None:
                    grown = None
                    break
                grown.update(_store_copy_value_to_tag_type(v, tag) for v in source_values)
            if grown is None or len(grown) > _TAG_COMPARED_DOMAIN_CAP:
                del values[name]
                changed = True
            elif grown != values[name]:
                values[name] = grown
                changed = True
    for name, found in values.items():
        exact[name] = frozenset(found)
    return exact


class _ComparedInputs(NamedTuple):
    """What ``_tag_compared_names`` found; see there."""

    compared: dict[str, str]
    exact_partners: dict[str, frozenset[str]]
    groups: tuple[frozenset[str], ...]
    index_only: frozenset[str]


def _tag_compared_names(
    program: Program,
    graph: ProgramGraph,
    exact_values: Mapping[str, frozenset[Any]],
    *,
    exprs: Iterable[Any] = (),
    free_inputs: frozenset[str] = frozenset(),
) -> _ComparedInputs:
    """Tags compared with a changing value, with exact tags, or with each other.

    The expression partition (``_extract_value_domain``) gives a tag one
    representative per class its comparisons distinguish, splitting its values
    at the literals it is compared with.  A comparison with another changing
    value (``T.Acc == In``, ``In + 5 > 100``, ``DS[Ptr] == In``,
    ``search(DS > In)``) splits them at points no literal names, and ``==``
    against a value that steps past the literals (an accumulator moving 10 ms a
    scan) needs that exact value.  No partition covers such a tag: the search
    needs every value it can hold.  These are ``compared``,
    ``{name: what it is compared with}``.

    A tag compared directly with a tag whose every value is known
    (``_exact_value_sets``: ``In < Limit`` with ``Limit`` in ``0..5``) is
    compared with those values as literals: its partition stays sound once the
    partner's values are among its split points (``exact_partners``).

    Free inputs (*free_inputs*: chosen afresh each scan) compared directly
    with each other (``PV > SP``) form ``groups``: each member's partition is
    taken over the split points of every member, with a representative per
    member in each gap (see ``_group_domains``).

    The value reaches a comparison through data flow as well: a tag copied or
    calculated into a tag compared with a changing or an exact value needs
    every value (the partner's values are not carried back through the
    arithmetic).  So does the index of an indirect access whose element is
    compared with a changing value, and what that index is computed from; an
    index that is itself a free input keeps its pointer domain
    (``index_only``).

    *exprs* are the lowered expressions of the search (the property and the
    program): their comparisons count like the program's.
    """
    from pyrung.core.analysis.simplified import And as ExprAnd
    from pyrung.core.analysis.simplified import ArithAtom, Atom
    from pyrung.core.analysis.simplified import Or as ExprOr
    from pyrung.core.condition import (
        AllCondition,
        AnyCondition,
        BitCondition,
        CompareEq,
        CompareGe,
        CompareGt,
        CompareLe,
        CompareLt,
        CompareNe,
        Condition,
        FallingEdgeCondition,
        IntTruthyCondition,
        NormallyClosedCondition,
        RisingEdgeCondition,
    )
    from pyrung.core.instruction.advanced import SearchInstruction
    from pyrung.core.tag import ImmediateRef, Tag
    from pyrung.core.validation._common import walk_instructions
    from pyrung.core.validation.walker import _condition_children

    compared: dict[str, str] = {}
    index_marks: dict[str, str] = {}
    partners: dict[str, set[str]] = {}
    group_of: dict[str, str] = {}

    def find(name: str) -> str:
        while group_of.get(name, name) != name:
            name = group_of[name]
        return name

    def mark(names: Iterable[str], partner: str) -> None:
        for name in names:
            compared.setdefault(name, partner)

    def mark_index(names: Iterable[str], what: str) -> None:
        for name in names:
            index_marks.setdefault(name, f"an index of {what}")

    def label(names: set[str]) -> str:
        return ", ".join(sorted(names)) if names else "a computed value"

    def plain_tag(value: Any) -> bool:
        if isinstance(value, ImmediateRef):
            value = value.value
        return isinstance(value, Tag)

    def seen_by_partition(value: Any) -> bool:
        # A literal, or a readonly tag (lowered to its value): the expression
        # partition splits at it.  An expression is not, even a constant one.
        if isinstance(value, ImmediateRef):
            value = value.value
        if isinstance(value, Tag):
            return value.readonly
        return isinstance(value, (bool, int, float, str))

    def compare(left_value: Any, right_value: Any) -> None:
        left_index: set[str] = set()
        right_index: set[str] = set()
        left = _compared_value_names(left_value, graph, left_index)
        right = _compared_value_names(right_value, graph, right_index)
        left_seen, right_seen = seen_by_partition(left_value), seen_by_partition(right_value)
        if (left_seen or plain_tag(left_value)) and (right_seen or plain_tag(right_value)):
            if left_seen or right_seen or left == right:
                # A literal or a constant against a tag: the partition splits
                # at it.
                return
            (left_name,), (right_name,) = left, right
            if left_name in free_inputs and right_name in free_inputs:
                left_root, right_root = find(left_name), find(right_name)
                if left_root != right_root:
                    group_of[left_root] = right_root
                group_of.setdefault(left_name, left_name)
                group_of.setdefault(right_name, right_name)
                return
            if right_name in exact_values:
                partners.setdefault(left_name, set()).add(right_name)
            else:
                mark(left, right_name)
            if left_name in exact_values:
                partners.setdefault(right_name, set()).add(left_name)
            else:
                mark(right, left_name)
            return
        mark(left, label(right))
        mark(right, label(left))
        everything = left | right
        mark_index(left_index | right_index, label(everything))

    def mark_all(values: Iterable[Any]) -> None:
        index: set[str] = set()
        names: set[str] = set()
        for value in values:
            names |= _compared_value_names(value, graph, index)
        for name in names:
            mark((name,), label(names - {name}))
        mark_index(index, label(names))

    leaf_skip = (
        BitCondition,
        NormallyClosedCondition,
        IntTruthyCondition,
        RisingEdgeCondition,
        FallingEdgeCondition,
    )
    direct_compare = (CompareEq, CompareNe, CompareLt, CompareLe, CompareGt, CompareGe)
    compare_forms = {"eq", "ne", "lt", "le", "gt", "ge"}

    stack: list[Any] = list(_iter_program_conditions(program))
    # The property's (and the program's) lowered atoms: a property names no
    # rung, so its comparisons are only found here.
    expr_stack: list[Any] = list(exprs)
    while expr_stack:
        expr = expr_stack.pop()
        if isinstance(expr, (ExprAnd, ExprOr)):
            expr_stack.extend(expr.terms)
        elif isinstance(expr, ArithAtom):
            names = {
                name
                for name in (expr.left, expr.right)
                if name in graph.tags and not graph.tags[name].readonly
            }
            for name in names:
                mark((name,), label(names - {name}))
        elif isinstance(expr, Atom):
            if expr.unsupported is not None:
                stack.append(expr.unsupported)
            elif expr.form in compare_forms and expr.tag in graph.tags:
                operand = expr.operand
                if expr.operand_is_tag:
                    operand = graph.tags.get(operand, operand)
                compare(graph.tags[expr.tag], operand)

    seen: set[int] = set()
    while stack:
        cond = stack.pop()
        if id(cond) in seen or not isinstance(cond, Condition):
            continue
        seen.add(id(cond))
        if isinstance(cond, (AllCondition, AnyCondition)):
            stack.extend(cond.conditions)
            continue
        if isinstance(cond, leaf_skip):
            continue
        if isinstance(cond, direct_compare):
            compare(cond.tag, cond.value)
            continue
        # Indirect, expression and unknown comparisons: every value they read
        # is compared with something the partition does not see.
        mark_all(child for _child_name, child in _condition_children(cond))

    for instr in walk_instructions(program):
        if isinstance(instr, SearchInstruction):
            mark_all((instr.value, instr.search_range))

    # Back through data flow: whatever such a tag is computed from.  The
    # index of an indirect read or write is a source too: it picks the
    # element read, or the element written.
    sources_of: dict[str, set[str]] = {}
    for instr in walk_instructions(program):
        reads: set[str] = set()
        for field_name in getattr(type(instr), "_reads", ()):
            reads |= _compared_value_names(getattr(instr, field_name, None), graph)
        for field_name in getattr(type(instr), "_writes", ()):
            write_index: set[str] = set()
            _compared_value_names(getattr(instr, field_name, None), graph, write_index)
            reads |= write_index
        if not reads:
            continue
        for target_name, _itype in _all_write_targets(instr, graph.indirect_reach):
            sources_of.setdefault(target_name, set()).update(reads)
    why = dict(compared)
    for name, reason in index_marks.items():
        why.setdefault(name, reason)
    for name, exact_partners in partners.items():
        why.setdefault(name, label(exact_partners))
    queue = list(why)
    while queue:
        target_name = queue.pop()
        for source in sources_of.get(target_name, ()):
            if source not in compared:
                compared[source] = f"{why[target_name]}, through {target_name}"
                why.setdefault(source, compared[source])
                queue.append(source)
    index_only = frozenset(name for name in index_marks if name not in compared)
    for name in index_only:
        compared[name] = index_marks[name]
    groups: dict[str, set[str]] = {}
    for name in group_of:
        groups.setdefault(find(name), set()).add(name)
    return _ComparedInputs(
        compared,
        {name: frozenset(found) for name, found in partners.items() if name not in compared},
        tuple(frozenset(members) for members in groups.values()),
        index_only,
    )


def _normalize_literal_write_value(raw_value: Any, target: Tag) -> Any | object:
    """Return the concrete value stored by a literal copy/fill write."""
    from pyrung.core.expression import Expression
    from pyrung.core.instruction.conversions import _store_copy_value_to_tag_type
    from pyrung.core.tag import ImmediateRef, Tag

    value = raw_value.value if isinstance(raw_value, ImmediateRef) else raw_value
    if isinstance(value, Tag):
        if value.readonly:
            value = value.default
        else:
            return _NO_LITERAL_WRITE
    if isinstance(value, Expression):
        return _NO_LITERAL_WRITE
    if not isinstance(value, (bool, int, float, str)):
        return _NO_LITERAL_WRITE
    return _store_copy_value_to_tag_type(value, target)


def _literal_write_values(
    instr: Any,
    tags: dict[str, Tag],
    reach: Mapping[int, tuple[str, ...]] | None = None,
) -> dict[str, Any] | None:
    """Return normalized literal values for copy/fill targets, if any."""
    from pyrung.core.instruction.data_transfer import CopyInstruction, FillInstruction

    if isinstance(instr, CopyInstruction):
        if instr.convert is not None:
            return None
        raw_value = instr.source
    elif isinstance(instr, FillInstruction):
        raw_value = instr.value
    else:
        return None

    values: dict[str, Any] = {}
    for target_name, _itype in _all_write_targets(instr, reach):
        target = tags.get(target_name)
        if target is None:
            return None
        stored = _normalize_literal_write_value(raw_value, target)
        if stored is _NO_LITERAL_WRITE:
            return None
        values[target_name] = stored
    return values


def _collect_literal_write_domains(
    program: Program,
    tags: dict[str, Tag],
    reach: Mapping[int, tuple[str, ...]] | None = None,
) -> dict[str, tuple[Any, ...]]:
    """Infer exact finite domains for tags written only by literal copy/fill."""
    from pyrung.core.validation._common import walk_instructions

    literal_values_by_target: dict[str, set[Any]] = {}
    disqualified: set[str] = set()

    for instr in walk_instructions(program):
        targets = [name for name, _itype in _all_write_targets(instr, reach)]
        if not targets:
            continue

        literal_values = _literal_write_values(instr, tags, reach)
        for target_name in targets:
            if target_name in disqualified:
                continue
            if literal_values is None or target_name not in literal_values:
                disqualified.add(target_name)
                literal_values_by_target.pop(target_name, None)
                continue
            literal_values_by_target.setdefault(target_name, set()).add(literal_values[target_name])

    domains: dict[str, tuple[Any, ...]] = {}
    for target_name, values in literal_values_by_target.items():
        if target_name in disqualified:
            continue
        tag = tags.get(target_name)
        if tag is None:
            continue
        domains[target_name] = tuple(sorted({tag.default, *values}))
    return domains


def _expr_reads_tag(expr: Any, tag_name: str) -> bool:
    """True when *expr* references *tag_name* anywhere in the tree.

    Uses the PDG's own read walker, so every expression shape (range sums and
    aggregates, math and shift functions, indirect refs) counts exactly as the
    PDG counts it.
    """
    from pyrung.core.analysis.pdg import _extract_tag_names

    return tag_name in _extract_tag_names(expr, {})


def _hop_affine_index(
    idx_tag: str,
    writer_instrs: dict[str, list[Any]],
) -> tuple[str, int, int | float]:
    """Follow a single-writer bijective ``copy`` / ``calc(src ± k)`` pointer back
    to its root register, tracking the address-as-a-function-of-root transform.

    A scratch pointer is often computed from the real driver
    (``calc(StateRequested + 150, JumpIdx)``); the scratch itself never steps, so
    the coupling must reach the root.  Only bijective affine hops (scale ∈ {1, -1})
    are followed — they preserve "cycles through values", so the root steps iff
    the pointer does.  Returns ``(root, scale, offset)`` where the addressed slot
    is ``scale * root_value + offset`` (an ``IndirectRef`` addresses the block by
    its pointer's value).  Bounded to three hops.
    """
    tag = idx_tag
    scale_acc: int = 1
    offset_acc: int | float = 0
    for _ in range(3):
        writers = writer_instrs.get(tag)
        if writers is None or len(writers) != 1:
            break
        fwd = _extract_forward_affine(writers[0])
        if fwd is None:
            break
        source, scale, offset = fwd
        if source == tag or abs(scale) != 1:
            break
        # address = scale_acc * tag + offset_acc; tag = scale * source + offset
        offset_acc = scale_acc * offset + offset_acc
        scale_acc = scale_acc * scale
        tag = source
    return tag, scale_acc, offset_acc


def _indirect_constant_table_index(
    source: Any,
    graph: ProgramGraph,
    writer_instrs: dict[str, list[Any]],
    literal_values: dict[str, set[Any]],
) -> str | None:
    """Index register a ``copy(block[index], dest)`` couples its dest's stepping to.

    Returns the (root) index tag when *source* is an ``IndirectRef`` whose read
    slots are never written **over the index's reachable address region**, else
    ``None`` (punt).  When those slots are constant, ``dest = table[index]`` is a
    pure function of the index, so ``dest`` steps iff the index steps.

    The region is bounded by the *index's* derivable value domain — declared
    ``choices=`` / ``min``/``max`` (a sound over-approximation) or, failing that,
    the index's own literal write values — NOT the whole block: real jump tables
    live inside a shared ``ds`` bank where unrelated slots are written, so a
    whole-block never-written test would always punt on real machines (see
    :func:`_domain_from_indirect_source`, which bounds the same way).  When the
    index has no derivable finite domain there is no sound region, so we punt.
    (``IndirectExprRef`` sources punt, matching the scope of
    :func:`_domain_from_indirect_source`.)
    """
    from pyrung.core.memory_block import IndirectRef

    if not isinstance(source, IndirectRef):
        return None
    idx_tag = getattr(source.pointer, "name", None)
    if idx_tag is None:
        return None

    root, scale, offset = _hop_affine_index(idx_tag, writer_instrs)

    root_tag = graph.tags.get(root)
    domain = _declared_domain(root_tag) if root_tag is not None else None
    if not domain:
        lits = literal_values.get(root)
        domain = tuple(sorted(lits)) if lits else None
    if not domain:
        return None

    block = source.block
    writers = graph.writers_of
    for value in domain:
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        addr = scale * value + offset
        if addr < block.start or addr > block.end:
            continue
        if block._effective_slot_name(addr) in writers:
            return None
    return root


def _compute_stepping_tags(
    program: Program,
    graph: ProgramGraph,
) -> frozenset[str]:
    """Tags with write-site structure that actively cycles through values.

    A tag is *stepping* if it has an arithmetic self-write (``calc(tag ± N, tag)``),
    a self-referential calc (``calc(f(tag), tag)``), or >=2 distinct literal write
    values.  Copy-coupled tags inherit stepping from their source — including a
    ``copy(block[index], tag)`` indirect copy over a *constant* table, which
    couples the destination to the table index (see
    :func:`_indirect_constant_table_index`).
    """
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.data_transfer import CopyInstruction
    from pyrung.core.validation._common import walk_instructions

    arithmetic: set[str] = set()
    literal_values: dict[str, set[Any]] = {}
    copy_targets: dict[str, str] = {}
    writer_instrs: dict[str, list[Any]] = {}
    indirect_pending: list[tuple[str, Any]] = []

    for instr in walk_instructions(program):
        targets = _all_write_targets(instr, graph.indirect_reach)
        if not targets:
            continue

        for target_name, _itype in targets:
            writer_instrs.setdefault(target_name, []).append(instr)

            fwd = _extract_forward_offset(instr)
            if fwd is not None:
                source, offset = fwd
                if source == target_name and offset != 0:
                    arithmetic.add(target_name)

            if isinstance(instr, CalcInstruction):
                if getattr(instr.dest, "name", None) == target_name and _expr_reads_tag(
                    instr.expression, target_name
                ):
                    arithmetic.add(target_name)

            if isinstance(instr, CopyInstruction) and instr.convert is None:
                source_name = _tag_name_from_value(instr.source)
                if source_name is not None and source_name != target_name:
                    copy_targets[target_name] = source_name
                elif source_name is None:
                    indirect_pending.append((target_name, instr.source))

        lit = _literal_write_values(instr, graph.tags, graph.indirect_reach)
        if lit is not None:
            for name, val in lit.items():
                literal_values.setdefault(name, set()).add(val)

    # An indirect copy over a *constant* table couples the destination to the
    # table INDEX (dest = table[index] is a pure function of the index).  Named
    # copies keep priority; a writable table punts (no coupling).
    for target_name, source in indirect_pending:
        index_tag = _indirect_constant_table_index(source, graph, writer_instrs, literal_values)
        if index_tag is not None and index_tag != target_name:
            copy_targets.setdefault(target_name, index_tag)

    stepping = set(arithmetic)
    for tag, vals in literal_values.items():
        if len(vals) >= 2:
            stepping.add(tag)

    for target, source in copy_targets.items():
        if source in stepping:
            stepping.add(target)

    return frozenset(stepping)


_TAG_COMPARED_DOMAIN_CAP = 1000


def _every_value_domain(tag: Tag) -> tuple[Any, ...] | None:
    """Every value *tag* can hold, when that is a small finite set.

    Bool, ``choices=``, or an integer ``min=``/``max=`` range of at most
    ``_TAG_COMPARED_DOMAIN_CAP`` values.  A REAL has values between any two,
    so a range does not list them.
    """
    if tag.type == TagType.BOOL:
        return (False, True)
    if tag.choices is not None:
        return tuple(sorted(tag.choices.keys()))
    if tag.type in (TagType.REAL, TagType.CHAR):
        return None
    if tag.min is None or tag.max is None:
        return None
    if int(tag.min) != tag.min or int(tag.max) != tag.max:
        return None
    if tag.max - tag.min + 1 > _TAG_COMPARED_DOMAIN_CAP:
        return None
    return tuple(range(int(tag.min), int(tag.max) + 1))


_INTEGRAL_TYPE_RANGE: dict[TagType, tuple[int, int]] = {
    TagType.INT: (-32768, 32767),
    TagType.DINT: (-(2**31), 2**31 - 1),
    TagType.WORD: (0, 65535),
}


def _group_domains(
    members: Sequence[str],
    domains: dict[str, tuple[Any, ...]],
    tags: Mapping[str, Tag],
) -> None:
    """Widen the domains of free inputs compared with each other (``PV > SP``).

    Each member's own partition splits only at its own literals; the
    comparison between members needs their relative order too.  Over the
    union *S* of every member's split points (and declared min/max), any
    assignment of the members is order-equivalent, against *S* and against
    each other, to one that puts in each gap of *S* at most one value per
    member: k members need k representatives per gap (the integers nearest
    each end of the gap for an integral member; for a REAL, k points between
    any two neighbouring candidates, so it can sit between or beside the
    integers).  Every member's domain becomes *S* plus those representatives,
    filtered to what the member can hold.  Mutates *domains*.
    """
    k = len(members)
    points: set[float] = set()
    for name in members:
        tag = tags[name]
        for value in domains[name]:
            if isinstance(value, int | float) and not isinstance(value, bool):
                points.add(value)
        for bound in (tag.min, tag.max):
            if isinstance(bound, int | float) and not isinstance(bound, bool):
                points.add(bound)
    if not points:
        return
    ordered = sorted(points)
    any_integral = any(tags[name].type in _INTEGRAL_TYPE_RANGE for name in members)
    any_real = any(tags[name].type == TagType.REAL for name in members)
    gaps = list(zip(ordered, ordered[1:], strict=False))
    gaps.insert(0, (ordered[0] - k - 1, ordered[0]))
    gaps.append((ordered[-1], ordered[-1] + k + 1))
    candidates: set[Any] = set(ordered)
    for low, high in gaps:
        ints: list[int] = []
        if any_integral or not any_real:
            first, last = math.floor(low) + 1, math.ceil(high) - 1
            if first <= last:
                ints = sorted(
                    set(range(first, min(first + k, last + 1)))
                    | set(range(max(last - k + 1, first), last + 1))
                )
                candidates.update(ints)
        if any_real:
            anchors = [low, *ints, high]
            for left, right in zip(anchors, anchors[1:], strict=False):
                step = (right - left) / (k + 1)
                candidates.update(left + step * j for j in range(1, k + 1))
    for name in members:
        tag = tags[name]
        allowed = set(domains[name])
        for value in candidates:
            if tag.type in _INTEGRAL_TYPE_RANGE:
                if value != int(value):
                    continue
                value = int(value)
                type_low, type_high = _INTEGRAL_TYPE_RANGE[tag.type]
                if not type_low <= value <= type_high:
                    continue
            elif tag.type == TagType.REAL:
                value = float(value)
            else:
                continue
            if tag.min is not None and value < tag.min:
                continue
            if tag.max is not None and value > tag.max:
                continue
            if tag.choices is not None and value not in tag.choices:
                continue
            allowed.add(value)
        domains[name] = tuple(sorted(allowed))


def _declared_domain(tag: Tag) -> tuple[Any, ...] | None:
    """Return a direct metadata domain when one is finite and explicit."""
    if tag.type == TagType.BOOL:
        return (False, True)
    if tag.choices is not None:
        return tuple(sorted(tag.choices.keys()))
    if tag.min is None or tag.max is None:
        return None
    if not isinstance(tag.min, int | float) or not isinstance(tag.max, int | float):
        return None
    if int(tag.min) != tag.min or int(tag.max) != tag.max:
        return None
    domain_size = tag.max - tag.min + 1
    if domain_size > 1000:
        return None
    return tuple(range(int(tag.min), int(tag.max) + 1))


def _domain_for_source_tag(
    tag_name: str,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    known_domains: dict[str, tuple[Any, ...]],
    atom_index: dict[str, list[Atom]] | None = None,
) -> tuple[Any, ...] | None:
    """Resolve the best current finite domain for a source tag."""
    if tag_name in known_domains:
        return known_domains[tag_name]

    tag = graph.tags.get(tag_name)
    if tag is None:
        return None
    # Unwritten internal tags are at their default for fixpoint resolution
    # purposes.  This is broader than the structural seeding (which excludes
    # tags with comparison atoms), but that's fine: this only affects how
    # copy/fill/calc source values propagate through the fixpoint, not what
    # enters known_domains directly.
    if (
        not tag.external
        and tag_name not in graph.writers_of
        and not graph.is_physical_input(tag_name)
    ):
        return (tag.default,)

    domain = _extract_value_domain(
        tag_name,
        tag,
        all_exprs,
        graph.tags,
        known_domains=known_domains,
        graph=graph,
        atom_index=atom_index,
    )
    if domain:
        return domain
    return _declared_domain(tag)


def _domain_from_indirect_source(
    indirect: Any,
    target: Tag,
    graph: ProgramGraph,
    known_domains: dict[str, tuple[Any, ...]],
) -> tuple[Any, ...] | None:
    """Infer a target domain from an indirect-ref copy/fill source.

    Resolves ``block[pointer]`` to its addressable elements using the
    pointer's known finite domain, then unions the element domains:

    - an in-range element with a known domain contributes that domain;
    - an in-range element that is never written contributes its slot
      default (its value is then constant);
    - an out-of-range index faults the copy, which leaves the target
      unwritten — it retains a prior value, so only the target's own
      default is genuinely new (retention is otherwise closed under the
      structural fixed point that unions every writer).

    Returns None when the index set cannot be bounded, or when an in-range
    element is written but has no inferred domain — the caller then leaves
    the source unresolved.
    """
    from pyrung.core.memory_block import IndirectRef

    if not isinstance(indirect, IndirectRef):
        return None
    block = indirect.block
    pointer = indirect.pointer

    ptr_name = getattr(pointer, "name", None)
    index_domain: tuple[Any, ...] | None = None
    if ptr_name is not None and ptr_name in known_domains:
        index_domain = known_domains[ptr_name]
    if not index_domain and pointer is not None:
        index_domain = _declared_domain(pointer)
    if not index_domain:
        return None

    values: set[Any] = set()
    for raw_addr in index_domain:
        if isinstance(raw_addr, bool) or not isinstance(raw_addr, int):
            return None
        if raw_addr < block.start or raw_addr > block.end:
            values.add(target.default)
            continue
        element_name = block._effective_slot_name(raw_addr)
        if element_name in known_domains:
            values.update(known_domains[element_name])
        elif element_name in graph.writers_of:
            return None
        else:
            values.add(block._effective_slot_policy(raw_addr)[1])
        if len(values) > 1000:
            return None
    if not values:
        return None
    return tuple(sorted(values))


def _domain_from_copy_like_value(
    raw_value: Any,
    target: Tag,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    known_domains: dict[str, tuple[Any, ...]],
    atom_index: dict[str, list[Atom]] | None = None,
) -> tuple[Any, ...] | None:
    """Infer a target domain from a copy/fill-style source operand."""
    source_tag_name = _tag_name_from_value(raw_value)
    if source_tag_name is not None:
        return _domain_for_source_tag(source_tag_name, graph, all_exprs, known_domains, atom_index)

    literal = _literal_value_from_value(raw_value)
    if literal is not None:
        stored = _normalize_literal_write_value(literal, target)
        if stored is _NO_LITERAL_WRITE:
            return None
        return (stored,)

    return _domain_from_indirect_source(raw_value, target, graph, known_domains)


def _interval_bounds(
    node: Any,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    known_domains: dict[str, tuple[Any, ...]],
    atom_index: dict[str, list[Atom]] | None = None,
) -> tuple[float, float] | None:
    """Resolve min/max interval for an expression node, or None if unbounded."""
    from pyrung.core.expression import BinaryExpr, RangeAggregateExpr

    if isinstance(node, RangeAggregateExpr):
        # max/min/median/mean all lie in the hull of the member intervals.
        hull: tuple[float, float] | None = None
        for member in node.block_range.tags():
            bounds = _interval_bounds(member, graph, all_exprs, known_domains, atom_index)
            if bounds is None:
                return None
            hull = bounds if hull is None else (min(hull[0], bounds[0]), max(hull[1], bounds[1]))
        return hull

    literal = _literal_value_from_value(node)
    if literal is not None and isinstance(literal, (int, float)):
        return (literal, literal)

    tag_name = _tag_name_from_value(node)
    if tag_name is not None:
        if tag_name in known_domains:
            vals = known_domains[tag_name]
            if vals:
                return (min(vals), max(vals))
            return None
        tag = graph.tags.get(tag_name)
        if tag is not None and tag.choices is not None:
            values = tuple(value for value in tag.choices.keys() if isinstance(value, (int, float)))
            if values:
                return (min(values), max(values))
        if tag is not None and tag.min is not None and tag.max is not None:
            return (tag.min, tag.max)
        return None

    if not isinstance(node, BinaryExpr):
        return None

    left = _interval_bounds(node.left, graph, all_exprs, known_domains, atom_index)
    right = _interval_bounds(node.right, graph, all_exprs, known_domains, atom_index)
    if left is None or right is None:
        return None

    a_lo, a_hi = left
    b_lo, b_hi = right

    if node.symbol == "/" and b_lo <= 0 <= b_hi:
        return None

    try:
        corners = [node.op(a, b) for a in (a_lo, a_hi) for b in (b_lo, b_hi)]
    except (ZeroDivisionError, OverflowError):
        return None
    return (min(corners), max(corners))


def _domain_from_calc_expression(
    expression: Any,
    target: Tag,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    known_domains: dict[str, tuple[Any, ...]],
    atom_index: dict[str, list[Atom]] | None = None,
) -> tuple[Any, ...] | None:
    """Infer a target domain from a supported calc expression shape."""
    from pyrung.core.expression import BinaryExpr

    direct = _domain_from_copy_like_value(
        expression, target, graph, all_exprs, known_domains, atom_index
    )
    if direct is not None:
        return direct

    if isinstance(expression, BinaryExpr) and expression.symbol == "%":
        modulus = _literal_value_from_value(expression.right)
        if isinstance(modulus, int) and not isinstance(modulus, bool) and 0 < modulus <= 1000:
            return tuple(range(modulus))

    bounds = _interval_bounds(expression, graph, all_exprs, known_domains, atom_index)
    if bounds is None:
        return None
    lo, hi = bounds
    from pyrung.core.expression import RangeAggregateExpr
    from pyrung.core.instruction.conversions import _truncate_to_tag_type

    lo_t = _truncate_to_tag_type(lo, target)
    hi_t = _truncate_to_tag_type(hi, target)
    if not isinstance(lo_t, (int, float)) or not isinstance(hi_t, (int, float)):
        return None
    if isinstance(expression, RangeAggregateExpr) and (lo_t != int(lo) or hi_t != int(hi)):
        # The destination wraps part of the member hull (e.g. a DD range into
        # an INT); the stored values are no longer the interval lo..hi.
        return None
    if isinstance(lo_t, int) and isinstance(hi_t, int):
        return tuple(range(lo_t, hi_t + 1))
    return None


def _domain_from_drum_instruction(
    instr: Any,
    target_name: str,
) -> tuple[Any, ...] | None:
    """Infer target domain for drum instruction outputs."""
    step_count = len(instr.pattern)
    if target_name == instr.current_step.name:
        return tuple(range(1, step_count + 1))
    if target_name == instr.completion_flag.name:
        return (False, True)
    for out_tag in instr.outputs:
        if target_name == out_tag.name:
            return (False, True)
    return None


def _domain_from_copy_convert(instr: Any) -> tuple[Any, ...] | None:
    """Infer target domain for copy with a text↔numeric converter."""
    from pyrung.core.copy_converters import CopyConverter

    convert = instr.convert
    if not isinstance(convert, CopyConverter):
        return None
    if convert.mode == "ascii":
        return tuple(range(0, 256))
    if convert.mode == "value":
        return tuple(range(0, 10))
    return None


def _domain_from_search_instruction(
    instr: Any,
    target_name: str,
) -> tuple[Any, ...] | None:
    """Infer target domain for search instruction outputs."""
    if target_name == instr.found.name:
        return (False, True)
    if target_name == instr.result.name:
        from pyrung.core.memory_block import BlockRange

        if isinstance(instr.search_range, BlockRange):
            return (-1, *range(instr.search_range.start, instr.search_range.end + 1))
    return None


def _domain_from_blockcopy(
    instr: Any,
    target_name: str,
    known_domains: dict[str, tuple[Any, ...]],
    reach: Mapping[int, tuple[str, ...]] | None = None,
) -> tuple[Any, ...] | None:
    """Infer target domain for a blockcopy by mapping the corresponding source element.

    With an indexed window on either side (``DS.select(Ptr, Ptr + 1)``) the
    position is unknown, so the target takes any source element's value.
    """
    from pyrung.core.memory_block import IndirectBlockRange
    from pyrung.core.validation._common import _resolve_tag_names

    source_names = _resolve_tag_names(instr.source) or _expand_indirect_tag_names(
        instr.source, reach
    )
    dest_names = _resolve_tag_names(instr.dest) or _expand_indirect_tag_names(instr.dest, reach)
    if not dest_names or target_name not in dest_names:
        return ()
    if isinstance(instr.source, IndirectBlockRange) or isinstance(instr.dest, IndirectBlockRange):
        if not source_names or any(name not in known_domains for name in source_names):
            return None
        values = {value for name in source_names for value in known_domains[name]}
        return tuple(sorted(values, key=repr))
    idx = dest_names.index(target_name)
    if idx >= len(source_names):
        return None
    source_name = source_names[idx]
    if source_name == target_name:
        return known_domains.get(target_name, ())
    return known_domains.get(source_name) if source_name in known_domains else None


def _domain_from_write_instruction(
    instr: Any,
    target_name: str,
    target: Tag,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    known_domains: dict[str, tuple[Any, ...]],
    atom_index: dict[str, list[Atom]] | None = None,
) -> tuple[Any, ...] | None:
    """Infer a target domain from one supported writer instruction."""
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.data_transfer import (
        BlockCopyInstruction,
        CopyInstruction,
        FillInstruction,
    )

    if isinstance(instr, CopyInstruction):
        if instr.convert is not None:
            return _domain_from_copy_convert(instr)
        return _domain_from_copy_like_value(
            instr.source, target, graph, all_exprs, known_domains, atom_index
        )

    if isinstance(instr, FillInstruction):
        return _domain_from_copy_like_value(
            instr.value, target, graph, all_exprs, known_domains, atom_index
        )

    if isinstance(instr, BlockCopyInstruction):
        if instr.convert is not None:
            return None
        return _domain_from_blockcopy(instr, target_name, known_domains, graph.indirect_reach)

    if isinstance(instr, CalcInstruction):
        if instr.dest.name != target_name:
            return None
        return _domain_from_calc_expression(
            instr.expression,
            target,
            graph,
            all_exprs,
            known_domains,
            atom_index,
        )

    from pyrung.core.instruction.drums import (
        EventDrumInstruction,
        TimeDrumInstruction,
        TimeEventDrumInstruction,
    )

    if isinstance(instr, (TimeDrumInstruction, EventDrumInstruction, TimeEventDrumInstruction)):
        return _domain_from_drum_instruction(instr, target_name)

    from pyrung.core.instruction.advanced import SearchInstruction

    if isinstance(instr, SearchInstruction):
        return _domain_from_search_instruction(instr, target_name)

    return None


def _is_monotone_self_feed_calc(instr: Any, target_name: str) -> bool:
    """True for direct non-oneshot ``calc(T +/- k, T)`` progress writes."""
    from pyrung.core.instruction.calc import CalcInstruction

    if not isinstance(instr, CalcInstruction):
        return False
    if getattr(instr, "oneshot", False):
        return False
    if getattr(instr.dest, "name", None) != target_name:
        return False
    fwd = _extract_forward_offset(instr)
    return fwd is not None and fwd[0] == target_name and fwd[1] != 0


def _merge_structural_domain_values(
    target: Tag,
    current_values: tuple[Any, ...],
    candidate_values: set[Any],
) -> tuple[Any, ...] | None:
    merged_values = set(current_values)
    merged_values.update(candidate_values)
    if target.choices is not None and candidate_values <= set(target.choices.keys()):
        merged_values = merged_values & set(target.choices.keys())
    if target.min is not None:
        merged_values = {v for v in merged_values if v >= target.min}
    if target.max is not None:
        merged_values = {v for v in merged_values if v <= target.max}
    if len(merged_values) > 1000:
        return None
    return tuple(sorted(merged_values))


def _collect_structural_domain_info(
    program: Program,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    literal_write_domains: dict[str, tuple[Any, ...]] | None = None,
    discovered_domains: dict[str, tuple[Any, ...]] | None = None,
    *,
    model_system_points: bool = False,
) -> tuple[dict[str, tuple[Any, ...]], frozenset[str]]:
    """Discover finite domains and reverse soundness blockers.

    Returns ``(domains, blockers)`` where *domains* maps tag names to their
    inferred finite value tuples and *blockers* lists source tags with
    unsupported reverse dependencies that could not be safely bounded.

    *model_system_points* (BFS only) treats scan-, clock- and time-derived
    system tags as free inputs: never constant storage, and seeded with their
    calendar range when it is small (``rtc.hour``, ``rtc.minute``, ...).
    """
    from pyrung.core.validation._common import walk_instructions

    profile = _classify_profile_enabled()
    timings: list[tuple[str, float]] = []
    profile_start = profile_last = time.perf_counter() if profile else 0.0

    known_domains = dict(
        literal_write_domains
        or _collect_literal_write_domains(program, graph.tags, graph.indirect_reach)
    )
    if profile:
        profile_last = _classify_profile_mark(timings, "literal_domain_seed", profile_last)

    by_target: dict[str, list[Any]] = {}
    for instr in walk_instructions(program):
        for target_name, _itype in _all_write_targets(instr, graph.indirect_reach):
            by_target.setdefault(target_name, []).append(instr)
    if profile:
        profile_last = _classify_profile_mark(timings, "writer_index", profile_last)

    monotone_self_feed_targets = frozenset(
        target_name
        for target_name, writers in by_target.items()
        if any(_is_monotone_self_feed_calc(instr, target_name) for instr in writers)
    )
    if profile:
        profile_last = _classify_profile_mark(timings, "self_feed_index", profile_last)

    atom_idx = _build_atom_index(all_exprs)
    if profile:
        profile_last = _classify_profile_mark(timings, "atom_index", profile_last)

    # Seed genuinely constant tags into known_domains so the fixpoint and
    # reverse-blocker analysis can resolve them.  Only two narrow cases:
    # (1) readonly tags — constant by annotation, and (2) unwritten internal
    # tags that never influence a comparison — constant storage (e.g. Block
    # slots).  Tags that feed a comparison (HMI inputs, setpoints, and free
    # inputs reaching a comparison through arithmetic/copy data flow) are
    # deliberately excluded so they surface as Intractable when bounds are
    # missing.  ``atom_idx`` only records *direct* comparison participation,
    # so the transitive data-flow closure is used.
    influences_comparison = _comparison_influencing_tags(graph, atom_idx, program)
    if profile:
        profile_last = _classify_profile_mark(timings, "comparison_influence_index", profile_last)
    # Under the BFS system-point model, scan-, clock- and time-derived system
    # tags are free inputs, not constant storage.
    free_system = (
        free_system_tags(graph.tags, graph.writers_of) if model_system_points else frozenset()
    )
    for tag_name in sorted(free_system):
        if tag_name not in known_domains:
            full_range = free_system_domain(tag_name)
            if full_range is not None:
                known_domains[tag_name] = full_range
    for tag_name, tag in graph.tags.items():
        if tag_name in known_domains:
            continue
        if tag.readonly:
            known_domains[tag_name] = (tag.default,)
        elif (
            not tag.external
            and tag_name not in graph.writers_of
            and not graph.is_physical_input(tag_name)
            and tag_name not in influences_comparison
            and tag_name not in free_system
        ):
            known_domains[tag_name] = (tag.default,)
    if profile:
        profile_last = _classify_profile_mark(timings, "constant_seed", profile_last)

    self_feed_boundary_targets: set[str] = set()
    for target_name in monotone_self_feed_targets:
        target = graph.tags.get(target_name)
        if target is None:
            continue
        seed_domains = {
            name: domain for name, domain in known_domains.items() if name != target_name
        }
        domain = _extract_value_domain(
            target_name,
            target,
            all_exprs,
            graph.tags,
            known_domains=seed_domains,
            graph=graph,
            atom_index=atom_idx,
        )
        if not domain:
            continue
        merged = _merge_structural_domain_values(
            target,
            known_domains.get(target_name, ()),
            set(domain),
        )
        if merged is not None:
            known_domains[target_name] = merged
            self_feed_boundary_targets.add(target_name)
    if profile:
        profile_last = _classify_profile_mark(timings, "self_feed_boundary_seed", profile_last)

    changed = True
    fixpoint_iterations = 0
    fixpoint_target_visits = 0
    fixpoint_writer_probes = 0
    fixpoint_domain_changes = 0
    fixpoint_self_feed_skips = 0
    while changed:
        if profile:
            fixpoint_iterations += 1
        changed = False
        for target_name, writers in by_target.items():
            if profile:
                fixpoint_target_visits += 1
            target = graph.tags.get(target_name)
            if target is None or not writers:
                continue

            candidate_values: set[Any] = set()
            for instr in writers:
                if profile:
                    fixpoint_writer_probes += 1
                if target_name in self_feed_boundary_targets and _is_monotone_self_feed_calc(
                    instr, target_name
                ):
                    if profile:
                        fixpoint_self_feed_skips += 1
                    continue
                domain = _domain_from_write_instruction(
                    instr,
                    target_name,
                    target,
                    graph,
                    all_exprs,
                    known_domains,
                    atom_idx,
                )
                if domain is None:
                    candidate_values = set()
                    break
                candidate_values.update(domain)

            if not candidate_values:
                continue

            merged = _merge_structural_domain_values(
                target,
                known_domains.get(target_name, ()),
                candidate_values,
            )
            if merged is None:
                continue
            if known_domains.get(target_name) != merged:
                known_domains[target_name] = merged
                changed = True
                if profile:
                    fixpoint_domain_changes += 1
    if profile:
        profile_last = _classify_profile_mark(timings, "fixpoint", profile_last)

    if discovered_domains is not None:
        for name, domain in discovered_domains.items():
            if name not in known_domains:
                known_domains[name] = domain
    if profile:
        profile_last = _classify_profile_mark(timings, "discovered_domain_merge", profile_last)

    blockers = _backward_propagate_comparison_boundaries(
        program, graph, all_exprs, known_domains, atom_idx
    )
    if profile:
        _classify_profile_mark(timings, "reverse_boundary_blockers", profile_last)
        _classify_profile_emit(
            "structural_domain_info",
            timings,
            time.perf_counter() - profile_start,
            domains=len(known_domains),
            targets=len(by_target),
            writers=sum(len(writers) for writers in by_target.values()),
            self_feed_targets=len(monotone_self_feed_targets),
            self_feed_boundary_targets=len(self_feed_boundary_targets),
            self_feed_skips=fixpoint_self_feed_skips,
            iterations=fixpoint_iterations,
            target_visits=fixpoint_target_visits,
            writer_probes=fixpoint_writer_probes,
            changes=fixpoint_domain_changes,
            blockers=len(blockers),
        )

    return known_domains, blockers


def _collect_structural_domains(
    program: Program,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    literal_write_domains: dict[str, tuple[Any, ...]] | None = None,
) -> dict[str, tuple[Any, ...]]:
    """Discover finite domains from structural writes via fixed-point propagation."""
    domains, _blockers = _collect_structural_domain_info(
        program, graph, all_exprs, literal_write_domains
    )
    return domains


def collect_produced_value_domains(
    program: Program,
    graph: ProgramGraph,
) -> dict[str, tuple[Any, ...]]:
    """Finite values justified solely by program producers.

    Unlike :func:`_collect_structural_domains`, this deliberately does not use
    comparison boundaries to construct a search domain.  It is therefore safe
    for diagnostics that ask whether a literal comparison has any producer in
    the program: accepting the comparison literal itself as domain evidence
    would make that question circular.

    A domain is returned only when every writer of the tag is understood.  The
    fixed point covers literal and named copies, bounded calculations, and
    constant/finite indirect-table reads through the same write-domain helpers
    used by prover classification.  Unknown sources, self-fed calculations, and
    over-cap indirect destinations cause a conservative punt.
    """
    from pyrung.core.analysis.sp_values import _expr_tag_names
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.data_transfer import (
        BlockCopyInstruction,
        CopyInstruction,
        FillInstruction,
    )
    from pyrung.core.memory_block import IndirectRef
    from pyrung.core.validation._common import _resolve_tag_names, walk_instructions

    from .passes import _collect_receive_dest_names

    by_target: dict[str, list[Any]] = {}
    for instr in walk_instructions(program):
        for target_name, _itype in _all_write_targets(instr, graph.indirect_reach):
            by_target.setdefault(target_name, []).append(instr)

    # An over-cap indirect destination is intentionally absent from the normal
    # static target list.  Any materialized slot in that block may still be its
    # runtime target, so none of those slots can carry a complete producer set.
    opaque_dest_blocks = {ref.block for ref in graph.indirect_writes}
    opaque_targets = {
        name
        for name, tag in graph.tags.items()
        if getattr(tag, "_pyrung_block", None) in opaque_dest_blocks
    }
    # Receive payloads are external producers even without an explicit external
    # annotation. The prover write-target helper lists their status outputs,
    # not their payloads; a ladder reset must not become the only known writer.
    incomplete_targets = (
        opaque_targets
        | _collect_receive_dest_names(program)
        | {
            name
            for name, tag in graph.tags.items()
            if graph.writers_of.get(name)
            and (tag.external or tag.readonly or graph.is_physical_input(name))
        }
    )

    known: dict[str, tuple[Any, ...]] = {}
    for name, tag in graph.tags.items():
        if name in incomplete_targets:
            continue
        writers = graph.writers_of.get(name)
        if tag.readonly:
            known[name] = (tag.default,)
        elif not writers:
            declared = _declared_domain(tag)
            if tag.external or graph.is_physical_input(name):
                if declared is not None:
                    known[name] = declared
            else:
                known[name] = (tag.default,)

    def dependencies_are_known(instr: Any, target_name: str) -> bool:
        """Prevent write helpers from falling back to comparison-derived bounds."""
        raw: Any | None = None
        if isinstance(instr, CopyInstruction):
            raw = instr.source
        elif isinstance(instr, FillInstruction):
            raw = instr.value

        if raw is not None:
            source_name = _tag_name_from_value(raw)
            if source_name is not None:
                # An identity self-copy only retains the already-accounted-for
                # value; it is not an unknown producer.
                return source_name == target_name or source_name in known
            if isinstance(raw, IndirectRef):
                pointer_name = getattr(raw.pointer, "name", None)
                return pointer_name is not None and pointer_name in known
            # A literal has no dependency.  Other indirect/expression forms punt.
            return _literal_value_from_value(raw) is not None

        if isinstance(instr, CalcInstruction):
            refs = _expr_tag_names(instr.expression)
            return refs is not None and target_name not in refs and refs <= known.keys()

        if isinstance(instr, BlockCopyInstruction):
            sources = _resolve_tag_names(instr.source)
            return bool(sources) and set(sources) <= known.keys()

        # Drums, searches, and converting copies have intrinsic finite domains;
        # unsupported instruction types will return None below.
        return True

    changed = True
    while changed:
        changed = False
        for target_name, writers in by_target.items():
            if target_name in incomplete_targets or target_name in known:
                continue
            target = graph.tags.get(target_name)
            if target is None:
                continue

            candidate_values: set[Any] = {target.default}
            complete = True
            for instr in writers:
                if not dependencies_are_known(instr, target_name):
                    complete = False
                    break
                # A self-copy contributes no new value beyond retention.
                if isinstance(instr, (CopyInstruction, FillInstruction)):
                    raw = instr.source if isinstance(instr, CopyInstruction) else instr.value
                    if _tag_name_from_value(raw) == target_name:
                        continue
                domain = _domain_from_write_instruction(
                    instr,
                    target_name,
                    target,
                    graph,
                    [],
                    known,
                    {},
                )
                if domain is None:
                    complete = False
                    break
                candidate_values.update(domain)

            if not complete:
                continue
            merged = _merge_structural_domain_values(target, (), candidate_values)
            if merged is not None:
                known[target_name] = merged
                changed = True

    return known


def _expand_indirect_tag_names(
    dest: Any, reach: Mapping[int, tuple[str, ...]] | None = None
) -> list[str]:
    """Expand indirect refs to possible concrete target tag names.

    For IndirectRef, uses pointer min/max/choices to tighten the range.
    For unbounded IndirectRef, IndirectBlockRange, or IndirectExprRef, falls
    back to already-materialized tags in the block (avoids forcing creation
    of all block elements in large blocks).  Past 1000 elements that is none,
    unless *reach* (``ProgramGraph.indirect_reach``) bounds the ref.
    """
    names = _expand_bounded_indirect_tag_names(dest)
    if not names and reach:
        return list(reach.get(id(dest), ()))
    return names


def _expand_bounded_indirect_tag_names(dest: Any) -> list[str]:
    from pyrung.core.memory_block import IndirectBlockRange, IndirectExprRef, IndirectRef

    if isinstance(dest, IndirectRef):
        block = dest.block
        lo = block.start
        hi = block.end
        ptr = dest.pointer
        if ptr.min is not None:
            lo = max(lo, int(ptr.min))
        if ptr.max is not None:
            hi = min(hi, int(ptr.max))
        if lo > hi:
            return []
        if ptr.choices is not None:
            addrs = sorted(int(a) for a in ptr.choices if lo <= int(a) <= hi)
            return [block._get_tag(addr).name for addr in addrs]
        size = hi - lo + 1
        if size > 1000:
            return []
        return [block._get_tag(addr).name for addr in range(lo, hi + 1)]

    if isinstance(dest, (IndirectBlockRange, IndirectExprRef)):
        block = dest.block
        size = block.end - block.start + 1
        if size > 1000:
            return []
        return [block._get_tag(addr).name for addr in range(block.start, block.end + 1)]

    return []


def _unsupported_reverse_sources(
    instr: Any,
    covered_pairs: set[tuple[str, str]],
    reach: Mapping[int, tuple[str, ...]] | None = None,
) -> list[tuple[str, list[str]]]:
    """Identify (source_tag, [target_tags]) for writes not covered by reverse edges.

    Returns pairs where a tag-valued source feeds an instruction whose
    reverse shape is unsupported, so backward propagation cannot invert it.
    """
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.data_transfer import (
        BlockCopyInstruction,
        CopyInstruction,
        FillInstruction,
    )
    from pyrung.core.validation._common import _resolve_tag_names

    if isinstance(instr, CalcInstruction):
        source_name = _tag_name_from_value(instr.expression)
        if source_name is None:
            from pyrung.core.expression import BinaryExpr, UnaryExpr

            if isinstance(instr.expression, UnaryExpr):
                source_name = _tag_name_from_value(instr.expression.operand)
            elif isinstance(instr.expression, BinaryExpr):
                source_name = _tag_name_from_value(instr.expression.left)
                if source_name is None:
                    source_name = _tag_name_from_value(instr.expression.right)
        if source_name is None:
            return []
        target_names = _resolve_tag_names(instr.dest)
        if not target_names:
            target_names = _expand_indirect_tag_names(instr.dest, reach)
        uncovered = [
            tn
            for tn in target_names
            if tn != source_name and (source_name, tn) not in covered_pairs
        ]
        if uncovered:
            return [(source_name, uncovered)]

    elif isinstance(instr, (CopyInstruction, FillInstruction)):
        raw = instr.source if isinstance(instr, CopyInstruction) else instr.value
        source_name = _tag_name_from_value(raw)
        if source_name is None:
            return []
        dest = instr.dest
        target_names = _resolve_tag_names(dest)
        if not target_names:
            target_names = _expand_indirect_tag_names(dest, reach)
        uncovered = [
            tn
            for tn in target_names
            if tn != source_name and (source_name, tn) not in covered_pairs
        ]
        if uncovered:
            return [(source_name, uncovered)]

    elif isinstance(instr, BlockCopyInstruction):
        source_names = _resolve_tag_names(instr.source)
        dest_names = _resolve_tag_names(instr.dest)
        if not source_names:
            source_names = _expand_indirect_tag_names(instr.source, reach)
        if not dest_names:
            dest_names = _expand_indirect_tag_names(instr.dest, reach)
        result: list[tuple[str, list[str]]] = []
        if source_names and dest_names:
            if len(source_names) == len(dest_names):
                for src, dst in zip(source_names, dest_names, strict=True):
                    if src != dst and (src, dst) not in covered_pairs:
                        result.append((src, [dst]))
            else:
                for src in source_names:
                    uncovered = [
                        dst for dst in dest_names if src != dst and (src, dst) not in covered_pairs
                    ]
                    if uncovered:
                        result.append((src, uncovered))
        return result

    return []


def _backward_propagate_comparison_boundaries(
    program: Program,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    known_domains: dict[str, tuple[Any, ...]],
    atom_idx: dict[str, list[Atom]],
) -> frozenset[str]:
    """Propagate comparison boundary values from write targets back to sources.

    Covers ``copy``, ``fill``, ``blockcopy`` (identity), and invertible
    ``calc(expr, target)`` expressions.  When a target has downstream
    comparisons (e.g. ``target == 75``), the boundary values are transformed
    through the inverse and added to the source's domain so the BFS
    enumerates them.

    Returns the set of *reverse soundness blockers* — source tags with
    unsupported reverse dependencies whose domains could not be safely
    bounded by structural or declared fallbacks.
    """
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.data_transfer import (
        BlockCopyInstruction,
        CopyInstruction,
        FillInstruction,
    )
    from pyrung.core.validation._common import _resolve_tag_names, walk_instructions

    reverse_edges: dict[str, list[tuple[str, _InvertFn]]] = {}
    covered_pairs: set[tuple[str, str]] = set()

    for instr in walk_instructions(program):
        if isinstance(instr, CopyInstruction):
            if instr.convert is not None:
                continue
            source_name = _tag_name_from_value(instr.source)
            if source_name is None:
                continue
            target_names = _resolve_tag_names(instr.dest)
            if not target_names:
                target_names = _expand_indirect_tag_names(instr.dest, graph.indirect_reach)
            for target_name in target_names:
                reverse_edges.setdefault(source_name, []).append((target_name, _IDENTITY))
                covered_pairs.add((source_name, target_name))

        elif isinstance(instr, FillInstruction):
            source_name = _tag_name_from_value(instr.value)
            if source_name is None:
                continue
            target_names = _resolve_tag_names(instr.dest)
            if not target_names:
                target_names = _expand_indirect_tag_names(instr.dest, graph.indirect_reach)
            for target_name in target_names:
                reverse_edges.setdefault(source_name, []).append((target_name, _IDENTITY))
                covered_pairs.add((source_name, target_name))

        elif isinstance(instr, BlockCopyInstruction):
            if instr.convert is not None:
                continue
            source_names = _resolve_tag_names(instr.source)
            dest_names = _resolve_tag_names(instr.dest)
            if not dest_names:
                dest_names = _expand_indirect_tag_names(instr.dest, graph.indirect_reach)
            if source_names and dest_names:
                if len(source_names) == len(dest_names):
                    for src, dst in zip(source_names, dest_names, strict=True):
                        reverse_edges.setdefault(src, []).append((dst, _IDENTITY))
                        covered_pairs.add((src, dst))
                else:
                    for src in source_names:
                        for dst in dest_names:
                            reverse_edges.setdefault(src, []).append((dst, _IDENTITY))
                            covered_pairs.add((src, dst))

        elif isinstance(instr, CalcInstruction):
            target_name = _tag_name_from_value(instr.dest)
            if target_name is None:
                continue
            edge = _calc_reverse_edge(instr.expression)
            if edge is not None:
                source_name, invert = edge
                reverse_edges.setdefault(source_name, []).append((target_name, invert))
                covered_pairs.add((source_name, target_name))

    comparison_forms = {"eq", "ne", "lt", "le", "gt", "ge"}

    if reverse_edges:
        target_to_sources: dict[str, list[tuple[str, _InvertFn]]] = {}
        for _src, _edges in reverse_edges.items():
            for _tgt, _inv in _edges:
                target_to_sources.setdefault(_tgt, []).append((_src, _inv))

        comp_boundaries: dict[str, list[Any]] = {}
        for _tag_name in atom_idx:
            for atom in atom_idx[_tag_name]:
                if atom.form not in comparison_forms or atom.operand is None:
                    continue
                if atom.operand_is_tag:
                    continue
                comp_boundaries.setdefault(_tag_name, []).append(atom.operand)

        for comp_tag, boundaries in comp_boundaries.items():
            queue: list[tuple[str, _InvertFn]] = list(target_to_sources.get(comp_tag, []))
            visited: set[str] = {comp_tag}

            while queue:
                source_name, composed_invert = queue.pop(0)
                if source_name in visited:
                    continue
                visited.add(source_name)

                source_tag = graph.tags.get(source_name)
                if source_tag is None:
                    continue

                back_values: set[Any] = set()
                split_points: set[Any] = set()
                for boundary in boundaries:
                    raw = composed_invert(boundary)
                    if not isinstance(raw, (int, float)):
                        continue
                    split_points.add(raw)
                    back_values.add(raw)
                    back_values.add(raw - 1)
                    back_values.add(raw + 1)

                if not back_values:
                    continue

                # A source that also carries its own direct comparisons keeps
                # those boundary partitions: storing known_domains here makes
                # _extract_value_domain short-circuit, so its direct-comparison
                # domain would otherwise be lost.
                for own_boundary in comp_boundaries.get(source_name, ()):
                    if isinstance(own_boundary, (int, float)) and not isinstance(
                        own_boundary, bool
                    ):
                        split_points.add(own_boundary)
                        back_values.add(own_boundary)
                        back_values.add(own_boundary - 1)
                        back_values.add(own_boundary + 1)
                if source_tag.type == TagType.REAL:
                    back_values |= _real_gap_points(split_points)

                existing = set(known_domains.get(source_name, ()))
                merged = existing | back_values
                if source_tag.choices is not None:
                    choices_set = set(source_tag.choices.keys())
                    if not (existing - choices_set):
                        merged = (merged | choices_set) & choices_set
                else:
                    if source_tag.min is not None:
                        merged = {v for v in merged if v >= source_tag.min}
                    if source_tag.max is not None:
                        merged = {v for v in merged if v <= source_tag.max}
                if len(merged) > 1000:
                    continue

                new_domain = tuple(sorted(merged))
                if known_domains.get(source_name) != new_domain:
                    known_domains[source_name] = new_domain

                for next_src, next_inv in target_to_sources.get(source_name, []):
                    if next_src not in visited:
                        queue.append(
                            (
                                next_src,
                                _compose_invert(next_inv, composed_invert),
                            )
                        )

    reverse_soundness_blockers: set[str] = set()
    for instr in walk_instructions(program):
        source_targets = _unsupported_reverse_sources(instr, covered_pairs, graph.indirect_reach)
        for source_name, target_names in source_targets:
            has_downstream_comparison = any(atom_idx.get(tn) for tn in target_names)
            if not has_downstream_comparison:
                continue
            if source_name in known_domains:
                continue
            source_tag = graph.tags.get(source_name)
            if source_tag is None:
                continue
            declared = _declared_domain(source_tag)
            if declared is not None:
                known_domains[source_name] = declared
                continue
            reverse_soundness_blockers.add(source_name)

    return frozenset(reverse_soundness_blockers)


def _extract_value_domain(
    tag_name: str,
    tag: Tag,
    all_exprs: list[Expr],
    all_tags: dict[str, Tag] | None = None,
    literal_write_domains: dict[str, tuple[Any, ...]] | None = None,
    known_domains: Mapping[str, tuple[Any, ...]] | None = None,
    graph: ProgramGraph | None = None,
    atom_index: dict[str, list[Atom]] | None = None,
) -> tuple[Any, ...] | None:
    """Determine the finite value domain for a tag, or None if unbounded."""
    if tag.type == TagType.BOOL:
        return (False, True)

    base_domain: tuple[Any, ...] | None = None
    if known_domains is not None and tag_name in known_domains:
        base_domain = known_domains[tag_name]
    elif literal_write_domains and tag_name in literal_write_domains:
        base_domain = literal_write_domains[tag_name]

    if atom_index is None:
        atom_index = _build_atom_index(all_exprs)
    atoms = _collect_atoms_for_tag(atom_index, tag_name)

    if not atoms:
        return base_domain or ()

    from pyrung.core.expression import Expression

    def _is_literal_operand(atom: Atom) -> bool:
        # An expression operand (``D == In + 5``) is no literal: it changes.
        return (
            atom.operand is not None
            and not atom.operand_is_tag
            and not isinstance(atom.operand, Expression)
        )

    if tag.choices is None and not (tag.min is not None and tag.max is not None):
        eq_ne_literals = {
            atom.operand
            for atom in atoms
            if atom.form in {"eq", "ne"} and _is_literal_operand(atom)
        }
        if (
            eq_ne_literals
            and all(atom.form in {"eq", "ne"} and _is_literal_operand(atom) for atom in atoms)
            and not _has_non_condition_data_read(tag_name, graph)
        ):
            if base_domain is not None:
                return tuple(sorted(set(base_domain) | eq_ne_literals))
            other = _eq_ne_other_value(tag, eq_ne_literals)
            return tuple(sorted(eq_ne_literals | {other}))

    comparison_forms = {"eq", "ne", "lt", "le", "gt", "ge"}
    literals: set[Any] = set()
    unresolved_tag_comparison = False

    for atom in atoms:
        if atom.form in {"truthy", "xic", "xio"}:
            literals.add(0)
            continue
        if atom.form not in comparison_forms or atom.operand is None:
            continue
        other_ref = atom.operand if atom.tag == tag_name else atom.tag
        other_is_tag = atom.operand_is_tag if atom.tag == tag_name else True
        if other_is_tag:
            if other_ref == tag_name:
                continue
            if known_domains is not None and other_ref in known_domains:
                boundary = list(known_domains[other_ref])
            else:
                other = all_tags.get(other_ref) if all_tags is not None else None
                boundary = _boundary_values_for_tag(other) if other is not None else []
            if boundary:
                literals.update(boundary)
            else:
                unresolved_tag_comparison = True
        elif isinstance(other_ref, Expression):
            unresolved_tag_comparison = True
        else:
            literals.add(other_ref)

    if base_domain is not None:
        # Merge comparison literals into the write domain — if the program
        # tests for a value, it belongs in the domain even if never written.
        merged = set(base_domain) | literals
        if tag.type == TagType.REAL:
            merged |= _real_gap_points(literals)
        return tuple(sorted(merged))

    if tag.choices is not None:
        return tuple(sorted(tag.choices.keys()))

    if tag.min is not None and tag.max is not None:
        domain_size = tag.max - tag.min + 1
        if literals:
            literals.add(tag.min)
            literals.add(tag.max)
        elif int(tag.min) != tag.min or int(tag.max) != tag.max:
            literals.add(tag.min)
            literals.add(tag.max)
        elif domain_size > 1000:
            return None
        else:
            return tuple(range(int(tag.min), int(tag.max) + 1))

    if unresolved_tag_comparison and not literals:
        return None

    if not literals:
        return ()

    partitioned: set[Any] = set()
    for lit in literals:
        partitioned.add(lit)
        if isinstance(lit, (int, float)):
            partitioned.add(lit - 1)
            partitioned.add(lit + 1)
    if tag.type == TagType.REAL:
        partitioned |= _real_gap_points(literals)
    if isinstance(tag.default, (int, float)) and not isinstance(tag.default, bool):
        partitioned.add(tag.default)
    if tag.min is not None:
        partitioned = {v for v in partitioned if v >= tag.min}
    if tag.max is not None:
        partitioned = {v for v in partitioned if v <= tag.max}
    return tuple(sorted(partitioned))


def _abstract_acc_domains(program: Program, graph: ProgramGraph) -> dict[str, tuple[Any, ...]]:
    """Value domains of every accumulator under black-box timers.

    The domain only sizes the search (hints, estimates): the kernel holds the
    values.  A timer's untracked accumulator takes the values its tick
    chooses (``blackbox._TimerOracle``): its default, its presets, and 1
    for progress below a preset already reached.  A counter counts
    concretely, up to its preset.
    """
    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.validation._common import walk_instructions

    from .blackbox import timed_sites

    values: dict[str, set[int]] = {}
    for site in timed_sites(program).values():
        values.setdefault(site.acc_name, {0, 1}).update(site.literal_presets)
    for instr in walk_instructions(program):
        if not isinstance(instr, (CountUpInstruction, CountDownInstruction)):
            continue
        preset = instr.preset if isinstance(instr.preset, int) else None
        if preset is not None and abs(preset) <= 1000:
            span = set(range(min(0, preset), max(0, preset) + 1))
        else:
            span = {0, 1} if preset is None else {0, preset}
        values.setdefault(instr.accumulator.name, set()).update(span)
    result: dict[str, tuple[Any, ...]] = {}
    for name, vals in values.items():
        tag = graph.tags.get(name)
        if tag is not None:
            vals = vals | {int(tag.default or 0)}
        result[name] = tuple(sorted(vals))
    return result


def _real_gap_points(points: Iterable[Any]) -> set[float]:
    """Midpoints between split points at most 1 apart, for a REAL.

    The partition stands for the values between two split points with
    ``point + 1`` and ``point - 1``.  A REAL also takes the values between
    split points closer than that (``In > 5, In < 6`` holds at 5.5 only), which
    neither stands for.
    """
    numbers = sorted(
        {float(p) for p in points if isinstance(p, (int, float)) and not isinstance(p, bool)}
    )
    return {(low + high) / 2 for low, high in itertools.pairwise(numbers) if high - low <= 1}


def _with_done_boundary(
    domain: tuple[Any, ...],
    tag: Tag,
    acc_name: str,
    done_acc_info: Any,
    done_by_acc: dict[str, str],
) -> tuple[Any, ...]:
    """Include the paired Done boundary for explicit consumed accumulators."""
    done_name = done_by_acc.get(acc_name)
    if done_name is None:
        return domain
    preset = done_acc_info.presets.get(done_name)
    if preset is None:
        return domain
    kind = done_acc_info.kinds.get(done_name)
    if kind == _DONE_KIND_COUNT_DOWN:
        boundary = preset  # done at Acc <= preset
    elif kind in {_DONE_KIND_COUNT_UP, _DONE_KIND_ON_DELAY, _DONE_KIND_OFF_DELAY}:
        boundary = preset
    else:
        return domain
    if tag.min is not None and boundary < tag.min:
        return domain
    if tag.max is not None and boundary > tag.max:
        return domain
    return tuple(sorted({*domain, boundary}))


def _done_acc_concrete_domain(
    tag: Tag,
    done_name: str,
    done_acc_info: Any,
) -> tuple[Any, ...] | None:
    """Finite exact domain for a Done/Acc accumulator that must stay explicit."""
    preset = done_acc_info.presets.get(done_name)
    if preset is None:
        return None
    kind = done_acc_info.kinds.get(done_name)
    if kind == _DONE_KIND_COUNT_DOWN:
        # Counts down from 0 and is done at Acc <= preset (from the start for
        # a preset >= 0).
        if preset < -1000:
            return None
        values = range(min(preset, 0), 1)
    elif kind in {_DONE_KIND_COUNT_UP, _DONE_KIND_ON_DELAY, _DONE_KIND_OFF_DELAY}:
        if preset < 0 or preset > 1000:
            return None
        values = range(0, preset + 1)
    else:
        return None
    result = set(values)
    result.add(tag.default)
    if tag.min is not None:
        result = {v for v in result if v >= tag.min}
    if tag.max is not None:
        result = {v for v in result if v <= tag.max}
    return tuple(sorted(result))


_BOUNDARY_DOMAIN_CAP = 32


def _compressed_acc_boundary_domain(
    program: Program,
    graph: ProgramGraph,
    tag: Tag,
    acc_name: str,
    done_name: str,
    done_acc_info: Any,
    atom_index: dict[str, list[Atom]],
) -> tuple[Any, ...] | None:
    """Boundary-compressed domain for a consumed accumulator.

    When all comparison atoms for the accumulator can be normalized and
    the accumulator has no forbidden data reads, the domain can be
    compressed to just the comparison boundary values plus the preset
    boundary and default.  The program only distinguishes the accumulator
    through these comparison partitions, so intermediate values are
    indistinguishable.
    """
    source_kinds = _collect_progress_source_kinds(program)
    kind = source_kinds.get(acc_name)
    if kind is None:
        return None

    if _has_forbidden_data_read(program, graph, acc_name):
        return None

    atoms = _collect_atoms_for_tag(atom_index, acc_name)
    boundaries: set[int] = set()
    for atom in atoms:
        specs = _threshold_atom_for_progress(atom, acc_name, graph, kind)
        if not specs:
            return None
        for spec in specs:
            if isinstance(spec.threshold, (int, float)):
                boundaries.add(int(spec.threshold))

    boundaries.add(int(tag.default or 0))
    preset = done_acc_info.presets.get(done_name)
    if preset is not None:
        done_kind = done_acc_info.kinds.get(done_name)
        if done_kind == _DONE_KIND_COUNT_DOWN:
            boundaries.add(preset)  # done at Acc <= preset
        elif done_kind in {_DONE_KIND_COUNT_UP, _DONE_KIND_ON_DELAY, _DONE_KIND_OFF_DELAY}:
            boundaries.add(preset)

    if tag.min is not None:
        boundaries = {v for v in boundaries if v >= tag.min}
    if tag.max is not None:
        boundaries = {v for v in boundaries if v <= tag.max}

    if len(boundaries) < 2:
        return None
    return tuple(sorted(boundaries))


def _expand_consumed_accs_for_reset_timing(
    program: Program,
    graph: ProgramGraph,
    done_acc_info: Any,
    consumed_accs: set[str],
) -> None:
    """Expose hidden Done/Acc progress when its Done timing gates explicit resets."""
    done_by_acc = {acc: done for done, acc in done_acc_info.pairs.items()}

    changed = True
    while changed:
        changed = False
        reset_refs: set[str] = set()
        for acc_name in consumed_accs:
            done_name = done_by_acc.get(acc_name)
            if done_name is None:
                continue
            kind = done_acc_info.kinds.get(done_name)
            if kind is None:
                continue
            reset_refs.update(_owner_reset_condition_refs(program, acc_name, kind))
        if not reset_refs:
            return

        for done_name, acc_name in done_acc_info.pairs.items():
            if acc_name in consumed_accs:
                continue
            downstream = set(graph.downstream_slice(done_name))
            downstream.add(done_name)
            if downstream & reset_refs:
                consumed_accs.add(acc_name)
                changed = True


def _is_ote_only(tag_name: str, graph: ProgramGraph) -> bool:
    """True if every writer of *tag_name* uses OutInstruction."""
    writer_indices = graph.writers_of.get(tag_name, frozenset())
    if not writer_indices:
        return False
    return all(tag_name in graph.rung_nodes[ni].ote_writes for ni in writer_indices)


def _is_ote_unconditionally_reachable(
    tag_name: str,
    graph: ProgramGraph,
) -> bool:
    """True if every OTE writer rung for *tag_name* executes every scan.

    An OTE in the main program always executes.  An OTE inside a subroutine
    only executes when every call site for that subroutine is itself in an
    unconditionally-reachable rung with no condition guard, and no
    ``return_early()`` ahead of it can skip it (``guard_reads``): a skipped
    OTE holds its value.
    """
    writer_indices = graph.writers_of.get(tag_name, frozenset())
    for ni in writer_indices:
        node = graph.rung_nodes[ni]
        if node.guard_reads:
            return False
        if node.subroutine is not None:
            has_unconditional_call = False
            for _caller_ni, caller_node in enumerate(graph.rung_nodes):
                if node.subroutine not in caller_node.calls:
                    continue
                if caller_node.subroutine is not None:
                    return False
                if caller_node.condition_reads:
                    return False
                has_unconditional_call = True
            if not has_unconditional_call:
                return False
    return True


def _is_self_referencing_ote(
    tag_name: str,
    graph: ProgramGraph,
) -> bool:
    """True if any OTE writer rung for *tag_name* reads the tag in its condition."""
    writer_indices = graph.writers_of.get(tag_name, frozenset())
    for ni in writer_indices:
        if tag_name in graph.rung_nodes[ni].ote_writes:
            if tag_name in graph.rung_nodes[ni].condition_reads:
                return True
    return False


def _reads_scan_entry_value(tag_name: str, graph: ProgramGraph) -> bool:
    """True if a read of *tag_name* comes before its first write in scan order.

    Such a read sees the value the tag ended the previous scan with, so the
    tag carries state across the scan boundary even when every writer is an
    unconditional OTE: ``Rung(Y2): out(Y3)`` above ``Rung(Y1): out(Y2)``.
    """
    chain = graph.def_use_chains.get(tag_name)
    return bool(chain) and bool(chain[0].read_by)


def _uses_prior_snapshot(program: Program, node: RungNode) -> bool:
    """Return whether a graph node belongs to a continued() top-level rung."""
    if node.scope == "main":
        return program.rungs[node.rung_index]._use_prior_snapshot
    assert node.subroutine is not None
    return program.subroutines[node.subroutine][node.rung_index]._use_prior_snapshot


def _has_continued_reader(
    program: Program,
    tag_name: str,
    graph: ProgramGraph,
) -> bool:
    """True if any read of *tag_name* comes from a continued() rung."""
    return any(
        _uses_prior_snapshot(program, graph.rung_nodes[reader_idx])
        for reader_idx in graph.readers_of.get(tag_name, frozenset())
    )


def _has_instruction_snapshot_reader(program: Program, tag_name: str) -> bool:
    """True when an instruction-internal helper condition snapshots *tag_name*.

    These conditions are evaluated from rung-entry values. An OTE-written tag
    feeding one of them therefore carries cross-scan information even if its
    ordinary rung readers are combinational.
    """
    from pyrung.core.analysis.pdg import _extract_tag_names
    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.instruction.timers import OnDelayInstruction
    from pyrung.core.validation._common import walk_instructions

    for instr in walk_instructions(program):
        conditions: list[Any] = []
        if isinstance(instr, OnDelayInstruction):
            conditions.append(instr.reset_condition)
        elif isinstance(instr, CountUpInstruction):
            conditions.extend((instr.reset_condition, instr.down_condition))
        elif isinstance(instr, CountDownInstruction):
            conditions.append(instr.reset_condition)
        for condition in conditions:
            if condition is not None and tag_name in _extract_tag_names(condition, {}):
                return True
    return False


def _has_same_rung_branch_reader(
    tag_name: str,
    graph: ProgramGraph,
) -> bool:
    """True if *tag_name* is read by a branch condition in the same top-level rung that writes it."""
    writer_indices = graph.writers_of.get(tag_name, frozenset())
    reader_indices = graph.readers_of.get(tag_name, frozenset())
    for w in writer_indices:
        w_rung = graph.rung_nodes[w].rung_index
        for r in reader_indices:
            if r != w and graph.rung_nodes[r].rung_index == w_rung:
                return True
    return False


_ClassifyResult = tuple[
    dict[str, tuple[Any, ...]],  # stateful_dims
    dict[str, tuple[Any, ...]],  # nondeterministic_dims
    frozenset[str],  # combinational_tags
    dict[str, str],  # done_acc_pairs: done_tag → acc_tag
    dict[str, int],  # done_presets: done_tag → constant preset value
    dict[str, str],  # done_kinds: done_tag → timer/counter instruction kind
]

_KIND_LABELS: dict[str, str] = {
    _DONE_KIND_ON_DELAY: "on-delay timer",
    _DONE_KIND_OFF_DELAY: "off-delay timer",
    _DONE_KIND_COUNT_UP: "count-up counter",
    _DONE_KIND_COUNT_DOWN: "count-down counter",
    _DONE_KIND_TIME_DRUM: "time drum",
    _PROGRESS_KIND_INT_UP: "integer progress",
    _PROGRESS_KIND_INT_DOWN: "descending integer progress",
    _PROGRESS_KIND_REAL_UP: "real-valued progress",
    _PROGRESS_KIND_REAL_DOWN: "descending real-valued progress",
}


def _unmodeled_accumulators(program: Program) -> dict[str, str]:
    """Accumulators of instructions the prover has no exact model for, by verb."""
    from pyrung.core.instruction.drums import TimeDrumInstruction, TimeEventDrumInstruction
    from pyrung.core.instruction.out_variants import (
        DebounceInstruction,
        FlasherInstruction,
        TimedInstruction,
    )
    from pyrung.core.validation._common import walk_instructions

    out: dict[str, str] = {}
    for instr in walk_instructions(program):
        if isinstance(instr, TimeEventDrumInstruction):
            out[instr.accumulator.name] = "time_event_drum"
        elif isinstance(instr, TimeDrumInstruction):
            # It times steps under a completion flag that is on.
            reason = _drum_times_under_its_flag(instr, program)
            if reason is not None:
                out[instr.accumulator.name] = f"time_drum ({reason})"
        elif isinstance(instr, (DebounceInstruction, FlasherInstruction)):
            verb = "debounce" if isinstance(instr, DebounceInstruction) else "flasher"
            out[instr.on_acc.name] = verb
            out[instr.off_acc.name] = verb
        elif isinstance(instr, TimedInstruction):
            out[instr.timer_acc.name] = "timed"
    return out


def _build_infeasible_hints(
    infeasible_tags: list[str],
    graph: ProgramGraph,
    threshold_blockers: dict[str, _ThresholdBlocker] | None = None,
    unmodeled_accs: dict[str, str] | None = None,
    tag_compared: Mapping[str, str] | None = None,
) -> list[str]:
    """Generate actionable hints for each infeasible tag.

    *tag_compared* names the inputs refused because they are compared with a
    changing value (``_tag_compared_names``), with what they are compared to.
    """
    blockers = threshold_blockers or {}
    unmodeled = unmodeled_accs or {}
    compared = tag_compared or {}
    hints: list[str] = []
    for name in infeasible_tags:
        blocker = blockers.get(name)
        if name in unmodeled:
            hints.append(
                f"  {name}: {unmodeled[name]} accumulator; how()'s domain prior does"
                f" not model its timing"
            )
            continue
        if blocker is not None:
            label = _KIND_LABELS.get(blocker.kind, "accumulator")
            hints.append(f"  {name}: {label}; threshold abstraction blocked:")
            for reason in blocker.reasons:
                hints.append(f"    {reason}")
            continue
        tag = graph.tags.get(name)
        if name in compared:
            why = (
                f"compared with a changing value ({compared[name]}), so every value it"
                f" can hold is explored"
            )
            cap = _TAG_COMPARED_DOMAIN_CAP
            if tag is not None and tag.type == TagType.REAL:
                hints.append(f"  {name}: REAL {why}, and a REAL's values cannot be listed")
            elif tag is not None and tag.min is not None and tag.max is not None:
                hints.append(
                    f"  {name}: range {tag.min}..{tag.max} ({int(tag.max - tag.min + 1)}"
                    f" values); too wide: it is {why}; narrow min=/max= to {cap} values"
                    f" at most, or add choices="
                )
            else:
                hints.append(
                    f"  {name}: no domain constraint; it is {why}; add choices=, or"
                    f" min=/max= spanning {cap} values at most"
                )
            continue
        ptr_info = graph.pointer_tags.get(name)
        if is_randint_tag(name):
            site = randint_sites((name,))
            spelled = repr(site[0]) if site else "randint()"
            hints.append(
                f"  {name}: draw of {spelled} with tag bounds; give it literal bounds"
                f", or declare min=/max= on the bound tags and narrow scope="
            )
        elif name in SYSTEM_TAGS_BY_NAME:
            hints.append(
                f"  {name}: system tag explored as a free input with no finite range"
                f"; compare it with literals where it is read (e.g. {name} >= 2030)"
                f" instead of copying it into arithmetic, or narrow scope="
            )
        elif ptr_info is not None:
            block_name, start, end = ptr_info
            lo, hi = _pointer_span(graph, name, start, end)
            hints.append(
                f"  {name}: pointer into {block_name}[{start}..{end}]"
                f"; add choices=, min={lo}/max={hi}, or readonly=True"
            )
        elif tag is not None and tag.min is not None and tag.max is not None:
            hints.append(
                f"  {name}: range {tag.min}..{tag.max} ({int(tag.max - tag.min + 1)} values)"
                f"; too wide; add choices=, narrow min=/max=, or readonly=True"
            )
        else:
            hints.append(
                f"  {name}: no domain constraint"
                f"; add choices= for discrete values, or cover with dt= testing"
            )
    return hints


def _pointer_span(graph: ProgramGraph, name: str, start: int, end: int) -> tuple[int, int]:
    """The pointer values of *name*'s first use that land in ``start..end``."""
    for use in graph.pointer_uses:
        if use.pointer == name:
            return use.pointer_span()
    return start, end


def _build_dimension_hints(context: _ExploreContext) -> list[str]:
    """Summarise the largest dimensions when max_states is exceeded."""
    dims: list[tuple[str, int]] = []
    for name, domain in context.stateful_dims.items():
        dims.append((name, len(domain)))
    for name, domain in context.nondeterministic_dims.items():
        dims.append((name, len(domain)))
    dims.sort(key=lambda x: x[1], reverse=True)
    product = 1
    for _, size in dims:
        product *= size
    hints = [f"  state space: {product:,} combinations across {len(dims)} dimensions"]
    for name, size in dims[:10]:
        ptr_info = context.graph.pointer_tags.get(name)
        suffix = ""
        if ptr_info is not None:
            block_name, start, end = ptr_info
            suffix = f" (pointer into {block_name}[{start}..{end}])"
        hints.append(f"  {name}: {size} values{suffix}")
    if len(dims) > 10:
        hints.append(f"  ... and {len(dims) - 10} more")
    hints.append("Constrain the largest dimensions with choices=, min=/max=, or readonly=True")
    return hints


def _classify_dimensions_from_graph(
    program: Program,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    *,
    scope: list[str] | None = None,
    project: tuple[str, ...] | None = None,
    discovered_domains: dict[str, tuple[Any, ...]] | None = None,
    structural_domain_info: tuple[dict[str, tuple[Any, ...]], frozenset[str]] | None = None,
    receive_dest_names: frozenset[str] = frozenset(),
    exclusions: dict[str, str] | None = None,
    unclassified: set[str] | None = None,
    stepping_tags_out: set[str] | None = None,
    threshold_absorptions_out: list[_ThresholdAbsorptions] | None = None,
    model_system_points: bool = False,
    observe_everything: bool = False,
    abstract_timers: bool,
    _skip_absorptions: bool,
    exhaust_tag_compared_inputs: bool = True,
) -> _ClassifyResult | Intractable:
    """Classify dimensions using prebuilt graph/expression context.

    *abstract_timers* classifies for the black-box timer kernel
    (``blackbox.py``): a Done bit is a plain Bool, and every accumulator
    (timer, counter, drum, Out timing) is state the search tracks itself.
    *_skip_absorptions* skips the accumulator absorptions.  The search
    passes True for both; only how()'s domain prior (``domains_only``)
    passes False, for the absorbing classification its world key reads
    (``absorb.py``).  Both are required so no caller gets the prior's
    classification by default.

    *model_system_points* applies the BFS system-point model
    (:mod:`.system_points`): ``sys.first_scan`` becomes a stateful bit and
    scan-, time- and clock-derived system tags become nondeterministic
    inputs.  Without it, unwritten system tags are left out (the ``how()``
    domain prior).

    *structural_domain_info* is the ``(domains, blockers)`` pair from
    :func:`_collect_structural_domain_info`; pass it to reuse a fixpoint already
    computed by another pass (only valid when *discovered_domains* is None, since
    that input feeds the fixpoint).

    *project* holds every tag read from outside the program: the projection
    and what callable properties read (``passes._observed_project``).  Their
    accumulators are tracked exactly, and one that accumulates from its own
    value but has no domain is infeasible.  *observe_everything* marks a
    callable without ``scope=`` (every tag is in *project*): it keeps
    accumulators exact but leaves domainless tags unclassified rather than
    refusing every such program.

    *exhaust_tag_compared_inputs* gives an input compared with a changing
    value (``_tag_compared_names``) every value it can hold, or refuses it,
    instead of the expression partition, which does not cover it.  The
    ``how()`` prior turns it off: its domains steer, they prove nothing.
    """
    profile = _classify_profile_enabled()
    timings: list[tuple[str, float]] = []
    profile_start = profile_last = time.perf_counter() if profile else 0.0

    def emit_profile(result: str, **meta: Any) -> None:
        if not profile:
            return
        _classify_profile_emit(
            "classify_dimensions_from_graph",
            timings,
            time.perf_counter() - profile_start,
            result=result,
            **meta,
        )

    if stepping_tags_out is not None:
        stepping_tags_out.update(_compute_stepping_tags(program, graph))
    if profile:
        profile_last = _classify_profile_mark(timings, "stepping_tags", profile_last)
    done_acc_info = _collect_done_acc_pairs(program)
    if profile:
        profile_last = _classify_profile_mark(timings, "done_acc_pairs", profile_last)
    # Always needed below (feeds _extract_value_domain); cheap to recompute.
    literal_write_domains = _collect_literal_write_domains(
        program, graph.tags, graph.indirect_reach
    )
    if profile:
        profile_last = _classify_profile_mark(timings, "literal_write_domains", profile_last)
    if structural_domain_info is not None and discovered_domains is None:
        # Reuse the fixpoint another pass already ran — it's the dominant cost.
        structural_domains, reverse_blockers = structural_domain_info
        if profile:
            profile_last = _classify_profile_mark(
                timings, "structural_domain_info_reuse", profile_last
            )
    else:
        structural_domains, reverse_blockers = _collect_structural_domain_info(
            program,
            graph,
            all_exprs,
            literal_write_domains,
            discovered_domains,
            model_system_points=model_system_points,
        )
        if profile:
            profile_last = _classify_profile_mark(
                timings, "structural_domain_info_collect", profile_last
            )
    known_domains = dict(structural_domains)

    for ptr_name in graph.pointer_tags:
        if ptr_name in known_domains:
            continue
        tag = graph.tags.get(ptr_name)
        if tag is None or tag.readonly:
            continue
        declared = _declared_domain(tag)
        if declared is not None:
            known_domains[ptr_name] = declared
            continue
        # The pointer values that land inside each block it indexes:
        # DS[Ptr + 2] into DS1..4 needs Ptr in -1..2.
        values = set()
        for use in graph.pointer_uses:
            if use.pointer != ptr_name:
                continue
            lo, hi = use.pointer_span()
            values.update(range(lo, hi + 1))
            if len(values) > 1000:
                break
        if not values or len(values) > 1000:
            continue
        values.add(tag.default)
        known_domains[ptr_name] = tuple(sorted(values))
    if profile:
        profile_last = _classify_profile_mark(timings, "pointer_domains", profile_last)

    atom_idx = _build_atom_index(all_exprs)
    if profile:
        profile_last = _classify_profile_mark(timings, "atom_index", profile_last)
    influences_comparison = _comparison_influencing_tags(graph, atom_idx, program)
    if profile:
        profile_last = _classify_profile_mark(timings, "comparison_influence_index", profile_last)

    consumed_accs: set[str] = set()
    observed = frozenset(project or ())
    for acc_name in done_acc_info.pairs.values():
        # Projected, or read by a callable property: tracked exactly.
        if (
            acc_name in observed
            or atom_idx.get(acc_name)
            or _has_forbidden_data_read(
                program,
                graph,
                acc_name,
            )
        ):
            consumed_accs.add(acc_name)
    if profile:
        profile_last = _classify_profile_mark(timings, "consumed_acc_scan", profile_last)

    if _skip_absorptions:
        absorptions = _RedundantAccAbsorptions(
            acc_names=frozenset(),
            preset_tags=frozenset(),
            synthetic_presets={},
        )
        threshold_absorptions = _ThresholdAbsorptions(
            progress_names=frozenset(),
            threshold_tags=frozenset(),
            comparison_tags=frozenset(),
            vector_specs=(),
        )
        if profile:
            profile_last = _classify_profile_mark(timings, "absorptions_skipped", profile_last)
    else:
        absorptions = _find_redundant_acc_absorptions(
            program,
            graph,
            all_exprs,
            done_acc_info,
            consumed_accs,
            atom_idx,
        )
        consumed_accs.difference_update(absorptions.acc_names)
        if profile:
            profile_last = _classify_profile_mark(
                timings, "redundant_acc_absorptions", profile_last
            )

        threshold_absorptions = _find_threshold_absorptions(
            program,
            graph,
            all_exprs,
            project=project,
            atom_index=atom_idx,
        )
        if profile:
            profile_last = _classify_profile_mark(timings, "threshold_absorptions", profile_last)
        comparison_absorptions = _find_comparison_absorptions(
            program,
            graph,
            all_exprs,
            structural_domains,
            project=project,
            receive_dest_names=receive_dest_names,
            atom_index=atom_idx,
            stable_partners_only=model_system_points,
        )
        if profile:
            profile_last = _classify_profile_mark(timings, "comparison_absorptions", profile_last)
        threshold_absorptions = _merge_threshold_absorptions(
            threshold_absorptions,
            comparison_absorptions,
        )
        consumed_accs.difference_update(threshold_absorptions.progress_names)
        if profile:
            profile_last = _classify_profile_mark(timings, "absorption_merge", profile_last)

    if not abstract_timers:
        # Exact-timing only: under black-box timers a reset's timing is a
        # choice like any other, and the accumulator is tracked anyway.
        _expand_consumed_accs_for_reset_timing(program, graph, done_acc_info, consumed_accs)
    if profile:
        profile_last = _classify_profile_mark(timings, "reset_timing_expansion", profile_last)
    if threshold_absorptions_out is not None:
        threshold_absorptions_out.append(threshold_absorptions)

    done_by_acc = {a: d for d, a in done_acc_info.pairs.items()}
    done_acc = {d: a for d, a in done_acc_info.pairs.items() if a not in consumed_accs}
    unconsumed_accs = frozenset(done_acc.values())

    scope_input_tags: frozenset[str] | None = None
    if scope is not None:
        # Under the model an unwritten system tag is an input even when only
        # the property reads it (no rung, so no graph role).
        _is_nd_input = lambda tn: (
            graph.tag_roles.get(tn) is TagRole.INPUT
            or tn in receive_dest_names
            or (model_system_points and tn in SYSTEM_TAGS_BY_NAME and tn not in graph.writers_of)
        )
        upstream_tags: set[str] = set()
        for tag_name in scope:
            if _is_nd_input(tag_name):
                upstream_tags.add(tag_name)
            upstream_tags.update(tag for tag in graph.upstream_slice(tag_name) if _is_nd_input(tag))
        for expr in all_exprs:
            upstream_tags.update(
                tag_name for tag_name in _referenced_tags(expr) if _is_nd_input(tag_name)
            )
        scope_input_tags = frozenset(upstream_tags)
    if profile:
        profile_last = _classify_profile_mark(timings, "scope_input_filter", profile_last)

    stateful: dict[str, tuple[Any, ...]] = {}
    nondeterministic: dict[str, tuple[Any, ...]] = {}
    combinational: set[str] = set()
    infeasible_tags: list[str] = []
    unmodeled_accs = frozenset(_unmodeled_accumulators(program))
    unread_timers = (
        _unread_timer_done_tags(program, graph, done_acc, scope=scope, project=project)
        if model_system_points and (scope is not None or project is not None)
        else frozenset()
    )

    unread_timer_accs = frozenset(done_acc[name] for name in unread_timers if name in done_acc)
    abstract_acc_domains = _abstract_acc_domains(program, graph) if abstract_timers else {}
    if abstract_timers:
        from .blackbox import timer_held_tags

        timer_held = timer_held_tags(program)
    else:
        timer_held = frozenset()

    free_system = (
        free_system_tags(graph.tags, graph.writers_of) if model_system_points else frozenset()
    )
    # Unwritten internal tags that never influence a comparison are constant
    # storage (Block slots, unused tags).  Everything else -- external inputs,
    # HMI tags used in conditions, receive destinations, and free inputs that
    # feed a comparison through arithmetic/copy data flow -- is genuinely
    # under-specified and must surface as Intractable (or heuristic seeding)
    # so the user adds bounds.  ``atom_idx`` only records *direct* comparison
    # participation, so the transitive data-flow closure is used.
    constant_storage = frozenset(
        name
        for name, tag in graph.tags.items()
        if name not in graph.writers_of
        and not tag.external
        and not graph.is_physical_input(name)
        and name not in receive_dest_names
        and name not in influences_comparison
        and name not in free_system
    )
    tag_compared: dict[str, str] = {}
    exact_partners: dict[str, frozenset[str]] = {}
    exact_values: dict[str, frozenset[Any]] = {}
    compared_groups: tuple[frozenset[str], ...] = ()
    index_only: frozenset[str] = frozenset()
    if exhaust_tag_compared_inputs:
        exact_values = _exact_value_sets(program, graph, receive_dest_names, constant_storage)
        free_inputs = frozenset(
            name
            for name, tag in graph.tags.items()
            if name not in constant_storage
            and not tag.readonly
            and tag.type in (TagType.INT, TagType.DINT, TagType.WORD, TagType.REAL)
            and (
                graph.tag_roles.get(name) is TagRole.INPUT
                or name not in graph.writers_of
                or name in receive_dest_names
            )
        )
        tag_compared, exact_partners, compared_groups, index_only = _tag_compared_names(
            program, graph, exact_values, exprs=all_exprs, free_inputs=free_inputs
        )
        if profile:
            profile_last = _classify_profile_mark(timings, "tag_compared_index", profile_last)
    tag_compared_refused: dict[str, str] = {}
    first_scan_tags = frozenset({FIRST_SCAN}) | FIRST_SCAN_DEPENDENTS
    for tag_name, tag in graph.tags.items():
        if tag_name in free_system:
            tag = bounded_system_tag(tag)
        if not model_system_points and tag_name in FAULT_FLAGS:
            # The how() prior keeps fault flags out, as before they had writers.
            continue
        if tag.readonly:
            if exclusions is not None:
                exclusions[tag_name] = "readonly"
            continue

        if tag_name in unconsumed_accs:
            if abstract_timers and tag_name not in unread_timer_accs:
                stateful[tag_name] = abstract_acc_domains.get(tag_name, (0, 1))
                continue
            if exclusions is not None:
                exclusions[tag_name] = "unconsumed_acc"
            continue
        if tag_name in absorptions.preset_tags:
            if exclusions is not None:
                exclusions[tag_name] = "preset_tag"
            continue
        if tag_name in threshold_absorptions.progress_names:
            if exclusions is not None:
                exclusions[tag_name] = "progress_acc"
            continue
        if tag_name in threshold_absorptions.threshold_tags:
            if exclusions is not None:
                exclusions[tag_name] = "threshold_tag"
            continue
        if tag_name in threshold_absorptions.comparison_tags and tag_name not in receive_dest_names:
            if exclusions is not None:
                exclusions[tag_name] = "comparison_only"
            continue

        role = graph.tag_roles.get(tag_name)
        is_written = tag_name in graph.writers_of

        if tag_name in SYSTEM_TAGS_BY_NAME and not is_written:
            if model_system_points and tag_name in first_scan_tags:
                # first_scan, and the scan-time min/max that differ on scan 0
                # only, ride on one stateful bit (see system_points).
                if scope_input_tags is None or scope_input_tags & first_scan_tags:
                    stateful[FIRST_SCAN] = (False, True)
                continue
            if tag_name not in free_system:
                continue
        elif model_system_points and tag_name in SCAN_START_RESETS and tag_name not in free_system:
            # Reset before every scan, so no scan reads a previous scan's value.
            combinational.add(tag_name)
            continue

        if role == TagRole.INPUT or not is_written or tag_name in receive_dest_names:
            if scope_input_tags is not None and tag_name not in scope_input_tags:
                if exclusions is not None:
                    exclusions[tag_name] = "out_of_scope_nd"
                continue
            if tag_name in tag_compared and tag_name not in constant_storage:
                # No partition covers it (see _tag_compared_names): every value.
                domain = _every_value_domain(tag)
                if domain is None and tag_name in index_only and tag_name in known_domains:
                    # Only an index: the values that land in its blocks.
                    domain = known_domains[tag_name]
                if domain is None:
                    infeasible_tags.append(tag_name)
                    tag_compared_refused[tag_name] = tag_compared[tag_name]
                    continue
            else:
                tag_known_domains: Mapping[str, tuple[Any, ...]] = known_domains
                if tag_name in exact_partners:
                    # Every value of its exact partners is a split point.
                    tag_known_domains = ChainMap(
                        {
                            partner: tuple(
                                set(known_domains.get(partner, ())) | exact_values[partner]
                            )
                            for partner in exact_partners[tag_name]
                        },
                        known_domains,
                    )
                domain = _extract_value_domain(
                    tag_name,
                    tag,
                    all_exprs,
                    graph.tags,
                    literal_write_domains,
                    tag_known_domains,
                    graph,
                    atom_idx,
                )
            if not domain:
                declared = _declared_domain(tag)
                if declared is not None:
                    domain = declared
            if not domain and _has_non_condition_data_read(tag_name, graph):
                declared = _declared_domain(tag)
                if declared is not None:
                    domain = declared
            if not domain and tag_name in free_system:
                domain = free_system_domain(tag_name) or ()
            if not domain:
                # Constant storage keeps its default (see constant_storage).
                if tag_name in constant_storage:
                    domain = (tag.default,)
                else:
                    infeasible_tags.append(tag_name)
                    continue
            if tag_name in free_system:
                domain = clip_to_range(tag_name, tuple(domain))
            if tag.default not in domain and tag_name not in free_system:
                # The power-on default is always a reachable initial value that
                # BFS must cover, but only when it respects declared constraints.
                # A free system tag has no power-on value of its own.
                default_valid = True
                if tag.min is not None and tag.default < tag.min:
                    default_valid = False
                elif tag.max is not None and tag.default > tag.max:
                    default_valid = False
                elif tag.choices is not None and tag.default not in tag.choices:
                    default_valid = False
                if default_valid:
                    domain = tuple(sorted(set(domain) | {tag.default}))
            nondeterministic[tag_name] = domain
            continue

        if not is_written:
            if exclusions is not None:
                exclusions[tag_name] = "unwritten_internal"
            continue

        if abstract_timers and tag_name in timer_held and is_written:
            # A drum's completion flag, an Out timing coil's target: held by
            # the instruction from scan to scan.
            stateful[tag_name] = (False, True)
            continue

        if abstract_timers and tag_name in done_acc_info.pairs and tag_name not in done_acc:
            # A Done whose accumulator is compared: still state (a retentive
            # or off-delay timer reads it back, a timer not run holds it).
            stateful[tag_name] = (False, True)
            continue

        if tag_name in done_acc:
            if tag_name in unread_timers:
                # Nothing reads its Done, accumulator or status bits, and
                # nothing outside the program observes them: like any unread
                # tag it changes no other state, so it is left out of the key.
                combinational.add(tag_name)
                if exclusions is not None:
                    exclusions[tag_name] = "unread_timer"
                continue
            stateful[tag_name] = (False, True) if abstract_timers else (False, PENDING, True)
            continue

        if tag_name in done_acc_info.pairs.values() and tag_name in consumed_accs:
            if atom_idx.get(tag_name):
                domain = _extract_value_domain(
                    tag_name,
                    tag,
                    all_exprs,
                    graph.tags,
                    literal_write_domains,
                    known_domains,
                    graph,
                    atom_idx,
                )
                if domain:
                    stateful[tag_name] = _with_done_boundary(
                        domain,
                        tag,
                        tag_name,
                        done_acc_info,
                        done_by_acc,
                    )
                elif tag_name in known_domains:
                    stateful[tag_name] = _with_done_boundary(
                        known_domains[tag_name],
                        tag,
                        tag_name,
                        done_acc_info,
                        done_by_acc,
                    )
                else:
                    done_name = done_by_acc.get(tag_name)
                    if done_name is not None:
                        domain = _done_acc_concrete_domain(tag, done_name, done_acc_info)
                    else:
                        domain = None
                    if domain is not None:
                        stateful[tag_name] = _with_done_boundary(
                            domain,
                            tag,
                            tag_name,
                            done_acc_info,
                            done_by_acc,
                        )
                    else:
                        infeasible_tags.append(tag_name)
            elif tag_name in known_domains:
                stateful[tag_name] = _with_done_boundary(
                    known_domains[tag_name],
                    tag,
                    tag_name,
                    done_acc_info,
                    done_by_acc,
                )
            else:
                done_name = done_by_acc.get(tag_name)
                if done_name is not None:
                    domain = _done_acc_concrete_domain(tag, done_name, done_acc_info)
                else:
                    domain = None
                if domain is not None and len(domain) > _BOUNDARY_DOMAIN_CAP:
                    compressed = (
                        _compressed_acc_boundary_domain(
                            program,
                            graph,
                            tag,
                            tag_name,
                            done_name,
                            done_acc_info,
                            atom_idx,
                        )
                        if done_name is not None
                        else None
                    )
                    if compressed is not None:
                        domain = compressed
                if domain is None and done_name is not None:
                    domain = _compressed_acc_boundary_domain(
                        program,
                        graph,
                        tag,
                        tag_name,
                        done_name,
                        done_acc_info,
                        atom_idx,
                    )
                if domain is not None:
                    stateful[tag_name] = domain
                else:
                    infeasible_tags.append(tag_name)
            continue

        if tag_name in unmodeled_accs and abstract_timers:
            # A black-box timer like the others: its tick is a choice.
            stateful[tag_name] = abstract_acc_domains.get(tag_name, (0, 1))
            continue
        if tag_name in unmodeled_accs:
            # Its timing is not modeled: the carry (``_frac:``) is in no state
            # key, so even a declared domain must not let the search track it
            # as plain state.  It times whether or not anything reads it.
            if unclassified is not None:
                unclassified.add(tag_name)
            else:
                infeasible_tags.append(tag_name)
            continue

        # An accumulator its own instruction reads back (a time & event drum's)
        # carries across scans; only the paired ones above are absorbed.
        self_read = tag_name in graph.all_readers_of and tag_name not in done_by_acc
        if (
            tag_name not in graph.readers_of
            and not self_read
            and not (
                _tag_is_observable(tag_name, scope=scope, project=project)
                and (
                    _has_inert_writer(program, tag_name, graph.indirect_reach)
                    or _has_out_oneshot_writer(program, tag_name)
                    or _has_skippable_coil_writer(program, tag_name, graph)
                )
            )
        ):
            combinational.add(tag_name)
            continue

        if (
            _is_ote_only(tag_name, graph)
            and not _has_continued_reader(program, tag_name, graph)
            and not _has_instruction_snapshot_reader(program, tag_name)
            and _is_ote_unconditionally_reachable(tag_name, graph)
            and not _is_self_referencing_ote(tag_name, graph)
            and not _has_same_rung_branch_reader(tag_name, graph)
            and not _reads_scan_entry_value(tag_name, graph)
        ):
            combinational.add(tag_name)
            continue

        domain = _extract_value_domain(
            tag_name,
            tag,
            all_exprs,
            graph.tags,
            literal_write_domains,
            known_domains,
            graph,
            atom_idx,
        )
        if domain is None:
            if tag_name in known_domains:
                stateful[tag_name] = known_domains[tag_name]
            else:
                declared = _declared_domain(tag)
                if declared is not None:
                    stateful[tag_name] = declared
                else:
                    infeasible_tags.append(tag_name)
            continue
        if not domain:
            # An empty domain means expression analysis found no comparison
            # atoms — but an explicitly declared choices/min-max domain is
            # still authoritative, so fall back to it before giving up.
            if tag_name in known_domains:
                stateful[tag_name] = known_domains[tag_name]
            else:
                declared = _declared_domain(tag)
                if declared is not None:
                    stateful[tag_name] = declared
                elif (
                    tag_name in observed
                    and not observe_everything
                    and graph.writers_of.get(tag_name, frozenset())
                    & graph.all_readers_of.get(tag_name, frozenset())
                ):
                    # Read from outside the program (projected, or by a
                    # callable) and accumulated from its own value
                    # (``calc(N + 1, N)``): its values are the answer, and
                    # with no domain the search cannot track them.
                    infeasible_tags.append(tag_name)
                elif unclassified is not None:
                    unclassified.add(tag_name)
            continue
        if tag_name in consumed_accs:
            domain = _with_done_boundary(domain, tag, tag_name, done_acc_info, done_by_acc)
        stateful[tag_name] = domain
    for group in compared_groups:
        # A member that took every value it can hold adds them all as split
        # points for the others.
        members = sorted(name for name in group if name in nondeterministic)
        if len(members) > 1:
            _group_domains(members, nondeterministic, graph.tags)
    if profile:
        profile_last = _classify_profile_mark(timings, "tag_classification_loop", profile_last)

    for ptr_name in graph.pointer_tags:
        if (
            ptr_name in stateful
            or ptr_name in nondeterministic
            or ptr_name in combinational
            or ptr_name in infeasible_tags
        ):
            continue
        tag = graph.tags.get(ptr_name)
        if tag is None or tag.readonly:
            if exclusions is not None and tag is not None:
                exclusions[ptr_name] = "readonly"
            continue
        infeasible_tags.append(ptr_name)
    if profile:
        profile_last = _classify_profile_mark(timings, "pointer_completion", profile_last)

    for blocker_name in sorted(reverse_blockers):
        if blocker_name not in stateful and blocker_name not in nondeterministic:
            continue
        if blocker_name in infeasible_tags:
            continue
        infeasible_tags.append(blocker_name)
    if profile:
        profile_last = _classify_profile_mark(timings, "reverse_blocker_merge", profile_last)

    if infeasible_tags:
        total_dims = len(stateful) + len(nondeterministic) + len(infeasible_tags)
        blocker_map = {b.acc_name: b for b in threshold_absorptions.blockers}
        # Under black-box timers every such accumulator is state (above); only
        # how()'s domain prior leaves one infeasible.
        hints = _build_infeasible_hints(
            sorted(infeasible_tags),
            graph,
            blocker_map,
            None if abstract_timers else _unmodeled_accumulators(program),
            tag_compared_refused,
        )
        done_presets_partial = {d: p for d, p in done_acc_info.presets.items() if d in done_acc}
        done_presets_partial.update(
            {d: p for d, p in absorptions.synthetic_presets.items() if d in done_acc}
        )
        done_kinds_partial = {d: done_acc_info.kinds[d] for d in done_acc}
        result = Intractable(
            reason=f"unbounded domain on {', '.join(sorted(infeasible_tags))}",
            dimensions=total_dims,
            estimated_space=0,
            tags=sorted(infeasible_tags),
            hints=hints,
            _debug_context=(
                stateful,
                nondeterministic,
                frozenset(combinational),
                done_acc,
                done_presets_partial,
                done_kinds_partial,
            ),
        )
        emit_profile(
            "intractable",
            stateful=len(stateful),
            nondeterministic=len(nondeterministic),
            combinational=len(combinational),
            infeasible=len(infeasible_tags),
        )
        return result

    fn_escape = _detect_function_escape_hatches(program, graph)
    if profile:
        profile_last = _classify_profile_mark(timings, "function_escape_hatches", profile_last)
    if fn_escape:
        total_dims = len(stateful) + len(nondeterministic) + len(fn_escape)
        hints = [
            f"  {name}: function output; add choices=, min=/max=, or readonly=True"
            for name in sorted(fn_escape)
        ]
        done_presets_partial = {d: p for d, p in done_acc_info.presets.items() if d in done_acc}
        done_presets_partial.update(
            {d: p for d, p in absorptions.synthetic_presets.items() if d in done_acc}
        )
        done_kinds_partial = {d: done_acc_info.kinds[d] for d in done_acc}
        result = Intractable(
            reason=f"unannotated function output: {', '.join(sorted(fn_escape))}",
            dimensions=total_dims,
            estimated_space=0,
            tags=sorted(fn_escape),
            hints=hints,
            _debug_context=(
                stateful,
                nondeterministic,
                frozenset(combinational),
                done_acc,
                done_presets_partial,
                done_kinds_partial,
            ),
        )
        emit_profile(
            "function_escape",
            stateful=len(stateful),
            nondeterministic=len(nondeterministic),
            combinational=len(combinational),
            escapes=len(fn_escape),
        )
        return result

    done_presets = {d: p for d, p in done_acc_info.presets.items() if d in done_acc}
    done_presets.update({d: p for d, p in absorptions.synthetic_presets.items() if d in done_acc})
    done_kinds = {d: done_acc_info.kinds[d] for d in done_acc}
    result = (
        stateful,
        nondeterministic,
        frozenset(combinational),
        done_acc,
        done_presets,
        done_kinds,
    )
    if profile:
        _classify_profile_mark(timings, "done_metadata", profile_last)
        emit_profile(
            "ok",
            stateful=len(stateful),
            nondeterministic=len(nondeterministic),
            combinational=len(combinational),
            done=len(done_acc),
            threshold_vectors=len(threshold_absorptions.vector_specs),
        )
    return result


def _classify_dimensions(
    program: Program,
    scope: list[str] | None = None,
) -> _ClassifyResult | Intractable:
    """Partition tags into stateful, nondeterministic, and combinational.

    Returns ``(stateful_dims, nondeterministic_dims, combinational_tags,
    done_acc_pairs, done_presets, done_kinds)`` where each dim dict maps
    tag name to its value domain.  Timers are classified as the search sees
    them (black boxes): a Done bit is a plain Bool and an accumulator
    something reads is state.  Returns ``Intractable`` when a domain cannot be
    bounded.
    """
    from pyrung.core.analysis.prove.passes import _collect_receive_dest_names

    graph = build_program_graph(program)
    all_exprs = _collect_all_exprs(program, graph, scope=scope)
    return _classify_dimensions_from_graph(
        program,
        graph,
        all_exprs,
        scope=scope,
        receive_dest_names=frozenset(_collect_receive_dest_names(program)),
        abstract_timers=True,
        _skip_absorptions=True,
    )


def _has_data_feedback(tag_name: str, graph: ProgramGraph) -> bool:
    """Detect data-flow cycles through *tag_name*.

    Follows ``data_reads`` through writer rungs — condition-only reads do
    not count as data feedback.  Returns True for direct self-feed
    (e.g. ``calc(Count + 1, Count)``) and transitive cycles
    (e.g. ``calc(A + 1, B)`` plus ``copy(B, A)``).
    """
    writer_indices = graph.writers_of.get(tag_name, frozenset())
    if not writer_indices:
        return False
    for wi in writer_indices:
        node = graph.rung_nodes[wi]
        if tag_name in node.data_reads:
            return True
    visited: set[str] = set()
    queue: list[str] = []
    for wi in writer_indices:
        node = graph.rung_nodes[wi]
        for src in node.data_reads:
            if src != tag_name and src not in visited:
                queue.append(src)
    while queue:
        current = queue.pop()
        if current in visited:
            continue
        visited.add(current)
        for wi in graph.writers_of.get(current, frozenset()):
            node = graph.rung_nodes[wi]
            if tag_name in node.writes:
                return True
            for src in node.data_reads:
                if src not in visited:
                    queue.append(src)
    return False


def _pilot_sweep_domains(
    compiled: CompiledKernel,
    infeasible_tags: list[str],
    nondeterministic_dims: dict[str, tuple[Any, ...]],
    graph: ProgramGraph,
    *,
    dt: float = 0.010,
    max_combos: int = 100_000,
    max_domain: int = 1000,
    max_scans: int = 30,
) -> dict[str, tuple[Any, ...]]:
    """Discover finite domains for infeasible tags via kernel pilot sweep."""
    candidates: list[str] = []
    for tag_name in infeasible_tags:
        tag = graph.tags.get(tag_name)
        if tag is None:
            continue
        if tag.readonly:
            continue
        if tag_name not in graph.writers_of:
            if not (tag.external and tag.final):
                continue
        if tag.external and not tag.final:
            continue
        if _has_data_feedback(tag_name, graph):
            continue
        candidates.append(tag_name)

    if not candidates:
        return {}

    candidate_upstream: dict[str, dict[str, tuple[Any, ...]]] = {}
    for cname in candidates:
        upstream: dict[str, tuple[Any, ...]] = {}
        visited_rungs: set[int] = set()
        queue: list[str] = [cname]
        visited_tags: set[str] = set()
        while queue:
            cur = queue.pop()
            if cur in visited_tags:
                continue
            visited_tags.add(cur)
            for wi in graph.writers_of.get(cur, frozenset()):
                if wi in visited_rungs:
                    continue
                visited_rungs.add(wi)
                node = graph.rung_nodes[wi]
                for src in node.condition_reads | node.data_reads:
                    if src not in visited_tags:
                        queue.append(src)
        for t in visited_tags:
            if t in nondeterministic_dims:
                upstream[t] = nondeterministic_dims[t]
        candidate_upstream[cname] = upstream

    relevant_nd: dict[str, tuple[Any, ...]] = {}
    for up in candidate_upstream.values():
        for t, domain in up.items():
            if t not in relevant_nd:
                relevant_nd[t] = domain

    combo_count = 1
    for domain in relevant_nd.values():
        combo_count *= len(domain)
        if combo_count > max_combos:
            break

    if combo_count > max_combos:
        return {}

    observed: dict[str, set[Any]] = {c: set() for c in candidates}
    for c in candidates:
        tag = graph.tags[c]
        observed[c].add(tag.default)

    nd_names = sorted(relevant_nd)
    nd_domains = [relevant_nd[n] for n in nd_names]

    initial_kernel = compiled.create_kernel()
    initial_snap = _snapshot_kernel(initial_kernel)

    kernel = initial_kernel
    for combo in itertools.product(*nd_domains) if nd_domains else [()]:
        _restore_kernel(kernel, initial_snap)
        for name, val in zip(nd_names, combo, strict=True):
            kernel.tags[name] = val

        for _scan in range(max_scans):
            prev_sizes = tuple(len(observed[c]) for c in candidates)
            _step_compiled_kernel(compiled, kernel, dt=dt)
            for c in candidates:
                observed[c].add(kernel.tags.get(c, graph.tags[c].default))
            new_sizes = tuple(len(observed[c]) for c in candidates)
            if new_sizes == prev_sizes:
                break

    result: dict[str, tuple[Any, ...]] = {}
    for c in candidates:
        vals = observed[c]
        if len(vals) > max_domain:
            continue
        result[c] = tuple(sorted(vals))
    return result


def _detect_function_escape_hatches(
    program: Program,
    graph: ProgramGraph,
) -> list[str]:
    """Find function output tags that lack domain annotations."""
    from pyrung.core.instruction.control import (
        EnabledFunctionCallInstruction,
        FunctionCallInstruction,
    )
    from pyrung.core.tag import Tag
    from pyrung.core.validation._common import _collect_write_sites

    fn_targets: set[str] = set()

    def _fn_write_targets(instr: Any) -> list[tuple[str, str]]:
        if not isinstance(instr, (FunctionCallInstruction, EnabledFunctionCallInstruction)):
            return []
        targets: list[str] = []
        for target in instr._outs.values():
            if isinstance(target, Tag):
                targets.append(target.name)
        return [(name, type(instr).__name__) for name in targets]

    sites = _collect_write_sites(program, target_extractor=_fn_write_targets)
    for site in sites:
        fn_targets.add(site.target_name)

    infeasible: list[str] = []
    for name in fn_targets:
        tag = graph.tags.get(name)
        if tag is None:
            continue
        if tag.type == TagType.BOOL:
            continue
        if tag.choices is not None or (tag.min is not None and tag.max is not None):
            continue
        infeasible.append(name)
    return infeasible
