"""Backward reachability from a snapshot — ``why()`` mode.

Given a frozen PLC state (no history), walk the program graph backward
from target tag(s) using SP-tree attribution to reconstruct how the
state was reached.  Terminates at external inputs (no rung writes them).

Three branches per writer rung:

- **Stateless (OTE):** rung condition IS the explanation.  ``attribute()``
  gives minimal load-bearing contacts regardless of TRUE/FALSE evaluation.
- **Stateful (latch), trigger cleared:** rung is FALSE but tag holds its
  value.  Enumerate all SP-tree leaves as candidate triggers (ambiguous).
- **Stateful (latch), still active:** rung is TRUE.  ``attribute()`` is
  definitive, same as stateless.

Reset path: for latched tags currently TRUE, also check reset rungs to
explain why the latch hasn't cleared.

A rung that writes through a pointer listing no targets is a writer of the tag
in three cases.  ``_PointerWriters`` says which applies.  The scan history decides
when it can.  Otherwise a trial run of the next scan decides.  When neither can say,
the walk keeps the rung where the program's own values can aim the pointer
(``ProgramGraph.unlisted_writers_of``), and marks its step ``uncertain``.  The roots
under such a step go to ``ambiguous_roots``, unless a certain path also reaches them.
``CausalChain.confidence`` says how the ambiguous roots count.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.analysis.pdg import continuous_search_results, resolve_rung
from pyrung.core.analysis.sp_tree import attribute, evaluate_sp
from pyrung.core.state import value_changed

from .models import CausalChain, ChainStep, Transition
from .support import _collect_sp_leaves, _condition_tag_name, _HistoricalView

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph, RungNode
    from pyrung.core.condition import Condition
    from pyrung.core.executor import RungRun
    from pyrung.core.program import Program
    from pyrung.core.rung import Rung
    from pyrung.core.state import SystemState


def _snapshot_value(state: SystemState, tag_name: str, inferred: dict[str, Any] | None) -> Any:
    """Return snapshot value, falling back to back-propagated inference."""
    val = state.tags.get(tag_name)
    if val is not None:
        return val
    if inferred:
        return inferred.get(tag_name)
    return None


def why_cause(
    logic: list[Rung],
    state: SystemState,
    tags: list[str],
    pdg: ProgramGraph,
    *,
    program: Program | None = None,
    frontier: Callable[[str], bool] | None = None,
    forces: Mapping[str, Any] | None = None,
    pointer_write: Callable[[int, str, Any], bool | None] | None = None,
    trial_scan: Callable[[], tuple[RungRun, ...] | None] | None = None,
) -> CausalChain:
    """Build a diagnostic causal chain from a snapshot.

    Args:
        logic: The program's main rung list (kept for API compat;
            ignored when *program* is provided).
        state: Frozen PLC state (snapshot).
        tags: Tag names to explain (at least one).
        pdg: Static program dependency graph.
        program: Program for full subroutine resolution and
            inference integration (cone scoping, back-propagation,
            init-constant pinning, write-before-read skipping).
        frontier: Optional termination predicate.  When provided and
            ``frontier(tag_name)`` is True, the backward walk terminates
            at that tag — classified as a conjunctive root, exactly like
            the no-writers (external input) case — even though the tag
            has writers.  ``None`` preserves the default behavior
            (terminate only at external inputs / init constants).
        forces: The session's active forces, tag name to value.  The walk
            still shows a forced tag's rungs, since they are what the logic
            does; the chain names the forces it touches.
        pointer_write: Reads the scan history, if there is one.  It takes a node
            that writes through a pointer listing no targets, a tag and the tag's
            value.  It returns True when the node stored that value at the tag's last
            change, False when something else did, and None when the history cannot
            say.
        trial_scan: Runs the next scan from *state* on a throwaway copy of the PLC,
            with the pending forces and patches, and returns its rung runs in order.
            It returns None when no scan can run.

    Returns:
        A ``CausalChain`` with ``mode='why'``.
    """
    forces = forces or {}
    # Each walked tag, and whether a path of certain writers reached it.
    visited: dict[str, bool] = {}
    steps: list[ChainStep] = []
    conjunctive_roots: list[Transition] = []
    # The triggers of a rung that set the tag and no longer conducts.  One of them set it.
    ambiguous_roots: list[Transition] = []
    # The roots reached under a write through a pointer that may have stored elsewhere.
    maybe_roots: list[Transition] = []

    init_constants: frozenset[str] = frozenset()
    wbr_tags: frozenset[str] = frozenset()
    inferred: dict[str, Any] = {}

    if program is not None:
        resolver = _RungResolver(program, program.rungs)

        cone: set[str] = set()
        for t in tags:
            cone |= pdg.upstream_slice(t)
            cone.add(t)

        from pyrung.core.analysis.reverse_edges import (
            back_propagate_value,
            build_reverse_edge_map,
        )

        edge_map = build_reverse_edge_map(program)
        for t in tags:
            val = state.tags.get(t)
            # A forced value says nothing about what the logic copied in.
            if val is not None and t not in forces:
                for src, src_val in back_propagate_value(edge_map, t, val).items():
                    if src in cone:
                        inferred.setdefault(src, src_val)

        from pyrung.core.analysis.init_constants import detect_init_constants
        from pyrung.core.validation._common import _collect_write_sites

        sites_by_target: dict[str, list[Any]] = {}
        for site in _collect_write_sites(program):
            sites_by_target.setdefault(site.target_name, []).append(site)
        projected = detect_init_constants(
            program=program,
            graph=pdg,
            sites_by_target=sites_by_target,
            # A pointer write may store any value, so its tag holds no constant.
            candidate_tags={
                t for t in cone & set(pdg.writers_of.keys()) if not pdg.pointer_writers_of(t)
            },
        )
        init_constants = frozenset(projected.keys())

        # A timer's Acc, a drum's step and a continuous search's result carry over
        # from scan to scan, because their instruction reads them back before writing.
        # The def-use chains miss that read, so skipping these tags would drop a later
        # rung that writes them.
        read_back = frozenset().union(
            *(node.exclusive_reads for node in pdg.rung_nodes)
        ) | continuous_search_results(program)
        wbr: set[str] = set()
        for t in cone:
            if t not in tags and t not in read_back and pdg.unconditional_write_before_read(t):
                wbr.add(t)
        wbr_tags = frozenset(wbr)
    else:
        resolver = _RungResolver(None, list(logic))

    view = _HistoricalView(state)

    def snapshot_eval(cond: Condition) -> bool:
        return cond.evaluate(view)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    def conducts(node_idx: int) -> bool:
        sp_tree = resolver.resolve(pdg.rung_nodes[node_idx]).sp_tree()
        return sp_tree is None or evaluate_sp(sp_tree, snapshot_eval)

    trial = _Trial(pdg, program, resolver, trial_scan)
    pointer_writers = _PointerWriters(pdg, pointer_write, conducts, trial)

    for tag_name in tags:
        _walk_backward(
            resolver,
            state,
            pdg,
            tag_name,
            snapshot_eval,
            visited,
            steps,
            conjunctive_roots,
            ambiguous_roots,
            init_constants=init_constants,
            wbr_tags=wbr_tags,
            inferred=inferred,
            frontier=frontier,
            pointer_writers=pointer_writers,
            maybe_roots=maybe_roots,
        )

    primary = Transition(
        tag_name=tags[0],
        scan_id=0,
        from_value=None,
        to_value=state.tags.get(tags[0]),
    )
    effects = (
        [
            Transition(
                tag_name=t,
                scan_id=0,
                from_value=None,
                to_value=state.tags.get(t),
            )
            for t in tags
        ]
        if len(tags) > 1
        else []
    )

    choice_labels: dict[str, dict[Any, str]] = {}
    for name, tag_obj in pdg.tags.items():
        choices = getattr(tag_obj, "choices", None)
        if choices:
            choice_labels[name] = dict(choices.items())

    uncertain_roots = _uncertain_roots(conjunctive_roots, ambiguous_roots, maybe_roots)
    chain = CausalChain(
        effect=primary,
        mode="why",
        steps=_distinct_steps(steps),
        conjunctive_roots=conjunctive_roots,
        ambiguous_roots=uncertain_roots,
        effects=effects,
        choice_labels=choice_labels,
    )
    for name in chain.tags():
        if name not in forces:
            continue
        current = state.tags.get(name)
        if current is None and name in pdg.tags:
            current = pdg.tags[name].default
        held = chain.force_pending if value_changed(current, forces[name]) else chain.forced
        held[name] = forces[name]
    return chain


class _PointerWriters:
    """The writes through a pointer listing no targets that write a tag, as ``(node, uncertain)``.

    The scan history decides when it can, and otherwise a trial run of the next scan
    does.  A rung counts as a certain writer of the tag in three cases:

    - The rung does not conduct, and in the trial its pointer aims at the tag where the
      write sits.  It is then a writer that is blocked or holds, as a listed one is.
    - The scan history shows that it stored the value at or after the tag's last change.
    - The history cannot say, and the rung stores the tag's value in the trial.

    A rung stays uncertain when the trial cannot tell where its write would aim.  So does
    a rung that the history and the trial cannot speak for, where the program's own values
    can aim its pointer.
    """

    def __init__(
        self,
        pdg: ProgramGraph,
        pointer_write: Callable[[int, str, Any], bool | None] | None,
        conducts: Callable[[int], bool],
        trial: _Trial,
    ) -> None:
        self._pdg = pdg
        self._pointer_write = pointer_write
        self._conducts = conducts
        self._trial = trial
        self._found: dict[tuple[str, Any], list[tuple[int, bool]]] = {}

    def __call__(self, tag_name: str, value: Any) -> list[tuple[int, bool]]:
        key = (tag_name, repr(value))
        found = self._found.get(key)
        if found is not None:
            return found
        pdg = self._pdg
        listed = pdg.writers_of.get(tag_name, frozenset())
        aimed = pdg.unlisted_writers_of(tag_name)
        found = []
        for node_idx in sorted(pdg.pointer_writers_of(tag_name) - listed):
            conducts = self._conducts(node_idx)
            maybe = False
            if not conducts:
                hit, unsure = self._trial.aims_at(node_idx, tag_name)
                if hit and not unsure:
                    found.append((node_idx, False))
                    continue
                # The rung may still have stored the value when it last conducted.
                maybe = unsure and (hit or node_idx in aimed)
            stored = None
            if self._pointer_write is not None:
                stored = self._pointer_write(node_idx, tag_name, value)
            if stored is None and conducts and self._trial.stores(node_idx, tag_name, value):
                stored = True
            if stored:
                found.append((node_idx, False))
            elif maybe or (stored is None and node_idx in aimed):
                found.append((node_idx, True))
        self._found[key] = found
        return found


@dataclass
class _Aims:
    """Where one run of a rung's writes through a pointer aim."""

    #: Each tag a write aims at, with the tags its pointer reads.
    hits: dict[str, frozenset[str]] = field(default_factory=dict)
    #: A write's pointer may move before the write runs, or the write sits in a loop.
    unsure: bool = False


def _rung_aims(rung: Rung, nodes: Mapping[tuple[int, ...], int], view: Any) -> dict[int, _Aims]:
    """Where each write through a pointer aims, for each part of *rung* in *nodes*.

    *nodes* maps a branch path in *rung* to the node of that part.  The walk reads each
    pointer in *view*, the state as the rung starts.  An earlier write in the rung can move
    a pointer before its write runs.  So can a call or a loop.  Then that write is unsure.
    A loop moves its own pointer, so a write in a loop's body is unsure too.
    """
    from pyrung.core.analysis.pdg import _extract_tag_names
    from pyrung.core.analysis.write_sites import instruction_write_targets
    from pyrung.core.instruction import CallInstruction, ForLoopInstruction
    from pyrung.core.memory_block import IndirectBlockRange, IndirectExprRef, IndirectRef
    from pyrung.core.rung import Rung as RungClass
    from pyrung.core.tag import ImmediateRef, Tag

    found = {node: _Aims() for node in nodes.values()}
    written: set[str] = set()
    # An earlier write went where this walk cannot tell.
    lost = False

    def pointer_reads(dest: Any) -> frozenset[str]:
        if isinstance(dest, IndirectRef):
            return frozenset(_extract_tag_names(dest.pointer, {}))
        if isinstance(dest, IndirectExprRef):
            return frozenset(_extract_tag_names(dest.expr, {}))
        return frozenset(
            _extract_tag_names(dest.start_expr, {}) | _extract_tag_names(dest.end_expr, {})
        )

    def targets(dest: Any) -> list[str] | None:
        """The tags *dest* writes, or None when its pointer may have moved."""
        if isinstance(dest, (IndirectRef, IndirectExprRef, IndirectBlockRange)):
            if lost or pointer_reads(dest) & written:
                return None
            try:
                if isinstance(dest, IndirectBlockRange):
                    return [tag.name for tag in dest.resolve_ctx(view).tags()]
                element = dest.block._element_or_none(dest.address_ctx(view))
            except (ArithmeticError, IndexError, TypeError, ValueError):
                # The pointer aims outside the block, so the write stores nothing.
                return []
            return [] if element is None else [element.name]
        if isinstance(dest, Tag):
            return [dest.name]
        tags = getattr(dest, "tags", None)
        if callable(tags):
            return [tag.name for tag in tags()]
        return sorted(_extract_tag_names(dest, {}))

    def walk(part: Rung, path: tuple[int, ...]) -> None:
        nonlocal lost
        node = nodes.get(path)
        ordinal = 0
        for item in part._execution_items:
            if isinstance(item, RungClass):
                walk(item, (*path, ordinal))
                ordinal += 1
                continue
            if isinstance(item, (CallInstruction, ForLoopInstruction)):
                if node is not None and isinstance(item, ForLoopInstruction):
                    found[node].unsure = True
                lost = True
                continue
            # An instruction reads its pointers as it starts, before any write of its own.
            stores: list[str] = []
            for dest in instruction_write_targets(item):
                if isinstance(dest, ImmediateRef):
                    dest = dest.value
                names = targets(dest)
                if names is None:
                    lost = True
                    if node is not None:
                        found[node].unsure = True
                    continue
                stores.extend(names)
                if node is None or not isinstance(
                    dest, (IndirectRef, IndirectExprRef, IndirectBlockRange)
                ):
                    continue
                hits = found[node].hits
                for name in names:
                    hits[name] = hits.get(name, frozenset()) | pointer_reads(dest)
            written.update(stores)

    walk(rung, ())
    return found


class _Trial:
    """What a trial run of the next scan says about the writes through a pointer.

    The trial runs at most once per ``why()``, and only when a question needs it.
    """

    def __init__(
        self,
        pdg: ProgramGraph,
        program: Program | None,
        resolver: _RungResolver,
        trial_scan: Callable[[], tuple[RungRun, ...] | None] | None,
    ) -> None:
        self._pdg = pdg
        self._program = program
        self._resolver = resolver
        self._trial_scan = trial_scan
        self._ran: tuple[RungRun, ...] | None = None
        self._aims: dict[int, list[_Aims]] | None = None

    def _runs(self) -> tuple[RungRun, ...]:
        if self._ran is None:
            ran = None if self._trial_scan is None else self._trial_scan()
            self._ran = () if ran is None else ran
        return self._ran

    def stores(self, node_idx: int, tag_name: str, value: Any) -> bool:
        """Whether *node_idx* stores *value* into *tag_name* in the trial."""
        from pyrung.core.analysis.observed import writer_runs_for_node

        runs = self._runs()
        if not runs or self._program is None:
            return False
        return bool(writer_runs_for_node(self._pdg, self._program, node_idx, tag_name, value, runs))

    def aims_at(self, node_idx: int, tag_name: str) -> tuple[bool, bool]:
        """Whether a run of *node_idx* aims at *tag_name*, and whether a run is unsure.

        Each write's pointer is read as its rung starts in the trial.  A rung the trial
        did not reach is unsure.  That is a subroutine no rung called, or every rung when
        no trial ran.
        """
        if self._aims is None:
            self._aims = self._read_aims()
        runs = self._aims.get(node_idx)
        if not runs:
            return False, True
        return (
            any(tag_name in run.hits for run in runs),
            any(run.unsure for run in runs),
        )

    def _read_aims(self) -> dict[int, list[_Aims]]:
        pdg = self._pdg
        # Each rung that holds a write through an unlisted pointer, by identity, with the
        # branch path of each of its parts that does.
        rungs: dict[int, tuple[Rung, dict[tuple[int, ...], int]]] = {}
        for writes in pdg.unlisted_writes.values():
            for node_idx, _reach in writes:
                node = pdg.rung_nodes[node_idx]
                try:
                    top = self._resolver.resolve(replace(node, branch_path=()))
                except LookupError:
                    continue
                rungs.setdefault(id(top), (top, {}))[1][node.branch_path] = node_idx
        aims: dict[int, list[_Aims]] = {}
        for run in self._runs():
            entry = rungs.get(id(run.rung))
            if entry is None:
                continue
            # A rung's run reads its contacts in the state as the rung starts.
            for node_idx, found in _rung_aims(entry[0], entry[1], run.view).items():
                aims.setdefault(node_idx, []).append(found)
        return aims


def _distinct_steps(steps: list[ChainStep]) -> list[ChainStep]:
    """*steps* without the repeats that a second walk of a tag adds."""
    seen: set[tuple[Any, ...]] = set()
    kept: list[ChainStep] = []
    for step in steps:
        key = (
            step.transition.tag_name,
            step.rung_index,
            step.subroutine,
            step.kind,
            step.instruction,
            step.uncertain,
        )
        if key not in seen:
            seen.add(key)
            kept.append(step)
    return kept


def _uncertain_roots(
    certain: list[Transition], alternatives: list[Transition], maybes: list[Transition]
) -> list[Transition]:
    """The chain's ``ambiguous_roots``, which ``CausalChain.confidence`` counts.

    *alternatives* stay as listed, repeats and all.  Each tag of *maybes* is added once,
    unless another path also reached it as a root.
    """
    reached = {t.tag_name for t in (*certain, *alternatives)}
    roots = list(alternatives)
    for t in maybes:
        if t.tag_name not in reached:
            reached.add(t.tag_name)
            roots.append(t)
    return roots


class _RungResolver:
    """Resolve any PDG node to its ``Rung`` — main or subroutine.

    Delegates to :func:`~pyrung.core.analysis.pdg.resolve_rung` when a
    full *program* is available, with a fallback for the legacy
    ``program=None`` path (main rungs only).
    """

    __slots__ = ("_program", "_main")

    def __init__(self, program: Program | None, main_rungs: list[Rung]) -> None:
        self._program = program
        self._main = main_rungs

    def resolve(self, node: RungNode) -> Rung:
        if self._program is not None:
            result = resolve_rung(self._program, node)
            if result is not None:
                return result
            raise LookupError(f"Cannot resolve rung: {node.subroutine}[{node.rung_index}]")
        if node.subroutine is not None:
            raise LookupError(
                f"Cannot resolve subroutine rung without program: "
                f"{node.subroutine}[{node.rung_index}]"
            )
        rung = self._main[node.rung_index]
        for branch_idx in node.branch_path:
            rung = rung._branches[branch_idx]
        return rung


_INSTRUCTION_LABELS: dict[str, str] = {
    "OutInstruction": "out",
    "LatchInstruction": "latch",
    "ResetInstruction": "reset",
    "CopyInstruction": "copy",
    "CalcInstruction": "math",
    "OnDelayInstruction": "on_delay",
    "OffDelayInstruction": "off_delay",
    "CountUpInstruction": "count_up",
    "CountDownInstruction": "count_down",
    "EventDrumInstruction": "event_drum",
    "TimeDrumInstruction": "time_drum",
    "TimeEventDrumInstruction": "time_event_drum",
    "ToggleInstruction": "toggle",
    "DebounceInstruction": "debounce",
    "FlasherInstruction": "flasher",
    "TimedInstruction": "timed",
    "FillInstruction": "fill",
    "BlockCopyInstruction": "blockcopy",
    "PackBitsInstruction": "pack_bits",
    "PackTextInstruction": "pack_text",
    "UnpackToBitsInstruction": "unpack_to_bits",
    "PackWordsInstruction": "pack_words",
    "UnpackToWordsInstruction": "unpack_to_words",
    "SearchInstruction": "search",
    "ShiftInstruction": "shift",
    "FunctionCallInstruction": "run_function",
    "EnabledFunctionCallInstruction": "run_enabled_function",
    "ModbusSendInstruction": "send",
    "ModbusReceiveInstruction": "receive",
    "ForLoopInstruction": "forloop",
}


def _instruction_label(
    rung: Rung, tag_name: str, reaches: Callable[[Any], bool] | None = None
) -> str:
    """Derive the instruction name that writes *tag_name* in *rung*.

    *reaches* takes a block and says whether the rung's write through a pointer into it
    may store into the tag.  The caller passes it for a rung that writes the tag only
    through such a pointer.
    """
    from pyrung.core.analysis.pdg import _extract_instruction_writes
    from pyrung.core.analysis.write_sites import instruction_write_targets
    from pyrung.core.instruction.control import ForLoopInstruction
    from pyrung.core.memory_block import IndirectBlockRange, IndirectExprRef, IndirectRef
    from pyrung.core.tag import ImmediateRef

    def reaches_by_pointer(instr: Any) -> bool:
        if reaches is None:
            return False
        for dest in instruction_write_targets(instr):
            if isinstance(dest, ImmediateRef):
                dest = dest.value
            if isinstance(dest, (IndirectRef, IndirectExprRef, IndirectBlockRange)) and reaches(
                dest.block
            ):
                return True
        return False

    def label_for(instr: Any) -> str:
        cls_name = type(instr).__name__
        base = cls_name.replace("Oneshot", "")
        return _INSTRUCTION_LABELS.get(cls_name, _INSTRUCTION_LABELS.get(base, cls_name))

    def walk(instructions: list[Any]) -> str | None:
        tag_refs: dict[str, Any] = {}
        for instr in instructions:
            writes, _reads, implicit_writes = _extract_instruction_writes(instr, tag_refs)
            if tag_name in writes or tag_name in implicit_writes or reaches_by_pointer(instr):
                return label_for(instr)
            if isinstance(instr, ForLoopInstruction):
                child_label = walk(list(instr.instructions))
                if child_label is not None:
                    return child_label
        return None

    found = walk(list(rung._instructions))
    if found is not None:
        return found
    return "write"


def _is_reset_for_tag(rung: Rung, tag_name: str) -> bool:
    """True if *rung* contains a ``ResetInstruction`` targeting *tag_name*."""
    from pyrung.core.instruction.coils import ResetInstruction
    from pyrung.core.tag import ImmediateRef
    from pyrung.core.tag import Tag as TagClass

    for instr in rung._instructions:
        if not isinstance(instr, ResetInstruction):
            continue
        target = instr.target
        if isinstance(target, ImmediateRef):
            target = object.__getattribute__(target, "value")
        if isinstance(target, TagClass) and target.name == tag_name:
            return True
    return False


def _walk_backward(
    resolver: _RungResolver,
    state: SystemState,
    pdg: ProgramGraph,
    tag_name: str,
    snapshot_eval: Callable[[Condition], bool],
    visited: dict[str, bool],
    steps: list[ChainStep],
    conjunctive_roots: list[Transition],
    ambiguous_roots: list[Transition],
    *,
    init_constants: frozenset[str] = frozenset(),
    wbr_tags: frozenset[str] = frozenset(),
    inferred: dict[str, Any] | None = None,
    frontier: Callable[[str], bool] | None = None,
    pointer_writers: _PointerWriters,
    certain: bool = True,
    maybe_roots: list[Transition],
) -> None:
    if tag_name in wbr_tags:
        return

    writer_indices = pdg.writers_of.get(tag_name, frozenset())
    tag_value = _snapshot_value(state, tag_name, inferred)
    by_pointer = pointer_writers(tag_name, tag_value)

    if (
        not (writer_indices or by_pointer)
        or tag_name in init_constants
        or (frontier is not None and frontier(tag_name))
    ):
        conjunctive_roots.append(
            Transition(
                tag_name=tag_name,
                scan_id=0,
                from_value=None,
                to_value=_snapshot_value(state, tag_name, inferred),
            )
        )
        return

    # A tag first reached under an uncertain writer is walked again on a certain path,
    # so that its roots become conjunctive.
    if tag_name in visited and (visited[tag_name] or not certain):
        return
    visited[tag_name] = certain

    if not writer_indices and all(uncertain for _node, uncertain in by_pointer):
        # Every writer may have stored elsewhere, so the tag may hold a value no rung stored.
        maybe_roots.append(
            Transition(tag_name=tag_name, scan_id=0, from_value=None, to_value=tag_value)
        )

    set_writers: list[int] = []
    reset_writers: list[int] = []
    for node_idx in writer_indices:
        node = pdg.rung_nodes[node_idx]
        rung = resolver.resolve(node)
        if _is_reset_for_tag(rung, tag_name):
            reset_writers.append(node_idx)
        else:
            set_writers.append(node_idx)

    writers = [(i, False, False) for i in set_writers]
    writers += [(i, uncertain, True) for i, uncertain in by_pointer]
    for node_idx, uncertain, through_pointer in writers:
        node = pdg.rung_nodes[node_idx]
        rung = resolver.resolve(node)
        sp_tree = rung.sp_tree()
        is_ote = tag_name in node.ote_writes
        reaches = None
        if through_pointer:

            def reaches(block: Any, node_idx: int = node_idx) -> bool:
                return node_idx in pdg.pointer_writers_of(tag_name, (block,))

        instr_label = _instruction_label(rung, tag_name, reaches)
        # Every root under a writer that may have stored elsewhere is uncertain.
        roots, alternatives = (
            (maybe_roots, maybe_roots) if uncertain else (conjunctive_roots, ambiguous_roots)
        )

        if sp_tree is None:
            continue

        rung_fires = evaluate_sp(sp_tree, snapshot_eval)

        if is_ote:
            is_transient = tag_value is not None and bool(tag_value) != rung_fires
        else:
            is_transient = rung_fires and tag_value is not None and not tag_value

        if not is_ote and not rung_fires and tag_value:
            _walk_stateful_cleared(
                resolver,
                state,
                pdg,
                tag_name,
                tag_value,
                node,
                sp_tree,
                snapshot_eval,
                visited,
                steps,
                roots,
                alternatives,
                init_constants=init_constants,
                wbr_tags=wbr_tags,
                inferred=inferred,
                instruction=instr_label,
                uncertain=uncertain,
                frontier=frontier,
                pointer_writers=pointer_writers,
                certain=certain and not uncertain,
                maybe_roots=maybe_roots,
            )
        elif not is_ote and not rung_fires and not tag_value:
            _walk_attributed(
                resolver,
                state,
                pdg,
                tag_name,
                tag_value,
                node,
                sp_tree,
                snapshot_eval,
                visited,
                steps,
                roots,
                alternatives,
                init_constants=init_constants,
                wbr_tags=wbr_tags,
                inferred=inferred,
                kind="latch_blocked",
                instruction=instr_label,
                uncertain=uncertain,
                frontier=frontier,
                pointer_writers=pointer_writers,
                certain=certain and not uncertain,
                maybe_roots=maybe_roots,
            )
        else:
            kind = "transient" if is_transient else "attributed"
            _walk_attributed(
                resolver,
                state,
                pdg,
                tag_name,
                tag_value,
                node,
                sp_tree,
                snapshot_eval,
                visited,
                steps,
                roots,
                alternatives,
                init_constants=init_constants,
                wbr_tags=wbr_tags,
                inferred=inferred,
                kind=kind,
                instruction=instr_label,
                uncertain=uncertain,
                frontier=frontier,
                pointer_writers=pointer_writers,
                certain=certain and not uncertain,
                maybe_roots=maybe_roots,
            )

    if reset_writers:
        if tag_value:
            _walk_reset_path(
                resolver,
                state,
                pdg,
                tag_name,
                tag_value,
                reset_writers,
                snapshot_eval,
                steps,
            )
        else:
            _walk_reset_cause(
                resolver,
                state,
                pdg,
                tag_name,
                reset_writers,
                snapshot_eval,
                visited,
                steps,
                conjunctive_roots,
                ambiguous_roots,
                init_constants=init_constants,
                wbr_tags=wbr_tags,
                inferred=inferred,
                frontier=frontier,
                pointer_writers=pointer_writers,
                certain=certain,
                maybe_roots=maybe_roots,
            )


def _walk_stateful_cleared(
    resolver: _RungResolver,
    state: SystemState,
    pdg: ProgramGraph,
    tag_name: str,
    tag_value: Any,
    node: RungNode,
    sp_tree: Any,
    snapshot_eval: Callable[[Condition], bool],
    visited: dict[str, bool],
    steps: list[ChainStep],
    conjunctive_roots: list[Transition],
    ambiguous_roots: list[Transition],
    *,
    init_constants: frozenset[str] = frozenset(),
    wbr_tags: frozenset[str] = frozenset(),
    inferred: dict[str, Any] | None = None,
    instruction: str | None = None,
    frontier: Callable[[str], bool] | None = None,
    uncertain: bool = False,
    pointer_writers: _PointerWriters,
    certain: bool = True,
    maybe_roots: list[Transition],
) -> None:
    """Handle stateful writer whose trigger has cleared."""
    leaves = _collect_sp_leaves(sp_tree)
    triggers: list[Transition] = []

    for leaf in leaves:
        contact_tag = _condition_tag_name(leaf.condition)
        if contact_tag is None:
            continue
        contact_transition = Transition(
            tag_name=contact_tag,
            scan_id=0,
            from_value=None,
            to_value=_snapshot_value(state, contact_tag, inferred),
        )
        triggers.append(contact_transition)
        if not (
            pdg.writers_of.get(contact_tag)
            or pointer_writers(contact_tag, contact_transition.to_value)
        ):
            ambiguous_roots.append(contact_transition)
        else:
            _walk_backward(
                resolver,
                state,
                pdg,
                contact_tag,
                snapshot_eval,
                visited,
                steps,
                conjunctive_roots,
                ambiguous_roots,
                init_constants=init_constants,
                wbr_tags=wbr_tags,
                inferred=inferred,
                frontier=frontier,
                pointer_writers=pointer_writers,
                certain=certain,
                maybe_roots=maybe_roots,
            )

    steps.append(
        ChainStep(
            transition=Transition(
                tag_name=tag_name,
                scan_id=0,
                from_value=None,
                to_value=tag_value,
            ),
            rung_index=node.rung_index,
            triggers=tuple(triggers),
            enablers=(),
            fidelity="structural",
            kind="trigger_cleared",
            instruction=instruction,
            subroutine=node.subroutine,
            uncertain=uncertain,
        )
    )


def _walk_attributed(
    resolver: _RungResolver,
    state: SystemState,
    pdg: ProgramGraph,
    tag_name: str,
    tag_value: Any,
    node: RungNode,
    sp_tree: Any,
    snapshot_eval: Callable[[Condition], bool],
    visited: dict[str, bool],
    steps: list[ChainStep],
    conjunctive_roots: list[Transition],
    ambiguous_roots: list[Transition],
    *,
    init_constants: frozenset[str] = frozenset(),
    wbr_tags: frozenset[str] = frozenset(),
    inferred: dict[str, Any] | None = None,
    kind: Literal[
        "attributed",
        "trigger_cleared",
        "latch_blocked",
        "reset_blocked",
        "reset_active",
        "reset_inconsistent",
        "transient",
    ] = "attributed",
    instruction: str | None = None,
    frontier: Callable[[str], bool] | None = None,
    uncertain: bool = False,
    pointer_writers: _PointerWriters,
    certain: bool = True,
    maybe_roots: list[Transition],
) -> None:
    """Handle stateless writer or active stateful writer via attribution."""
    attributions = attribute(sp_tree, snapshot_eval)
    triggers: list[Transition] = []

    for attr in attributions:
        contact_tag = _condition_tag_name(attr.condition)
        if contact_tag is None:
            continue
        contact_transition = Transition(
            tag_name=contact_tag,
            scan_id=0,
            from_value=None,
            to_value=_snapshot_value(state, contact_tag, inferred),
        )
        triggers.append(contact_transition)
        if not (
            pdg.writers_of.get(contact_tag)
            or pointer_writers(contact_tag, contact_transition.to_value)
        ):
            conjunctive_roots.append(contact_transition)
        else:
            _walk_backward(
                resolver,
                state,
                pdg,
                contact_tag,
                snapshot_eval,
                visited,
                steps,
                conjunctive_roots,
                ambiguous_roots,
                init_constants=init_constants,
                wbr_tags=wbr_tags,
                inferred=inferred,
                frontier=frontier,
                pointer_writers=pointer_writers,
                certain=certain,
                maybe_roots=maybe_roots,
            )

    steps.append(
        ChainStep(
            transition=Transition(
                tag_name=tag_name,
                scan_id=0,
                from_value=None,
                to_value=tag_value,
            ),
            rung_index=node.rung_index,
            triggers=tuple(triggers),
            enablers=(),
            fidelity="structural",
            kind=kind,
            instruction=instruction,
            subroutine=node.subroutine,
            uncertain=uncertain,
        )
    )


def _walk_reset_path(
    resolver: _RungResolver,
    state: SystemState,
    pdg: ProgramGraph,
    tag_name: str,
    tag_value: Any,
    reset_writer_indices: list[int],
    snapshot_eval: Callable[[Condition], bool],
    steps: list[ChainStep],
) -> None:
    """Explain why reset rungs haven't cleared a latched tag."""
    for node_idx in reset_writer_indices:
        node = pdg.rung_nodes[node_idx]
        rung = resolver.resolve(node)
        sp_tree = rung.sp_tree()

        if sp_tree is None:
            continue

        rung_fires = evaluate_sp(sp_tree, snapshot_eval)
        transition = Transition(
            tag_name=tag_name,
            scan_id=0,
            from_value=None,
            to_value=tag_value,
        )

        if rung_fires:
            steps.append(
                ChainStep(
                    transition=transition,
                    rung_index=node.rung_index,
                    triggers=(),
                    enablers=(),
                    fidelity="structural",
                    kind="reset_inconsistent",
                    instruction="reset",
                    subroutine=node.subroutine,
                )
            )
        else:
            attributions = attribute(sp_tree, snapshot_eval)
            reset_blockers: list[Transition] = []
            for attr in attributions:
                contact_tag = _condition_tag_name(attr.condition)
                if contact_tag is not None:
                    reset_blockers.append(
                        Transition(
                            tag_name=contact_tag,
                            scan_id=0,
                            from_value=None,
                            to_value=state.tags.get(contact_tag),
                        )
                    )
            steps.append(
                ChainStep(
                    transition=transition,
                    rung_index=node.rung_index,
                    triggers=tuple(reset_blockers),
                    enablers=(),
                    fidelity="structural",
                    kind="reset_blocked",
                    instruction="reset",
                    subroutine=node.subroutine,
                )
            )


def _walk_reset_cause(
    resolver: _RungResolver,
    state: SystemState,
    pdg: ProgramGraph,
    tag_name: str,
    reset_writer_indices: list[int],
    snapshot_eval: Callable[[Condition], bool],
    visited: dict[str, bool],
    steps: list[ChainStep],
    conjunctive_roots: list[Transition],
    ambiguous_roots: list[Transition],
    *,
    init_constants: frozenset[str] = frozenset(),
    wbr_tags: frozenset[str] = frozenset(),
    inferred: dict[str, Any] | None = None,
    frontier: Callable[[str], bool] | None = None,
    pointer_writers: _PointerWriters,
    certain: bool = True,
    maybe_roots: list[Transition],
) -> None:
    """Explain why a latch tag is FALSE by finding active reset rungs."""
    for node_idx in reset_writer_indices:
        node = pdg.rung_nodes[node_idx]
        rung = resolver.resolve(node)
        sp_tree = rung.sp_tree()

        if sp_tree is None:
            continue

        rung_fires = evaluate_sp(sp_tree, snapshot_eval)
        if not rung_fires:
            continue

        attributions = attribute(sp_tree, snapshot_eval)
        triggers: list[Transition] = []
        for attr in attributions:
            contact_tag = _condition_tag_name(attr.condition)
            if contact_tag is None:
                continue
            contact_transition = Transition(
                tag_name=contact_tag,
                scan_id=0,
                from_value=None,
                to_value=_snapshot_value(state, contact_tag, inferred),
            )
            triggers.append(contact_transition)
            if not (
                pdg.writers_of.get(contact_tag)
                or pointer_writers(contact_tag, contact_transition.to_value)
            ):
                conjunctive_roots.append(contact_transition)
            else:
                _walk_backward(
                    resolver,
                    state,
                    pdg,
                    contact_tag,
                    snapshot_eval,
                    visited,
                    steps,
                    conjunctive_roots,
                    ambiguous_roots,
                    init_constants=init_constants,
                    wbr_tags=wbr_tags,
                    inferred=inferred,
                    frontier=frontier,
                    pointer_writers=pointer_writers,
                    certain=certain,
                    maybe_roots=maybe_roots,
                )

        steps.append(
            ChainStep(
                transition=Transition(
                    tag_name=tag_name,
                    scan_id=0,
                    from_value=None,
                    to_value=False,
                ),
                rung_index=node.rung_index,
                triggers=tuple(triggers),
                enablers=(),
                fidelity="structural",
                kind="reset_active",
                instruction="reset",
                subroutine=node.subroutine,
            )
        )
